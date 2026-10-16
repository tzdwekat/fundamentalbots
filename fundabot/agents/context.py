"""System prompt and user message assembly. The agent sees only what is here."""
import json

from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag
from fundabot.retrieval.chunk import Chunk

BASE_QUERIES = ["business overview and segments", "results of operations latest period", "guidance and outlook",
                "liquidity and capital resources", "risk factors", "management outlook and guidance for next quarter"]

HARD_RULES = """Hard rules:
1. Cite a metric id or chunk id for every claim, in the claim's `evidence` list. Ids look like metric:revenue_yoy@FY2024,
   metric:pe@current, or chunk:0000320193-25-000079#item1a-14, and must be copied exactly from the inputs.
2. Never state a number that is not in the inputs. Do not compute new numbers; quote the metric by id instead.
3. If the inputs do not support a judgement, write "insufficient data" rather than guess. Do not use anything you
   remember about this company from outside the inputs: no prior-year figures, no executives, no news.
4. Metrics marked with flags (restated, derived, out_of_bounds) are less reliable; say so when you use them.
5. A null metric means the data is unavailable for the stated reason, not that the value is zero."""

OUTPUT_GUIDE = """Output: a JSON object with fields thesis (one paragraph), claims (each with text, evidence ids, direction
positive|negative, weight 1-5 where 5 is thesis-defining), key_risks, what_would_change_my_mind, score (-10 strong
avoid to 10 strong own, integer), confidence (0-1). Make 4 to 10 claims. Each claim is one checkable statement."""


def system_prompt(persona: dict) -> str:
    return "\n\n".join([
        f"You are the {persona['name']} analyst on a panel of five deliberately biased analysts reviewing one US-listed company.",
        f"Role and bias: {persona['bias'].strip()}",
        f"You read most closely: {', '.join(persona['reads_most'])}.",
        HARD_RULES, OUTPUT_GUIDE,
    ] + [f"Additional rule: {r}" for r in persona.get("extra_rules", [])])


def select_periods(metrics: list[Metric]) -> set[str]:
    fys = sorted({m.period for m in metrics if m.period.startswith("FY")})[-5:]
    qs = sorted({m.period for m in metrics if m.period.startswith("Q")}, key=lambda p: (int(p[4:]), int(p[1])))[-8:]
    return set(fys) | set(qs) | {"current"}


def metric_line(m: Metric) -> str:
    head = f"metric:{m.metric_id}@{m.period} = "
    if m.value is None:
        return f"{head}null ({m.null_reason}) <- {m.formula_id}"
    v = round(m.value, 4) if m.unit == "ratio" else m.value
    flags = f" [{', '.join(m.flags)}]" if m.flags else ""
    return f"{head}{v} {m.unit}{flags} <- {m.formula_id}"


def build_user_message(ticker: str, snapshot: dict, metrics: list[Metric], flags: list[RedFlag], chunks: list[Chunk], notes: list[str]) -> str:
    keep = select_periods(metrics)
    lines = [f"SNAPSHOT\nticker: {ticker}\nsnapshot_id: {snapshot['id']}\nprice_date: {snapshot.get('price_date')}\n"
             f"latest_close: {snapshot.get('latest_close')}\nfilings: {', '.join(snapshot.get('accessions') or [])}", ""]
    lines.append("METRICS (one per line: id@period = value unit [flags] <- formula; cite by id)")
    for m in sorted((m for m in metrics if m.period in keep), key=lambda m: (m.metric_id, m.period)):
        lines.append(metric_line(m))
    lines += ["", "RED_FLAGS (rules engine)"]
    lines += [f"{f.flag_id} [{f.severity}]: {f.detail} evidence={json.dumps(f.evidence)}" for f in flags] or ["none"]
    if notes:
        lines += ["", "NOTES"] + notes
    lines += ["", "FILING_EXCERPTS (cite as chunk:<id>)"]
    for c in chunks:
        lines += [f"--- chunk:{c.id} ({c.item}, {c.accession})", c.text]
    return "\n".join(lines)
