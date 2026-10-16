import json
from pathlib import Path

import pytest

from fundabot.ingest import fmp, transcripts
from tests.test_pipeline import fixture_prepare  # noqa: F401

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_fmp(monkeypatch):
    dates = json.loads((FIX / "fmp_aapl_transcript_dates.json").read_text())
    content = json.loads((FIX / "fmp_aapl_transcript_2026q3.json").read_text())[0]
    calls = []

    def fetch_transcript(ticker, year, quarter):
        calls.append((year, quarter))
        return {**content, "year": year, "period": f"Q{quarter}", "content": f"[{year} Q{quarter}] " + content["content"]}
    monkeypatch.setattr(fmp, "fetch_transcript_dates", lambda t: dates)
    monkeypatch.setattr(fmp, "fetch_transcript", fetch_transcript)
    return calls


def test_key():
    assert transcripts.key(2026, 3) == "T2026Q3"


@pytest.mark.db
def test_transcript_key_uses_fmp_fiscal_fields(db_conn, fake_fmp):
    keys = transcripts.ingest(db_conn, 320193, "AAPL", n=4)
    assert keys == ["T2026Q3", "T2026Q2", "T2026Q1", "T2025Q4"]
    with db_conn.cursor() as cur:
        cur.execute("SELECT accession, form, filed FROM filings WHERE form = 'TRANSCRIPT' ORDER BY filed DESC")
        rows = cur.fetchall()
        assert [r[0] for r in rows] == keys and str(rows[0][2]) == "2026-07-30"
        cur.execute("SELECT accession, item, left(text, 30) FROM filing_sections WHERE accession = 'T2026Q3'")
        acc, item, text = cur.fetchone()
        assert item == "call" and text.startswith("[2026 Q3] Suhasini")
        cur.execute("SELECT fy, fq, call_date FROM transcripts WHERE cik = 320193 ORDER BY fy DESC, fq DESC LIMIT 1")
        assert cur.fetchone() == (2026, 3, __import__("datetime").date(2026, 7, 30))


@pytest.mark.db
def test_ingest_idempotent(db_conn, fake_fmp):
    transcripts.ingest(db_conn, 320193, "AAPL", n=4)
    n = len(fake_fmp)
    assert transcripts.ingest(db_conn, 320193, "AAPL", n=4) == ["T2026Q3", "T2026Q2", "T2026Q1", "T2025Q4"] and len(fake_fmp) == n == 4


@pytest.mark.db
def test_prepare_includes_transcript_keys(fixture_prepare, fake_fmp, monkeypatch):
    from fundabot.orchestrate import pipeline, snapshot
    from fundabot.retrieval.embed import FakeEmbedder
    from fundabot.retrieval.query import embed_missing, retrieve

    monkeypatch.setattr(fmp, "fetch_all", lambda t: {k: json.loads((FIX / f"fmp_aapl_{k}.json").read_text()) for k in ("estimates_annual", "earnings", "price_target")})
    conn = fixture_prepare
    sid = pipeline.prepare(conn, "AAPL")
    s = snapshot.load(conn, sid)
    assert "T2026Q3" in s["accessions"] and "0000320193-25-000079" in s["accessions"]
    embed_missing(conn, FakeEmbedder(), s["accessions"])
    got = retrieve(conn, FakeEmbedder(), ["T2026Q3"], ["guidance for next quarter"], top_k=3)
    assert got and all(c.id.startswith("T2026Q3#call-") for c in got)


def test_base_and_persona_queries_mention_transcripts():
    from fundabot.agents import context, personas

    assert any("guidance" in q for q in context.BASE_QUERIES)
    for p in personas.load_all().values():
        assert any(("call" in q) or ("management" in q) for q in p["retrieval_queries"]), p["name"]
