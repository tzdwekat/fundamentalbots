"""Per-ticker pipeline. prepare() = ingest -> snapshot -> metrics -> flags. Agent stages are added later."""
import logging
from datetime import date, timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from fundabot.analytics import earnings, periods, red_flags, valuation
from fundabot.analytics.metrics import compute_fundamental_metrics
from fundabot.ingest import edgar_facts, edgar_filings, fmp, prices
from fundabot.orchestrate import snapshot

log = logging.getLogger("fundabot")


def _recent_fetch(conn: psycopg.Connection, cik: int, max_age_hours: float = 24) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM raw_companyfacts WHERE cik = %s AND fetched_at > now() - make_interval(hours => %s) ORDER BY id DESC LIMIT 1",
                    (cik, max_age_hours))
        row = cur.fetchone()
    return row[0] if row else None


def prepare(conn: psycopg.Connection, ticker: str, force: bool = False) -> str:
    ticker = ticker.upper()
    cik = edgar_facts.ticker_to_cik(ticker)
    fetch_id = None if force else _recent_fetch(conn, cik)
    if fetch_id is None:
        _, fetch_id = edgar_facts.ingest(conn, ticker)
    facts = edgar_facts.load_facts(conn, cik)

    try:
        price_date = prices.ingest(conn, cik, ticker)
        prices_df = prices.load(conn, cik)
        prices.ingest_splits(conn, cik, ticker)
    except prices.PriceUnavailable as e:
        log.warning("prices unavailable for %s: %s", ticker, e)
        price_date, prices_df = None, None
    as_of = price_date or date.today()
    facts = periods.adjust_for_splits(facts, prices.load_splits(conn, cik))

    filings = edgar_filings.list_filings(cik)
    accessions = edgar_filings.select_for_snapshot(filings)
    edgar_filings.ingest(conn, cik, filings, accessions, filings.attrs.get("name", ""))

    tag_map = periods.load_tag_map()
    series = periods.build_series(facts, tag_map)
    fundamentals = compute_fundamental_metrics(series)
    val = valuation.compute_valuation(series, facts, prices_df, filings, as_of, fundamentals)
    try:
        fmp.ingest(conn, cik, ticker)
    except fmp.FMPError as e:
        log.warning("FMP unavailable for %s: %s", ticker, e)
    fmp_data = fmp.load(conn, cik)
    latest_fetch = fmp_data["estimates"].fetched_at.max() if len(fmp_data["estimates"]) else None
    cur_price = next((m.value for m in val if m.metric_id == "price" and m.period == "current"), None)
    earn = earnings.compute_earnings_metrics(fmp_data, as_of, cur_price, periods.fiscal_year_end(facts, tag_map), periods.fy_offset(facts, tag_map), latest_fetch)
    flags = red_flags.numeric_flags(fundamentals)
    latest_10k = next((a for a in accessions if filings[filings.accession == a].form.iloc[0] == "10-K"), None)
    if latest_10k:
        flags += red_flags.text_flags(latest_10k, edgar_filings.load_sections(conn, latest_10k), filings)

    cur = {m.metric_id: m for m in val if m.period == "current"}
    close = cur["price"].value if "price" in cur else None
    shares = cur["shares_outstanding"].value if "shares_outstanding" in cur else None
    sid = snapshot.create_snapshot(conn, ticker, cik, fetch_id, accessions, price_date, close, shares)
    snapshot.store_metrics(conn, sid, fundamentals + val + earn)
    snapshot.store_flags(conn, sid, flags)
    return sid


async def analyze(conn: psycopg.Connection, ticker: str, personas: list[str], adapter, embedder, force: bool = False, rerun: bool = False,
                  out_dir: Path = Path("reports")) -> Path:
    """Phase 2 run over one or more providers. `adapter` may be one Adapter or a list. Cached analyses are reused unless rerun=True."""
    from fundabot.agents.runner import run_grid
    from fundabot.report.render import render_report

    adapters = adapter if isinstance(adapter, list) else [adapter]
    from fundabot.retrieval.query import embed_missing, load_chunks

    sid = prepare(conn, ticker, force=force)
    s = snapshot.load(conn, sid)
    embed_missing(conn, embedder, s["accessions"])
    if rerun:
        with conn.cursor() as cur:
            for a in adapters:
                cur.execute("DELETE FROM analyses WHERE snapshot_id = %s AND provider = %s AND model = %s", (sid, a.provider, a.model))
        conn.commit()
    analyses = await run_grid(conn, sid, personas, adapters, embedder)  # grounded inside run_grid
    metrics, flags = snapshot.load_metrics(conn, sid), snapshot.load_flags(conn, sid)
    chunks_by_id = load_chunks(conn, sorted({c for a in analyses for c in a["chunk_ids"]}))
    with conn.cursor() as cur:
        cur.execute("SELECT accession, form, filed FROM filings WHERE accession = ANY(%s) ORDER BY filed DESC", (s["accessions"],))
        filings = [dict(zip(["accession", "form", "filed"], r)) for r in cur.fetchall()]
    _, html = render_report(ticker, s, metrics, flags, analyses, chunks_by_id, filings, out_dir)
    log.info("%s: report %s", ticker, html)
    return html
