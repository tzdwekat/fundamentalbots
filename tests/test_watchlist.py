from datetime import date, datetime, timedelta, timezone

import pytest

from fundabot.orchestrate import snapshot, watchlist


@pytest.fixture
def ciks(monkeypatch):
    from fundabot.ingest import edgar_facts
    monkeypatch.setattr(edgar_facts, "ticker_to_cik", lambda t: {"AAPL": 320193, "NVDA": 1045810, "MSFT": 789019}[t.upper()])


@pytest.mark.db
def test_watch_add_remove_list(db_conn, ciks):
    assert watchlist.add(db_conn, ["aapl", "NVDA"], note="core") == ["AAPL", "NVDA"]
    watchlist.add(db_conn, ["AAPL"])  # idempotent
    rows = watchlist.entries(db_conn)
    assert [(r["ticker"], r["cik"]) for r in rows] == [("AAPL", 320193), ("NVDA", 1045810)] and rows[0]["note"] == "core"
    watchlist.remove(db_conn, ["AAPL"])
    assert [r["ticker"] for r in watchlist.entries(db_conn)] == ["NVDA"]


@pytest.mark.db
def test_scan_orders_stalest_first(db_conn):
    snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["a"], date(2026, 10, 14), 1.0, 1.0)
    snapshot.create_snapshot(db_conn, "NVDA", 1045810, 1, ["b"], date(2026, 10, 13), 1.0, 1.0)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE snapshots SET created_at = %s WHERE ticker = 'AAPL'", (datetime(2026, 10, 1, tzinfo=timezone.utc),))
        cur.execute("UPDATE snapshots SET created_at = %s WHERE ticker = 'NVDA'", (datetime(2026, 10, 10, tzinfo=timezone.utc),))
    db_conn.commit()
    assert watchlist.order_by_staleness(db_conn, ["NVDA", "AAPL", "MSFT"]) == ["MSFT", "AAPL", "NVDA"]


async def _runner_factory(conn, cost_per_ticker, fail=()):
    async def runner(ticker, full):
        if ticker in fail:
            raise RuntimeError(f"{ticker} boom")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO model_calls (provider, model, cost_usd) VALUES ('fake', 'm', %s)", (cost_per_ticker,))
        conn.commit()
        return {"composite": 1.0, "dispersion": 0.1, "report": f"reports/{ticker}/x.html"}
    return runner


@pytest.mark.db
async def test_scan_stops_at_budget(db_conn):
    runner = await _runner_factory(db_conn, 60.0)
    results = await watchlist.scan(db_conn, ["AAPL", "NVDA", "MSFT"], full=False, max_usd=100.0, runner=runner, concurrency=1)
    statuses = {r["ticker"]: r["status"] for r in results}
    assert statuses == {"AAPL": "ok", "NVDA": "ok", "MSFT": "skipped"} and results[-1]["error"] == "budget"


@pytest.mark.db
async def test_scan_continues_past_one_error(db_conn):
    runner = await _runner_factory(db_conn, 1.0, fail=("NVDA",))
    results = await watchlist.scan(db_conn, ["AAPL", "NVDA", "MSFT"], full=True, max_usd=100.0, runner=runner, concurrency=3)
    by = {r["ticker"]: r for r in results}
    assert by["AAPL"]["status"] == "ok" and by["MSFT"]["status"] == "ok" and by["NVDA"]["status"] == "error" and "boom" in by["NVDA"]["error"]
    assert by["AAPL"]["report"].endswith("x.html")
