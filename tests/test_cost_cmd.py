"""`fundabot cost`: per-round token and USD breakdown from model_calls."""
import pytest


@pytest.mark.db
def test_cost_table_groups_rounds(db_conn):
    from fundabot.cli import cost_table

    rows = [  # (persona, provider, model, input, cached, write, output, usd)
        ("bull", "anthropic", "s", 50000, 0, 40000, 3000, 0.15),
        ("bear", "anthropic", "s", 50000, 40000, 0, 3000, 0.06),
        ("rebuttal:bull", "anthropic", "s", 20000, 0, 0, 1000, 0.05),
        ("judge:r1", "openai", "g", 30000, 0, 0, 2000, 0.10),
        ("judge:r2", "openai", "g", 30000, 0, 0, 2000, 0.10),
        ("guidance", "anthropic", "h", 5000, 0, 0, 500, 0.01),
    ]
    with db_conn.cursor() as cur:
        for p, pr, m, i, c, w, o, usd in rows:
            cur.execute("INSERT INTO model_calls (snapshot_id, persona, provider, model, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, cost_usd) "
                        "VALUES ('snapA', %s, %s, %s, %s, %s, %s, %s, %s)", (p, pr, m, i, c, w, o, usd))
        cur.execute("INSERT INTO model_calls (snapshot_id, persona, provider, model, input_tokens, cost_usd) VALUES ('snapB', 'bull', 'anthropic', 's', 999, 9.0)")
    out = cost_table(db_conn, "snapA")
    lines = {l.split()[0]: l.split() for l in out.splitlines() if l.strip() and not l.startswith("round")}
    assert lines["persona"][1:6] == ["2", "100,000", "40,000", "40,000", "6,000"] and lines["persona"][6] == "0.21"
    assert lines["rebuttal"][1] == "1" and lines["judge:r1"][1] == "1" and lines["judge:r2"][1] == "1" and lines["guidance"][1] == "1"
    assert lines["total"][1:6] == ["6", "185,000", "40,000", "40,000", "11,500"] and lines["total"][6] == "0.47"
