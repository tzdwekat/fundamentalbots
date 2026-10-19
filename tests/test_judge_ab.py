"""`fundabot judge-ab`: compare the triage verdict with the stored one."""
from datetime import date

import pytest
from psycopg.types.json import Jsonb


def verdict(composite, consensus=(), contested=(), screen_paths=(), baseline=None, escalated=("x",), n=10):
    def entry(cid, mat, path="screen", strength=4.0):
        return {"id": cid, "judge": {"evidence_strength": strength, "materiality": mat, "spread": 0, "notes": [], "path": path}}
    v = {"composite_score": composite, "quality_score": 70, "value_score": 40, "earnings_momentum_score": 60, "accounting_risk_score": 20,
         "judge_agreement": 0.9, "escalated_ids": list(escalated), "n_claims": n,
         "consensus_claims": [entry(*c) for c in consensus], "contested_claims": [entry(*c) for c in contested]}
    if baseline is not None:
        v["baseline_claim_means"] = baseline
    return v


def test_judge_ab_table_rule():
    from fundabot.cli import judge_ab_table

    old = verdict(2.6, consensus=[("a", 3), ("b", 4)], contested=[("c", 5)])
    ship = judge_ab_table(old, verdict(1.5, consensus=[("a", 3), ("b", 4)], contested=[("c", 5, "escalated")], baseline={"a": {"evidence_strength": 3.0}}))
    assert ship.strip().endswith("verdict: ship") and "composite" in ship and "2.6 -> 1.5" in ship and "escalated  1 of 10" in ship and "a " in ship
    widen = judge_ab_table(old, verdict(0.9, consensus=[("a", 3), ("b", 4)], contested=[("c", 5, "escalated")]))
    assert "verdict: widen" in widen and "composite" in widen.split("verdict: widen")[1]
    moved = judge_ab_table(old, verdict(2.4, consensus=[("a", 3)], contested=[("b", 4), ("c", 5, "escalated")]))
    assert "verdict: widen" in moved and "b consensus->contested (materiality 4)" in moved
    minor_move = judge_ab_table(old, verdict(2.4, consensus=[("b", 4)], contested=[("a", 3), ("c", 5, "escalated")]))
    assert minor_move.strip().endswith("verdict: ship") and "a consensus->contested (materiality 3)" in minor_move


@pytest.mark.db
def test_judge_ab_picks_snapshot_with_verdict(db_conn):
    from fundabot.cli import latest_with_verdict
    from fundabot.orchestrate import snapshot

    older = snapshot.create_snapshot(db_conn, "AAPL", 320193, 1, ["acc"], date(2026, 10, 10), 300.0, 1e10)
    newer = snapshot.create_snapshot(db_conn, "AAPL", 320193, 2, ["acc", "acc2"], date(2026, 10, 14), 310.0, 1e10)
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO verdicts (snapshot_id, run_kind, verdict) VALUES (%s, 'full', %s)", (older, Jsonb({"composite_score": 1.0})))
    assert latest_with_verdict(db_conn, "AAPL") == older and newer != older
    assert latest_with_verdict(db_conn, "MSFT") is None
