"""Deterministic fundamental metrics from period series. The only place numbers are produced (with valuation.py)."""
from dataclasses import dataclass, field, asdict

import pandas as pd

from fundabot.analytics import bounds, periods

GROWTH_ITEMS = ["revenue", "gross_profit", "operating_income", "eps_diluted", "fcf"]
PROPAGATED_FLAGS = {"restated", "derived", "partial_debt", "no_prior_year", "tax_rate_clamped", "no_issuance_data", "no_debt_tag"}
NONPOS = object()  # sentinel: nonpositive denominator


@dataclass
class Metric:
    metric_id: str
    period: str
    value: float | None
    unit: str
    formula_id: str
    inputs: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    tag_used: str | None = None
    null_reason: str | None = None

    def to_dict(self):
        return asdict(self)


def _prov(name: str, m: "Metric | None", period: str) -> dict:
    if m is None:
        return {"item": name, "period": period, "value": None, "tag": None}
    return {"item": m.metric_id, "period": m.period, "value": m.value, "tag": m.tag_used}


def combine(metric_id, period, formula, inputs: dict, fn, unit="ratio", flags=()) -> Metric:
    """Build a metric from named input metrics; None/missing input -> tag_missing; NONPOS -> nonpositive_denominator."""
    prov = {name: _prov(name, m, period) for name, m in inputs.items()}
    out_flags = sorted(set(flags) | {f for m in inputs.values() if m for f in m.flags if f in PROPAGATED_FLAGS})
    tag = None  # derived metrics carry their tags in `inputs`; base items carry tag_used
    missing = [name for name, m in inputs.items() if m is None or m.value is None]
    if missing:
        return Metric(metric_id, period, None, unit, formula, prov, out_flags, tag, "tag_missing")
    v = fn({name: m.value for name, m in inputs.items()})
    if v is NONPOS:
        return Metric(metric_id, period, None, unit, formula, prov, out_flags, tag, "nonpositive_denominator")
    if v is None:
        return Metric(metric_id, period, None, unit, formula, prov, out_flags, tag, "nonpositive_endpoint")
    if not bounds.check(metric_id, v):
        out_flags = sorted(set(out_flags) | {"out_of_bounds"})
    return Metric(metric_id, period, float(v), unit, formula, prov, out_flags, tag, None)


def _div(n, d):
    return n / d if d > 0 else NONPOS


def _yoy(v):
    return v["current"] / v["prior"] - 1 if v["prior"] > 0 else NONPOS


class _Table:
    def __init__(self):
        self.m: dict[tuple[str, str], Metric] = {}

    def add(self, metric: Metric):
        self.m[(metric.metric_id, metric.period)] = metric

    def get(self, metric_id, period) -> Metric | None:
        return self.m.get((metric_id, period))

    def periods(self, metric_id) -> list[str]:
        return [p for (i, p) in self.m if i == metric_id]


def _row_flags(row) -> list[str]:
    return [f for f in ("restated", "derived") if getattr(row, f)]


def compute_fundamental_metrics(series: dict[str, pd.DataFrame]) -> list[Metric]:
    t = _Table()
    if not series:
        return []
    anchor = series[anchor_name(series)]
    fys = sorted(anchor[anchor.fq == 0].fy.unique())
    latest_fy = int(fys[-1]) if len(fys) else int(anchor.fy.max())
    fy_window = [f"FY{y}" for y in range(latest_fy - 4, latest_fy + 1)]
    base_window = set(range(latest_fy - 10, latest_fy + 2))
    quarters = list(anchor[anchor.fq >= 1].sort_values("end").tail(8).period)

    # 1. Base items straight from XBRL.
    for item, df in series.items():
        unit = "USD/share" if item == "eps_diluted" else "USD"
        for r in df.itertuples(index=False):
            if r.fy in base_window:
                t.add(Metric(item, r.period, float(r.value), unit, f"xbrl:{r.tag}", {}, _row_flags(r), r.tag))

    # 2. Derived base items, for every period where inputs exist.
    all_periods = sorted({p for (_, p) in t.m})
    for p in all_periods:
        if t.get("gross_profit", p) is None and t.get("revenue", p) and t.get("cost_of_revenue", p):
            t.add(combine("gross_profit", p, "revenue - cost_of_revenue",
                          {"revenue": t.get("revenue", p), "cost_of_revenue": t.get("cost_of_revenue", p)},
                          lambda v: v["revenue"] - v["cost_of_revenue"], "USD"))
        if t.get("ocf", p) and t.get("capex", p):
            t.add(combine("fcf", p, "ocf - capex", {"ocf": t.get("ocf", p), "capex": t.get("capex", p)},
                          lambda v: v["ocf"] - v["capex"], "USD"))
        if t.get("operating_income", p) and t.get("da", p):
            t.add(combine("ebitda", p, "operating_income + da",
                          {"operating_income": t.get("operating_income", p), "da": t.get("da", p)},
                          lambda v: v["operating_income"] + v["da"], "USD"))
        ltd, std = t.get("long_term_debt", p), t.get("short_term_debt", p)
        if ltd:
            if std:
                t.add(combine("total_debt", p, "short_term_debt + long_term_debt", {"short_term_debt": std, "long_term_debt": ltd},
                              lambda v: v["short_term_debt"] + v["long_term_debt"], "USD"))
            else:
                t.add(combine("total_debt", p, "long_term_debt (short-term missing)", {"long_term_debt": ltd},
                              lambda v: v["long_term_debt"], "USD", flags=["partial_debt"]))
        if t.get("total_debt", p) and t.get("cash", p):
            t.add(combine("net_debt", p, "total_debt - cash", {"total_debt": t.get("total_debt", p), "cash": t.get("cash", p)},
                          lambda v: v["total_debt"] - v["cash"], "USD"))

    out: list[Metric] = list(t.m.values())

    def prev_fy(p):
        return f"FY{int(p[2:]) - 1}"

    def avg(item, p):
        """Average of FY and prior FY; falls back to FY alone with a flag."""
        cur, prior = t.get(item, p), t.get(item, prev_fy(p))
        if cur is None or cur.value is None:
            return None
        if prior is None or prior.value is None:
            return Metric(f"avg_{item}", p, cur.value, cur.unit, f"{item} (no prior year)", {item: _prov(item, cur, p)},
                          sorted(set(cur.flags) | {"no_prior_year"}), cur.tag_used)
        return Metric(f"avg_{item}", p, (cur.value + prior.value) / 2, cur.unit, f"avg({item}, {item} prior FY)",
                      {item: _prov(item, cur, p), f"{item}_prior": _prov(item, prior, prev_fy(p))},
                      sorted(set(cur.flags) | set(prior.flags)), cur.tag_used)

    # 3. Growth.
    for item in GROWTH_ITEMS:
        for p in fy_window:
            fy = int(p[2:])
            out.append(combine(f"{item}_yoy", p, f"{item} / {item} prior FY - 1",
                               {"current": t.get(item, p), "prior": t.get(item, prev_fy(p))}, _yoy))
            for n in (3, 5):
                out.append(combine(f"{item}_cagr_{n}y", p, f"({item} / {item} {n} FY earlier) ^ (1/{n}) - 1",
                                   {"end": t.get(item, p), "start": t.get(item, f"FY{fy - n}")},
                                   lambda v, n=n: (v["end"] / v["start"]) ** (1 / n) - 1 if v["end"] > 0 and v["start"] > 0 else None))
        for q in quarters:
            fq, fy = int(q[1]), int(q[4:])
            out.append(combine(f"{item}_yoy", q, f"{item} / {item} same quarter prior FY - 1",
                               {"current": t.get(item, q), "prior": t.get(item, f"Q{fq}FY{fy - 1}")}, _yoy))

    # 4. Profitability, cash quality, capital allocation (FY).
    for p in fy_window:
        for mid, num in (("gross_margin", "gross_profit"), ("operating_margin", "operating_income"),
                         ("net_margin", "net_income"), ("fcf_margin", "fcf")):
            out.append(combine(mid, p, f"{num} / revenue", {num: t.get(num, p), "revenue": t.get("revenue", p)},
                               lambda v, num=num: _div(v[num], v["revenue"])))
        out.append(combine("roe", p, "net_income / avg equity", {"net_income": t.get("net_income", p), "avg_equity": avg("equity", p)},
                           lambda v: _div(v["net_income"], v["avg_equity"])))
        out.append(combine("roa", p, "net_income / avg total_assets",
                           {"net_income": t.get("net_income", p), "avg_total_assets": avg("total_assets", p)},
                           lambda v: _div(v["net_income"], v["avg_total_assets"])))
        out.append(_roic(t, p, avg))
        out.append(combine("fcf_to_net_income", p, "fcf / net_income", {"fcf": t.get("fcf", p), "net_income": t.get("net_income", p)},
                           lambda v: _div(v["fcf"], v["net_income"])))
        out.append(combine("accruals_ratio", p, "(net_income - ocf) / avg total_assets",
                           {"net_income": t.get("net_income", p), "ocf": t.get("ocf", p), "avg_total_assets": avg("total_assets", p)},
                           lambda v: _div(v["net_income"] - v["ocf"], v["avg_total_assets"])))
        out.append(combine("sbc_to_revenue", p, "sbc / revenue", {"sbc": t.get("sbc", p), "revenue": t.get("revenue", p)},
                           lambda v: _div(v["sbc"], v["revenue"])))
        out.append(combine("capex_to_da", p, "capex / da", {"capex": t.get("capex", p), "da": t.get("da", p)},
                           lambda v: _div(v["capex"], v["da"])))
        if t.get("buybacks", p) and t.get("share_issuance", p) is None:
            out.append(combine("net_buybacks", p, "buybacks (no issuance data)", {"buybacks": t.get("buybacks", p)},
                               lambda v: v["buybacks"], "USD", flags=["no_issuance_data"]))
        else:
            out.append(combine("net_buybacks", p, "buybacks - share_issuance",
                               {"buybacks": t.get("buybacks", p), "share_issuance": t.get("share_issuance", p)},
                               lambda v: v["buybacks"] - v["share_issuance"], "USD"))
        out.append(combine("dividend_coverage", p, "fcf / dividends_paid",
                           {"fcf": t.get("fcf", p), "dividends_paid": t.get("dividends_paid", p)},
                           lambda v: _div(v["fcf"], v["dividends_paid"])))
        out.append(combine("acquisitions_to_ocf", p, "acquisitions / ocf",
                           {"acquisitions": t.get("acquisitions", p), "ocf": t.get("ocf", p)},
                           lambda v: _div(v["acquisitions"], v["ocf"])))

    # 5. Balance sheet: FY window plus the latest quarter (TTM flows).
    for p in fy_window + quarters[-1:]:
        is_q = p.startswith("Q")
        ebitda, opinc, intexp = t.get("ebitda", p), t.get("operating_income", p), t.get("interest_expense", p)
        if is_q:
            as_of = series[anchor_name(series)][series[anchor_name(series)].period == p].end.iloc[0]
            ebitda = _ttm_metric("ebitda", series, as_of, p, ("operating_income", "da"))
            opinc = _ttm_metric("operating_income", series, as_of, p)
            intexp = _ttm_metric("interest_expense", series, as_of, p)
        out.append(combine("net_debt_to_ebitda", p, "net_debt / ebitda" + (" (TTM)" if is_q else ""),
                           {"net_debt": t.get("net_debt", p), "ebitda": ebitda}, lambda v: _div(v["net_debt"], v["ebitda"])))
        out.append(combine("interest_coverage", p, "operating_income / interest_expense" + (" (TTM)" if is_q else ""),
                           {"operating_income": opinc, "interest_expense": intexp},
                           lambda v: _div(v["operating_income"], v["interest_expense"])))
        out.append(combine("current_ratio", p, "current_assets / current_liabilities",
                           {"current_assets": t.get("current_assets", p), "current_liabilities": t.get("current_liabilities", p)},
                           lambda v: _div(v["current_assets"], v["current_liabilities"])))
        out.append(combine("goodwill_to_assets", p, "goodwill / total_assets",
                           {"goodwill": t.get("goodwill", p), "total_assets": t.get("total_assets", p)},
                           lambda v: _div(v["goodwill"], v["total_assets"])))
    return out


def anchor_name(series):
    return next((i for i in ("revenue", "net_income") if i in series), next(iter(series)))


def _ttm_metric(item, series, as_of, period, parts=None) -> Metric | None:
    """TTM value of an item (or sum of parts) as a Metric, for quarter-period balance-sheet ratios."""
    items = parts or (item,)
    vals = []
    for it in items:
        if it not in series:
            return None
        v = periods.ttm(series[it], as_of)
        if v is None:
            return None
        vals.append(v)
    tag = series[items[0]].tag.iloc[0]
    return Metric(f"{item}_ttm", period, float(sum(vals)), "USD", f"ttm({' + '.join(items)})", {}, [], tag)


def _roic(t, p, avg) -> Metric:
    tax, pretax = t.get("income_tax", p), t.get("pretax_income", p)
    flags = []
    if tax is None or pretax is None or tax.value is None or pretax.value is None or pretax.value <= 0:
        rate, rate_prov = None, None
    else:
        raw = tax.value / pretax.value
        rate = min(max(raw, 0.0), 0.35)
        if rate != raw:
            flags.append("tax_rate_clamped")
        rate_prov = Metric("tax_rate", p, rate, "ratio", "clamp(income_tax / pretax_income, 0, 0.35)",
                           {"income_tax": _prov("income_tax", tax, p), "pretax_income": _prov("pretax_income", pretax, p)}, flags, tax.tag_used)
    cap = {"avg_equity": avg("equity", p), "avg_total_debt": avg("total_debt", p), "avg_cash": avg("cash", p)}
    inputs = {"operating_income": t.get("operating_income", p), "tax_rate": rate_prov, **cap}
    return combine("roic", p, "operating_income * (1 - tax_rate) / avg(equity + total_debt - cash)", inputs,
                   lambda v: _div(v["operating_income"] * (1 - v["tax_rate"]), v["avg_equity"] + v["avg_total_debt"] - v["avg_cash"]),
                   flags=flags)
