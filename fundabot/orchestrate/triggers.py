"""Event triggers: diff each watchlist ticker's EDGAR submissions and FMP transcript dates into a per-quarter run queue.

One earnings quarter produces up to three signals days apart (8-K Item 2.02, transcript, 10-Q). They collapse onto
one queue row keyed (cik, period_key); a filing signal waits up to 3 days for the transcript, a transcript signal
clears the wait. `worker` drains ready rows one at a time.
"""
import logging
from datetime import date, datetime, timedelta, timezone

import psycopg

from fundabot.agents.base import BudgetExceeded
from fundabot.analytics import periods
from fundabot.ingest import edgar_facts, edgar_filings, fmp
from fundabot.orchestrate import snapshot, watchlist

log = logging.getLogger("fundabot")
TRANSCRIPT_WAIT = timedelta(days=3)
FIRST_POLL_WINDOW = timedelta(days=7)
TRIGGER_FORMS = {"10-K", "10-Q", "8-K", "8-K/A"}


def period_key(form: str, report_date: date, fye: tuple[int, int], offset: int) -> str:
    """FY{y} for a 10-K; Q{n}FY{y} otherwise (an earnings 8-K's report date is the quarter just reported, so step back 35 days)."""
    if form.startswith("10-K"):
        return f"FY{periods.fy_of(report_date, fye) + offset}"
    end = report_date - timedelta(days=35) if form.startswith("8-K") else report_date
    fy_cal = periods.fy_of(end, fye)
    fq = periods.fq_of(end, fy_cal, fye)
    if fq == 0:
        fq, fy_cal = 4, fy_cal - 1
    return f"Q{fq}FY{fy_cal + offset}"


def transcript_period_key(fy: int, fq: int) -> str:
    return f"Q{fq}FY{fy}"


def transcript_key_for(call_date: date, fmp_fy: int, fmp_fq: int, fye: tuple[int, int], offset: int) -> str:
    """Our label for the quarter a call reports (from the call date, like an earnings 8-K); FMP's own label is only cross-checked."""
    ours = period_key("8-K", call_date, fye, offset)
    theirs = transcript_period_key(fmp_fy, fmp_fq)
    if ours != theirs:
        log.warning("transcript fiscal label mismatch: FMP says %s, our calendar says %s for a call on %s", theirs, ours, call_date)
    return ours


def fiscal_calendar(conn: psycopg.Connection, cik: int) -> tuple[tuple[int, int], int]:
    facts = edgar_facts.load_facts(conn, cik)
    tag_map = periods.load_tag_map()
    return periods.fiscal_year_end(facts, tag_map), periods.fy_offset(facts, tag_map)


def _queue(conn, ticker, cik, key, reason, accession, not_before) -> bool:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_queue (ticker, cik, period_key, reason, accession, not_before) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (cik, period_key) DO NOTHING RETURNING id", (ticker, cik, key, reason, accession, not_before))
        return cur.fetchone() is not None


def poll(conn: psycopg.Connection, list_filings=edgar_filings.list_filings_fresh, transcript_dates=fmp.fetch_transcript_dates, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    queued = []
    for e in watchlist.entries(conn):
        ticker, cik = e["ticker"], e["cik"]
        since = e["last_poll"] or (now - FIRST_POLL_WINDOW)
        try:
            fye, offset = fiscal_calendar(conn, cik)
        except Exception as ex:  # no facts yet: cannot label periods; skip until the first analysis has ingested them
            log.warning("poll %s: %s", ticker, ex)
            continue
        # Filing and call dates are day-granular: compare on dates inclusively; the (cik, period_key) key absorbs re-sightings.
        since_date = since.date()
        latest = snapshot.latest_for(conn, ticker)
        analysed = set(latest["accessions"]) if latest else set()
        ticker_queued = []
        try:
            f = list_filings(cik)
            f = f[f.form.isin(TRIGGER_FORMS) & (f.filed >= since_date)]
            for r in f.itertuples(index=False):
                if r.form.startswith("8-K") and "2.02" not in str(r.items):
                    continue
                if r.period_end is None or r.accession in analysed:
                    continue
                key = period_key(r.form, r.period_end, fye, offset)
                if _queue(conn, ticker, cik, key, r.form, r.accession, now + TRANSCRIPT_WAIT):
                    ticker_queued.append({"ticker": ticker, "period_key": key, "reason": r.form})
            for d in transcript_dates(ticker):
                call = date.fromisoformat(d["date"][:10])
                fy, fq = int(d["fiscalYear"]), int(d["quarter"])
                if call < since_date or f"T{fy}Q{fq}" in analysed:
                    continue
                key = transcript_key_for(call, fy, fq, fye, offset)
                if _queue(conn, ticker, cik, key, "transcript", None, now):
                    ticker_queued.append({"ticker": ticker, "period_key": key, "reason": "transcript"})
                with conn.cursor() as cur:  # a transcript ends the wait for an already-queued quarter
                    cur.execute("UPDATE run_queue SET not_before = %s WHERE cik = %s AND period_key = %s AND status = 'queued' AND not_before > %s", (now, cik, key, now))
        except Exception as ex:
            log.warning("poll %s failed: %s", ticker, ex)
            conn.rollback()
            continue
        queued += ticker_queued
        with conn.cursor() as cur:
            cur.execute("UPDATE watchlist SET last_poll = %s WHERE ticker = %s", (now, ticker))
        conn.commit()
    return queued


async def worker(conn: psycopg.Connection, run_full, now: datetime | None = None, limit: int = 1) -> list[dict]:
    """Run ready queue rows oldest first. BudgetExceeded leaves the row queued and stops; other errors mark it."""
    now = now or datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute("SELECT id, ticker, period_key FROM run_queue WHERE status = 'queued' AND not_before <= %s ORDER BY queued_at LIMIT %s", (now, limit))
        rows = cur.fetchall()
    done = []
    for rid, ticker, key in rows:
        with conn.cursor() as cur:
            cur.execute("UPDATE run_queue SET status = 'running', started_at = %s WHERE id = %s", (now, rid))
        conn.commit()
        try:
            report = await run_full(ticker)
            status, error = "done", None
        except BudgetExceeded as e:
            with conn.cursor() as cur:
                cur.execute("UPDATE run_queue SET status = 'queued', started_at = NULL, error = %s WHERE id = %s", (str(e)[:300], rid))
            conn.commit()
            log.warning("worker: budget exceeded, leaving %s %s queued", ticker, key)
            break
        except Exception as e:
            report, status, error = None, "error", str(e)[:500]
        with conn.cursor() as cur:
            cur.execute("UPDATE run_queue SET status = %s, finished_at = %s, error = %s WHERE id = %s", (status, datetime.now(timezone.utc), error, rid))
        conn.commit()
        done.append({"ticker": ticker, "period_key": key, "status": status, "report": report, "error": error})
    return done
