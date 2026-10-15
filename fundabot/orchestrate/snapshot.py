import hashlib
import json
import subprocess
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from fundabot.analytics.metrics import Metric
from fundabot.analytics.red_flags import RedFlag


def _json_default(o):
    if hasattr(o, "item"):  # numpy scalar
        return o.item()
    return str(o)


def jsonb(obj):
    return Jsonb(obj, dumps=lambda o: json.dumps(o, default=_json_default))


def snapshot_id(cik: int, fetch_id: int, accessions: list[str], price_date: date | None) -> str:
    key = f"{cik}|{fetch_id}|{','.join(sorted(accessions))}|{price_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _code_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def create_snapshot(conn, ticker, cik, fetch_id, accessions, price_date, latest_close, shares) -> str:
    sid = snapshot_id(cik, fetch_id, accessions, price_date)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO snapshots (id, ticker, cik, companyfacts_fetch_id, accessions, price_date, latest_close, shares_outstanding, code_sha) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET code_sha = EXCLUDED.code_sha, created_at = now()",
            (sid, ticker.upper(), cik, fetch_id, jsonb(accessions), price_date, latest_close, shares, _code_sha()),
        )
    conn.commit()
    return sid


def latest_for(conn, ticker: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id, ticker, cik, companyfacts_fetch_id, accessions, price_date, latest_close, shares_outstanding, code_sha, created_at "
                    "FROM snapshots WHERE ticker = %s ORDER BY created_at DESC LIMIT 1", (ticker.upper(),))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ["id", "ticker", "cik", "companyfacts_fetch_id", "accessions", "price_date", "latest_close", "shares_outstanding", "code_sha", "created_at"]
    d = dict(zip(keys, row))
    d["latest_close"] = None if d["latest_close"] is None else float(d["latest_close"])
    return d


def store_metrics(conn: psycopg.Connection, sid: str, metrics: list[Metric]) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM metrics WHERE snapshot_id = %s", (sid,))
        cur.executemany(
            "INSERT INTO metrics (snapshot_id, metric_id, period, value, unit, formula_id, inputs, flags, tag_used, null_reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            [(sid, m.metric_id, m.period, m.value, m.unit, m.formula_id, jsonb(m.inputs), jsonb(m.flags), m.tag_used, m.null_reason) for m in metrics],
        )
    conn.commit()


def load_metrics(conn: psycopg.Connection, sid: str) -> list[Metric]:
    with conn.cursor() as cur:
        cur.execute("SELECT metric_id, period, value, unit, formula_id, inputs, flags, tag_used, null_reason FROM metrics WHERE snapshot_id = %s", (sid,))
        return [Metric(r[0], r[1], None if r[2] is None else float(r[2]), r[3], r[4], r[5], r[6], r[7], r[8]) for r in cur.fetchall()]


def store_flags(conn: psycopg.Connection, sid: str, flags: list[RedFlag]) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM red_flags WHERE snapshot_id = %s", (sid,))
        cur.executemany("INSERT INTO red_flags (snapshot_id, flag_id, severity, evidence, detail) VALUES (%s,%s,%s,%s,%s)",
                        [(sid, f.flag_id, f.severity, jsonb(f.evidence), f.detail) for f in flags])
    conn.commit()


def load_flags(conn: psycopg.Connection, sid: str) -> list[RedFlag]:
    with conn.cursor() as cur:
        cur.execute("SELECT flag_id, severity, evidence, detail FROM red_flags WHERE snapshot_id = %s ORDER BY flag_id", (sid,))
        return [RedFlag(*r) for r in cur.fetchall()]
