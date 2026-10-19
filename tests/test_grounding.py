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


def test_percentage_not_matched_loosely():
    ms = [Metric(f"ratio{i}", "FY2024", i / 100, "ratio", "x") for i in range(0, 100)]  # 0.00, 0.01, ... 0.99
    g = ground(out([claim("margin of 23.4%", ["metric:ratio5@FY2024"])]), ms, [])
    assert g["claims"][0]["unsupported_numbers"] == ["23.4%"]
    g2 = ground(out([claim("margin of 25.0%", ["metric:ratio5@FY2024"])]), ms, [])
    assert g2["claims"][0]["unsupported_numbers"] == []


def test_values_limited_to_shown_periods():
    ms = [Metric("revenue", f"FY{y}", 1.0, "USD", "x") for y in range(2015, 2026)] + [Metric("revenue", "FY2010", 420e9, "USD", "x")]
    g = ground(out([claim("revenue of $420 billion", ["metric:revenue@FY2025"])]), ms, [])
    assert g["claims"][0]["unsupported_numbers"] == ["$420 billion"]


def test_chunk_number_matches_across_unit_formats():
    chunks = [Chunk("a#item7-0", "a", "item7", 0, "We repurchased $2.5 billion of stock. Net sales were 2,500 and 1,234 (in millions).", 20)]
    g = ground(out([claim("Buybacks of $2.5B and sales of $2,500 million and $1.234 billion", ["chunk:a#item7-0"])]), METRICS, chunks)
    assert g["claims"][0]["unsupported_numbers"] == []


def test_filing_evidence_resolves():
    g = ground(out([claim("Late filer.", ["filing:acc-9"]), claim("x", ["filing:other"])]), METRICS, CHUNKS, accessions=["acc-9"])
    assert [c["text"] for c in g["claims"]] == ["Late filer."] and len(g["dropped_claims"]) == 1


def test_ranges_are_not_negative_numbers():
    ms = [Metric("revenue_yoy", "Q1FY2026", 0.157, "ratio", "x"), Metric("revenue_yoy", "Q2FY2026", 0.166, "ratio", "x"),
          Metric("buybacks", "FY2024", 95e9, "USD", "x"), Metric("buybacks", "FY2021", 78e9, "USD", "x")]
    assert [t for t, _ in extract_numbers("revenue up 16-17% YoY and buybacks of 78-95B")] == ["17%", "95B"] or \
           [t for t, _ in extract_numbers("revenue up 16-17% YoY and buybacks of 78-95B")] == ["16", "17%", "78", "95B"]
    g = ground(out([claim("revenue up 16-17% YoY and buybacks of 78-95B a year", ["metric:revenue_yoy@Q2FY2026"])]), ms, [])
    assert g["claims"][0]["unsupported_numbers"] == []
    assert [v for _, v in extract_numbers("fell -3.5% to -2.1B")] == [-0.035, -2.1e9]


def test_rounded_quotes_match_at_quoted_precision():
    ms = [Metric("net_debt_to_ebitda", "Q3FY2026", 0.2497, "ratio", "x"), Metric("accruals_ratio", "FY2025", 0.001483, "ratio", "x"),
          Metric("dividend_coverage", "FY2025", 6.40, "ratio", "x")]
    g = ground(out([claim("Net debt/EBITDA is 0.25x; accruals ratio 0.0015; dividend coverage 6x", ["metric:net_debt_to_ebitda@Q3FY2026"])]), ms, [])
    assert g["claims"][0]["unsupported_numbers"] == []  # all within half a unit of the quoted precision


def test_what_would_change_my_mind_is_exempt_from_number_checks():
    o = out([claim("a", ["metric:revenue@FY2024"])], risks=["P/E above 99x"])
    o["what_would_change_my_mind"] = ["FCF yield above 3.5%"]
    g = ground(o, METRICS, CHUNKS)
    assert g["risks_unsupported_numbers"] == ["99x"] and g["stats"]["numbers_total"] == 1


def test_inline_evidence_ids_are_not_numbers():
    # a model may write ids in prose; the CIK/accession digits inside them are not figures to verify
    text = "Supply risk is structural. Evidence: chunk:0000320193-25-000079#item8-18, metric:pe_5y_max@current; margin was 46.2%."
    assert [v for _, v in extract_numbers(text)] == [0.462]
