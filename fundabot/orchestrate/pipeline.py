"""Per-ticker pipeline. prepare() = ingest -> snapshot -> metrics -> flags. Agent stages are added later."""
import logging
from datetime import date, timedelta

import psycopg

from fundabot.analytics import periods, red_flags, valuation
from fundabot.analytics.metrics import compute_fundamental_metrics
from fundabot.ingest import edgar_facts, edgar_filings, prices
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
    except prices.PriceUnavailable as e:
        log.warning("prices unavailable for %s: %s", ticker, e)
        price_date, prices_df = None, None
    as_of = price_date or date.today()

    filings = edgar_filings.list_filings(cik)
    accessions = edgar_filings.select_for_snapshot(filings)
    edgar_filings.ingest(conn, cik, filings, accessions, filings.attrs.get("name", ""))

    series = periods.build_series(facts, periods.load_tag_map())
    fundamentals = compute_fundamental_metrics(series)
    val = valuation.compute_valuation(series, facts, prices_df, filings, as_of, fundamentals)
    flags = red_flags.numeric_flags(fundamentals)
    latest_10k = next((a for a in accessions if filings[filings.accession == a].form.iloc[0] == "10-K"), None)
    if latest_10k:
        flags += red_flags.text_flags(latest_10k, edgar_filings.load_sections(conn, latest_10k), filings)

    cur = {m.metric_id: m for m in val if m.period == "current"}
    close = cur["price"].value if "price" in cur else None
    shares = cur["shares_outstanding"].value if "shares_outstanding" in cur else None
    sid = snapshot.create_snapshot(conn, ticker, cik, fetch_id, accessions, price_date, close, shares)
    snapshot.store_metrics(conn, sid, fundamentals + val)
    snapshot.store_flags(conn, sid, flags)
    return sid
