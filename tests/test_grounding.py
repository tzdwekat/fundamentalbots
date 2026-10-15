from fundabot.analytics.metrics import Metric
from fundabot.eval.grounding import extract_numbers, ground
from fundabot.retrieval.chunk import Chunk

METRICS = [
    Metric("revenue", "FY2024", 391035000000.0, "USD", "x"),
    Metric("gross_margin", "FY2024", 0.4621, "ratio", "x"),
    Metric("pe", "current", 39.38, "ratio", "x"),
    Metric("eps_diluted", "FY2024", 6.08, "USD/share", "x"),
]
CHUNKS = [Chunk("acc#item7-2", "acc", "item7", 2, "The Company repurchased $2.5 billion of stock and holds 14 patents.", 12)]


def out(claims, thesis="t", risks=None):
    return {"thesis": thesis, "claims": claims, "key_risks": risks or [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}


def claim(text, evidence):
    return {"text": text, "evidence": evidence, "direction": "positive", "weight": 3}


def test_unresolved_evidence_drops_claim():
    g = ground(out([claim("a", ["metric:revenue@FY2024"]), claim("b", ["metric:made_up@FY2024"]), claim("c", ["chunk:acc#item7-99"])]), METRICS, CHUNKS)
    assert [c["text"] for c in g["claims"]] == ["a"] and [c["text"] for c in g["dropped_claims"]] == ["b", "c"]
    assert g["stats"]["claims_total"] == 3 and g["stats"]["claims_dropped"] == 2
    assert g["claims"][0]["evidence_resolved"] is True


def test_metric_period_evidence_resolves():
    g = ground(out([claim("a", ["metric:revenue@FY2024", "metric:pe", "chunk:acc#item7-2"])]), METRICS, CHUNKS)
    assert len(g["claims"]) == 1 and g["dropped_claims"] == []


def test_number_in_metrics_supported():
    g = ground(out([claim("gross margin of 46.2% and revenue of $391.0 billion", ["metric:gross_margin@FY2024"])]), METRICS, CHUNKS)
    assert g["claims"][0]["unsupported_numbers"] == [] and g["stats"]["numbers_total"] == 2


def test_number_in_chunk_text_is_grounded():
    g = ground(out([claim("Buybacks of $2.5 billion in the year", ["chunk:acc#item7-2"])]), METRICS, CHUNKS)
    assert g["claims"][0]["unsupported_numbers"] == []


def test_years_and_small_ints_ignored():
    g = ground(out([claim("In 2024, over 3 quarters, the top 2 segments grew", ["metric:revenue@FY2024"])]), METRICS, CHUNKS)
    assert g["stats"]["numbers_total"] == 0


def test_hallucinated_number_flagged():
    g = ground(out([claim("revenue of $420 billion and a P/E of 25x", ["metric:revenue@FY2024"])], thesis="EPS was $9.99."), METRICS, CHUNKS)
    assert g["claims"][0]["unsupported_numbers"] == ["$420 billion", "25x"]
    assert g["thesis_unsupported_numbers"] == ["$9.99"]
    assert g["stats"]["numbers_unsupported"] == 3 and abs(g["stats"]["hallucination_rate"] - 1.0) < 1e-9


def test_extract_numbers_normalization():
    assert [v for _, v in extract_numbers("$391,035 million")] == [391035e6]
    assert [v for _, v in extract_numbers("46.2%")] == [0.462]
    assert [v for _, v in extract_numbers("1.2x and 3.5bn and 700k")] == [1.2, 3.5e9, 700e3]
