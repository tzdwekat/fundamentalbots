import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fundabot.analytics import periods
from fundabot.ingest.edgar_facts import normalize

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def tag_map():
    return periods.load_tag_map()


def facts(ticker):
    return normalize(json.loads((FIX / f"{ticker}_companyfacts.json").read_text()))


@pytest.fixture(scope="module")
def aapl(tag_map):
    return periods.build_series(facts("AAPL"), tag_map)


def row(series, item, period):
    df = series[item]
    sub = df[df.period == period]
    assert len(sub) == 1, f"{item}@{period}: {len(sub)} rows"
    return sub.iloc[0]


def test_aapl_fiscal_year_end():
    m, d = periods.fiscal_year_end(facts("AAPL"), periods.load_tag_map())
    assert m == 9 and d >= 24


def test_aapl_annual_revenue(aapl):
    r = row(aapl, "revenue", "FY2024")
    assert r.value == 391035000000 and r.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert r.fy == 2024 and r.fq == 0 and r.end == date(2024, 9, 28)


def test_ytd_excluded(aapl):
    df = aapl["revenue"]
    dur = (pd.to_datetime(df.end) - pd.to_datetime(df.start)).dt.days
    assert not ((dur > 150) & (dur < 300)).any()


def test_q4_derived(aapl):
    fy = row(aapl, "revenue", "FY2024").value
    q = [row(aapl, "revenue", f"Q{i}FY2024") for i in (1, 2, 3, 4)]
    assert q[3].value == fy - (q[0].value + q[1].value + q[2].value)
    assert q[3].derived and not q[0].derived
    assert q[0].end == date(2023, 12, 30)


def test_msft_june_fiscal_year(tag_map):
    s = periods.build_series(facts("MSFT"), tag_map)
    r = row(s, "revenue", "FY2024")
    assert r.end == date(2024, 6, 30) and r.value == 245122000000
    q = s["revenue"][s["revenue"].end == date(2024, 3, 31)]
    assert list(q.period) == ["Q3FY2024"]
    assert row(s, "cost_of_revenue", "FY2024").tag == "CostOfGoodsAndServicesSold"


def test_jpm_uses_revenues_tag(tag_map):
    s = periods.build_series(facts("JPM"), tag_map)
    assert s["revenue"].tag.iloc[0] == "Revenues"
    assert "cost_of_revenue" not in s


def test_instants_labelled(aapl):
    r = row(aapl, "total_assets", "FY2024")
    assert r.end == date(2024, 9, 28)
    assert row(aapl, "total_assets", "Q4FY2024").value == r.value
    assert row(aapl, "total_assets", "Q1FY2024").end == date(2023, 12, 30)


def test_restated_flag(tag_map):
    base = dict(taxonomy="us-gaap", tag="Revenues", unit="USD", fy=2024, fp="FY", form="10-K")
    rows = [
        dict(base, start_date=date(2023, 1, 1), end_date=date(2023, 12, 31), value=100.0, accession="a-1", filed=date(2024, 2, 1)),
        dict(base, start_date=date(2023, 1, 1), end_date=date(2023, 12, 31), value=102.0, accession="a-2", filed=date(2025, 2, 1)),
        dict(base, start_date=date(2024, 1, 1), end_date=date(2024, 12, 31), value=110.0, accession="a-2", filed=date(2025, 2, 1)),
    ]
    s = periods.build_series(pd.DataFrame(rows), {"revenue": ["Revenues"]})
    r = row(s, "revenue", "FY2023")
    assert r.value == 102.0 and r.restated and r.accession == "a-2"
    assert not row(s, "revenue", "FY2024").restated


def test_only_periodic_forms_used(tag_map):
    base = dict(taxonomy="us-gaap", tag="Revenues", unit="USD", fy=2024, fp="FY", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
    rows = [
        dict(base, form="10-K", value=100.0, accession="k-1", filed=date(2025, 2, 1)),
        dict(base, form="8-K", value=999.0, accession="e-1", filed=date(2025, 3, 1)),
    ]
    s = periods.build_series(pd.DataFrame(rows), {"revenue": ["Revenues"]})
    assert row(s, "revenue", "FY2024").value == 100.0


def test_ttm(aapl):
    assert periods.ttm(aapl["revenue"], date(2024, 9, 28)) == 391035000000
    assert periods.ttm(aapl["revenue"], date(2009, 1, 1)) is None


def test_q2_q3_derived_from_ytd(aapl):
    """10-Q cash-flow statements are cumulative: Q2 = 6-month YTD - Q1, Q3 = 9-month YTD - 6-month YTD."""
    f = facts("AAPL")
    ocf = f[(f.tag == "NetCashProvidedByUsedInOperatingActivities") & (f.form.isin(["10-Q", "10-K"]))]
    ytd6 = ocf[(ocf.start_date == date(2023, 10, 1)) & (ocf.end_date == date(2024, 3, 30))].value.iloc[0]
    ytd9 = ocf[(ocf.start_date == date(2023, 10, 1)) & (ocf.end_date == date(2024, 6, 29))].value.iloc[0]
    q1 = row(aapl, "ocf", "Q1FY2024")
    q2 = row(aapl, "ocf", "Q2FY2024")
    q3 = row(aapl, "ocf", "Q3FY2024")
    assert not q1.derived and q2.derived and q3.derived
    assert q2.value == ytd6 - q1.value and q2.start == date(2023, 12, 31) and q2.end == date(2024, 3, 30)
    assert q3.value == ytd9 - ytd6
    assert row(aapl, "ocf", "Q4FY2024").value == row(aapl, "ocf", "FY2024").value - ytd9
    assert periods.ttm(aapl["ocf"], date(2024, 9, 28)) == row(aapl, "ocf", "FY2024").value


def test_insufficient_history_raises():
    base = dict(taxonomy="us-gaap", tag="Revenues", unit="USD", fy=2026, fp="Q2", form="10-Q", accession="q", filed=date(2026, 8, 1))
    df = pd.DataFrame([dict(base, start_date=date(2026, 4, 1), end_date=date(2026, 6, 30), value=1.0)])
    with pytest.raises(periods.InsufficientHistory):
        periods.build_series(df, {"revenue": ["Revenues"]})


def test_costco_style_12_week_quarters_derive_from_ytd():
    """12/12/12/16-week quarters: Q1 = 84 days, YTD6 = 168, YTD9 = 252, FY = 364."""
    base = dict(taxonomy="us-gaap", tag="Revenues", unit="USD", fy=2025, fp="FY", form="10-K", accession="k", filed=date(2025, 10, 1))
    rows = [
        dict(base, form="10-Q", accession="q1", start_date=date(2024, 9, 2), end_date=date(2024, 11, 24), value=10.0),
        dict(base, form="10-Q", accession="q2", start_date=date(2024, 9, 2), end_date=date(2025, 2, 16), value=25.0),
        dict(base, form="10-Q", accession="q3", start_date=date(2024, 9, 2), end_date=date(2025, 5, 11), value=45.0),
        dict(base, start_date=date(2024, 9, 2), end_date=date(2025, 8, 31), value=70.0),
    ]
    s = periods.build_series(pd.DataFrame(rows), {"revenue": ["Revenues"]})
    vals = {r.period: (r.value, r.derived) for r in s["revenue"].itertuples()}
    assert vals["Q2FY2025"] == (15.0, True) and vals["Q3FY2025"] == (20.0, True) and vals["Q4FY2025"] == (25.0, True)
    assert periods.ttm(s["revenue"], date(2025, 8, 31)) == 70.0


def test_fy_label_follows_company_convention():
    """Home Depot's year ending 2026-02-01 is its 'fiscal 2025'; the 10-K's fy field carries that convention."""
    base = dict(taxonomy="us-gaap", tag="Revenues", unit="USD", fp="FY", form="10-K", accession="k", filed=date(2026, 3, 20))
    rows = [
        dict(base, fy=2024, start_date=date(2024, 1, 29), end_date=date(2025, 2, 2), value=150.0),
        dict(base, fy=2025, start_date=date(2025, 2, 3), end_date=date(2026, 2, 1), value=160.0),
        dict(base, fy=2026, fp="Q1", form="10-Q", accession="q", filed=date(2026, 5, 20), start_date=date(2026, 2, 2), end_date=date(2026, 5, 3), value=40.0),
    ]
    s = periods.build_series(pd.DataFrame(rows), {"revenue": ["Revenues"]})
    assert set(s["revenue"].period) == {"FY2024", "FY2025", "Q1FY2026"}
