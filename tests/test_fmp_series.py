"""FMP statements -> the same per-item frames periods.build_series makes; EDGAR fills what FMP lacks."""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fundabot.analytics import fmp_series as FS
from fundabot.analytics import periods
from fundabot.analytics.metrics import compute_fundamental_metrics
from fundabot.ingest.edgar_facts import normalize

FIX = Path(__file__).parent / "fixtures"


def statements():
    out = {}
    for key in ("income", "balance", "cashflow"):
        for pt in ("annual", "quarter"):
            rows = json.loads((FIX / f"fmp_aapl_{key}_{pt}.json").read_text())
            out[f"{key}_{pt}"] = [{**r, "fiscal_year": int(r["fiscalYear"]), "period": r["period"], "date": date.fromisoformat(r["date"]), "filing_date": date.fromisoformat(r["filingDate"][:10])} for r in rows]
    return out


FILINGS = pd.DataFrame([{"accession": "0000320193-25-000079", "form": "10-K", "filed": date(2025, 10, 31)}, {"accession": "0000320193-26-000013", "form": "10-Q", "filed": date(2026, 1, 30)}])


def edgar_series():
    return periods.build_series(normalize(json.loads((FIX / "AAPL_companyfacts.json").read_text())), periods.load_tag_map())


def test_frames_match_columns_and_labels():
    s = FS.build_fmp_series(statements(), FILINGS)
    assert set(s) >= {"revenue", "net_income", "eps_diluted", "ocf", "capex", "total_assets", "equity", "cash", "diluted_shares", "da", "sbc", "buybacks", "dividends_paid"}
    for item, df in s.items():
        assert list(df.columns) == periods.COLUMNS, item
    rev = s["revenue"]
    fy25 = rev[rev.period == "FY2025"].iloc[0]
    assert fy25.value == 416161000000 and fy25.fq == 0 and fy25.fy == 2025 and fy25.end == date(2025, 9, 27) and fy25.tag == "fmp:income:revenue"
    q3 = rev[rev.period == "Q3FY2026"].iloc[0]
    assert q3.value == 109417000000 and q3.fq == 3 and q3.fy == 2026
    assert not rev.restated.any() and not rev.derived.any() and not rev.split_adjusted.any()


def test_signs_and_absolutes():
    s = FS.build_fmp_series(statements(), None)
    for item in ("capex", "dividends_paid", "buybacks", "acquisitions"):
        if item in s:
            assert (s[item].value > 0).all(), item
    assert "acquisitions" not in s or s["acquisitions"].value.gt(0).all()


def test_zero_is_missing_and_edgar_fills():
    s = FS.build_fmp_series(statements(), None)
    assert "interest_expense" not in s or "FY2025" not in set(s["interest_expense"].period)  # FMP says 0: no row
    assert "goodwill" not in s and "intangibles" in s and (s["intangibles"].value > 0).all()  # only the quarters FMP reports
    merged = FS.merge_series(s, edgar_series())
    ie = merged["interest_expense"]
    assert "FY2025" not in set(ie.period) and "FY2024" not in set(ie.period)  # neither source has it: stays missing, never an invented zero
    assert ie[ie.period == "FY2023"].tag.iloc[0].startswith("Interest") and ie.attrs["source_notes"][(2023, 0)] == "confirmed:fmp"  # both agree: filed value, confirmed
    assert ie[ie.period == "FY2013"].tag.iloc[0].startswith("Interest")
    fake_edgar = {"interest_expense": pd.DataFrame([{"period": "FY2025", "fy": 2025, "fq": 0, "start": date(2024, 9, 29), "end": date(2025, 9, 27), "value": 1.0e9, "accession": "x", "tag": "InterestExpense",
                                                     "restated": False, "derived": False, "split_adjusted": False}])[periods.COLUMNS]}
    filled = FS.merge_series(s, fake_edgar)["interest_expense"]
    assert filled[filled.period == "FY2025"].tag.iloc[0] == "InterestExpense" and filled[filled.period == "FY2025"].value.iloc[0] == 1.0e9  # EDGAR fills FMP's zero


def test_accession_matching():
    s = FS.build_fmp_series(statements(), FILINGS)
    rev = s["revenue"]
    assert rev[rev.period == "FY2025"].accession.iloc[0] == "0000320193-25-000079"
    assert rev[rev.period == "Q4FY2025"].accession.iloc[0] == "0000320193-25-000079"  # Q4 carries the 10-K filing date
    assert rev[rev.period == "Q1FY2026"].accession.iloc[0] == "0000320193-26-000013"
    assert rev[rev.period == "Q3FY2026"].accession.iloc[0] == "fmp:2026-07-31"  # no stored filing for it


def test_fmp_quarters_support_ttm():
    s = FS.build_fmp_series(statements(), None)
    rev = s["revenue"]
    q = rev[(rev.fq >= 1) & (rev.end <= date(2026, 7, 1))].sort_values("end").tail(4)
    assert periods.ttm(rev, date(2026, 7, 1)) == pytest.approx(float(q.value.sum()))
    assert rev.start.notna().all()


def test_merge_keeps_older_edgar_history():
    s = FS.build_fmp_series(statements(), None)
    e = edgar_series()
    merged = FS.merge_series(s, e)
    oldest_fmp = s["revenue"].fy.min()
    older = merged["revenue"][(merged["revenue"].fy < oldest_fmp) & (merged["revenue"].fq == 0)]
    assert len(older) > 0 and not older.tag.str.startswith("fmp:").any()
    assert set(merged) >= set(e) and set(merged) >= set(s)  # no item lost from either source


def row(fy, fq, value, tag, derived=False):
    end = date(fy, 9, 27) if fq == 0 else date(fy - 1 + (fq > 1), (12 + 3 * fq) % 12 or 12, 28)
    return {"period": f"FY{fy}" if fq == 0 else f"Q{fq}FY{fy}", "fy": fy, "fq": fq, "start": end - pd.Timedelta(days=90).to_pytimedelta(), "end": end, "value": value, "accession": "acc", "tag": tag,
            "restated": False, "derived": derived, "split_adjusted": False}


def frame(rows):
    return pd.DataFrame(rows)[periods.COLUMNS]


def test_merge_rule_agree_prefers_edgar_and_records_confirmation():
    fmp = {"revenue": frame([row(2025, 0, 100.5, "fmp:income:revenue")])}
    edgar = {"revenue": frame([row(2025, 0, 100.0, "Revenues")])}
    m = FS.merge_series(fmp, edgar)["revenue"]
    assert m.tag.iloc[0] == "Revenues" and m.value.iloc[0] == 100.0 and m.attrs["source_notes"][(2025, 0)] == "confirmed:fmp"


def test_merge_rule_reported_beats_derived():
    fmp = {"revenue": frame([row(2025, 4, 30.0, "fmp:income:revenue")])}
    edgar = {"revenue": frame([row(2025, 4, 35.0, "Revenues", derived=True)])}  # EDGAR's Q4 is FY minus nine months; FMP reports the quarter
    m = FS.merge_series(fmp, edgar)["revenue"]
    assert m.tag.iloc[0] == "fmp:income:revenue" and m.value.iloc[0] == 30.0 and m.attrs["source_notes"][(2025, 4)] == "replaces_derived:edgar=35"
    agree = FS.merge_series(fmp, {"revenue": frame([row(2025, 4, 30.2, "Revenues", derived=True)])})["revenue"]
    assert agree.tag.iloc[0] == "fmp:income:revenue" and not agree.derived.iloc[0] and agree.attrs["source_notes"][(2025, 4)] == "confirmed:edgar"  # reported wins even when they agree


def test_merge_rule_conflict_keeps_edgar_and_flags():
    fmp = {"short_term_debt": frame([row(2025, 0, 20.3, "fmp:balance:shortTermDebt")])}
    edgar = {"short_term_debt": frame([row(2025, 0, 12.4, "LongTermDebtCurrent")])}
    m = FS.merge_series(fmp, edgar)["short_term_debt"]
    assert m.tag.iloc[0] == "LongTermDebtCurrent" and m.value.iloc[0] == 12.4 and m.attrs["source_notes"][(2025, 0)] == "conflict:fmp=20.3"
    ms = {f"{x.metric_id}@{x.period}": x for x in compute_fundamental_metrics({"short_term_debt": m, "revenue": edgar["short_term_debt"].assign(tag="Revenues")})}
    assert "source_conflict" in ms["short_term_debt@FY2025"].flags and ms["short_term_debt@FY2025"].inputs["source_note"] == "conflict:fmp=20.3"
    assert "source_conflict" not in ms["revenue@FY2025"].flags


def test_split_adjusted_flags():
    s = FS.build_fmp_series(statements(), None)
    assert s["eps_diluted"].split_adjusted.all() and s["diluted_shares"].split_adjusted.all() and not s["net_income"].split_adjusted.any()


def test_formula_names_source():
    merged = FS.merge_series(FS.build_fmp_series(statements(), FILINGS), edgar_series())
    ms = {f"{m.metric_id}@{m.period}": m for m in compute_fundamental_metrics(merged)}
    rev = ms["revenue@FY2025"]
    assert rev.formula_id.startswith("xbrl:") and rev.inputs["source_note"] == "confirmed:fmp"  # both agree: the filed value, confirmed
    q4 = ms["revenue@Q4FY2025"]
    assert q4.formula_id == "fmp:income:revenue" and "derived" not in q4.flags  # EDGAR derives Q4; FMP reports it
    assert ms["gross_margin@FY2025"].value == pytest.approx(195200000000 / 416161000000, rel=1e-4)  # EDGAR wins on agreement; FMP rounds to the million
    assert ms["interest_expense@FY2023"].formula_id.startswith("xbrl:")
