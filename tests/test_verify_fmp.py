"""The FMP TTM cross-check compares like with like and never changes our metrics."""
import json
from pathlib import Path

import pytest

from fundabot.analytics.metrics import Metric
from fundabot.report import verify

FIX = Path(__file__).parent / "fixtures"


def ttm():
    out = {}
    for name in ("ratios_ttm", "key_metrics_ttm"):
        out.update({k: (float(v) if k != "symbol" else v) for k, v in json.loads((FIX / f"fmp_aapl_{name}.json").read_text())[0].items()})
    return out


def quarterly(mid, values, unit="USD"):
    qs = ["Q4FY2025", "Q1FY2026", "Q2FY2026", "Q3FY2026"]
    return [Metric(mid, q, v, unit, "x", {"end": "2026-06-27"}) for q, v in zip(qs, values)]


def metrics():
    ms = [Metric("pe", "current", 39.4, "ratio", "x"), Metric("ev_sales", "current", 10.8, "ratio", "x"), Metric("ev_ebitda", "current", 30.0, "ratio", "x"),
          Metric("fcf_yield", "current", 0.0273, "ratio", "x"), Metric("current_ratio", "Q3FY2026", 1.003, "ratio", "x"), Metric("net_debt", "Q3FY2026", 42.8e9, "USD", "x")]
    ms += quarterly("revenue", [102.47e9, 143.76e9, 111.18e9, 109.42e9]) + quarterly("gross_profit", [48.34e9, 69.23e9, 54.78e9, 54.77e9])
    ms += quarterly("operating_income", [32.43e9, 50.85e9, 35.88e9, 35.70e9]) + quarterly("net_income", [27.47e9, 42.10e9, 29.58e9, 29.79e9])
    ms += quarterly("sbc", [3.0e9, 3.5e9, 3.6e9, 3.7e9]) + quarterly("ebitda", [36e9, 54e9, 39e9, 39e9])
    return ms


def test_crosscheck_ttm_margins_from_quarters():
    rows = {r["metric"]: r for r in verify.crosscheck_fmp(metrics(), ttm())}
    gm = rows["gross_margin_ttm"]
    assert gm["ours"] == pytest.approx((48.34 + 69.23 + 54.78 + 54.77) / (102.47 + 143.76 + 111.18 + 109.42)) and gm["fmp"] == pytest.approx(0.4865, rel=1e-3) and gm["ok"] is True
    assert rows["pe"]["ok"] is True and rows["ev_sales"]["ok"] is True and rows["current_ratio"]["ok"] is True
    assert rows["net_debt_to_ebitda_ttm"]["ours"] == pytest.approx(42.8 / 168) and rows["net_debt_to_ebitda_ttm"]["fmp"] == pytest.approx(0.2657, rel=1e-3)
    assert set(rows) == {"pe", "ev_sales", "ev_ebitda", "fcf_yield", "gross_margin_ttm", "operating_margin_ttm", "net_margin_ttm", "sbc_to_revenue_ttm", "net_debt_to_ebitda_ttm", "current_ratio"}
    assert "roe" not in rows and "roic" not in rows


def test_crosscheck_reports_but_never_alters():
    ms = metrics()
    before = [(m.metric_id, m.period, m.value) for m in ms]
    ms[0] = Metric("pe", "current", 49.0, "ratio", "x")  # 25% above FMP's 39.1
    rows = {r["metric"]: r for r in verify.crosscheck_fmp(ms, ttm())}
    assert rows["pe"]["ok"] is False and rows["pe"]["diff"] == pytest.approx(49.0 / 39.148 - 1, rel=1e-3)
    assert [(m.metric_id, m.period, m.value) for m in ms][1:] == before[1:] and ms[0].value == 49.0
    missing = verify.crosscheck_fmp([Metric("pe", "current", None, "ratio", "x")], ttm())
    assert all(r["ok"] is None for r in missing if r["metric"] == "pe")  # no value on our side: not a mismatch, just unchecked
    text = verify.format_crosscheck(verify.crosscheck_fmp(ms, ttm()))
    assert "CHECK" in text and "pe" in text


def test_crosscheck_edgar_rows_and_tolerance():
    import pandas as pd

    from fundabot.analytics import periods

    def frame(rows):
        return pd.DataFrame(rows, columns=periods.COLUMNS)
    r = lambda fy, fq, v, tag: (f"FY{fy}" if fq == 0 else f"Q{fq}FY{fy}", fy, fq, None, __import__("datetime").date(fy, 9, 27), v, "a", tag, False, False, False)
    fmp_s = {"revenue": frame([r(2025, 0, 101.0, "fmp:income:revenue"), r(2024, 0, 90.0, "fmp:income:revenue"), r(2015, 0, 50.0, "fmp:income:revenue")]), "cash": frame([r(2025, 0, 10.0, "fmp:balance:cashAndCashEquivalents")])}
    edgar_s = {"revenue": frame([r(2025, 0, 100.0, "Revenues"), r(2024, 0, 95.0, "Revenues"), r(2015, 0, 50.0, "Revenues")]), "equity": frame([r(2025, 0, 5.0, "StockholdersEquity")])}
    rows = {(x["item"], x["period"]): x for x in verify.crosscheck_edgar(fmp_s, edgar_s)}
    assert rows[("revenue", "FY2025")]["ok"] is True and rows[("revenue", "FY2025")]["diff"] == pytest.approx(0.01)
    assert rows[("revenue", "FY2024")]["ok"] is False and rows[("revenue", "FY2024")]["diff"] == pytest.approx(-90 / 95 + 1, abs=1e-9) or rows[("revenue", "FY2024")]["diff"] == pytest.approx(90 / 95 - 1)
    assert ("revenue", "FY2015") not in rows and ("cash", "FY2025") not in rows and ("equity", "FY2025") not in rows  # outside the window / not shared
    assert "CHECK" in verify.format_edgar_check(list(rows.values()))
