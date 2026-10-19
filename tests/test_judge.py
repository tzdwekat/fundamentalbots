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


def jrow(provider, scores, mats=None):
    """A stored round-1 judgement row."""
    out = jout(scores)
    for c in out["claims"]:
        c["materiality"] = (mats or {}).get(c["claim_id"], 3)
    return {"provider": provider, "model": "m", "round": 1, "output": out}


def pclaim(i, **kw):
    return {"id": i, "withdrawn": False, "refutations": [], "conceded_by": [], "agent": "bull/openai", "direction": "positive", "weight": 3, "text": f"text {i}", "evidence": ["metric:revenue@FY2024"], **kw}


TPOOL = [pclaim(i) for i in ("a", "b", "c", "d", "e")]


def test_escalate_rules():
    screen = [jrow("anthropic", {"a": 4, "b": 2, "c": 4, "d": 4, "e": 4}, {"c": 4}),
              jrow("openai", {"a": 4, "b": 4, "c": 4, "d": 4}),
              jrow("gemini", {"a": 4, "b": 3, "c": 4, "d": 4})]
    pool = [dict(c) for c in TPOOL]
    pool[3]["refutations"] = [{"by": "bear/openai", "reason": "r", "evidence": []}]
    # b: spread 2; c: one judge materiality 4; d: refuted; e: scored by one judge only; a: unanimous
    assert judge.escalate(screen, pool, 3) == ["b", "c", "d", "e"]


def test_escalate_single_judge_panel():
    screen = [jrow("anthropic", {"a": 4, "b": 2}, {"b": 4})]
    assert judge.escalate(screen, TPOOL[:2], 1) == ["b"]  # one judge: no "fewer than two" escalation; materiality still counts


def test_escalate_skips_withdrawn():
    screen = [jrow("anthropic", {"a": 4}), jrow("openai", {"a": 4})]
    pool = [pclaim("a", withdrawn=True)]
    assert judge.escalate(screen, pool, 2) == []


def test_merge_panels_paths_and_fallback():
    screen = {"a": {"evidence_strength": 4.0, "materiality": 3.0, "n_judges": 3, "spread": 0, "notes": []},
              "b": {"evidence_strength": 3.0, "materiality": 4.0, "n_judges": 3, "spread": 2, "notes": []},
              "c": {"evidence_strength": 2.0, "materiality": 4.0, "n_judges": 3, "spread": 0, "notes": []}}
    strong = {"b": {"evidence_strength": 5.0, "materiality": 5.0, "n_judges": 3, "spread": 1, "notes": ["n"]},
              "c": {"evidence_strength": 0.0, "materiality": 0.0, "n_judges": 0, "spread": 0, "notes": []}}
    m = judge.merge_panels(screen, strong, ["b", "c"])
    assert m["a"]["path"] == "screen" and m["a"]["evidence_strength"] == 4.0 and "fallback" not in m["a"]
    assert m["b"]["path"] == "escalated" and m["b"]["evidence_strength"] == 5.0 and m["b"]["notes"] == ["n"]
    assert m["c"]["path"] == "screen" and m["c"]["fallback"] is True and m["c"]["evidence_strength"] == 2.0  # no strong judge scored it


def test_mean_scores_with_missing_judge():
    recon = [{"provider": "anthropic", "output": jout({"a": 3, "b": 5})}, {"provider": "openai", "output": jout({"a": 5})}, {"provider": "gemini", "output": jout({"a": 4, "b": 1})}]
    m = judge.mean_scores(recon, ["a", "b", "c"])
    assert m["a"]["evidence_strength"] == 4.0 and m["a"]["n_judges"] == 3 and m["a"]["spread"] == 2
    assert m["b"]["evidence_strength"] == 3.0 and m["b"]["n_judges"] == 2 and m["b"]["spread"] == 4
    assert m["c"] == {"evidence_strength": 0.0, "materiality": 0.0, "n_judges": 0, "spread": 0, "notes": []}


def strong(outputs, provider="anthropic"):
    a = FakeAdapter(outputs)
    a.provider, a.model = provider, "strong"
    return a


def sid_for(db_conn):
    from fundabot.orchestrate import snapshot
    return snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)


@pytest.mark.db
async def test_run_judges_round1_cached(db_conn):
    sid = sid_for(db_conn)
    judges = []
    for pr in ("anthropic", "openai", "gemini"):
        a = FakeAdapter([jout({"bull/openai#0": 4}, summary=pr)])
        a.provider, a.model = pr, "screen-m"
        judges.append(a)
    r1 = await judge.run_judges(db_conn, sid, judges, "JUDGE MESSAGE")
    assert [j["provider"] for j in r1] == ["anthropic", "openai", "gemini"] and all(j["round"] == 1 for j in r1) and sum(a.calls for a in judges) == 3
    await judge.run_judges(db_conn, sid, judges, "JUDGE MESSAGE")
    assert sum(a.calls for a in judges) == 3


@pytest.mark.db
async def test_escalation_round_drops_unescalated_scores(db_conn):
    sid = sid_for(db_conn)
    a = strong([jout({"a": 5, "z": 1})])
    r2 = await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    assert [c["claim_id"] for c in r2[0]["output"]["claims"]] == ["a"] and r2[0]["output"]["_escalated"] == ["a"] and r2[0]["round"] == 2


@pytest.mark.db
async def test_round2_cache_keyed_on_escalated_set(db_conn):
    sid = sid_for(db_conn)
    a = strong([jout({"a": 5}), jout({"a": 4, "b": 4})])
    await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    assert a.calls == 1  # same set: cached
    r = await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a", "b"])
    assert a.calls == 2 and r[0]["output"]["_escalated"] == ["a", "b"] and len(r) == 1


@pytest.mark.db
async def test_old_reconcile_row_is_not_reused_as_strong_panel(db_conn):
    from psycopg.types.json import Jsonb

    sid = sid_for(db_conn)
    with db_conn.cursor() as cur:  # a round-2 row written by the old reconcile pipeline: no _escalated marker
        cur.execute("INSERT INTO judgements (snapshot_id, provider, model, round, output) VALUES (%s, 'anthropic', 'strong', 2, %s)", (sid, Jsonb(jout({"a": 1}))))
    a = strong([jout({"a": 5})])
    r = await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    assert a.calls == 1 and r[0]["output"]["claims"][0]["evidence_strength"] == 5


def test_escalation_message_blocks():
    msg = judge.build_escalation_message([TPOOL[1]], [jrow("anthropic", {"b": 2}), jrow("openai", {"b": 4})],
                                         [(TPOOL[0], {"evidence_strength": 4.0, "materiality": 3.0})], [], [], False,
                                         {"metric:revenue@FY2024": "revenue = 1"}, {"bull/openai": (2, 0.6)})
    assert "SCREEN_PANEL_SCORES" in msg and "anthropic strength 2 materiality 3" in msg and "UNANIMOUS_CLAIMS" in msg and "a by bull/openai" in msg
    assert msg.index("SCREEN_PANEL_SCORES") < msg.index("UNANIMOUS_CLAIMS") < msg.index("EVIDENCE")
    assert "b by bull/openai" in msg.split("SCREEN_PANEL_SCORES")[0] and "a by bull/openai" not in msg.split("SCREEN_PANEL_SCORES")[0]
