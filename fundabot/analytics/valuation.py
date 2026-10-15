"""Valuation multiples (current and per-filing history), own 5y ranges, DCF and reverse DCF."""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from fundabot.analytics import periods
from fundabot.analytics.metrics import NONPOS, Metric, anchor_name, combine
from fundabot.ingest.prices import close_on_or_after, close_on_or_before

RATIOS = ["pe", "ev_ebitda", "ev_sales", "fcf_yield"]
RANGE_STATS = ["5y_min", "5y_median", "5y_max", "5y_percentile"]
POINT_IDS = ["price", "shares_outstanding", "market_cap", "ev", "eps_ttm", "ebitda_ttm", "revenue_ttm", "fcf_ttm"] + RATIOS
CURRENT_ONLY = ["peg", "dcf_bear", "dcf_base", "dcf_bull", "reverse_dcf_implied_growth"] + [f"{r}_{s}" for r in RATIOS for s in RANGE_STATS]
DISCOUNT_RATE, TERMINAL_GROWTH, YEARS = 0.10, 0.025, 10


def shares_outstanding(facts: pd.DataFrame) -> pd.DataFrame:
    """Cover-page shares per date: latest accession for that date, all classes summed."""
    dei = facts[(facts.taxonomy == "dei") & (facts.tag == "EntityCommonStockSharesOutstanding")]
    rows = []
    for end, grp in dei.groupby("end_date"):
        acc = grp.sort_values(["filed", "accession"]).accession.iloc[-1]
        rows.append((end, float(grp[grp.accession == acc].value.sum()), acc))
    return pd.DataFrame(rows, columns=["end_date", "shares", "accession"]).sort_values("end_date").reset_index(drop=True)


def dcf_ev(fcf0: float, g: float, r: float = DISCOUNT_RATE, tg: float = TERMINAL_GROWTH, years: int = YEARS) -> float:
    pv = sum(fcf0 * (1 + g) ** t / (1 + r) ** t for t in range(1, years + 1))
    tv = fcf0 * (1 + g) ** years * (1 + tg) / (r - tg)
    return pv + tv / (1 + r) ** years


def implied_growth(ev_target: float, fcf0: float, lo: float = -0.5, hi: float = 1.0) -> float | None:
    if fcf0 <= 0 or ev_target <= 0:
        return None
    if dcf_ev(fcf0, lo) > ev_target or dcf_ev(fcf0, hi) < ev_target:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if dcf_ev(fcf0, mid) < ev_target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    return (lo + hi) / 2


def _ttm_metric(mid, series, items, as_of, period, sign=None) -> Metric | None:
    vals = []
    for it in items:
        v = periods.ttm(series[it], as_of) if it in series else None
        if v is None:
            return None
        vals.append(v)
    sign = sign or [1] * len(items)
    return Metric(mid, period, float(sum(s * v for s, v in zip(sign, vals))), "USD" if mid != "eps_ttm" else "USD/share",
                  f"ttm({' '.join(f'{'+' if s > 0 else '-'}{i}' for s, i in zip(sign, items)).lstrip('+')}) as of {as_of}", {}, [], series[items[0]].tag.iloc[0])


def _point(period, price_pt, shares_row, as_of_ttm, bs_period, series, fm) -> list[Metric]:
    """Market cap, EV, TTM flows and the four ratios for one valuation point."""
    out = []
    price = Metric("price", period, price_pt[1], "USD/share", f"close on {price_pt[0]}", {}, [], None) if price_pt else None
    shares = Metric("shares_outstanding", period, shares_row.shares, "shares",
                    f"dei:EntityCommonStockSharesOutstanding as of {shares_row.end_date}", {}, [], "EntityCommonStockSharesOutstanding",
                    ) if shares_row is not None else None
    for m in (price, shares):
        if m:
            out.append(m)
    mcap = combine("market_cap", period, "price * shares_outstanding", {"price": price, "shares_outstanding": shares},
                   lambda v: v["price"] * v["shares_outstanding"], "USD")
    net_debt = fm.get(("net_debt", bs_period))
    flags = []
    if net_debt is None and fm.get(("cash", bs_period)) is not None and fm.get(("total_debt", bs_period)) is None:
        cash = fm[("cash", bs_period)]
        net_debt = Metric("net_debt", bs_period, -cash.value if cash.value is not None else None, "USD", "-cash (no debt tag)",
                          {"cash": {"item": "cash", "period": bs_period, "value": cash.value, "tag": cash.tag_used}}, ["no_debt_tag"], None)
        flags = ["no_debt_tag"]
    ev = combine("ev", period, "market_cap + net_debt", {"market_cap": mcap, "net_debt": net_debt}, lambda v: v["market_cap"] + v["net_debt"], "USD", flags=flags)
    eps = _ttm_metric("eps_ttm", series, ["eps_diluted"], as_of_ttm, period)
    ebitda = _ttm_metric("ebitda_ttm", series, ["operating_income", "da"], as_of_ttm, period)
    rev = _ttm_metric("revenue_ttm", series, ["revenue"], as_of_ttm, period)
    fcf = _ttm_metric("fcf_ttm", series, ["ocf", "capex"], as_of_ttm, period, sign=[1, -1])
    out += [mcap, ev] + [m for m in (eps, ebitda, rev, fcf) if m]

    def pos_div(n, d):
        return lambda v: v[n] / v[d] if v[d] > 0 else NONPOS

    out.append(combine("pe", period, "price / eps_ttm", {"price": price, "eps_ttm": eps}, pos_div("price", "eps_ttm")))
    out.append(combine("ev_ebitda", period, "ev / ebitda_ttm", {"ev": ev, "ebitda_ttm": ebitda}, pos_div("ev", "ebitda_ttm")))
    out.append(combine("ev_sales", period, "ev / revenue_ttm", {"ev": ev, "revenue_ttm": rev}, pos_div("ev", "revenue_ttm")))
    out.append(combine("fcf_yield", period, "fcf_ttm / market_cap", {"fcf_ttm": fcf, "market_cap": mcap}, pos_div("fcf_ttm", "market_cap")))
    for m in out:
        if m.null_reason == "nonpositive_denominator":
            m.null_reason = "negative_denominator"
    return out


def _period_label(anchor: pd.DataFrame, end: date, annual: bool = False) -> str | None:
    """Period label for a period end: the quarter label, or the FY label for annual filings."""
    near = anchor[(anchor.end - end).abs() <= timedelta(days=10)]
    if near.empty:
        return None
    pref = near[near.fq == 0] if annual else near[near.fq >= 1]
    return (pref if not pref.empty else near).period.iloc[0]


def compute_valuation(series, facts, prices_df, filings: pd.DataFrame, as_of: date, fundamentals: list[Metric]) -> list[Metric]:
    if prices_df is None or prices_df.empty:
        return [Metric(mid, "current", None, "ratio", mid, {}, [], None, "price_unavailable") for mid in POINT_IDS + CURRENT_ONLY]
    fm = {(m.metric_id, m.period): m for m in fundamentals}
    anchor = series[anchor_name(series)]
    sh = shares_outstanding(facts)
    out: list[Metric] = []

    # Current point.
    q_ends = anchor[(anchor.fq >= 1) & (anchor.end <= as_of)]
    latest_q_end = q_ends.end.max() if not q_ends.empty else None
    cur_label = _period_label(anchor, latest_q_end) if latest_q_end else None
    sh_cur = sh[sh.end_date <= as_of].tail(1)
    out += _point("current", close_on_or_before(prices_df, as_of), sh_cur.iloc[0] if not sh_cur.empty else None,
                  latest_q_end or as_of, cur_label, series, fm)

    # One point per 10-K/10-Q filing in the last 5 years.
    hist = filings[filings.form.isin(periods.PERIODIC_FORMS) & (filings.filed >= as_of - timedelta(days=5 * 365))]
    for f in hist.itertuples(index=False):
        label = _period_label(anchor, f.period_end, annual=f.form.startswith("10-K")) if f.period_end else None
        if label is None:
            continue
        sh_f = sh[sh.accession == f.accession]
        if sh_f.empty:
            sh_f = sh[sh.end_date <= f.filed].tail(1)
        out += _point(label, close_on_or_after(prices_df, f.filed), sh_f.iloc[0] if not sh_f.empty else None, f.period_end, label, series, fm)

    # Own 5y ranges.
    cur = {m.metric_id: m for m in out if m.period == "current"}
    for r in RATIOS:
        hist_vals = [m.value for m in out if m.metric_id == r and m.period != "current" and m.value is not None]
        inputs = {f"{r}@{m.period}": {"item": r, "period": m.period, "value": m.value, "tag": None} for m in out if m.metric_id == r and m.period != "current"}
        for stat, fn in (("5y_min", min), ("5y_median", lambda v: float(np.median(v))), ("5y_max", max)):
            out.append(Metric(f"{r}_{stat}", "current", fn(hist_vals) if hist_vals else None, "ratio", f"{stat} of {r} over filings in last 5y",
                              inputs, [], None, None if hist_vals else "no_history"))
        c = cur[r].value
        pct = sum(v <= c for v in hist_vals) / len(hist_vals) if hist_vals and c is not None else None
        out.append(Metric(f"{r}_5y_percentile", "current", pct, "ratio", f"share of historical {r} values <= current", inputs, [], None,
                          None if pct is not None else "no_history"))

    # PEG and DCF.
    latest_fy = f"FY{int(anchor[anchor.fq == 0].fy.max())}" if (anchor.fq == 0).any() else None
    eps_cagr = fm.get(("eps_diluted_cagr_3y", latest_fy))
    out.append(combine("peg", "current", "pe / (eps_diluted_cagr_3y * 100)", {"pe": cur["pe"], "eps_diluted_cagr_3y": eps_cagr},
                       lambda v: v["pe"] / (v["eps_diluted_cagr_3y"] * 100) if v["eps_diluted_cagr_3y"] > 0 else NONPOS))
    growth = fm.get(("fcf_cagr_5y", latest_fy)) or fm.get(("fcf_cagr_3y", latest_fy))
    fcf0, ev, net_debt, shares = cur.get("fcf_ttm"), cur.get("ev"), fm.get(("net_debt", cur_label)), cur.get("shares_outstanding")
    base_g = min(max(growth.value, 0.0), 0.25) if growth and growth.value is not None else None
    assumptions = {"discount_rate": DISCOUNT_RATE, "terminal_growth": TERMINAL_GROWTH, "years": YEARS}
    for name, delta in (("dcf_bear", -0.05), ("dcf_base", 0.0), ("dcf_bull", 0.05)):
        if base_g is None:
            out.append(Metric(name, "current", None, "USD/share", "dcf", assumptions, [], None, "no_fcf_history"))
            continue
        g = base_g + delta
        m = combine(name, "current", f"(dcf_ev(fcf_ttm, growth={g:.3f}) - net_debt) / shares_outstanding",
                    {"fcf_ttm": fcf0, "net_debt": net_debt or ev and None, "shares_outstanding": shares},
                    lambda v, g=g: (dcf_ev(v["fcf_ttm"], g) - v["net_debt"]) / v["shares_outstanding"] if v["fcf_ttm"] > 0 and v["shares_outstanding"] > 0 else NONPOS,
                    "USD/share")
        m.inputs.update({"growth": g, **assumptions})
        out.append(m)
    m = combine("reverse_dcf_implied_growth", "current", "growth g such that dcf_ev(fcf_ttm, g) = ev", {"fcf_ttm": fcf0, "ev": ev},
                lambda v: implied_growth(v["ev"], v["fcf_ttm"]) or NONPOS, "ratio")
    m.inputs.update(assumptions)
    out.append(m)
    return out
