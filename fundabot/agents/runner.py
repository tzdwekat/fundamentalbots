"""Run persona agents over a snapshot across providers; cache per (snapshot, persona, provider, model)."""
import asyncio
import logging

import psycopg
from psycopg.types.json import Jsonb

from fundabot.agents import context, personas, schemas
from fundabot.agents.base import Adapter, check_budget, run_batch
from fundabot.eval.grounding import ground
from fundabot.orchestrate import snapshot as snap
from fundabot.retrieval.query import load_chunks, retrieve

log = logging.getLogger("fundabot")
PERSONA_ORDER = ["bull", "bear", "skeptic", "quality", "macro"]


def agent_key(persona: str, provider: str) -> str:
    return f"{persona}/{provider}"


def load_analyses(conn: psycopg.Connection, sid: str, status: str | None = "ok") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT persona, provider, model, output, grounded, chunk_ids, status, error FROM analyses WHERE snapshot_id = %s "
                    + ("AND status = %s " if status else "") + "ORDER BY persona, provider", (sid, status) if status else (sid,))
        rows = [dict(zip(["persona", "provider", "model", "output", "grounded", "chunk_ids", "status", "error"], r)) for r in cur.fetchall()]
    for a in rows:
        a["agent"] = agent_key(a["persona"], a["provider"])
    return rows


async def run_grid(conn: psycopg.Connection, sid: str, persona_names: list[str], adapters: list[Adapter], embedder) -> list[dict]:
    """Every (persona, adapter) cell without a cached ok analysis is run; failures are stored as status='failed' and skipped."""
    s = snap.load(conn, sid)
    metrics, flags = snap.load_metrics(conn, sid), snap.load_flags(conn, sid)
    all_personas = personas.load_all()
    cached = {(a["persona"], a["provider"], a["model"]) for a in load_analyses(conn, sid)}
    todo = {a: [n for n in persona_names if (n, a.provider, a.model) not in cached] for a in adapters}
    needed = {n for names in todo.values() for n in names}

    messages = {}  # persona -> (system, user, chunk_ids); retrieval once per persona, shared across providers
    for name in needed:
        p = all_personas[name]
        chunks = retrieve(conn, embedder, s["accessions"], p["retrieval_queries"] + context.BASE_QUERIES)
        notes = ["No macro series or peer medians are available in this build; peer comparisons are insufficient data."] if name == "macro" else []
        messages[name] = (context.system_prompt(p), context.build_user_message(s["ticker"], s, metrics, flags, chunks, notes), [c.id for c in chunks])

    async def one_provider(adapter: Adapter):
        names = todo[adapter]
        if not names:
            return []
        calls = [(adapter, n, messages[n][0], messages[n][1], schemas.AGENT_OUTPUT_SCHEMA) for n in names]
        return list(zip(names, await run_batch(conn, sid, calls)))

    check_budget(conn, [(a, messages[n][0], messages[n][1]) for a in adapters for n in todo[a]])  # whole grid, before any call
    per_provider = await asyncio.gather(*[one_provider(a) for a in adapters], return_exceptions=True)
    for r in per_provider:
        if isinstance(r, BaseException):
            raise r
    chunks_by_id = load_chunks(conn, sorted({c for n in needed for c in messages[n][2]}))
    for adapter, results in zip(adapters, per_provider):
        for name, r in results:
            with conn.cursor() as cur:
                if isinstance(r, Exception):
                    log.warning("%s/%s failed: %s", name, adapter.provider, r)
                    cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, chunk_ids, status, error) VALUES (%s,%s,%s,%s,'{}','[]','failed',%s) "
                                "ON CONFLICT (snapshot_id, persona, provider, model) DO UPDATE SET status = 'failed', error = EXCLUDED.error",
                                (sid, name, adapter.provider, adapter.model, str(r)[:500]))
                    continue
                try:
                    output = schemas.validate(r.output)
                except schemas.SchemaError as e:
                    cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, chunk_ids, status, error) VALUES (%s,%s,%s,%s,%s,'[]','failed',%s) "
                                "ON CONFLICT (snapshot_id, persona, provider, model) DO UPDATE SET status = 'failed', error = EXCLUDED.error",
                                (sid, name, adapter.provider, adapter.model, Jsonb(r.output), f"schema: {e}"[:500]))
                    continue
                ids = messages[name][2]
                grounded = ground(output, metrics, [chunks_by_id[c] for c in ids if c in chunks_by_id], s["accessions"])
                cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, grounded, chunk_ids, status, error) VALUES (%s,%s,%s,%s,%s,%s,%s,'ok',NULL) "
                            "ON CONFLICT (snapshot_id, persona, provider, model) DO UPDATE SET output = EXCLUDED.output, grounded = EXCLUDED.grounded, "
                            "chunk_ids = EXCLUDED.chunk_ids, status = 'ok', error = NULL",
                            (sid, name, adapter.provider, adapter.model, Jsonb(output), Jsonb(grounded), Jsonb(ids)))
                log.info("%s/%s: %s in, %s out, $%.4f", name, adapter.provider, r.input_tokens, r.output_tokens, r.cost_usd)
            conn.commit()
    wanted = {(n, a.provider, a.model) for a in adapters for n in persona_names}
    return [a for a in load_analyses(conn, sid) if (a["persona"], a["provider"], a["model"]) in wanted]


async def run_personas(conn: psycopg.Connection, sid: str, persona_names: list[str], adapter: Adapter, embedder) -> list[dict]:
    return await run_grid(conn, sid, persona_names, [adapter], embedder)
