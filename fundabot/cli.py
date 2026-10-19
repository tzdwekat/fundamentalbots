import typer

app = typer.Typer(help="Fundabot: sourced fundamental analysis.", no_args_is_help=True)


@app.callback()
def _root():
    import logging

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("fundabot").setLevel(logging.INFO)


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


def cost_table(conn, sid: str) -> str:
    """Per-round token and USD totals for one snapshot. Rounds: persona, rebuttal, judge:rN; guidance and embedding
    calls are logged without a snapshot id (keyed by transcript / filing) and so are not in this table."""
    from fundabot.agents.runner import PERSONA_ORDER

    with conn.cursor() as cur:
        cur.execute("SELECT persona, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, cost_usd FROM model_calls WHERE snapshot_id = %s", (sid,))
        rows = cur.fetchall()
    rounds: dict[str, list] = {}
    for persona, *nums in rows:
        key = "persona" if persona in PERSONA_ORDER else persona.split(":")[0] if not persona.startswith("judge") else persona
        rounds.setdefault(key, [0, 0, 0, 0, 0, 0.0])
        rounds[key][0] += 1
        for i, n in enumerate(nums, 1):
            rounds[key][i] += float(n) if i == 5 else int(n)
    total = [sum(r[i] for r in rounds.values()) for i in range(6)]
    cols = ("calls", "input", "cached", "write", "output", "usd")
    lines = ["round".ljust(12) + "".join(c.rjust(12) for c in cols)]
    for name, r in list(rounds.items()) + [("total", total)]:
        lines.append(name.ljust(12) + "".join(f"{v:,}".rjust(12) for v in r[:5]) + f"{r[5]:.2f}".rjust(12))
    return "\n".join(lines)


@app.command()
def cost(ticker: str):
    """Print the latest snapshot's model-call cost by round (tokens, cached tokens, USD)."""
    from fundabot.db.session import connect
    from fundabot.orchestrate import snapshot

    conn = connect()
    snap = snapshot.latest_for(conn, ticker)
    if snap is None:
        raise typer.Exit(f"no snapshot for {ticker}")
    typer.echo(f"{ticker.upper()} snapshot {snap['id']}")
    typer.echo(cost_table(conn, snap["id"]))


AB_BAND = 1.5  # composite points: the triage verdict ships when it lands within this of the stored one


def latest_with_verdict(conn, ticker: str) -> str | None:
    """Newest snapshot of the ticker that has a stored `full` verdict (ingest/metrics mint newer snapshots with no grid)."""
    with conn.cursor() as cur:
        cur.execute("SELECT s.id FROM snapshots s JOIN verdicts v ON v.snapshot_id = s.id AND v.run_kind = 'full' WHERE s.ticker = %s ORDER BY s.created_at DESC LIMIT 1",
                    (ticker.upper(),))
        row = cur.fetchone()
    return row[0] if row else None


def judge_ab_table(old: dict, new: dict) -> str:
    """Stored verdict vs triage verdict: scores, escalation, reclassified claims, largest pass-through deltas, and the ship/widen rule."""
    lines = [f"composite  {old['composite_score']} -> {new['composite_score']}  (Δ {new['composite_score'] - old['composite_score']:+.1f})"]
    for k in ("quality_score", "value_score", "earnings_momentum_score", "accounting_risk_score", "judge_agreement"):
        lines.append(f"{k}  {old.get(k)} -> {new.get(k)}")
    lines.append(f"escalated  {len(new.get('escalated_ids', []))} of {new.get('n_claims', 0)}")

    def classes(v):
        return {**{c["id"]: ("consensus", c) for c in v["consensus_claims"]}, **{c["id"]: ("contested", c) for c in v["contested_claims"]}}
    before, after = classes(old), classes(new)
    moved = []
    for cid in sorted(set(before) & set(after)):
        if before[cid][0] != after[cid][0]:
            mat = max(before[cid][1]["judge"]["materiality"], after[cid][1]["judge"]["materiality"])
            moved.append((cid, f"{before[cid][0]}->{after[cid][0]}", mat, after[cid][1]["judge"].get("path", "screen")))
    lines += [f"reclassified: {cid} {how} (materiality {mat:.0f}{', escalated' if path == 'escalated' else ''})" for cid, how, mat, path in moved] or ["reclassified: none"]
    baseline = new.get("baseline_claim_means") or {}
    deltas = sorted(((abs(c["judge"]["evidence_strength"] - baseline[cid]["evidence_strength"]), cid, c["judge"]["evidence_strength"], baseline[cid]["evidence_strength"])
                     for cid, (_, c) in after.items() if c["judge"].get("path") == "screen" and cid in baseline), reverse=True)[:10]
    lines += ["screen-panel strength vs previous strong panel (largest deltas):"] + [f"  {cid}  {new_s:.1f} vs {old_s:.1f} (Δ {d:.1f})" for d, cid, new_s, old_s in deltas] if deltas else ["no baseline claim means"]
    reasons = []
    if abs(new["composite_score"] - old["composite_score"]) > AB_BAND:
        reasons.append(f"composite moved more than {AB_BAND}")
    reasons += [f"{cid} moved {how} at materiality {mat:.0f}" for cid, how, mat, path in moved if mat >= 4 and path == "screen"]  # the strong panel's own splits are its call
    lines.append("verdict: ship" if not reasons else "verdict: widen (" + "; ".join(reasons) + ")")
    return "\n".join(lines)


@app.command("judge-ab")
def judge_ab(ticker: str, snapshot: str = "", providers: str = "anthropic", judges: int = 3, out_dir: str = "reports", profile: str = "multi"):
    """Re-judge a stored snapshot with the screen/escalation panels (grid and rebuttals cached) and compare with its stored verdict."""
    import asyncio
    from pathlib import Path

    from psycopg.types.json import Jsonb

    from fundabot.db.session import connect
    from fundabot.debate import judge
    from fundabot.orchestrate.pipeline import analyze_full
    from fundabot.retrieval.embed import OpenAIEmbedder

    conn = connect()
    sid = snapshot or latest_with_verdict(conn, ticker)
    if sid is None:
        raise typer.BadParameter(f"no snapshot of {ticker.upper()} has a stored full verdict")
    with conn.cursor() as cur:
        cur.execute("SELECT verdict FROM verdicts WHERE snapshot_id = %s AND run_kind = 'full'", (sid,)); old = cur.fetchone()[0]
        cur.execute("SELECT verdict FROM verdicts WHERE snapshot_id = %s AND run_kind = 'triage'", (sid,)); prior = cur.fetchone()
    if prior and prior[0].get("baseline_claim_means"):
        baseline = prior[0]["baseline_claim_means"]
    else:  # the previous pipeline's round-2 rows (no _escalated marker) are about to be overwritten: keep their per-claim means
        rows = [j for j in judge.load_judgements(conn, sid, 2) if "_escalated" not in j["output"]]
        baseline = judge.mean_scores(rows, sorted({c["claim_id"] for j in rows for c in j["output"]["claims"]}))
    adapters, screen_judges, judge_adapters, _ = build_adapters(True, providers, None, judges, profile=profile)
    asyncio.run(analyze_full(conn, ticker, PERSONAS_ALL, adapters, screen_judges, judge_adapters, OpenAIEmbedder(), out_dir=Path(out_dir), run_kind="triage", snapshot_id=sid))
    with conn.cursor() as cur:
        cur.execute("UPDATE verdicts SET verdict = verdict || %s WHERE snapshot_id = %s AND run_kind = 'triage' RETURNING verdict", (Jsonb({"baseline_claim_means": baseline}), sid))
        new = cur.fetchone()[0]
    conn.commit()
    typer.echo(f"{ticker.upper()} snapshot {sid}")
    typer.echo(judge_ab_table(old, new))


from fundabot import profiles as _profiles  # noqa: E402

_MULTI = _profiles.load("multi")
JUDGE_MODELS = _MULTI["escalation"]  # the strong panel (views of the `multi` profile; see fundabot/profiles.yaml)
SCREEN_MODELS = _MULTI["screen"]  # --tier screen: cheap personas, same judges


SCREEN_JUDGE_MODELS = SCREEN_MODELS  # the screen panel: one cheap judge per provider scores every claim first


def build_adapters(full: bool, providers: str, model: str | None, judges: int, tier: str = "default", profile: str = "multi"):
    """(persona adapters, screen judges, escalation judges, guidance adapter) from the profile. The judge panels are empty
    unless --full; --providers narrows the persona set within the profile; --judges 1 keeps each panel's first provider;
    --model overrides the Anthropic persona model. Missing keys fail here, loudly."""
    if tier not in ("default", "screen"):
        raise typer.BadParameter("--tier must be default or screen")
    try:
        prof = _profiles.load(profile)
    except KeyError as e:
        raise typer.BadParameter(str(e))
    persona_models = dict(prof["screen"] if tier == "screen" else prof["personas"])
    if model and "anthropic" in persona_models and tier != "screen":
        persona_models["anthropic"] = model
    wanted = [p.strip() for p in providers.split(",") if p.strip()]
    if full and wanted == ["anthropic"]:  # the historical default: --full means every provider the profile has
        wanted = list(persona_models)
    unknown = [p for p in wanted if p not in persona_models]
    if unknown:
        raise typer.BadParameter(f"profile {profile!r} has no persona provider {unknown}; it has {list(persona_models)}")
    adapters = [make_adapter(p, persona_models[p]) for p in wanted]
    (gp, gm), = prof["guidance"].items()
    guidance = make_adapter(gp, gm, 8000)
    if not full:
        return adapters, [], [], guidance
    if judges not in (1, 3):
        raise typer.BadParameter("--judges must be 1 or 3")
    screen_models, strong_models = prof["screen"], prof["escalation"]
    if judges == 1:
        screen_models, strong_models = dict([next(iter(screen_models.items()))]), dict([next(iter(strong_models.items()))])
    screen = [make_adapter(p, m, JUDGE_MAX_TOKENS) for p, m in screen_models.items()]
    judge_adapters = [make_adapter(p, m, JUDGE_MAX_TOKENS) for p, m in strong_models.items()]
    return adapters, screen, judge_adapters, guidance


@app.command()
def analyze(tickers: list[str], personas: str = "bull,bear,skeptic,quality,macro", model: str = "claude-sonnet-5-5",
            providers: str = "anthropic", full: bool = False, judges: int = 3, tier: str = "default", profile: str = "multi",
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
    adapters, screen_judges, judge_adapters, guidance_adapter = build_adapters(full, providers, model, judges, tier, profile)
    for t in tickers:
        try:
            if full:
                html = asyncio.run(analyze_full(conn, t, names, adapters, screen_judges, judge_adapters, embedder, force=force, rerun=rerun, out_dir=Path(out_dir),
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


def scan_runner(full: bool, providers: str = "anthropic", model: str | None = None, judges: int = 3, tier: str = "default", profile: str = "multi"):
    """A runner that opens its own DB connection per ticker (connections are not safe to share across concurrent tickers)."""
    import json
    from pathlib import Path

    from fundabot.db.session import connect
    from fundabot.orchestrate.pipeline import analyze as run, analyze_full
    from fundabot.retrieval.embed import OpenAIEmbedder

    adapters, screen_judges, judge_adapters, guidance_adapter = build_adapters(full, providers, model, judges, tier, profile)

    async def runner(ticker: str, full_run: bool, force: bool = False) -> dict:
        conn = connect()
        try:
            if full_run:
                html = await analyze_full(conn, ticker, PERSONAS_ALL, adapters, screen_judges, judge_adapters, OpenAIEmbedder(), force=force, out_dir=Path("reports"), guidance_adapter=guidance_adapter)
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
         providers: str = "anthropic", concurrency: int = 3, tier: str = "default", profile: str = "multi"):
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
    results = asyncio.run(watchlist.scan(conn, names, full, max_usd, scan_runner(full, providers, tier=tier, profile=profile), concurrency))
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
def worker(limit: int = 1, providers: str = "anthropic,openai,gemini", tier: str = "default", profile: str = "multi"):
    """Run queued full analyses whose wait has elapsed."""
    import asyncio

    from fundabot.db.session import connect
    from fundabot.orchestrate import triggers

    runner = scan_runner(True, providers, tier=tier, profile=profile)

    async def run_full(ticker):
        return (await runner(ticker, True, force=True))["report"]  # a new filing or transcript means a new snapshot
    for d in asyncio.run(triggers.worker(connect(), run_full, limit=limit)):
        typer.echo(f"{d['ticker']} {d['period_key']}: {d['status']} {d['report'] or d['error']}")


@app.command()
def schedule(action: str = typer.Argument(..., help="install | uninstall | status"), dry_run: bool = False):
    """launchd jobs: poll and worker every 10 minutes, cheap watchlist scan every Sunday 06:00."""
    from pathlib import Path

    from fundabot.orchestrate import schedule as sched

    if action == "install":
        if dry_run:
            for label, xml in sched.plists(Path(__import__("sys").argv[0]).resolve(), Path.cwd(), Path.home() / "Library" / "Logs" / "fundabot").items():
                typer.echo(f"--- {label}\n{xml}")
            return
        for p in sched.install():
            typer.echo(f"installed {p}")
    elif action == "uninstall":
        typer.echo("removed: " + ", ".join(sched.uninstall()))
    elif action == "status":
        jobs = sched.status()
        typer.echo("\n".join(jobs) if jobs else "no fundabot jobs loaded")
    else:
        raise typer.BadParameter("action must be install, uninstall or status")


@app.command()
def calibrate(horizon: str = "30,90,180"):
    """Score past verdicts against forward returns and the next earnings surprise."""
    import json

    from fundabot.db.session import connect
    from fundabot.eval import calibration

    horizons = tuple(int(h) for h in horizon.split(",") if h.strip())
    for h, stats in calibration.run(connect(), horizons=horizons).items():
        if stats.get("error"):
            typer.echo(f"{h}d: {stats['error']}")
        elif stats.get("insufficient_history"):
            typer.echo(f"{h}d: insufficient history (n={stats['n']}, need 10)")
        else:
            typer.echo(f"{h}d: n={stats['n']} " + json.dumps({k: v for k, v in stats.items() if k not in ('insufficient_history', 'n')}, default=lambda x: round(x, 3) if isinstance(x, float) else x))


@app.command()
def review(ticker: str = typer.Argument(None), agent: str = "", vote: str = "", note: str = "", labels: bool = False, n: int = 10):
    """List the most contested verdicts, or record a thumbs up/down for an agent's thesis: review NVDA --agent bear/openai --vote up."""
    from fundabot.db.session import connect
    from fundabot.eval import review as rev

    conn = connect()
    if labels:
        for persona, provider, up, down in rev.label_counts(conn):
            typer.echo(f"{persona:<8} {provider:<10} up {up:>3}  down {down:>3}")
        return
    if ticker and agent and vote:
        typer.echo(f"recorded #{rev.label(conn, ticker, agent, vote, note)}")
        return
    for r in rev.contested(conn, n):
        typer.echo(f"{r['ticker']:<6} composite {r['composite']:>5}  dispersion {r['dispersion']:.2f}  agreement {r['agreement']:.2f}  contested {r['n_contested']}  ({r['created_at']:%Y-%m-%d})")
        c = r["top_claim"]
        if c:
            typer.echo(f"       {c['id']}: {c['text'][:140]}")
            for ref in c.get("refutations", [])[:2]:
                typer.echo(f"         refuted by {ref['by']}: {ref['reason'][:120]}")


if __name__ == "__main__":
    app()
