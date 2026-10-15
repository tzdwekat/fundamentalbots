from datetime import date, datetime, timedelta, timezone

import pandas as pd

from fundabot.analytics.earnings import compute_earnings_metrics

T1 = datetime(2026, 10, 15, tzinfo=timezone.utc)
FYE, OFFSET = (9, 27), 0  # Apple-like


def get(ms, mid, period="current"):
    hits = [m for m in ms if m.metric_id == mid and m.period == period]
    assert len(hits) == 1, f"{mid}@{period}: {len(hits)}"
    return hits[0]


def surprises(rows):
    return pd.DataFrame(rows, columns=["report_date", "eps_actual", "eps_estimated", "revenue_actual", "revenue_estimated"])


def estimates(rows):
    return pd.DataFrame(rows, columns=["fetched_at", "period", "period_end", "eps_avg", "revenue_avg"])


def fmp(sur=None, est=None, pt=None):
    return {"surprises": surprises(sur or []), "estimates": estimates(est or []),
            "price_targets": pd.DataFrame(pt or [], columns=["fetched_at", "consensus", "median", "high", "low"])}


SUR = [(date(2025, 10, 30), 1.85, 1.73, 102e9, 100e9), (date(2026, 1, 29), 2.85, 2.67, 143e9, 140e9),
       (date(2026, 4, 30), 2.01, 1.95, 111e9, 112e9), (date(2026, 7, 30), 2.02, 1.89, 109e9, 108e9)]
EST = [(T1, "annual", date(2026, 9, 27), 8.83, 420e9), (T1, "annual", date(2027, 9, 27), 9.615, 450e9)]


def test_surprise_and_streak():
    ms = compute_earnings_metrics(fmp(SUR, EST), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    m = get(ms, "eps_surprise", "Q3FY2026")
    assert abs(m.value - (2.02 - 1.89) / 1.89) < 1e-9 and "vendor_fmp" in m.flags
    assert get(ms, "revenue_surprise", "Q2FY2026").value < 0
    assert get(ms, "eps_beat_streak").value == 4
    assert get(ms, "eps_surprise", "Q1FY2026").period == "Q1FY2026"  # report 2026-01-29 -> quarter ending Dec 2025


def test_next_fy_growth_same_basis():
    ms = compute_earnings_metrics(fmp(SUR, EST), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert abs(get(ms, "eps_actual_fmp_ttm").value - (1.85 + 2.85 + 2.01 + 2.02)) < 1e-9
    nxt = get(ms, "eps_estimate_next_fy")
    assert nxt.value == 9.615 and nxt.inputs["period_end"] == "2027-09-27"
    assert abs(get(ms, "eps_growth_consensus_next_fy").value - (9.615 / 8.73 - 1)) < 1e-9
    assert get(ms, "revenue_estimate_next_fy").value == 450e9


def test_forward_pe_and_target_upside():
    ms = compute_earnings_metrics(fmp(SUR, EST, [(T1, 341.68, 357.5, 400.0, 245.0)]), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert abs(get(ms, "forward_pe").value - 340.0 / 9.615) < 1e-9
    assert abs(get(ms, "price_target_upside").value - (341.68 / 340.0 - 1)) < 1e-9
    assert get(ms, "forward_pe").null_reason is None
    ms2 = compute_earnings_metrics(fmp(SUR, EST), date(2026, 10, 14), None, FYE, OFFSET, T1)
    assert get(ms2, "forward_pe").null_reason == "price_unavailable"


def test_revision_needs_history():
    ms = compute_earnings_metrics(fmp(SUR, EST), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert get(ms, "eps_estimate_revision_30d").null_reason == "no_history"
    older = [(T1 - timedelta(days=31), "annual", date(2027, 9, 27), 9.0, 440e9)]
    ms = compute_earnings_metrics(fmp(SUR, EST + older), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert abs(get(ms, "eps_estimate_revision_30d").value - (9.615 / 9.0 - 1)) < 1e-9
    assert get(ms, "eps_estimate_revision_90d").null_reason == "no_history"


def test_no_fmp_coverage_is_null():
    ms = compute_earnings_metrics(fmp(), date(2026, 10, 14), 340.0, FYE, OFFSET, T1)
    assert ms and all(m.value is None and m.null_reason == "no_coverage" and "vendor_fmp" in m.flags for m in ms)
