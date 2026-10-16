"""Watchlist management and scans over any ticker list: stalest first, bounded concurrency, per-scan budget."""
import asyncio
import logging

import psycopg

from fundabot.agents.base import month_spend
from fundabot.ingest import edgar_facts

log = logging.getLogger("fundabot")


def add(conn: psycopg.Connection, tickers: list[str], note: str = "") -> list[str]:
    out = []
    with conn.cursor() as cur:
        for t in tickers:
            t = t.upper()
            cur.execute("INSERT INTO watchlist (ticker, cik, note) VALUES (%s, %s, %s) ON CONFLICT (ticker) DO NOTHING", (t, edgar_facts.ticker_to_cik(t), note or None))
            out.append(t)
    conn.commit()
    return out


def remove(conn: psycopg.Connection, tickers: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM watchlist WHERE ticker = ANY(%s)", ([t.upper() for t in tickers],))
    conn.commit()


def entries(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, cik, added_at, note, last_poll FROM watchlist ORDER BY ticker")
        return [dict(zip(["ticker", "cik", "added_at", "note", "last_poll"], r)) for r in cur.fetchall()]


def order_by_staleness(conn: psycopg.Connection, tickers: list[str]) -> list[str]:
    """Tickers with no snapshot first, then by oldest latest-snapshot; an interrupted scan resumes where it stopped."""
    tickers = [t.upper() for t in tickers]
    with conn.cursor() as cur:
        cur.execute("SELECT ticker, max(created_at) FROM snapshots WHERE ticker = ANY(%s) GROUP BY ticker", (tickers,))
        latest = dict(cur.fetchall())
    return sorted(tickers, key=lambda t: (latest.get(t) is not None, latest.get(t)))


async def scan(conn: psycopg.Connection, tickers: list[str], full: bool, max_usd: float, runner, concurrency: int = 3) -> list[dict]:
    """runner(ticker, full) -> {composite, dispersion, report}; stops starting new tickers once this scan's spend reaches max_usd."""
    order = order_by_staleness(conn, tickers)
    start_spend = month_spend(conn)
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, dict] = {}

    async def one(ticker):
        async with sem:
            if month_spend(conn) - start_spend >= max_usd:
                results[ticker] = {"ticker": ticker, "status": "skipped", "error": "budget"}
                return
            try:
                r = await runner(ticker, full)
                results[ticker] = {"ticker": ticker, "status": "ok", "error": None, **r}
            except Exception as e:  # one bad ticker must not stop the scan
                log.warning("scan %s failed: %s", ticker, e)
                results[ticker] = {"ticker": ticker, "status": "error", "error": str(e)[:300]}

    await asyncio.gather(*[one(t) for t in order])
    return [results[t] for t in order]
