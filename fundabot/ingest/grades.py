"""FMP analyst grades (upgrades / downgrades are move explainers) and the next earnings date, cached in Postgres."""
from datetime import date, datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Jsonb

from fundabot.ingest import fmp


def _fresh(conn, cik: int, table: str, max_age_hours: float, where: str = "") -> bool:
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(fetched_at) FROM {table} WHERE cik = %s {where}", (cik,))
        last = cur.fetchone()[0]
    return last is not None and datetime.now(timezone.utc) - last < timedelta(hours=max_age_hours)


def ingest(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 6, limit: int = 200) -> int:
    """Every grade row FMP returns (the event matcher keeps only upgrades and downgrades). Returns rows inserted."""
    if _fresh(conn, cik, "grades", max_age_hours):
        return 0
    rows = get("grades", symbol=ticker.upper().replace("-", "."), limit=limit)
    inserted = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("INSERT INTO grades (cik, date, company, previous_grade, new_grade, action) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (cik, date, company) DO NOTHING",
                        (cik, date.fromisoformat(r["date"]), r["gradingCompany"], r.get("previousGrade"), r.get("newGrade"), (r.get("action") or "").lower()))
            inserted += cur.rowcount
        cur.execute("UPDATE grades SET fetched_at = now() WHERE cik = %s", (cik,))
    conn.commit()
    return inserted


def next_earnings(conn: psycopg.Connection, cik: int, ticker: str, get=fmp.get, max_age_hours: float = 6, today: date | None = None) -> date | None:
    """First earnings date on or after today whose actuals are not in yet; cached in ticker_facts."""
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT value, fetched_at FROM ticker_facts WHERE cik = %s AND key = 'next_earnings'", (cik,))
        row = cur.fetchone()
    if row and datetime.now(timezone.utc) - row[1] < timedelta(hours=max_age_hours):
        d = date.fromisoformat(row[0]) if row[0] else None
        return d if d and d >= today else None
    rows = get("earnings", symbol=ticker.upper().replace("-", "."), limit=8)
    upcoming = sorted(date.fromisoformat(r["date"]) for r in rows if r.get("epsActual") is None and r.get("date") and date.fromisoformat(r["date"]) >= today)
    value = upcoming[0].isoformat() if upcoming else None
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ticker_facts (cik, key, value, fetched_at) VALUES (%s, 'next_earnings', %s, now()) ON CONFLICT (cik, key) DO UPDATE SET value = EXCLUDED.value, fetched_at = now()",
                    (cik, Jsonb(value)))
    conn.commit()
    return upcoming[0] if upcoming else None
