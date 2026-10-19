# Phase 5b Web App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local web app that shows, per ticker, the price picture with event markers, a cited "what moved it" narrative, a code-computed outlook, recent news and the bull/bear decision panel, plus overview, review, queue and calibration pages with Analyze buttons that show cost.

**Architecture:** New analytics (`technicals.py`, `moves.py`) and ingest (`news.py`) modules compute everything in code and store narratives; `fundabot/web/` is a FastAPI + Jinja + htmx front end that reads stored structures and starts pipeline runs as in-process background jobs. One new SVG chart helper. Migration 007 adds `news`, `narratives`, `jobs`.

**Tech Stack:** FastAPI, uvicorn, Jinja2, htmx (vendored), pandas, existing adapters/`run_batch`, existing grounding.

**Spec:** docs/superpowers/specs/2026-10-19-fundabot-phase5b-webapp-design.md

## Global Constraints

- Every number on a page is computed in code or copied from stored structures; the narrative's numbers must match the stats table, a metric, or quoted news/chunk text (`eval.grounding` tolerances).
- Every narrative sentence cites at least one resolvable id (`price:`, `news:`, `filing:`, `metric:`, `chunk:`); failing sentences are dropped and counted.
- A page load never runs `prepare()` and never calls a model. Model spend starts only from a button that shows the estimate.
- Narrative model: the profile's `guidance` role; calls go through `run_batch` with persona `narrative:<window>`.
- Server binds 127.0.0.1:8787; no auth. Dependencies added: `fastapi`, `uvicorn[standard]`, `python-multipart`, `httpx` (tests).
- `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot` before `uv run`; migrations via `fundabot init-db`.

## Review Focus

1. A ticker with fewer than 200 trading days of prices: stats fall back to `None` for the 200-day fields and the outlook label is `insufficient data`, never a crash (Task 1 test `test_short_history_is_insufficient`).
2. A news item whose url repeats with a different published time is a separate row; the same url and time is deduped (Task 2 test `test_news_key_is_url_and_time`).
3. A narrative sentence that quotes a number only in a news item's text (not the stats table) is supported (Task 3 test `test_number_from_news_text_is_supported`).
4. Starting a full job while the worker's `run_queue` holds the ticker is refused with a message, not silently queued (Task 4 test `test_job_refused_when_worker_has_ticker`).
5. The ticker page for a watchlist ticker with no snapshot renders with `insufficient data` and the Refresh button (Task 4 test `test_ticker_page_without_snapshot`).

---

### Task 1: Price statistics, events, outlook (code only)

**Files:** create `fundabot/analytics/technicals.py`; test `tests/test_technicals.py`.

**Interfaces:**
- `Event` dataclass: `date: date, kind: str ("filing"|"earnings"|"transcript"|"split"|"news"), id: str, title: str, url: str | None`.
- `price_stats(closes: pd.DataFrame, asof: date | None = None) -> dict`: input is `prices.load` output (columns `date`, `close`, sorted). Keys: `asof, close, ret_1m, ret_3m, ret_6m, ret_12m, high_52w, low_52w, drawdown_52w, dist_52w_low, ma50, ma200, above_ma50, above_ma200, ma50_slope_20d, vol_20d, n_days`. Month windows are calendar months via `close_on_or_before`; `ma50_slope_20d = ma50[t] / ma50[t-20] - 1`; `vol_20d` is the annualised std of daily log returns over 20 days; any field whose window is missing is `None`.
- `largest_moves(closes, window_days: int, n: int = 10, asof=None) -> list[dict]`: `{"id": "price:YYYY-MM-DD", "date", "close", "ret"}` sorted by `abs(ret)` desc, daily simple returns.
- `match_events(moves: list[dict], events: list[Event], trading_dates: list[date], within: int = 2) -> list[dict]`: each move gains `"events": [Event…]` whose date is within `within` trading days (by index in `trading_dates`).
- `events_for(conn, cik: int, ticker: str, start: date, end: date, list_filings=edgar_filings.list_filings) -> list[Event]`: filings from the submissions list (form in 10-K, 10-Q, 8-K, 10-K/A, 10-Q/A; title `"<form>"`, url the primary doc), earnings from `surprises` (title `"EPS <actual> vs <est>"`), transcripts (`transcripts` table dates, title `"Earnings call FY..Q.."`), splits, news from `news` (Task 2; title = headline, url). `id`s: `filing:<accession>`, `earnings:<date>`, `transcript:<key>`, `split:<date>`, `news:<id>`.
- `outlook(stats: dict, metrics: list[Metric]) -> dict`: `{"label", "components": {"momentum": {...}, "valuation": {...}, "trend": {...}}, "rule"}`. Momentum from the last four quarterly `revenue_yoy` and `eps_diluted_yoy`, `eps_beat_streak@current`, `eps_estimate_revision_90d@current`; direction `accelerating` if mean of last two > mean of previous two + 0.02 (revenue), `decelerating` if < −0.02, else `steady`. Valuation from `pe_5y_percentile@current` and `ev_sales_5y_percentile@current` (`stretched` ≥ 0.8, `cheap` ≤ 0.2, else `fair`). Trend: `up` if above both averages and slope > 0, `down` if below both and slope < 0, `turning_up` if below the 200-day, slope > 0 and drawdown_52w < −0.2, else `mixed`. Label table (first match): insufficient data (any component missing) → `insufficient data`; momentum accelerating/steady & trend up & valuation stretched → `growing / extended`; momentum accelerating/steady & trend up → `growing / fair`; trend turning_up → `bottoming`; momentum decelerating & trend down → `declining`; momentum decelerating → `decelerating`; trend up → `recovering`; else `mixed`. `rule` is the matched line as text.

- [ ] **Step 1: Failing tests** (synthetic 400-day series built with `pd.date_range` business days): `test_price_stats_windows_and_mas` (known values: a linear series so returns and averages are hand-computable), `test_largest_moves_sorted_by_abs`, `test_match_events_within_two_trading_days` (an event 2 trading days after a move matches, 3 does not), `test_short_history_is_insufficient` (120 days → `ma200 is None`, `outlook` label `insufficient data`), `test_outlook_labels` (hand-built stats + `Metric` lists for `growing / extended`, `bottoming`, `declining`), `test_events_for_merges_sources` (db: insert a surprise, a split, a news row; `list_filings` faked with one 8-K → four events with the right ids).
- [ ] **Step 2:** `uv run pytest tests/test_technicals.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: price statistics, event matching and outlook rule`.

### Task 2: News ingest and migration 007

**Files:** create `fundabot/ingest/news.py`, `fundabot/db/schema/007_webapp.sql`; modify `tests/test_db.py` (migration count 7); test `tests/test_news.py`.

**Interfaces:**
- `007_webapp.sql`:
  ```sql
  CREATE TABLE news (id text PRIMARY KEY, cik int NOT NULL, published timestamptz NOT NULL, publisher text, site text, title text NOT NULL, url text NOT NULL, text text, kind text NOT NULL, fetched_at timestamptz DEFAULT now());
  CREATE INDEX news_cik_published ON news (cik, published DESC);
  CREATE TABLE narratives (cik int NOT NULL, window_days int NOT NULL, inputs_sha text NOT NULL, output jsonb NOT NULL, dropped int NOT NULL DEFAULT 0, cost_usd numeric NOT NULL DEFAULT 0, created_at timestamptz DEFAULT now(), PRIMARY KEY (cik, window_days));
  CREATE TABLE jobs (id serial PRIMARY KEY, ticker text NOT NULL, kind text NOT NULL, profile text NOT NULL, status text NOT NULL DEFAULT 'running', started_at timestamptz DEFAULT now(), finished_at timestamptz, error text, report_path text, cost_usd numeric);
  ```
- `news.news_id(url: str, published: datetime) -> str` (sha256 of `url|published isoformat`, 16 hex chars).
- `news.fetch(ticker: str, limit: int = 100, get=fmp.get) -> list[dict]`: `news/stock` + `news/press-releases` (`symbols=`, `limit=`), rows normalised to `{"id","published","publisher","site","title","url","text","kind"}` with `kind` `news` or `press`.
- `news.ingest(conn, cik, ticker, fetch=fetch, max_age_hours: float = 6) -> int`: skips when the newest stored `fetched_at` is younger than `max_age_hours`; `INSERT … ON CONFLICT (id) DO NOTHING`; returns rows inserted.
- `news.load(conn, cik, days: int, until: date | None = None) -> list[dict]` newest first.

- [ ] **Step 1: Failing tests:** `test_news_key_is_url_and_time`, `test_fetch_normalises_both_kinds` (fake `get` returns the FMP shapes), `test_ingest_dedupes_and_respects_max_age` (db), `test_load_window` (db).
- [ ] **Step 2:** FAIL. **Step 3:** implement; `fundabot init-db`; `tests/test_db.py` expects 7 migrations. **Step 4:** `uv run pytest tests/test_news.py tests/test_db.py -q` → PASS. **Step 5:** Commit `feat: FMP news ingest; webapp tables`.

### Task 3: Cited move narrative and the price chart

**Files:** create `fundabot/analytics/moves.py`; modify `fundabot/report/charts.py` (`price_chart`), `fundabot/eval/grounding.py` (expose `supported(value, values, texts)` if not already callable from outside); test `tests/test_moves.py`, `tests/test_charts.py` (extend).

**Interfaces:**
- `moves.NARRATIVE_SCHEMA`: `{"sentences": [{"text": str, "evidence": [str]}], "one_line": str}` (strict, additionalProperties false).
- `moves.build_inputs(conn, cik, ticker, window_days, stats, moves_with_events, news, metrics, chunks_by_id) -> dict` and `moves.inputs_sha(inputs) -> str` over `(window_days, move dates, news ids, snapshot_id)`.
- `moves.build_message(inputs) -> str` with blocks `PRICE_STATS`, `LARGEST_MOVES (with nearby events)`, `NEWS (id, date, publisher, title, first 300 chars)`, `EARNINGS`, `EXCERPTS` (the window's filing/transcript chunks, retrieved with queries `["what changed this quarter", "guidance outlook demand", "results compared to expectations"]`, 6k tokens; reuse `retrieval.query.retrieve` with the snapshot's accessions filtered to the window).
- `moves.system_prompt() -> str`: at most six sentences, each about one driver of the move, each with evidence ids from the inputs only, no advice, no numbers that are not in the inputs.
- `moves.ground_narrative(output, stats, metrics, news_by_id, chunks_by_id, event_ids: set[str]) -> dict`: `{"sentences": [kept with "unsupported_numbers": []], "dropped": [...], "one_line", "stats"}`; an id resolves if it is a known event id, `news:` in `news_by_id`, `chunk:` in `chunks_by_id`, or `metric:` resolving as in `eval.grounding`; numbers are supported by `stats` values, shown metric values, or the text of a cited news item or chunk.
- `moves.ensure_narrative(conn, adapter, cik, ticker, window_days, inputs, force=False) -> dict`: returns the stored row's output when `inputs_sha` matches and not `force`; otherwise calls the adapter through `run_batch(conn, None, [(adapter, f"narrative:{window_days}", system, user, NARRATIVE_SCHEMA)])`, grounds, upserts, returns `{"output", "dropped", "stale": bool, "cost_usd"}`.
- `moves.stale(conn, cik, window_days, inputs_sha) -> bool`.
- `charts.price_chart(title, closes: list[tuple[str, float]], ma50: list[tuple[str, float | None]], ma200: list[...], events: list[dict(date, kind, title)], highlights: list[str] (move dates)) -> str`: SVG in the existing style; marker glyph per kind; `<title>` tooltips; highlighted moves get a ring.

- [ ] **Step 1: Failing tests:** `test_message_has_blocks_and_ids`, `test_ground_drops_unresolvable_and_unsupported` (a sentence citing `news:zzz` is dropped; one with "fell 12%" when stats say −0.118 at quoted precision is kept), `test_number_from_news_text_is_supported`, `test_ensure_narrative_caches_by_inputs_sha` (db + FakeAdapter: second call with same sha makes no call; `force` makes one), `test_price_chart_markers_and_highlights` (SVG contains one marker per event, kind in class, highlighted dates ringed, palette validated as in the existing chart tests).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS, then `uv run pytest -q`. **Step 5:** Commit `feat: cited move narrative; price chart with event markers`.

### Task 4: Web app

**Files:** create `fundabot/web/__init__.py`, `fundabot/web/app.py`, `fundabot/web/jobs.py`, `fundabot/web/views.py` (page data assembly), `fundabot/web/templates/{base,overview,ticker,_chart,_narrative,_news,review,queue,calibration}.html.j2`, `fundabot/web/static/{htmx.min.js,app.css}`; modify `fundabot/cli.py` (`web` command), `fundabot/profiles.yaml` (`estimate_usd` per profile), `fundabot/profiles.py` (`estimates(name) -> dict`), `pyproject.toml`; tests `tests/test_web.py` (FastAPI `TestClient`, fixture DB via `conftest`).

**Interfaces:**
- `views.ticker_page(conn, ticker, window_days=90, news_days=30, refresh_prices=True) -> dict` keys: `ticker, stats, chart_svg, moves, outlook, narrative (or None), narrative_stale, news, panel (composite, veto_note, bull, bear, red_flags, catalysts, summary, report_url, verdict_date), snapshot (or None), reports (list), jobs (running for ticker)`. Refreshes prices via `prices.ingest` when the newest stored date is older than the last weekday, and news via `news.ingest` (6 h); both wrapped so a network failure logs and renders the stored data.
- `views.overview(conn) -> list[dict]` per watchlist ticker: `ticker, close, ret_1m, ret_3m, ret_6m, outlook_label, composite, verdict_date, queue_status, running_job`.
- `views.evidence(conn, snapshot_id, eid) -> dict` for the drawer: metric line or chunk text or news or filing link.
- `jobs.start(kind: "cheap"|"full"|"refresh", ticker, profile) -> int` (job id) and `jobs.status(job_id) -> dict`; refuses (`JobRefused`) when a web job for the ticker is running, when more than three jobs run, or (full/cheap) when `run_queue` has a `queued`/`started` row for the ticker. Each job runs in a thread with its own connection: `refresh` → `pipeline.prepare`; `cheap` → `pipeline.analyze`; `full` → `pipeline.analyze_full`, adapters from `cli.build_adapters(..., profile=)`; writes the `jobs` row; cost = sum of `model_calls` since start for the ticker's snapshot.
- Routes: `GET /`, `POST /watchlist` (add/remove), `GET /t/{ticker}?window=30|90|180&news=7|30|90`, `GET /t/{ticker}/chart`, `GET /t/{ticker}/narrative`, `POST /t/{ticker}/narrative` (generate; shows cost), `GET /t/{ticker}/news`, `GET /evidence/{snapshot_id}/{eid}`, `POST /jobs` (form: ticker, kind, profile), `GET /jobs/{id}` (htmx fragment, polled every 5 s), `GET /review`, `POST /review` (label), `GET /queue`, `GET /calibration`, `/reports/...` static, `/static/...`.
- `fundabot web --host 127.0.0.1 --port 8787` runs uvicorn.
- `profiles.yaml` gains per profile `estimate_usd: {cheap: 0.45, full: 3.0}` (multi), `{0.45, 0.95}` (anthropic), `{0.4, 0.8}` (openai), `{0.5, 0.9}` (gemini); `profiles.load` ignores it; `profiles.estimates(name)` returns it.

- [ ] **Step 1: Failing tests** (`TestClient`, fixture DB with the AAPL fixture snapshot from `tests/test_pipeline.fixture_prepare`, `prices` and news refresh monkeypatched off): `test_overview_lists_watchlist_with_returns`, `test_ticker_page_without_snapshot` (ticker added to the watchlist only → 200, "insufficient data", Refresh button, no Analyze cost missing), `test_ticker_page_sections` (stats, chart svg, outlook label, news list, bull/bear columns from a stored verdict, report link), `test_narrative_requires_post_and_shows_estimate` (GET shows the button with "~$0.03"; POST with FakeAdapter patched into the profile generates and renders citations), `test_evidence_drawer_resolves_metric_chunk_news`, `test_job_refused_when_worker_has_ticker` (insert a `run_queue` row → POST /jobs returns 409 with the message), `test_job_runs_and_reports_cost` (kind refresh with `pipeline.prepare` monkeypatched → status done), `test_review_label_roundtrip`, `test_queue_shows_spend_vs_budget`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_web.py -q` → PASS; `uv run pytest -q` → PASS. **Step 5:** Commit `feat: web app — overview, ticker picture, review, queue, calibration`.

### Task 5: Live check and docs

- [ ] **Step 1:** `fundabot web` in the background; `curl` `/`, `/t/AAPL?window=90`, `/t/NVDA`, `/queue`, `/calibration`, `/review`; confirm each returns 200 and the ticker page contains the chart, outlook label and bull/bear columns. Generate the AAPL 90-day narrative via `POST /t/AAPL/narrative` and read it: every sentence cites, numbers match, note the cost from `model_calls`.
- [ ] **Step 2:** Render `/t/AAPL` to `reports/_web_aapl.html` with curl and open it in the browser (`open`) for a visual pass: chart legibility, marker tooltips, dark mode.
- [ ] **Step 3:** README: "Web app" section (start command, pages, what is computed where, narrative cost, job rules) and the measured narrative cost. Commit `docs: web app`.
