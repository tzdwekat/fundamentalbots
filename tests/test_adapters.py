import pytest

from fundabot.agents import base, schemas
from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.config import settings

GOOD = {
    "thesis": "Strong business.",
    "claims": [{"text": "Gross margin is high.", "evidence": ["metric:gross_margin@FY2024"], "direction": "positive", "weight": 4}],
    "key_risks": ["China exposure"],
    "what_would_change_my_mind": ["Margin compression"],
    "score": 6,
    "confidence": 0.7,
}


def test_schema_accepts_good_output():
    assert schemas.validate(dict(GOOD)) == GOOD


def test_schema_rejects_extra_field_and_bad_weight():
    with pytest.raises(schemas.SchemaError):
        schemas.validate({**GOOD, "extra": 1})
    bad = {**GOOD, "claims": [{**GOOD["claims"][0], "weight": 9}]}
    with pytest.raises(schemas.SchemaError):
        schemas.validate(bad)
    with pytest.raises(schemas.SchemaError):
        schemas.validate({**GOOD, "score": 11})
    with pytest.raises(schemas.SchemaError):
        schemas.validate({**GOOD, "claims": [{**GOOD["claims"][0], "direction": "sideways"}]})
    with pytest.raises(schemas.SchemaError):
        schemas.validate({k: v for k, v in GOOD.items() if k != "key_risks"})


def test_schema_has_required_and_no_additional():
    s = schemas.AGENT_OUTPUT_SCHEMA
    assert s["additionalProperties"] is False and set(s["required"]) == set(GOOD)
    assert s["properties"]["claims"]["items"]["properties"]["direction"]["enum"] == ["positive", "negative"]


async def test_fake_adapter_counts_calls():
    a = FakeAdapter([GOOD])
    r = await a.complete("sys", "user", schemas.AGENT_OUTPUT_SCHEMA)
    assert r.output == GOOD and r.cost_usd == 0 and a.calls == 1 and a.provider == "fake"


@pytest.mark.db
async def test_budget_exceeded_refuses(db_conn, monkeypatch):
    monkeypatch.setattr(settings, "fundabot_monthly_budget_usd", 5.0)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO model_calls (provider, model, cost_usd) VALUES ('anthropic', 'm', 5.0)")
    db_conn.commit()
    a = FakeAdapter([GOOD])
    with pytest.raises(base.BudgetExceeded):
        await base.call_logged(db_conn, a, "snap", "bull", "sys", "user", schemas.AGENT_OUTPUT_SCHEMA)
    assert a.calls == 0


@pytest.mark.db
async def test_call_logged_inserts_row(db_conn):
    a = FakeAdapter([GOOD])
    r = await base.call_logged(db_conn, a, "snap", "bull", "sys", "user text", schemas.AGENT_OUTPUT_SCHEMA)
    assert r.output == GOOD
    with db_conn.cursor() as cur:
        cur.execute("SELECT snapshot_id, persona, provider, model, prompt_sha256, input_tokens FROM model_calls")
        rows = cur.fetchall()
    assert len(rows) == 1 and rows[0][:4] == ("snap", "bull", "fake", "fake-model") and len(rows[0][4]) == 64


def test_anthropic_price_table_and_cost():
    from fundabot.agents.adapters.anthropic import PRICES, AnthropicAdapter

    assert PRICES["claude-sonnet-5-5"] == (2.0, 10.0) and PRICES["claude-opus-5-5"] == (4.0, 20.0)
    assert AnthropicAdapter.cost("claude-sonnet-5-5", 1_000_000, 100_000) == 2.0 + 1.0
