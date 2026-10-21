"""Human labels, formats and flag words for metric ids. The note never shows a bare id or flag name outside the appendix."""
LABELS: dict[str, tuple[str, str]] = {  # id -> (label, kind)
    "revenue": ("Revenue", "usd"), "gross_profit": ("Gross profit", "usd"), "operating_income": ("Operating income", "usd"), "net_income": ("Net income", "usd"),
    "eps_diluted": ("Diluted EPS", "usd_share"), "fcf": ("Free cash flow", "usd"), "revenue_yoy": ("Revenue growth", "pct"), "gross_profit_yoy": ("Gross profit growth", "pct"),
    "operating_income_yoy": ("Operating income growth", "pct"), "eps_diluted_yoy": ("EPS growth", "pct"), "fcf_yoy": ("Free cash flow growth", "pct"),
    "revenue_cagr_3y": ("Revenue 3-year CAGR", "pct"), "revenue_cagr_5y": ("Revenue 5-year CAGR", "pct"), "eps_diluted_cagr_3y": ("EPS 3-year CAGR", "pct"),
    "eps_diluted_cagr_5y": ("EPS 5-year CAGR", "pct"), "fcf_cagr_3y": ("FCF 3-year CAGR", "pct"), "fcf_cagr_5y": ("FCF 5-year CAGR", "pct"),
    "gross_margin": ("Gross margin", "pct"), "operating_margin": ("Operating margin", "pct"), "net_margin": ("Net margin", "pct"), "fcf_margin": ("FCF margin", "pct"),
    "roic": ("Return on invested capital", "pct"), "roe": ("Return on equity", "pct"), "roa": ("Return on assets", "pct"),
    "total_debt": ("Total debt", "usd"), "net_debt": ("Net debt", "usd"), "cash": ("Cash", "usd"), "net_debt_to_ebitda": ("Net debt to EBITDA", "x"),
    "interest_coverage": ("Interest coverage", "x"), "current_ratio": ("Current ratio", "x"), "goodwill_to_assets": ("Goodwill share of assets", "pct"),
    "fcf_to_net_income": ("FCF to net income", "x"), "accruals_ratio": ("Accruals ratio", "pct"), "sbc_to_revenue": ("Stock compensation share of revenue", "pct"), "capex_to_da": ("Capex to depreciation", "x"),
    "net_buybacks": ("Net buybacks", "usd"), "dividend_coverage": ("Dividend coverage by FCF", "x"), "acquisitions_to_ocf": ("Acquisitions share of operating cash flow", "x"),
    "dividend_per_share_ttm": ("Dividend per share (TTM)", "usd_share"), "dividend_yield": ("Dividend yield", "pct"), "dividend_growth_5y": ("Dividend 5-year growth", "pct"),
    "price": ("Price", "usd_share"), "shares_outstanding": ("Shares outstanding", "shares"), "market_cap": ("Market cap", "usd"), "ev": ("Enterprise value", "usd"),
    "pe": ("P/E", "x"), "ev_ebitda": ("EV/EBITDA", "x"), "ev_sales": ("EV/sales", "x"), "fcf_yield": ("FCF yield", "pct"), "peg": ("PEG", "x"),
    "pe_5y_min": ("P/E 5-year low", "x"), "pe_5y_median": ("P/E 5-year median", "x"), "pe_5y_max": ("P/E 5-year high", "x"), "pe_5y_percentile": ("P/E 5-year percentile", "pct"),
    "ev_ebitda_5y_median": ("EV/EBITDA 5-year median", "x"), "ev_ebitda_5y_percentile": ("EV/EBITDA 5-year percentile", "pct"), "ev_sales_5y_median": ("EV/sales 5-year median", "x"),
    "fcf_yield_5y_median": ("FCF yield 5-year median", "pct"), "pe_peer_median": ("Peer median P/E", "x"), "pe_vs_peer_median": ("P/E vs peer median", "x"),
    "ev_sales_peer_median": ("Peer median EV/sales", "x"), "ev_sales_vs_peer_median": ("EV/sales vs peer median", "x"), "ev_ebitda_peer_median": ("Peer median EV/EBITDA", "x"),
    "dcf_bear": ("DCF value, bear case", "usd_share"), "dcf_base": ("DCF value, base case", "usd_share"), "dcf_bull": ("DCF value, bull case", "usd_share"),
    "reverse_dcf_implied_growth": ("Growth the price implies", "pct"), "risk_free_10y": ("10-year Treasury yield", "pct"), "altman_z": ("Altman Z-score", "score"), "piotroski": ("Piotroski score", "count"),
    "eps_surprise": ("EPS surprise", "pct"), "revenue_surprise": ("Revenue surprise", "pct"), "eps_beat_streak": ("Consecutive EPS beats", "count"), "eps_actual_fmp_ttm": ("EPS actual (TTM, FMP)", "usd_share"),
    "eps_estimate_next_fy": ("Consensus EPS, next fiscal year", "usd_share"), "eps_growth_consensus_next_fy": ("Consensus EPS growth, next fiscal year", "pct"), "forward_pe": ("Forward P/E", "x"),
    "revenue_estimate_next_fy": ("Consensus revenue, next fiscal year", "usd"), "price_target_upside": ("Analyst target upside", "pct"), "eps_estimate_revision_30d": ("EPS estimate revision, 30 days", "pct"),
    "eps_estimate_revision_90d": ("EPS estimate revision, 90 days", "pct"), "eps_estimate_next_q": ("Consensus EPS, next quarter", "usd_share"), "revenue_estimate_next_q": ("Consensus revenue, next quarter", "usd"),
    "eps_estimate_next_q_revision_30d": ("Next-quarter EPS revision, 30 days", "pct"), "eps_estimate_next_q_revision_90d": ("Next-quarter EPS revision, 90 days", "pct"),
    "guidance_direction": ("Guidance direction", "score"), "management_tone": ("Management tone", "score"), "inst_holders_change_pct": ("Change in institutional holders", "pct"),
    "inst_shares_change_pct": ("Change in institutional shares", "pct"), "insider_net_shares_90d": ("Insider net shares, 90 days", "shares"), "insider_buy_sell_ratio": ("Insider buy/sell ratio", "x"),
}
FLAG_WORDS = {"derived": "derived", "restated": "restated", "source_conflict": "FMP and the filing disagree", "split_adjusted": "split-adjusted", "vendor_fmp": "vendor data",
              "llm_extracted": "extracted from a call", "out_of_bounds": "outside normal bounds", "tag_ambiguous": "ambiguous tag", "partial_debt": "partial debt data",
              "da_composed": "D&A composed", "depreciation_only": "depreciation only", "no_issuance_data": "no issuance data", "debt_total_tag": "reported total debt",
              "shares_diluted_weighted": "weighted diluted shares", "no_prior_year": "no prior year", "tax_rate_clamped": "tax rate clamped", "no_debt_tag": "no debt tag"}


def label(mid: str) -> str:
    return LABELS.get(mid, (mid.replace("_", " "), "ratio"))[0]


def kind(mid: str) -> str:
    if mid.startswith("seg_"):
        return "usd" if mid.endswith("_revenue") else "pct"
    return LABELS.get(mid, ("", "ratio"))[1]


def seg_label(mid: str, inputs: dict | None) -> str:
    name = (inputs or {}).get("name") or mid.split("_", 2)[-1].rsplit("_", 1)[0].replace("_", " ")
    field = mid.rsplit("_", 1)[1]
    return {"revenue": f"{name} revenue", "share": f"{name} share of revenue", "yoy": f"{name} growth"}.get(field, f"{name} {field}")


def fmt(value, k: str) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    if k == "usd":
        a = abs(v)
        s = f"${a / 1e9:,.1f}B" if a >= 1e9 else f"${a / 1e6:,.1f}M" if a >= 1e6 else f"${a:,.0f}"
        return ("-" if v < 0 else "") + s
    if k == "pct":
        return f"{v * 100:.1f}%"
    if k == "x":
        return f"{v:.1f}x"
    if k == "count":
        return f"{v:.0f}"
    if k == "shares":
        return f"{v / 1e6:,.0f}M shares"
    if k == "usd_share":
        return f"${v:,.2f}"
    if k == "score":
        return f"{v:.1f}"
    return f"{v:,.2f}"


def flag_words(flags) -> str:
    return "; ".join(FLAG_WORDS[f] for f in flags if f in FLAG_WORDS)


def unlabelled(ids) -> list[str]:
    return sorted(i for i in ids if i not in LABELS and not i.startswith("seg_"))
