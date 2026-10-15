from fundabot.analytics.metrics import Metric
from fundabot.debate import claims
from fundabot.retrieval.chunk import Chunk
from fundabot.retrieval.embed import FakeEmbedder


def analysis(persona, provider, score, texts, direction="positive", weight=3):
    return {"persona": persona, "provider": provider, "agent": f"{persona}/{provider}", "model": "m",
            "grounded": {"score": score, "confidence": 0.5,
                         "claims": [{"text": t, "evidence": ["metric:revenue@FY2024"], "direction": direction, "weight": weight, "evidence_resolved": True, "unsupported_numbers": []} for t in texts]}}


def test_pool_ids_and_shape():
    pool = claims.build_pool([analysis("bull", "openai", 5, ["a", "b"]), analysis("bear", "gemini", -4, ["c"], "negative")])
    assert [c["id"] for c in pool] == ["bull/openai#0", "bull/openai#1", "bear/gemini#0"]
    c = pool[2]
    assert c == {"id": "bear/gemini#0", "persona": "bear", "provider": "gemini", "agent": "bear/gemini", "text": "c", "evidence": ["metric:revenue@FY2024"],
                 "direction": "negative", "weight": 3, "conceded_by": [], "refutations": [], "withdrawn": False}


def test_dedupe_keeps_highest_weight_of_identical_texts():
    items = [{"text": "same claim", "weight": 2, "id": "x"}, {"text": "same claim", "weight": 5, "id": "y"}, {"text": "a different claim entirely", "weight": 1, "id": "z"}]
    kept = claims.dedupe(items, FakeEmbedder())
    assert [k["id"] for k in kept] == ["y", "z"]
    assert claims.dedupe([], FakeEmbedder()) == []


def test_top_opposing_filters_direction_and_self():
    pool = claims.build_pool([
        analysis("bull", "openai", 5, ["bull says up"], "positive", 4),
        analysis("bear", "gemini", -4, ["bear says down 1", "bear says down 2"], "negative", 3),
        analysis("skeptic", "anthropic", 0, ["skeptic worry"], "negative", 5),
        analysis("quality", "anthropic", 6, ["quality likes it"], "positive", 2),
    ])
    opp = claims.top_opposing(pool, "bull/openai", 5, FakeEmbedder(), k=10)
    assert [c["id"] for c in opp] == ["skeptic/anthropic#0", "bear/gemini#0", "bear/gemini#1"]  # negatives only, by weight, not self
    opp2 = claims.top_opposing(pool, "bear/gemini", -4, FakeEmbedder(), k=1)
    assert [c["id"] for c in opp2] == ["bull/openai#0"]
    both = claims.top_opposing(pool, "skeptic/anthropic", 0, FakeEmbedder())
    assert len(both) == 4


def test_evidence_text_lookup():
    ms = [Metric("revenue", "FY2024", 391e9, "USD", "xbrl:Revenues"), Metric("revenue", "FY2023", 383e9, "USD", "xbrl:Revenues"), Metric("pe", "current", 30.0, "ratio", "price / eps_ttm")]
    chunks = {"acc#item7-0": Chunk("acc#item7-0", "acc", "item7", 0, "MD&A text", 3)}
    ev = claims.evidence_text(["metric:revenue@FY2024", "metric:pe", "chunk:acc#item7-0", "filing:acc", "metric:nope"], ms, chunks, ["acc"])
    assert ev["metric:revenue@FY2024"] == "metric:revenue@FY2024 = 391000000000.0 USD <- xbrl:Revenues"
    assert "metric:pe@current = 30.0 ratio" in ev["metric:pe"] and ev["chunk:acc#item7-0"] == "MD&A text" and ev["filing:acc"] == "filing acc"
    assert "metric:nope" not in ev
