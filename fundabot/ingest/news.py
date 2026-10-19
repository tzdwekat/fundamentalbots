"""FMP stock news and press releases (news/stock, news/press-releases; verified 2026-10-19: no id field, so the key is
a hash of url and published time)."""
import hashlib
from datetime import date, datetime, timedelta, timezone

import psycopg

from fundabot.ingest import fmp

KINDS = (("news/stock", "news"), ("news/press-releases", "press"))


def news_id(url: str, published: datetime) -> str:
    return hashlib.sha256(f"{url}|{published.isoformat()}".encode()).hexdigest()[:16]


def _published(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)  # FMP times are UTC without an offset


def fetch(ticker: str, limit: int = 100, get=fmp.get) -> list[dict]:
    rows = []
    for path, kind in KINDS:
        for r in get(path, symbols=ticker.upper(), limit=limit):
            published = _published(r["publishedDate"])
            rows.append({"id": news_id(r["url"], published), "published": published, "publisher": r.get("publisher"), "site": r.get("site"),
                         "title": r["title"], "url": r["url"], "text": r.get("text"), "kind": kind})
    return rows


def ingest(conn: psycopg.Connection, cik: int, ticker: str, fetch=fetch, max_age_hours: float = 6) -> int:
    """Fetch when the newest stored item was fetched more than max_age_hours ago. Returns rows inserted."""
    with conn.cursor() as cur:
        cur.execute("SELECT max(fetched_at) FROM news WHERE cik = %s", (cik,))
        last = cur.fetchone()[0]
    if last is not None and datetime.now(timezone.utc) - last < timedelta(hours=max_age_hours):
        return 0
    rows = fetch(ticker)
    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("INSERT INTO news (id, cik, published, publisher, site, title, url, text, kind) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (r["id"], cik, r["published"], r["publisher"], r["site"], r["title"], r["url"], r["text"], r["kind"]))
            inserted += cur.rowcount
        if rows:  # a refetch that found only duplicates still counts as fresh
            cur.execute("UPDATE news SET fetched_at = now() WHERE cik = %s AND id = ANY(%s)", (cik, [r["id"] for r in rows]))
    conn.commit()
    return inserted


def load(conn: psycopg.Connection, cik: int, days: int, until: date | None = None) -> list[dict]:
    end = until or date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT id, published, publisher, site, title, url, text, kind FROM news WHERE cik = %s AND published::date BETWEEN %s AND %s ORDER BY published DESC",
                    (cik, end - timedelta(days=days), end))
        return [dict(zip(["id", "published", "publisher", "site", "title", "url", "text", "kind"], r)) for r in cur.fetchall()]
