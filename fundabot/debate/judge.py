"""Three judges (one per provider) score the surviving claims, then reconcile once after reading each other."""
import json
import logging

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents.base import run_batch
from fundabot.agents.schemas import SchemaError
from fundabot.analytics.metrics import Metric
from fundabot.report.table import GROUPS

log = logging.getLogger("fundabot")

SUB_SCORES = ["quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score"]
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim_id": {"type": "string"}, "evidence_strength": {"type": "integer", "description": "1-5"},
                           "materiality": {"type": "integer", "description": "1-5"}, "note": {"type": "string"}},
            "required": ["claim_id", "evidence_strength", "materiality", "note"], "additionalProperties": False}},
        **{k: {"type": "integer", "description": "0-100"} for k in SUB_SCORES},
        "catalysts": {"type": "array", "items": {
            "type": "object", "properties": {"what": {"type": "string"}, "when": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["what", "when", "evidence"], "additionalProperties": False}},
        "one_paragraph_summary": {"type": "string"},
    },
    "required": ["claims", *SUB_SCORES, "catalysts", "one_paragraph_summary"],
    "additionalProperties": False,
}

SYSTEM = """You are the judge on a panel of deliberately biased analysts. You do not form a thesis of your own. You score the claims
the analysts made, after they have rebutted each other, and you produce four sub-scores, catalysts and a summary.

Scoring each claim:
- evidence_strength: 1 = asserted with weak support, 3 = consistent with the cited evidence, 5 = directly shown by the cited metric or passage.
  A refutation with good evidence lowers it; a concession from an opposing analyst raises it.
- materiality: 1 = trivia, 3 = relevant to the thesis, 5 = decides the investment case.
- note: one sentence when your score disagrees with the claim's weight, the refutation, or another judge.

Sub-scores (0-100):
- quality_score: moat, return on capital and its trend, durability of the business.
- value_score: price against the company's own history and the DCF; 100 = very cheap, 0 = very expensive.
- earnings_momentum_score: surprises, estimate revisions and growth acceleration; from consensus data when available, else reported results.
- accounting_risk_score: 0 = clean, 50 = questions worth asking, 100 = likely misstatement. Lean on the rules-engine flags and the skeptic's surviving claims.

Catalysts must cite evidence ids from the inputs. The summary is at most 120 words, states the composite picture and the main disagreement,
and quotes no number that is not in the inputs. Use only the inputs; nothing from memory."""


def validate_judgement(output: dict) -> dict:
    if not isinstance(output, dict) or set(output) != set(JUDGE_SCHEMA["required"]):
        raise SchemaError(f"fields: {sorted(output) if isinstance(output, dict) else type(output)}")
    for c in output["claims"]:
        if not isinstance(c, dict) or set(c) != {"claim_id", "evidence_strength", "materiality", "note"}:
            raise SchemaError(f"bad claim score {c}")
        for k in ("evidence_strength", "materiality"):
            if not isinstance(c[k], int) or not 1 <= c[k] <= 5:
                raise SchemaError(f"{k} out of range in {c['claim_id']}")
    for k in SUB_SCORES:
        if not isinstance(output[k], int) or not 0 <= output[k] <= 100:
            raise SchemaError(f"{k} out of range")
    for cat in output["catalysts"]:
        if not isinstance(cat, dict) or set(cat) != {"what", "when", "evidence"} or not isinstance(cat["evidence"], list):
            raise SchemaError("bad catalyst")
    if not isinstance(output["one_paragraph_summary"], str):
        raise SchemaError("summary")
    return output


def system_prompt() -> str:
    return SYSTEM


def precheck(pool: list[dict], resolvable) -> list[dict]:
    """Belt and braces: judges never see withdrawn claims or claims whose evidence does not resolve."""
    return [c for c in pool if not c["withdrawn"] and c["evidence"] and all(resolvable(e) for e in c["evidence"])]


def _metrics_summary(metrics: list[Metric]) -> list[str]:
    from fundabot.agents.context import metric_line, select_periods

    ids = [mid for group in GROUPS.values() for mid in group]
    keep = select_periods(metrics)
    latest_fy = max((p for p in keep if p.startswith("FY")), default=None)
    rows = [m for m in metrics if m.metric_id in ids and m.period in (latest_fy, "current")]
    return [metric_line(m) for m in sorted(rows, key=lambda m: (ids.index(m.metric_id), m.period))]


def build_message(pool, flags, metrics, earnings_available: bool, evidence: dict[str, str], revised: dict) -> str:
    from fundabot.agents.context import metric_line

    lines = ["CLAIMS (after rebuttal)"]
    for c in pool:
        lines.append(f"{c['id']} by {c['agent']} ({c['direction']}, weight {c['weight']}): {c['text']} evidence={c['evidence']}")
        if c["conceded_by"]:
            lines.append(f"    conceded_by: {', '.join(c['conceded_by'])}")
        for r in c["refutations"]:
            lines.append(f"    refuted by {r['by']}: {r['reason']} evidence={r['evidence']}")
    lines += ["", "RED_FLAGS (rules engine)"] + ([f"{f.flag_id} [{f.severity}]: {f.detail} evidence={json.dumps(f.evidence)}" for f in flags] or ["none"])
    lines += ["", "METRICS_SUMMARY (latest fiscal year and current)"] + _metrics_summary(metrics)
    lines += ["", "CONSENSUS_DATA (FMP)"]
    if earnings_available:
        lines += [metric_line(m) for m in metrics if "vendor_fmp" in m.flags or "llm_extracted" in m.flags]
    else:
        lines.append("Consensus estimates are unavailable for this company; score earnings_momentum from reported results only and say so.")
    lines += ["", "AGENT_SCORES (revised after rebuttal)"] + [f"{a}: {s} (confidence {c})" for a, (s, c) in sorted(revised.items())]
    lines += ["", "EVIDENCE"] + [f"--- {eid}\n{text}" for eid, text in evidence.items()]
    return "\n".join(lines)


def build_reconcile_message(own: dict, others: list[tuple[str, dict]]) -> str:
    lines = ["YOUR_ROUND_1", json.dumps(own, indent=1)]
    for provider, out in others:
        lines += ["", f"OTHER_JUDGE ({provider})", json.dumps(out, indent=1)]
    lines += ["", "Revise where persuaded; where you keep a different score, say why in note. Return the full schema again."]
    return "\n".join(lines)


def load_judgements(conn: psycopg.Connection, sid: str, round_: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT provider, model, round, output FROM judgements WHERE snapshot_id = %s AND round = %s ORDER BY provider", (sid, round_))
        return [dict(zip(["provider", "model", "round", "output"], r)) for r in cur.fetchall()]


async def _run_round(conn, sid, judges, messages: dict[str, str], round_: int) -> list[dict]:
    cached = {(j["provider"], j["model"]) for j in load_judgements(conn, sid, round_)}
    todo = [j for j in judges if (j.provider, j.model) not in cached]
    if todo:
        results = await run_batch(conn, sid, [(j, f"judge:r{round_}", system_prompt(), messages[j.provider], JUDGE_SCHEMA) for j in todo])
        for j, r in zip(todo, results):
            if isinstance(r, Exception):
                log.warning("judge %s round %s failed: %s", j.provider, round_, r)
                continue
            try:
                output = validate_judgement(r.output)
            except SchemaError as e:
                log.warning("judge %s round %s schema: %s", j.provider, round_, e)
                continue
            with conn.cursor() as cur:
                cur.execute("INSERT INTO judgements (snapshot_id, provider, model, round, output) VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT (snapshot_id, provider, model, round) DO UPDATE SET output = EXCLUDED.output",
                            (sid, j.provider, j.model, round_, Jsonb(output)))
            conn.commit()
    wanted = {(j.provider, j.model) for j in judges}
    order = {j.provider: i for i, j in enumerate(judges)}
    return sorted([j for j in load_judgements(conn, sid, round_) if (j["provider"], j["model"]) in wanted], key=lambda j: order[j["provider"]])


async def run_judges(conn, sid, judges, message: str) -> list[dict]:
    return await _run_round(conn, sid, judges, {j.provider: message for j in judges}, 1)


async def run_reconcile(conn, sid, judges, round1: list[dict]) -> list[dict]:
    by_provider = {j["provider"]: j["output"] for j in round1}
    messages = {j.provider: build_reconcile_message(by_provider[j.provider], [(p, o) for p, o in by_provider.items() if p != j.provider])
                for j in judges if j.provider in by_provider}
    return await _run_round(conn, sid, [j for j in judges if j.provider in messages], messages, 2)


def mean_scores(reconciled: list[dict], claim_ids: list[str]) -> dict[str, dict]:
    """Per claim: mean strength and materiality over the judges that scored it; unscored claims get zeros."""
    out = {}
    for cid in claim_ids:
        rows = [c for j in reconciled for c in j["output"]["claims"] if c["claim_id"] == cid]
        if not rows:
            out[cid] = {"evidence_strength": 0.0, "materiality": 0.0, "n_judges": 0, "spread": 0, "notes": []}
            continue
        s = [c["evidence_strength"] for c in rows]
        out[cid] = {"evidence_strength": sum(s) / len(s), "materiality": sum(c["materiality"] for c in rows) / len(rows),
                    "n_judges": len(rows), "spread": max(s) - min(s), "notes": [c["note"] for c in rows if c["note"]]}
    return out
