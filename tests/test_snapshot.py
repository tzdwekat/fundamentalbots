from datetime import date

import pytest

from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag
from fundabot.orchestrate import snapshot


def test_snapshot_id_deterministic():
    a = snapshot.snapshot_id(320193, 7, ["b", "a"], date(2026, 10, 15))
    assert a == snapshot.snapshot_id(320193, 7, ["a", "b"], date(2026, 10, 15))
    assert a != snapshot.snapshot_id(320193, 7, ["a", "b"], date(2026, 10, 16))
    assert len(a) == 16


@pytest.mark.db
def test_store_load_metrics_roundtrip(db_conn):
    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["a"], date(2026, 10, 15), 200.0, 1.5e10)
    ms = [
        Metric("revenue", "FY2024", 391035000000.0, "USD", "xbrl:Revenues", {}, ["restated"], "Revenues"),
        Metric("gross_margin", "FY2024", 0.46, "ratio", "gross_profit / revenue", {"revenue": {"item": "revenue", "period": "FY2024", "value": 1.0, "tag": "Revenues"}}, []),
        Metric("pe", "current", None, "ratio", "price / eps_ttm", {}, [], None, "negative_denominator"),
    ]
    snapshot.store_metrics(db_conn, sid, ms)
    snapshot.store_metrics(db_conn, sid, ms)  # idempotent
    snapshot.store_flags(db_conn, sid, [RedFlag("accruals_high", "high", ["metric:accruals_ratio@FY2024"], "x")])
    loaded = sorted(snapshot.load_metrics(db_conn, sid), key=lambda m: m.metric_id)
    assert loaded == sorted(ms, key=lambda m: m.metric_id)
    assert snapshot.load_flags(db_conn, sid) == [RedFlag("accruals_high", "high", ["metric:accruals_ratio@FY2024"], "x")]
    assert snapshot.latest_for(db_conn, "AAPL")["id"] == sid
