"""Plausibility ranges per metric id. Out-of-range values are kept but flagged."""

BOUNDS: dict[str, tuple[float, float]] = {
    "gross_margin": (-2, 1),
    "operating_margin": (-2, 1),
    "net_margin": (-2, 1),
    "fcf_margin": (-2, 1),
    "roe": (-2, 2),
    "roa": (-2, 2),
    "roic": (-2, 2),
    "current_ratio": (0, 50),
    "goodwill_to_assets": (0, 1),
    "accruals_ratio": (-1, 1),
    "sbc_to_revenue": (0, 1),
    "capex_to_da": (0, 20),
}
GROWTH_BOUNDS = (-1, 10)


def check(metric_id: str, value: float) -> bool:
    lo, hi = BOUNDS.get(metric_id) or (GROWTH_BOUNDS if metric_id.endswith(("_yoy", "_cagr_3y", "_cagr_5y")) else (None, None))
    return lo is None or lo <= value <= hi
