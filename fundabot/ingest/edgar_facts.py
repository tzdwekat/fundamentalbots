from datetime import date

import pandas as pd
import psycopg
from psycopg.types.json import Jsonb

from fundabot.db.session import execute_values
from fundabot.ingest.edgar_client import get_json

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COLUMNS = ["taxonomy", "tag", "unit", "start_date", "end_date", "value", "fy", "fp", "form", "accession", "filed"]


class UnknownTicker(Exception):
    pass


def ticker_to_cik(ticker: str) -> int:
    ticker = ticker.upper().replace(".", "-")
    for row in get_json(TICKERS_URL).values():
        if row["ticker"] == ticker:
            return int(row["cik_str"])
    raise UnknownTicker(ticker)


def fetch_companyfacts(cik: int) -> dict:
    return get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")


def _d(s):
    return date.fromisoformat(s) if s else None


def normalize(payload: dict) -> pd.DataFrame:
    rows = []
    for taxonomy, tags in payload.get("facts", {}).items():
        for tag, body in tags.items():
            for unit, facts in body.get("units", {}).items():
                for f in facts:
                    rows.append((
                        taxonomy, tag, unit, _d(f.get("start")), _d(f["end"]), float(f["val"]),
                        f.get("fy"), f.get("fp"), f.get("form"), f["accn"], _d(f.get("filed")),
                    ))
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["fy"] = df["fy"].astype("Int64")
    return df


def ingest(conn: psycopg.Connection, ticker: str) -> tuple[int, int]:
    """Fetch companyfacts, store the raw payload, upsert normalized facts. Returns (cik, fetch_id)."""
    cik = ticker_to_cik(ticker)
    payload = fetch_companyfacts(cik)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO raw_companyfacts (cik, payload) VALUES (%s, %s) RETURNING id", (cik, Jsonb(payload)))
        fetch_id = cur.fetchone()[0]
    df = normalize(payload)
    rows = [
        (cik, r.taxonomy, r.tag, r.unit, r.start_date, r.end_date, r.value,
         None if pd.isna(r.fy) else int(r.fy), r.fp, r.form, r.accession, r.filed)
        for r in df.itertuples(index=False)
    ]
    execute_values(
        conn,
        "INSERT INTO facts (cik, taxonomy, tag, unit, start_date, end_date, value, fy, fp, form, accession, filed) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        rows,
    )
    return cik, fetch_id


def load_facts(conn: psycopg.Connection, cik: int) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(COLUMNS)} FROM facts WHERE cik = %s", (cik,))
        df = pd.DataFrame(cur.fetchall(), columns=COLUMNS)
    df["value"] = df["value"].astype(float)
    df["fy"] = df["fy"].astype("Int64")
    return df
