"""Tests that touch Postgres use a separate database (<name>_test) so they never truncate real data."""
import pytest

from fundabot import config


def _test_url(url: str) -> str:
    base, _, name = url.rpartition("/")
    name = name.split("?")[0]
    return f"{base}/{name}_test"


@pytest.fixture
def db_conn(monkeypatch):
    import psycopg

    from fundabot.db.session import connect, run_migrations

    url = _test_url(config.settings.database_url)
    monkeypatch.setattr(config.settings, "database_url", url)
    try:
        maint = psycopg.connect(url.rsplit("/", 1)[0] + "/postgres", autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"database unreachable: {e}")
    dbname = url.rsplit("/", 1)[1]
    with maint.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    maint.close()
    conn = connect()
    assert conn.info.dbname.endswith("_test")
    run_migrations(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")
        for (t,) in cur.fetchall():
            cur.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()
