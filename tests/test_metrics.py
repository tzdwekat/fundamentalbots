import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fundabot.analytics import periods
from fundabot.analytics.metrics import Metric, compute_fundamental_metrics
from fundabot.ingest.edgar_facts import normalize

FIX = Path(__file__).parent / "fixtures"


def series_for(ticker):
    facts = normalize(json.loads((FIX / f"{ticker}_companyfacts.json").read_text()))
    return periods.build_series(facts, periods.load_tag_map())


@pytest.fixture(scope="module")
def aapl():
    return compute_fundamental_metrics(series_for("AAPL"))


def get(metrics, metric_id, period) -> Metric:
    hits = [m for m in metrics if m.metric_id == metric_id and m.period == period]
    assert len(hits) == 1, f"{metric_id}@{period}: {len(hits)}"
    return hits[0]


def hand_series(**items):
    """items: name -> {period: value}; builds annual series with FY end Dec 31."""
    out = {}
    for name, vals in items.items():
        rows = []
        for period, v in vals.items():
            fy = int(period[2:])
            rows.append((period, fy, 0, date(fy, 1, 1), date(fy, 12, 31), v, "acc", "Tag", False, False, False))
        out[name] = pd.DataFrame(rows, columns=periods.COLUMNS)
    return out


def test_aapl_gross_margin_fy2024(aapl):
    m = get(aapl, "gross_margin", "FY2024")
    assert abs(m.value - 180683 / 391035) < 0.001
    assert m.unit == "ratio" and m.tag_used is None
    assert set(m.inputs) == {"gross_profit", "revenue"}
    assert m.inputs["revenue"]["value"] == 391035000000
    assert m.inputs["revenue"]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert get(aapl, "revenue", "FY2024").tag_used == "RevenueFromContractWithCustomerExcludingAssessedTax"


def test_base_item_provenance(aapl):
    m = get(aapl, "revenue", "FY2024")
    assert m.inputs == {"accession": "0000320193-25-000079", "start": "2023-10-01", "end": "2024-09-28"}


def test_aapl_fcf_fy2024(aapl):
    m = get(aapl, "fcf", "FY2024")
    assert m.value == 118254000000 - 9447000000 and m.unit == "USD"


def test_missing_tag_is_null():
    jpm = compute_fundamental_metrics(series_for("JPM"))
    m = get(jpm, "gross_margin", "FY2024")
    assert m.value is None and m.null_reason == "tag_missing"
    assert get(jpm, "net_income", "FY2024").value > 0


def test_cagr_nonpositive_endpoint():
    s = hand_series(operating_income={"FY2021": -5.0, "FY2022": 1.0, "FY2023": 2.0, "FY2024": 4.0})
    m = get(compute_fundamental_metrics(s), "operating_income_cagr_3y", "FY2024")
    assert m.value is None and m.null_reason == "nonpositive_endpoint"


def test_cagr_value():
    s = hand_series(revenue={"FY2021": 100.0, "FY2024": 133.1})
    m = get(compute_fundamental_metrics(s), "revenue_cagr_3y", "FY2024")
    assert abs(m.value - 0.10) < 1e-6


def test_out_of_bounds_flag():
    s = hand_series(revenue={"FY2024": 1.0}, gross_profit={"FY2024": 5.0})
    m = get(compute_fundamental_metrics(s), "gross_margin", "FY2024")
    assert m.value == 5.0 and "out_of_bounds" in m.flags


def test_derived_flag_propagates(aapl):
    assert "derived" in get(aapl, "revenue_yoy", "Q4FY2024").flags
    assert "derived" not in get(aapl, "revenue_yoy", "Q1FY2025").flags


def test_nonpositive_denominator():
    s = hand_series(net_income={"FY2024": 10.0}, dividends_paid={"FY2024": 0.0}, ocf={"FY2024": 12.0}, capex={"FY2024": 2.0})
    m = get(compute_fundamental_metrics(s), "dividend_coverage", "FY2024")
    assert m.value is None and m.null_reason == "nonpositive_denominator"


def test_roic_uses_clamped_tax_rate():
    s = hand_series(
        operating_income={"FY2023": 100.0, "FY2024": 100.0}, income_tax={"FY2024": 90.0}, pretax_income={"FY2024": 100.0},
        equity={"FY2023": 400.0, "FY2024": 400.0}, long_term_debt={"FY2023": 100.0, "FY2024": 100.0},
        cash={"FY2023": 0.0, "FY2024": 0.0},
    )
    m = get(compute_fundamental_metrics(s), "roic", "FY2024")
    assert abs(m.value - 100 * (1 - 0.35) / 500) < 1e-9 and "tax_rate_clamped" in m.flags and "partial_debt" in m.flags


def test_net_buybacks_without_issuance_tag():
    s = hand_series(revenue={"FY2024": 1.0}, buybacks={"FY2024": 50.0})
    m = get(compute_fundamental_metrics(s), "net_buybacks", "FY2024")
    assert m.value == 50.0 and "no_issuance_data" in m.flags


def test_da_composed_from_depreciation_and_amortization():
    s = hand_series(revenue={"FY2024": 10.0}, operating_income={"FY2024": 3.0}, depreciation={"FY2024": 1.0}, amortization={"FY2024": 0.5})
    m = get(compute_fundamental_metrics(s), "da", "FY2024")
    assert m.value == 1.5 and "da_composed" in m.flags
    assert get(compute_fundamental_metrics(s), "ebitda", "FY2024").value == 4.5
    s2 = hand_series(revenue={"FY2024": 10.0}, depreciation={"FY2024": 1.0})
    m2 = get(compute_fundamental_metrics(s2), "da", "FY2024")
    assert m2.value == 1.0 and "depreciation_only" in m2.flags


def test_total_debt_from_reported_total_tag():
    s = hand_series(revenue={"FY2024": 1.0}, total_debt_reported={"FY2024": 50.0}, cash={"FY2024": 10.0})
    m = get(compute_fundamental_metrics(s), "total_debt", "FY2024")
    assert m.value == 50.0 and "debt_total_tag" in m.flags
    assert get(compute_fundamental_metrics(s), "net_debt", "FY2024").value == 40.0
    s2 = hand_series(revenue={"FY2024": 1.0}, total_debt_reported={"FY2024": 50.0}, long_term_debt={"FY2024": 30.0}, short_term_debt={"FY2024": 5.0})
    assert get(compute_fundamental_metrics(s2), "total_debt", "FY2024").value == 35.0


def test_split_adjusted_flag_reaches_metrics():
    rows = []
    for fy, v, filed, acc in [(2019, 8.0, date(2019, 11, 1), "a"), (2020, 2.2, date(2020, 11, 1), "b"), (2021, 2.5, date(2021, 11, 1), "c")]:
        rows.append(dict(taxonomy="us-gaap", tag="EarningsPerShareDiluted", unit="USD/shares", fy=fy, fp="FY", form="10-K", accession=acc, filed=filed,
                         start_date=date(fy, 1, 1), end_date=date(fy, 12, 31), value=v))
    facts = periods.adjust_for_splits(pd.DataFrame(rows), pd.DataFrame({"date": [date(2020, 6, 1)], "ratio": [4.0]}))
    s = periods.build_series(facts, {"eps_diluted": ["EarningsPerShareDiluted"]})
    ms = compute_fundamental_metrics(s)
    assert get(ms, "eps_diluted", "FY2019").value == 2.0 and "split_adjusted" in get(ms, "eps_diluted", "FY2019").flags
    yoy = get(ms, "eps_diluted_yoy", "FY2020")
    assert abs(yoy.value - 0.1) < 1e-9 and "split_adjusted" in yoy.flags
