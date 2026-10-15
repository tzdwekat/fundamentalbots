from datetime import date

import pytest

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.agents.runner import agent_key, run_grid
from fundabot.analytics.metrics import Metric
from fundabot.orchestrate import snapshot
from fundabot.retrieval.embed import FakeEmbedder

PERSONAS = ["bull", "bear", "skeptic", "quality", "macro"]
OUT = {"thesis": "t", "claims": [{"text": "c", "evidence": ["metric:revenue@FY2024"], "direction": "positive", "weight": 3}],
       "key_risks": [], "what_would_change_my_mind": [], "score": 2, "confidence": 0.5}


class Named(FakeAdapter):
    def __init__(self, provider, model, outputs, fail_persona=None):
        super().__init__(outputs)
        self.provider, self.model, self.fail_persona = provider, model, fail_persona

    async def complete(self, system, user, schema):
        if self.fail_persona and f"You are the {self.fail_persona} analyst" in system:
            self.calls += 1
            raise RuntimeError("provider down")
        return await super().complete(system, user, schema)


@pytest.fixture
def sid(db_conn):
    s = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)
    snapshot.store_metrics(db_conn, s, [Metric("revenue", "FY2024", 1.0, "USD", "x")])
    return s


def test_agent_key():
    assert agent_key("bull", "openai") == "bull/openai"


@pytest.mark.db
async def test_grid_runs_15_cells_and_caches(db_conn, sid):
    adapters = [Named("anthropic", "m1", [OUT]), Named("openai", "m2", [OUT]), Named("gemini", "m3", [OUT])]
    first = await run_grid(db_conn, sid, PERSONAS, adapters, FakeEmbedder())
    assert len(first) == 15 and sum(a.calls for a in adapters) == 15
    assert {a["agent"] for a in first} == {f"{p}/{pr}" for p in PERSONAS for pr in ("anthropic", "openai", "gemini")}
    assert all("grounded" in a and a["grounded"]["stats"]["claims_total"] == 1 for a in first)
    second = await run_grid(db_conn, sid, PERSONAS, adapters, FakeEmbedder())
    assert len(second) == 15 and sum(a.calls for a in adapters) == 15


@pytest.mark.db
async def test_grid_survives_one_provider_failure(db_conn, sid):
    adapters = [Named("anthropic", "m1", [OUT]), Named("openai", "m2", [OUT], fail_persona="bear"), Named("gemini", "m3", [OUT])]
    ok = await run_grid(db_conn, sid, PERSONAS, adapters, FakeEmbedder())
    assert len(ok) == 14 and "bear/openai" not in {a["agent"] for a in ok}
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, error FROM analyses WHERE persona='bear' AND provider='openai'")
        status, error = cur.fetchone()
    assert status == "failed" and "provider down" in error
    again = await run_grid(db_conn, sid, PERSONAS, adapters, FakeEmbedder())  # failed cell is retried, others cached
    assert sum(a.calls for a in adapters) == 16 and len(again) == 14


@pytest.mark.db
def test_grid_output_prints_table(db_conn, sid, capsys):
    from fundabot.cli import grid_table

    with db_conn.cursor() as cur:
        for p, pr, score in (("bull", "anthropic", 5), ("bull", "openai", 1), ("bear", "anthropic", -3)):
            cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, grounded, status) VALUES (%s,%s,%s,'m',%s,%s,'ok')",
                        (sid, p, pr, '{}', '{"score": %d, "confidence": 0.5, "stats": {"hallucination_rate": 0.1}}' % score))
        cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, status, error) VALUES (%s,'bear','openai','m','{}','failed','boom')", (sid,))
    db_conn.commit()
    text = grid_table(db_conn, sid)
    assert "bull" in text and "failed" in text and "5 / 0.50 / 10%" in text and "row sd" in text and "col sd" in text
