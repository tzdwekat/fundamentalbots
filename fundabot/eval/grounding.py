"""Grounding: every evidence id must resolve; every number must match a metric or appear in a retrieved chunk."""
import re
from copy import deepcopy

from fundabot.analytics.metrics import Metric
from fundabot.retrieval.chunk import Chunk

NUMBER = re.compile(r"(\$)?\s?(-?\d[\d,]*(?:\.\d+)?)\s?(%|(?:bn|billion|mm|million|thousand|trillion|tn|b|m|k|x)\b)?", re.I)
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "tn": 1e12, "trillion": 1e12}


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """(matched text, normalized value). Skips years and bare small integers (counts, ordinals)."""
    out = []
    for m in NUMBER.finditer(text):
        dollar, num, unit = m.group(1), m.group(2), (m.group(3) or "").lower()
        raw = float(num.replace(",", ""))
        is_int = "." not in num
        if not dollar and not unit:
            if is_int and (1900 <= raw <= 2100 or abs(raw) < 1000):
                continue
        value = raw / 100 if unit == "%" else raw * MULT.get(unit, 1.0)
        out.append((m.group(0).strip(), value))
    return out


def _supported(text: str, value: float, values: list[float], chunk_blob: str) -> bool:
    is_pct = text.endswith("%")
    for v in values:
        if is_pct and abs(v - value) <= 0.005:
            return True
        if v != 0 and abs(v - value) / abs(v) <= 0.01:
            return True
        if v == 0 and value == 0:
            return True
    return re.sub(r"\s+", "", text).lower() in chunk_blob


def ground(output: dict, metrics: list[Metric], chunks: list[Chunk]) -> dict:
    g = deepcopy(output)
    exact = {f"metric:{m.metric_id}@{m.period}" for m in metrics}
    any_period = {f"metric:{m.metric_id}" for m in metrics}
    chunk_ids = {f"chunk:{c.id}" for c in chunks}
    values = [m.value for m in metrics if m.value is not None]
    chunk_blob = re.sub(r"\s+", "", " ".join(c.text for c in chunks)).lower()

    def resolve(eid: str) -> bool:
        return eid in exact or eid in any_period or eid in chunk_ids

    def check_numbers(text: str) -> list[str]:
        return [t for t, v in extract_numbers(text) if not _supported(t, v, values, chunk_blob)]

    kept, dropped, n_total, n_unsupported = [], [], 0, 0
    for c in g["claims"]:
        c["evidence_resolved"] = all(resolve(e) for e in c["evidence"]) and bool(c["evidence"])
        c["unsupported_numbers"] = check_numbers(c["text"])
        n_total += len(extract_numbers(c["text"]))
        n_unsupported += len(c["unsupported_numbers"])
        (kept if c["evidence_resolved"] else dropped).append(c)
    g["claims"], g["dropped_claims"] = kept, dropped
    g["thesis_unsupported_numbers"] = check_numbers(g["thesis"])
    g["risks_unsupported_numbers"] = [n for r in g["key_risks"] + g["what_would_change_my_mind"] for n in check_numbers(r)]
    extra_total = len(extract_numbers(g["thesis"])) + sum(len(extract_numbers(r)) for r in g["key_risks"] + g["what_would_change_my_mind"])
    n_total += extra_total
    n_unsupported += len(g["thesis_unsupported_numbers"]) + len(g["risks_unsupported_numbers"])
    g["stats"] = {"claims_total": len(output["claims"]), "claims_dropped": len(dropped), "numbers_total": n_total,
                  "numbers_unsupported": n_unsupported, "hallucination_rate": (n_unsupported / n_total) if n_total else 0.0}
    return g
