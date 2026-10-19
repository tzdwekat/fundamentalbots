"""In-process background runs started from the page: refresh (prepare only), cheap and full analyses. One per ticker,
three at a time; refused when the launchd worker already holds the ticker. Each job has its own DB connection."""
import asyncio
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from fundabot.db.session import connect

log = logging.getLogger("fundabot")
MAX_RUNNING = 3
KINDS = ("refresh", "cheap", "full")
_lock = threading.Lock()
_running: dict[int, dict] = {}  # job id -> {"ticker", "kind", "thread"}


class JobRefused(Exception):
    pass


def running() -> dict[int, dict]:
    with _lock:
        return {k: {"ticker": v["ticker"], "kind": v["kind"]} for k, v in _running.items()}


def running_for(ticker: str) -> int | None:
    with _lock:
        return next((k for k, v in _running.items() if v["ticker"] == ticker.upper()), None)


def start(kind: str, ticker: str, profile: str) -> int:
    ticker = ticker.upper()
    if kind not in KINDS:
        raise JobRefused(f"unknown job kind {kind}")
    conn = connect()
    try:
        with _lock:
            if any(v["ticker"] == ticker for v in _running.values()):
                raise JobRefused(f"a job for {ticker} is already running")
            if len(_running) >= MAX_RUNNING:
                raise JobRefused(f"{MAX_RUNNING} jobs are already running; try again in a few minutes")
        if kind != "refresh":
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM run_queue WHERE ticker = %s AND status IN ('queued', 'started') LIMIT 1", (ticker,))
                if cur.fetchone():
                    raise JobRefused(f"the worker has {ticker} queued or running; wait for it or let it finish")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO jobs (ticker, kind, profile) VALUES (%s, %s, %s) RETURNING id", (ticker, kind, profile))
            job_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    t = threading.Thread(target=_run, args=(job_id, kind, ticker, profile), daemon=True, name=f"job-{job_id}")
    with _lock:
        _running[job_id] = {"ticker": ticker, "kind": kind, "thread": t}
    t.start()
    return job_id


def _run(job_id: int, kind: str, ticker: str, profile: str) -> None:
    from fundabot.cli import PERSONAS_ALL, build_adapters
    from fundabot.orchestrate import pipeline

    started = datetime.now(timezone.utc)
    conn = connect()
    report, error, cost = None, None, 0.0
    try:
        if kind == "refresh":
            pipeline.prepare(conn, ticker, force=True)
        else:
            from fundabot.retrieval.embed import OpenAIEmbedder

            adapters, screen, judges, guidance = build_adapters(kind == "full", "", None, 3, profile=profile)
            if kind == "full":
                report = asyncio.run(pipeline.analyze_full(conn, ticker, PERSONAS_ALL, adapters, screen, judges, OpenAIEmbedder(), out_dir=Path("reports"), guidance_adapter=guidance))
            else:
                report = asyncio.run(pipeline.analyze(conn, ticker, PERSONAS_ALL, adapters, OpenAIEmbedder(), out_dir=Path("reports"), guidance_adapter=guidance))
        with conn.cursor() as cur:
            cur.execute("SELECT coalesce(sum(m.cost_usd), 0) FROM model_calls m LEFT JOIN snapshots s ON s.id = m.snapshot_id WHERE m.created_at >= %s AND (s.ticker = %s OR m.snapshot_id IS NULL)",
                        (started, ticker))
            cost = float(cur.fetchone()[0])
    except Exception as e:  # the row records it; the page shows it
        log.exception("job %s (%s %s) failed", job_id, kind, ticker)
        error = str(e)[:500]
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET status = %s, finished_at = now(), error = %s, report_path = %s, cost_usd = %s WHERE id = %s",
                            ("failed" if error else "done", error, str(report) if report else None, cost, job_id))
            conn.commit()
        finally:
            conn.close()
        with _lock:
            _running.pop(job_id, None)


def status(job_id: int) -> dict | None:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, ticker, kind, profile, status, started_at, finished_at, error, report_path, cost_usd FROM jobs WHERE id = %s", (job_id,))
            r = cur.fetchone()
    finally:
        conn.close()
    if r is None:
        return None
    d = dict(zip(["id", "ticker", "kind", "profile", "status", "started_at", "finished_at", "error", "report_path", "cost_usd"], r))
    if d["report_path"]:
        p = Path(d["report_path"])
        d["report_url"] = f"/reports/{p.parent.name}/{p.name}"
    return d


def wait(job_id: int, timeout: float = 60) -> None:
    with _lock:
        t = _running.get(job_id, {}).get("thread")
    if t:
        t.join(timeout)
