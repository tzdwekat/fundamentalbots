"""The cited move narrative: message blocks, grounding of ids and numbers, caching by inputs hash."""
from datetime import date, datetime, timezone

import pytest

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.analytics import moves
from fundabot.analytics.metrics import Metric
from fundabot.analytics.technicals import Event
from fundabot.retrieval.chunk import Chunk

NEWS = [{"id": "n1", "published": datetime(2026, 9, 12, 14, tzinfo=timezone.utc), "publisher": "Reuters", "site": "r.com", "title": "Apple shares fall after tariff ruling",
         "url": "http://r/1", "text": "Shares dropped 4.1% on Friday after the ruling; analysts expect a $2 billion cost.", "kind": "news"}]
CHUNK = Chunk("0000320193-26-000077#item2-3", "0000320193-26-000077", "item2", 3, "Net sales increased 8% year over year driven by iPhone.", 20)
MOVES = [{"id": "price:2026-09-12", "date": date(2026, 9, 12), "close": 220.0, "ret": -0.041, "events": [Event(date(2026, 9, 12), "news", "news:n1", "Apple shares fall after tariff ruling", "http://r/1"),
                                                                                                        Event(date(2026, 9, 11), "filing", "filing:0000320193-26-000077", "8-K", "http://sec")]}]
STATS = {"asof": date(2026, 10, 17), "close": 240.0, "ret_1m": 0.05, "ret_3m": -0.118, "ret_6m": 0.2, "ret_12m": 0.3, "ma50": 230.0, "ma200": 210.0, "drawdown_52w": -0.03,
         "high_52w": 247.0, "low_52w": 180.0, "dist_52w_low": 0.33, "above_ma50": True, "above_ma200": True, "ma50_slope_20d": 0.02, "vol_20d": 0.25, "n_days": 1200}
METRICS = [Metric("revenue_yoy", "Q3FY2026", 0.08, "ratio", "x"), Metric("eps_surprise", "Q3FY2026", 0.06, "ratio", "x", {}, ["vendor_fmp"])]


def inputs(snapshot_id="snapA"):
    return {"window_days": 90, "snapshot_id": snapshot_id, "stats": STATS, "moves": MOVES, "news": NEWS, "metrics": METRICS, "chunks": [CHUNK]}


def test_message_has_blocks_and_ids():
    msg = moves.build_message(inputs())
    for block in ("PRICE_STATS", "LARGEST_MOVES", "NEWS", "EARNINGS", "EXCERPTS"):
        assert block in msg
    assert "price:2026-09-12" in msg and "news:n1" in msg and "filing:0000320193-26-000077" in msg and "chunk:0000320193-26-000077#item2-3" in msg
    assert "ret_3m = -11.8%" in msg and "metric:eps_surprise@Q3FY2026" in msg and "Reuters" in msg
    assert "six sentences" in moves.system_prompt() and "no advice" in moves.system_prompt().lower()


def test_inputs_sha_ignores_quiet_changes():
    a, b = moves.inputs_sha(inputs()), moves.inputs_sha(inputs())
    assert a == b and len(a) == 16
    changed = inputs(); changed["stats"] = {**STATS, "close": 241.0}
    assert moves.inputs_sha(changed) == a  # a quiet day does not stale the narrative
    assert moves.inputs_sha(inputs("snapB")) != a
    more_news = inputs(); more_news["news"] = NEWS + [{**NEWS[0], "id": "n2"}]
    assert moves.inputs_sha(more_news) != a


def test_ground_drops_unresolvable_and_unsupported():
    out = {"sentences": [
        {"text": "The stock fell 12% over three months, with the largest drop on the tariff ruling.", "evidence": ["price:2026-09-12", "news:n1"]},
        {"text": "Revenue grew 8% year over year.", "evidence": ["metric:revenue_yoy@Q3FY2026"]},
        {"text": "A buyback of $90 billion was announced.", "evidence": ["news:zzz"]},
        {"text": "Margins expanded 3.5%.", "evidence": ["chunk:0000320193-26-000077#item2-3"]},
    ], "one_line": "Down on tariffs, recovering on results."}
    g = moves.ground_narrative(out, inputs())
    kept = [s["text"] for s in g["sentences"]]
    assert kept == ["The stock fell 12% over three months, with the largest drop on the tariff ruling.", "Revenue grew 8% year over year."]
    assert [d["reason"] for d in g["dropped"]] == ["unresolved: news:zzz", "unsupported: 3.5%"]
    assert g["one_line"] == out["one_line"] and all(s["unsupported_numbers"] == [] for s in g["sentences"])


def test_number_from_news_text_is_supported():
    out = {"sentences": [{"text": "Analysts put the tariff cost at $2 billion and the day's drop at 4.1%.", "evidence": ["news:n1", "price:2026-09-12"]}], "one_line": "x"}
    g = moves.ground_narrative(out, inputs())
    assert len(g["sentences"]) == 1 and g["dropped"] == []
    bad = {"sentences": [{"text": "Analysts put the tariff cost at $5 billion.", "evidence": ["news:n1"]}], "one_line": "x"}
    assert moves.ground_narrative(bad, inputs())["dropped"][0]["reason"] == "unsupported: $5 billion"


GOOD = {"sentences": [{"text": "The stock fell 12% over three months.", "evidence": ["price:2026-09-12"]}], "one_line": "down"}


@pytest.mark.db
async def test_ensure_narrative_caches_by_inputs_sha(db_conn):
    a = FakeAdapter([GOOD, GOOD, GOOD])
    r1 = await moves.ensure_narrative(db_conn, a, 320193, 90, inputs())
    assert a.calls == 1 and r1["output"]["sentences"][0]["text"].startswith("The stock fell") and r1["stale"] is False and r1["dropped"] == 0
    r2 = await moves.ensure_narrative(db_conn, a, 320193, 90, inputs())
    assert a.calls == 1 and r2["output"] == r1["output"]
    assert moves.load(db_conn, 320193, 90)["inputs_sha"] == moves.inputs_sha(inputs())
    assert moves.stale(db_conn, 320193, 90, moves.inputs_sha(inputs("snapB"))) is True and moves.stale(db_conn, 320193, 90, moves.inputs_sha(inputs())) is False
    await moves.ensure_narrative(db_conn, a, 320193, 90, inputs(), force=True)
    assert a.calls == 2
    assert moves.load(db_conn, 1, 90) is None


def test_stats_key_names_are_not_numbers():
    out = {"sentences": [{"text": "The excess_1m return was -2.3% while ret_12m stayed positive.", "evidence": ["price:2026-09-12"]}], "one_line": "x"}
    assert moves.ground_narrative(out, inputs())["dropped"][0]["reason"] == "unsupported: -2.3%"  # the key names vanish; the real number is still checked


def test_one_line_numbers_are_checked():
    out = {"sentences": [], "one_line": "Up 5.0% over the window on product news."}
    assert moves.ground_narrative(out, inputs())["one_line"] == "Up 5.0% over the window on product news."  # ret_1m = 0.05
    bad = {"sentences": [], "one_line": "Up 27% over the window."}
    g = moves.ground_narrative(bad, inputs())
    assert g["one_line"] == "" and g["one_line_unsupported"] == ["27%"]


def test_inline_ids_of_every_kind_are_not_numbers():
    out = {"sentences": [{"text": "The 8-K filing:0000320193-26-000077 and news:n1 preceded the price:2026-09-12 drop of 4.1%.", "evidence": ["filing:0000320193-26-000077", "news:n1", "price:2026-09-12"]}], "one_line": ""}
    assert moves.ground_narrative(out, inputs())["dropped"] == []
