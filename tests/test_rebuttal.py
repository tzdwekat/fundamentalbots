from datetime import date

import pytest

from fundabot.agents import personas
from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.debate import claims, rebuttal
from tests.test_claims import analysis

GOOD = {"responses": [{"claim_id": "bear/gemini#0", "verdict": "concede", "reason": "fair", "evidence": []}],
        "withdraws": [], "revised_score": 3, "revised_confidence": 0.6}


def pool3():
    return claims.build_pool([analysis("bull", "openai", 5, ["up a", "up b"], "positive", 4),
                              analysis("bear", "gemini", -4, ["down c"], "negative", 3),
                              analysis("skeptic", "anthropic", -2, ["worry d"], "negative", 5)])


def test_schema_validation():
    assert rebuttal.validate_rebuttal(dict(GOOD)) == GOOD
    for bad in ({**GOOD, "revised_score": 11}, {**GOOD, "responses": [{**GOOD["responses"][0], "verdict": "maybe"}]}, {**GOOD, "extra": 1},
                {**GOOD, "withdraws": "bull/openai#0"}):
        with pytest.raises(rebuttal.SchemaError):
            rebuttal.validate_rebuttal(bad)
    assert rebuttal.REBUTTAL_SCHEMA["additionalProperties"] is False


def test_message_contains_opposing_and_evidence():
    a = analysis("bull", "openai", 5, ["up a"], "positive", 4)
    opp = [c for c in pool3() if c["agent"] != "bull/openai"]
    msg = rebuttal.build_message(a, opp, {"metric:revenue@FY2024": "metric:revenue@FY2024 = 1.0 USD <- x"})
    assert "YOUR_CLAIMS" in msg and "bull/openai#0" in msg and "OPPOSING_CLAIMS" in msg and "bear/gemini#0" in msg and "worry d" in msg
    assert "EVIDENCE" in msg and "metric:revenue@FY2024 = 1.0 USD" in msg
    sysmsg = rebuttal.system_prompt(personas.load_all()["bull"])
    assert "concede" in sysmsg and "refute" in sysmsg and "Withdraw" in sysmsg and "Cite a metric id or chunk id" in sysmsg


def test_apply_concede_refute_withdraw():
    pool = pool3()
    rebs = [
        {"agent": "bull/openai", "persona": "bull", "provider": "openai", "output": {
            "responses": [{"claim_id": "bear/gemini#0", "verdict": "concede", "reason": "true", "evidence": []},
                          {"claim_id": "skeptic/anthropic#0", "verdict": "refute", "reason": "no", "evidence": ["metric:revenue@FY2024"]}],
            "withdraws": ["bull/openai#1"], "revised_score": 3, "revised_confidence": 0.6}},
    ]
    pool, revised = rebuttal.apply_rebuttals(pool, rebs, lambda e: e.startswith("metric:"))
    by = {c["id"]: c for c in pool}
    assert by["bear/gemini#0"]["conceded_by"] == ["bull/openai"]
    assert by["skeptic/anthropic#0"]["refutations"] == [{"by": "bull/openai", "reason": "no", "evidence": ["metric:revenue@FY2024"]}]
    assert by["bull/openai#1"]["withdrawn"] is True and by["bull/openai#0"]["withdrawn"] is False
    assert revised["bull/openai"] == (3, 0.6)
    assert "bear/gemini" not in revised  # no rebuttal from the bear: caller keeps its original score


def test_rebuttal_ignores_unknown_and_foreign_ids():
    pool = pool3()
    rebs = [{"agent": "bull/openai", "persona": "bull", "provider": "openai", "output": {
        "responses": [{"claim_id": "nobody/none#9", "verdict": "concede", "reason": "x", "evidence": []},
                      {"claim_id": "bull/openai#0", "verdict": "concede", "reason": "self", "evidence": []}],
        "withdraws": ["bear/gemini#0"], "revised_score": 0, "revised_confidence": 0.5}}]
    pool, _ = rebuttal.apply_rebuttals(pool, rebs, lambda e: True)
    by = {c["id"]: c for c in pool}
    assert by["bear/gemini#0"]["withdrawn"] is False and by["bull/openai#0"]["conceded_by"] == [] and all(not c["conceded_by"] for c in pool)


def test_refutation_without_resolvable_evidence_dropped():
    pool = pool3()
    rebs = [{"agent": "bull/openai", "persona": "bull", "provider": "openai", "output": {
        "responses": [{"claim_id": "bear/gemini#0", "verdict": "refute", "reason": "x", "evidence": ["metric:made_up"]},
                      {"claim_id": "skeptic/anthropic#0", "verdict": "refute", "reason": "y", "evidence": []}],
        "withdraws": [], "revised_score": 1, "revised_confidence": 0.5}}]
    pool, _ = rebuttal.apply_rebuttals(pool, rebs, lambda e: e == "metric:revenue@FY2024")
    assert all(c["refutations"] == [] for c in pool)  # unresolvable evidence and empty evidence both drop the refutation


@pytest.mark.db
async def test_run_rebuttals_caches(db_conn):
    from fundabot.analytics.metrics import Metric
    from fundabot.orchestrate import snapshot
    from fundabot.retrieval.embed import FakeEmbedder

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)
    analyses = [analysis(p, pr, 2 if p in ("bull", "quality") else -2, [f"{p} {pr} claim"], "positive" if p in ("bull", "quality") else "negative")
                for p in ("bull", "bear", "skeptic", "quality", "macro") for pr in ("anthropic", "openai", "gemini")]
    pool = claims.build_pool(analyses)
    adapters = {pr: FakeAdapter([GOOD]) for pr in ("anthropic", "openai", "gemini")}
    for pr, a in adapters.items():
        a.provider, a.model = pr, "m"
    metrics = [Metric("revenue", "FY2024", 1.0, "USD", "x")]
    rebs = await rebuttal.run_rebuttals(db_conn, sid, analyses, pool, adapters, FakeEmbedder(), metrics, {}, ["acc"])
    assert len(rebs) == 15 and sum(a.calls for a in adapters.values()) == 15 and rebs[0]["output"]["revised_score"] == 3
    rebs2 = await rebuttal.run_rebuttals(db_conn, sid, analyses, pool, adapters, FakeEmbedder(), metrics, {}, ["acc"])
    assert len(rebs2) == 15 and sum(a.calls for a in adapters.values()) == 15
