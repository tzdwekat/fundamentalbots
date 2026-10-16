import pytest

from fundabot.db.session import run_migrations


@pytest.mark.db
def test_migrations_idempotent(db_conn):
    # db_conn already ran migrations once; a fresh DB would return ["001_init.sql"].
    with db_conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    db_conn.commit()
    assert run_migrations(db_conn) == ["001_init.sql", "002_splits.sql", "003_debate.sql", "004_phase4.sql"]
    assert run_migrations(db_conn) == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations")
        assert cur.fetchone()[0] == 4


def test_test_database_name_is_separate():
    from tests.conftest import _test_url

    assert _test_url("postgresql://u:p@localhost:5433/fundabot") == "postgresql://u:p@localhost:5433/fundabot_test"


@pytest.mark.db
def test_db_fixture_uses_test_database(db_conn):
    assert db_conn.info.dbname == "fundabot_test"
