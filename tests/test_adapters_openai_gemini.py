import json
from types import SimpleNamespace as NS

import pytest

from fundabot.agents import base, schemas
from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.agents.adapters.gemini import PRICES as GPRICES, GeminiAdapter
from fundabot.agents.adapters.openai import PRICES as OPRICES, OpenAIAdapter
from fundabot.config import settings

GOOD = {"thesis": "t", "claims": [], "key_risks": [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}


async def test_openai_adapter_parses_response(monkeypatch):
    a = OpenAIAdapter("gpt-5.4")
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return NS(output_text=json.dumps(GOOD), status="completed", usage=NS(input_tokens=80_000, output_tokens=2_000), model="gpt-5.4",
                  output=[NS(type="message", content=[NS(type="output_text", text=json.dumps(GOOD))])])
    monkeypatch.setattr(a._client.responses, "create", fake_create)
    r = await a.complete("sys", "user", schemas.AGENT_OUTPUT_SCHEMA)
    assert r.output == GOOD and abs(r.cost_usd - (2.5 * 80_000 / 1e6 + 15 * 2_000 / 1e6)) < 1e-9 and r.model == "gpt-5.4"
    assert captured["instructions"] == "sys" and captured["input"] == "user"
    fmt = captured["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["schema"] == schemas.AGENT_OUTPUT_SCHEMA and a.provider == "openai"


async def test_openai_adapter_raises_on_incomplete(monkeypatch):
    a = OpenAIAdapter("gpt-5.4")

    async def fake_create(**kw):
        return NS(output_text="", status="incomplete", usage=NS(input_tokens=1, output_tokens=1), model="gpt-5.4", output=[],
                  incomplete_details=NS(reason="max_output_tokens"))
    monkeypatch.setattr(a._client.responses, "create", fake_create)
    with pytest.raises(RuntimeError):
        await a.complete("s", "u", schemas.AGENT_OUTPUT_SCHEMA)


async def test_gemini_adapter_parses_response(monkeypatch):
    a = GeminiAdapter("gemini-3.1-pro-preview")
    captured = {}

    async def fake_generate(**kw):
        captured.update(kw)
        return NS(text=json.dumps(GOOD), usage_metadata=NS(prompt_token_count=50_000, candidates_token_count=1_500, thoughts_token_count=500),
                  candidates=[NS(finish_reason="STOP")])
    monkeypatch.setattr(a._client.aio.models, "generate_content", fake_generate)
    r = await a.complete("sys", "user", schemas.AGENT_OUTPUT_SCHEMA)
    assert r.output == GOOD and r.input_tokens == 50_000 and r.output_tokens == 2_000
    assert abs(r.cost_usd - (2.0 * 50_000 / 1e6 + 12.0 * 2_000 / 1e6)) < 1e-9 and a.provider == "gemini"
    assert captured["model"] == "gemini-3.1-pro-preview" and captured["contents"] == "user"
    cfg = captured["config"]
    assert cfg.system_instruction == "sys" and cfg.response_mime_type == "application/json" and cfg.response_json_schema == schemas.AGENT_OUTPUT_SCHEMA


def test_prices_tables():
    assert OPRICES["gpt-5.4"] == (2.5, 15.0) and OPRICES["gpt-5.4-mini"] == (0.75, 4.5) and OPRICES["gpt-5.4-pro"] == (30.0, 180.0)
    assert GPRICES["gemini-3.1-pro-preview"] == (2.0, 12.0) and GPRICES["gemini-3.8-flash"] == (0.75, 3.75)
    assert OpenAIAdapter.cost("unknown-model", 1e6, 0) == 30.0 and GeminiAdapter.cost("unknown", 1e6, 0) == 2.0


class Boom(FakeAdapter):
    provider, model = "boom", "boom-model"

    async def complete(self, system, user, schema):
        self.calls += 1
        raise RuntimeError("provider down")


@pytest.mark.db
async def test_run_batch_logs_each_success_and_returns_exceptions(db_conn):
    ok1, ok2, bad = FakeAdapter([GOOD]), FakeAdapter([GOOD]), Boom([GOOD])
    calls = [(ok1, "bull", "s", "u", schemas.AGENT_OUTPUT_SCHEMA), (bad, "bear", "s", "u", schemas.AGENT_OUTPUT_SCHEMA), (ok2, "macro", "s", "u", schemas.AGENT_OUTPUT_SCHEMA)]
    results = await base.run_batch(db_conn, "snap", calls)
    assert results[0].output == GOOD and isinstance(results[1], RuntimeError) and results[2].output == GOOD
    with db_conn.cursor() as cur:
        cur.execute("SELECT persona FROM model_calls ORDER BY persona")
        assert [r[0] for r in cur.fetchall()] == ["bull", "macro"]


class Pricey(FakeAdapter):
    def estimate_cost(self, input_chars):
        return 0.5


@pytest.mark.db
async def test_run_batch_budget_checks_whole_batch(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "fundabot_monthly_budget_usd", 1.0)
    adapters = [Pricey([GOOD]) for _ in range(3)]
    with pytest.raises(base.BudgetExceeded):
        await base.run_batch(db_conn, "snap", [(a, "bull", "s", "u", schemas.AGENT_OUTPUT_SCHEMA) for a in adapters])
    assert sum(a.calls for a in adapters) == 0
