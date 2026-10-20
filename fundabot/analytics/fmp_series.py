"""FMP standardised statements -> the per-item frames periods.build_series makes from XBRL, so the metric formulas run
unchanged on either source. FMP is primary; merge_series appends the EDGAR rows for periods and items FMP lacks.
An FMP zero is a missing line (FMP writes 0 for lines a filer does not report), never a value."""
from datetime import date, timedelta

import pandas as pd

from fundabot.analytics.periods import COLUMNS

# item -> (statement, field, take absolute value)
FIELD_MAP: dict[str, tuple[str, str, bool]] = {
    "revenue": ("income", "revenue", False), "cost_of_revenue": ("income", "costOfRevenue", False), "gross_profit": ("income", "grossProfit", False),
    "operating_income": ("income", "operatingIncome", False), "pretax_income": ("income", "incomeBeforeTax", False), "income_tax": ("income", "incomeTaxExpense", False),
    "net_income": ("income", "netIncome", False), "eps_diluted": ("income", "epsDiluted", False), "interest_expense": ("income", "interestExpense", False),
    "da": ("income", "depreciationAndAmortization", False), "diluted_shares": ("income", "weightedAverageShsOutDil", False),
    "ocf": ("cashflow", "operatingCashFlow", False), "capex": ("cashflow", "capitalExpenditure", True), "sbc": ("cashflow", "stockBasedCompensation", False),
    "dividends_paid": ("cashflow", "commonDividendsPaid", True), "buybacks": ("cashflow", "commonStockRepurchased", True),
    "share_issuance": ("cashflow", "commonStockIssuance", False), "acquisitions": ("cashflow", "acquisitionsNet", True),
    "total_assets": ("balance", "totalAssets", False), "total_liabilities": ("balance", "totalLiabilities", False), "equity": ("balance", "totalStockholdersEquity", False),
    "cash": ("balance", "cashAndCashEquivalents", False), "short_term_debt": ("balance", "shortTermDebt", False), "long_term_debt": ("balance", "longTermDebt", False),
    "total_debt_reported": ("balance", "totalDebt", False), "goodwill": ("balance", "goodwill", False), "intangibles": ("balance", "intangibleAssets", False),
    "current_assets": ("balance", "totalCurrentAssets", False), "current_liabilities": ("balance", "totalCurrentLiabilities", False),
}
DA_FALLBACK = ("cashflow", "depreciationAndAmortization")
SPLIT_ADJUSTED = {"eps_diluted", "diluted_shares"}  # FMP reports these split-adjusted
MATCH_DAYS = 3


def _accession(filings: pd.DataFrame | None, filing_date: date | None, annual: bool) -> str:
    if filings is None or filings.empty or filing_date is None:
        return f"fmp:{filing_date}" if filing_date else "fmp:unknown"
    form = "10-K" if annual else "10-Q"
    near = filings[(filings.form == form) & ((pd.to_datetime(filings.filed).dt.date - filing_date).abs() <= timedelta(days=MATCH_DAYS))]
    return near.sort_values("filed").accession.iloc[-1] if not near.empty else f"fmp:{filing_date}"


def _rows(statements: dict, statement: str, field: str, absolute: bool, filings) -> list[dict]:
    out = []
    for period_type in ("annual", "quarter"):
        rows = sorted(statements.get(f"{statement}_{period_type}", []), key=lambda r: r["date"])
        prev_end = None
        for r in rows:
            v = r.get(field)
            fy, per = int(r["fiscal_year"]), str(r["period"])
            end = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"])[:10])
            fq = 0 if per == "FY" else int(per[1]) if per.startswith("Q") and per[1:].isdigit() else None
            if fq is None:
                continue
            start = (prev_end + timedelta(days=1)) if prev_end and (end - prev_end).days <= (400 if fq == 0 else 100) else end - timedelta(days=364 if fq == 0 else 91)
            prev_end = end
            try:
                v = float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                v = 0.0  # a non-numeric cell is a missing line too
            if v == 0.0:
                continue  # FMP writes 0 for lines the filer does not report: not a value
            fd = r.get("filing_date")
            fd = fd if isinstance(fd, date) or fd is None else date.fromisoformat(str(fd)[:10])
            out.append({"fy": fy, "fq": fq, "start": start, "end": end, "value": abs(float(v)) if absolute else float(v),
                        "accession": _accession(filings, fd, annual=(fq in (0, 4))), "restated": False, "derived": False})
    return out


def build_fmp_series(statements: dict[str, list[dict]], filings: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """One frame per item with periods.COLUMNS; items with no non-zero values are omitted."""
    series = {}
    for item, (statement, field, absolute) in FIELD_MAP.items():
        rows = _rows(statements, statement, field, absolute, filings)
        tag = f"fmp:{statement}:{field}"
        if item == "da" and not rows:
            rows, tag = _rows(statements, *DA_FALLBACK, False, filings), f"fmp:{DA_FALLBACK[0]}:{DA_FALLBACK[1]}"
        if not rows:
            continue
        df = pd.DataFrame(rows)
        if item == "diluted_shares":  # FMP's newest Q4 share count is the annual average copied over, not the quarter's
            fy_vals = {r["fy"]: r["value"] for r in rows if r["fq"] == 0}
            df = df[~((df.fq == 4) & (df.fy.map(fy_vals) == df.value))]
        df["tag"] = tag
        df["split_adjusted"] = item in SPLIT_ADJUSTED
        df["period"] = [f"FY{fy}" if fq == 0 else f"Q{fq}FY{fy}" for fy, fq in zip(df.fy, df.fq)]
        df = df.drop_duplicates(["fy", "fq"], keep="last").sort_values(["end", "fq"]).reset_index(drop=True)
        series[item] = df[COLUMNS]
    return series


TOLERANCE = 0.02  # same filed number from two sources: beyond this it is a definitional difference or an error, and the reader is told


def _note_value(v: float) -> str:
    return f"{v:.4g}" if abs(v) < 1e6 else f"{v:.6g}"


def merge_series(fmp: dict[str, pd.DataFrame], edgar: dict[str, pd.DataFrame], tolerance: float = TOLERANCE) -> dict[str, pd.DataFrame]:
    """The more reliable source per value: only one has it -> that one; EDGAR's row is derived (Q4 / year-to-date
    subtraction) -> FMP's reported figure (confirmed by EDGAR when they agree); both reported and agree -> the filed
    EDGAR value (confirmed by FMP); both reported and disagree -> EDGAR, flagged as a conflict with FMP's value attached.
    Notes live in frame.attrs["source_notes"]."""
    out = {}
    for item in set(fmp) | set(edgar):
        f, e = fmp.get(item), edgar.get(item)
        if f is None or f.empty:
            out[item] = e
            continue
        if e is None or e.empty:
            out[item] = f
            continue
        fi = {(r.fy, r.fq): r for r in f.itertuples(index=False)}
        ei = {(r.fy, r.fq): r for r in e.itertuples(index=False)}
        rows, notes = [], {}
        for key in set(fi) | set(ei):
            fr, er = fi.get(key), ei.get(key)
            if er is None:
                rows.append(fr)
            elif fr is None:
                rows.append(er)
            else:
                ev, fv = float(er.value), float(fr.value)
                agree = ev == fv or (ev != 0 and abs(fv - ev) / abs(ev) <= tolerance)
                if er.derived:  # a reported quarter beats one derived by subtraction, agreeing or not
                    rows.append(fr); notes[key] = "confirmed:edgar" if agree else f"replaces_derived:edgar={_note_value(ev)}"
                elif agree:
                    rows.append(er); notes[key] = "confirmed:fmp"
                else:
                    rows.append(er); notes[key] = f"conflict:fmp={_note_value(fv)}"
        merged = pd.DataFrame(rows, columns=COLUMNS).sort_values(["end", "fq"]).reset_index(drop=True)
        merged.attrs["source_notes"] = notes
        if e.attrs.get("tag_ambiguous"):
            merged.attrs["tag_ambiguous"] = True
        out[item] = merged
    return out
