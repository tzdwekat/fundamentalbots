import json
import re
from pathlib import Path

import pytest

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.orchestrate import pipeline
from fundabot.retrieval.embed import FakeEmbedder
from tests.test_pipeline import FIX, PERSONAS, fixture_prepare  # noqa: F401  (fixture reused)


def persona_fake(provider):
    """Cites a real metric and the first chunk; bull/quality positive, others negative."""
    def out(system, user):
        persona = re.search(r"You are the (\w+) analyst", system).group(1)
        if "YOUR_CLAIMS" in user:  # rebuttal round: concede the strongest opposing claim
            opp = re.findall(r"^(\S+/\S+#\d+) by", user, re.M)
            return {"responses": [{"claim_id": opp[0], "verdict": "concede", "reason": "fair", "evidence": []}] if opp else [],
                    "withdraws": [], "revised_score": 4 if persona in ("bull", "quality") else -2, "revised_confidence": 0.6}
        chunk_id = next(l.split()[1] for l in (system + "\n" + user).splitlines() if l.startswith("--- chunk:"))
        positive = persona in ("bull", "quality")
        d = "positive" if positive else "negative"
        return {"thesis": f"{persona} thesis", "claims": [
            {"text": f"{persona} says margins", "evidence": ["metric:gross_margin@FY2024"], "direction": d, "weight": 4},
            {"text": f"{persona} cites filing", "evidence": [chunk_id], "direction": d, "weight": 2}],
            "key_risks": [], "what_would_change_my_mind": [], "score": 5 if positive else -3, "confidence": 0.6}
    a = FakeAdapter(out)
    a.provider, a.model = provider, f"{provider}-persona"
    return a


def rebuttal_or_judge(provider):
    def out(system, user):
        if "YOUR_CLAIMS" in user:  # rebuttal
            opp = re.findall(r"^(\S+/\S+#\d+) by", user, re.M)
            return {"responses": [{"claim_id": opp[0], "verdict": "concede", "reason": "fair", "evidence": []}] if opp else [],
                    "withdraws": [], "revised_score": 2, "revised_confidence": 0.6}
        ids = re.findall(r"^(\S+/\S+#\d+) by", user.split("SCREEN_PANEL_SCORES")[0], re.M)  # the CLAIMS block only, never UNANIMOUS_CLAIMS
        return {"claims": [{"claim_id": i, "evidence_strength": 4, "materiality": 4 if "bear" in i else 3, "note": ""} for i in dict.fromkeys(ids)],
                "quality_score": 70, "value_score": 40, "earnings_momentum_score": 60, "accounting_risk_score": 20,
                "catalysts": [{"what": "new products", "when": "FY2027", "evidence": ["metric:revenue@FY2024"]}],
                "one_paragraph_summary": f"{provider} judge summary"}
    a = FakeAdapter(out)
    a.provider, a.model = provider, f"{provider}-judge"
    return a


def screen_fake(provider):
    a = rebuttal_or_judge(provider)
    a.model = f"{provider}-screen"
    return a


PROVIDERS = ("anthropic", "openai", "gemini")


@pytest.fixture
def fmp_fixture(monkeypatch):
    from fundabot.ingest import fmp
    monkeypatch.setattr(fmp, "fetch_all", lambda t: {k: json.loads((FIX / f"fmp_aapl_{k}.json").read_text()) for k in ("estimates_annual", "earnings", "price_target")})


def calls(adapters):
    return sum(a.calls for a in adapters)


@pytest.mark.db
async def test_full_run_call_counts_and_verdict(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake(p) for p in PROVIDERS]
    screen, judges = [screen_fake(p) for p in PROVIDERS], [rebuttal_or_judge(p) for p in PROVIDERS]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) == 15 + 15 and calls(screen) == 3 and calls(judges) == 3  # personas also answer their own rebuttals; one round per panel
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rebuttals"); assert cur.fetchone()[0] == 15
        cur.execute("SELECT count(*) FROM judgements"); assert cur.fetchone()[0] == 6
        cur.execute("SELECT verdict FROM verdicts WHERE run_kind = 'full'")
        v = cur.fetchone()[0]
    assert v["weighting"] == "evidence-weighted" and v["judge_agreement"] == 1.0 and any(c["conceded_by"] for c in v["consensus_claims"] + v["contested_claims"])
    for k in ("composite_score", "dispersion", "judge_agreement", "grid", "consensus_claims", "contested_claims", "one_paragraph_summary", "earnings_momentum_score"):
        assert k in v
    assert len(v["grid"]) == 15 and v["one_paragraph_summary"].endswith("judge summary") and v["catalysts"]
    listed = v["consensus_claims"] + v["contested_claims"]
    assert v["n_claims"] > len(v["escalated_ids"]) > 0 and all(i.startswith("bear") or "/" in i for i in v["escalated_ids"])
    assert all(c["judge"]["path"] in ("screen", "escalated") for c in listed)
    assert {c["judge"]["path"] for c in listed} == {"screen", "escalated"}  # bear claims (materiality 4) escalate; the rest pass through
    assert all((c["id"] in v["escalated_ids"]) == (c["judge"]["path"] == "escalated") for c in listed)
    assert v["screen_judges"][0]["model"] == "anthropic-screen" and v["judges"][0]["model"] == "anthropic-judge"
    md = html.with_suffix(".md").read_text()
    assert html.name.endswith("-full.html") and "judges agreed" in md and "Bull case" in md and "Bear case" in md  # no contested claims in the fixture run
    assert "claims escalated" in md and "escalation panel" in md.lower() and "screen panel" in md.lower()
    assert "anthropic judge summary" in md and "openai judge summary" in md and "eps_surprise" in md  # the other judges and the metrics table live in the appendix


@pytest.mark.db
async def test_full_rerun_zero_calls(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake(p) for p in PROVIDERS]
    screen, judges = [screen_fake(p) for p in PROVIDERS], [rebuttal_or_judge(p) for p in PROVIDERS]
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    n1 = calls(personas) + calls(screen) + calls(judges)
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) + calls(screen) + calls(judges) == n1
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), rerun=True, out_dir=tmp_path)
    assert calls(personas) + calls(screen) + calls(judges) == 2 * n1


@pytest.mark.db
async def test_full_with_one_judge(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake("anthropic")]
    screen, judges = [screen_fake("anthropic")], [rebuttal_or_judge("anthropic")]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) == 10 and calls(screen) == 1 and calls(judges) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts WHERE run_kind = 'full'")
        v = cur.fetchone()[0]
    assert v["judge_agreement"] == 1.0 and v["median_judge"] == "anthropic" and html.exists()
    listed = v["consensus_claims"] + v["contested_claims"]
    assert {c["judge"]["path"] for c in listed} == {"screen", "escalated"}  # a single screen judge does not escalate everything


@pytest.mark.db
async def test_full_with_screen_panel_down_escalates_everything(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    down = FakeAdapter([RuntimeError("down")])
    down.provider, down.model = "anthropic", "anthropic-screen"
    personas, judges = [persona_fake("anthropic")], [rebuttal_or_judge("anthropic")]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, [down], judges, FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts WHERE run_kind = 'full'")
        v = cur.fetchone()[0]
    assert len(v["escalated_ids"]) == v["n_claims"] > 0 and calls(judges) == 1 and html.exists()
    assert all(c["judge"]["path"] == "escalated" for c in v["consensus_claims"] + v["contested_claims"])


@pytest.mark.db
async def test_snapshot_id_skips_prepare_and_writes_run_kind(fixture_prepare, fmp_fixture, tmp_path, monkeypatch):
    conn = fixture_prepare
    personas, screen, judges = [persona_fake("anthropic")], [screen_fake("anthropic")], [rebuttal_or_judge("anthropic")]
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_id, verdict FROM verdicts WHERE run_kind = 'full'"); sid, full = cur.fetchone()

    def boom(*a, **k):
        raise AssertionError("prepare must not run")
    monkeypatch.setattr(pipeline, "prepare", boom)
    n = calls(personas) + calls(screen) + calls(judges)
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path, run_kind="triage", snapshot_id=sid)
    assert html.name.endswith("-triage.html") and calls(personas) + calls(screen) + calls(judges) == n  # every round cached
    with conn.cursor() as cur:
        cur.execute("SELECT run_kind, verdict FROM verdicts WHERE snapshot_id = %s ORDER BY run_kind", (sid,))
        rows = dict(cur.fetchall())
    assert set(rows) == {"full", "triage"} and rows["full"] == full


@pytest.mark.db
async def test_verdict_report_has_heatmap(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake(p) for p in ("anthropic", "openai")]
    judges = [rebuttal_or_judge("anthropic")]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, [screen_fake("anthropic")], judges, FakeEmbedder(), out_dir=tmp_path)
    text = html.read_text()
    assert 'class="cell"' in text and text.count('class="cell"') == 10 and "<svg" in text


@pytest.mark.db
async def test_snapshot_id_never_touches_the_grid(fixture_prepare, fmp_fixture, tmp_path):
    """judge-ab must not retry failed persona cells: a retried cell would invalidate rebuttals and the stored full verdict."""
    conn = fixture_prepare
    personas, screen, judges = [persona_fake("anthropic")], [screen_fake("anthropic")], [rebuttal_or_judge("anthropic")]
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT snapshot_id, verdict FROM verdicts WHERE run_kind = 'full'"); sid, full = cur.fetchone()
        cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, chunk_ids, status, error) VALUES (%s, 'bull', 'openai', 'openai-persona', '{}', '[]', 'failed', 'x')", (sid,))
    conn.commit()
    openai_persona = persona_fake("openai")
    n = calls(personas) + calls(screen) + calls(judges)
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas + [openai_persona], screen, judges, FakeEmbedder(), out_dir=tmp_path, run_kind="triage", snapshot_id=sid)
    assert openai_persona.calls == 0 and calls(personas) + calls(screen) + calls(judges) == n
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts WHERE snapshot_id = %s AND run_kind = 'full'", (sid,))
        assert cur.fetchone()[0] == full
        cur.execute("SELECT count(*) FROM rebuttals WHERE snapshot_id = %s", (sid,)); assert cur.fetchone()[0] == 5


@pytest.mark.db
async def test_unscored_escalated_claims_are_reported(fixture_prepare, fmp_fixture, tmp_path, caplog):
    conn = fixture_prepare
    strong = rebuttal_or_judge("anthropic")
    inner = strong.outputs

    def out(system, user):
        o = inner(system, user)
        o["claims"] = o["claims"][1:]  # forgets the first escalated claim
        return o
    strong.outputs = out
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, [persona_fake("anthropic")], [screen_fake("anthropic")], [strong], FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts WHERE run_kind = 'full'"); v = cur.fetchone()[0]
    assert len(v["fallback_ids"]) == 1 and v["fallback_ids"][0] in v["escalated_ids"]
    assert any("escalated but unscored" in r.message for r in caplog.records)
