"""Calibration: join stored verdicts to what happened afterwards. Only prices after the verdict's price date are used;
verdicts younger than the horizon stay pending."""
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from fundabot.ingest import prices as prices_mod

log = logging.getLogger("fundabot")
SUB_SCORES = ["composite_score", "quality_score", "value_score", "earnings_momentum_score"]


def candidates(conn: psycopg.Connection, horizon_days: int, today: date | None = None) -> list[dict]:
    """Verdicts (full) and cheap runs whose price date is at least `horizon_days` old and have no outcome yet."""
    today = today or date.today()
    cutoff = today - timedelta(days=horizon_days)
    out = []
    with conn.cursor() as cur:
        cur.execute("""SELECT s.id, s.ticker, s.cik, s.price_date, v.verdict FROM snapshots s JOIN verdicts v ON v.snapshot_id = s.id AND v.run_kind = 'full'
                       WHERE s.price_date <= %s AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.snapshot_id = s.id AND o.run_kind = 'full' AND o.horizon_days = %s)""",
                    (cutoff, horizon_days))
        for sid, ticker, cik, t0, v in cur.fetchall():
            agents = [(c["score"], c.get("confidence", 0.5)) for c in (v.get("grid") or {}).values()]
            out.append({"snapshot_id": sid, "ticker": ticker, "cik": cik, "t0": t0, "run_kind": "full", "composite_score": v.get("composite_score"),
                        **{k: v.get(k) for k in ("quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score")}, "agents": agents})
        cur.execute("""SELECT s.id, s.ticker, s.cik, s.price_date, json_agg(json_build_object('score', a.grounded->'score', 'confidence', a.grounded->'confidence'))
                       FROM snapshots s JOIN analyses a ON a.snapshot_id = s.id AND a.status = 'ok' AND a.grounded IS NOT NULL
                       WHERE s.price_date <= %s AND NOT EXISTS (SELECT 1 FROM verdicts v WHERE v.snapshot_id = s.id)
                         AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.snapshot_id = s.id AND o.run_kind = 'cheap' AND o.horizon_days = %s)
                       GROUP BY s.id, s.ticker, s.cik, s.price_date""", (cutoff, horizon_days))
        for sid, ticker, cik, t0, agents in cur.fetchall():
            scores = [a["score"] for a in agents if a["score"] is not None]
            out.append({"snapshot_id": sid, "ticker": ticker, "cik": cik, "t0": t0, "run_kind": "cheap", "composite_score": sum(scores) / len(scores) if scores else None,
                        "quality_score": None, "value_score": None, "earnings_momentum_score": None, "accounting_risk_score": None,
                        "agents": [(a["score"], a.get("confidence") or 0.5) for a in agents if a["score"] is not None]})
    return out


def compute_outcome(prices_df: pd.DataFrame, spy_df: pd.DataFrame, t0: date, horizon_days: int, surprises_df: pd.DataFrame) -> dict:
    """Returns from the first close after t0 ... no: from the close on t0 (or the last before it) to the last close on or before t0 + horizon."""
    t1 = t0 + timedelta(days=horizon_days)

    def window(df):
        base = df[df.date <= t0].sort_values("date").tail(1)
        after = df[(df.date > t0) & (df.date <= t1)].sort_values("date")
        return base, after
    base, after = window(prices_df)
    out = {"t0": t0, "t1": None, "ticker_return": None, "spy_return": None, "excess_return": None, "max_drawdown": None, "next_eps_surprise": None, "reason": None}
    if base.empty or after.empty:
        out["reason"] = "no_prices_after_t0"
    else:
        p0, path = float(base.close.iloc[0]), after.close.astype(float).values
        out["t1"] = after.date.iloc[-1]
        out["ticker_return"] = float(path[-1] / p0 - 1)
        full = np.concatenate([[p0], path])
        out["max_drawdown"] = float((full / np.maximum.accumulate(full) - 1).min())
        sbase, safter = window(spy_df)
        if not sbase.empty and not safter.empty:
            out["spy_return"] = float(safter.close.iloc[-1] / sbase.close.iloc[0] - 1)
            out["excess_return"] = out["ticker_return"] - out["spy_return"]
        else:
            out["reason"] = "no_benchmark_prices"
    if len(surprises_df):
        nxt = surprises_df[surprises_df.report_date > t0].sort_values("report_date").head(1)
        if len(nxt) and nxt.iloc[0].eps_estimated:
            r = nxt.iloc[0]
            out["next_eps_surprise"] = float((r.eps_actual - r.eps_estimated) / abs(r.eps_estimated))
    return out


def _spearman(x, y) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = pd.Series(x).rank(), pd.Series(y).rank()
    return float(np.corrcoef(rx, ry)[0, 1])


def statistics(rows: list[dict]) -> dict:
    rows = [r for r in rows if r.get("excess_return") is not None]
    if len(rows) < 10:
        return {"insufficient_history": True, "n": len(rows)}
    ex = [r["excess_return"] for r in rows]
    rho = {}
    for k in SUB_SCORES:
        pairs = [(r[k], r["excess_return"]) for r in rows if r.get(k) is not None]
        rho[k] = _spearman([p[0] for p in pairs], [p[1] for p in pairs]) if len(pairs) >= 3 else None
    by_comp = sorted((r for r in rows if r.get("composite_score") is not None), key=lambda r: r["composite_score"])
    k = max(1, len(by_comp) // 3)
    tercile = float(np.mean([r["excess_return"] for r in by_comp[-k:]]) - np.mean([r["excess_return"] for r in by_comp[:k]])) if by_comp else None
    dd = [(r["accounting_risk_score"], r["max_drawdown"]) for r in rows if r.get("accounting_risk_score") is not None and r.get("max_drawdown") is not None]
    rho_dd = _spearman([d[0] for d in dd], [d[1] for d in dd]) if len(dd) >= 3 else None
    mom = [(r["earnings_momentum_score"], r["next_eps_surprise"]) for r in rows if r.get("earnings_momentum_score") is not None and r.get("next_eps_surprise") is not None]
    high = [s for m, s in mom if m > 60]
    hit = float(np.mean([s > 0 for s in high])) if high else None
    briers = []
    for r in rows:
        up = r["excess_return"] > 0
        for score, conf in r.get("agents", []):
            p = 0.5 + (score / 10) * (conf or 0.5) / 2
            briers.append((p - (1.0 if up else 0.0)) ** 2)
    return {"insufficient_history": False, "n": len(rows), "rho": rho, "tercile_spread": tercile, "rho_accounting_risk_vs_drawdown": rho_dd,
            "momentum_hit_rate": hit, "brier": float(np.mean(briers)) if briers else None, "mean_excess_return": float(np.mean(ex))}


def _benchmark(conn, fetch_prices) -> pd.DataFrame:
    df = fetch_prices("SPY")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO benchmark_prices (ticker, date, close) VALUES ('SPY', %s, %s) ON CONFLICT (ticker, date) DO UPDATE SET close = EXCLUDED.close",
                        [(r.date, float(r.close)) for r in df.itertuples(index=False)])
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT date, close FROM benchmark_prices WHERE ticker = 'SPY' ORDER BY date")
        out = pd.DataFrame(cur.fetchall(), columns=["date", "close"])
    out["close"] = out["close"].astype(float)
    return out


def run(conn: psycopg.Connection, horizons=(30, 90, 180), fetch_prices=prices_mod.fetch_daily_closes, today: date | None = None) -> dict[int, dict]:
    from fundabot.ingest import fmp

    today = today or date.today()
    try:
        spy = _benchmark(conn, fetch_prices)
    except prices_mod.PriceUnavailable as e:  # no benchmark, no outcomes; say so instead of a traceback
        log.warning("calibration: %s", e)
        return {h: {"insufficient_history": True, "n": 0, "error": f"benchmark prices unavailable: {e}"} for h in horizons}
    results = {}
    for h in horizons:
        for c in candidates(conn, h, today):
            px = prices_mod.load(conn, c["cik"])
            sur = fmp.load(conn, c["cik"])["surprises"]
            o = compute_outcome(px, spy, c["t0"], h, sur)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO outcomes (snapshot_id, run_kind, horizon_days, t0, t1, ticker_return, spy_return, excess_return, next_eps_surprise, max_drawdown, reason) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (c["snapshot_id"], c["run_kind"], h, o["t0"], o["t1"], o["ticker_return"], o["spy_return"], o["excess_return"], o["next_eps_surprise"], o["max_drawdown"], o["reason"]))
            conn.commit()
        with conn.cursor() as cur:
            cur.execute("""SELECT o.snapshot_id, o.run_kind, o.excess_return, o.max_drawdown, o.next_eps_surprise, v.verdict FROM outcomes o
                           LEFT JOIN verdicts v ON v.snapshot_id = o.snapshot_id AND v.run_kind = 'full' WHERE o.horizon_days = %s AND o.excess_return IS NOT NULL""", (h,))
            rows = []
            for sid, kind, ex, dd, sup, v in cur.fetchall():
                v = v or {}
                agents = [(c["score"], c.get("confidence", 0.5)) for c in (v.get("grid") or {}).values()]
                rows.append({"composite_score": v.get("composite_score"), "quality_score": v.get("quality_score"), "value_score": v.get("value_score"),
                             "earnings_momentum_score": v.get("earnings_momentum_score"), "accounting_risk_score": v.get("accounting_risk_score"),
                             "excess_return": float(ex), "max_drawdown": None if dd is None else float(dd), "next_eps_surprise": None if sup is None else float(sup), "agents": agents})
        stats = statistics(rows)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO calibration_runs (horizon_days, n, stats) VALUES (%s, %s, %s)", (h, stats["n"], Jsonb(stats)))
        conn.commit()
        results[h] = stats
    return results
