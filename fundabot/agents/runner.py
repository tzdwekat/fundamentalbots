"""Run persona agents over a snapshot; cache results per (snapshot, persona, provider, model)."""
import asyncio
import logging

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents import context, personas, schemas
from fundabot.agents.base import Adapter, call_logged
from fundabot.orchestrate import snapshot as snap
from fundabot.retrieval.query import retrieve

log = logging.getLogger("fundabot")


def load_analyses(conn: psycopg.Connection, sid: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT persona, provider, model, output, grounded, chunk_ids FROM analyses WHERE snapshot_id = %s ORDER BY persona", (sid,))
        return [dict(zip(["persona", "provider", "model", "output", "grounded", "chunk_ids"], r)) for r in cur.fetchall()]


async def run_personas(conn: psycopg.Connection, sid: str, persona_names: list[str], adapter: Adapter, embedder) -> list[dict]:
    s = snap.load(conn, sid)
    metrics, flags = snap.load_metrics(conn, sid), snap.load_flags(conn, sid)
    all_personas = personas.load_all()
    cached = {a["persona"] for a in load_analyses(conn, sid) if a["provider"] == adapter.provider and a["model"] == adapter.model}
    todo = [n for n in persona_names if n not in cached]

    async def one(name: str):
        p = all_personas[name]
        chunks = retrieve(conn, embedder, s["accessions"], p["retrieval_queries"] + context.BASE_QUERIES)
        notes = ["No macro series or peer medians are available in this build; peer comparisons are insufficient data."] if name == "macro" else []
        user = context.build_user_message(s["ticker"], s, metrics, flags, chunks, notes)
        result = await call_logged(conn, adapter, sid, name, context.system_prompt(p), user, schemas.AGENT_OUTPUT_SCHEMA)
        output = schemas.validate(result.output)
        with conn.cursor() as cur:
            cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, chunk_ids) VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT (snapshot_id, persona, provider, model) DO UPDATE SET output = EXCLUDED.output, chunk_ids = EXCLUDED.chunk_ids, grounded = NULL",
                        (sid, name, adapter.provider, adapter.model, Jsonb(output), Jsonb([c.id for c in chunks])))
        conn.commit()
        log.info("%s/%s: %s tokens in, %s out, $%.4f", s["ticker"], name, result.input_tokens, result.output_tokens, result.cost_usd)

    # shortcut: calls share one psycopg connection, so they run sequentially on the DB side; upgrade to a pool if latency matters
    for name in todo:
        await one(name)
    return [a for a in load_analyses(conn, sid) if a["persona"] in persona_names and a["provider"] == adapter.provider and a["model"] == adapter.model]
