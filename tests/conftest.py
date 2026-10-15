import os

import pytest

from fundabot.config import settings


@pytest.fixture
def db_conn():
    import psycopg

    from fundabot.db.session import connect, run_migrations

    try:
        conn = connect()
    except psycopg.OperationalError as e:
        pytest.skip(f"database unreachable: {e}")
    run_migrations(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'"
        )
        for (t,) in cur.fetchall():
            cur.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
