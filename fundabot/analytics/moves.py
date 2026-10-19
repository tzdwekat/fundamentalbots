"""What moved the stock: a short narrative from a cheap model, every sentence citing ids from the inputs, every number
checked against the stats table, shown metrics or the cited news / excerpt text. Numbers are never the model's."""
import hashlib
import json
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents.base import run_batch
from fundabot.agents.context import metric_line, select_metrics
from fundabot.eval.grounding import EVIDENCE_ID, _supported, extract_numbers

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                                                 "required": ["text", "evidence"], "additionalProperties": False}},
        "one_line": {"type": "string"},
    },
    "required": ["sentences", "one_line"], "additionalProperties": False,
}
PCT = ("ret_1m", "ret_3m", "ret_6m", "ret_12m", "drawdown_52w", "dist_52w_low", "ma50_slope_20d", "vol_20d", "spy_ret", "excess_ret")
SYSTEM = """You explain what moved a stock's price over a window, for an investor who will check every word.
Write at most six sentences. Each sentence names one driver (a filing, a news item, an earnings result, a market-wide
move, a technical level) and cites the ids that show it, from the inputs only: price:<date> for a daily move,
news:<id>, filing:<accession>, earnings:<date>, transcript:<key>, grade:<id>, chunk:<id>, metric:<id>@<period>.
Quote no number that is not in the inputs; copy numbers as given. Say "no clear driver" when the inputs do not explain a
move. No advice, no forecast, no opinion on whether to buy or sell. one_line: the window in one short sentence."""


def system_prompt() -> str:
    return SYSTEM


def _pct(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def inputs_sha(inputs: dict) -> str:
    """Changes only when something that could change the story changes: window, the big-move dates, the news set, the snapshot."""
    key = {"window": inputs["window_days"], "snapshot": inputs.get("snapshot_id"), "moves": sorted(str(m["date"]) for m in inputs["moves"]),
           "news": sorted(n["id"] for n in inputs["news"]), "events": sorted(e.id for m in inputs["moves"] for e in m.get("events", []))}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]


def build_message(inputs: dict) -> str:
    s = inputs["stats"]
    lines = [f"WINDOW: last {inputs['window_days']} days to {s.get('asof')}", "", "PRICE_STATS (computed from daily closes)"]
    for k, v in s.items():
        if k in ("asof",):
            continue
        lines.append(f"{k} = {_pct(v) if k in PCT else v}")
    lines += ["", "LARGEST_MOVES (daily, with events within two trading days)"]
    for m in inputs["moves"]:
        lines.append(f"{m['id']}: {_pct(m['ret'])} to {m['close']:.2f}")
        for e in m.get("events", []):
            lines.append(f"    {e.id} ({e.kind}, {e.date}): {e.title}")
    lines += ["", "NEWS (id, date, publisher, title, start of text)"]
    lines += [f"news:{n['id']} {n['published'].date() if hasattr(n['published'], 'date') else n['published']} {n.get('publisher') or ''}: {n['title']} -- {(n.get('text') or '')[:300]}"
              for n in inputs["news"]] or ["none"]
    lines += ["", "EARNINGS (metrics)"] + [metric_line(m) for m in inputs["metrics"] if "vendor_fmp" in m.flags or "llm_extracted" in m.flags or m.metric_id.endswith("_yoy")] or ["none"]
    lines += ["", "EXCERPTS (filings and calls in the window)"] + [f"--- chunk:{c.id}\n{c.text}" for c in inputs["chunks"]] or ["none"]
    lines += ["", "Write the narrative as JSON following the schema."]
    return "\n".join(lines)


def _resolvable(inputs: dict) -> tuple[set[str], dict[str, str]]:
    """(ids that resolve, text per cited id that may support a number)."""
    ids, texts = set(), {}
    for m in inputs["moves"]:
        ids.add(m["id"])
        for e in m.get("events", []):
            ids.add(e.id)
            texts[e.id] = e.title
    for n in inputs["news"]:
        ids.add(f"news:{n['id']}")
        texts[f"news:{n['id']}"] = f"{n['title']} {n.get('text') or ''}"
    for c in inputs["chunks"]:
        ids.add(f"chunk:{c.id}")
        texts[f"chunk:{c.id}"] = c.text
    for m in select_metrics(inputs["metrics"]):
        ids.add(f"metric:{m.metric_id}@{m.period}")
        ids.add(f"metric:{m.metric_id}")
    return ids, texts


def ground_narrative(output: dict, inputs: dict) -> dict:
    """Keep a sentence only when every id resolves and every number is in the stats, a shown metric, a move, or the text of a cited id."""
    ids, texts = _resolvable(inputs)
    stats_values = [v for k, v in inputs["stats"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    stats_values += [v for m in inputs["moves"] for v in (m["ret"], m["close"])]
    stats_values += [m.value for m in select_metrics(inputs["metrics"]) if m.value is not None]
    stats_values += [abs(v) for v in stats_values]  # "fell 12%" against a stored -0.118
    kept, dropped = [], []
    for sent in output.get("sentences", []):
        evidence = list(dict.fromkeys(sent.get("evidence", [])))
        missing = [e for e in evidence if e not in ids]
        if missing or not evidence:
            dropped.append({**sent, "reason": f"unresolved: {', '.join(missing) or 'no evidence'}"})
            continue
        cited_values = [v for e in evidence for _, v in extract_numbers(texts.get(e, ""))]
        unsupported = [t for t, v in extract_numbers(EVIDENCE_ID.sub(" ", sent["text"])) if not _supported(t, v, stats_values, cited_values)]
        if unsupported:
            dropped.append({**sent, "reason": f"unsupported: {', '.join(unsupported)}"})
            continue
        kept.append({**sent, "evidence": evidence, "unsupported_numbers": []})
    return {"sentences": kept, "dropped": dropped, "one_line": output.get("one_line", "")}


def load(conn: psycopg.Connection, cik: int, window_days: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT inputs_sha, output, dropped, cost_usd, created_at FROM narratives WHERE cik = %s AND window_days = %s", (cik, window_days))
        r = cur.fetchone()
    return None if r is None else dict(zip(["inputs_sha", "output", "dropped", "cost_usd", "created_at"], r))


def stale(conn: psycopg.Connection, cik: int, window_days: int, sha: str) -> bool:
    row = load(conn, cik, window_days)
    return row is not None and row["inputs_sha"] != sha


async def ensure_narrative(conn: psycopg.Connection, adapter, cik: int, window_days: int, inputs: dict, force: bool = False) -> dict:
    """Stored narrative for the same inputs hash, else one model call (logged, budgeted), grounded and stored."""
    sha = inputs_sha(inputs)
    row = load(conn, cik, window_days)
    if row and row["inputs_sha"] == sha and not force:
        return {"output": row["output"], "dropped": row["dropped"], "stale": False, "cost_usd": float(row["cost_usd"]), "cached": True}
    (r,) = await run_batch(conn, None, [(adapter, f"narrative:{window_days}", system_prompt(), build_message(inputs), NARRATIVE_SCHEMA)])
    if isinstance(r, Exception):
        raise r
    g = ground_narrative(r.output, inputs)
    out = {"sentences": g["sentences"], "one_line": g["one_line"], "dropped_sentences": g["dropped"], "asof": str(inputs["stats"].get("asof") or date.today())}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO narratives (cik, window_days, inputs_sha, output, dropped, cost_usd) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (cik, window_days) DO UPDATE SET inputs_sha = EXCLUDED.inputs_sha, output = EXCLUDED.output, dropped = EXCLUDED.dropped, cost_usd = EXCLUDED.cost_usd, created_at = now()",
                    (cik, window_days, sha, Jsonb(out), len(g["dropped"]), r.cost_usd))
    conn.commit()
    return {"output": out, "dropped": len(g["dropped"]), "stale": False, "cost_usd": r.cost_usd, "cached": False}
