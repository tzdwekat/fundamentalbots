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
        chunk_id = next(l.split()[1] for l in user.splitlines() if l.startswith("--- chunk:"))
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
        ids = re.findall(r"^(\S+/\S+#\d+) by", user, re.M) if "CLAIMS (after rebuttal)" in user else re.findall(r'"claim_id": "([^"]+)"', user)
        return {"claims": [{"claim_id": i, "evidence_strength": 4, "materiality": 3, "note": ""} for i in dict.fromkeys(ids)],
                "quality_score": 70, "value_score": 40, "earnings_momentum_score": 60, "accounting_risk_score": 20,
                "catalysts": [{"what": "new products", "when": "FY2027", "evidence": ["metric:revenue@FY2024"]}],
                "one_paragraph_summary": f"{provider} judge summary"}
    a = FakeAdapter(out)
    a.provider, a.model = provider, f"{provider}-judge"
    return a


@pytest.fixture
def fmp_fixture(monkeypatch):
    from fundabot.ingest import fmp
    monkeypatch.setattr(fmp, "fetch_all", lambda t: {k: json.loads((FIX / f"fmp_aapl_{k}.json").read_text()) for k in ("estimates_annual", "earnings", "price_target")})


def calls(adapters):
    return sum(a.calls for a in adapters)


@pytest.mark.db
async def test_full_run_call_counts_and_verdict(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake(p) for p in ("anthropic", "openai", "gemini")]
    judges = [rebuttal_or_judge(p) for p in ("anthropic", "openai", "gemini")]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) == 15 + 15 and calls(judges) == 3 + 3  # personas also answer their own rebuttals; judges do both rounds
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rebuttals"); assert cur.fetchone()[0] == 15
        cur.execute("SELECT count(*) FROM judgements"); assert cur.fetchone()[0] == 6
        cur.execute("SELECT verdict FROM verdicts WHERE run_kind = 'full'")
        v = cur.fetchone()[0]
    assert v["weighting"] == "evidence-weighted" and v["judge_agreement"] == 1.0 and any(c["conceded_by"] for c in v["consensus_claims"] + v["contested_claims"])
    for k in ("composite_score", "dispersion", "judge_agreement", "grid", "consensus_claims", "contested_claims", "one_paragraph_summary", "earnings_momentum_score"):
        assert k in v
    assert len(v["grid"]) == 15 and v["one_paragraph_summary"].endswith("judge summary") and v["catalysts"]
    md = html.with_suffix(".md").read_text()
    assert html.name.endswith("-full.html") and "Judge agreement" in md and "bull" in md and "contested" in md.lower()
    assert "anthropic judge summary" in md and "openai judge summary" in md and "eps_surprise" in md


@pytest.mark.db
async def test_full_rerun_zero_calls(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake(p) for p in ("anthropic", "openai", "gemini")]
    judges = [rebuttal_or_judge(p) for p in ("anthropic", "openai", "gemini")]
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, judges, FakeEmbedder(), out_dir=tmp_path)
    n1 = calls(personas) + calls(judges)
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) + calls(judges) == n1
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, judges, FakeEmbedder(), rerun=True, out_dir=tmp_path)
    assert calls(personas) + calls(judges) == 2 * n1


@pytest.mark.db
async def test_full_with_one_judge(fixture_prepare, fmp_fixture, tmp_path):
    conn = fixture_prepare
    personas = [persona_fake("anthropic")]
    judges = [rebuttal_or_judge("anthropic")]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) == 10 and calls(judges) == 1  # no reconcile round with a single judge
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts WHERE run_kind = 'full'")
        v = cur.fetchone()[0]
    assert v["judge_agreement"] == 1.0 and v["median_judge"] == "anthropic" and html.exists()
