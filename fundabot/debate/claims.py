"""Claim pool for the debate: ids, evidence text lookup, embedding dedupe, opposing-claim selection."""
import numpy as np

from fundabot.agents.context import metric_line
from fundabot.analytics.metrics import Metric
from fundabot.retrieval.chunk import Chunk


def build_pool(analyses: list[dict]) -> list[dict]:
    """One claim dict per kept (grounded) claim; id = {agent}#{index}."""
    pool = []
    for a in analyses:
        for n, c in enumerate(a["grounded"]["claims"]):
            pool.append({"id": f"{a['agent']}#{n}", "persona": a["persona"], "provider": a["provider"], "agent": a["agent"],
                         "text": c["text"], "evidence": list(c["evidence"]), "direction": c["direction"], "weight": c["weight"],
                         "conceded_by": [], "refutations": [], "withdrawn": False})
    return pool


def evidence_text(ids: list[str], metrics: list[Metric], chunks_by_id: dict[str, Chunk], accessions: list[str]) -> dict[str, str]:
    """Readable text for each resolvable evidence id; unresolvable ids are omitted."""
    out = {}
    by_id: dict[str, list[Metric]] = {}
    for m in metrics:
        by_id.setdefault(m.metric_id, []).append(m)
    for eid in ids:
        kind, _, rest = eid.partition(":")
        if kind == "metric":
            mid, _, period = rest.partition("@")
            rows = [m for m in by_id.get(mid, []) if not period or m.period == period]
            if rows:
                out[eid] = "\n".join(metric_line(m) for m in sorted(rows, key=lambda m: m.period))
        elif kind == "chunk" and rest in chunks_by_id:
            out[eid] = chunks_by_id[rest].text
        elif kind == "filing" and rest in accessions:
            out[eid] = f"filing {rest}"
    return out


def dedupe(items: list[dict], embedder, key: str = "text", threshold: float = 0.85) -> list[dict]:
    """Greedy: highest weight first; keep an item unless it is within `threshold` cosine of one already kept."""
    if not items:
        return []
    order = sorted(range(len(items)), key=lambda i: -(items[i].get("weight") or len(items[i].get("evidence", []))))
    vecs = np.array(embedder.embed([items[i][key] for i in order]), dtype=float)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    kept, kept_vecs = [], []
    for i, v in zip(order, vecs):
        if kept_vecs and max(float(v @ k) for k in kept_vecs) >= threshold:
            continue
        kept.append(items[i])
        kept_vecs.append(v)
    return kept


def top_opposing(pool: list[dict], agent: str, score: int, embedder, k: int = 10) -> list[dict]:
    """Other agents' non-withdrawn claims whose direction opposes `score` (both when 0), deduplicated, by weight, top k."""
    want = {"negative"} if score > 0 else {"positive"} if score < 0 else {"positive", "negative"}
    cands = [c for c in pool if c["agent"] != agent and not c["withdrawn"] and c["direction"] in want]
    return sorted(dedupe(cands, embedder), key=lambda c: -c["weight"])[:k]
