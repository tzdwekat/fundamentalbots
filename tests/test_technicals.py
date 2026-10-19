"""Price statistics, event matching and the outlook rule: all computed in code."""
from datetime import date, timedelta

import pandas as pd
import pytest

from fundabot.analytics import technicals as T
from fundabot.analytics.metrics import Metric


def series(n_days: int, start=date(2025, 1, 1), step: float = 1.0, base: float = 100.0) -> pd.DataFrame:
    """Linear closes on business days: close = base + step * i."""
    dates = pd.bdate_range(start, periods=n_days)
    return pd.DataFrame({"date": [d.date() for d in dates], "close": [base + step * i for i in range(n_days)]})


def test_price_stats_windows_and_mas():
    df = series(400)
    s = T.price_stats(df)
    assert s["asof"] == df.date.iloc[-1] and s["close"] == df.close.iloc[-1] and s["n_days"] == 400
    one_month = T.close_on_or_before_date(df, s["asof"] - timedelta(days=30))
    assert abs(s["ret_1m"] - (s["close"] / one_month - 1)) < 1e-12
    assert s["ret_12m"] is not None and s["ret_12m"] > s["ret_6m"] > s["ret_3m"] > s["ret_1m"] > 0
    assert s["ma50"] == pytest.approx(sum(df.close.iloc[-50:]) / 50) and s["ma200"] == pytest.approx(sum(df.close.iloc[-200:]) / 200)
    assert s["above_ma50"] and s["above_ma200"] and s["ma50_slope_20d"] == pytest.approx(20 / (s["ma50"] - 20), rel=1e-6)
    assert s["high_52w"] == s["close"] and s["drawdown_52w"] == 0 and s["dist_52w_low"] > 0 and s["vol_20d"] > 0


def test_short_history_is_insufficient():
    df = series(120)
    s = T.price_stats(df)
    assert s["ma200"] is None and s["ret_12m"] is None and s["ret_3m"] is not None
    assert T.outlook(s, [])["label"] == "insufficient data"


def test_largest_moves_sorted_by_abs():
    df = series(300)
    df.loc[250:, "close"] *= 0.90  # a -10% day that sticks
    df.loc[270:, "close"] *= 1.05  # a +5% day that sticks
    moves = T.largest_moves(df, window_days=120, n=3)
    assert moves[0]["date"] == df.date[250] and moves[0]["ret"] == pytest.approx(df.close[250] / df.close[249] - 1) and moves[0]["id"] == f"price:{df.date[250]}"
    assert moves[1]["date"] == df.date[270] and abs(moves[2]["ret"]) < abs(moves[1]["ret"])
    assert all(m["date"] >= df.date.iloc[-1] - timedelta(days=120) for m in moves)


def test_match_events_within_two_trading_days():
    df = series(30)
    dates = list(df.date)
    moves = [{"id": f"price:{dates[10]}", "date": dates[10], "close": 1.0, "ret": -0.05}]
    ev_near = T.Event(dates[12], "filing", "filing:a", "8-K", None)
    ev_far = T.Event(dates[13], "news", "news:b", "headline", "http://x")
    ev_before = T.Event(dates[8], "earnings", "earnings:x", "EPS", None)
    out = T.match_events(moves, [ev_near, ev_far, ev_before], dates, within=2)
    assert [e.id for e in out[0]["events"]] == ["earnings:x", "filing:a"]


def stats(**kw):
    base = {"ma50": 100, "ma200": 95, "above_ma50": True, "above_ma200": True, "ma50_slope_20d": 0.02, "drawdown_52w": -0.05, "close": 105}
    base.update(kw)
    return base


def metrics(rev=(0.1, 0.1, 0.1, 0.1), pe_pct=0.5, evs_pct=0.5):
    qs = ["Q4FY2025", "Q1FY2026", "Q2FY2026", "Q3FY2026"]
    ms = [Metric("revenue_yoy", q, v, "ratio", "x") for q, v in zip(qs, rev)] + [Metric("eps_diluted_yoy", q, v, "ratio", "x") for q, v in zip(qs, rev)]
    ms += [Metric("pe_5y_percentile", "current", pe_pct, "ratio", "x"), Metric("ev_sales_5y_percentile", "current", evs_pct, "ratio", "x"),
           Metric("eps_beat_streak", "current", 4, "count", "x")]
    return ms


def test_outlook_labels():
    o = T.outlook(stats(), metrics(rev=(0.05, 0.05, 0.10, 0.12), pe_pct=0.95))
    assert o["label"] == "growing / extended" and o["components"]["momentum"]["direction"] == "accelerating" and o["components"]["valuation"]["state"] == "stretched"
    assert o["components"]["momentum"]["revenue_yoy"][-1] == 0.12 and "rule" in o
    assert T.outlook(stats(), metrics(rev=(0.05, 0.05, 0.10, 0.12)))["label"] == "growing / fair"
    bottom = stats(above_ma50=True, above_ma200=False, ma50_slope_20d=0.01, drawdown_52w=-0.3)
    assert T.outlook(bottom, metrics())["label"] == "bottoming"
    down = stats(above_ma50=False, above_ma200=False, ma50_slope_20d=-0.02)
    assert T.outlook(down, metrics(rev=(0.2, 0.2, 0.05, 0.02)))["label"] == "declining"
    assert T.outlook(stats(), metrics(rev=(0.2, 0.2, 0.05, 0.02)))["label"] == "decelerating"
    assert T.outlook(stats(ma200=None), metrics())["label"] == "insufficient data"
    assert T.outlook(stats(), [])["label"] == "insufficient data"


@pytest.mark.db
def test_events_for_merges_sources(db_conn):
    from datetime import datetime, timezone

    cik = 320193
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO surprises (cik, report_date, eps_actual, eps_estimated) VALUES (%s, '2026-07-30', 1.57, 1.43)", (cik,))
        cur.execute("INSERT INTO splits (cik, date, ratio) VALUES (%s, '2026-06-10', 4)", (cik,))
        cur.execute("INSERT INTO transcripts (cik, fy, fq, call_date, content) VALUES (%s, 2026, 3, '2026-07-30', 'x')", (cik,))
        cur.execute("INSERT INTO news (id, cik, published, publisher, site, title, url, text, kind) VALUES ('n1', %s, %s, 'Reuters', 'reuters.com', 'Apple beats', 'http://r/1', 't', 'news')",
                    (cik, datetime(2026, 7, 31, 12, tzinfo=timezone.utc)))
    filings = pd.DataFrame([{"accession": "0000320193-26-000077", "form": "8-K", "filed": date(2026, 7, 30), "primary_doc_url": "http://sec/8k"},
                            {"accession": "0000320193-26-000001", "form": "4", "filed": date(2026, 7, 1), "primary_doc_url": "http://sec/4"}])
    events = T.events_for(db_conn, cik, "AAPL", date(2026, 6, 1), date(2026, 8, 31), list_filings=lambda c: filings)
    assert sorted(e.id for e in events) == ["earnings:2026-07-30", "filing:0000320193-26-000077", "news:n1", "split:2026-06-10", "transcript:T2026Q3"]
    assert next(e for e in events if e.kind == "earnings").title == "EPS 1.57 vs 1.43" and next(e for e in events if e.kind == "news").url == "http://r/1"


@pytest.mark.db
def test_events_include_grade_changes_only(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO grades (cik, date, company, previous_grade, new_grade, action) VALUES (1, '2026-07-02', 'JP Morgan', 'Neutral', 'Overweight', 'upgrade'), (1, '2026-07-03', 'Needham', 'Hold', 'Hold', 'maintain')")
    events = T.events_for(db_conn, 1, "X", date(2026, 7, 1), date(2026, 7, 31), list_filings=lambda c: pd.DataFrame(columns=["accession", "form", "filed", "primary_doc_url"]))
    assert [e.id for e in events] == ["grade:2026-07-02:JP Morgan"] and events[0].title == "JP Morgan: Neutral -> Overweight (upgrade)"
