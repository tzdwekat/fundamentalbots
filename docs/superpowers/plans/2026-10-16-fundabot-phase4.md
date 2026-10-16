# Fundabot Phase 4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcripts and guidance, quarterly consensus and ownership signals, a watchlist with event-triggered full runs and a weekly cheap scan under launchd, a calibration job, and a human review loop.

**Architecture:** Transcripts ride the existing section/chunk machinery under synthetic accessions `T{fy}Q{fq}`; guidance is a verified structured extraction stored as quotes with chunk citations; polling diffs per-ticker EDGAR submissions and FMP transcript dates into a per-quarter `run_queue` that a separate worker drains; calibration joins verdicts to forward returns; everything is CLI + launchd.

**Tech Stack:** as Phases 1–3; `claude-haiku-5-5` for guidance extraction; launchd plists; yfinance for SPY.

**Spec:** `docs/superpowers/specs/2026-10-16-fundabot-phase4-design.md`.

## Global Constraints

- New metrics carry `vendor_fmp` (FMP data) or `llm_extracted` (guidance, tone); guidance values are categorical (−1/0/+1) with the quotes and chunk ids in `inputs`.
- Transcript chunk ids are `T{fy}Q{fq}#call-{n}`; evidence `chunk:T2026Q3#call-12` resolves through the existing `chunks`/`filing_sections` tables; `filings` gets a row per transcript with `form='TRANSCRIPT'`.
- One `run_queue` row per `(cik, period_key)`; 8-K/10-Q signals set `not_before = queued + 3 days`, a transcript signal clears the wait.
- `poll` makes no model calls and bypasses the EDGAR disk cache; `worker` is the only thing that runs `analyze_full` from the queue.
- `scan` default is the cheap run; `--max-usd` default 100; stalest ticker first; 3 at a time; one DB connection per ticker.
- Calibration uses only prices after the verdict's price date; fewer than 10 scored verdicts prints "insufficient history" and no statistics.
- Tests: no network, no launchctl; db-marked tests use `fundabot_test`; the 157 existing tests keep passing.

## Review Focus

1. A transcript whose FMP fiscal quarter does not match our label (e.g. NVDA calls FY2027 Q2 what our offset labels Q2FY2027 too, but a company with a different offset may not): the transcript must still be ingested under FMP's own (fy, fq) and cited; metrics keyed by our labels must not silently mismatch (Task 1 test `test_transcript_key_uses_fmp_fiscal_fields`, Task 2 test `test_guidance_metric_period_uses_transcript_key`).
2. A guidance quote that the model paraphrased (not verbatim in the chunk) must be dropped, not stored (Task 2 test `test_paraphrased_quote_dropped`).
3. A second 8-K for the same quarter (amended 8-K/A, or a 10-Q arriving after the run) must not queue a second run (Task 5 test `test_same_quarter_signals_collapse`).
4. `scan` hitting `--max-usd` midway must stop cleanly and the next scan must start with the tickers it skipped (Task 4 tests `test_scan_stops_at_budget`, `test_scan_orders_stalest_first`).
5. `calibrate` on a verdict younger than the horizon must leave it pending, and a ticker with no prices after t0 (delisted) must produce an outcome row with nulls and a reason rather than crash (Task 7 tests `test_pending_verdict_not_scored`, `test_delisted_ticker_outcome_null`).

---

## File structure

```
fundabot/db/schema/004_phase4.sql      transcripts, guidance, institutional, insiders, watchlist, run_queue, outcomes, calibration_runs, reviews, benchmark_prices
fundabot/ingest/transcripts.py         dates + content -> transcripts, filing_sections, filings rows
fundabot/ingest/fmp.py                 + fetch/ingest/load for quarterly estimates, institutional summary, insider statistics
fundabot/analytics/guidance.py         GUIDANCE_SCHEMA, extract_guidance(), verify(), guidance_metrics()
fundabot/analytics/earnings.py         + next-quarter consensus/revisions, institutional and insider metrics
fundabot/orchestrate/pipeline.py       prepare: transcripts, guidance, new metrics; accessions gain T keys
fundabot/orchestrate/watchlist.py      add/remove/list; scan()
fundabot/orchestrate/triggers.py       poll(), worker(), period_key()
fundabot/orchestrate/schedule.py       plist text, install/uninstall/status
fundabot/eval/calibration.py           compute_outcomes(), statistics(), run()
fundabot/eval/review.py                contested(), label(), label_counts()
fundabot/cli.py                        watch, scan, poll, worker, schedule, calibrate, review
tests/fixtures/fmp_aapl_{transcript_dates,transcript_2026q3,estimates_quarter,institutional,insider_stats}.json (saved)
tests/test_transcripts.py test_guidance.py test_signals.py test_watchlist.py test_triggers.py test_schedule.py test_calibration.py test_review.py
```

---

### Task 1: Schema 004 and transcripts

**Files:** Create `fundabot/db/schema/004_phase4.sql`, `fundabot/ingest/transcripts.py`; modify `fundabot/ingest/fmp.py` (two fetchers), `fundabot/orchestrate/pipeline.py`, `fundabot/agents/context.py` (BASE_QUERIES), `fundabot/agents/personas/*.yaml` (one transcript query each). Test `tests/test_transcripts.py`.

**Interfaces:**
- Tables: `transcripts(cik, fy int, fq int, call_date date, content text, fetched_at, pk(cik, fy, fq))`; `guidance(cik, fy, fq, statements jsonb, tone text, tone_quote text, tone_chunk text, model text, pk(cik, fy, fq))`; `institutional(cik, quarter_end date, holders int, holders_change int, shares numeric, shares_change numeric, fetched_at, pk(cik, quarter_end))`; `insiders(cik, year int, quarter int, acquired numeric, disposed numeric, purchases int, sales int, fetched_at, pk(cik, year, quarter))`; `watchlist(ticker text pk, cik int, added_at, note text, last_poll timestamptz)`; `run_queue(id serial pk, ticker, cik, period_key text, reason text, accession text, queued_at timestamptz default now(), not_before timestamptz, started_at, finished_at, status text default 'queued', error text, unique(cik, period_key))`; `outcomes(snapshot_id, run_kind, horizon_days int, t0 date, t1 date, ticker_return numeric, spy_return numeric, excess_return numeric, next_eps_surprise numeric, max_drawdown numeric, reason text, computed_at, pk(snapshot_id, run_kind, horizon_days))`; `calibration_runs(id serial, run_at, horizon_days, n int, stats jsonb)`; `reviews(id serial, snapshot_id, agent text, vote text, note text, reviewed_at)`; `benchmark_prices(ticker text, date date, close numeric, pk(ticker, date))`.
- `fmp.fetch_transcript_dates(ticker) -> list[dict]` (`quarter, fiscalYear, date`), `fmp.fetch_transcript(ticker, year, quarter) -> dict | None` (`content`, `date`).
- `transcripts.key(fy, fq) -> str` = `f"T{fy}Q{fq}"`; `transcripts.ingest(conn, cik, ticker, n=4) -> list[str]` (keys present after ingest): fetch dates, for the newest `n` without a row fetch content, insert `transcripts`, `filing_sections (key, 'call', content)`, `filings (accession=key, cik, form='TRANSCRIPT', filed=call_date, period_end=call_date, items_ok=true)`.
- `pipeline.prepare`: after `edgar_filings.ingest`, `accessions += transcripts.ingest(...)` (errors logged, never fatal); `latest_10k` lookup filters `form == '10-K'` (already does; verify it tolerates T keys).
- `context.BASE_QUERIES` gains `"management outlook and guidance for next quarter"`; each persona YAML gains one transcript query (bull: "growth drivers management highlighted on the call"; bear: "analyst questions on risks weakness slowdown"; skeptic: "one-time items adjustments non-GAAP reconciliation discussed on the call"; quality: "pricing power retention competitive position discussed by management"; macro: "demand environment end markets macro commentary").

- [ ] Step 1: Failing tests: `test_transcript_key_uses_fmp_fiscal_fields` (ingest with monkeypatched fetchers from fixtures → keys `["T2026Q3", "T2026Q2", "T2026Q1", "T2025Q4"]` in `filings` with form TRANSCRIPT and `filing_sections` rows item `call`), `test_ingest_idempotent` (second ingest fetches no content: counter on the fake fetcher), `test_prepare_includes_transcript_keys` (fixture `prepare` → snapshot accessions contain `T2026Q3`; `embed_missing` + `retrieve` with FakeEmbedder can return a chunk whose id starts with `T2026Q3#call-`).
- [ ] Step 2–5: FAIL, implement, PASS, `init-db` applies 004, commit `feat: transcripts ingested as retrievable sections`.

---

### Task 2: Guidance extraction

**Files:** Create `fundabot/analytics/guidance.py`; modify `pipeline.prepare` (extraction after transcripts, before metrics), `fundabot/debate/judge.py` (consensus block admits `llm_extracted`), `fundabot/report/table.py` (ids). Test `tests/test_guidance.py`.

**Interfaces:**
- `GUIDANCE_SCHEMA` per the spec; `guidance.system_prompt()`; `guidance.build_message(key, chunks: list[Chunk]) -> str` (chunk id header then text, as the agent message does).
- `guidance.verify(output, chunks_by_id) -> dict`: keeps a statement only if `chunk` resolves and `quote` (whitespace-normalized) is a substring of that chunk's text; same for tone; returns the cleaned output and `dropped` count.
- `guidance.extract(conn, cik, key, adapter, chunks) -> dict | None` (cached by `guidance` row; uses `call_logged` with persona `guidance:{key}`; stores the verified output).
- `guidance.metrics(rows: list[dict], label_for_key) -> list[Metric]`: `guidance_direction@{label}` and `management_tone@{label}` with `inputs={"quotes": [...], "chunks": [...]}` and flag `llm_extracted`; direction from the revenue statement, else eps, else majority; `null_reason="no_guidance"` when no statement survived. `label_for_key("T2026Q3") -> "Q3FY2026"` is the identity mapping of FMP's fiscal fields (Review Focus 1: the period label uses FMP's fy/fq as-is, documented in the metric's `formula_id`).
- `prepare` runs extraction only when an adapter is passed (`prepare(conn, ticker, force, guidance_adapter=None)`); the CLI passes `AnthropicAdapter("claude-haiku-5-5", max_tokens=4000)` for `analyze`, `scan`, `worker`; `metrics`/`verify` pass None (no model calls).
- `judge.build_message`: CONSENSUS_DATA lists metrics with `vendor_fmp` or `llm_extracted`.

- [ ] Step 1: Failing tests: `test_verify_keeps_verbatim_quotes_only` / `test_paraphrased_quote_dropped`, `test_extract_caches` (db; FakeAdapter → 1 call then 0), `test_guidance_metrics_direction_and_tone` (revenue raised + eps lowered → +1 from revenue; tone cautious → −1; no statements → `no_guidance`), `test_guidance_metric_period_uses_transcript_key`, `test_judge_message_includes_llm_extracted`.
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: guidance and tone extracted from transcripts with verified quotes`.

---

### Task 3: Quarterly consensus, institutional and insider signals

**Files:** Modify `fundabot/ingest/fmp.py`, `fundabot/analytics/earnings.py`, `fundabot/report/table.py`, `fundabot/agents/personas/skeptic.yaml` (reads_most). Test `tests/test_signals.py`.

**Interfaces:**
- `fmp.fetch_all` adds `estimates_quarter` (`period=quarter&limit=8`), `institutional` (summary for the last two quarters: `year/quarter` derived from today), `insider_stats`; `fmp.ingest` stores `estimates` with `period='quarter'`, `institutional`, `insiders`; `fmp.load` returns them (`institutional` DataFrame[quarter_end, holders, holders_change, shares, shares_change], `insiders` DataFrame[year, quarter, acquired, disposed, purchases, sales]). Field maps: institutional `date, investorsHolding, investorsHoldingChange, numberOf13Fshares, numberOf13FsharesChange`; insiders `year, quarter, totalAcquired, totalDisposed, totalPurchases, totalSales`.
- `earnings.compute_earnings_metrics` adds: `eps_estimate_next_q`, `revenue_estimate_next_q` (first quarter row with `period_end > as_of` at the latest fetch), `eps_estimate_next_q_revision_30d/_90d` (same ±7-day rule), `inst_holders_change_pct = holders_change / (holders − holders_change)`, `inst_shares_change_pct` likewise, `insider_net_shares_90d = acquired − disposed` (latest quarter), `insider_buy_sell_ratio = purchases / sales` (`nonpositive_denominator` when sales = 0); all `vendor_fmp`, period `current`.

- [ ] Step 1: Failing tests on fixtures and hand-built frames: `test_quarterly_rows_stored_and_loaded` (db), `test_next_q_consensus_and_revision`, `test_institutional_and_insider_metrics`, `test_missing_ownership_data_is_null`.
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: quarterly consensus, 13F and insider signals`.

---

### Task 4: Watchlist and scan

**Files:** Create `fundabot/orchestrate/watchlist.py`; modify `fundabot/cli.py`. Test `tests/test_watchlist.py`.

**Interfaces:**
- `watchlist.add(conn, tickers: list[str], note="") -> list[str]` (resolves CIK via `ticker_to_cik`, upserts), `remove(conn, tickers)`, `entries(conn) -> list[dict]`.
- `watchlist.order_by_staleness(conn, tickers) -> list[str]`: tickers with no snapshot first, then oldest `snapshots.created_at` first.
- `async watchlist.scan(tickers, full: bool, max_usd: float, runner, concurrency=3) -> list[dict]`: `runner(ticker, full) -> dict` is injected (the CLI passes one that opens its own connection and calls `analyze`/`analyze_full`); before starting each ticker, if `month_spend(conn) − spend_at_start >= max_usd` stop and mark the rest `skipped: budget`; results `{ticker, status: ok|error|skipped, composite, dispersion, report, error}`.
- CLI: `watch add T...`, `watch remove T...`, `watch list`; `scan [--watchlist] [--tickers T,T] [--full] [--max-usd 100]` printing one line per ticker.

- [ ] Step 1: Failing tests (db): `test_watch_add_remove_list`, `test_scan_orders_stalest_first`, `test_scan_stops_at_budget` (fake runner that inserts a model_calls row of $60 per ticker, max_usd 100 → third ticker skipped), `test_scan_continues_past_one_error`.
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: watchlist and scan`.

---

### Task 5: Poll and worker

**Files:** Create `fundabot/orchestrate/triggers.py`; modify `fundabot/ingest/edgar_client.py` (`get_json(..., max_age_hours=0)` bypasses the cache), `fundabot/cli.py`. Test `tests/test_triggers.py`.

**Interfaces:**
- `triggers.period_key(form, report_date, fye, offset) -> str`: `FY{y}` for 10-K (fiscal year of report_date), else `Q{n}FY{y}` via `periods.fy_of/fq_of`; `triggers.transcript_period_key(fy, fq) -> f"Q{fq}FY{fy}"`.
- `triggers.poll(conn, list_filings=edgar_filings.list_filings_fresh, transcript_dates=fmp.fetch_transcript_dates, now=None) -> list[dict]`: for each watchlist entry, filings filed after `last_poll` (or the last 7 days on first poll) with form in `10-K, 10-Q` or `8-K` whose `items` contains `2.02`, plus transcript dates after `last_poll`; insert queue rows (`ON CONFLICT (cik, period_key) DO NOTHING`), transcript signals `UPDATE ... SET not_before = now()`; update `last_poll`; return the rows inserted. `edgar_filings.parse_submissions` must carry `items`.
- `triggers.worker(conn, run_full, limit=1) -> list[dict]`: rows `status='queued' AND not_before <= now()` oldest first, `run_full(ticker)` injected; status `done`/`error`; `BudgetExceeded` leaves the row queued and stops.
- CLI: `poll [--dry-run]` (prints what would be queued), `worker [--limit 1]`.

- [ ] Step 1: Failing tests (db, injected fetchers): `test_poll_queues_8k_202_10q_10k_and_transcript`, `test_same_quarter_signals_collapse` (8-K then 10-Q then transcript for one quarter → one row, `not_before` cleared by the transcript), `test_non_202_8k_ignored`, `test_worker_runs_only_ready_rows_and_records_status`, `test_worker_budget_leaves_row_queued`.
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: EDGAR and transcript triggers with a per-quarter run queue`.

---

### Task 6: launchd scheduling

**Files:** Create `fundabot/orchestrate/schedule.py`; modify `fundabot/cli.py`. Test `tests/test_schedule.py`.

**Interfaces:**
- `schedule.plists(binary: Path, workdir: Path, log_dir: Path) -> dict[str, str]`: label → plist XML for `com.fundabot.poll` (StartInterval 600), `com.fundabot.worker` (StartInterval 600), `com.fundabot.weekly` (StartCalendarInterval Weekday 0 Hour 6 Minute 0; `scan --watchlist`); each sets `WorkingDirectory`, `StandardOutPath`/`StandardErrorPath` under `log_dir`, and `EnvironmentVariables.UV_PROJECT_ENVIRONMENT` is not needed because `binary` is the venv's `fundabot` path.
- `schedule.install(run=subprocess.run, home=Path.home()) -> list[Path]` writes the files under `~/Library/LaunchAgents/` and runs `launchctl bootstrap gui/{uid} {plist}`; `uninstall` runs `launchctl bootout gui/{uid}/{label}` and removes; `status` runs `launchctl list | grep com.fundabot`.
- CLI: `schedule install|uninstall|status [--dry-run]`.

- [ ] Step 1: Failing tests: `test_plists_content` (parse with `plistlib`; labels, intervals, program arguments, log paths), `test_install_writes_and_bootstraps` (tmp home, fake `run` records commands).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: launchd schedule for poll, worker and weekly scan`.

---

### Task 7: Calibration

**Files:** Create `fundabot/eval/calibration.py`; modify `fundabot/cli.py`, `fundabot/ingest/prices.py` (`fetch_daily_closes` reused for SPY; `benchmark_prices` store/load). Test `tests/test_calibration.py`.

**Interfaces:**
- `calibration.candidates(conn, horizon_days, today) -> list[dict]`: verdicts (`run_kind='full'`) and cheap runs (a snapshot with ≥1 ok analysis and no verdict → `run_kind='cheap'`, composite = unweighted mean of `grounded.score`) whose `snapshots.price_date + horizon <= today` and no `outcomes` row.
- `calibration.compute_outcome(prices_df, spy_df, t0, horizon_days, surprises_df) -> dict`: `t1` = t0 + horizon (last available close on or before), returns, excess, `max_drawdown` over (t0, t1], `next_eps_surprise` = first surprise row with report_date > t0; if no price after t0 → values None, `reason="no_prices_after_t0"`.
- `calibration.statistics(rows: list[dict]) -> dict`: Spearman rho for composite/quality/value/earnings_momentum vs excess_return; tercile spread; rho(accounting_risk, max_drawdown); momentum hit rate; Brier; `n`. Returns `{"insufficient_history": True, "n": n}` when n < 10.
- `calibration.run(conn, horizons=(30, 90, 180), fetch_prices=prices.fetch_daily_closes, today=None) -> dict[int, dict]`: computes missing outcomes (prices via `prices.load` for the ticker, SPY via `benchmark_prices` refreshed once per run), stores them and a `calibration_runs` row per horizon, returns the stats.
- CLI: `calibrate [--horizon 30|90|180]` prints the stats table or "insufficient history (n=...)".

- [ ] Step 1: Failing tests: `test_pending_verdict_not_scored`, `test_compute_outcome_returns_and_drawdown` (hand-built price series), `test_delisted_ticker_outcome_null`, `test_statistics_insufficient_below_10`, `test_statistics_values` (12 hand-built rows with a known monotone relation → rho 1.0, brier computed), `test_run_stores_outcomes` (db).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: calibration of verdicts against forward returns`.

---

### Task 8: Review loop, README, first live use

**Files:** Create `fundabot/eval/review.py`; modify `fundabot/cli.py`, `README.md`. Test `tests/test_review.py`.

**Interfaces:**
- `review.contested(conn, n=10) -> list[dict]`: recent verdicts ranked by `dispersion × (1 − judge_agreement) + 0.05 × len(contested_claims)` desc; each with ticker, composite, dispersion, agreement, the top contested claim (text, refutations, conceded_by).
- `review.label(conn, ticker, agent, vote, note) -> int` (latest snapshot for the ticker; vote in `up|down`); `review.label_counts(conn) -> list[tuple[persona, provider, up, down]]`.
- CLI: `review` (list), `review T --agent bear/openai --vote up|down [--note]`, `review --labels`.

- [ ] Step 1: Failing tests (db): `test_contested_ranking`, `test_label_and_counts`.
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: README: the new commands, the trigger model (one full run per earnings quarter), the cost arithmetic from the spec, `schedule install` instructions, the calibration caveat.
- [ ] Step 6: Live: `fundabot watch add AAPL NVDA`, `fundabot poll --dry-run`, `fundabot analyze AAPL` (cheap run now includes transcript chunks and guidance; record cost), `fundabot calibrate` (expect "insufficient history"), `fundabot review`. Ledger what you see.
- [ ] Step 7: Commit `feat: review loop and phase 4 docs`.
