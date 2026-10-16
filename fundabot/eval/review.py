"""Human review loop: the most contested recent verdicts, and thumbs up/down labels per agent for prompt revision."""
import psycopg


def contested(conn: psycopg.Connection, n: int = 10) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT v.snapshot_id, s.ticker, v.verdict, v.created_at FROM verdicts v JOIN snapshots s ON s.id = v.snapshot_id "
                    "WHERE v.run_kind = 'full' ORDER BY v.created_at DESC LIMIT 200")
        rows = cur.fetchall()
    out = []
    for sid, ticker, v, created in rows:
        disp, agree = float(v.get("dispersion") or 0), float(v.get("judge_agreement") if v.get("judge_agreement") is not None else 1.0)
        claims = v.get("contested_claims") or []
        out.append({"snapshot_id": sid, "ticker": ticker, "created_at": created, "composite": v.get("composite_score"), "dispersion": disp,
                    "agreement": agree, "n_contested": len(claims), "score": disp * (1 - agree) + 0.05 * len(claims), "top_claim": claims[0] if claims else None})
    return sorted(out, key=lambda r: -r["score"])[:n]


def label(conn: psycopg.Connection, ticker: str, agent: str, vote: str, note: str = "") -> int:
    if vote not in ("up", "down"):
        raise ValueError("vote must be up or down")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM snapshots WHERE ticker = %s ORDER BY created_at DESC LIMIT 1", (ticker.upper(),))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"no snapshot for {ticker}")
        cur.execute("INSERT INTO reviews (snapshot_id, agent, vote, note) VALUES (%s, %s, %s, %s) RETURNING id", (row[0], agent, vote, note or None))
        rid = cur.fetchone()[0]
    conn.commit()
    return rid


def label_counts(conn: psycopg.Connection) -> list[tuple[str, str, int, int]]:
    with conn.cursor() as cur:
        cur.execute("SELECT split_part(agent, '/', 1), split_part(agent, '/', 2), count(*) FILTER (WHERE vote = 'up'), count(*) FILTER (WHERE vote = 'down') "
                    "FROM reviews GROUP BY 1, 2 ORDER BY 1, 2")
        return [(p, pr, int(u), int(d)) for p, pr, u, d in cur.fetchall()]
