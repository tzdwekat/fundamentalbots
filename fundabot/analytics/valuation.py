"""Valuation multiples (current and per-filing history), own 5y ranges, DCF and reverse DCF.

All trailing-twelve-month flows come from the metrics table by period label, so derived items
(composed D&A, FCF, EBITDA, YTD-derived quarters) are included and every input is cited.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from fundabot.analytics.metrics import NONPOS, Metric, anchor_name, combine, _prov

RATIOS = ["pe", "ev_ebitda", "ev_sales", "fcf_yield"]
RANGE_STATS = ["5y_min", "5y_median", "5y_max", "5y_percentile"]
POINT_IDS = ["price", "shares_outstanding", "market_cap", "ev", "eps_ttm", "ebitda_ttm", "revenue_ttm", "fcf_ttm"] + RATIOS
CURRENT_ONLY = ["peg", "dcf_bear", "dcf_base", "dcf_bull", "reverse_dcf_implied_growth"] + [f"{r}_{s}" for r in RATIOS for s in RANGE_STATS]
DISCOUNT_RATE, TERMINAL_GROWTH, YEARS = 0.10, 0.025, 10
ORIGINAL_FORMS = ["10-K", "10-Q"]


def shares_outstanding(facts: pd.DataFrame) -> pd.DataFrame:
    """Cover-page shares per date: latest accession for that date, all entries summed."""
    dei = facts[(facts.taxonomy == "dei") & (facts.tag == "EntityCommonStockSharesOutstanding")]
    if "split_adjusted" not in dei.columns:
        dei = dei.assign(split_adjusted=False)
    rows = []
    for end, grp in dei.groupby("end_date"):
        acc = grp.sort_values(["filed", "accession"]).accession.iloc[-1]
        sel = grp[grp.accession == acc]
        rows.append((end, float(sel.value.sum()), acc, bool(sel.split_adjusted.any())))
    return pd.DataFrame(rows, columns=["end_date", "shares", "accession", "split_adjusted"]).sort_values("end_date").reset_index(drop=True)


def dcf_ev(fcf0: float, g: float, r: float = DISCOUNT_RATE, tg: float = TERMINAL_GROWTH, years: int = YEARS) -> float:
    pv = sum(fcf0 * (1 + g) ** t / (1 + r) ** t for t in range(1, years + 1))
    tv = fcf0 * (1 + g) ** years * (1 + tg) / (r - tg)
    return pv + tv / (1 + r) ** years


def implied_growth(ev_target: float, fcf0: float, lo: float = -0.5, hi: float = 1.0) -> float | None:
    if fcf0 <= 0 or ev_target <= 0 or dcf_ev(fcf0, lo) > ev_target or dcf_ev(fcf0, hi) < ev_target:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if dcf_ev(fcf0, mid) < ev_target else (lo, mid)
        if hi - lo < 1e-4:
            break
    return (lo + hi) / 2


def _prev_quarter(label: str) -> str:
    fq, fy = int(label[1]), int(label[4:])
    return f"Q{fq - 1}FY{fy}" if fq > 1 else f"Q4FY{fy - 1}"


def ttm_from_table(fm: dict, item: str, label: str, out_id: str) -> Metric | None:
    """TTM of a flow item at a period label: the FY value for FY labels, else the sum of four consecutive quarters."""
    unit = "USD/share" if item == "eps_diluted" else "USD"
    if label is None:
        return None
    if label.startswith("FY"):
        m = fm.get((item, label))
        return None if m is None or m.value is None else Metric(out_id, label, m.value, unit, f"{item}@{label} (fiscal year)",
                                                                {item: _prov(item, m, label)}, list(m.flags), m.tag_used)
    labels, cur = [], label
    for _ in range(4):
        labels.append(cur)
        cur = _prev_quarter(cur)
    ms = [fm.get((item, lb)) for lb in labels]
    if any(m is None or m.value is None for m in ms):
        return None
    return Metric(out_id, label, float(sum(m.value for m in ms)), unit, f"sum({item} over {', '.join(labels)})",
                  {f"{item}@{lb}": _prov(item, m, lb) for lb, m in zip(labels, ms)},
                  sorted({f for m in ms for f in m.flags}), ms[0].tag_used)


def _net_debt(fm: dict, bs_period: str) -> Metric | None:
    nd = fm.get(("net_debt", bs_period))
    if nd is not None:
        return nd
    cash = fm.get(("cash", bs_period))
    if cash is None or cash.value is None or fm.get(("total_debt", bs_period)) is not None:
        return None
    return Metric("net_debt", bs_period, -cash.value, "USD", "-cash (no debt tag)", {"cash": _prov("cash", cash, bs_period)}, ["no_debt_tag"], None)


def _shares(sh: pd.DataFrame, fm: dict, period: str, label: str, as_of: date | None = None, accession: str | None = None) -> Metric | None:
    """Cover-page shares (by accession, else latest on or before as_of); else latest diluted weighted average, flagged."""
    row = sh[sh.accession == accession] if accession else sh.iloc[0:0]
    if row.empty and as_of is not None:
        row = sh[sh.end_date <= as_of].tail(1)
    if not row.empty:
        r = row.iloc[0]
        return Metric("shares_outstanding", period, r.shares, "shares", f"dei:EntityCommonStockSharesOutstanding as of {r.end_date}", {},
                      ["split_adjusted"] if r.split_adjusted else [], "EntityCommonStockSharesOutstanding")
    d = fm.get(("diluted_shares", label)) if label else None
    if d is None or d.value is None:
        return None
    return Metric("shares_outstanding", period, d.value, "shares", f"diluted weighted-average shares @{label} (no cover-page count)",
                  {"diluted_shares": _prov("diluted_shares", d, label)}, sorted(set(d.flags) | {"shares_diluted_weighted"}), d.tag_used)


def _point(period, price_pt, shares, label, fm) -> list[Metric]:
    """Market cap, EV, TTM flows and the four ratios for one valuation point at a period label."""
    out = []
    price = Metric("price", period, price_pt[1], "USD/share", f"close on {price_pt[0]}", {}, [], None) if price_pt else None
    out += [m for m in (price, shares) if m]
    mcap = combine("market_cap", period, "price * shares_outstanding", {"price": price, "shares_outstanding": shares},
                   lambda v: v["price"] * v["shares_outstanding"], "USD")
    net_debt = _net_debt(fm, label)
    ev = combine("ev", period, "market_cap + net_debt", {"market_cap": mcap, "net_debt": net_debt}, lambda v: v["market_cap"] + v["net_debt"], "USD")
    eps = ttm_from_table(fm, "eps_diluted", label, "eps_ttm")
    ebitda = ttm_from_table(fm, "ebitda", label, "ebitda_ttm")
    rev = ttm_from_table(fm, "revenue", label, "revenue_ttm")
    fcf = ttm_from_table(fm, "fcf", label, "fcf_ttm")
    for m in (eps, ebitda, rev, fcf):
        if m:
            m.period = period
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
    from fundabot.ingest.prices import close_on_or_after, close_on_or_before

    if prices_df is None or prices_df.empty:
        return [Metric(mid, "current", None, "ratio", mid, {}, [], None, "price_unavailable") for mid in POINT_IDS + CURRENT_ONLY]
    fm = {(m.metric_id, m.period): m for m in fundamentals}
    anchor = series[anchor_name(series)]
    sh = shares_outstanding(facts)
    out: list[Metric] = []

    # Current point: latest quarter on or before as_of.
    q_ends = anchor[(anchor.fq >= 1) & (anchor.end <= as_of)]
    cur_label = _period_label(anchor, q_ends.end.max()) if not q_ends.empty else None
    out += _point("current", close_on_or_before(prices_df, as_of), _shares(sh, fm, "current", cur_label, as_of=as_of), cur_label, fm)

    # One point per original 10-K/10-Q filing in the last 5 years, newest first, one per period label.
    hist = filings[filings.form.isin(ORIGINAL_FORMS) & (filings.filed >= as_of - timedelta(days=5 * 365))].sort_values("filed", ascending=False)
    seen = set()
    for f in hist.itertuples(index=False):
        label = _period_label(anchor, f.period_end, annual=f.form == "10-K") if f.period_end else None
        if label is None or label in seen:
            continue
        seen.add(label)
        out += _point(label, close_on_or_after(prices_df, f.filed), _shares(sh, fm, label, label, as_of=f.filed, accession=f.accession), label, fm)

    # Own 5y ranges.
    cur = {m.metric_id: m for m in out if m.period == "current"}
    for r in RATIOS:
        hist_ms = [m for m in out if m.metric_id == r and m.period != "current"]
        hist_vals = [m.value for m in hist_ms if m.value is not None]
        inputs = {f"{r}@{m.period}": {"item": r, "period": m.period, "value": m.value, "tag": None} for m in hist_ms}
        for stat, fn in (("5y_min", min), ("5y_median", lambda v: float(np.median(v))), ("5y_max", max)):
            out.append(Metric(f"{r}_{stat}", "current", fn(hist_vals) if hist_vals else None, "ratio", f"{stat} of {r} over filings in last 5y",
                              inputs, [], None, None if hist_vals else "no_history"))
        c = cur[r].value
        pct = sum(v <= c for v in hist_vals) / len(hist_vals) if hist_vals and c is not None else None
        out.append(Metric(f"{r}_5y_percentile", "current", pct, "ratio", f"share of historical {r} values <= current", inputs, [], None,
                          None if pct is not None else "no_history"))

    # PEG and DCF.
    latest_fy = f"FY{int(anchor[anchor.fq == 0].fy.max())}" if (anchor.fq == 0).any() else None
    out.append(combine("peg", "current", "pe / (eps_diluted_cagr_3y * 100)", {"pe": cur["pe"], "eps_diluted_cagr_3y": fm.get(("eps_diluted_cagr_3y", latest_fy))},
                       lambda v: v["pe"] / (v["eps_diluted_cagr_3y"] * 100) if v["eps_diluted_cagr_3y"] > 0 else NONPOS))
    growth = next((g for g in (fm.get(("fcf_cagr_5y", latest_fy)), fm.get(("fcf_cagr_3y", latest_fy))) if g and g.value is not None), None)
    fcf0, ev, net_debt, shares = cur.get("fcf_ttm"), cur.get("ev"), _net_debt(fm, cur_label), cur.get("shares_outstanding")
    base_g = min(max(growth.value, 0.0), 0.25) if growth else None
    assumptions = {"discount_rate": DISCOUNT_RATE, "terminal_growth": TERMINAL_GROWTH, "years": YEARS}
    for name, delta in (("dcf_bear", -0.05), ("dcf_base", 0.0), ("dcf_bull", 0.05)):
        if base_g is None:
            out.append(Metric(name, "current", None, "USD/share", "dcf", assumptions, [], None, "no_fcf_history"))
            continue
        g = base_g + delta
        m = combine(name, "current", f"(dcf_ev(fcf_ttm, growth={g:.3f}) - net_debt) / shares_outstanding",
                    {"fcf_ttm": fcf0, "net_debt": net_debt, "shares_outstanding": shares},
                    lambda v, g=g: (dcf_ev(v["fcf_ttm"], g) - v["net_debt"]) / v["shares_outstanding"] if v["fcf_ttm"] > 0 and v["shares_outstanding"] > 0 else NONPOS,
                    "USD/share")
        m.inputs.update({"growth": g, **assumptions})
        out.append(m)
    m = combine("reverse_dcf_implied_growth", "current", "growth g such that dcf_ev(fcf_ttm, g) = ev", {"fcf_ttm": fcf0, "ev": ev},
                lambda v: implied_growth(v["ev"], v["fcf_ttm"]) or NONPOS, "ratio")
    m.inputs.update(assumptions)
    out.append(m)
    return out
