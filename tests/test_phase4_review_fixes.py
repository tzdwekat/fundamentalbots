"""Tests pinned by the Phase 4 whole-branch review."""
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from fundabot.analytics import guidance
from fundabot.analytics.earnings import compute_earnings_metrics
from fundabot.eval import calibration
from fundabot.orchestrate import snapshot, triggers, watchlist
from tests.test_calibration import prices
from tests.test_earnings import EST, FYE, OFFSET, SUR, T1, get
from tests.test_signals import full_fmp
from tests.test_triggers import filings, queue, watched  # noqa: F401

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


# 1. steady-state poll sees same-day filings and transcripts
@pytest.mark.db
def test_poll_ten_minutes_later_queues_same_day_signals(watched):
    conn = watched
    assert triggers.poll(conn, list_filings=lambda cik: filings([]), transcript_dates=lambda t: [], now=NOW) == []
    later = NOW + timedelta(minutes=10)
    f = filings([("acc-8k", "8-K", later.date(), later.date(), "", "2.02")])
    rows = triggers.poll(conn, list_filings=lambda cik: f, transcript_dates=lambda t: [{"quarter": 4, "fiscalYear": 2026, "date": later.strftime("%Y-%m-%d")}], now=later)
    assert [r["period_key"] for r in rows] == ["Q4FY2026"] and queue(conn)[0][3] == later  # transcript cleared the wait
    again = triggers.poll(conn, list_filings=lambda cik: f, transcript_dates=lambda t: [], now=later + timedelta(minutes=10))
    assert again == [] and len(queue(conn)) == 1  # re-sighting the same filing is a no-op


# 7. first poll skips what the latest snapshot already analysed
@pytest.mark.db
def test_first_poll_skips_already_analysed_filing(watched):
    conn = watched
    snapshot.create_snapshot(conn, "AAPL", 320193, 1, ["acc-10q", "T2026Q4"], date(2026, 10, 15), 1.0, 1.0)
    f = filings([("acc-10q", "10-Q", date(2026, 10, 14), date(2026, 6, 27), "", ""), ("acc-new", "10-K", date(2026, 10, 15), date(2026, 9, 26), "", "")])
    rows = triggers.poll(conn, list_filings=lambda cik: f, transcript_dates=lambda t: [{"quarter": 4, "fiscalYear": 2026, "date": "2026-10-15"}], now=NOW)
    assert [r["period_key"] for r in rows] == ["FY2026"]


# 5. transcript period key derived with our fiscal calendar; mismatch logged
def test_transcript_key_uses_our_calendar(caplog):
    # a company whose FMP fiscal year is one ahead of ours: call on 2026-07-30 is our Q3FY2026 regardless of FMP's label
    key = triggers.transcript_key_for(date(2026, 7, 30), 2027, 3, FYE, OFFSET)
    assert key == "Q3FY2026" and any("fiscal label" in r.message for r in caplog.records)


def test_guidance_labels_use_supplied_calendar():
    rows = [{"key": "T2027Q3", "call_date": date(2026, 7, 30), "statements": [], "tone": None, "tone_quote": None, "tone_chunk": None}]
    ms = guidance.metrics(rows, label_fn=lambda r: "Q3FY2026")
    assert {m.period for m in ms} == {"Q3FY2026"}


# 8. a quote must be a real sentence
def test_short_quote_dropped():
    from fundabot.retrieval.chunk import Chunk
    c = Chunk("T2026Q3#call-0", "T2026Q3", "call", 0, "We expect revenue to grow 10 to 12 percent next quarter. Thank you.", 14)
    out = guidance.verify({"statements": [{"metric": "revenue", "period": "Q4", "direction": "raised", "quote": "revenue", "chunk": "chunk:T2026Q3#call-0"},
                                          {"metric": "revenue", "period": "Q4", "direction": "raised", "quote": "We expect revenue to grow 10 to 12 percent next quarter.", "chunk": "chunk:T2026Q3#call-0"}],
                           "tone": "confident", "tone_quote": "Thank you.", "tone_chunk": "chunk:T2026Q3#call-0"}, {c.id: c})
    assert len(out["statements"]) == 1 and out["tone"] is None


# 3. insider statistics must come from a completed quarter
def test_insider_uses_completed_quarter_only():
    ins = [(2026, 3, 455601, 59762, 2, 3), (2026, 4, 774053, 1223409, 0, 12)]
    ms = compute_earnings_metrics(full_fmp((), [], ins), date(2026, 10, 16), 340.0, FYE, OFFSET, T1)
    m = get(ms, "insider_net_shares_90d")
    assert m.value == 455601 - 59762 and m.inputs["quarter"] == 3
    ms2 = compute_earnings_metrics(full_fmp((), [], ins), date(2027, 1, 5), 340.0, FYE, OFFSET, T1)
    assert get(ms2, "insider_net_shares_90d").inputs["quarter"] == 4


# 9. constant scores must not produce NaN
def test_spearman_constant_is_none():
    assert calibration._spearman([5, 5, 5, 5], [1, 2, 3, 4]) is None


# 2. calibration refreshes ticker prices and refuses truncated horizons
@pytest.mark.db
def test_calibration_refreshes_prices_and_requires_full_horizon(db_conn):
    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["a"], date(2026, 1, 1), 100.0, 1.0)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, 'full', '{\"composite_score\": 1.0, \"grid\": {}}')", (sid,))
        cur.executemany("INSERT INTO prices (cik, date, close) VALUES (320193, %s, %s)", [(date(2026, 1, 1) + timedelta(days=i), 100 + i) for i in range(3)])  # stored closes stop after 2 days
    db_conn.commit()
    served = {"SPY": prices(date(2026, 1, 1), [100 + i for i in range(40)]), "AAPL": prices(date(2026, 1, 1), [100 + i for i in range(10)])}  # AAPL fetch also stops early

    def fetch(ticker, years=5):
        return served[ticker]
    stats = calibration.run(db_conn, horizons=(30,), fetch_prices=fetch, today=date(2026, 3, 1))
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outcomes"); assert cur.fetchone()[0] == 0  # not enough prices to cover the horizon: left pending, no truncated return stored
        cur.execute("SELECT count(*) FROM prices WHERE cik = 320193"); assert cur.fetchone()[0] == 10  # ticker prices were refreshed
    served["AAPL"] = prices(date(2026, 1, 1), [100 + i for i in range(40)])
    calibration.run(db_conn, horizons=(30,), fetch_prices=fetch, today=date(2026, 3, 1))
    with db_conn.cursor() as cur:
        cur.execute("SELECT ticker_return, reason FROM outcomes"); tr, reason = cur.fetchone()
    assert abs(float(tr) - 0.30) < 1e-9 and reason is None


@pytest.mark.db
def test_calibration_delisted_ticker_gets_null_row(db_conn):
    sid = snapshot.create_snapshot(db_conn, "DEAD", 999, 1, ["a"], date(2026, 1, 1), 100.0, 1.0)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, 'full', '{\"composite_score\": 1.0, \"grid\": {}}')", (sid,))
    db_conn.commit()
    from fundabot.ingest.prices import PriceUnavailable

    def fetch(ticker, years=5):
        if ticker == "SPY":
            return prices(date(2026, 1, 1), [100 + i for i in range(40)])
        raise PriceUnavailable("gone")
    calibration.run(db_conn, horizons=(30,), fetch_prices=fetch, today=date(2026, 3, 1))
    with db_conn.cursor() as cur:
        cur.execute("SELECT ticker_return, reason FROM outcomes"); assert cur.fetchone() == (None, "prices_unavailable")


# 4. cheap-run outcomes carry their composite into the statistics
@pytest.mark.db
def test_cheap_run_outcome_scores_feed_statistics(db_conn):
    import json
    for i in range(12):
        sid = snapshot.create_snapshot(db_conn, f"T{i}", 1000 + i, 1, ["a"], date(2026, 1, 1), 100.0, 1.0)
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, grounded, status) VALUES (%s, 'bull', 'fake', 'm', '{}', %s, 'ok')",
                        (sid, json.dumps({"score": i - 6, "confidence": 0.5, "stats": {}})))
            cur.executemany("INSERT INTO prices (cik, date, close) VALUES (%s, %s, %s)", [(1000 + i, date(2026, 1, 1) + timedelta(days=d), 100 * (1 + (i - 6) / 50) ** (d / 40)) for d in range(40)])
        db_conn.commit()

    def fetch(ticker, years=5):
        if ticker == "SPY":
            return prices(date(2026, 1, 1), [100.0] * 40)
        i = int(ticker[1:])
        return prices(date(2026, 1, 1), [100 * (1 + (i - 6) / 50) ** (d / 40) for d in range(40)])
    stats = calibration.run(db_conn, horizons=(30,), fetch_prices=fetch, today=date(2026, 3, 1))[30]
    assert stats["n"] == 12 and stats["rho"]["composite_score"] is not None and stats["rho"]["composite_score"] > 0.9 and stats["brier"] is not None


# 6. launchctl failures are not swallowed
def test_install_raises_on_bootstrap_failure(tmp_path):
    from fundabot.orchestrate import schedule

    def fake_run(cmd, **kw):
        rc = 5 if cmd[:2] == ["launchctl", "bootstrap"] else 0
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": "Input/output error"})()
    with pytest.raises(RuntimeError, match="bootstrap"):
        schedule.install(binary=tmp_path / "fundabot", workdir=tmp_path, home=tmp_path, run=fake_run, uid=501)
