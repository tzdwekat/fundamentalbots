import json
from pathlib import Path

import pytest

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.analytics import guidance
from fundabot.analytics.metrics import Metric
from fundabot.retrieval.chunk import Chunk, chunk_section

FIX = Path(__file__).parent / "fixtures"


def chunks():
    text = json.loads((FIX / "fmp_aapl_transcript_2026q3.json").read_text())[0]["content"]
    return chunk_section("T2026Q3", "call", text)


def output(statements, tone="confident", tone_quote=None, tone_chunk=None):
    return {"statements": statements, "tone": tone, "tone_quote": tone_quote or "", "tone_chunk": tone_chunk or ""}


def test_verify_keeps_verbatim_quotes_only():
    cs = chunks()
    by_id = {c.id: c for c in cs}
    quote = cs[0].text[100:160]
    good = {"metric": "revenue", "period": "Q4 FY2026", "direction": "raised", "quote": quote, "chunk": f"chunk:{cs[0].id}"}
    out = guidance.verify(output([good], tone="confident", tone_quote=cs[0].text[10:40], tone_chunk=f"chunk:{cs[0].id}"), by_id)
    assert out["statements"] == [good] and out["tone"] == "confident" and out["dropped"] == 0


def test_paraphrased_quote_dropped():
    cs = chunks()
    by_id = {c.id: c for c in cs}
    para = {"metric": "revenue", "period": "Q4", "direction": "raised", "quote": "we expect revenue to grow strongly next quarter", "chunk": f"chunk:{cs[0].id}"}
    wrong_chunk = {"metric": "eps", "period": "Q4", "direction": "raised", "quote": cs[0].text[100:140], "chunk": "chunk:T2026Q3#call-99"}
    out = guidance.verify(output([para, wrong_chunk], tone="cautious", tone_quote="not in text", tone_chunk=f"chunk:{cs[0].id}"), by_id)
    assert out["statements"] == [] and out["dropped"] == 3 and out["tone"] is None  # two statements and the tone quote


def test_guidance_metrics_direction_and_tone():
    rows = [{"key": "T2026Q3", "statements": [
        {"metric": "revenue", "period": "Q4", "direction": "raised", "quote": "q1", "chunk": "chunk:T2026Q3#call-1"},
        {"metric": "eps", "period": "Q4", "direction": "lowered", "quote": "q2", "chunk": "chunk:T2026Q3#call-2"}],
        "tone": "cautious", "tone_quote": "tq", "tone_chunk": "chunk:T2026Q3#call-0"},
        {"key": "T2026Q2", "statements": [], "tone": None, "tone_quote": None, "tone_chunk": None}]
    ms = guidance.metrics(rows)
    g = {(m.metric_id, m.period): m for m in ms}
    d = g[("guidance_direction", "Q3FY2026")]
    assert d.value == 1 and "llm_extracted" in d.flags and d.inputs["quotes"] == ["q1", "q2"] and d.inputs["chunks"] == ["chunk:T2026Q3#call-1", "chunk:T2026Q3#call-2"]
    assert g[("management_tone", "Q3FY2026")].value == -1
    assert g[("guidance_direction", "Q2FY2026")].value is None and g[("guidance_direction", "Q2FY2026")].null_reason == "no_guidance"
    assert g[("management_tone", "Q2FY2026")].null_reason == "no_guidance"


def test_guidance_metric_period_uses_transcript_key():
    assert guidance.label_for_key("T2027Q2") == "Q2FY2027"
    m = guidance.metrics([{"key": "T2027Q2", "statements": [{"metric": "margin", "period": "", "direction": "maintained", "quote": "q", "chunk": "c"}], "tone": "neutral", "tone_quote": "t", "tone_chunk": "c"}])
    assert {x.period for x in m} == {"Q2FY2027"} and "FMP fiscal" in m[0].formula_id


@pytest.mark.db
async def test_extract_caches(db_conn):
    cs = chunks()
    adapter = FakeAdapter([output([{"metric": "revenue", "period": "Q4", "direction": "raised", "quote": cs[0].text[100:160], "chunk": f"chunk:{cs[0].id}"}],
                                  tone="confident", tone_quote=cs[0].text[10:40], tone_chunk=f"chunk:{cs[0].id}")])
    row = await guidance.extract(db_conn, 320193, "T2026Q3", adapter, cs)
    assert row["statements"][0]["direction"] == "raised" and row["tone"] == "confident" and adapter.calls == 1
    row2 = await guidance.extract(db_conn, 320193, "T2026Q3", adapter, cs)
    assert row2["statements"] == row["statements"] and adapter.calls == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT persona FROM model_calls")
        assert cur.fetchone()[0] == "guidance:T2026Q3"


def test_judge_message_includes_llm_extracted():
    from fundabot.debate import judge

    ms = [Metric("guidance_direction", "Q3FY2026", 1.0, "ratio", "x", {}, ["llm_extracted"]), Metric("revenue", "FY2024", 1.0, "USD", "x")]
    msg = judge.build_message([], [], ms, earnings_available=True, evidence={}, revised={})
    assert "guidance_direction" in msg


@pytest.mark.db
async def test_ensure_guidance_adds_metrics_to_snapshot(db_conn, monkeypatch):
    from fundabot.orchestrate import pipeline, snapshot

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["0000320193-25-000079", "T2026Q3"], None, None, None)
    cs = chunks()
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO filing_sections (accession, item, text) VALUES ('T2026Q3', 'call', %s)", (" ".join(c.text for c in cs),))
    db_conn.commit()
    adapter = FakeAdapter([output([{"metric": "revenue", "period": "Q4", "direction": "lowered", "quote": cs[0].text[100:160], "chunk": f"chunk:{cs[0].id}"}],
                                  tone="cautious", tone_quote=cs[0].text[10:40], tone_chunk=f"chunk:{cs[0].id}")])
    assert await pipeline.ensure_guidance(db_conn, sid, adapter) == 1
    ms = {(m.metric_id, m.period): m for m in snapshot.load_metrics(db_conn, sid)}
    assert ms[("guidance_direction", "Q3FY2026")].value == -1 and ms[("management_tone", "Q3FY2026")].value == -1
    assert await pipeline.ensure_guidance(db_conn, sid, adapter) == 0 and adapter.calls == 1


@pytest.mark.db
async def test_analyze_runs_guidance_extraction(db_conn, monkeypatch, tmp_path):
    """The cheap run must extract guidance for the snapshot's transcripts when given a guidance adapter."""
    from fundabot.orchestrate import pipeline
    from fundabot.retrieval.embed import FakeEmbedder
    from tests.test_pipeline import FIX as PFIX, fake_output
    from tests.test_pipeline import fixture_prepare as _fp  # noqa: F401
    import json
    from datetime import date
    import pandas as pd
    from fundabot.ingest import edgar_facts, edgar_filings, fmp, prices

    payload = json.loads((PFIX / "AAPL_companyfacts.json").read_text())
    monkeypatch.setattr(edgar_facts, "ticker_to_cik", lambda t: 320193)
    monkeypatch.setattr(edgar_facts, "fetch_companyfacts", lambda cik: payload)
    monkeypatch.setattr(prices, "fetch_daily_closes", lambda t, years=5: pd.DataFrame({"date": [date(2026, 10, 14)], "close": [340.0]}))
    monkeypatch.setattr(prices, "fetch_splits", lambda t: pd.DataFrame(columns=["date", "ratio"]))
    filings = pd.DataFrame([("0000320193-25-000079", "10-K", date(2025, 10, 31), date(2025, 9, 27), "", "")], columns=["accession", "form", "filed", "period_end", "primary_doc_url", "items"])
    filings.attrs["name"] = "Apple Inc."
    monkeypatch.setattr(edgar_filings, "list_filings", lambda cik: filings)
    monkeypatch.setattr(edgar_filings, "extract_items", lambda acc, cik, form, filed, company="": ({"item1a": (PFIX / "sections/aapl_10k_item1a.txt").read_text()}, True))
    monkeypatch.setattr(fmp, "fetch_all", lambda t: {k: json.loads((PFIX / f"fmp_aapl_{k}.json").read_text()) for k in ("estimates_annual", "earnings", "price_target")})
    monkeypatch.setattr(fmp, "fetch_transcript_dates", lambda t: [{"quarter": 3, "fiscalYear": 2026, "date": "2026-07-30"}])
    monkeypatch.setattr(fmp, "fetch_transcript", lambda t, y, q: json.loads((PFIX / "fmp_aapl_transcript_2026q3.json").read_text())[0])
    cs = chunks()
    gadapter = FakeAdapter([output([{"metric": "revenue", "period": "Q4", "direction": "raised", "quote": cs[0].text[100:160], "chunk": f"chunk:{cs[0].id}"}],
                                   tone="confident", tone_quote=cs[0].text[10:40], tone_chunk=f"chunk:{cs[0].id}")])
    await pipeline.analyze(db_conn, "AAPL", ["bull"], FakeAdapter(fake_output), FakeEmbedder(), out_dir=tmp_path, guidance_adapter=gadapter)
    assert gadapter.calls == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT value FROM metrics WHERE metric_id = 'guidance_direction' AND period = 'Q3FY2026'")
        assert cur.fetchone()[0] == 1


@pytest.mark.db
async def test_ensure_guidance_skips_failed_transcripts(db_conn):
    from fundabot.orchestrate import pipeline, snapshot

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["T2026Q3", "T2026Q2"], None, None, None)
    cs = chunks()
    with db_conn.cursor() as cur:
        for k in ("T2026Q3", "T2026Q2"):
            cur.execute("INSERT INTO filing_sections (accession, item, text) VALUES (%s, 'call', %s)", (k, " ".join(c.text for c in cs)))
    db_conn.commit()

    class Flaky(FakeAdapter):
        async def complete(self, system, user, schema):
            if "T2026Q3" in user:
                self.calls += 1
                raise RuntimeError("output truncated at max_tokens")
            return await super().complete(system, user, schema)  # counts the call itself
    adapter = Flaky([output([], tone="neutral", tone_quote=cs[0].text[10:40], tone_chunk=f"chunk:{cs[0].id}")])
    assert await pipeline.ensure_guidance(db_conn, sid, adapter) == 1  # the other transcript still gets extracted; the run continues
    assert adapter.calls == 2
