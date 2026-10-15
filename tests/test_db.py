import pytest

from fundabot.db.session import run_migrations


@pytest.mark.db
def test_migrations_idempotent(db_conn):
    # db_conn already ran migrations once; a fresh DB would return ["001_init.sql"].
    with db_conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db_conn.commit()
    assert run_migrations(db_conn) == ["001_init.sql"]
    assert run_migrations(db_conn) == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] == 1
