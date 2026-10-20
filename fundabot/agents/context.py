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


def persona_tail(persona: dict) -> str:
    """The persona-specific part of the system prompt (everything shared sits in the cacheable prefix)."""
    return "\n\n".join([
        f"You are the {persona['name']} analyst on a panel of five deliberately biased analysts reviewing one US-listed company.",
        f"Role and bias: {persona['bias'].strip()}",
        f"You read most closely: {', '.join(persona['reads_most'])}.",
        OUTPUT_GUIDE,
    ] + [f"Additional rule: {r}" for r in persona.get("extra_rules", [])])


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


def select_metrics(metrics: list[Metric], flags=()) -> list[Metric]:
    """The metrics an agent is shown (and what counts as "shown" for grounding): report-group ids at the report's
    columns (5 FY, 4 Q, current), raw XBRL items and other derived ids at fiscal years plus the newest quarter (the
    latest OCF, SBC, debt and equity between annual reports), TTM / 5-year-range / DCF ids at current, plus any
    red-flag evidence id."""
    from fundabot.report.table import GROUPS, columns_for

    cols = set(columns_for(metrics))
    fys = {c for c in cols if c.startswith("FY")}
    qs = [c for c in cols if c.startswith("Q")]
    newest_q = max(qs, key=lambda p: (int(p[4:]), int(p[1]))) if qs else None
    group_ids = {m for g in GROUPS.values() for m in g}
    flagged = {e.split(":", 1)[1] for f in flags for e in f.evidence if e.startswith("metric:")}
    out = []
    for m in metrics:
        key = f"{m.metric_id}@{m.period}"
        if key in flagged or m.metric_id in flagged:
            out.append(m)
            continue
        if m.period not in cols:
            continue
        if m.metric_id in group_ids:
            out.append(m)
        elif m.formula_id.startswith("xbrl:") and (m.period in fys or m.period == newest_q):
            out.append(m)
        elif m.period == "current" and (m.metric_id.endswith("_ttm") or "_5y_" in m.metric_id or m.metric_id.startswith("dcf") or m.metric_id == "reverse_dcf_implied_growth"):
            out.append(m)
        elif m.period in fys or m.period == "current" or m.period == newest_q:  # other derived ids: no older quarters
            out.append(m)
    return sorted(out, key=lambda m: (m.metric_id, m.period))


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
             f"latest_close: {snapshot.get('latest_close')}\nfilings: {', '.join(snapshot.get('accessions') or [])}" + company_lines(snapshot.get("profile")), ""]
    lines.append("METRICS (one per line: id@period = value unit [flags] <- formula; cite by id)")
    for m in select_metrics(metrics, flags):
        lines.append(metric_line(m))
    lines += ["", "RED_FLAGS (rules engine)"]
    lines += [f"{f.flag_id} [{f.severity}]: {f.detail} evidence={json.dumps(f.evidence)}" for f in flags] or ["none"]
    if notes:
        lines += ["", "NOTES"] + notes
    lines += ["", "FILING_EXCERPTS (cite as chunk:<id>)"]
    for c in chunks:
        lines += [f"--- chunk:{c.id} ({c.item}, {c.accession})", c.text]
    return "\n".join(lines)


def company_lines(profile: dict | None) -> str:
    """What the company is, from the FMP profile, so a persona reads the numbers in context."""
    if not profile:
        return ""
    emp = f" · {profile['employees']:,} employees" if profile.get("employees") else ""
    about = (profile.get("description") or "").strip()
    return (f"\ncompany: {profile.get('name')} · {profile.get('sector')} · {profile.get('industry')}{emp}"
            + (f"\nabout: {about[:200]}{'…' if len(about) > 200 else ''}" if about else ""))


def shared_prefix(ticker: str, snapshot: dict, metrics: list[Metric], flags: list[RedFlag], shared_chunks: list[Chunk]) -> str:
    """Byte-identical across the personas of one snapshot: the inputs every agent sees, then the hard rules."""
    lines = [f"SNAPSHOT\nticker: {ticker}\nsnapshot_id: {snapshot['id']}\nprice_date: {snapshot.get('price_date')}\n"
             f"latest_close: {snapshot.get('latest_close')}\nfilings: {', '.join(snapshot.get('accessions') or [])}" + company_lines(snapshot.get("profile")), ""]
    lines.append("METRICS (one per line: id@period = value unit [flags] <- formula; cite by id)")
    lines += [metric_line(m) for m in select_metrics(metrics, flags)]
    lines += ["", "RED_FLAGS (rules engine)"]
    lines += [f"{f.flag_id} [{f.severity}]: {f.detail} evidence={json.dumps(f.evidence)}" for f in flags] or ["none"]
    lines += ["", "SHARED_EXCERPTS (cite as chunk:<id>)"]
    for c in sorted(shared_chunks, key=lambda c: c.id):
        lines += [f"--- chunk:{c.id} ({c.item}, {c.accession})", c.text]
    lines += ["", HARD_RULES]
    return "\n".join(lines)


def build_messages(ticker: str, snapshot: dict, metrics: list[Metric], flags: list[RedFlag], shared_chunks: list[Chunk], persona_chunks: list[Chunk],
                   persona: dict, notes: list[str]) -> tuple[str, str, str]:
    """(prefix, system_tail, user): the prefix is shared and cacheable, the tail is the persona, the user part is persona-specific input."""
    lines = []
    if notes:
        lines += ["NOTES"] + list(notes) + [""]
    lines.append("PERSONA_EXCERPTS (retrieved for your role; cite as chunk:<id>)")
    if persona_chunks:
        for c in persona_chunks:
            lines += [f"--- chunk:{c.id} ({c.item}, {c.accession})", c.text]
    else:
        lines.append("(no additional excerpts beyond the shared set)")
    lines += ["", "Write your analysis as JSON following the schema."]
    return shared_prefix(ticker, snapshot, metrics, flags, shared_chunks), persona_tail(persona), "\n".join(lines)
