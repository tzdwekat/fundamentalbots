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
