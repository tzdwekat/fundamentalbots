from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from fundabot.agents.base import BudgetExceeded
from fundabot.orchestrate import triggers, watchlist

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
FYE, OFFSET = (9, 27), 0


def filings(rows):
    return pd.DataFrame(rows, columns=["accession", "form", "filed", "period_end", "primary_doc_url", "items"])


def test_period_key():
    assert triggers.period_key("10-K", date(2026, 9, 26), FYE, OFFSET) == "FY2026"
    assert triggers.period_key("10-Q", date(2026, 6, 27), FYE, OFFSET) == "Q3FY2026"
    assert triggers.period_key("8-K", date(2026, 7, 30), FYE, OFFSET) == "Q3FY2026"  # earnings 8-K report date = quarter just reported
    assert triggers.transcript_period_key(2026, 3) == "Q3FY2026"


@pytest.fixture
def watched(db_conn, monkeypatch):
    from fundabot.ingest import edgar_facts
    monkeypatch.setattr(edgar_facts, "ticker_to_cik", lambda t: 320193)
    monkeypatch.setattr(triggers, "fiscal_calendar", lambda conn, cik: (FYE, OFFSET))
    watchlist.add(db_conn, ["AAPL"])
    return db_conn


def queue(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT period_key, reason, accession, not_before, status FROM run_queue ORDER BY id")
        return cur.fetchall()


@pytest.mark.db
def test_poll_queues_8k_202_10q_10k_and_transcript(watched):
    f = filings([("acc-8k", "8-K", date(2026, 10, 15), date(2026, 10, 15), "", "2.02,9.01"),
                 ("acc-10q", "10-Q", date(2026, 10, 14), date(2026, 6, 27), "", ""),
                 ("acc-10k", "10-K", date(2026, 10, 13), date(2026, 9, 26), "", ""),
                 ("acc-old", "10-Q", date(2026, 5, 1), date(2026, 3, 28), "", "")])
    dates = [{"quarter": 4, "fiscalYear": 2026, "date": "2026-10-15"}]
    rows = triggers.poll(watched, list_filings=lambda cik: f, transcript_dates=lambda t: dates, now=NOW)
    keys = {r[0]: r for r in queue(watched)}
    assert set(keys) == {"Q4FY2026", "Q3FY2026", "FY2026"} and len(rows) == 3  # the May filing is older than the 7-day first-poll window
    assert keys["Q4FY2026"][3] is not None and keys["Q4FY2026"][3] <= NOW  # transcript present: no wait
    assert keys["Q3FY2026"][3] == NOW + timedelta(days=3) and keys["FY2026"][3] == NOW + timedelta(days=3)
    assert watchlist.entries(watched)[0]["last_poll"] == NOW


@pytest.mark.db
def test_same_quarter_signals_collapse(watched):
    conn = watched
    triggers.poll(conn, list_filings=lambda cik: filings([("acc-8k", "8-K", date(2026, 10, 15), date(2026, 10, 15), "", "2.02")]), transcript_dates=lambda t: [], now=NOW)
    assert [r[0] for r in queue(conn)] == ["Q4FY2026"] and queue(conn)[0][3] == NOW + timedelta(days=3)
    later = NOW + timedelta(days=1)
    triggers.poll(conn, list_filings=lambda cik: filings([("acc-8ka", "8-K/A", later.date(), later.date(), "", "2.02"), ("acc-10q", "10-Q", later.date(), date(2026, 9, 26), "", "")]),
                  transcript_dates=lambda t: [{"quarter": 4, "fiscalYear": 2026, "date": later.strftime("%Y-%m-%d")}], now=later)
    rows = queue(conn)
    assert len(rows) == 1 and rows[0][0] == "Q4FY2026" and rows[0][3] == later  # one row; the transcript cleared the wait


@pytest.mark.db
def test_non_202_8k_ignored(watched):
    triggers.poll(watched, list_filings=lambda cik: filings([("acc-8k", "8-K", date(2026, 10, 15), date(2026, 10, 15), "", "5.02,9.01")]), transcript_dates=lambda t: [], now=NOW)
    assert queue(watched) == []


@pytest.mark.db
async def test_worker_runs_only_ready_rows_and_records_status(watched):
    conn = watched
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_queue (ticker, cik, period_key, reason, not_before) VALUES ('AAPL', 320193, 'Q3FY2026', '10-Q', %s), ('AAPL', 320193, 'Q4FY2026', '8-K', %s)",
                    (NOW - timedelta(hours=1), NOW + timedelta(days=3)))
    conn.commit()
    ran = []

    async def run_full(ticker):
        ran.append(ticker)
        return "reports/AAPL/x-full.html"
    done = await triggers.worker(conn, run_full, now=NOW, limit=5)
    assert ran == ["AAPL"] and len(done) == 1 and done[0]["status"] == "done"
    rows = {r[0]: r[4] for r in queue(conn)}
    assert rows == {"Q3FY2026": "done", "Q4FY2026": "queued"}


@pytest.mark.db
async def test_worker_budget_leaves_row_queued(watched):
    conn = watched
    with conn.cursor() as cur:
        cur.execute("INSERT INTO run_queue (ticker, cik, period_key, reason, not_before) VALUES ('AAPL', 320193, 'Q3FY2026', '10-Q', %s)", (NOW - timedelta(hours=1),))
    conn.commit()

    async def run_full(ticker):
        raise BudgetExceeded("cap")
    done = await triggers.worker(conn, run_full, now=NOW)
    assert done == [] and queue(conn)[0][4] == "queued"

    async def run_boom(ticker):
        raise RuntimeError("provider down")
    done = await triggers.worker(conn, run_boom, now=NOW)
    assert done[0]["status"] == "error" and queue(conn)[0][4] == "error"
