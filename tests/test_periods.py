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
