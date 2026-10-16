import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from fundabot.analytics.earnings import compute_earnings_metrics
from fundabot.ingest import fmp
from tests.test_earnings import EST, FYE, OFFSET, SUR, T1, estimates, fmp as base_fmp, get, surprises

FIX = Path(__file__).parent / "fixtures"


def fixtures():
    return {k: json.loads((FIX / f"fmp_aapl_{k}.json").read_text()) for k in ("estimates_annual", "estimates_quarter", "earnings", "price_target", "institutional", "insider_stats")}


@pytest.mark.db
def test_quarterly_rows_stored_and_loaded(db_conn, monkeypatch):
    monkeypatch.setattr(fmp, "fetch_all", lambda t: fixtures())
    fmp.ingest(db_conn, 320193, "AAPL", fetched_at=T1)
    d = fmp.load(db_conn, 320193)
    assert set(d["estimates"].period) == {"annual", "quarter"} and (d["estimates"].period == "quarter").sum() == 8
    assert list(d["institutional"].columns) == ["quarter_end", "holders", "holders_change", "shares", "shares_change"] and len(d["institutional"]) == 2
    assert list(d["insiders"].columns) == ["year", "quarter", "acquired", "disposed", "purchases", "sales"] and len(d["insiders"]) == 4
    assert float(d["insiders"].sort_values(["year", "quarter"]).iloc[-1].acquired) == 774053


def full_fmp(quarter_rows=(), inst=(), ins=()):
    f = base_fmp(SUR, EST)
    f["estimates"] = pd.concat([f["estimates"], estimates(quarter_rows)], ignore_index=True) if quarter_rows else f["estimates"]
    f["institutional"] = pd.DataFrame(inst, columns=["quarter_end", "holders", "holders_change", "shares", "shares_change"])
    f["insiders"] = pd.DataFrame(ins, columns=["year", "quarter", "acquired", "disposed", "purchases", "sales"])
    return f


def test_next_q_consensus_and_revision():
    q = [(T1, "quarter", date(2026, 12, 27), 2.5, 140e9), (T1, "quarter", date(2027, 3, 27), 2.2, 120e9), (T1 - timedelta(days=31), "quarter", date(2026, 12, 27), 2.4, 139e9)]
    ms = compute_earnings_metrics(full_fmp(q), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert get(ms, "eps_estimate_next_q").value == 2.5 and get(ms, "revenue_estimate_next_q").value == 140e9
    assert get(ms, "eps_estimate_next_q").inputs["period_end"] == "2026-12-27"
    assert abs(get(ms, "eps_estimate_next_q_revision_30d").value - (2.5 / 2.4 - 1)) < 1e-9
    assert get(ms, "eps_estimate_next_q_revision_90d").null_reason == "no_history"


def test_institutional_and_insider_metrics():
    inst = [(date(2026, 3, 31), 6418, 20, 9.4e9, 1e8), (date(2026, 6, 30), 6474, 56, 9.78e9, 3.7e8)]
    ins = [(2026, 3, 455601, 59762, 2, 3), (2026, 4, 774053, 1223409, 0, 12)]
    ms = compute_earnings_metrics(full_fmp((), inst, ins), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert abs(get(ms, "inst_holders_change_pct").value - 56 / 6418) < 1e-9 and get(ms, "inst_holders_change_pct").inputs["quarter_end"] == "2026-06-30"
    assert abs(get(ms, "inst_shares_change_pct").value - 3.7e8 / (9.78e9 - 3.7e8)) < 1e-9
    assert get(ms, "insider_net_shares_90d").value == 774053 - 1223409 and get(ms, "insider_net_shares_90d").unit == "shares"
    assert get(ms, "insider_buy_sell_ratio").value == 0.0  # 0 purchases over 12 sales is a real ratio
    ms0 = compute_earnings_metrics(full_fmp((), inst, [(2026, 4, 1.0, 0.0, 0, 0)]), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert get(ms0, "insider_buy_sell_ratio").value is None and get(ms0, "insider_buy_sell_ratio").null_reason == "nonpositive_denominator"
    assert all("vendor_fmp" in get(ms, k).flags for k in ("inst_holders_change_pct", "insider_net_shares_90d"))


def test_missing_ownership_data_is_null():
    ms = compute_earnings_metrics(full_fmp(), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    for k in ("eps_estimate_next_q", "inst_holders_change_pct", "insider_net_shares_90d"):
        assert get(ms, k).value is None and get(ms, k).null_reason == "no_coverage"
    f = base_fmp(SUR, EST)  # frames without the new keys at all (older callers)
    assert get(compute_earnings_metrics(f, date(2026, 10, 14), 340.0, FYE, OFFSET, T1), "inst_holders_change_pct").null_reason == "no_coverage"


def test_skeptic_reads_ownership():
    from fundabot.agents import personas
    assert any("insider" in r.lower() or "13f" in r.lower() for r in personas.load_all()["skeptic"]["reads_most"])


def test_institutional_ignores_quarters_still_being_filed():
    """13F filings arrive up to 45 days after quarter end; a quarter younger than 60 days is partial and must be skipped."""
    inst = [(date(2026, 6, 30), 6474, 56, 9.78e9, 3.7e8), (date(2026, 9, 30), 1016, -5458, 1.22e8, -9.66e9)]
    ms = compute_earnings_metrics(full_fmp((), inst, []), date(2026, 10, 16), 340.0, FYE, OFFSET, T1)
    m = get(ms, "inst_holders_change_pct")
    assert m.inputs["quarter_end"] == "2026-06-30" and abs(m.value - 56 / 6418) < 1e-9
    ms2 = compute_earnings_metrics(full_fmp((), inst, []), date(2026, 12, 15), 340.0, FYE, OFFSET, T1)
    assert get(ms2, "inst_holders_change_pct").inputs["quarter_end"] == "2026-09-30"
