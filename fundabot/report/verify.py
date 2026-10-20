"""Diagnostic: compare our FY values with yfinance's statements (an independent source)."""
from datetime import date, timedelta

import pandas as pd

from fundabot.analytics.metrics import Metric

CHECKS = {  # our metric id -> (yfinance statement attr, row label)
    "revenue": ("income_stmt", "Total Revenue"),
    "net_income": ("income_stmt", "Net Income"),
    "total_assets": ("balance_sheet", "Total Assets"),
    "ocf": ("cashflow", "Operating Cash Flow"),
    "eps_diluted": ("income_stmt", "Diluted EPS"),  # split-adjusted on yfinance's side: catches missed splits
}


def cross_check(ticker: str, metrics: list[Metric], tolerance: float = 0.02) -> str:
    import yfinance as yf

    tk = yf.Ticker(ticker.replace(".", "-"))
    rows = []
    for mid, (attr, label) in CHECKS.items():
        try:
            stmt = getattr(tk, attr)
            theirs = stmt.loc[label]
        except Exception as e:  # missing statement or label
            rows.append((mid, "", "", "", f"yfinance: {type(e).__name__}"))
            continue
        ours = {m.period: m for m in metrics if m.metric_id == mid and m.period.startswith("FY") and m.value is not None}
        for col, tv in theirs.items():
            if pd.isna(tv):
                continue
            end = col.date()
            match = [m for p, m in ours.items() if abs(_period_end(m) - end) <= timedelta(days=10)] if ours else []
            if not match:
                continue
            m = match[0]
            diff = (m.value - float(tv)) / abs(float(tv)) if tv else float("nan")
            f = "{:,.2f}" if mid == "eps_diluted" else "{:,.0f}"
            rows.append((mid, m.period, f.format(m.value), f.format(float(tv)), f"{diff:+.2%}" + ("  <-- CHECK" if abs(diff) > tolerance else "")))
    return pd.DataFrame(rows, columns=["metric", "period", "fundabot", "yfinance", "diff"]).to_string(index=False)


def _period_end(m: Metric):
    return date.fromisoformat(m.inputs["end"])


FMP_TTM = {  # our comparison id -> FMP TTM key
    "pe": "priceToEarningsRatioTTM", "ev_sales": "evToSalesTTM", "ev_ebitda": "evToEBITDATTM", "fcf_yield": "freeCashFlowYieldTTM",
    "gross_margin_ttm": "grossProfitMarginTTM", "operating_margin_ttm": "operatingProfitMarginTTM", "net_margin_ttm": "netProfitMarginTTM",
    "sbc_to_revenue_ttm": "stockBasedCompensationToRevenueTTM", "net_debt_to_ebitda_ttm": "netDebtToEBITDATTM", "current_ratio": "currentRatioTTM",
}


def _latest_quarters(metrics: list[Metric], mid: str, n: int = 4) -> list[float] | None:
    rows = sorted((m for m in metrics if m.metric_id == mid and m.period.startswith("Q") and m.value is not None), key=lambda m: (int(m.period[4:]), int(m.period[1])))
    return [float(m.value) for m in rows[-n:]] if len(rows) >= n else None


def _ttm_ratio(metrics: list[Metric], num: str, den: str = "revenue") -> float | None:
    a, b = _latest_quarters(metrics, num), _latest_quarters(metrics, den)
    return (sum(a) / sum(b)) if a and b and sum(b) else None


def _latest_q(metrics: list[Metric], mid: str) -> float | None:
    rows = _latest_quarters(metrics, mid, 1)
    return rows[0] if rows else None


def crosscheck_fmp(metrics: list[Metric], ttm: dict, tolerance: float = 0.10) -> list[dict]:
    """Our values against FMP's TTM figures, like with like; reports only, never writes. ok is None when either side is missing."""
    cur = {m.metric_id: m.value for m in metrics if m.period == "current"}
    ebitda = _latest_quarters(metrics, "ebitda")
    ours = {"pe": cur.get("pe"), "ev_sales": cur.get("ev_sales"), "ev_ebitda": cur.get("ev_ebitda"), "fcf_yield": cur.get("fcf_yield"),
            "gross_margin_ttm": _ttm_ratio(metrics, "gross_profit"), "operating_margin_ttm": _ttm_ratio(metrics, "operating_income"),
            "net_margin_ttm": _ttm_ratio(metrics, "net_income"), "sbc_to_revenue_ttm": _ttm_ratio(metrics, "sbc"),
            "net_debt_to_ebitda_ttm": (_latest_q(metrics, "net_debt") / sum(ebitda)) if ebitda and sum(ebitda) and _latest_q(metrics, "net_debt") is not None else None,
            "current_ratio": _latest_q(metrics, "current_ratio")}
    rows = []
    for mid, key in FMP_TTM.items():
        mine, theirs = ours.get(mid), ttm.get(key)
        diff = (mine / theirs - 1) if mine is not None and theirs not in (None, 0) else None
        rows.append({"metric": mid, "ours": mine, "fmp": theirs, "diff": diff, "ok": None if diff is None else abs(diff) <= tolerance})
    return rows


def format_crosscheck(rows: list[dict]) -> str:
    def f(v):
        return "" if v is None else f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"
    table = [(r["metric"], f(r["ours"]), f(r["fmp"]), "" if r["diff"] is None else f"{r['diff']:+.1%}" + ("" if r["ok"] else "  <-- CHECK")) for r in rows]
    return pd.DataFrame(table, columns=["metric", "fundabot", "fmp_ttm", "diff"]).to_string(index=False)


def crosscheck_edgar(fmp_series: dict, edgar_series: dict, tolerance: float = 0.02, fys: int = 5, quarters: int = 8) -> list[dict]:
    """FMP's statement lines against EDGAR's filed values for every shared (item, period) in the last `fys` fiscal years
    and `quarters` quarters. Report only."""
    rows = []
    for item in sorted(set(fmp_series) & set(edgar_series)):
        f, e = fmp_series[item], edgar_series[item]
        if f is None or e is None or f.empty or e.empty:
            continue
        latest_fy = int(max(f.fy.max(), e.fy.max()))
        fy_keep = set(range(latest_fy - fys + 1, latest_fy + 1))
        q_keep = set(zip(*[list(x) for x in zip(*sorted(((r.fy, r.fq) for r in e.itertuples(index=False) if r.fq >= 1), key=lambda t: (t[0], t[1]))[-quarters:])])) if (e.fq >= 1).any() else set()
        q_keep = {(fy, fq) for fy, fq in q_keep} if q_keep else set()
        fi = {(r.fy, r.fq): float(r.value) for r in f.itertuples(index=False)}
        for r in e.itertuples(index=False):
            key = (r.fy, r.fq)
            if key not in fi or not ((r.fq == 0 and r.fy in fy_keep) or (r.fq >= 1 and key in q_keep)):
                continue
            ev, fv = float(r.value), fi[key]
            diff = (fv / ev - 1) if ev else None
            rows.append({"item": item, "period": r.period, "fmp": fv, "edgar": ev, "diff": diff, "ok": None if diff is None else abs(diff) <= tolerance})
    return rows


def format_edgar_check(rows: list[dict]) -> str:
    table = [(r["item"], r["period"], f"{r['fmp']:,.0f}" if abs(r["fmp"]) >= 1000 else f"{r['fmp']:,.4f}", f"{r['edgar']:,.0f}" if abs(r["edgar"]) >= 1000 else f"{r['edgar']:,.4f}",
              "" if r["diff"] is None else f"{r['diff']:+.1%}" + ("" if r["ok"] else "  <-- CHECK")) for r in rows]
    return pd.DataFrame(table, columns=["item", "period", "fmp", "edgar", "diff"]).to_string(index=False)
