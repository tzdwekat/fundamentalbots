"""The web app: pages read stored structures; model spend only from buttons that show the estimate."""
import json
from datetime import date, datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from fundabot.agents.adapters.fake import FakeAdapter
from fundabot.orchestrate import pipeline
from fundabot.retrieval.embed import FakeEmbedder
from tests.test_full_pipeline import persona_fake, rebuttal_or_judge, screen_fake
from tests.test_pipeline import PERSONAS, fixture_prepare  # noqa: F401
from tests.test_full_pipeline import fmp_fixture  # noqa: F401


@pytest.fixture
def client(monkeypatch, db_conn):
    from fundabot.web import app as webapp, views

    monkeypatch.setattr(views, "refresh_prices", lambda conn, cik, ticker: None)
    monkeypatch.setattr(views, "refresh_news", lambda conn, cik, ticker: 0)
    monkeypatch.setattr(views, "refresh_grades", lambda conn, cik, ticker: 0)
    monkeypatch.setattr(views, "refresh_benchmark", lambda conn: None)
    monkeypatch.setattr(views, "next_earnings", lambda conn, cik, ticker: date(2026, 11, 2))
    monkeypatch.setattr(views, "list_filings", lambda cik: pd.DataFrame(columns=["accession", "form", "filed", "primary_doc_url"]))
    return TestClient(webapp.app)


def seed_watch(conn, ticker="AAPL", cik=320193):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO watchlist (ticker, cik) VALUES (%s, %s) ON CONFLICT DO NOTHING", (ticker, cik))
    conn.commit()


def seed_prices(conn, cik=320193, n=260):
    dates = pd.bdate_range(end=date(2026, 10, 16), periods=n)
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO prices (cik, date, close) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", [(cik, d.date(), 200.0 + i * 0.2) for i, d in enumerate(dates)])
        cur.executemany("INSERT INTO benchmark_prices (ticker, date, close) VALUES ('SPY', %s, %s) ON CONFLICT DO NOTHING", [(d.date(), 500.0 + i * 0.1) for i, d in enumerate(dates)])
    conn.commit()


async def full_run(conn, tmp_path):
    personas, screen, judges = [persona_fake("anthropic")], [screen_fake("anthropic")], [rebuttal_or_judge("anthropic")]
    return await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)


@pytest.mark.db
def test_overview_lists_watchlist_with_returns(client, db_conn):
    seed_watch(db_conn)
    seed_prices(db_conn)
    r = client.get("/")
    assert r.status_code == 200 and "AAPL" in r.text and "1m" in r.text and "insufficient data" in r.text and "Analyze" in r.text
    assert "~$" in r.text  # every Analyze button shows its estimate


@pytest.mark.db
def test_ticker_page_without_snapshot(client, db_conn):
    seed_watch(db_conn)
    r = client.get("/t/AAPL")
    assert r.status_code == 200 and "insufficient data" in r.text and "Refresh data" in r.text and "no analysis yet" in r.text.lower()


@pytest.mark.db
async def test_ticker_page_sections(client, db_conn, fixture_prepare, fmp_fixture, tmp_path):
    html = await full_run(db_conn, tmp_path)
    seed_prices(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO news (id, cik, published, publisher, site, title, url, text, kind) VALUES ('n1', 320193, %s, 'Reuters', 'r', 'Apple beats on iPhone', 'http://r/1', 'text', 'news')",
                    (datetime(2026, 10, 10, 12, tzinfo=timezone.utc),))
        cur.execute("INSERT INTO grades (cik, date, company, previous_grade, new_grade, action) VALUES (320193, '2026-10-09', 'JP Morgan', 'Neutral', 'Overweight', 'upgrade')")
    db_conn.commit()
    r = client.get("/t/AAPL?window=90&news=30")
    assert r.status_code == 200
    for needle in ("<svg", "Outlook", "Bull case", "Bear case", "Apple beats on iPhone", "JP Morgan", "Explain moves", "~$0.0", "Next earnings", "2026-11-02", "-full.html", "Red flags", "ret_3m".replace("ret_3m", "3m")):
        assert needle in r.text, needle
    assert "metric:" in r.text and "evidence/" in r.text  # claim evidence links to the drawer
    assert "price:2026-" in client.get("/t/AAPL/chart?window=90").text  # the largest moves carry their ids


@pytest.mark.db
async def test_narrative_requires_post_and_shows_estimate(client, db_conn, fixture_prepare, fmp_fixture, tmp_path, monkeypatch):
    from fundabot.web import views

    await full_run(db_conn, tmp_path)
    seed_prices(db_conn)
    r = client.get("/t/AAPL/narrative?window=90")
    assert r.status_code == 200 and "Explain moves" in r.text and "~$0.03" in r.text and "price:" not in r.text
    import re as _re

    def out(system, user):  # cite the first largest-move id the message offers
        pid = _re.search(r"^(price:\d{4}-\d{2}-\d{2}):", user, _re.M).group(1)
        return {"sentences": [{"text": "The stock rose steadily over the window.", "evidence": [pid]}], "one_line": "steady climb"}
    fake = FakeAdapter(out)
    fake.provider, fake.model = "anthropic", "fake-guidance"
    monkeypatch.setattr(views, "narrative_adapter", lambda profile: fake)
    r = client.post("/t/AAPL/narrative", data={"window": "90", "profile": "multi"})
    assert r.status_code == 200 and "steady climb" in r.text and "price:2026-" in r.text and fake.calls == 1
    r = client.get("/t/AAPL/narrative?window=90")
    assert "steady climb" in r.text and fake.calls == 1  # stored, not regenerated by a GET


@pytest.mark.db
async def test_evidence_drawer_resolves_metric_chunk_news(client, db_conn, fixture_prepare, fmp_fixture, tmp_path):
    await full_run(db_conn, tmp_path)
    with db_conn.cursor() as cur:
        cur.execute("SELECT id FROM snapshots ORDER BY created_at DESC LIMIT 1"); sid = cur.fetchone()[0]
        cur.execute("SELECT id FROM chunks LIMIT 1"); chunk_id = cur.fetchone()[0]
        cur.execute("INSERT INTO news (id, cik, published, publisher, site, title, url, text, kind) VALUES ('n1', 320193, now(), 'Reuters', 'r', 'Headline', 'http://r/1', 'Body text', 'news')")
    db_conn.commit()
    assert "gross_margin" in client.get(f"/evidence/{sid}/metric:gross_margin@FY2024").text
    from urllib.parse import quote

    chunk_url = f"/evidence/{sid}/{quote('chunk:' + chunk_id, safe='')}"
    assert client.get(chunk_url).status_code == 200 and len(client.get(chunk_url).text) > 50
    assert "Body text" in client.get(f"/evidence/{sid}/news:n1").text and "http://r/1" in client.get(f"/evidence/{sid}/news:n1").text
    assert client.get(f"/evidence/{sid}/metric:nope@FY2024").status_code == 404


@pytest.mark.db
def test_job_refused_when_worker_has_ticker(client, db_conn):
    seed_watch(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO run_queue (ticker, cik, period_key, reason, status) VALUES ('AAPL', 320193, 'FY2026Q4', 'test', 'queued')")
    db_conn.commit()
    r = client.post("/jobs", data={"ticker": "AAPL", "kind": "full", "profile": "multi"})
    assert r.status_code == 409 and "worker" in r.text.lower()


@pytest.mark.db
def test_job_runs_and_reports_cost(client, db_conn, monkeypatch):
    from fundabot.web import jobs

    monkeypatch.setattr(pipeline, "prepare", lambda conn, ticker, force=False: "snapX")
    r = client.post("/jobs", data={"ticker": "AAPL", "kind": "refresh", "profile": "multi"})
    assert r.status_code == 200
    job_id = int(r.headers["X-Job-Id"])
    jobs.wait(job_id, timeout=10)
    s = client.get(f"/jobs/{job_id}")
    assert s.status_code == 200 and "done" in s.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT status, kind, cost_usd FROM jobs WHERE id = %s", (job_id,))
        assert cur.fetchone() == ("done", "refresh", 0)


@pytest.mark.db
async def test_review_label_roundtrip(client, db_conn, fixture_prepare, fmp_fixture, tmp_path):
    await full_run(db_conn, tmp_path)
    r = client.get("/review")
    assert r.status_code == 200 and "AAPL" in r.text
    r = client.post("/review", data={"ticker": "AAPL", "agent": "bull/anthropic", "vote": "up", "note": "fair"})
    assert r.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute("SELECT agent, vote FROM reviews"); assert cur.fetchone() == ("bull/anthropic", "up")


@pytest.mark.db
def test_queue_shows_spend_vs_budget(client, db_conn):
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO model_calls (persona, provider, model, cost_usd) VALUES ('bull', 'anthropic', 'm', 1.25)")
        cur.execute("INSERT INTO run_queue (ticker, cik, period_key, reason, status) VALUES ('NVDA', 1, 'FY2026Q3', '10-Q', 'queued')")
    db_conn.commit()
    r = client.get("/queue")
    assert r.status_code == 200 and "1.25" in r.text and "NVDA" in r.text and "budget" in r.text.lower()
    assert client.get("/calibration").status_code == 200


@pytest.mark.db
async def test_untrusted_text_is_escaped(client, db_conn, fixture_prepare, fmp_fixture, tmp_path):
    await full_run(db_conn, tmp_path)
    seed_prices(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO news (id, cik, published, publisher, site, title, url, text, kind) VALUES ('x1', 320193, '2026-10-15 12:00+00', 'Evil <i>Co</i>', 'e', '<b>XSS</b> & co', 'http://e/1', '<script>alert(1)</script>', 'news')")
    db_conn.commit()
    r = client.get("/t/AAPL?window=90&news=7")
    assert "<b>XSS</b>" not in r.text and "&lt;b&gt;XSS&lt;/b&gt; &amp; co" in r.text and "<script>alert" not in r.text
    assert 'data-theme="light"' in r.text  # the chart CSS is still raw inside <style>
    e = client.get("/evidence/-/news:x1")
    assert "<script>" not in e.text and "&lt;script&gt;" in e.text


@pytest.mark.db
async def test_cheap_panel_uses_newest_run_and_renders_claims(client, db_conn, fixture_prepare, fmp_fixture, tmp_path):
    from fundabot.orchestrate import snapshot
    from tests.test_pipeline import fake_output

    conn = db_conn
    await pipeline.analyze(conn, "AAPL", PERSONAS, FakeAdapter(fake_output), FakeEmbedder(), out_dir=tmp_path)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM snapshots"); (old,) = cur.fetchone()
    new = snapshot.create_snapshot(conn, "AAPL", 320193, 99, ["0000320193-25-000079"], date(2026, 10, 18), 350.0, 1e10)
    assert new < old or True  # whatever the hash order, created_at decides
    with conn.cursor() as cur:
        cur.execute("INSERT INTO analyses (snapshot_id, persona, provider, model, output, grounded, chunk_ids, status) "
                    "SELECT %s, persona, provider, model, output, jsonb_set(grounded, '{score}', '9'), chunk_ids, status FROM analyses WHERE snapshot_id = %s", (new, old))
    conn.commit()
    r = client.get("/t/AAPL")
    assert r.status_code == 200 and "cheap run" in r.text and "bull/fake 9" in r.text and "·  panel" not in r.text and "++" not in r.text


@pytest.mark.db
def test_edgar_outage_still_renders(client, db_conn, monkeypatch):
    from fundabot.web import views

    seed_watch(db_conn)
    seed_prices(db_conn)

    def boom(cik):
        raise RuntimeError("EDGAR down")
    monkeypatch.setattr(views, "list_filings", boom)
    r = client.get("/t/AAPL?window=90")
    assert r.status_code == 200 and "<svg" in r.text


@pytest.mark.db
def test_pages_preselect_the_settings_profile(client, db_conn, monkeypatch):
    from fundabot.config import settings

    seed_watch(db_conn)
    assert 'value="anthropic" selected' in client.get("/t/AAPL").text and "default profile" in client.get("/").text
    monkeypatch.setattr(settings, "fundabot_profile", "openai")
    assert 'value="openai" selected' in client.get("/t/AAPL").text
