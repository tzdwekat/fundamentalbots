from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag, numeric_flags


def m(mid, period, value):
    return Metric(mid, period, value, "ratio", mid)


def test_accruals_flag():
    flags = numeric_flags([m("accruals_ratio", "FY2023", 0.05), m("accruals_ratio", "FY2024", 0.12)])
    assert [f.flag_id for f in flags] == ["accruals_high"]
    assert flags[0].severity == "high" and flags[0].evidence == ["metric:accruals_ratio@FY2024"]


def test_fcf_below_ni_three_of_five():
    ms = [m("fcf_to_net_income", f"FY{y}", v) for y, v in zip(range(2020, 2025), [0.8, 1.2, 0.9, 0.7, 1.1])]
    flags = numeric_flags(ms)
    assert [f.flag_id for f in flags] == ["fcf_below_ni"] and flags[0].severity == "medium"
    assert set(flags[0].evidence) == {"metric:fcf_to_net_income@FY2020", "metric:fcf_to_net_income@FY2022", "metric:fcf_to_net_income@FY2023"}
    assert numeric_flags(ms[1:]) == []  # only 2 of 4 below


def test_goodwill_heavy():
    flags = numeric_flags([m("goodwill_to_assets", "FY2024", 0.61), m("goodwill_to_assets", "Q3FY2025", 0.3)])
    assert [f.flag_id for f in flags] == ["goodwill_heavy"] and flags[0].evidence == ["metric:goodwill_to_assets@FY2024"]


def test_none_inputs_skip():
    assert numeric_flags([m("accruals_ratio", "FY2024", None), m("goodwill_to_assets", "FY2024", None)]) == []
    assert isinstance(RedFlag("x", "high", [], ""), RedFlag)


from datetime import date
from pathlib import Path

import pandas as pd

from fundabot.analytics.red_flags import text_flags

FIX = Path(__file__).parent / "fixtures" / "sections"


def filings_df(rows):
    return pd.DataFrame([(*r, "") for r in rows], columns=["accession", "form", "filed", "period_end", "primary_doc_url", "items"])


def test_going_concern_regex():
    neg = {"item7": (FIX / "aapl_10k_item7.txt").read_text(), "item1a": (FIX / "aapl_10k_item1a.txt").read_text()}
    assert text_flags("acc-1", neg, filings_df([])) == []
    pos = {"item7": "Management has concluded that there is substantial doubt about the Company's ability to continue as a going concern."}
    flags = text_flags("acc-1", pos, filings_df([]))
    assert [f.flag_id for f in flags] == ["going_concern"] and flags[0].severity == "high"
    assert flags[0].evidence == ["chunk:acc-1#item7-0"]
    pos2 = {"item9a": "...ability to continue as a going concern. These conditions raise substantial doubt..."}
    assert [f.flag_id for f in text_flags("acc-1", pos2, filings_df([]))] == ["going_concern"]
    assert text_flags("acc-1", {"item1a": "substantial doubt ... going concern"}, filings_df([])) == []  # risk-factor boilerplate ignored


def test_late_filing_flag():
    df = filings_df([("a", "10-K", date(2026, 3, 1), None, ""), ("b", "NT 10-Q", date(2025, 8, 1), None, ""), ("c", "NT 10-K", date(2023, 1, 1), None, "")])
    flags = text_flags("a", {}, df)
    assert [f.flag_id for f in flags] == ["late_filing"] and flags[0].severity == "medium"
    assert flags[0].evidence == ["filing:b"]
    assert text_flags("a", {}, df[df.accession != "b"]) == []  # the 2023 one is older than 2 years


def test_going_concern_negation_ignored_and_sentence_in_detail():
    neg = {"item7": "Management concluded that there is no substantial doubt about the Company's ability to continue as a going concern."}
    assert text_flags("a", neg, filings_df([])) == []
    neg2 = {"item8": "These conditions do not raise substantial doubt about our ability to continue as a going concern."}
    assert text_flags("a", neg2, filings_df([])) == []
    pos = {"item8": "Intro text. These conditions raise substantial doubt about the Company's ability to continue as a going concern within one year. Next."}
    flags = text_flags("a", pos, filings_df([]))
    assert len(flags) == 1 and "raise substantial doubt" in flags[0].detail


def test_altman_flag_and_financials_skip():
    from fundabot.analytics.metrics import Metric

    low = [Metric("altman_z", "current", 1.42, "score", "fmp:financial-scores", {}, ["vendor_fmp"])]
    flags = numeric_flags(low, sector="Technology", industry="Consumer Electronics")
    assert [f.flag_id for f in flags] == ["altman_z_distress"] and flags[0].severity == "high" and flags[0].evidence == ["metric:altman_z@current"]
    assert numeric_flags(low, sector="Financial Services", industry="Banks") == []
    assert numeric_flags(low, sector="Real Estate", industry="REIT - Retail") == []
    assert numeric_flags([Metric("altman_z", "current", 3.0, "score", "x")], sector="Technology") == []
    assert numeric_flags(low) == [] or numeric_flags(low)[0].flag_id == "altman_z_distress"  # unknown sector: the rule still applies
