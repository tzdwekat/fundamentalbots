"""FMP news ingest: stable ids, both kinds, dedupe, refresh interval, window loads."""
from datetime import date, datetime, timedelta, timezone

import pytest

from fundabot.ingest import news

ROW = {"symbol": "AAPL", "publishedDate": "2026-10-13 08:56:00", "publisher": "Business Wire", "site": "businesswire.com",
       "title": "Meet Apple HomeView", "url": "https://bw/1", "text": "CUPERTINO -- Apple today introduced", "image": None}


def fake_get(path, **params):
    if path == "news/stock":
        return [{**ROW, "title": "Tech stocks rebound", "url": "https://ibd/1", "publishedDate": "2026-10-19 17:58:04", "publisher": "IBD"}]
    if path == "news/press-releases":
        return [ROW]
    raise AssertionError(path)


def test_news_key_is_url_and_time():
    t = datetime(2026, 10, 13, 8, 56, tzinfo=timezone.utc)
    a, b, c = news.news_id("https://bw/1", t), news.news_id("https://bw/1", t + timedelta(hours=1)), news.news_id("https://bw/2", t)
    assert len(a) == 16 and a != b and a != c and a == news.news_id("https://bw/1", t)


def test_fetch_normalises_both_kinds():
    rows = news.fetch("AAPL", get=fake_get)
    assert [r["kind"] for r in rows] == ["news", "press"] and rows[0]["publisher"] == "IBD"
    assert rows[1]["published"] == datetime(2026, 10, 13, 8, 56, tzinfo=timezone.utc) and rows[1]["id"] == news.news_id("https://bw/1", rows[1]["published"])
    assert set(rows[0]) == {"id", "published", "publisher", "site", "title", "url", "text", "kind"}


@pytest.mark.db
def test_ingest_dedupes_and_respects_max_age(db_conn):
    calls = []

    def fetch(ticker, limit=100, get=None):
        calls.append(ticker)
        return news.fetch(ticker, get=fake_get)
    assert news.ingest(db_conn, 320193, "AAPL", fetch=fetch) == 2
    assert news.ingest(db_conn, 320193, "AAPL", fetch=fetch) == 0 and calls == ["AAPL"]  # fetched minutes ago: skipped
    assert news.ingest(db_conn, 320193, "AAPL", fetch=fetch, max_age_hours=0) == 0 and calls == ["AAPL", "AAPL"]  # refetched, all duplicates
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM news"); assert cur.fetchone()[0] == 2


@pytest.mark.db
def test_load_window(db_conn):
    news.ingest(db_conn, 320193, "AAPL", fetch=lambda t, limit=100, get=None: news.fetch(t, get=fake_get))
    rows = news.load(db_conn, 320193, days=30, until=date(2026, 10, 20))
    assert [r["title"] for r in rows] == ["Tech stocks rebound", "Meet Apple HomeView"]  # newest first
    assert news.load(db_conn, 320193, days=3, until=date(2026, 10, 20))[0]["title"] == "Tech stocks rebound" and len(news.load(db_conn, 320193, days=3, until=date(2026, 10, 20))) == 1
    assert news.load(db_conn, 1, days=30) == []
