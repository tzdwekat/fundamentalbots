import typer

app = typer.Typer(help="Fundabot: sourced fundamental analysis.", no_args_is_help=True)


@app.callback()
def _root():
    pass


@app.command("init-db")
def init_db():
    """Create or upgrade the database schema."""
    from fundabot.db.session import connect, run_migrations

    applied = run_migrations(connect())
    typer.echo("\n".join(applied) if applied else "up to date")


@app.command()
def ingest(tickers: list[str]):
    """Fetch facts, prices and the filing list; build a snapshot with metrics and flags."""
    from fundabot.db.session import connect
    from fundabot.orchestrate.pipeline import prepare

    conn = connect()
    for t in tickers:
        try:
            typer.echo(f"{t.upper()}: snapshot {prepare(conn, t)}")
        except Exception as e:  # keep going; one bad ticker must not stop a batch
            conn.rollback()
            typer.echo(f"{t.upper()}: ERROR {type(e).__name__}: {e}", err=True)


@app.command()
def metrics(ticker: str, json_out: bool = typer.Option(False, "--json"), force: bool = False):
    """Print the sourced metrics table for a ticker (ingests if needed)."""
    import json

    from fundabot.db.session import connect
    from fundabot.orchestrate import snapshot
    from fundabot.orchestrate.pipeline import prepare
    from fundabot.report.table import format_table

    conn = connect()
    sid = prepare(conn, ticker, force=force)
    ms = snapshot.load_metrics(conn, sid)
    if json_out:
        typer.echo(json.dumps([m.to_dict() for m in ms], default=str))
    else:
        typer.echo(f"{ticker.upper()} snapshot {sid}")
        typer.echo(format_table(ms))
        for f in snapshot.load_flags(conn, sid):
            typer.echo(f"RED FLAG [{f.severity}] {f.flag_id}: {f.detail}")


@app.command()
def verify(ticker: str):
    """Cross-check revenue, net income, total assets and OCF against yfinance statements."""
    from fundabot.db.session import connect
    from fundabot.orchestrate import snapshot
    from fundabot.orchestrate.pipeline import prepare
    from fundabot.report.verify import cross_check

    conn = connect()
    sid = prepare(conn, ticker)
    typer.echo(cross_check(ticker, snapshot.load_metrics(conn, sid)))


GUIDANCE_MODEL = "claude-haiku-5-5"  # transcript guidance extraction, ~$0.01 per call
JUDGE_MAX_TOKENS = 20000  # a judge scores every surviving claim (70+ on a full run) with notes


def make_adapter(provider: str, model: str | None = None, max_tokens: int | None = None):
    kw = {k: v for k, v in (("model", model), ("max_tokens", max_tokens)) if v}
    if provider == "anthropic":
        from fundabot.agents.adapters.anthropic import AnthropicAdapter
        return AnthropicAdapter(**kw)
    if provider == "openai":
        from fundabot.agents.adapters.openai import OpenAIAdapter
        return OpenAIAdapter(**kw)
    if provider == "gemini":
        from fundabot.agents.adapters.gemini import GeminiAdapter
        return GeminiAdapter(**kw)
    raise typer.BadParameter(f"unknown provider {provider}")


def grid_table(conn, sid: str) -> str:
    """Persona x provider table of score / confidence / hallucination%, with row and column standard deviations."""
    import statistics

    from fundabot.agents.runner import PERSONA_ORDER, load_analyses

    rows = load_analyses(conn, sid, status=None)
    providers = sorted({a["provider"] for a in rows})
    cell = {(a["persona"], a["provider"]): a for a in rows}
    scores = {k: a["grounded"]["score"] for k, a in cell.items() if a["status"] == "ok" and a["grounded"]}

    def sd(vals):
        return f"{statistics.pstdev(vals):.1f}" if len(vals) > 1 else "-"

    lines = ["persona".ljust(10) + "".join(p.ljust(20) for p in providers) + "row sd"]
    for persona in [p for p in PERSONA_ORDER if any(k[0] == p for k in cell)]:
        cells = []
        for pr in providers:
            a = cell.get((persona, pr))
            if a is None:
                cells.append("-")
            elif a["status"] != "ok" or not a["grounded"]:
                cells.append("failed")
            else:
                g = a["grounded"]
                cells.append(f"{g['score']} / {g['confidence']:.2f} / {g['stats']['hallucination_rate'] * 100:.0f}%")
        lines.append(persona.ljust(10) + "".join(c.ljust(20) for c in cells) + sd([v for (p, _), v in scores.items() if p == persona]))
    lines.append("col sd".ljust(10) + "".join(sd([v for (_, pr), v in scores.items() if pr == p]).ljust(20) for p in providers))
    return "\n".join(lines)


@app.command()
def grid(ticker: str):
    """Print the latest snapshot's persona x provider score grid."""
    from fundabot.db.session import connect
    from fundabot.orchestrate import snapshot

    conn = connect()
    snap = snapshot.latest_for(conn, ticker)
    if snap is None:
        raise typer.Exit(f"no snapshot for {ticker}")
    typer.echo(f"{ticker.upper()} snapshot {snap['id']}")
    typer.echo(grid_table(conn, snap["id"]))


JUDGE_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-5.4", "gemini": None}  # None = the adapter's default


def build_adapters(full: bool, providers: str, model: str, judges: int):
    """Persona adapters for the run and judge adapters (only for --full). Missing keys fail here, loudly."""
    provider_names = ["anthropic", "openai", "gemini"] if full and providers == "anthropic" else [p.strip() for p in providers.split(",") if p.strip()]
    adapters = [make_adapter(p, model if p == "anthropic" else None) for p in provider_names]
    if not full:
        return adapters, []
    if judges not in (1, 3):
        raise typer.BadParameter("--judges must be 1 or 3")
    judge_adapters = [make_adapter(p, JUDGE_MODELS[p], JUDGE_MAX_TOKENS) for p in (["anthropic"] if judges == 1 else ["anthropic", "openai", "gemini"])]
    return adapters, judge_adapters


@app.command()
def analyze(tickers: list[str], personas: str = "bull,bear,skeptic,quality,macro", model: str = "claude-sonnet-5-5",
            providers: str = "anthropic", full: bool = False, judges: int = 3,
            open_report: bool = typer.Option(False, "--open"), force: bool = False, rerun: bool = False, out_dir: str = "reports"):
    """Run the persona agents over a fresh snapshot and write a report. --full: 3 providers, rebuttal round, judges, verdict."""
    import asyncio
    import webbrowser
    from pathlib import Path

    from fundabot.db.session import connect
    from fundabot.orchestrate.pipeline import analyze as run, analyze_full
    from fundabot.retrieval.embed import OpenAIEmbedder

    conn, embedder = connect(), OpenAIEmbedder()
    names = [p.strip() for p in personas.split(",") if p.strip()]
    adapters, judge_adapters = build_adapters(full, providers, model, judges)
    guidance_adapter = make_adapter("anthropic", GUIDANCE_MODEL, 4000)
    for t in tickers:
        try:
            if full:
                html = asyncio.run(analyze_full(conn, t, names, adapters, judge_adapters, embedder, force=force, rerun=rerun, out_dir=Path(out_dir),
                                                guidance_adapter=guidance_adapter))
            else:
                html = asyncio.run(run(conn, t, names, adapters, embedder, force=force, rerun=rerun, out_dir=Path(out_dir), guidance_adapter=guidance_adapter))
            typer.echo(f"{t.upper()}: {html}")
            if open_report:
                webbrowser.open(html.resolve().as_uri())
        except Exception as e:  # keep going; one bad ticker must not stop a batch
            conn.rollback()
            typer.echo(f"{t.upper()}: ERROR {type(e).__name__}: {e}", err=True)


watch_app = typer.Typer(help="Manage the watchlist.")
app.add_typer(watch_app, name="watch")


@watch_app.command("add")
def watch_add(tickers: list[str], note: str = ""):
    from fundabot.db.session import connect
    from fundabot.orchestrate import watchlist

    typer.echo("added: " + ", ".join(watchlist.add(connect(), tickers, note)))


@watch_app.command("remove")
def watch_remove(tickers: list[str]):
    from fundabot.db.session import connect
    from fundabot.orchestrate import watchlist

    watchlist.remove(connect(), tickers)
    typer.echo("removed: " + ", ".join(t.upper() for t in tickers))


@watch_app.command("list")
def watch_list():
    from fundabot.db.session import connect
    from fundabot.orchestrate import watchlist

    for e in watchlist.entries(connect()):
        typer.echo(f"{e['ticker']:<8} cik {e['cik']:<10} added {e['added_at']:%Y-%m-%d}  last poll {e['last_poll'] or '-'}  {e['note'] or ''}")


def scan_runner(full: bool, providers: str = "anthropic", model: str = "claude-sonnet-5-5", judges: int = 3):
    """A runner that opens its own DB connection per ticker (connections are not safe to share across concurrent tickers)."""
    import json
    from pathlib import Path

    from fundabot.db.session import connect
    from fundabot.orchestrate.pipeline import analyze as run, analyze_full
    from fundabot.retrieval.embed import OpenAIEmbedder

    adapters, judge_adapters = build_adapters(full, providers, model, judges)
    guidance_adapter = make_adapter("anthropic", GUIDANCE_MODEL, 4000)

    async def runner(ticker: str, full_run: bool, force: bool = False) -> dict:
        conn = connect()
        try:
            if full_run:
                html = await analyze_full(conn, ticker, PERSONAS_ALL, adapters, judge_adapters, OpenAIEmbedder(), force=force, out_dir=Path("reports"), guidance_adapter=guidance_adapter)
            else:
                html = await run(conn, ticker, PERSONAS_ALL, adapters, OpenAIEmbedder(), force=force, out_dir=Path("reports"), guidance_adapter=guidance_adapter)
            with conn.cursor() as cur:
                cur.execute("SELECT verdict->>'composite_score', verdict->>'dispersion' FROM verdicts WHERE snapshot_id = (SELECT id FROM snapshots WHERE ticker = %s ORDER BY created_at DESC LIMIT 1) AND run_kind = 'full'", (ticker.upper(),))
                row = cur.fetchone()
            return {"composite": float(row[0]) if row else None, "dispersion": float(row[1]) if row else None, "report": str(html)}
        finally:
            conn.close()
    return runner


PERSONAS_ALL = ["bull", "bear", "skeptic", "quality", "macro"]


@app.command()
def scan(watchlist_flag: bool = typer.Option(False, "--watchlist"), tickers: str = "", full: bool = False, max_usd: float = 100.0,
         providers: str = "anthropic", concurrency: int = 3):
    """Analyze many tickers (cheap run by default, --full for the debate), stalest first, under a per-scan budget."""
    import asyncio

    from fundabot.db.session import connect
    from fundabot.orchestrate import watchlist

    conn = connect()
    names = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if watchlist_flag:
        names += [e["ticker"] for e in watchlist.entries(conn) if e["ticker"] not in names]
    if not names:
        raise typer.BadParameter("give --watchlist or --tickers A,B")
    results = asyncio.run(watchlist.scan(conn, names, full, max_usd, scan_runner(full, providers), concurrency))
    for r in results:
        if r["status"] == "ok":
            typer.echo(f"{r['ticker']:<8} ok   composite {r.get('composite')}  dispersion {r.get('dispersion')}  {r.get('report')}")
        else:
            typer.echo(f"{r['ticker']:<8} {r['status']}  {r['error']}")


@app.command()
def poll(dry_run: bool = False):
    """Diff watchlist tickers' EDGAR filings and transcript dates into the run queue (no model calls)."""
    from fundabot.db.session import connect
    from fundabot.orchestrate import triggers

    conn = connect()
    if dry_run:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, period_key, reason, status, not_before FROM run_queue ORDER BY id")
            for r in cur.fetchall():
                typer.echo(f"queue: {r}")
        typer.echo("dry run: not polling")
        return
    for q in triggers.poll(conn):
        typer.echo(f"queued {q['ticker']} {q['period_key']} ({q['reason']})")


@app.command()
def worker(limit: int = 1, providers: str = "anthropic,openai,gemini"):
    """Run queued full analyses whose wait has elapsed."""
    import asyncio

    from fundabot.db.session import connect
    from fundabot.orchestrate import triggers

    runner = scan_runner(True, providers)

    async def run_full(ticker):
        return (await runner(ticker, True, force=True))["report"]  # a new filing or transcript means a new snapshot
    for d in asyncio.run(triggers.worker(connect(), run_full, limit=limit)):
        typer.echo(f"{d['ticker']} {d['period_key']}: {d['status']} {d['report'] or d['error']}")


if __name__ == "__main__":
    app()
