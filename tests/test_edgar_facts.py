import json
from datetime import date
from pathlib import Path

import pytest

from fundabot.ingest import edgar_facts

FIX = Path(__file__).parent / "fixtures"


def load(ticker):
    return json.loads((FIX / f"{ticker}_companyfacts.json").read_text())


def test_normalize_columns_and_instants():
    df = edgar_facts.normalize(load("AAPL"))
    assert list(df.columns) == [
        "taxonomy", "tag", "unit", "start_date", "end_date", "value", "fy", "fp", "form", "accession", "filed",
    ]
    assets = df[df.tag == "Assets"]
    assert len(assets) > 0 and assets.start_date.isna().all()
    rev = df[df.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"]
    assert rev.start_date.notna().all()
    assert isinstance(rev.end_date.iloc[0], date)


def test_normalize_known_value():
    df = edgar_facts.normalize(load("AAPL"))
    row = df[
        (df.tag == "RevenueFromContractWithCustomerExcludingAssessedTax")
        & (df.end_date == date(2024, 9, 28))
        & (df.start_date == date(2023, 10, 1))
    ]
    assert set(row.value) == {391035000000}
    assert "dei" in set(df.taxonomy)


@pytest.mark.db
def test_ingest_upsert_idempotent(db_conn, monkeypatch):
    monkeypatch.setattr(edgar_facts, "fetch_companyfacts", lambda cik: load("AAPL"))
    monkeypatch.setattr(edgar_facts, "ticker_to_cik", lambda t: 320193)
    cik, fetch1 = edgar_facts.ingest(db_conn, "AAPL")
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM facts")
        n1 = cur.fetchone()[0]
    cik2, fetch2 = edgar_facts.ingest(db_conn, "AAPL")
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM facts")
        n2 = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM raw_companyfacts")
        raw = cur.fetchone()[0]
    assert cik == cik2 == 320193 and n1 == n2 > 1000 and raw == 2 and fetch2 > fetch1
