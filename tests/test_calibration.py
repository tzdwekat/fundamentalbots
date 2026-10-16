from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from fundabot.eval import calibration
from fundabot.orchestrate import snapshot


def prices(start: date, closes: list[float]):
    return pd.DataFrame({"date": [start + timedelta(days=i) for i in range(len(closes))], "close": closes})


def test_compute_outcome_returns_and_drawdown():
    t0 = date(2026, 1, 1)
    px = prices(t0, [100, 110, 90, 120, 130])            # +30% over the horizon, worst drawdown -18.2% (110 -> 90)
    spy = prices(t0, [100, 101, 102, 103, 104])           # +4%
    sur = pd.DataFrame([(date(2026, 1, 3), 1.1, 1.0)], columns=["report_date", "eps_actual", "eps_estimated"])
    o = calibration.compute_outcome(px, spy, t0, 4, sur)
    assert o["t1"] == date(2026, 1, 5) and abs(o["ticker_return"] - 0.30) < 1e-9 and abs(o["spy_return"] - 0.04) < 1e-9
    assert abs(o["excess_return"] - 0.26) < 1e-9 and abs(o["max_drawdown"] - (90 / 110 - 1)) < 1e-9
    assert abs(o["next_eps_surprise"] - 0.1) < 1e-9 and o["reason"] is None


def test_delisted_ticker_outcome_null():
    t0 = date(2026, 1, 1)
    o = calibration.compute_outcome(prices(date(2025, 12, 1), [1, 2, 3]), prices(t0, [100, 101]), t0, 30, pd.DataFrame(columns=["report_date", "eps_actual", "eps_estimated"]))
    assert o["ticker_return"] is None and o["excess_return"] is None and o["reason"] == "no_prices_after_t0" and o["next_eps_surprise"] is None


def test_statistics_insufficient_below_10():
    rows = [{"composite_score": i, "quality_score": 50, "value_score": 50, "earnings_momentum_score": 50, "accounting_risk_score": 10,
             "excess_return": i / 100, "max_drawdown": -0.1, "next_eps_surprise": 0.01, "agents": [(i, 0.5)]} for i in range(9)]
    assert calibration.statistics(rows) == {"insufficient_history": True, "n": 9}


def test_statistics_values():
    rows = []
    for i in range(12):
        rows.append({"composite_score": i - 6, "quality_score": 40 + i * 4, "value_score": 100 - i * 5, "earnings_momentum_score": 70 if i % 2 else 50,
                     "accounting_risk_score": 10 * i, "excess_return": (i - 6) / 50, "max_drawdown": -0.02 * i,
                     "next_eps_surprise": 0.05 if i % 2 else -0.05, "agents": [(i - 6, 1.0)]})
    s = calibration.statistics(rows)
    assert s["n"] == 12 and abs(s["rho"]["composite_score"] - 1.0) < 1e-9 and abs(s["rho"]["value_score"] + 1.0) < 1e-9
    assert s["tercile_spread"] > 0 and abs(s["rho_accounting_risk_vs_drawdown"] + 1.0) < 1e-9
    assert s["momentum_hit_rate"] == 1.0  # every momentum>60 row had a positive surprise
    assert 0 <= s["brier"] <= 1


@pytest.mark.db
def test_pending_verdict_not_scored(db_conn):
    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["a"], date(2026, 10, 10), 100.0, 1.0)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, 'full', '{\"composite_score\": 2.0}')", (sid,))
    db_conn.commit()
    assert calibration.candidates(db_conn, 30, today=date(2026, 10, 20)) == []
    c = calibration.candidates(db_conn, 30, today=date(2026, 11, 20))
    assert len(c) == 1 and c[0]["snapshot_id"] == sid and c[0]["run_kind"] == "full" and c[0]["composite_score"] == 2.0


@pytest.mark.db
def test_run_stores_outcomes(db_conn):
    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["a"], date(2026, 1, 1), 100.0, 1.0)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, 'full', %s)",
                    (sid, '{"composite_score": 2.0, "quality_score": 50, "value_score": 50, "earnings_momentum_score": 70, "accounting_risk_score": 10, "revised_scores": {"bull/openai": 3}, "grid": {"bull/openai": {"score": 3, "confidence": 0.7}}}'))
        cur.executemany("INSERT INTO prices (cik, date, close) VALUES (320193, %s, %s)", [(date(2026, 1, 1) + timedelta(days=i), 100 + i) for i in range(40)])
    db_conn.commit()
    fetched = []

    def fetch(ticker, years=5):
        fetched.append(ticker)
        return prices(date(2026, 1, 1), [100 + 0.5 * i for i in range(40)])
    stats = calibration.run(db_conn, horizons=(30,), fetch_prices=fetch, today=date(2026, 3, 1))
    assert fetched == ["SPY"] and stats[30]["insufficient_history"] is True and stats[30]["n"] == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT horizon_days, ticker_return, excess_return FROM outcomes WHERE snapshot_id = %s", (sid,))
        h, tr, ex = cur.fetchone()
        cur.execute("SELECT count(*) FROM calibration_runs"); assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM benchmark_prices WHERE ticker = 'SPY'"); assert cur.fetchone()[0] == 40
    assert h == 30 and abs(float(tr) - 0.30) < 1e-9 and abs(float(ex) - (0.30 - 0.15)) < 1e-9
    assert calibration.run(db_conn, horizons=(30,), fetch_prices=fetch, today=date(2026, 3, 1))[30]["n"] == 1 and fetched == ["SPY", "SPY"]  # outcome cached, SPY refreshed per run
