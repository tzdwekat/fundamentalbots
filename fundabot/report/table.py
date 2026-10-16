"""Plain-text metrics table for the CLI."""
import pandas as pd

from fundabot.analytics.metrics import Metric

GROUPS = {
    "growth": ["revenue", "gross_profit", "operating_income", "net_income", "eps_diluted", "fcf", "revenue_yoy", "gross_profit_yoy", "operating_income_yoy",
               "eps_diluted_yoy", "fcf_yoy", "revenue_cagr_3y", "revenue_cagr_5y", "eps_diluted_cagr_3y", "eps_diluted_cagr_5y", "fcf_cagr_3y", "fcf_cagr_5y"],
    "profitability": ["gross_margin", "operating_margin", "net_margin", "fcf_margin", "roic", "roe", "roa"],
    "balance_sheet": ["total_debt", "net_debt", "cash", "net_debt_to_ebitda", "interest_coverage", "current_ratio", "goodwill_to_assets"],
    "cash_quality": ["fcf_to_net_income", "accruals_ratio", "sbc_to_revenue", "capex_to_da"],
    "capital_allocation": ["net_buybacks", "dividend_coverage", "acquisitions_to_ocf"],
    "valuation": ["price", "shares_outstanding", "market_cap", "ev", "pe", "ev_ebitda", "ev_sales", "fcf_yield", "peg",
                  "pe_5y_min", "pe_5y_median", "pe_5y_max", "pe_5y_percentile", "ev_ebitda_5y_median", "ev_ebitda_5y_percentile",
                  "ev_sales_5y_median", "fcf_yield_5y_median"],
    "intrinsic_value": ["dcf_bear", "dcf_base", "dcf_bull", "reverse_dcf_implied_growth"],
    "earnings_signals (consensus, FMP)": ["eps_surprise", "revenue_surprise", "eps_beat_streak", "eps_actual_fmp_ttm", "eps_estimate_next_fy",
                                          "eps_growth_consensus_next_fy", "forward_pe", "revenue_estimate_next_fy", "price_target_upside",
                                          "eps_estimate_revision_30d", "eps_estimate_revision_90d", "guidance_direction", "management_tone"],
}


def fmt(m: Metric) -> str:
    if m.value is None:
        return f"null({m.null_reason})"
    v = m.value
    if m.unit == "USD":
        s = f"{v / 1e9:,.2f}B" if abs(v) >= 1e9 else f"{v / 1e6:,.1f}M"
    elif m.unit == "shares":
        s = f"{v / 1e6:,.0f}M"
    elif m.unit == "count":
        s = f"{v:.0f}"
    elif m.unit == "USD/share":
        s = f"{v:,.2f}"
    elif any(k in m.metric_id for k in ("margin", "yoy", "cagr", "yield", "to_assets", "to_revenue", "percentile", "growth", "surprise", "upside", "revision")) or m.metric_id in ("roe", "roa", "roic", "accruals_ratio"):
        s = f"{v:.1%}"
    else:
        s = f"{v:,.2f}"
    return s + ("*" if m.flags else "")


def columns_for(metrics: list[Metric]) -> list[str]:
    fys = sorted({m.period for m in metrics if m.period.startswith("FY")})[-5:]
    qs = sorted({m.period for m in metrics if m.period.startswith("Q")}, key=lambda p: (int(p[4:]), int(p[1])))[-4:]
    return fys + qs + (["current"] if any(m.period == "current" for m in metrics) else [])


def format_table(metrics: list[Metric]) -> str:
    cols = columns_for(metrics)
    lookup = {(m.metric_id, m.period): m for m in metrics}
    out = []
    for group, ids in GROUPS.items():
        rows = []
        for mid in ids:
            cells = [fmt(lookup[(mid, c)]) if (mid, c) in lookup else "" for c in cols]
            if any(cells):
                rows.append([mid] + cells)
        if rows:
            out.append(f"\n== {group} ==")
            out.append(pd.DataFrame(rows, columns=["metric"] + cols).to_string(index=False))
    out.append("\n* = flagged (restated, derived, out_of_bounds, partial data); see --json for details")
    return "\n".join(out)
