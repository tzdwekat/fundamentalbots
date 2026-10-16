import json
from datetime import date

import pytest

from fundabot.eval import review
from fundabot.orchestrate import snapshot


def verdict(dispersion, agreement, contested, composite=1.0):
    return json.dumps({"composite_score": composite, "dispersion": dispersion, "judge_agreement": agreement,
                       "contested_claims": [{"id": f"bear/openai#{i}", "text": f"claim {i}", "refutations": [{"by": "bull/anthropic", "reason": "no", "evidence": []}],
                                             "conceded_by": [], "judge": {"evidence_strength": 3, "materiality": 4, "spread": 2, "notes": []}} for i in range(contested)]})


@pytest.fixture
def three(db_conn):
    ids = {}
    for t, d, a, c in (("AAPL", 0.2, 1.0, 0), ("NVDA", 0.5, 0.6, 3), ("MSFT", 0.4, 0.9, 1)):
        sid = snapshot.create_snapshot(db_conn, t, hash(t) % 10**6, 1, ["a"], date(2026, 10, 10), 1.0, 1.0)
        with db_conn.cursor() as cur:
            cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, 'full', %s)", (sid, verdict(d, a, c)))
        db_conn.commit()
        ids[t] = sid
    return ids


@pytest.mark.db
def test_contested_ranking(db_conn, three):
    rows = review.contested(db_conn, n=10)
    assert [r["ticker"] for r in rows] == ["NVDA", "MSFT", "AAPL"]
    top = rows[0]
    assert top["composite"] == 1.0 and top["dispersion"] == 0.5 and top["agreement"] == 0.6 and top["n_contested"] == 3
    assert top["top_claim"]["text"] == "claim 0" and top["top_claim"]["refutations"][0]["by"] == "bull/anthropic"
    assert rows[-1]["top_claim"] is None
    assert len(review.contested(db_conn, n=2)) == 2


@pytest.mark.db
def test_label_and_counts(db_conn, three):
    rid = review.label(db_conn, "nvda", "bear/openai", "up", "well argued")
    review.label(db_conn, "NVDA", "bull/anthropic", "down", "")
    review.label(db_conn, "AAPL", "bear/openai", "down", "weak")
    assert isinstance(rid, int)
    assert review.label_counts(db_conn) == [("bear", "openai", 1, 1), ("bull", "anthropic", 0, 1)]
    with pytest.raises(ValueError):
        review.label(db_conn, "NVDA", "bear/openai", "maybe", "")
    with pytest.raises(ValueError):
        review.label(db_conn, "ZZZZ", "bear/openai", "up", "")
