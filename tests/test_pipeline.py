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
    chunk_id = next(l.split()[1] for l in (system + "\n" + user).splitlines() if l.startswith("--- chunk:"))
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
    filings = pd.DataFrame([("0000320193-25-000079", "10-K", date(2025, 10, 31), date(2025, 9, 27), "", "")],
                           columns=["accession", "form", "filed", "period_end", "primary_doc_url", "items"])
    filings.attrs["name"] = "Apple Inc."
    monkeypatch.setattr(edgar_filings, "list_filings", lambda cik: filings)
    from fundabot.ingest import fmp
    monkeypatch.setattr(fmp, "fetch_transcript_dates", lambda t: [])  # no network; test_transcripts overrides
    from fundabot.ingest import fmp_extra
    for name in ("ingest_profile", "ingest_peers", "ingest_segments", "ingest_dividends", "ingest_volume", "ingest_insider_trades", "ingest_holders"):
        monkeypatch.setattr(fmp_extra, name, lambda *a, **k: 0)  # FMP breadth: no network in tests
    monkeypatch.setattr(fmp_extra, "fetch_scores", lambda *a, **k: None)
    monkeypatch.setattr(fmp_extra, "fetch_treasury_10y", lambda *a, **k: None)
    monkeypatch.setattr(fmp_extra, "fetch_ttm", lambda *a, **k: {})
    import json as _json
    from datetime import date as _date

    def fixture_statements(ticker, get=None, **k):  # Phase 5d: FMP statements from fixtures; tests override to {} for the EDGAR-only path
        out = {}
        for key in ("income", "balance", "cashflow"):
            for pt in ("annual", "quarter"):
                out[f"{key}_{pt}"] = _json.loads((FIX / f"fmp_aapl_{key}_{pt}.json").read_text())
        return out
    monkeypatch.setattr(fmp_extra, "fetch_statements", fixture_statements)
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


@pytest.mark.db
async def test_report_contains_charts(fixture_prepare, tmp_path):
    conn = fixture_prepare
    html = await pipeline.analyze(conn, "AAPL", PERSONAS, FakeAdapter(fake_output), FakeEmbedder(), out_dir=tmp_path)
    text = html.read_text()
    assert "## Charts" in html.with_suffix(".md").read_text() or "Charts" in text
    assert text.count("<svg") >= 4 and "Revenue and free cash flow" in text and "Margins and return on capital" in text and "Valuation against its own 5-year range" in text
    assert ".viz-root" in text and "prefers-color-scheme: dark" in text


@pytest.mark.db
def test_prepare_uses_the_more_reliable_source(fixture_prepare):
    from fundabot.orchestrate import snapshot

    conn = fixture_prepare
    sid = pipeline.prepare(conn, "AAPL")
    ms = {f"{m.metric_id}@{m.period}": m for m in snapshot.load_metrics(conn, sid)}
    rev = ms["revenue@FY2025"]
    assert rev.formula_id.startswith("xbrl:") and rev.inputs["source_note"] == "confirmed:fmp"  # filed value, confirmed by FMP
    q4 = ms["revenue@Q4FY2025"]
    assert q4.formula_id == "fmp:income:revenue" and "derived" not in q4.flags  # reported quarter beats the derived one
    assert "interest_expense@FY2025" not in ms or ms["interest_expense@FY2025"].value is None  # FMP's zero is not a value
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM ticker_facts WHERE cik = 320193 AND key = 'edgar_crosscheck'")
        cc = cur.fetchone()[0]
    assert cc["total"] > 100 and cc["ok"] / cc["total"] > 0.85 and all({"item", "period", "fmp", "edgar", "diff", "ok"} <= set(r) for r in cc["rows"])
    bad = {(r["item"], r["period"]) for r in cc["rows"] if not r["ok"]}
    assert ("short_term_debt", "FY2025") in bad and ("cash", "Q2FY2026") in bad  # FMP's commercial-paper definition; one FMP cash figure off the filed value
    std = ms["short_term_debt@FY2025"]
    assert std.formula_id.startswith("xbrl:") and "source_conflict" in std.flags and std.inputs["source_note"].startswith("conflict:fmp=")  # filed value kept, conflict shown
    assert ms["cash@Q2FY2026"].value == pytest.approx(45.572e9, rel=1e-3) and "source_conflict" in ms["cash@Q2FY2026"].flags


@pytest.mark.db
def test_prepare_without_fmp_statements_is_edgar_only(fixture_prepare, monkeypatch):
    from fundabot.ingest import fmp_extra
    from fundabot.orchestrate import snapshot

    conn = fixture_prepare
    monkeypatch.setattr(fmp_extra, "fetch_statements", lambda *a, **k: {})
    sid = pipeline.prepare(conn, "AAPL")
    base = [m for m in snapshot.load_metrics(conn, sid) if m.formula_id.startswith(("xbrl:", "fmp:income:", "fmp:balance:", "fmp:cashflow:"))]
    assert base and all(m.formula_id.startswith("xbrl:") for m in base)
