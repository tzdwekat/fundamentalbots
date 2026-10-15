from datetime import date

import pytest

from fundabot.agents import context, personas
from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag
from fundabot.retrieval.chunk import Chunk

OUT = {"thesis": "t", "claims": [{"text": "c", "evidence": ["metric:revenue@FY2024"], "direction": "positive", "weight": 3}],
       "key_risks": [], "what_would_change_my_mind": [], "score": 2, "confidence": 0.5}


def test_personas_load_all_five():
    ps = personas.load_all()
    assert set(ps) == {"bull", "bear", "skeptic", "quality", "macro"}
    for name, p in ps.items():
        assert p["name"] == name and len(p["bias"]) > 100 and len(p["retrieval_queries"]) >= 4 and p["reads_most"]


def test_system_prompt_contains_rules_and_bias():
    s = context.system_prompt(personas.load_all()["bear"])
    for rule in ("Cite a metric id or chunk id for every claim", "Never state a number that is not in the inputs", "insufficient data"):
        assert rule in s
    assert "metric:" in s and "chunk:" in s and personas.load_all()["bear"]["bias"][:60] in s


def test_user_message_metric_lines():
    ms = [
        Metric("revenue", "FY2024", 391035000000.0, "USD", "xbrl:Revenues", {}, [], "Revenues"),
        Metric("revenue_yoy", "Q4FY2024", 0.079, "ratio", "revenue / revenue same quarter prior FY - 1", {}, ["derived"]),
        Metric("gross_margin", "FY2024", None, "ratio", "gross_profit / revenue", {}, [], None, "tag_missing"),
    ]
    flags = [RedFlag("accruals_high", "high", ["metric:accruals_ratio@FY2024"], "accruals ratio 12%")]
    chunks = [Chunk("acc#item1a-3", "acc", "item1a", 3, "Risk text here.", 4)]
    snap = {"id": "s1", "ticker": "AAPL", "price_date": date(2026, 10, 14), "latest_close": 342.96, "accessions": ["acc"]}
    msg = context.build_user_message("AAPL", snap, ms, flags, chunks, ["No macro series are available in this build."])
    assert "metric:revenue@FY2024 = 391035000000.0 USD <- xbrl:Revenues" in msg
    assert "metric:revenue_yoy@Q4FY2024 = 0.079 ratio [derived]" in msg
    assert "metric:gross_margin@FY2024 = null (tag_missing)" in msg
    assert "accruals_high" in msg and "chunk:acc#item1a-3" in msg and "Risk text here." in msg
    assert "No macro series" in msg and "2026-10-14" in msg


def test_user_message_limits_periods():
    ms = [Metric("revenue", f"FY{y}", 1.0, "USD", "x") for y in range(2010, 2026)]
    msg = context.build_user_message("T", {"id": "s", "ticker": "T", "price_date": None, "latest_close": None, "accessions": []}, ms, [], [], [])
    assert "metric:revenue@FY2025" in msg and "metric:revenue@FY2021" in msg and "metric:revenue@FY2015" not in msg


@pytest.mark.db
async def test_runner_skips_cached(db_conn):
    from fundabot.agents.runner import run_personas
    from fundabot.orchestrate import snapshot
    from fundabot.retrieval.embed import FakeEmbedder

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)
    snapshot.store_metrics(db_conn, sid, [Metric("revenue", "FY2024", 1.0, "USD", "x")])
    adapter = FakeAdapter([OUT])
    first = await run_personas(db_conn, sid, ["bull", "bear", "skeptic", "quality", "macro"], adapter, FakeEmbedder())
    assert adapter.calls == 5 and len(first) == 5 and {a["persona"] for a in first} == {"bull", "bear", "skeptic", "quality", "macro"}
    second = await run_personas(db_conn, sid, ["bull", "bear", "skeptic", "quality", "macro"], adapter, FakeEmbedder())
    assert adapter.calls == 5 and len(second) == 5
    third = await run_personas(db_conn, sid, ["bull"], FakeAdapter([OUT]), FakeEmbedder())  # same provider/model -> cached
    assert len(third) == 1 and third[0]["output"] == OUT and third[0]["chunk_ids"] == []
