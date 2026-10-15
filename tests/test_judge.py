from datetime import date

import pytest

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag
from fundabot.debate import claims, judge
from tests.test_claims import analysis


def pool3():
    p = claims.build_pool([analysis("bull", "openai", 5, ["up a", "up b"], "positive", 4), analysis("bear", "gemini", -4, ["down c"], "negative", 3)])
    p[1]["withdrawn"] = True
    p[2]["evidence"] = ["metric:nope"]
    return p


def jout(scores: dict, summary="s", sub=50):
    return {"claims": [{"claim_id": k, "evidence_strength": v, "materiality": 3, "note": ""} for k, v in scores.items()],
            "quality_score": sub, "value_score": sub, "earnings_momentum_score": sub, "accounting_risk_score": sub,
            "catalysts": [{"what": "x", "when": "soon", "evidence": ["metric:revenue@FY2024"]}], "one_paragraph_summary": summary}


def test_precheck_drops_unresolvable_and_withdrawn():
    kept = judge.precheck(pool3(), lambda e: e == "metric:revenue@FY2024")
    assert [c["id"] for c in kept] == ["bull/openai#0"]


def test_schema_validation():
    assert judge.validate_judgement(jout({"a": 3})) == jout({"a": 3})
    for bad in (jout({"a": 6}), {**jout({"a": 3}), "quality_score": 101}, {**jout({"a": 3}), "extra": 1}):
        with pytest.raises(judge.SchemaError):
            judge.validate_judgement(bad)


def test_judge_message_states_missing_consensus():
    pool = [c for c in pool3() if not c["withdrawn"]]
    pool[0]["conceded_by"] = ["bear/gemini"]
    pool[1]["refutations"] = [{"by": "bull/openai", "reason": "wrong", "evidence": ["metric:revenue@FY2024"]}]
    ms = [Metric("revenue", "FY2024", 1.0, "USD", "x"), Metric("gross_margin", "FY2024", 0.4, "ratio", "x")]
    flags = [RedFlag("accruals_high", "high", ["metric:accruals_ratio@FY2024"], "accruals 12%")]
    msg = judge.build_message(pool, flags, ms, earnings_available=False, evidence={"metric:revenue@FY2024": "metric:revenue@FY2024 = 1.0 USD <- x"},
                              revised={"bull/openai": (3, 0.6), "bear/gemini": (-4, 0.5)})
    assert "Consensus estimates are unavailable" in msg and "CLAIMS" in msg and "conceded_by: bear/gemini" in msg and "refuted by bull/openai: wrong" in msg
    assert "accruals_high" in msg and "AGENT_SCORES" in msg and "bull/openai: 3" in msg and "EVIDENCE" in msg
    msg2 = judge.build_message(pool, flags, ms + [Metric("eps_surprise", "Q3FY2026", 0.05, "ratio", "x", {}, ["vendor_fmp"])], True, {}, {})
    assert "CONSENSUS_DATA" in msg2 and "eps_surprise" in msg2 and "unavailable" not in msg2
    assert "do not form a thesis" in judge.system_prompt().lower() or "do not form" in judge.system_prompt().lower()
    rec = judge.build_reconcile_message(jout({"a": 3}, "mine"), [("openai", jout({"a": 5}, "theirs"))])
    assert "YOUR_ROUND_1" in msg or "YOUR_ROUND_1" in rec and "openai" in rec and "theirs" in rec


def test_mean_scores_with_missing_judge():
    recon = [{"provider": "anthropic", "output": jout({"a": 3, "b": 5})}, {"provider": "openai", "output": jout({"a": 5})}, {"provider": "gemini", "output": jout({"a": 4, "b": 1})}]
    m = judge.mean_scores(recon, ["a", "b", "c"])
    assert m["a"]["evidence_strength"] == 4.0 and m["a"]["n_judges"] == 3 and m["a"]["spread"] == 2
    assert m["b"]["evidence_strength"] == 3.0 and m["b"]["n_judges"] == 2 and m["b"]["spread"] == 4
    assert m["c"] == {"evidence_strength": 0.0, "materiality": 0.0, "n_judges": 0, "spread": 0, "notes": []}


@pytest.mark.db
async def test_run_judges_and_reconcile_cache(db_conn):
    from fundabot.orchestrate import snapshot

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)
    judges = []
    for pr in ("anthropic", "openai", "gemini"):
        a = FakeAdapter([jout({"bull/openai#0": 4}, summary=pr)])
        a.provider, a.model = pr, "judge-m"
        judges.append(a)
    r1 = await judge.run_judges(db_conn, sid, judges, "JUDGE MESSAGE")
    assert [j["provider"] for j in r1] == ["anthropic", "openai", "gemini"] and all(j["round"] == 1 for j in r1) and sum(a.calls for a in judges) == 3
    r2 = await judge.run_reconcile(db_conn, sid, judges, r1)
    assert all(j["round"] == 2 for j in r2) and sum(a.calls for a in judges) == 6 and r2[0]["output"]["one_paragraph_summary"] == "anthropic"
    await judge.run_judges(db_conn, sid, judges, "JUDGE MESSAGE")
    await judge.run_reconcile(db_conn, sid, judges, r1)
    assert sum(a.calls for a in judges) == 6
