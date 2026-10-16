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
    for t in tickers:
        try:
            if full:
                html = asyncio.run(analyze_full(conn, t, names, adapters, judge_adapters, embedder, force=force, rerun=rerun, out_dir=Path(out_dir)))
            else:
                html = asyncio.run(run(conn, t, names, adapters, embedder, force=force, rerun=rerun, out_dir=Path(out_dir)))
            typer.echo(f"{t.upper()}: {html}")
            if open_report:
                webbrowser.open(html.resolve().as_uri())
        except Exception as e:  # keep going; one bad ticker must not stop a batch
            conn.rollback()
            typer.echo(f"{t.upper()}: ERROR {type(e).__name__}: {e}", err=True)


if __name__ == "__main__":
    app()
