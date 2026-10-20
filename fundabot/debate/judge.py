"""Judges: a cheap screen panel scores every surviving claim (round 1); the strong panel re-judges the escalated ones with evidence (round 2)."""
import json
import logging

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents.base import run_batch
from fundabot.agents.schemas import SchemaError
from fundabot.analytics.earnings import IDS as EARNINGS_IDS
from fundabot.analytics.metrics import Metric
from fundabot.report.table import GROUPS

log = logging.getLogger("fundabot")

ESCALATE_SPREAD = 2  # screen judges' evidence_strength spread at or above this sends the claim to the strong panel
ESCALATE_MATERIALITY = 4  # any screen judge rating a claim this material sends it to the strong panel
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
        lines += [metric_line(m) for m in metrics if m.metric_id in EARNINGS_IDS or "llm_extracted" in m.flags]
    else:
        lines.append("Consensus estimates are unavailable for this company; score earnings_momentum from reported results only and say so.")
    lines += ["", "AGENT_SCORES (revised after rebuttal)"] + [f"{a}: {s} (confidence {c})" for a, (s, c) in sorted(revised.items())]
    lines += ["", "EVIDENCE"] + [f"--- {eid}\n{text}" for eid, text in evidence.items()]
    return "\n".join(lines)


def build_escalation_message(escalated_pool, screen: list[dict], unanimous: list[tuple[dict, dict]], flags, metrics, earnings_available: bool,
                             evidence: dict[str, str], revised: dict) -> str:
    """Round 2: the escalated claims with their evidence (as round 1), plus what the screen panel said about them and the
    unanimous claims it already settled (for the sub-scores and summary; not to be scored again)."""
    base = build_message(escalated_pool, flags, metrics, earnings_available, evidence, revised)
    head, _, tail = base.partition("\nEVIDENCE\n")
    lines = [head, "", "SCREEN_PANEL_SCORES (a cheaper panel's first pass on the claims above; you decide)"]
    for c in escalated_pool:
        for j in screen:
            for sc in j["output"]["claims"]:
                if sc["claim_id"] == c["id"]:
                    lines.append(f"{c['id']}: {j['provider']} strength {sc['evidence_strength']} materiality {sc['materiality']}" + (f' note "{sc["note"]}"' if sc["note"] else ""))
    lines += ["", "UNANIMOUS_CLAIMS (scored by the screen panel; do not score these, but weigh them in the sub-scores and summary)"]
    lines += [f"{c['id']} by {c['agent']} ({c['direction']}, weight {c['weight']}): {c['text']} -> strength {m['evidence_strength']:.1f} materiality {m['materiality']:.1f}"
              for c, m in unanimous] or ["none"]
    lines += ["", "EVIDENCE", tail]
    return "\n".join(lines)


def load_judgements(conn: psycopg.Connection, sid: str, round_: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT provider, model, round, output FROM judgements WHERE snapshot_id = %s AND round = %s ORDER BY provider", (sid, round_))
        return [dict(zip(["provider", "model", "round", "output"], r)) for r in cur.fetchall()]


async def _run_round(conn, sid, judges, messages: dict[str, str], round_: int, escalated: list[str] | None = None) -> list[dict]:
    """escalated (round 2): the output is stored with that id list and reused only for the same list; scores for other ids are dropped."""
    def usable(row):
        return escalated is None or row["output"].get("_escalated") == escalated
    cached = {(j["provider"], j["model"]) for j in load_judgements(conn, sid, round_) if usable(j)}
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
            if escalated is not None:
                extra = [c["claim_id"] for c in output["claims"] if c["claim_id"] not in escalated]
                if extra:
                    log.warning("judge %s scored %d claims outside the escalated set; dropped: %s", j.provider, len(extra), extra[:5])
                output = {**output, "claims": [c for c in output["claims"] if c["claim_id"] in escalated], "_escalated": list(escalated)}
            with conn.cursor() as cur:
                cur.execute("INSERT INTO judgements (snapshot_id, provider, model, round, output) VALUES (%s,%s,%s,%s,%s) "
                            "ON CONFLICT (snapshot_id, provider, model, round) DO UPDATE SET output = EXCLUDED.output",
                            (sid, j.provider, j.model, round_, Jsonb(output)))
            conn.commit()
    wanted = {(j.provider, j.model) for j in judges}
    order = {j.provider: i for i, j in enumerate(judges)}
    return sorted([j for j in load_judgements(conn, sid, round_) if (j["provider"], j["model"]) in wanted and usable(j)], key=lambda j: order[j["provider"]])


async def run_judges(conn, sid, judges, message: str) -> list[dict]:
    return await _run_round(conn, sid, judges, {j.provider: message for j in judges}, 1)


async def run_escalation(conn, sid, judges, messages: dict[str, str], escalated: list[str]) -> list[dict]:
    return await _run_round(conn, sid, [j for j in judges if j.provider in messages], messages, 2, list(escalated))


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


def escalate(screen: list[dict], pool: list[dict], panel_size: int) -> list[str]:
    """Claim ids the strong panel must judge with evidence: screen judges disagree (spread), any screen judge calls it
    case-deciding (materiality), it was refuted, or too few screen judges scored it. Deterministic; no model involved."""
    live = [c for c in pool if not c["withdrawn"]]
    means = mean_scores(screen, [c["id"] for c in live])
    material = {c["claim_id"] for j in screen for c in j["output"]["claims"] if c["materiality"] >= ESCALATE_MATERIALITY}
    min_judges = min(2, panel_size)
    return sorted(c["id"] for c in live
                  if means[c["id"]]["spread"] >= ESCALATE_SPREAD or c["id"] in material or c["refutations"] or means[c["id"]]["n_judges"] < min_judges)


def merge_panels(screen_means: dict, strong_means: dict, escalated: list[str]) -> dict:
    """Per claim: the strong panel's entry when escalated and scored, else the screen panel's; each tagged with its path."""
    out = {}
    esc = set(escalated)
    for cid, m in screen_means.items():
        strong = strong_means.get(cid)
        if cid in esc and strong and strong["n_judges"] > 0:
            out[cid] = {**strong, "path": "escalated"}
        elif cid in esc:
            out[cid] = {**m, "path": "screen", "fallback": True}  # escalated but no strong judge scored it
        else:
            out[cid] = {**m, "path": "screen"}
    return out
