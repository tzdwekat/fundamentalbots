"""Guidance and tone from earnings call transcripts: a structured extraction whose every quote is verified verbatim
against the cited chunk. Values that reach the metrics table are categorical (-1/0/+1), flagged llm_extracted."""
import re

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents.base import call_logged
from fundabot.analytics.metrics import Metric
from fundabot.retrieval.chunk import Chunk

GUIDANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "statements": {"type": "array", "items": {
            "type": "object",
            "properties": {"metric": {"type": "string", "enum": ["revenue", "eps", "margin", "capex", "other"]},
                           "period": {"type": "string"},
                           "direction": {"type": "string", "enum": ["raised", "maintained", "lowered", "initiated", "withdrawn"]},
                           "quote": {"type": "string", "description": "verbatim sentence from the transcript"},
                           "chunk": {"type": "string", "description": "the chunk id the quote comes from"}},
            "required": ["metric", "period", "direction", "quote", "chunk"], "additionalProperties": False}},
        "tone": {"type": "string", "enum": ["confident", "neutral", "cautious"]},
        "tone_quote": {"type": "string"}, "tone_chunk": {"type": "string"},
    },
    "required": ["statements", "tone", "tone_quote", "tone_chunk"], "additionalProperties": False,
}
DIRECTION = {"raised": 1, "initiated": 0, "maintained": 0, "lowered": -1, "withdrawn": -1}
TONE = {"confident": 1, "neutral": 0, "cautious": -1}
FLAGS = ["llm_extracted"]


def system_prompt() -> str:
    return ("You extract forward guidance from an earnings call transcript. For every explicit statement by management about expected "
            "revenue, EPS, margins, capex or other outlook for a future period, record the metric, the period as stated, whether the guidance "
            "was raised, maintained, lowered, initiated or withdrawn versus prior guidance, a verbatim quote (copy the sentence exactly), and "
            "the chunk id it came from. Then classify management's overall tone as confident, neutral or cautious with one verbatim quote and "
            "its chunk id. Quote only text that appears in the chunks. If there is no guidance, return an empty statements list.")


def build_message(key: str, chunks: list[Chunk]) -> str:
    return "\n".join([f"TRANSCRIPT {key} (chunks; cite as chunk:<id>)"] + [f"--- chunk:{c.id}\n{c.text}" for c in chunks])


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def verify(output: dict, chunks_by_id: dict[str, Chunk]) -> dict:
    """Keep a statement only if its chunk resolves and its quote is verbatim in that chunk; same for tone."""
    def ok(quote, chunk_ref):
        cid = chunk_ref.split(":", 1)[1] if chunk_ref.startswith("chunk:") else chunk_ref
        return cid in chunks_by_id and bool(_norm(quote)) and _norm(quote) in _norm(chunks_by_id[cid].text)
    kept = [s for s in output.get("statements", []) if ok(s.get("quote", ""), s.get("chunk", ""))]
    dropped = len(output.get("statements", [])) - len(kept)
    tone_ok = output.get("tone") in TONE and ok(output.get("tone_quote", ""), output.get("tone_chunk", ""))
    if output.get("tone") and not tone_ok:
        dropped += 1
    return {"statements": kept, "tone": output["tone"] if tone_ok else None, "tone_quote": output.get("tone_quote") if tone_ok else None,
            "tone_chunk": output.get("tone_chunk") if tone_ok else None, "dropped": dropped}


def label_for_key(key: str) -> str:
    fy, fq = key[1:].split("Q")
    return f"Q{fq}FY{fy}"


def load(conn: psycopg.Connection, cik: int, keys: list[str]) -> list[dict]:
    rows = []
    with conn.cursor() as cur:
        for k in keys:
            fy, fq = (int(x) for x in k[1:].split("Q"))
            cur.execute("SELECT statements, tone, tone_quote, tone_chunk FROM guidance WHERE cik = %s AND fy = %s AND fq = %s", (cik, fy, fq))
            r = cur.fetchone()
            if r:
                rows.append({"key": k, "statements": r[0], "tone": r[1], "tone_quote": r[2], "tone_chunk": r[3]})
    return rows


async def extract(conn: psycopg.Connection, cik: int, key: str, adapter, chunks: list[Chunk]) -> dict | None:
    """One extraction per transcript, cached in `guidance`. Returns the verified row."""
    cached = load(conn, cik, [key])
    if cached:
        return cached[0]
    result = await call_logged(conn, adapter, None, f"guidance:{key}", system_prompt(), build_message(key, chunks), GUIDANCE_SCHEMA)
    v = verify(result.output, {c.id: c for c in chunks})
    fy, fq = (int(x) for x in key[1:].split("Q"))
    with conn.cursor() as cur:
        cur.execute("INSERT INTO guidance (cik, fy, fq, statements, tone, tone_quote, tone_chunk, model) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (cik, fy, fq) DO UPDATE SET statements = EXCLUDED.statements, tone = EXCLUDED.tone, tone_quote = EXCLUDED.tone_quote, "
                    "tone_chunk = EXCLUDED.tone_chunk, model = EXCLUDED.model",
                    (cik, fy, fq, Jsonb(v["statements"]), v["tone"], v["tone_quote"], v["tone_chunk"], adapter.model))
    conn.commit()
    return {"key": key, **{k: v[k] for k in ("statements", "tone", "tone_quote", "tone_chunk")}}


def metrics(rows: list[dict]) -> list[Metric]:
    out = []
    for r in rows:
        label = label_for_key(r["key"])
        formula = f"guidance statements extracted from transcript {r['key']} (FMP fiscal labels), verified quotes"
        sts = r["statements"] or []
        primary = next((s for s in sts if s["metric"] == "revenue"), None) or next((s for s in sts if s["metric"] == "eps"), None)
        if primary:
            direction = DIRECTION[primary["direction"]]
        elif sts:
            vals = [DIRECTION[s["direction"]] for s in sts]
            direction = max(set(vals), key=vals.count)
        else:
            direction = None
        inputs = {"quotes": [s["quote"] for s in sts], "chunks": [s["chunk"] for s in sts], "directions": [s["direction"] for s in sts]}
        out.append(Metric("guidance_direction", label, direction, "ratio", formula, inputs, list(FLAGS), None, None if direction is not None else "no_guidance"))
        tone = TONE.get(r["tone"]) if r.get("tone") else None
        out.append(Metric("management_tone", label, tone, "ratio", f"management tone classified from transcript {r['key']} (FMP fiscal labels), verified quote",
                          {"quote": r.get("tone_quote"), "chunk": r.get("tone_chunk"), "tone": r.get("tone")}, list(FLAGS), None,
                          None if tone is not None else "no_guidance"))
    return out
