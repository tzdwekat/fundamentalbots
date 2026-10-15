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


if __name__ == "__main__":
    app()
