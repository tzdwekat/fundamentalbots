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
