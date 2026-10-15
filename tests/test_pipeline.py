import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.ingest.edgar_facts import normalize
from fundabot.orchestrate import pipeline
from fundabot.retrieval.embed import FakeEmbedder

FIX = Path(__file__).parent / "fixtures"
PERSONAS = ["bull", "bear", "skeptic", "quality", "macro"]


def fake_output(system, user):
    """Cites a real metric and a real chunk from the inputs; includes one hallucinated number."""
    chunk_id = next(l.split()[1] for l in user.splitlines() if l.startswith("--- chunk:"))
    return {"thesis": "Revenue of $999 billion is large.",
            "claims": [{"text": "Gross margin is high.", "evidence": ["metric:gross_margin@FY2024"], "direction": "positive", "weight": 4},
                       {"text": "Risk factors mention competition.", "evidence": [chunk_id], "direction": "negative", "weight": 2},
                       {"text": "Made up.", "evidence": ["metric:nonexistent@FY2024"], "direction": "positive", "weight": 1}],
            "key_risks": ["China"], "what_would_change_my_mind": ["Margin compression"], "score": 5, "confidence": 0.8}


@pytest.fixture
def fixture_prepare(monkeypatch, db_conn):
    """prepare() wired to fixtures: AAPL facts, two saved sections, a hand-built price series, no network."""
    from fundabot.ingest import edgar_facts, edgar_filings, prices

    payload = json.loads((FIX / "AAPL_companyfacts.json").read_text())
    monkeypatch.setattr(edgar_facts, "ticker_to_cik", lambda t: 320193)
    monkeypatch.setattr(edgar_facts, "fetch_companyfacts", lambda cik: payload)
    px = pd.DataFrame({"date": [date(2026, 10, 14)], "close": [340.0]})
    monkeypatch.setattr(prices, "fetch_daily_closes", lambda t, years=5: px)
    monkeypatch.setattr(prices, "fetch_splits", lambda t: pd.DataFrame({"date": [date(2020, 8, 31)], "ratio": [4.0]}))
    filings = pd.DataFrame([("0000320193-25-000079", "10-K", date(2025, 10, 31), date(2025, 9, 27), "")],
                           columns=["accession", "form", "filed", "period_end", "primary_doc_url"])
    filings.attrs["name"] = "Apple Inc."
    monkeypatch.setattr(edgar_filings, "list_filings", lambda cik: filings)
    monkeypatch.setattr(edgar_filings, "extract_items", lambda acc, cik, form, filed, company="": (
        {"item1a": (FIX / "sections/aapl_10k_item1a.txt").read_text(), "item7": (FIX / "sections/aapl_10k_item7.txt").read_text()}, True))
    return db_conn


@pytest.mark.db
async def test_end_to_end_report_written(fixture_prepare, tmp_path):
    conn = fixture_prepare
    adapter = FakeAdapter(fake_output)
    html = await pipeline.analyze(conn, "AAPL", PERSONAS, adapter, FakeEmbedder(), out_dir=tmp_path)
    md = html.with_suffix(".md")
    assert html.exists() and md.exists() and html.parent.name == "AAPL"
    text = md.read_text()
    assert "metric:gross_margin@FY2024" in text and all(p in text for p in PERSONAS)
    assert "unweighted mean" in text and "$999 billion" in text and "dropped" in text.lower()
    assert "chunk:0000320193-25-000079#item1a-0" in text
    assert adapter.calls == 5


@pytest.mark.db
async def test_rerun_same_snapshot_no_model_calls(fixture_prepare, tmp_path):
    conn = fixture_prepare
    adapter = FakeAdapter(fake_output)
    await pipeline.analyze(conn, "AAPL", PERSONAS, adapter, FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM model_calls WHERE persona <> 'embed'")
        n1 = cur.fetchone()[0]
    await pipeline.analyze(conn, "AAPL", PERSONAS, adapter, FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM model_calls WHERE persona <> 'embed'")
        n2 = cur.fetchone()[0]
    assert adapter.calls == 5 and n1 == n2 == 5


@pytest.mark.db
async def test_rerun_flag_calls_again(fixture_prepare, tmp_path):
    conn = fixture_prepare
    adapter = FakeAdapter(fake_output)
    await pipeline.analyze(conn, "AAPL", PERSONAS, adapter, FakeEmbedder(), out_dir=tmp_path)
    await pipeline.analyze(conn, "AAPL", PERSONAS, adapter, FakeEmbedder(), rerun=True, out_dir=tmp_path)
    assert adapter.calls == 10
