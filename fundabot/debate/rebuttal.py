"""Rebuttal round: each agent reads the strongest opposing claims and concedes, refutes or withdraws."""
import asyncio
import logging

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents import context, personas
from fundabot.agents.base import check_budget, run_batch
from fundabot.agents.schemas import SchemaError
from fundabot.debate import claims as claims_mod

log = logging.getLogger("fundabot")

REBUTTAL_SCHEMA = {
    "type": "object",
    "properties": {
        "responses": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["concede", "refute"]},
                           "reason": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["claim_id", "verdict", "reason", "evidence"], "additionalProperties": False}},
        "withdraws": {"type": "array", "items": {"type": "string"}, "description": "ids of your own claims you no longer stand behind"},
        "revised_score": {"type": "integer", "description": "-10 to 10"},
        "revised_confidence": {"type": "number", "description": "0 to 1"},
    },
    "required": ["responses", "withdraws", "revised_score", "revised_confidence"],
    "additionalProperties": False,
}

INSTRUCTIONS = ("You will read the strongest claims made against your position by other analysts. For at least the two strongest, either "
                "concede (you accept the claim is true) or refute it with a citation. Withdraw any of your own claims you no longer stand "
                "behind. Then give a revised score and confidence. Conceding when the evidence is against you is a strength, not a loss.")


def validate_rebuttal(output: dict) -> dict:
    if not isinstance(output, dict) or set(output) != set(REBUTTAL_SCHEMA["required"]):
        raise SchemaError(f"fields: {sorted(output) if isinstance(output, dict) else type(output)}")
    if not isinstance(output["responses"], list) or not isinstance(output["withdraws"], list) or not all(isinstance(w, str) for w in output["withdraws"]):
        raise SchemaError("responses and withdraws must be lists")
    for r in output["responses"]:
        if not isinstance(r, dict) or set(r) != {"claim_id", "verdict", "reason", "evidence"} or r["verdict"] not in ("concede", "refute"):
            raise SchemaError(f"bad response {r}")
        if not isinstance(r["evidence"], list):
            raise SchemaError("evidence must be a list")
    if not isinstance(output["revised_score"], int) or not -10 <= output["revised_score"] <= 10:
        raise SchemaError("revised_score")
    if not isinstance(output["revised_confidence"], (int, float)) or not 0 <= output["revised_confidence"] <= 1:
        raise SchemaError("revised_confidence")
    return output


def system_prompt(persona: dict) -> str:
    return "\n\n".join([f"You are the {persona['name']} analyst on a panel of deliberately biased analysts. Role and bias: {persona['bias'].strip()}",
                        INSTRUCTIONS, context.HARD_RULES,
                        "Output: JSON with responses (claim_id, verdict concede|refute, reason, evidence ids), withdraws (your own claim ids), "
                        "revised_score (-10..10 integer), revised_confidence (0..1)."])


def build_message(analysis: dict, opposing: list[dict], evidence: dict[str, str]) -> str:
    g = analysis["grounded"]
    lines = [f"YOUR_THESIS\n{g['thesis']}", "", "YOUR_CLAIMS"]
    lines += [f"{analysis['agent']}#{n}: ({c['direction']}, weight {c['weight']}) {c['text']} evidence={c['evidence']}" for n, c in enumerate(g["claims"])]
    lines += [f"\nYOUR_SCORE: {g['score']} confidence {g['confidence']}", "", "OPPOSING_CLAIMS (strongest first)"]
    lines += [f"{c['id']} by {c['agent']} ({c['direction']}, weight {c['weight']}): {c['text']} evidence={c['evidence']}" for c in opposing]
    lines += ["", "EVIDENCE (cited by the claims above)"]
    lines += [f"--- {eid}\n{text}" for eid, text in evidence.items()]
    return "\n".join(lines)


def load_rebuttals(conn: psycopg.Connection, sid: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT persona, provider, model, output, grounded FROM rebuttals WHERE snapshot_id = %s ORDER BY persona, provider", (sid,))
        rows = [dict(zip(["persona", "provider", "model", "output", "grounded"], r)) for r in cur.fetchall()]
    for r in rows:
        r["agent"] = f"{r['persona']}/{r['provider']}"
    return rows


async def run_rebuttals(conn, sid, analyses: list[dict], pool: list[dict], adapters_by_provider: dict, embedder, metrics, chunks_by_id, accessions) -> list[dict]:
    """One rebuttal per analysis without a cached one; calls run concurrently per provider."""
    cached = {(r["persona"], r["provider"], r["model"]) for r in load_rebuttals(conn, sid)}
    all_personas = personas.load_all()
    todo = [a for a in analyses if (a["persona"], a["provider"], a["model"]) not in cached and a["provider"] in adapters_by_provider]
    plan: dict[str, list[tuple[dict, str, str]]] = {}
    for a in todo:
        opposing = claims_mod.top_opposing(pool, a["agent"], a["grounded"]["score"], embedder)
        ids = [e for c in opposing for e in c["evidence"]] + [e for c in a["grounded"]["claims"] for e in c["evidence"]]
        evidence = claims_mod.evidence_text(sorted(set(ids)), metrics, chunks_by_id, accessions)
        plan.setdefault(a["provider"], []).append((a, system_prompt(all_personas[a["persona"]]), build_message(a, opposing, evidence)))

    async def one_provider(provider):
        adapter = adapters_by_provider[provider]
        items = plan[provider]
        results = await run_batch(conn, sid, [(adapter, f"rebuttal:{a['persona']}", s, u, REBUTTAL_SCHEMA) for a, s, u in items])
        return [(a, r) for (a, _, _), r in zip(items, results)]

    check_budget(conn, [(adapters_by_provider[p], s, u) for p, items in plan.items() for _, s, u in items])
    gathered = await asyncio.gather(*[one_provider(p) for p in plan], return_exceptions=True)
    for r in gathered:
        if isinstance(r, BaseException):
            raise r
    for results in gathered:
        for a, r in results:
            if isinstance(r, Exception):
                log.warning("rebuttal %s failed: %s", a["agent"], r)
                continue
            try:
                output = validate_rebuttal(r.output)
            except SchemaError as e:
                log.warning("rebuttal %s schema: %s", a["agent"], e)
                continue
            with conn.cursor() as cur:
                cur.execute("INSERT INTO rebuttals (snapshot_id, persona, provider, model, output) VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT (snapshot_id, persona, provider, model) DO UPDATE SET output = EXCLUDED.output",
                            (sid, a["persona"], a["provider"], a["model"], Jsonb(output)))
            conn.commit()
    wanted = {(a["persona"], a["provider"], a["model"]) for a in analyses}
    return [r for r in load_rebuttals(conn, sid) if (r["persona"], r["provider"], r["model"]) in wanted]


def apply_rebuttals(pool: list[dict], rebuttals: list[dict], resolvable) -> tuple[list[dict], dict[str, tuple[int, float]]]:
    """Mark concessions, keep refutations whose evidence all resolves (and is non-empty), withdraw own claims. Unknown ids are ignored."""
    by_id = {c["id"]: c for c in pool}
    revised = {}
    for r in rebuttals:
        agent, out = r["agent"], r["output"]
        for resp in out["responses"]:
            c = by_id.get(resp["claim_id"])
            if c is None or c["agent"] == agent:
                continue
            if resp["verdict"] == "concede":
                if agent not in c["conceded_by"]:
                    c["conceded_by"].append(agent)
            elif resp["evidence"] and all(resolvable(e) for e in resp["evidence"]):
                c["refutations"].append({"by": agent, "reason": resp["reason"], "evidence": list(resp["evidence"])})
        for wid in out["withdraws"]:
            c = by_id.get(wid)
            if c is not None and c["agent"] == agent:
                c["withdrawn"] = True
        revised[agent] = (int(out["revised_score"]), float(out["revised_confidence"]))
    return pool, revised
