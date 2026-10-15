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
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.attrs["name"] = payload.get("name", "")
    return df


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


# ---- Filing text: Item extraction via edgartools ---------------------------------------------

import logging
import re

import psycopg

from fundabot.config import settings

log = logging.getLogger("fundabot")

WANTED = {
    "10-K": ["Item 1", "Item 1A", "Item 1C", "Item 3", "Item 7", "Item 7A", "Item 8", "Item 9A"],
    "10-Q": ["Part I, Item 2", "Part I, Item 3", "Part I, Item 4", "Part II, Item 1", "Part II, Item 1A"],
}


def normalize_item_key(key: str) -> str:
    """'Item 1A' -> 'item1a'; 'Part II, Item 1A' -> 'p2item1a'."""
    m = re.match(r"(?:Part\s+(I{1,3}),?\s*)?Item\s+(\w+)", key, re.I)
    if not m:
        return re.sub(r"\W+", "", key).lower()
    part = {"I": "p1", "II": "p2", "III": "p3"}.get((m.group(1) or "").upper(), "")
    return f"{part}item{m.group(2).lower()}"


def wanted_items(form: str, available: list[str]) -> dict[str, str]:
    want = WANTED.get(form.split("/")[0], [])
    return {k: normalize_item_key(k) for k in available if k in want}


def extract_items(accession: str, cik: int, form: str, filed: date, company: str = "") -> tuple[dict[str, str], bool]:
    """Item text per normalized key. On failure returns ({'full': text}, False)."""
    from edgar import Filing, set_identity

    set_identity(settings.edgar_user_agent)
    filing = Filing(form=form, filing_date=str(filed), company=company, cik=cik, accession_no=accession)
    try:
        obj = filing.obj()
        keys = wanted_items(form, list(obj.items))
        out = {norm: obj[k] for k, norm in keys.items() if obj[k]}
        if out:
            return out, True
    except Exception as e:  # edgartools parsing is best-effort across filers
        log.warning("item extraction failed for %s: %s", accession, e)
    return {"full": filing.text()}, False


def ingest(conn: psycopg.Connection, cik: int, filings: pd.DataFrame, accessions: list[str], company: str = "") -> None:
    """Store filing rows and Item text for the accessions not yet in filing_sections."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT accession FROM filing_sections WHERE accession = ANY(%s)", (accessions,))
        done = {r[0] for r in cur.fetchall()}
    for f in filings[filings.accession.isin(accessions)].itertuples(index=False):
        if f.accession in done:
            continue
        sections, ok = extract_items(f.accession, cik, f.form, f.filed, company)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO filings (accession, cik, form, filed, period_end, primary_doc_url, items_ok) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (accession) DO UPDATE SET items_ok = EXCLUDED.items_ok",
                        (f.accession, cik, f.form, f.filed, f.period_end, f.primary_doc_url, ok))
            cur.executemany("INSERT INTO filing_sections (accession, item, text) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            [(f.accession, item, text) for item, text in sections.items()])
        conn.commit()


def load_sections(conn: psycopg.Connection, accession: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT item, text FROM filing_sections WHERE accession = %s", (accession,))
        return dict(cur.fetchall())
