import math

from fundabot.debate import judge, scoring

CELLS = [(p, pr) for p in ("bull", "bear") for pr in ("anthropic", "openai")]


def claim(cid, agent, weight=3, direction="positive", conceded=(), refuted=(), withdrawn=False):
    return {"id": cid, "agent": agent, "persona": agent.split("/")[0], "provider": agent.split("/")[1], "text": cid, "evidence": ["metric:x"],
            "direction": direction, "weight": weight, "conceded_by": list(conceded),
            "refutations": [{"by": r, "reason": "r", "evidence": ["metric:x"]} for r in refuted], "withdrawn": withdrawn}


def means(**kw):
    return {k: {"evidence_strength": v[0], "materiality": v[1], "n_judges": 3, "spread": v[2] if len(v) > 2 else 0, "notes": []} for k, v in kw.items()}


POOL = [claim("bull/anthropic#0", "bull/anthropic", 4), claim("bull/anthropic#1", "bull/anthropic", 2, conceded=["bear/openai"]),
        claim("bear/openai#0", "bear/openai", 5, "negative", refuted=["bull/anthropic"]), claim("bear/openai#1", "bear/openai", 1, "negative", withdrawn=True)]
MEANS = means(**{"bull/anthropic#0": (4, 4), "bull/anthropic#1": (3, 2), "bear/openai#0": (5, 5, 2), "bear/openai#1": (1, 1)})
REVISED = {"bull/anthropic": (6, 0.7), "bear/openai": (-4, 0.6), "bull/openai": (2, 0.5), "bear/anthropic": (-2, 0.5)}


def test_weights_and_concession_bonus():
    w = scoring.agent_weights(POOL, MEANS)
    assert w["bull/anthropic"] == 4 * 4 + 2 * 3 * 1.5 and w["bear/openai"] == 5 * 5  # withdrawn claim excluded


def test_composite_weighted_vs_plain():
    w = {"bull/anthropic": 25.0, "bear/openai": 25.0, "bull/openai": 0.0, "bear/anthropic": 0.0}
    v, how = scoring.composite(REVISED, w)
    assert v == (6 * 25 - 4 * 25) / 50 and how == "evidence-weighted"


def test_all_withdrawn_falls_back_to_plain_mean():
    v, how = scoring.composite(REVISED, {a: 0.0 for a in REVISED})
    assert v == (6 - 4 + 2 - 2) / 4 and how == "unweighted (no surviving claims)"


def test_dispersion_rows_and_columns():
    d = scoring.dispersion(REVISED, CELLS)
    assert math.isclose(d["overall"], scoring.pstd([6, -4, 2, -2]) / 10)
    assert math.isclose(d["by_persona"]["bull"], scoring.pstd([6, 2]) / 10) and math.isclose(d["by_provider"]["openai"], scoring.pstd([2, -4]) / 10)
    assert 0 <= d["overall"] <= 1


def test_veto_caps():
    assert scoring.veto(5.0, 50) == (5.0, None)
    v, note = scoring.veto(5.0, 75)
    assert v == 0.0 and "70" in note
    v, note = scoring.veto(5.0, 90)
    assert v == -3.0 and "85" in note
    assert scoring.veto(-6.0, 90) == (-6.0, None)


def test_classify_consensus_and_contested():
    consensus, contested = scoring.classify_claims(POOL, MEANS)
    assert [c["id"] for c in consensus] == ["bull/anthropic#0"]  # strength>=3, materiality>=3, no refutation, not withdrawn
    assert [c["id"] for c in contested] == ["bear/openai#0"]  # refuted and judge spread >= 2


def test_implied_composite_and_agreement():
    out = lambda s: {"claims": [{"claim_id": k, "evidence_strength": s, "materiality": 3, "note": ""} for k in MEANS]}
    implied = [scoring.implied_composite(POOL, out(5), REVISED), scoring.implied_composite(POOL, out(1), REVISED)]
    assert len(implied) == 2 and all(-10 <= x <= 10 for x in implied)
    assert scoring.judge_agreement([1.0, 3.0, 5.0]) == 1 - 4 / 20 and scoring.judge_agreement([2.0]) == 1.0
    assert scoring.median_judge([1.0, 5.0, 3.0], ["anthropic", "openai", "gemini"]) == "gemini"


def test_implied_from_means_matches_implied_composite():
    jo = {"claims": [{"claim_id": k, "evidence_strength": 4, "materiality": 3, "note": ""} for k in MEANS]}
    means_only = {c["claim_id"]: {"evidence_strength": float(c["evidence_strength"])} for c in jo["claims"]}
    assert scoring.implied_from_means(POOL, means_only, REVISED) == scoring.implied_composite(POOL, jo, REVISED)


def test_missing_judge_scores_use_available_judges():
    recon = [{"provider": "a", "output": {"claims": [{"claim_id": "bull/anthropic#0", "evidence_strength": 5, "materiality": 5, "note": ""}]}},
             {"provider": "b", "output": {"claims": [{"claim_id": "bull/anthropic#0", "evidence_strength": 3, "materiality": 3, "note": ""}]}},
             {"provider": "c", "output": {"claims": []}}]
    m = judge.mean_scores(recon, [c["id"] for c in POOL])
    w = scoring.agent_weights(POOL, m)
    assert w["bull/anthropic"] == 4 * 4.0 and w["bear/openai"] == 0.0


def test_verdict_schema_keys():
    recon = [{"provider": p, "model": "m", "round": 2, "output": {"claims": [], "quality_score": 70, "value_score": 30, "earnings_momentum_score": 60,
              "accounting_risk_score": 20, "catalysts": [], "one_paragraph_summary": f"{p} says"}} for p in ("anthropic", "openai", "gemini")]
    cells = {(c[0], c[1]): {"score": s, "confidence": 0.5, "hallucination_rate": 0.0} for c, s in zip(CELLS, (6, 2, -2, -4))}
    v = scoring.build_verdict("AAPL", "sid", POOL, REVISED, MEANS, recon, [2.0, 1.0, 3.0], cells, [{"what": "x", "when": "y", "evidence": []}],
                              [{"flag_id": "accruals_high", "severity": "high", "evidence": [], "detail": "d"}])
    for k in ("ticker", "snapshot_id", "composite_score", "dispersion", "quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score",
              "consensus_claims", "contested_claims", "red_flags", "catalysts", "one_paragraph_summary",
              "grid", "dispersion_by_persona", "dispersion_by_provider", "judge_agreement", "median_judge", "summaries", "veto_note", "weighting"):
        assert k in v, k
    assert v["median_judge"] == "anthropic" and v["one_paragraph_summary"] == "anthropic says" and v["quality_score"] == 70
    assert isinstance(v["composite_score"], float) and v["grid"]["bull/anthropic"]["score"] == 6 and v["summaries"]["gemini"] == "gemini says"
