"""The agent output contract. Field names are fixed: Phase 3's rebuttal and judge consume them."""

AGENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string", "description": "One paragraph."},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}, "description": "metric:<id>[@<period>] or chunk:<id> ids from the inputs"},
                    "direction": {"type": "string", "enum": ["positive", "negative"]},
                    "weight": {"type": "integer", "description": "1 (minor) to 5 (thesis-defining)"},
                },
                "required": ["text", "evidence", "direction", "weight"],
                "additionalProperties": False,
            },
        },
        "key_risks": {"type": "array", "items": {"type": "string"}},
        "what_would_change_my_mind": {"type": "array", "items": {"type": "string"}},
        "score": {"type": "integer", "description": "-10 (strong avoid/short) to 10 (strong own)"},
        "confidence": {"type": "number", "description": "0 to 1"},
    },
    "required": ["thesis", "claims", "key_risks", "what_would_change_my_mind", "score", "confidence"],
    "additionalProperties": False,
}


class SchemaError(ValueError):
    pass


def validate(output: dict) -> dict:
    """Structural and range checks beyond what structured output guarantees."""
    props = AGENT_OUTPUT_SCHEMA["properties"]
    if not isinstance(output, dict):
        raise SchemaError("output is not an object")
    extra = set(output) - set(props)
    missing = set(AGENT_OUTPUT_SCHEMA["required"]) - set(output)
    if extra or missing:
        raise SchemaError(f"extra={sorted(extra)} missing={sorted(missing)}")
    if not isinstance(output["thesis"], str) or not isinstance(output["claims"], list):
        raise SchemaError("thesis must be a string and claims a list")
    for k in ("key_risks", "what_would_change_my_mind"):
        if not isinstance(output[k], list) or not all(isinstance(x, str) for x in output[k]):
            raise SchemaError(f"{k} must be a list of strings")
    if not isinstance(output["score"], int) or not -10 <= output["score"] <= 10:
        raise SchemaError("score must be an integer in [-10, 10]")
    if not isinstance(output["confidence"], (int, float)) or not 0 <= output["confidence"] <= 1:
        raise SchemaError("confidence must be in [0, 1]")
    for i, c in enumerate(output["claims"]):
        if not isinstance(c, dict) or set(c) != {"text", "evidence", "direction", "weight"}:
            raise SchemaError(f"claim {i} has wrong fields")
        if c["direction"] not in ("positive", "negative"):
            raise SchemaError(f"claim {i} direction")
        if not isinstance(c["weight"], int) or not 1 <= c["weight"] <= 5:
            raise SchemaError(f"claim {i} weight must be an integer in [1, 5]")
        if not isinstance(c["evidence"], list) or not all(isinstance(e, str) for e in c["evidence"]):
            raise SchemaError(f"claim {i} evidence must be a list of ids")
    return output
