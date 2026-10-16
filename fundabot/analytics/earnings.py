"""Earnings signals from FMP consensus data. Vendor data: every metric is flagged vendor_fmp.

Estimates and actuals come from FMP on the same (adjusted) basis; XBRL GAAP EPS is never mixed in.
"""
from datetime import date, datetime, timedelta

import pandas as pd

from fundabot.analytics import periods
from fundabot.analytics.metrics import Metric

FLAGS = ["vendor_fmp"]
IDS = ["eps_surprise", "revenue_surprise", "eps_beat_streak", "eps_estimate_next_fy", "revenue_estimate_next_fy", "eps_actual_fmp_ttm",
       "eps_growth_consensus_next_fy", "forward_pe", "price_target_upside", "eps_estimate_revision_30d", "eps_estimate_revision_90d"]
UNITS = {"eps_estimate_next_fy": "USD/share", "revenue_estimate_next_fy": "USD", "eps_actual_fmp_ttm": "USD/share", "eps_beat_streak": "count"}


def _m(mid, period, value, formula, inputs=None, reason=None):
    return Metric(mid, period, None if value is None else float(value), UNITS.get(mid, "ratio"), formula, inputs or {}, list(FLAGS), None, reason)


def _label(report_date: date, fye, offset) -> str:
    """Quarter label from the report date (FMP rows carry no fiscal period): a Q4 reported well after year end rounds to
    quarter 0 of the next year, which is Q4 of the prior year."""
    end = report_date - timedelta(days=35)
    fy_cal = periods.fy_of(end, fye)
    fq = periods.fq_of(end, fy_cal, fye)
    if fq == 0:
        fq, fy_cal = 4, fy_cal - 1
    return f"Q{fq}FY{fy_cal + offset}"


def compute_earnings_metrics(fmp: dict, as_of: date, price: float | None, fye: tuple[int, int], fy_offset: int, latest_fetch: datetime | None) -> list[Metric]:
    sur = fmp["surprises"].dropna(subset=["eps_actual"]).sort_values("report_date").tail(4)
    est = fmp["estimates"]
    est = est[est.period == "annual"] if len(est) else est
    if sur.empty and est.empty:
        return [_m(mid, "current", None, mid, reason="no_coverage") for mid in IDS]
    out = []

    # Surprises and streak.
    streak, streak_alive = 0, True
    for r in sur.sort_values("report_date", ascending=False).itertuples(index=False):
        lab = _label(r.report_date, fye, fy_offset)
        inp = {"report_date": str(r.report_date)}
        for mid, actual, estimate in (("eps_surprise", r.eps_actual, r.eps_estimated), ("revenue_surprise", r.revenue_actual, r.revenue_estimated)):
            if estimate is None or pd.isna(estimate) or estimate == 0 or actual is None or pd.isna(actual):
                out.append(_m(mid, lab, None, "(actual - estimate) / abs(estimate)", inp, "no_estimate"))
            else:
                out.append(_m(mid, lab, (actual - estimate) / abs(estimate), "(actual - estimate) / abs(estimate)", {**inp, "actual": actual, "estimate": estimate}))
        eps_s = out[-2]
        if streak_alive and eps_s.value is not None and eps_s.value > 0:
            streak += 1
        else:
            streak_alive = False
    out.append(_m("eps_beat_streak", "current", streak if len(sur) else None, "consecutive quarters with eps_surprise > 0",
                  reason=None if len(sur) else "no_coverage"))
    ttm = float(sur.eps_actual.sum()) if len(sur) == 4 else None
    out.append(_m("eps_actual_fmp_ttm", "current", ttm, "sum of last 4 FMP quarterly eps_actual", {"quarters": [str(d) for d in sur.report_date]},
                  reason=None if ttm is not None else "no_coverage"))

    # Next fiscal year consensus.
    latest = est[est.fetched_at == latest_fetch] if latest_fetch is not None and len(est) else est.iloc[0:0]
    nxt = latest[latest.period_end > as_of].sort_values("period_end").head(1)
    if nxt.empty:
        for mid in ("eps_estimate_next_fy", "revenue_estimate_next_fy", "eps_growth_consensus_next_fy", "forward_pe", "eps_estimate_revision_30d", "eps_estimate_revision_90d"):
            out.append(_m(mid, "current", None, mid, reason="no_coverage"))
    else:
        n = nxt.iloc[0]
        inp = {"period_end": str(n.period_end), "fetched_at": str(n.fetched_at)}
        out.append(_m("eps_estimate_next_fy", "current", n.eps_avg, "FMP consensus EPS avg, next fiscal year", inp))
        out.append(_m("revenue_estimate_next_fy", "current", n.revenue_avg, "FMP consensus revenue avg, next fiscal year", inp))
        g = (n.eps_avg / ttm - 1) if ttm and ttm > 0 and n.eps_avg is not None else None
        out.append(_m("eps_growth_consensus_next_fy", "current", g, "eps_estimate_next_fy / eps_actual_fmp_ttm - 1 (same basis)", {**inp, "eps_actual_fmp_ttm": ttm},
                      reason=None if g is not None else ("nonpositive_denominator" if ttm is not None else "no_coverage")))
        fpe = price / n.eps_avg if price and n.eps_avg and n.eps_avg > 0 else None
        out.append(_m("forward_pe", "current", fpe, "price / eps_estimate_next_fy (FMP adjusted basis)", {**inp, "price": price},
                      reason=None if fpe is not None else ("price_unavailable" if not price else "nonpositive_denominator")))
        for days, mid in ((30, "eps_estimate_revision_30d"), (90, "eps_estimate_revision_90d")):
            target = latest_fetch - timedelta(days=days)
            hist = est[(est.period_end == n.period_end) & (est.fetched_at != latest_fetch)].copy()
            if len(hist):
                hist["gap"] = (hist.fetched_at - target).abs()
                hist = hist[hist.gap <= timedelta(days=7)].sort_values("gap")
            if len(hist) and hist.iloc[0].eps_avg:
                old = hist.iloc[0]
                out.append(_m(mid, "current", n.eps_avg / old.eps_avg - 1, f"next-FY eps_avg now / eps_avg fetched ~{days}d earlier - 1",
                              {**inp, "older_fetch": str(old.fetched_at), "older_eps_avg": old.eps_avg}))
            else:
                out.append(_m(mid, "current", None, mid, inp, "no_history"))

    # Price target.
    pt = fmp["price_targets"].sort_values("fetched_at").tail(1)
    if pt.empty or not price:
        out.append(_m("price_target_upside", "current", None, "consensus target / price - 1", reason="no_coverage" if pt.empty else "price_unavailable"))
    else:
        c = float(pt.iloc[0].consensus)
        out.append(_m("price_target_upside", "current", c / price - 1, "consensus target / price - 1", {"consensus": c, "price": price, "fetched_at": str(pt.iloc[0].fetched_at)}))
    return out
