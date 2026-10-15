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


if __name__ == "__main__":
    app()
