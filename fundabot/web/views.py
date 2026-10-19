"""Page data, assembled from stored structures and the code-computed price picture. No model calls here."""
import logging
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import psycopg

from fundabot.agents.context import metric_line
from fundabot.analytics import moves, technicals
from fundabot.analytics.metrics import Metric
from fundabot.config import settings
from fundabot.db.session import connect  # noqa: F401  (re-exported for the app; tests patch settings.database_url)
from fundabot.debate import claims
from fundabot.eval import calibration
from fundabot.ingest import edgar_filings, grades, news, prices
from fundabot.orchestrate import snapshot, watchlist
from fundabot.report import charts
from fundabot.retrieval.query import load_chunks

log = logging.getLogger("fundabot")
WINDOWS = (30, 90, 180)
NEWS_WINDOWS = (7, 30, 90)
NARRATIVE_ESTIMATE = 0.03
list_filings = edgar_filings.list_filings  # module attribute so tests can stub the network


def _last_weekday(today: date | None = None) -> date:
    d = today or date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def cik_for(conn: psycopg.Connection, ticker: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT cik FROM snapshots WHERE ticker = %s ORDER BY created_at DESC LIMIT 1", (ticker.upper(),))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("SELECT cik FROM watchlist WHERE ticker = %s", (ticker.upper(),))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def refresh_prices(conn, cik: int, ticker: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM prices WHERE cik = %s", (cik,))
        last = cur.fetchone()[0]
    if last is None or last < _last_weekday():
        try:
            prices.ingest(conn, cik, ticker)
        except Exception as e:  # the page renders stored prices; the gap is logged
            log.warning("%s: price refresh failed: %s", ticker, e)


def refresh_news(conn, cik: int, ticker: str) -> int:
    try:
        return news.ingest(conn, cik, ticker)
    except Exception as e:
        log.warning("%s: news refresh failed: %s", ticker, e)
        return 0


def refresh_grades(conn, cik: int, ticker: str) -> int:
    try:
        return grades.ingest(conn, cik, ticker)
    except Exception as e:
        log.warning("%s: grades refresh failed: %s", ticker, e)
        return 0


def refresh_benchmark(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT max(date) FROM benchmark_prices WHERE ticker = 'SPY'")
        last = cur.fetchone()[0]
    if last is None or last < _last_weekday():
        try:
            calibration._benchmark(conn, prices.fetch_daily_closes)
        except Exception as e:
            log.warning("SPY refresh failed: %s", e)


def next_earnings(conn, cik: int, ticker: str) -> date | None:
    try:
        return grades.next_earnings(conn, cik, ticker)
    except Exception as e:
        log.warning("%s: next earnings lookup failed: %s", ticker, e)
        return None


def narrative_adapter(profile: str):
    from fundabot import profiles
    from fundabot.cli import make_adapter

    (p, m), = profiles.load(profile)["guidance"].items()
    return make_adapter(p, m, 8000)


def _spy(conn) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute("SELECT date, close FROM benchmark_prices WHERE ticker = 'SPY' ORDER BY date")
        df = pd.DataFrame(cur.fetchall(), columns=["date", "close"])
    df["close"] = df["close"].astype(float)
    return df


def stats_with_benchmark(px: pd.DataFrame, spy: pd.DataFrame) -> dict:
    s = technicals.price_stats(px)
    if s["asof"] is None or spy.empty:
        return s
    b = technicals.price_stats(spy, asof=s["asof"])
    for k in ("ret_1m", "ret_3m", "ret_6m", "ret_12m"):
        s[f"spy_{k}"] = b.get(k)
        s[f"excess_{k[4:]}"] = (s[k] - b[k]) if s.get(k) is not None and b.get(k) is not None else None
    return s


def latest_snapshot_with_metrics(conn, ticker: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT s.id FROM snapshots s WHERE s.ticker = %s AND EXISTS (SELECT 1 FROM metrics m WHERE m.snapshot_id = s.id) ORDER BY s.created_at DESC LIMIT 1", (ticker.upper(),))
        row = cur.fetchone()
    return snapshot.load(conn, row[0]) if row else None


def chart_data(px: pd.DataFrame, window_days: int) -> dict:
    df = px.sort_values("date").reset_index(drop=True)
    df["ma50"] = df.close.rolling(50).mean()
    df["ma200"] = df.close.rolling(200).mean()
    if df.empty:
        return {"closes": [], "ma50": [], "ma200": [], "dates": []}
    start = df.date.iloc[-1] - timedelta(days=window_days)
    w = df[df.date >= start]
    iso = [d.isoformat() for d in w.date]
    return {"closes": list(zip(iso, (float(v) for v in w.close))), "ma50": [(x, None if pd.isna(v) else float(v)) for x, v in zip(iso, w.ma50)],
            "ma200": [(x, None if pd.isna(v) else float(v)) for x, v in zip(iso, w.ma200)], "dates": list(w.date)}


def _panel_from_verdict(conn, ticker: str, sid: str, v: dict) -> dict:
    positive = [c for c in v.get("consensus_claims", []) if c["direction"] == "positive"]
    negative = [c for c in v.get("consensus_claims", []) if c["direction"] == "negative"]
    contested = v.get("contested_claims", [])
    with conn.cursor() as cur:
        cur.execute("SELECT created_at FROM verdicts WHERE snapshot_id = %s AND run_kind = 'full'", (sid,))
        created = cur.fetchone()[0]
    return {"kind": "full", "snapshot_id": sid, "composite": v.get("composite_score"), "veto_note": v.get("veto_note"), "judge_agreement": v.get("judge_agreement"),
            "dispersion": v.get("dispersion"), "bull": positive, "bear": negative + [c for c in contested if c["direction"] == "negative"],
            "contested_bull": [c for c in contested if c["direction"] == "positive"], "red_flags": v.get("red_flags", []), "catalysts": v.get("catalysts", []),
            "summary": v.get("one_paragraph_summary"), "subs": {k: v.get(k) for k in ("quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score")},
            "report_url": f"/reports/{ticker.upper()}/{sid}-full.html", "verdict_date": created.date() if created else None,
            "escalated": len(v.get("escalated_ids", [])), "n_claims": v.get("n_claims")}


def _panel_from_analyses(conn, ticker: str, sid: str) -> dict | None:
    from fundabot.agents.runner import load_analyses

    rows = load_analyses(conn, sid)
    if not rows:
        return None
    bull, bear = [], []
    for a in rows:
        for c in (a["grounded"] or {}).get("claims", []):
            entry = {**c, "agent": a["agent"], "id": f"{a['agent']}#{c.get('text', '')[:0]}"}
            (bull if c["direction"] == "positive" else bear).append(entry)
    bull.sort(key=lambda c: -c.get("weight", 0)); bear.sort(key=lambda c: -c.get("weight", 0))
    scores = {a["agent"]: a["grounded"]["score"] for a in rows if a["grounded"]}
    return {"kind": "cheap", "snapshot_id": sid, "composite": round(sum(scores.values()) / len(scores), 1) if scores else None, "veto_note": None, "judge_agreement": None,
            "dispersion": None, "bull": bull[:8], "bear": bear[:8], "contested_bull": [], "red_flags": [], "catalysts": [], "summary": None, "subs": {},
            "report_url": f"/reports/{ticker.upper()}/{sid}.html", "verdict_date": None, "scores": scores, "escalated": None, "n_claims": None}


def panel_for(conn, ticker: str) -> dict | None:
    from fundabot.cli import latest_with_verdict

    sid = latest_with_verdict(conn, ticker)
    if sid:
        with conn.cursor() as cur:
            cur.execute("SELECT verdict FROM verdicts WHERE snapshot_id = %s AND run_kind = 'full'", (sid,))
            return _panel_from_verdict(conn, ticker, sid, cur.fetchone()[0])
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT a.snapshot_id FROM analyses a JOIN snapshots s ON s.id = a.snapshot_id WHERE s.ticker = %s AND a.status = 'ok' ORDER BY a.snapshot_id DESC LIMIT 1", (ticker.upper(),))
        row = cur.fetchone()
    return _panel_from_analyses(conn, ticker, row[0]) if row else None


def ownership(metrics: list[Metric]) -> list[str]:
    ids = ("inst_holders_change_pct", "inst_shares_change_pct", "insider_net_shares_90d", "insider_buy_sell_ratio", "price_target_upside", "eps_estimate_revision_90d")
    return [metric_line(m) for m in metrics if m.metric_id in ids and m.period == "current" and m.value is not None]


def narrative_inputs(conn, ticker: str, cik: int, window_days: int, with_chunks: bool, px: pd.DataFrame | None = None, snap: dict | None = None,
                     metrics: list[Metric] | None = None, stats: dict | None = None) -> dict:
    px = px if px is not None else prices.load(conn, cik)
    snap = snap if snap is not None else latest_snapshot_with_metrics(conn, ticker)
    metrics = metrics if metrics is not None else (snapshot.load_metrics(conn, snap["id"]) if snap else [])
    stats = stats if stats is not None else stats_with_benchmark(px, _spy(conn))
    asof = stats.get("asof") or date.today()
    start = asof - timedelta(days=window_days)
    events = technicals.events_for(conn, cik, ticker, start, asof, list_filings=list_filings)
    mv = technicals.match_events(technicals.largest_moves(px, window_days), events, list(px.date))
    items = news.load(conn, cik, window_days, until=asof)
    chunks = []
    if with_chunks and snap:
        accs = [e.id.split(":", 1)[1] for e in events if e.kind == "filing" and e.id.split(":", 1)[1] in snap["accessions"]]
        accs += [f"T{e.id[len('transcript:T'):]}" for e in events if e.kind == "transcript"]
        if accs:
            from fundabot.retrieval.embed import OpenAIEmbedder
            from fundabot.retrieval.query import retrieve
            chunks = retrieve(conn, OpenAIEmbedder(), accs, ["what changed this quarter", "guidance outlook demand", "results compared to expectations"], max_tokens=6000)
    return {"window_days": window_days, "snapshot_id": snap["id"] if snap else None, "stats": stats, "moves": mv, "news": items, "metrics": metrics, "chunks": chunks, "events": events}


def ticker_page(conn, ticker: str, window_days: int = 90, news_days: int = 30, refresh: bool = True) -> dict:
    ticker = ticker.upper()
    cik = cik_for(conn, ticker)
    page = {"ticker": ticker, "cik": cik, "window_days": window_days, "news_days": news_days, "windows": WINDOWS, "news_windows": NEWS_WINDOWS,
            "narrative_estimate": NARRATIVE_ESTIMATE, "stats": None, "chart_svg": None, "moves": [], "outlook": {"label": "insufficient data", "components": {}, "rule": "no data"},
            "narrative": None, "narrative_stale": False, "news": [], "panel": None, "snapshot": None, "ownership": [], "next_earnings": None, "reports": [],
            "on_watchlist": any(e["ticker"] == ticker for e in watchlist.entries(conn))}
    if cik is None:
        return page
    if refresh:
        refresh_prices(conn, cik, ticker); refresh_news(conn, cik, ticker); refresh_grades(conn, cik, ticker); refresh_benchmark(conn)
    px = prices.load(conn, cik)
    snap = latest_snapshot_with_metrics(conn, ticker)
    metrics = snapshot.load_metrics(conn, snap["id"]) if snap else []
    stats = stats_with_benchmark(px, _spy(conn))
    page.update({"stats": stats, "snapshot": snap, "outlook": technicals.outlook(stats, metrics), "ownership": ownership(metrics), "next_earnings": next_earnings(conn, cik, ticker)})
    inputs = narrative_inputs(conn, ticker, cik, window_days, with_chunks=False, px=px, snap=snap, metrics=metrics, stats=stats)
    cd = chart_data(px, window_days)
    events = [{"date": e.date.isoformat(), "kind": e.kind, "title": e.title} for e in inputs["events"] if e.kind != "news" or any(e.id in [x.id for x in m["events"]] for m in inputs["moves"])]
    page["chart_svg"] = charts.price_chart(f"{ticker} close, {window_days} days", cd["closes"], cd["ma50"], cd["ma200"], events, highlights=[m["date"].isoformat() for m in inputs["moves"]]) if cd["closes"] else None
    page["moves"] = inputs["moves"]
    row = moves.load(conn, cik, window_days)
    page["narrative"] = row["output"] if row else None
    page["narrative_dropped"] = row["dropped"] if row else 0
    page["narrative_stale"] = moves.stale(conn, cik, window_days, moves.inputs_sha(inputs))
    page["news"] = news.load(conn, cik, news_days, until=stats.get("asof") or date.today())
    page["panel"] = panel_for(conn, ticker)
    page["metrics_snapshot_id"] = snap["id"] if snap else None
    return page


def overview(conn) -> list[dict]:
    from fundabot.cli import latest_with_verdict

    rows = []
    spy = _spy(conn)
    for e in watchlist.entries(conn):
        t = e["ticker"]
        cik = e.get("cik") or cik_for(conn, t)
        stats = stats_with_benchmark(prices.load(conn, cik), spy) if cik else technicals.price_stats(pd.DataFrame(columns=["date", "close"]))
        snap = latest_snapshot_with_metrics(conn, t)
        metrics = snapshot.load_metrics(conn, snap["id"]) if snap else []
        sid = latest_with_verdict(conn, t)
        composite = verdict_date = None
        if sid:
            with conn.cursor() as cur:
                cur.execute("SELECT verdict->>'composite_score', created_at FROM verdicts WHERE snapshot_id = %s AND run_kind = 'full'", (sid,))
                c, created = cur.fetchone()
                composite, verdict_date = float(c) if c else None, created.date()
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM run_queue WHERE ticker = %s AND status IN ('queued', 'started') ORDER BY id DESC LIMIT 1", (t,))
            q = cur.fetchone()
        rows.append({"ticker": t, "close": stats.get("close"), "ret_1m": stats.get("ret_1m"), "ret_3m": stats.get("ret_3m"), "ret_6m": stats.get("ret_6m"),
                     "outlook": technicals.outlook(stats, metrics)["label"], "composite": composite, "verdict_date": verdict_date, "queue_status": q[0] if q else None})
    return rows


def evidence(conn, sid: str, eid: str) -> dict | None:
    """What an evidence id points at, for the drawer."""
    kind = eid.split(":", 1)[0]
    if kind == "metric":
        metrics = snapshot.load_metrics(conn, sid)
        s = snapshot.load(conn, sid)
        text = claims.evidence_text([eid], metrics, {}, s["accessions"]).get(eid)
        return {"kind": "metric", "id": eid, "text": text} if text else None
    if kind == "chunk":
        c = load_chunks(conn, [eid.split(":", 1)[1]]).get(eid.split(":", 1)[1])
        return {"kind": "chunk", "id": eid, "text": c.text, "accession": c.accession, "item": c.item} if c else None
    if kind == "news":
        with conn.cursor() as cur:
            cur.execute("SELECT published, publisher, title, url, text FROM news WHERE id = %s", (eid.split(":", 1)[1],))
            r = cur.fetchone()
        return {"kind": "news", "id": eid, "published": r[0], "publisher": r[1], "title": r[2], "url": r[3], "text": r[4]} if r else None
    if kind == "filing":
        acc = eid.split(":", 1)[1]
        with conn.cursor() as cur:
            cur.execute("SELECT form, filed, primary_doc_url FROM filings WHERE accession = %s", (acc,))
            r = cur.fetchone()
        url = r[2] if r and r[2] else f"https://www.sec.gov/Archives/edgar/data/{acc.split('-')[0].lstrip('0')}/{acc.replace('-', '')}/"
        return {"kind": "filing", "id": eid, "text": f"{r[0]} filed {r[1]}" if r else "EDGAR filing", "url": url}
    if kind in ("price", "earnings", "transcript", "split", "grade"):
        return {"kind": kind, "id": eid, "text": eid.split(":", 1)[1]}
    return None


def month_spend(conn) -> tuple[float, float]:
    from fundabot.agents.base import month_spend as spend

    return spend(conn), float(settings.fundabot_monthly_budget_usd)
