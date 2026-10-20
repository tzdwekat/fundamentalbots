"""Price picture in code: window returns, moving averages, largest moves matched to events, and the outlook rule."""
from dataclasses import dataclass
from datetime import date, timedelta
from math import log, sqrt

import pandas as pd
import psycopg

from fundabot.analytics.metrics import Metric
from fundabot.ingest import edgar_filings
from fundabot.ingest.prices import close_on_or_before

EVENT_FORMS = ("10-K", "10-Q", "8-K", "10-K/A", "10-Q/A")
WINDOWS = {"ret_1m": 30, "ret_3m": 91, "ret_6m": 182, "ret_12m": 365}


@dataclass
class Event:
    date: date
    kind: str  # filing | earnings | transcript | split | news
    id: str
    title: str
    url: str | None = None


def close_on_or_before_date(df: pd.DataFrame, d: date) -> float | None:
    hit = close_on_or_before(df, d)
    return hit[1] if hit else None


def price_stats(closes: pd.DataFrame, asof: date | None = None) -> dict:
    """closes: `prices.load` output (date, close) sorted ascending. Fields whose window is missing are None."""
    df = closes if asof is None else closes[closes.date <= asof]
    df = df.sort_values("date").reset_index(drop=True)
    if df.empty:
        return {"asof": None, "close": None, "n_days": 0, **{k: None for k in ("ret_1m", "ret_3m", "ret_6m", "ret_12m", "high_52w", "low_52w", "drawdown_52w", "dist_52w_low", "ma50", "ma200", "above_ma50", "above_ma200", "ma50_slope_20d", "vol_20d")}}
    last_date, close = df.date.iloc[-1], float(df.close.iloc[-1])
    s = {"asof": last_date, "close": close, "n_days": int(len(df))}
    for key, days in WINDOWS.items():
        base = close_on_or_before_date(df, last_date - timedelta(days=days)) if df.date.iloc[0] <= last_date - timedelta(days=days) else None
        s[key] = (close / base - 1) if base else None
    if df.date.iloc[0] <= last_date - timedelta(days=365):
        year = df[df.date >= last_date - timedelta(days=365)]
        s["high_52w"], s["low_52w"] = float(year.close.max()), float(year.close.min())
        s["drawdown_52w"], s["dist_52w_low"] = close / s["high_52w"] - 1, close / s["low_52w"] - 1
    else:  # under a year of history: no 52-week figures rather than a shorter span dressed as one
        s["high_52w"] = s["low_52w"] = s["drawdown_52w"] = s["dist_52w_low"] = None
    c = df.close.astype(float)
    s["ma50"] = float(c.iloc[-50:].mean()) if len(c) >= 50 else None
    s["ma200"] = float(c.iloc[-200:].mean()) if len(c) >= 200 else None
    s["above_ma50"] = (close > s["ma50"]) if s["ma50"] is not None else None
    s["above_ma200"] = (close > s["ma200"]) if s["ma200"] is not None else None
    s["ma50_slope_20d"] = (s["ma50"] / float(c.iloc[-70:-20].mean()) - 1) if len(c) >= 70 else None
    if len(c) >= 21:
        rets = [log(c.iloc[i] / c.iloc[i - 1]) for i in range(len(c) - 20, len(c))]
        mean = sum(rets) / len(rets)
        s["vol_20d"] = sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * sqrt(252)
    else:
        s["vol_20d"] = None
    return s


def largest_moves(closes: pd.DataFrame, window_days: int, n: int = 10, asof: date | None = None) -> list[dict]:
    df = closes if asof is None else closes[closes.date <= asof]
    df = df.sort_values("date").reset_index(drop=True)
    if len(df) < 2:
        return []
    start = df.date.iloc[-1] - timedelta(days=window_days)
    rows = []
    for i in range(1, len(df)):
        if df.date[i] < start:
            continue
        ret = float(df.close[i]) / float(df.close[i - 1]) - 1
        rows.append({"id": f"price:{df.date[i]}", "date": df.date[i], "close": float(df.close[i]), "ret": ret})
    return sorted(rows, key=lambda r: -abs(r["ret"]))[:n]


def match_events(moves: list[dict], events: list[Event], trading_dates: list[date], within: int = 2) -> list[dict]:
    """Each move gets the events within `within` trading days (either side), oldest first."""
    idx = {d: i for i, d in enumerate(sorted(trading_dates))}
    order = sorted(idx)

    def pos(d: date) -> int:  # index of the last trading day on or before d
        lo, hi = 0, len(order) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if order[mid] <= d:
                lo = mid
            else:
                hi = mid - 1
        return lo
    out = []
    for m in moves:
        p = pos(m["date"])
        near = [e for e in events if abs(pos(e.date) - p) <= within]
        out.append({**m, "events": sorted(near, key=lambda e: (e.date, e.id))})
    return out


def events_for(conn: psycopg.Connection, cik: int, ticker: str, start: date, end: date, list_filings=edgar_filings.list_filings) -> list[Event]:
    """Every dated thing that can explain a move: EDGAR filings (the submissions list, 8-Ks included), earnings reports,
    transcripts, splits and stored news."""
    events: list[Event] = []
    filings = list_filings(cik)
    for r in filings.itertuples():
        if r.form in EVENT_FORMS and start <= r.filed <= end:
            events.append(Event(r.filed, "filing", f"filing:{r.accession}", r.form, getattr(r, "primary_doc_url", None)))
    with conn.cursor() as cur:
        cur.execute("SELECT report_date, eps_actual, eps_estimated FROM surprises WHERE cik = %s AND report_date BETWEEN %s AND %s", (cik, start, end))
        for d, a, e in cur.fetchall():
            events.append(Event(d, "earnings", f"earnings:{d}", f"EPS {a} vs {e}" if a is not None else "Earnings report"))
        cur.execute("SELECT fy, fq, call_date FROM transcripts WHERE cik = %s AND call_date BETWEEN %s AND %s", (cik, start, end))
        for fy, fq, d in cur.fetchall():
            events.append(Event(d, "transcript", f"transcript:T{fy}Q{fq}", f"Earnings call FY{fy} Q{fq}"))
        cur.execute("SELECT date, ratio FROM splits WHERE cik = %s AND date BETWEEN %s AND %s", (cik, start, end))
        for d, ratio in cur.fetchall():
            events.append(Event(d, "split", f"split:{d}", f"{ratio:g}-for-1 split"))
        cur.execute("SELECT id, published, title, url FROM news WHERE cik = %s AND published::date BETWEEN %s AND %s", (cik, start, end))
        for nid, published, title, url in cur.fetchall():
            events.append(Event(published.date(), "news", f"news:{nid}", title, url))
        cur.execute("SELECT date, company, previous_grade, new_grade, action FROM grades WHERE cik = %s AND date BETWEEN %s AND %s AND action IN ('upgrade', 'downgrade')", (cik, start, end))
        for d, company, prev, new, action in cur.fetchall():
            events.append(Event(d, "grade", f"grade:{d}:{company}", f"{company}: {prev} -> {new} ({action})"))
    return sorted(events, key=lambda e: (e.date, e.id))


def _last(metrics: list[Metric], mid: str, period: str = "current") -> float | None:
    return next((m.value for m in metrics if m.metric_id == mid and m.period == period), None)


def _quarterly(metrics: list[Metric], mid: str, n: int = 4) -> list[float]:
    rows = [m for m in metrics if m.metric_id == mid and m.period.startswith("Q") and m.value is not None]
    rows.sort(key=lambda m: (int(m.period[4:]), int(m.period[1])))
    return [float(m.value) for m in rows[-n:]]


def _direction(series: list[float], tol: float = 0.02) -> str | None:
    if len(series) < 4:
        return None
    recent, earlier = sum(series[-2:]) / 2, sum(series[-4:-2]) / 2
    return "accelerating" if recent > earlier + tol else "decelerating" if recent < earlier - tol else "steady"


RULES = [  # (label, predicate over (momentum, trend, valuation)); first match wins
    ("growing / extended", lambda m, t, v: m in ("accelerating", "steady") and t == "up" and v == "stretched"),
    ("growing / fair", lambda m, t, v: m in ("accelerating", "steady") and t == "up"),
    ("bottoming", lambda m, t, v: t == "turning_up"),
    ("declining", lambda m, t, v: m == "decelerating" and t == "down"),
    ("decelerating", lambda m, t, v: m == "decelerating"),
    ("mixed", lambda m, t, v: True),
]


def outlook(stats: dict, metrics: list[Metric]) -> dict:
    """Three measured components and a label from RULES; no model, no forecast."""
    rev, eps = _quarterly(metrics, "revenue_yoy"), _quarterly(metrics, "eps_diluted_yoy")
    momentum = {"revenue_yoy": rev, "eps_diluted_yoy": eps, "direction": _direction(rev),
                "eps_beat_streak": _last(metrics, "eps_beat_streak"), "eps_estimate_revision_90d": _last(metrics, "eps_estimate_revision_90d")}
    pe_pct, evs_pct = _last(metrics, "pe_5y_percentile"), _last(metrics, "ev_sales_5y_percentile")
    pct = pe_pct if pe_pct is not None else evs_pct
    valuation = {"pe_5y_percentile": pe_pct, "ev_sales_5y_percentile": evs_pct,
                 "state": None if pct is None else "stretched" if pct >= 0.8 else "cheap" if pct <= 0.2 else "fair"}
    if stats.get("ma200") is None or stats.get("ma50") is None or stats.get("ma50_slope_20d") is None:
        trend_state = None
    elif stats["above_ma50"] and stats["above_ma200"] and stats["ma50_slope_20d"] > 0:
        trend_state = "up"
    elif not stats["above_ma50"] and not stats["above_ma200"] and stats["ma50_slope_20d"] < 0:
        trend_state = "down"
    elif not stats["above_ma200"] and stats["ma50_slope_20d"] > 0 and (stats.get("drawdown_52w") if stats.get("drawdown_52w") is not None else 0) < -0.2:
        trend_state = "turning_up"
    else:
        trend_state = "mixed"
    trend = {k: stats.get(k) for k in ("close", "ma50", "ma200", "above_ma50", "above_ma200", "ma50_slope_20d", "drawdown_52w")}
    trend["state"] = trend_state
    components = {"momentum": momentum, "valuation": valuation, "trend": trend}
    if momentum["direction"] is None or valuation["state"] is None or trend_state is None:
        return {"label": "insufficient data", "components": components, "rule": "a component is missing (needs 4 quarters of growth, a 5y valuation percentile and 200 days of prices)"}
    for label, pred in RULES:
        if pred(momentum["direction"], trend_state, valuation["state"]):
            return {"label": label, "components": components, "rule": f"momentum {momentum['direction']}, trend {trend_state}, valuation {valuation['state']} -> {label}"}
    raise AssertionError("unreachable: RULES ends with a catch-all")
