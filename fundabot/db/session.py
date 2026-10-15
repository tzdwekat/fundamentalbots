from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from fundabot.config import settings

SCHEMA_DIR = Path(__file__).parent / "schema"


def connect() -> psycopg.Connection:
    conn = psycopg.connect(settings.database_url)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)
    return conn


def run_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply schema/*.sql in name order, skipping those already recorded. Returns names applied."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (filename text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        done = {r[0] for r in cur.fetchall()}
        applied = []
        for path in sorted(SCHEMA_DIR.glob("*.sql")):
            if path.name in done:
                continue
            cur.execute(path.read_text())
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            applied.append(path.name)
    conn.commit()
    return applied


def execute_values(conn: psycopg.Connection, sql: str, rows: list[tuple]) -> None:
    """Run a parametrized INSERT for many rows in one transaction."""
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
