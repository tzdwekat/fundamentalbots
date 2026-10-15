import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from fundabot.analytics import periods, valuation
from fundabot.analytics.metrics import compute_fundamental_metrics
from fundabot.ingest.edgar_facts import normalize
from fundabot.ingest.prices import close_on_or_after

FIX = Path(__file__).parent / "fixtures"


def get(metrics, metric_id, period):
    hits = [m for m in metrics if m.metric_id == metric_id and m.period == period]
    assert len(hits) == 1, f"{metric_id}@{period}: {len(hits)}"
    return hits[0]


def dei_facts(rows):
    """rows: (end_date, value, accession[, filed])"""
    rows = [r if len(r) == 4 else (*r, r[0]) for r in rows]
    return pd.DataFrame(
        [("dei", "EntityCommonStockSharesOutstanding", "shares", None, e, v, 2024, "Q1", "10-Q", a, f) for e, v, a, f in rows],
        columns=["taxonomy", "tag", "unit", "start_date", "end_date", "value", "fy", "fp", "form", "accession", "filed"],
    )


def quarterly(item, values, first_fy=2022):
    """Calendar-year quarterly series: values listed Q1FY{first_fy} onward, plus annual sums."""
    rows = []
    fy, fq = first_fy, 1
    annual = {}
    for v in values:
        start = date(fy, 3 * (fq - 1) + 1, 1)
        end = (date(fy + (fq == 4), (3 * fq) % 12 + 1, 1) - timedelta(days=1))
        rows.append((f"Q{fq}FY{fy}", fy, fq, start, end, v, f"acc-{fy}-{fq}", "Tag", False, False))
        annual[fy] = annual.get(fy, 0) + v
        if fq == 4:
            rows.append((f"FY{fy}", fy, 0, date(fy, 1, 1), date(fy, 12, 31), annual[fy], f"acc-{fy}-4", "Tag", False, False))
            fy, fq = fy + 1, 1
        else:
            fq += 1
    return pd.DataFrame(rows, columns=periods.COLUMNS)


def prices(points):
    return pd.DataFrame([(d, c) for d, c in points], columns=["date", "close"])


def test_close_on_or_after():
    df = prices([(date(2024, 1, 2), 10.0), (date(2024, 1, 5), 11.0)])
    assert close_on_or_after(df, date(2024, 1, 3)) == (date(2024, 1, 5), 11.0)
    assert close_on_or_after(df, date(2024, 2, 1)) is None


def test_shares_multi_class_summed():
    facts = dei_facts([(date(2024, 1, 31), 100.0, "new", date(2024, 2, 2)), (date(2024, 1, 31), 50.0, "new", date(2024, 2, 2)),
                       (date(2024, 1, 31), 999.0, "old", date(2024, 2, 1))])
    df = valuation.shares_outstanding(facts)
    assert len(df) == 1 and df.shares.iloc[0] == 150.0 and df.accession.iloc[0] == "new"


@pytest.fixture(scope="module")
def aapl():
    facts = normalize(json.loads((FIX / "AAPL_companyfacts.json").read_text()))
    series = periods.build_series(facts, periods.load_tag_map())
    return facts, series, compute_fundamental_metrics(series)


def test_pe_current(aapl):
    facts, series, fund = aapl
    as_of = date(2026, 7, 31)
    px = prices([(as_of, 200.0)])
    out = valuation.compute_valuation(series, facts, px, pd.DataFrame(columns=["accession", "form", "filed", "period_end"]), as_of, fund)
    ttm_eps = periods.ttm(series["eps_diluted"], as_of)
    m = get(out, "pe", "current")
    assert abs(m.value - 200.0 / ttm_eps) < 1e-9
    assert m.inputs["price"]["value"] == 200.0 and m.inputs["eps_ttm"]["value"] == ttm_eps
    assert get(out, "shares_outstanding", "current").value == 14594180000
    assert get(out, "market_cap", "current").value == 200.0 * 14594180000


def test_pe_negative_earnings():
    series = {"revenue": quarterly("revenue", [10.0] * 8), "eps_diluted": quarterly("eps", [-1.0] * 8)}
    facts = dei_facts([(date(2023, 12, 31), 100.0, "a")])
    out = valuation.compute_valuation(series, facts, prices([(date(2024, 1, 15), 50.0)]), pd.DataFrame(columns=["accession", "form", "filed", "period_end"]),
                                      date(2024, 1, 15), compute_fundamental_metrics(series))
    m = get(out, "pe", "current")
    assert m.value is None and m.null_reason == "negative_denominator"


def test_history_range():
    series = {"revenue": quarterly("revenue", [10.0] * 12), "eps_diluted": quarterly("eps", [2.5] * 12)}
    facts = dei_facts([(date(2023, 1, 31), 100.0, "f1"), (date(2023, 7, 31), 100.0, "f2"), (date(2024, 1, 31), 100.0, "f3")])
    filings = pd.DataFrame(
        [("f1", "10-Q", date(2023, 2, 1), date(2022, 12, 31)), ("f2", "10-Q", date(2023, 8, 1), date(2023, 6, 30)), ("f3", "10-K", date(2024, 2, 1), date(2023, 12, 31))],
        columns=["accession", "form", "filed", "period_end"],
    )
    px = prices([(date(2023, 2, 1), 100.0), (date(2023, 8, 1), 200.0), (date(2024, 2, 1), 300.0), (date(2024, 12, 31), 250.0)])
    out = valuation.compute_valuation(series, facts, px, filings, date(2024, 12, 31), compute_fundamental_metrics(series))
    assert get(out, "pe", "Q4FY2022").value == 10.0 and get(out, "pe", "FY2023").value == 30.0
    assert get(out, "pe_5y_min", "current").value == 10.0
    assert get(out, "pe_5y_median", "current").value == 20.0
    assert get(out, "pe_5y_max", "current").value == 30.0
    assert abs(get(out, "pe_5y_percentile", "current").value - 2 / 3) < 1e-9


def test_dcf_reverse_roundtrip():
    ev = valuation.dcf_ev(100.0, 0.08)
    assert abs(valuation.implied_growth(ev, 100.0) - 0.08) < 1e-3


def test_price_unavailable(aapl):
    facts, series, fund = aapl
    out = valuation.compute_valuation(series, facts, None, pd.DataFrame(columns=["accession", "form", "filed", "period_end"]), date(2026, 7, 31), fund)
    assert out and all(m.value is None and m.null_reason == "price_unavailable" for m in out)


def test_history_ignores_amendments_and_duplicate_labels():
    series = {"revenue": quarterly("revenue", [10.0] * 12), "eps_diluted": quarterly("eps", [2.5] * 12)}
    facts = dei_facts([(date(2024, 1, 31), 100.0, "k")])
    filings = pd.DataFrame(
        [("k", "10-K", date(2024, 2, 1), date(2023, 12, 31)), ("ka", "10-K/A", date(2024, 4, 1), date(2023, 12, 31))],
        columns=["accession", "form", "filed", "period_end"],
    )
    px = prices([(date(2024, 2, 1), 300.0), (date(2024, 4, 1), 100.0), (date(2024, 12, 31), 250.0)])
    out = valuation.compute_valuation(series, facts, px, filings, date(2024, 12, 31), compute_fundamental_metrics(series))
    assert [m.value for m in out if m.metric_id == "pe" and m.period == "FY2023"] == [30.0]


def test_dcf_growth_falls_back_to_3y_when_5y_is_null():
    # FY2021..FY2024 only: fcf_cagr_5y is null (no FY2019), fcf_cagr_3y is 10%.
    ocf = [10.0] * 4 + [11.0] * 4 + [12.1] * 4 + [13.31] * 4
    series = {"revenue": quarterly("revenue", [100.0] * 16, 2021), "ocf": quarterly("ocf", ocf, 2021), "capex": quarterly("capex", [0.0] * 16, 2021),
              "cash": quarterly("cash", [0.0] * 16, 2021), "long_term_debt": quarterly("ltd", [0.0] * 16, 2021), "eps_diluted": quarterly("eps", [1.0] * 16, 2021)}
    fund = compute_fundamental_metrics(series)
    assert get(fund, "fcf_cagr_5y", "FY2024").value is None
    facts = dei_facts([(date(2024, 12, 31), 10.0, "a")])
    out = valuation.compute_valuation(series, facts, prices([(date(2024, 12, 31), 50.0)]), pd.DataFrame(columns=["accession", "form", "filed", "period_end"]),
                                      date(2024, 12, 31), fund)
    m = get(out, "dcf_base", "current")
    assert m.value is not None and abs(m.inputs["growth"] - 0.10) < 1e-6


def _base_series(n=8, first_fy=2022, **extra):
    """Eight calendar quarters FY2022-FY2023; extra series must use the same years (quarterly() defaults to 2022)."""
    s = {"revenue": quarterly("revenue", [100.0] * n, first_fy), "eps_diluted": quarterly("eps", [1.0] * n, first_fy),
         "cash": quarterly("cash", [0.0] * n, first_fy), "long_term_debt": quarterly("ltd", [0.0] * n, first_fy)}
    s.update(extra)
    return s


def _run(series, facts, as_of=date(2023, 12, 31), close=50.0):
    fund = compute_fundamental_metrics(series)
    return valuation.compute_valuation(series, facts, prices([(as_of, close)]), pd.DataFrame(columns=["accession", "form", "filed", "period_end"]), as_of, fund), fund


def test_ttm_uses_metrics_table_for_composed_da():
    series = _base_series(operating_income=quarterly("oi", [10.0] * 8), depreciation=quarterly("dep", [2.0] * 8))
    out, _ = _run(series, dei_facts([(date(2023, 12, 31), 10.0, "a")]))
    m = get(out, "ebitda_ttm", "current")
    assert m.value == 48.0 and "da_composed" in m.flags
    assert get(out, "ev_ebitda", "current").value == 500.0 / 48.0


def test_dcf_uses_same_net_debt_fallback_as_ev():
    series = _base_series(n=16, first_fy=2020, ocf=quarterly("ocf", [5.0] * 16, 2020), capex=quarterly("capex", [1.0] * 16, 2020))
    del series["long_term_debt"]
    series["cash"] = quarterly("cash", [20.0] * 16, 2020)
    out, _ = _run(series, dei_facts([(date(2023, 12, 31), 10.0, "a")]))
    assert get(out, "ev", "current").value == 480.0 and "no_debt_tag" in get(out, "ev", "current").flags
    m = get(out, "dcf_base", "current")
    assert m.value is not None and "no_debt_tag" in m.flags


def test_shares_fallback_to_diluted_weighted():
    series = _base_series(diluted_shares=quarterly("sh", [9.0] * 7 + [11.0]))
    empty_dei = dei_facts([]).iloc[0:0]
    out, _ = _run(series, empty_dei)
    m = get(out, "shares_outstanding", "current")
    assert m.value == 11.0 and "shares_diluted_weighted" in m.flags
    assert get(out, "market_cap", "current").value == 550.0
