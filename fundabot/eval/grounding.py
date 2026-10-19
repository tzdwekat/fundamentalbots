"""Grounding: every evidence id must resolve; every number must match a metric or appear in a retrieved chunk."""
import re
from copy import deepcopy

from fundabot.analytics.metrics import Metric
from fundabot.retrieval.chunk import Chunk

# a minus counts only when not preceded by a digit, so "16-17%" is a range, not "-17%"
NUMBER = re.compile(r"(\$)?\s?((?<!\d)-?\d[\d,]*(?:\.\d+)?)\s?(%|(?:bn|billion|mm|million|thousand|trillion|tn|b|m|k|x)\b)?", re.I)
EVIDENCE_ID = re.compile(r"\b(?:chunk|metric):\S+")  # ids written inline in prose carry accession/CIK digits that are not figures
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "tn": 1e12, "trillion": 1e12}


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """(matched text, normalized value). Skips years and bare small integers (counts, ordinals)."""
    out = []
    for m in NUMBER.finditer(EVIDENCE_ID.sub(" ", text)):
        dollar, num, unit = m.group(1), m.group(2), (m.group(3) or "").lower()
        raw = float(num.replace(",", ""))
        is_int = "." not in num
        if not dollar and not unit:
            if is_int and (1900 <= raw <= 2100 or abs(raw) < 1000):
                continue
        value = raw / 100 if unit == "%" else raw * MULT.get(unit, 1.0)
        out.append((m.group(0).strip(), value))
    return out


def _tolerance(text: str) -> float:
    """Half a unit of the last quoted digit, in normalized terms: "0.25x" -> 0.005, "46.2%" -> 0.0005, "$2.5 billion" -> 5e7."""
    m = NUMBER.search(text)
    num, unit = m.group(2), (m.group(3) or "").lower()
    decimals = len(num.split(".")[1]) if "." in num else 0
    scale = 0.01 if unit == "%" else MULT.get(unit, 1.0)
    return 0.5 * 10 ** (-decimals) * scale


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol or (b != 0 and abs(a - b) / abs(b) <= 0.01)


def _supported(text: str, value: float, values: list[float], chunk_values: list[float]) -> bool:
    """Within 1% (or the quoted precision) of a shown metric, or of a number in a retrieved chunk (also as thousands/millions table units)."""
    tol = _tolerance(text)
    if any(_close(value, v, tol) for v in values):
        return True
    return any(_close(value, cv * mult, tol) for cv in chunk_values for mult in (1.0, 1e3, 1e6))


def ground(output: dict, metrics: list[Metric], chunks: list[Chunk], accessions: list[str] = (), flags=()) -> dict:
    from fundabot.agents.context import select_metrics

    g = deepcopy(output)
    exact = {f"metric:{m.metric_id}@{m.period}" for m in metrics}
    any_period = {f"metric:{m.metric_id}" for m in metrics}
    chunk_ids = {f"chunk:{c.id}" for c in chunks}
    filing_ids = {f"filing:{a}" for a in accessions}
    values = [m.value for m in select_metrics(metrics, flags) if m.value is not None]  # only what the agent was shown counts as support
    chunk_values = [v for c in chunks for _, v in extract_numbers(c.text)]

    def resolve(eid: str) -> bool:
        return eid in exact or eid in any_period or eid in chunk_ids or eid in filing_ids

    def check_numbers(text: str) -> list[str]:
        return [t for t, v in extract_numbers(text) if not _supported(t, v, values, chunk_values)]

    kept, dropped, n_total, n_unsupported = [], [], 0, 0
    for c in g["claims"]:
        c["evidence_resolved"] = all(resolve(e) for e in c["evidence"]) and bool(c["evidence"])
        c["unsupported_numbers"] = check_numbers(c["text"])
        n_total += len(extract_numbers(c["text"]))
        n_unsupported += len(c["unsupported_numbers"])
        (kept if c["evidence_resolved"] else dropped).append(c)
    g["claims"], g["dropped_claims"] = kept, dropped
    g["thesis_unsupported_numbers"] = check_numbers(g["thesis"])
    # what_would_change_my_mind holds hypothetical thresholds, not quotes, so it is not number-checked
    g["risks_unsupported_numbers"] = [n for r in g["key_risks"] for n in check_numbers(r)]
    extra_total = len(extract_numbers(g["thesis"])) + sum(len(extract_numbers(r)) for r in g["key_risks"])
    n_total += extra_total
    n_unsupported += len(g["thesis_unsupported_numbers"]) + len(g["risks_unsupported_numbers"])
    g["stats"] = {"claims_total": len(output["claims"]), "claims_dropped": len(dropped), "numbers_total": n_total,
                  "numbers_unsupported": n_unsupported, "hallucination_rate": (n_unsupported / n_total) if n_total else 0.0}
    return g


def unsupported_numbers(text: str, metrics: list[Metric], chunks: list[Chunk], flags=()) -> list[str]:
    """Numbers in free text (e.g. a judge summary) that match neither a shown metric nor a retrieved chunk."""
    from fundabot.agents.context import select_metrics

    values = [m.value for m in select_metrics(metrics, flags) if m.value is not None]
    chunk_values = [v for c in chunks for _, v in extract_numbers(c.text)]
    return [t for t, v in extract_numbers(text) if not _supported(t, v, values, chunk_values)]
