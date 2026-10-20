"""FMP-derived metrics computed in code: peer medians and relatives, segment shares and growth, dividends, scores, yield."""
import json
from datetime import date
from pathlib import Path

import pytest

from fundabot.analytics import fmp_metrics as F

FIX = Path(__file__).parent / "fixtures"


def by_id(ms):
    return {f"{m.metric_id}@{m.period}": m for m in ms}


def peers3():
    return [{"peer": "A", "pe_ttm": 20.0, "ev_sales_ttm": 4.0, "ev_ebitda_ttm": 12.0}, {"peer": "B", "pe_ttm": 30.0, "ev_sales_ttm": 6.0, "ev_ebitda_ttm": None},
            {"peer": "C", "pe_ttm": None, "ev_sales_ttm": 8.0, "ev_ebitda_ttm": 16.0}]


def test_peer_medians_and_relative():
    ms = by_id(F.compute_fmp_metrics(peers3(), [], [], None, None, 100.0, {"pe": 50.0, "ev_sales": 9.0}))
    assert ms["pe_peer_median@current"].value == 25.0 and ms["ev_sales_peer_median@current"].value == 6.0 and ms["ev_ebitda_peer_median@current"].value == 14.0
    assert ms["pe_vs_peer_median@current"].value == 2.0 and ms["ev_sales_vs_peer_median@current"].value == 1.5
    assert "vendor_fmp" in ms["pe_peer_median@current"].flags and ms["pe_peer_median@current"].formula_id == "peers:median" and ms["pe_peer_median@current"].inputs["peers"] == ["A", "B"]


def test_peer_medians_empty():
    ms = by_id(F.compute_fmp_metrics([], [], [], None, None, 100.0, {"pe": 50.0, "ev_sales": 9.0}))
    assert ms["pe_peer_median@current"].value is None and ms["pe_peer_median@current"].null_reason == "no_peers" and ms["pe_vs_peer_median@current"].value is None


def segs():
    rows = []
    for kind, name in (("product", "seg_product"), ("geographic", "seg_geo")):
        for row in json.loads((FIX / f"fmp_aapl_{name}.json").read_text()):
            rows += [{"kind": kind, "fiscal_year": int(row["fiscalYear"]), "name": n, "revenue": float(v)} for n, v in row["data"].items()]
    return rows


def test_segments_share_and_yoy():
    ms = by_id(F.compute_fmp_metrics([], segs(), [], None, None, None, {}))
    iphone = ms["seg_iphone_share@FY2025"]
    assert iphone.value == pytest.approx(209586e6 / (33708e6 + 109158e6 + 35686e6 + 28023e6 + 209586e6)) and iphone.inputs["name"] == "iPhone" and "vendor_fmp" in iphone.flags
    assert ms["seg_iphone_revenue@FY2025"].value == 209586e6 and ms["seg_iphone_revenue@FY2025"].formula_id == "fmp:revenue-product-segmentation"
    prev = ms["seg_iphone_revenue@FY2024"].value
    assert ms["seg_iphone_yoy@FY2025"].value == pytest.approx(209586e6 / prev - 1)
    assert any(k.startswith("seg_americas") for k in ms) and ms["seg_iphone_share@FY2025"].unit == "ratio"
    shares = [m.value for k, m in ms.items() if k.endswith("_share@FY2025") and m.formula_id.endswith("product-segmentation")]
    assert sum(shares) == pytest.approx(1.0)


def test_segment_rename_no_false_growth():
    rows = [{"kind": "product", "fiscal_year": 2024, "name": "Service", "revenue": 100.0}, {"kind": "product", "fiscal_year": 2025, "name": "Services", "revenue": 110.0},
            {"kind": "product", "fiscal_year": 2025, "name": "New Thing", "revenue": 10.0}]
    ms = by_id(F.compute_fmp_metrics([], rows, [], None, None, None, {}))
    assert ms["seg_services_yoy@FY2025"].value == pytest.approx(0.10)  # Service -> Services is the same segment
    assert ms["seg_new_thing_yoy@FY2025"].value is None and ms["seg_new_thing_yoy@FY2025"].null_reason == "no_prior"
    assert F.slug("Wearables, Home and Accessories") == "wearables_home_and_acces" and len(F.slug("x" * 40)) <= 24  # 24-char cap


def test_segment_caps_8_and_6():
    rows = [{"kind": "product", "fiscal_year": 2025, "name": f"P{i}", "revenue": float(100 - i)} for i in range(12)]
    rows += [{"kind": "geographic", "fiscal_year": 2025, "name": f"G{i}", "revenue": float(100 - i)} for i in range(9)]
    ms = F.compute_fmp_metrics([], rows, [], None, None, None, {})
    assert len({m.metric_id for m in ms if m.formula_id.endswith("product-segmentation") and m.metric_id.endswith("_revenue")}) == 8
    assert len({m.metric_id for m in ms if m.formula_id.endswith("geographic-segmentation") and m.metric_id.endswith("_revenue")}) == 6


def test_dividends_ttm_yield_growth():
    rows = json.loads((FIX / "fmp_aapl_dividends.json").read_text())
    divs = [{"ex_date": date.fromisoformat(r["date"]), "amount": float(r["dividend"]), "adj_amount": float(r["adjDividend"])} for r in rows]
    ms = by_id(F.compute_fmp_metrics([], [], divs, None, None, 300.0, {}, today=date(2026, 10, 19)))
    assert ms["dividend_per_share_ttm@current"].value == pytest.approx(1.06) and ms["dividend_yield@current"].value == pytest.approx(1.06 / 300)
    g = ms["dividend_growth_5y@current"].value
    assert 0.03 < g < 0.08  # Apple grows the dividend a few percent a year
    assert ms["dividend_growth_5y@current"].formula_id == "fmp:dividends" and "vendor_fmp" in ms["dividend_yield@current"].flags


def test_scores_and_treasury():
    ms = by_id(F.compute_fmp_metrics([], [], [], {"altman_z": 13.5, "piotroski": 9}, (date(2026, 10, 16), 5.09), None, {}))
    assert ms["altman_z@current"].value == 13.5 and ms["piotroski@current"].value == 9 and ms["risk_free_10y@current"].value == pytest.approx(0.0509)
    assert ms["risk_free_10y@current"].inputs["date"] == "2026-10-16"


def test_fmp_metrics_without_coverage():
    ms = by_id(F.compute_fmp_metrics([], [], [], None, None, None, {}))
    for k in ("pe_peer_median@current", "dividend_yield@current", "altman_z@current", "piotroski@current", "risk_free_10y@current"):
        assert ms[k].value is None and ms[k].null_reason in ("no_coverage", "no_peers")
    assert not any(m.metric_id.startswith("seg_") for m in ms.values())
