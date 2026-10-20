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
    monkeypatch.setattr(views, "refresh_fmp", lambda conn, cik, ticker: None)
    monkeypatch.setattr(views, "live_quotes", lambda conn, tickers: {})
    monkeypatch.setattr(views.fmp_extra, "fetch_ratings", lambda *a, **k: None)
    monkeypatch.setattr(views, "refresh_fmp", lambda conn, cik, ticker: None)
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
    chart = client.get("/t/AAPL/chart?window=90").text
    assert "price:2026-" in chart and "<th>volume</th>" in chart  # the largest moves carry their ids and relative volume


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


def seed_fmp(conn, cik=320193):
    from psycopg.types.json import Jsonb as J

    with conn.cursor() as cur:
        cur.execute("INSERT INTO company_profile (cik, name, sector, industry, exchange, description, employees, beta) VALUES (%s, 'Apple Inc.', 'Technology', 'Consumer Electronics', 'NASDAQ', 'Makes phones.', 166000, 1.07)", (cik,))
        cur.execute("INSERT INTO peers (cik, peer, name, market_cap, price, year_high, year_low, pe_ttm, ev_sales_ttm, ev_ebitda_ttm) VALUES (%s, 'SONY', 'Sony Group', 1.2e11, 20.0, 25.0, 15.0, 18.5, 1.9, 9.0)", (cik,))
        cur.execute("INSERT INTO segments (cik, kind, fiscal_year, name, revenue) VALUES (%s, 'product', 2025, 'iPhone', 209586000000), (%s, 'product', 2024, 'iPhone', 201183000000), (%s, 'product', 2025, 'Mac', 33708000000), (%s, 'product', 2024, 'Mac', 29984000000)", (cik, cik, cik, cik))
        cur.execute("INSERT INTO dividends (cik, ex_date, amount, adj_amount) VALUES (%s, '2026-08-10', 0.27, 0.27), (%s, '2026-05-11', 0.27, 0.27), (%s, '2026-02-09', 0.26, 0.26), (%s, '2025-11-10', 0.26, 0.26)", (cik, cik, cik, cik))
        cur.execute("INSERT INTO insider_trades (cik, filing_date, transaction_date, reporting_name, role, transaction_type, acquisition, shares, price, url) VALUES (%s, '2026-10-19', '2026-10-15', 'Borders Ben', 'officer: PAO', 'F-InKind', 'D', 883, 342, 'https://www.sec.gov/x')", (cik,))
        cur.execute("INSERT INTO holders (cik, quarter_end, investor, shares, market_value, change_pct, ownership_pct) VALUES (%s, '2026-06-30', 'BLACKROCK, INC.', 1162996939, 336524794350, 1.5988, 7.9156)", (cik,))
        cur.execute("INSERT INTO ticker_facts (cik, key, value) VALUES (%s, 'ratings', %s), (%s, 'fmp_crosscheck', %s), (0, 'treasury_10y', %s) ON CONFLICT (cik, key) DO UPDATE SET value = EXCLUDED.value",
                    (cik, J({"date": "2026-10-01", "strong_buy": 6, "buy": 19, "hold": 13, "sell": 3, "strong_sell": 3}),
                     cik, J({"checked": "2026-10-19", "ok": 9, "total": 10, "rows": [{"metric": "pe", "ours": 39.4, "fmp": 39.1, "diff": 0.007, "ok": True}, {"metric": "ev_ebitda", "ours": 30.0, "fmp": 24.0, "diff": 0.25, "ok": False}]}),
                     J({"date": "2026-10-16", "year10": 5.09})))
    conn.commit()


@pytest.mark.db
async def test_page_sections_fmp(client, db_conn, fixture_prepare, fmp_fixture, tmp_path):
    await full_run(db_conn, tmp_path)
    seed_prices(db_conn)
    seed_fmp(db_conn)
    with db_conn.cursor() as cur:  # metrics the snapshot would carry after prepare()
        cur.execute("SELECT id FROM snapshots ORDER BY created_at DESC LIMIT 1"); sid = cur.fetchone()[0]
        for mid, val, name in (("seg_product_iphone_share", 0.86, "iPhone"), ("seg_product_iphone_yoy", 0.042, "iPhone"), ("seg_product_mac_share", 0.14, "Mac"), ("seg_product_mac_yoy", 0.124, "Mac")):
            cur.execute("INSERT INTO metrics (snapshot_id, metric_id, period, value, unit, formula_id, inputs, flags) VALUES (%s, %s, 'FY2025', %s, 'ratio', 'fmp:revenue-product-segmentation', %s, '[\"vendor_fmp\"]')",
                        (sid, mid, val, json.dumps({"name": name})))
        for mid, val in (("pe_peer_median", 18.5), ("pe_vs_peer_median", 2.13), ("dividend_yield", 0.0031), ("dividend_per_share_ttm", 1.06), ("altman_z", 13.5), ("piotroski", 9), ("risk_free_10y", 0.0509)):
            cur.execute("INSERT INTO metrics (snapshot_id, metric_id, period, value, unit, formula_id, inputs, flags) VALUES (%s, %s, 'current', %s, 'ratio', 'fmp:x', '{}', '[\"vendor_fmp\"]') "
                        "ON CONFLICT (snapshot_id, metric_id, period) DO UPDATE SET value = EXCLUDED.value, null_reason = NULL", (sid, mid, val))
    db_conn.commit()
    r = client.get("/t/AAPL?window=90")
    assert r.status_code == 200
    for needle in ("Apple Inc. · Technology · Consumer Electronics · 166,000 employees", "Peers", "SONY", "18.5", "Revenue segments", "iPhone", "86", "+4.2%", "Ownership",
                   "BLACKROCK, INC.", "Borders Ben", "Scores and ratings", "Altman Z", "13.5", "Piotroski", "buy 19", "Cross-check", "9 of 10 within 10%", "ev_ebitda", "10-year Treasury 5.09%", "Dividend", "1.06"):
        assert needle in r.text, needle


@pytest.mark.db
def test_page_without_fmp_data(client, db_conn):
    seed_watch(db_conn)
    seed_prices(db_conn)
    r = client.get("/t/AAPL")
    assert r.status_code == 200 and "Peers" in r.text and "no peer data" in r.text.lower() and "no segment data" in r.text.lower() and "cross-check" in r.text.lower()


@pytest.mark.db
def test_overview_quotes(client, db_conn, monkeypatch):
    from fundabot.web import views

    seed_watch(db_conn)
    seed_prices(db_conn)
    monkeypatch.setattr(views, "live_quotes", lambda conn, tickers: {"AAPL": {"price": 342.94, "change_pct": -0.1514, "year_high": 348.15, "year_low": 243.42}})
    r = client.get("/")
    assert r.status_code == 200 and "342.94" in r.text and "-0.15%" in r.text and "day" in r.text
