"""Tests pinned by the Phase 3 whole-branch review."""
import asyncio
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS

import httpx
import pandas as pd
import pytest

from fundabot.agents import schemas
from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.config import settings
from tests.test_full_pipeline import calls, fmp_fixture, persona_fake, rebuttal_or_judge  # noqa: F401
from tests.test_pipeline import PERSONAS, fixture_prepare  # noqa: F401

GOOD = {"thesis": "t", "claims": [], "key_risks": [], "what_would_change_my_mind": [], "score": 1, "confidence": 0.5}


def test_phase2_command_builds_no_judges_without_gemini_key(monkeypatch):
    from fundabot.cli import build_adapters

    monkeypatch.setattr(settings, "gemini_api_key", "")
    adapters, judges = build_adapters(full=False, providers="anthropic", model="claude-sonnet-5-5", judges=3)
    assert [a.provider for a in adapters] == ["anthropic"] and judges == []
    with pytest.raises(Exception):
        build_adapters(full=True, providers="anthropic", model="claude-sonnet-5-5", judges=3)  # gemini needed, no key: fail loudly


def test_q4_surprise_label_december_fye():
    from fundabot.analytics.earnings import compute_earnings_metrics

    sur = pd.DataFrame([(date(2027, 2, 10), 1.0, 0.9, 10.0, 9.0), (date(2026, 10, 28), 1.0, 0.9, 10.0, 9.0)],
                       columns=["report_date", "eps_actual", "eps_estimated", "revenue_actual", "revenue_estimated"])
    fmp = {"surprises": sur, "estimates": pd.DataFrame(columns=["fetched_at", "period", "period_end", "eps_avg", "revenue_avg"]),
           "price_targets": pd.DataFrame(columns=["fetched_at", "consensus", "median", "high", "low"])}
    ms = compute_earnings_metrics(fmp, date(2027, 3, 1), 100.0, (12, 31), 0, None)
    periods = sorted(m.period for m in ms if m.metric_id == "eps_surprise")
    assert periods == ["Q3FY2026", "Q4FY2026"]
    ms2 = compute_earnings_metrics(fmp.copy() | {"surprises": sur.assign(report_date=[date(2026, 11, 20), date(2026, 8, 1)])}, date(2026, 12, 1), 100.0, (9, 27), 0, None)
    assert sorted(m.period for m in ms2 if m.metric_id == "eps_surprise") == ["Q3FY2026", "Q4FY2026"]


class Failing(FakeAdapter):
    def __init__(self, provider):
        super().__init__([GOOD])
        self.provider, self.model = provider, "judge-fail"

    async def complete(self, system, user, schema):
        self.calls += 1
        raise RuntimeError("judge down")


@pytest.mark.db
async def test_full_with_no_surviving_judge_raises_and_stores_no_verdict(fixture_prepare, fmp_fixture, tmp_path):
    from fundabot.orchestrate import pipeline

    conn = fixture_prepare
    with pytest.raises(RuntimeError, match="no judge"):
        await pipeline.analyze_full(conn, "AAPL", PERSONAS, [persona_fake("anthropic")], [Failing("anthropic")], __import__("fundabot.retrieval.embed", fromlist=["FakeEmbedder"]).FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM verdicts")
        assert cur.fetchone()[0] == 0


@pytest.mark.db
async def test_new_cells_invalidate_rebuttals_and_judgements(fixture_prepare, fmp_fixture, tmp_path):
    from fundabot.orchestrate import pipeline
    from fundabot.retrieval.embed import FakeEmbedder

    conn = fixture_prepare
    judges = [rebuttal_or_judge("anthropic")]
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, [persona_fake("anthropic")], judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(judges) == 1
    await pipeline.analyze_full(conn, "AAPL", PERSONAS, [persona_fake("anthropic"), persona_fake("openai")], judges, FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM rebuttals"); n_reb = cur.fetchone()[0]
        cur.execute("SELECT verdict FROM verdicts"); v = cur.fetchone()[0]
    assert n_reb == 10 and calls(judges) == 2  # the judge re-ran over the 10-agent pool; no Gemini-style ghost agents with weight 0
    assert all(w > 0 for w in v["agent_weights"].values())


async def test_gemini_lock_survives_a_new_event_loop(monkeypatch):
    from fundabot.agents.adapters.gemini import GeminiAdapter

    a = GeminiAdapter("gemini-3.8-flash")

    async def fake_generate(**kw):
        return NS(text=json.dumps(GOOD), usage_metadata=NS(prompt_token_count=1, candidates_token_count=1, thoughts_token_count=0), candidates=[NS(finish_reason="STOP")])
    monkeypatch.setattr(a._client.aio.models, "generate_content", fake_generate)

    async def pair():
        return await asyncio.gather(a.complete("s", "u", schemas.AGENT_OUTPUT_SCHEMA), a.complete("s", "u", schemas.AGENT_OUTPUT_SCHEMA))

    import threading
    out, errors = [], []

    def run_once():
        try:
            out.append(asyncio.run(pair()))  # a fresh event loop each time, as the CLI does per ticker
        except Exception as e:
            errors.append(e)
    for _ in range(2):
        t = threading.Thread(target=run_once)
        t.start(); t.join()
    assert errors == [] and len(out) == 2 and all(r.output == GOOD for rs in out for r in rs)


@pytest.mark.db
async def test_catalysts_and_summaries_are_checked(fixture_prepare, fmp_fixture, tmp_path):
    from fundabot.orchestrate import pipeline
    from fundabot.retrieval.embed import FakeEmbedder

    conn = fixture_prepare
    base = rebuttal_or_judge("anthropic")
    inner = base.outputs

    def out(system, user):
        o = inner(system, user)
        o["catalysts"] = [{"what": "real", "when": "soon", "evidence": ["metric:revenue@FY2024"]}, {"what": "invented", "when": "soon", "evidence": ["metric:guidance@FY2027"]}]
        o["one_paragraph_summary"] = "Revenue of $999 billion and a gross margin of 46.2%."
        return o
    base.outputs = out
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, [persona_fake("anthropic")], [base], FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts"); v = cur.fetchone()[0]
    assert [c["what"] for c in v["catalysts"]] == ["real"]
    assert v["summary_unsupported"]["anthropic"] == ["$999 billion"]
    assert "unsupported numbers in summary" in html.with_suffix(".md").read_text()


@pytest.mark.db
def test_fmp_network_error_does_not_fail_prepare(fixture_prepare, monkeypatch):
    from fundabot.ingest import fmp
    from fundabot.orchestrate import pipeline, snapshot

    def boom(ticker):
        raise httpx.ConnectError("dns")
    monkeypatch.setattr(fmp, "fetch_all", boom)
    sid = pipeline.prepare(fixture_prepare, "AAPL")
    ms = {m.metric_id: m for m in snapshot.load_metrics(fixture_prepare, sid) if m.period == "current"}
    assert ms["forward_pe"].value is None and ms["forward_pe"].null_reason == "no_coverage"


class Pricey(FakeAdapter):
    def __init__(self, provider):
        super().__init__([GOOD])
        self.provider, self.model = provider, "m"

    def estimate_cost(self, input_chars):
        return 0.5


@pytest.mark.db
async def test_grid_budget_checked_across_providers(db_conn, monkeypatch):
    from fundabot.agents.base import BudgetExceeded
    from fundabot.agents.runner import run_grid
    from fundabot.analytics.metrics import Metric
    from fundabot.orchestrate import snapshot
    from fundabot.retrieval.embed import FakeEmbedder

    monkeypatch.setattr(settings, "fundabot_monthly_budget_usd", 1.2)  # one provider's batch (0.5) fits; three (1.5) do not
    sid = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 14), 300.0, 1e10)
    snapshot.store_metrics(db_conn, sid, [Metric("revenue", "FY2024", 1.0, "USD", "x")])
    adapters = [Pricey(p) for p in ("anthropic", "openai", "gemini")]
    with pytest.raises(BudgetExceeded):
        await run_grid(db_conn, sid, ["bull"], adapters, FakeEmbedder())
    assert sum(a.calls for a in adapters) == 0


def test_screen_tier_only_affects_personas():
    from fundabot.cli import build_adapters

    adapters, judges = build_adapters(full=True, providers="anthropic", model="claude-sonnet-5-5", judges=3, tier="screen")
    assert [a.model for a in adapters] == ["claude-haiku-5-5", "gpt-5.4-mini", "gemini-3.8-flash"]
    assert [j.model for j in judges] == ["claude-opus-5-5", "gpt-5.4", "gemini-3.1-pro-preview"]  # judges keep the strong models
    default, _ = build_adapters(full=False, providers="anthropic", model="claude-sonnet-5-5", judges=3)
    assert [a.model for a in default] == ["claude-sonnet-5-5"]
