"""FMP analyst grades: only changes are stored as events; next earnings date from the earnings endpoint."""
from datetime import date

import pytest

from fundabot.ingest import grades

ROWS = [{"symbol": "AAPL", "date": "2026-10-12", "gradingCompany": "JP Morgan", "previousGrade": "Overweight", "newGrade": "Overweight", "action": "maintain"},
        {"symbol": "AAPL", "date": "2026-10-01", "gradingCompany": "Needham", "previousGrade": "Hold", "newGrade": "Buy", "action": "upgrade"}]
EARN = [{"symbol": "AAPL", "date": "2026-11-02", "epsActual": None, "epsEstimated": 1.99}, {"symbol": "AAPL", "date": "2026-07-30", "epsActual": 2.02, "epsEstimated": 1.89}]


def fake_get(path, **params):
    return {"grades": ROWS, "earnings": EARN}[path]


@pytest.mark.db
def test_ingest_grades_and_next_earnings(db_conn):
    assert grades.ingest(db_conn, 320193, "AAPL", get=fake_get) == 2
    assert grades.ingest(db_conn, 320193, "AAPL", get=fake_get) == 0  # fetched minutes ago
    with db_conn.cursor() as cur:
        cur.execute("SELECT company, action FROM grades ORDER BY date"); assert cur.fetchall() == [("Needham", "upgrade"), ("JP Morgan", "maintain")]
    assert grades.next_earnings(db_conn, 320193, "AAPL", get=fake_get, today=date(2026, 10, 19)) == date(2026, 11, 2)
    assert grades.next_earnings(db_conn, 320193, "AAPL", get=lambda *a, **k: (_ for _ in ()).throw(AssertionError("cached")), today=date(2026, 10, 19)) == date(2026, 11, 2)
    assert grades.next_earnings(db_conn, 320193, "AAPL", get=fake_get, today=date(2026, 11, 3), max_age_hours=0) is None
