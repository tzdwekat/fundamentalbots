import asyncio
import json
from datetime import date
from types import SimpleNamespace as NS

import pytest

from fundabot.agents import base, schemas
from fundabot.agents.adapters.anthropic import AnthropicAdapter
from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.agents.adapters.gemini import GeminiAdapter
from fundabot.agents.adapters.openai import OpenAIAdapter

GOOD = {"thesis": "t", "claims": [], "key_risks": [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}
LONG_PREFIX = "SHARED CONTEXT " * 1500  # ~4.5k tokens by any count


async def test_anthropic_sends_cached_system_block(monkeypatch):
    a = AnthropicAdapter("claude-sonnet-5-5")
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(GOOD))], model="claude-sonnet-5-5",
                  usage=NS(input_tokens=10_000, output_tokens=500, cache_read_input_tokens=30_000, cache_creation_input_tokens=0))
    monkeypatch.setattr(a._client.messages, "create", fake_create)
    r = await a.complete("TAIL", "user", schemas.AGENT_OUTPUT_SCHEMA, prefix=LONG_PREFIX)
    sysblocks = captured["system"]
    assert isinstance(sysblocks, list) and sysblocks[0]["text"] == LONG_PREFIX and sysblocks[0]["cache_control"] == {"type": "ephemeral"} and sysblocks[1]["text"] == "TAIL"
    assert r.cached_input_tokens == 30_000 and r.cache_write_tokens == 0
    assert abs(r.cost_usd - (10_000 / 1e6 * 2.0 + 30_000 / 1e6 * 0.2 + 500 / 1e6 * 10.0)) < 1e-9


async def test_anthropic_no_cache_control_for_short_prefix(monkeypatch):
    a = AnthropicAdapter("claude-sonnet-5-5")
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(GOOD))], model="m", usage=NS(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(a._client.messages, "create", fake_create)
    await a.complete("TAIL", "user", schemas.AGENT_OUTPUT_SCHEMA, prefix="short prefix")
    assert isinstance(captured["system"], str) and captured["system"].startswith("short prefix") and "TAIL" in captured["system"]


def test_anthropic_cost_with_cached_and_write_tokens():
    assert abs(AnthropicAdapter.cost("claude-sonnet-5-5", 40_000, 1_000, cached=30_000, cache_write=10_000)
               - ((40_000 - 30_000 - 10_000) / 1e6 * 2.0 + 30_000 / 1e6 * 0.2 + 10_000 / 1e6 * 2.5 + 1_000 / 1e6 * 10.0)) < 1e-9


async def test_openai_prefix_in_instructions_and_cached_cost(monkeypatch):
    a = OpenAIAdapter("gpt-5.4")
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return NS(output_text=json.dumps(GOOD), status="completed", output=[], model="gpt-5.4",
                  usage=NS(input_tokens=40_000, output_tokens=1_000, input_tokens_details=NS(cached_tokens=30_000)))
    monkeypatch.setattr(a._client.responses, "create", fake_create)
    r = await a.complete("TAIL", "user", schemas.AGENT_OUTPUT_SCHEMA, prefix=LONG_PREFIX)
    assert captured["instructions"].startswith(LONG_PREFIX) and captured["instructions"].endswith("TAIL")
    assert r.cached_input_tokens == 30_000 and abs(r.cost_usd - (10_000 / 1e6 * 2.5 + 30_000 / 1e6 * 0.25 + 1_000 / 1e6 * 15.0)) < 1e-9


async def test_gemini_long_prefix_uses_one_explicit_cache(monkeypatch):
    """Gemini's implicit cache misses whenever the user turn differs, so a long shared prefix becomes one explicit cache
    (created once per adapter per prefix) and the persona tail travels with the user turn."""
    a = GeminiAdapter("gemini-3.1-pro-preview")
    captured, created = {}, []

    async def fake_create(**kw):
        created.append(kw)
        return NS(name="cachedContents/abc", usage_metadata=NS(total_token_count=30_000))

    async def fake_generate(**kw):
        captured.update(kw)
        return NS(text=json.dumps(GOOD), usage_metadata=NS(prompt_token_count=40_000, candidates_token_count=1_000, thoughts_token_count=0, cached_content_token_count=30_000),
                  candidates=[NS(finish_reason="STOP")])
    monkeypatch.setattr(a._client.aio.caches, "create", fake_create)
    monkeypatch.setattr(a._client.aio.models, "generate_content", fake_generate)
    r = await a.complete("TAIL", "user", schemas.AGENT_OUTPUT_SCHEMA, prefix=LONG_PREFIX)
    r2 = await a.complete("TAIL2", "user2", schemas.AGENT_OUTPUT_SCHEMA, prefix=LONG_PREFIX)
    assert len(created) == 1 and created[0]["model"] == "gemini-3.1-pro-preview" and created[0]["config"].system_instruction == LONG_PREFIX
    assert captured["config"].cached_content == "cachedContents/abc" and captured["config"].system_instruction is None
    assert captured["contents"] == "TAIL2\n\nuser2"
    # creating the cache bills the prefix once at the normal rate; that lands on the call that created it, as cache-write tokens
    assert r.cache_write_tokens == 30_000 and abs(r.cost_usd - (10_000 / 1e6 * 2.0 + 30_000 / 1e6 * 0.2 + 30_000 / 1e6 * 2.0 + 1_000 / 1e6 * 12.0)) < 1e-9
    assert r2.cache_write_tokens == 0 and r2.cached_input_tokens == 30_000 and abs(r2.cost_usd - (10_000 / 1e6 * 2.0 + 30_000 / 1e6 * 0.2 + 1_000 / 1e6 * 12.0)) < 1e-9


async def test_gemini_short_prefix_is_concatenated(monkeypatch):
    a = GeminiAdapter("gemini-3.1-pro-preview")
    captured = {}

    async def fake_generate(**kw):
        captured.update(kw)
        return NS(text=json.dumps(GOOD), usage_metadata=NS(prompt_token_count=100, candidates_token_count=10, thoughts_token_count=0, cached_content_token_count=None),
                  candidates=[NS(finish_reason="STOP")])
    monkeypatch.setattr(a._client.aio.models, "generate_content", fake_generate)
    await a.complete("TAIL", "user", schemas.AGENT_OUTPUT_SCHEMA, prefix="short")
    assert captured["config"].system_instruction == "short\n\nTAIL" and captured["config"].cached_content is None


class Ordered(FakeAdapter):
    """Records when each call starts and finishes relative to the others."""
    provider, model = "anthropic", "m"

    def __init__(self, outputs, fail_first=False):
        super().__init__(outputs)
        self.events, self.fail_first = [], fail_first

    async def complete(self, system, user, schema, prefix=None):
        n = sum(1 for e in self.events if e[0] == "start")  # self.calls only advances after the await, so concurrent starts would share it
        self.events.append(("start", n))
        await asyncio.sleep(0.01)
        if self.fail_first and n == 0:
            self.calls += 1
            self.events.append(("fail", n))
            raise RuntimeError("warm-up failed")
        r = await super().complete(system, user, schema, prefix=prefix)
        self.events.append(("end", n))
        return r


@pytest.mark.db
async def test_run_batch_warm_first_order(db_conn):
    a = Ordered([GOOD])
    calls = [(a, f"p{i}", "s", "u", schemas.AGENT_OUTPUT_SCHEMA, LONG_PREFIX) for i in range(5)]
    results = await base.run_batch(db_conn, "snap", calls, warm_first=True)
    assert all(r.output == GOOD for r in results) and a.events[0] == ("start", 0) and a.events[1] == ("end", 0)  # first call completes before the rest start
    assert [e for e in a.events[2:] if e[0] == "start"] == [("start", i) for i in range(1, 5)]


@pytest.mark.db
async def test_warm_first_failure_still_runs_rest(db_conn):
    a = Ordered([GOOD], fail_first=True)
    calls = [(a, f"p{i}", "s", "u", schemas.AGENT_OUTPUT_SCHEMA, LONG_PREFIX) for i in range(3)]
    results = await base.run_batch(db_conn, "snap", calls, warm_first=True)
    assert isinstance(results[0], Exception) and results[1].output == GOOD and results[2].output == GOOD


@pytest.mark.db
async def test_model_calls_records_cached_tokens(db_conn):
    class Cached(FakeAdapter):
        provider, model = "anthropic", "m"

        async def complete(self, system, user, schema, prefix=None):
            r = await super().complete(system, user, schema, prefix=prefix)
            r.cached_input_tokens, r.cache_write_tokens = 30_000, 5_000
            return r
    await base.run_batch(db_conn, "snap", [(Cached([GOOD]), "bull", "s", "u", schemas.AGENT_OUTPUT_SCHEMA, "p")])
    with db_conn.cursor() as cur:
        cur.execute("SELECT cached_input_tokens, cache_write_tokens FROM model_calls")
        assert cur.fetchone() == (30_000, 5_000)


@pytest.mark.db
async def test_grid_warms_cache_first(db_conn):
    from fundabot.agents.runner import run_grid
    from fundabot.analytics.metrics import Metric
    from fundabot.orchestrate import snapshot
    from fundabot.retrieval.embed import FakeEmbedder

    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)
    snapshot.store_metrics(db_conn, sid, [Metric("revenue", "FY2024", 1.0, "USD", "x")])
    a = Ordered([GOOD])
    await run_grid(db_conn, sid, ["bull", "bear", "skeptic"], [a], FakeEmbedder())
    assert a.events[:2] == [("start", 0), ("end", 0)] and a.calls == 3
