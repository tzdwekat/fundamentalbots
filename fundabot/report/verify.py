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
            rows.append((mid, m.period, f"{m.value:,.0f}", f"{float(tv):,.0f}", f"{diff:+.2%}" + ("  <-- CHECK" if abs(diff) > tolerance else "")))
    return pd.DataFrame(rows, columns=["metric", "period", "fundabot", "yfinance", "diff"]).to_string(index=False)


def _period_end(m: Metric):
    return date.fromisoformat(m.inputs["end"])
