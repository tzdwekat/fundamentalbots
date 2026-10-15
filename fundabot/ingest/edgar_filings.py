"""EDGAR submissions list and filing selection. Item extraction is added in the retrieval phase."""
from datetime import date

import pandas as pd

from fundabot.ingest.edgar_client import get_json

COLUMNS = ["accession", "form", "filed", "period_end", "primary_doc_url"]
ANNUAL_FORMS = {"10-K", "20-F", "40-F"}


class UnsupportedFiler(Exception):
    pass


def parse_submissions(payload: dict) -> pd.DataFrame:
    cik = int(payload["cik"])
    r = payload["filings"]["recent"]
    rows = []
    for acc, form, filed, report, doc in zip(r["accessionNumber"], r["form"], r["filingDate"], r["reportDate"], r["primaryDocument"]):
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}" if doc else None
        rows.append((acc, form, date.fromisoformat(filed), date.fromisoformat(report) if report else None, url))
    return pd.DataFrame(rows, columns=COLUMNS)


def list_filings(cik: int) -> pd.DataFrame:
    return parse_submissions(get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))


def select_for_snapshot(filings: pd.DataFrame) -> list[str]:
    """Last 5 original 10-Ks then last 8 original 10-Qs, newest first. Rejects foreign private issuers."""
    annual = filings[filings.form.isin(ANNUAL_FORMS)].sort_values("filed")
    if not annual.empty and annual.form.iloc[-1] != "10-K":
        raise UnsupportedFiler(f"latest annual filing is a {annual.form.iloc[-1]}: foreign private issuer not supported")
    ks = filings[filings.form == "10-K"].sort_values("filed", ascending=False).head(5)
    qs = filings[filings.form == "10-Q"].sort_values("filed", ascending=False).head(8)
    return list(ks.accession) + list(qs.accession)
