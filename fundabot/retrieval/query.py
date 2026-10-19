import numpy as np
import psycopg

from fundabot.retrieval.chunk import Chunk, chunk_section
from fundabot.retrieval.embed import Embedder


def embed_missing(conn: psycopg.Connection, embedder: Embedder, accessions: list[str]) -> int:
    """Chunk and embed every section of each accession that has no chunks yet. Returns chunks added."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT accession FROM chunks WHERE accession = ANY(%s)", (accessions,))
        done = {r[0] for r in cur.fetchall()}
    added = 0
    for acc in accessions:
        if acc in done:
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT item, text FROM filing_sections WHERE accession = %s", (acc,))
            sections = cur.fetchall()
        chunks = [c for item, text in sections for c in chunk_section(acc, item, text)]
        if not chunks:
            continue
        before = embedder.total_tokens
        vectors = embedder.embed([c.text for c in chunks])
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO chunks (id, accession, item, n, text, token_count, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                            [(c.id, c.accession, c.item, c.n, c.text, c.token_count, np.array(v, dtype=np.float32)) for c, v in zip(chunks, vectors)])
            used = embedder.total_tokens - before
            cur.execute("INSERT INTO model_calls (persona, provider, model, input_tokens, cost_usd) VALUES ('embed', 'embedding', %s, %s, %s)",
                        (embedder.model, used, embedder.cost_usd(used)))
        conn.commit()
        added += len(chunks)
    return added


def dedupe_boilerplate(chunks: list[Chunk], accession_rank: dict[str, int]) -> list[Chunk]:
    """Drop chunks whose first 200 normalized characters repeat an already-kept chunk (10-Q boilerplate across quarters);
    the chunk from the newest accession (lowest rank) wins. Order of the input is preserved otherwise."""
    import re
    ranked = sorted(chunks, key=lambda c: accession_rank.get(c.accession, 10**6))
    seen, keep_ids = {}, set()
    for c in ranked:
        key = re.sub(r"\s+", " ", c.text[:200]).strip().lower()
        if key in seen:
            continue
        seen[key] = c.id
        keep_ids.add(c.id)
    return [c for c in chunks if c.id in keep_ids]


def retrieve(conn: psycopg.Connection, embedder: Embedder, accessions: list[str], queries: list[str], top_k: int = 8, max_tokens: int = 20000,
             exclude_ids=frozenset()) -> list[Chunk]:
    """Top-k chunks per query by cosine similarity within the accessions, deduplicated, best first, capped by tokens."""
    best: dict[str, tuple[float, Chunk]] = {}
    for vec in embedder.embed(queries):
        v = np.array(vec, dtype=np.float32)
        with conn.cursor() as cur:
            cur.execute("SELECT id, accession, item, n, text, token_count, 1 - (embedding <=> %s) AS score FROM chunks "
                        "WHERE accession = ANY(%s) AND embedding IS NOT NULL ORDER BY embedding <=> %s LIMIT %s", (v, accessions, v, top_k))
            for cid, acc, item, n, text, tokens, score in cur.fetchall():
                if cid in exclude_ids:
                    continue
                if cid not in best or score > best[cid][0]:
                    best[cid] = (float(score), Chunk(cid, acc, item, n, text, tokens))
    out, used = [], 0
    for score, chunk in sorted(best.values(), key=lambda s: -s[0]):
        if used + chunk.token_count > max_tokens:
            break
        out.append(chunk)
        used += chunk.token_count
    return out


def load_chunks(conn: psycopg.Connection, ids: list[str]) -> dict[str, Chunk]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, accession, item, n, text, token_count FROM chunks WHERE id = ANY(%s)", (ids,))
        return {r[0]: Chunk(*r) for r in cur.fetchall()}
