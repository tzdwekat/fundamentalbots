"""FastAPI routes. Pages are server-rendered Jinja templates; htmx swaps the chart, narrative, news and job fragments."""
import html
from pathlib import Path

import jinja2

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from fundabot import profiles
from fundabot.analytics import moves
from fundabot.eval import review
from fundabot.orchestrate import watchlist
from fundabot.report import charts
from fundabot.web import jobs, views

HERE = Path(__file__).parent
app = FastAPI(title="fundabot")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(env=jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True))  # .j2 files are not auto-detected as HTML
templates.env.globals.update(viz_css=charts.VIZ_CSS, profiles=profiles.names, estimates=profiles.estimates)


def pct(v, digits=1):
    return "n/a" if v is None else f"{v * 100:+.{digits}f}%"


def num(v, digits=2):
    return "n/a" if v is None else f"{v:,.{digits}f}"


templates.env.filters.update(pct=pct, num=num)


def render(name: str, request: Request, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    with views.connect() as conn:
        rows = views.overview(conn)
        spend, budget = views.month_spend(conn)
    return render("overview.html.j2", request, rows=rows, running=jobs.running(), spend=spend, budget=budget, all_estimates={p: profiles.estimates(p) for p in profiles.names()})


@app.post("/watchlist")
def watchlist_edit(ticker: str = Form(...), action: str = Form("add")):
    with views.connect() as conn:
        (watchlist.add if action == "add" else watchlist.remove)(conn, [ticker.upper()])
    return RedirectResponse("/", status_code=303)


def _window(w: int) -> int:
    return w if w in views.WINDOWS else 90


def _news_window(w: int) -> int:
    return w if w in views.NEWS_WINDOWS else 30


@app.get("/t/{ticker}", response_class=HTMLResponse)
def ticker_page(request: Request, ticker: str, window: int = Query(90), news: int = Query(30)):
    with views.connect() as conn:
        page = views.ticker_page(conn, ticker, _window(window), _news_window(news))
        spend, budget = views.month_spend(conn)
    return render("ticker.html.j2", request, running=jobs.running_for(ticker), spend=spend, budget=budget, **page)


@app.get("/t/{ticker}/chart", response_class=HTMLResponse)
def ticker_chart(request: Request, ticker: str, window: int = Query(90)):
    with views.connect() as conn:
        page = views.ticker_page(conn, ticker, _window(window), 30, refresh=False)
    return render("_chart.html.j2", request, **page)


@app.get("/t/{ticker}/narrative", response_class=HTMLResponse)
def narrative_get(request: Request, ticker: str, window: int = Query(90)):
    with views.connect() as conn:
        page = views.ticker_page(conn, ticker, _window(window), 30, refresh=False)
    return render("_narrative.html.j2", request, **page)


@app.post("/t/{ticker}/narrative", response_class=HTMLResponse)
async def narrative_post(request: Request, ticker: str, window: int = Form(90), profile: str = Form("multi")):
    window = _window(window)
    with views.connect() as conn:
        cik = views.cik_for(conn, ticker)
        if cik is None:
            raise HTTPException(404, f"no data for {ticker}")
        inputs = views.narrative_inputs(conn, ticker, cik, window, with_chunks=True)
        try:
            await moves.ensure_narrative(conn, views.narrative_adapter(profile), cik, window, inputs, force=True)
        except Exception as e:  # budget refusal or provider error: show it, never swallow it
            page = views.ticker_page(conn, ticker, window, 30, refresh=False)
            return render("_narrative.html.j2", request, error=str(e)[:300], **page)
        page = views.ticker_page(conn, ticker, window, 30, refresh=False)
    return render("_narrative.html.j2", request, **page)


@app.get("/t/{ticker}/news", response_class=HTMLResponse)
def news_fragment(request: Request, ticker: str, news: int = Query(30)):
    with views.connect() as conn:
        page = views.ticker_page(conn, ticker, 90, _news_window(news), refresh=False)
    return render("_news.html.j2", request, **page)


@app.get("/evidence/{snapshot_id}/{eid:path}", response_class=HTMLResponse)
def evidence(request: Request, snapshot_id: str, eid: str):
    with views.connect() as conn:
        e = views.evidence(conn, snapshot_id, eid)
    if e is None:
        raise HTTPException(404, f"{eid} does not resolve")
    return render("_evidence.html.j2", request, e=e)


@app.post("/jobs", response_class=HTMLResponse)
def job_start(request: Request, response: Response, ticker: str = Form(...), kind: str = Form(...), profile: str = Form("multi")):
    try:
        job_id = jobs.start(kind, ticker, profile)
    except jobs.JobRefused as e:
        return HTMLResponse(f'<div class="job refused">{html.escape(str(e))}</div>', status_code=409)
    resp = render("_job.html.j2", request, job=jobs.status(job_id))
    resp.headers["X-Job-Id"] = str(job_id)
    return resp


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_status(request: Request, job_id: int):
    job = jobs.status(job_id)
    if job is None:
        raise HTTPException(404)
    return render("_job.html.j2", request, job=job)


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request):
    with views.connect() as conn:
        rows, counts = review.contested(conn), review.label_counts(conn)
    return render("review.html.j2", request, rows=rows, counts=counts)


@app.post("/review", response_class=HTMLResponse)
def review_label(request: Request, ticker: str = Form(...), agent: str = Form(...), vote: str = Form(...), note: str = Form("")):
    with views.connect() as conn:
        review.label(conn, ticker, agent, vote, note)
    return HTMLResponse(f'<span class="labelled">labelled {html.escape(agent)} {html.escape(vote)}</span>')


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request):
    with views.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, period_key, reason, status, queued_at, not_before, started_at, finished_at, error FROM run_queue ORDER BY id DESC LIMIT 50")
            queue = [dict(zip(["ticker", "period_key", "reason", "status", "queued_at", "not_before", "started_at", "finished_at", "error"], r)) for r in cur.fetchall()]
            cur.execute("SELECT id, ticker, kind, profile, status, started_at, finished_at, error, report_path, cost_usd FROM jobs ORDER BY id DESC LIMIT 30")
            recent = [dict(zip(["id", "ticker", "kind", "profile", "status", "started_at", "finished_at", "error", "report_path", "cost_usd"], r)) for r in cur.fetchall()]
            cur.execute("SELECT date_trunc('day', created_at)::date, round(sum(cost_usd)::numeric, 2) FROM model_calls WHERE created_at >= now() - interval '14 days' GROUP BY 1 ORDER BY 1 DESC")
            daily = cur.fetchall()
        spend, budget = views.month_spend(conn)
    return render("queue.html.j2", request, queue=queue, recent=recent, running=jobs.running(), spend=spend, budget=budget, daily=daily)


@app.get("/calibration", response_class=HTMLResponse)
def calibration_page(request: Request):
    with views.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT run_at, horizon_days, n, stats FROM calibration_runs ORDER BY run_at DESC, horizon_days LIMIT 12")
            runs = [dict(zip(["run_at", "horizon_days", "n", "stats"], r)) for r in cur.fetchall()]
            cur.execute("SELECT o.snapshot_id, s.ticker, o.horizon_days, o.t0, o.t1, o.ticker_return, o.spy_return, o.excess_return, v.verdict->>'composite_score' "
                        "FROM outcomes o JOIN snapshots s ON s.id = o.snapshot_id LEFT JOIN verdicts v ON v.snapshot_id = o.snapshot_id AND v.run_kind = o.run_kind ORDER BY o.t0 DESC LIMIT 50")
            outcomes = [dict(zip(["snapshot_id", "ticker", "horizon_days", "t0", "t1", "ticker_return", "spy_return", "excess_return", "composite"], r)) for r in cur.fetchall()]
    return render("calibration.html.j2", request, runs=runs, outcomes=outcomes)


reports_dir = Path("reports")
if reports_dir.exists():
    app.mount("/reports", StaticFiles(directory=reports_dir), name="reports")
