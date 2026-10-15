"""Deterministic scoring: agent weights, composite, dispersion, judge agreement, veto, claim classification, verdict assembly."""
import math
import statistics

CONCESSION_BONUS = 1.5
VETO_SOFT, VETO_HARD = 70, 85


def pstd(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def agent_weights(pool: list[dict], judge_means: dict[str, dict]) -> dict[str, float]:
    """Sum over the agent's non-withdrawn claims of weight x judge strength x 1.5 if conceded by an opponent."""
    weights: dict[str, float] = {}
    for c in pool:
        weights.setdefault(c["agent"], 0.0)
        if c["withdrawn"]:
            continue
        strength = judge_means.get(c["id"], {}).get("evidence_strength", 0.0)
        weights[c["agent"]] += c["weight"] * strength * (CONCESSION_BONUS if c["conceded_by"] else 1.0)
    return weights


def composite(revised: dict[str, tuple[int, float]], weights: dict[str, float]) -> tuple[float, str]:
    total = sum(weights.get(a, 0.0) for a in revised)
    if total <= 0:
        return sum(s for s, _ in revised.values()) / len(revised), "unweighted (no surviving claims)"
    return sum(s * weights.get(a, 0.0) for a, (s, _) in revised.items()) / total, "evidence-weighted"


def dispersion(revised: dict[str, tuple[int, float]], cells: list[tuple[str, str]]) -> dict:
    scores = {a: s for a, (s, _) in revised.items()}
    clip = lambda x: max(0.0, min(1.0, x))
    personas = sorted({p for p, _ in cells})
    providers = sorted({pr for _, pr in cells})
    return {
        "overall": clip(pstd(list(scores.values())) / 10),
        "by_persona": {p: clip(pstd([scores[f"{p}/{pr}"] for pr in providers if f"{p}/{pr}" in scores]) / 10) for p in personas},
        "by_provider": {pr: clip(pstd([scores[f"{p}/{pr}"] for p in personas if f"{p}/{pr}" in scores]) / 10) for pr in providers},
    }


def implied_composite(pool: list[dict], judge_output: dict, revised: dict) -> float:
    means = {c["claim_id"]: {"evidence_strength": float(c["evidence_strength"])} for c in judge_output["claims"]}
    return composite(revised, agent_weights(pool, means))[0]


def judge_agreement(implied: list[float]) -> float:
    return 1.0 if len(implied) < 2 else max(0.0, 1 - (max(implied) - min(implied)) / 20)


def median_judge(implied: list[float], providers: list[str]) -> str:
    order = sorted(range(len(implied)), key=lambda i: implied[i])
    return providers[order[len(order) // 2]]


def veto(composite_value: float, accounting_risk: float) -> tuple[float, str | None]:
    if accounting_risk > VETO_HARD and composite_value > -3:
        return -3.0, f"accounting_risk_score {accounting_risk:.0f} > {VETO_HARD}: composite capped at -3"
    if accounting_risk > VETO_SOFT and composite_value > 0:
        return 0.0, f"accounting_risk_score {accounting_risk:.0f} > {VETO_SOFT}: composite capped at 0"
    return composite_value, None


def classify_claims(pool: list[dict], judge_means: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    consensus, contested = [], []
    for c in pool:
        if c["withdrawn"]:
            continue
        m = judge_means.get(c["id"], {"evidence_strength": 0.0, "materiality": 0.0, "spread": 0, "notes": []})
        entry = {**c, "judge": m}
        if c["refutations"] or m["spread"] >= 2:
            contested.append(entry)
        elif m["evidence_strength"] >= 3 and m["materiality"] >= 3:
            consensus.append(entry)
    consensus.sort(key=lambda e: -(e["judge"]["evidence_strength"] * e["judge"]["materiality"]))
    contested.sort(key=lambda e: -(e["weight"] * e["judge"]["materiality"]))
    return consensus, contested


def build_verdict(ticker, sid, pool, revised, judge_means, reconciled, implied, cells, catalysts, red_flags) -> dict:
    providers = [j["provider"] for j in reconciled]
    weights = agent_weights(pool, judge_means)
    raw, weighting = composite(revised, weights)
    subs = {k: round(sum(j["output"][k] for j in reconciled) / len(reconciled)) if reconciled else None
            for k in ("quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score")}
    capped, veto_note = veto(raw, subs["accounting_risk_score"] or 0)
    consensus, contested = classify_claims(pool, judge_means)
    disp = dispersion(revised, [tuple(k.split("/")) if isinstance(k, str) else k for k in cells])
    med = median_judge(implied, providers) if reconciled else None
    summaries = {j["provider"]: j["output"]["one_paragraph_summary"] for j in reconciled}
    return {
        "ticker": ticker, "snapshot_id": sid,
        "composite_score": round(capped, 1), "composite_uncapped": round(raw, 2), "weighting": weighting, "veto_note": veto_note,
        "dispersion": round(disp["overall"], 3), "dispersion_by_persona": disp["by_persona"], "dispersion_by_provider": disp["by_provider"],
        **subs,
        "consensus_claims": consensus, "contested_claims": contested, "red_flags": red_flags, "catalysts": catalysts,
        "one_paragraph_summary": summaries.get(med, ""), "median_judge": med, "summaries": summaries,
        "judge_agreement": round(judge_agreement(implied), 3) if reconciled else None, "implied_composites": dict(zip(providers, [round(x, 2) for x in implied])),
        "grid": {f"{p}/{pr}" if not isinstance(k, str) else k: v for k, v in cells.items() for p, pr in [k if not isinstance(k, str) else k.split("/")]},
        "agent_weights": {a: round(w, 2) for a, w in weights.items()}, "revised_scores": {a: s for a, (s, _) in revised.items()},
    }
