import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from fundabot.ingest import fmp

FIX = Path(__file__).parent / "fixtures"


def fixtures():
    return {k: json.loads((FIX / f"fmp_aapl_{k}.json").read_text()) for k in ("estimates_annual", "earnings", "price_target")}


def test_parse_estimates_fields():
    rows = fmp.parse_estimates(fixtures()["estimates_annual"])
    by_end = {r["period_end"]: r for r in rows}
    assert date(2030, 9, 27) in by_end and by_end[date(2030, 9, 27)]["eps_avg"] == 12.905
    assert by_end[date(2030, 9, 27)]["revenue_avg"] == 647707000000 and by_end[date(2030, 9, 27)]["analysts"] >= 0
    assert set(rows[0]) == {"period_end", "revenue_avg", "revenue_low", "revenue_high", "eps_avg", "eps_low", "eps_high", "analysts"}


def test_parse_surprises_skips_future_and_keeps_actuals():
    rows = fmp.parse_surprises(fixtures()["earnings"])
    assert all(r["eps_actual"] is not None for r in rows)
    first = max(rows, key=lambda r: r["report_date"])
    assert first["report_date"] == date(2026, 7, 30) and first["eps_actual"] == 2.02 and first["eps_estimated"] == 1.89


def test_parse_price_target():
    pt = fmp.parse_price_target(fixtures()["price_target"])
    assert pt == {"consensus": 341.68, "median": 357.5, "high": 400.0, "low": 245.0}


def test_restricted_endpoint_raises(monkeypatch):
    class R:
        status_code, text = 402, "Premium Query Parameter: 'Special Endpoint ..."
    monkeypatch.setattr(fmp.httpx, "get", lambda *a, **k: R())
    with pytest.raises(fmp.FMPError):
        fmp.get("earnings", symbol="AAPL")


@pytest.mark.db
def test_ingest_and_load_roundtrip(db_conn, monkeypatch):
    monkeypatch.setattr(fmp, "fetch_all", lambda ticker: fixtures())
    t1 = datetime(2026, 10, 15, tzinfo=timezone.utc)
    fmp.ingest(db_conn, 320193, "AAPL", fetched_at=t1)
    fmp.ingest(db_conn, 320193, "AAPL", fetched_at=t1 + timedelta(days=31))
    d = fmp.load(db_conn, 320193)
    assert d["estimates"].fetched_at.nunique() == 2 and (d["estimates"].period == "annual").all()
    assert d["surprises"].report_date.is_unique and len(d["surprises"]) == 11
    assert len(d["price_targets"]) == 2 and float(d["price_targets"].consensus.iloc[0]) == 341.68
