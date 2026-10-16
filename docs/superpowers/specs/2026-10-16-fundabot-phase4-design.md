# Fundabot Phase 4: transcripts and richer signals, watchlist and triggers, scans, calibration, review loop

2026-10-16. Builds on Phases 1–3 and implements the remaining items of [docs/architecture.md](../../architecture.md):
transcripts (now available on the FMP Ultimate plan), the earnings-signal pieces that needed them, insider and
institutional data, EDGAR-triggered re-analysis, scheduled scans, the calibration job and the human review loop.
Tariq's decisions: FMP Ultimate is active; costs stay liberal; build the whole phase.

## Intent

Make the bot run itself on a watchlist: a new filing or earnings call triggers a fresh full analysis, the watchlist
is re-scored weekly, and after enough time passes the verdicts are checked against what actually happened, so the
sub-scores can be trusted or discounted on evidence rather than on faith. Transcripts give the agents management's
own words and a guidance signal the filings cannot provide.

## Goals

1. Earnings call transcripts for the last 4 calls are ingested, chunked, embedded and retrievable by every agent,
   cited as `chunk:T{fy}Q{fq}#call-{n}`.
2. A `guidance` extraction per transcript (structured, cheap model, every statement cites a transcript chunk) feeds
   `guidance_direction` and `management_tone` into the earnings-signals group, flagged `llm_extracted`.
3. Quarterly consensus (`eps_estimate_next_q`, `revenue_estimate_next_q`, their 30/90-day revisions), 13F ownership
   change and insider net activity join the metrics table, flagged `vendor_fmp`.
4. `fundabot watch add|remove|list` manages a watchlist in the database; `fundabot scan [--watchlist | --tickers ...]
   [--full]` runs the cheap or full analysis over it with bounded concurrency and a per-scan budget.
5. `fundabot poll` diffs each watchlist ticker's EDGAR submissions and FMP transcript dates and queues one full
   re-analysis per earnings quarter (or per 10-K); `fundabot worker` drains the queue; `fundabot schedule install`
   writes launchd jobs for the poll (every 10 min), the worker (every 10 min) and the weekly cheap scan.
6. `fundabot calibrate` joins stored verdicts to forward returns and the next reported surprise at 30/90/180 days and
   prints rank correlations per sub-score plus a Brier score for agent confidence; results are stored in `outcomes`.
7. `fundabot review` lists the 10 most contested recent verdicts; `fundabot review T --agent bear/openai --vote up|down
   --note ...` stores labels in `reviews` for prompt revision.

## Non-goals

- Provider batch APIs. Deferred until the universe size is decided: at the measured ~$1 per cheap run, a monthly
  Russell-1000 scan is ~$1,000 and batch would save about a third of the Anthropic and OpenAI share. `scan` takes any
  list now; `--batch` is a later addition behind the same adapter interface.
- A web UI, Prefect, or anything beyond launchd + CLI.
- Automatic prompt rewriting from review labels. Labels are stored and printed; Tariq edits the persona YAML.

## Anti-goals

- A guidance "number" produced by a model entering the metrics table as if computed. Guidance statements are stored
  as quoted text with chunk citations; the only metric values derived are categorical (direction, tone) and flagged.
- A trigger storm. One queued run per (ticker, accession); the poll never re-runs a snapshot it has already analysed.
- Calibration that quietly uses look-ahead data. Forward returns use the verdict's price date as t0 and only prices
  after it; a verdict younger than the horizon is "pending", never scored.

## Constraints

- FMP Ultimate endpoints verified 2026-10-16: `earning-call-transcript-dates`, `earning-call-transcript`
  (symbol, year, quarter; fiscal convention is the company's own), `analyst-estimates?period=quarter`,
  `institutional-ownership/symbol-positions-summary`, `insider-trading/statistics`, `insider-trading/search`.
- Polling uses each watchlist ticker's submissions JSON (`edgar_filings.list_filings`, cache bypassed) whose `items`
  field marks 8-K items (e.g. `2.02,9.01`), plus FMP `earning-call-transcript-dates`. The public 100-entry Atom feed is
  not used: it can overflow on busy days and offers no recovery.
- Guidance extraction model: `claude-haiku-5-5` (structured output, ~$0.01 per transcript). Configurable.
- launchd only (macOS); the plist points at the venv binary and logs to `~/Library/Logs/fundabot/`.
- Every new metric carries `vendor_fmp` or `llm_extracted`; the existing grounding and report conventions apply.

## Architecture additions

```
fundabot/ingest/transcripts.py        FMP transcript dates + content -> transcripts table; chunks via retrieval
fundabot/ingest/fmp.py                + quarterly estimates, institutional summary, insider statistics/search
fundabot/analytics/guidance.py        extract_guidance(): Haiku structured extraction -> guidance table; metrics
fundabot/analytics/earnings.py        + next-quarter consensus, revisions, inst/insider metrics
fundabot/orchestrate/watchlist.py     watch add/remove/list; scan runner with concurrency and budget
fundabot/orchestrate/triggers.py      EDGAR feed poll -> run_queue; worker drains the queue
fundabot/orchestrate/schedule.py      launchd plist generation/install/uninstall
fundabot/eval/calibration.py          outcomes join + statistics
fundabot/eval/review.py               contested list + label storage
fundabot/db/schema/004_phase4.sql     transcripts, guidance, institutional, insiders, watchlist, run_queue, outcomes, reviews
fundabot/cli.py                       watch, scan, poll, schedule, calibrate, review
```

### Transcripts

- `transcripts (cik, fy int, fq int, call_date date, content text, fetched_at, pk(cik, fy, fq))`. On `prepare`, the
  last 4 entries of `earning-call-transcript-dates` are fetched if missing. Snapshot `accessions` gains the keys
  `T{fy}Q{fq}` so retrieval, evidence resolution and the sources appendix work unchanged (`filing_sections` gets a
  row `(T{fy}Q{fq}, "call", content)` so `embed_missing` and `load_sections` need no special case).
- Persona retrieval queries gain transcript-oriented queries (`guidance for next quarter`, `questions from analysts
  about margins`); the base set gains `management outlook and guidance`.

### Guidance extraction

- One Haiku call per transcript, structured output:
  `{"statements": [{"metric": "revenue|eps|margin|capex|other", "period": "...", "direction": "raised|maintained|lowered|initiated|withdrawn",
  "quote": "...", "chunk": "chunk:T2026Q3#call-12"}], "tone": "confident|neutral|cautious", "tone_quote": "...", "tone_chunk": "..."}`.
  A statement whose chunk id does not resolve or whose quote is not found verbatim in that chunk is dropped.
- Stored in `guidance (cik, fy, fq, statements jsonb, tone text, model, pk(cik, fy, fq))`.
- Metrics (earnings-signals group, flag `llm_extracted`): `guidance_direction@Q{fq}FY{fy}` = +1 raised / 0 maintained
  or initiated / −1 lowered or withdrawn, from the revenue or EPS statement if present else the majority; `null`
  (`no_guidance`) when no statement survived. `management_tone@…` = +1 / 0 / −1. `inputs` carry the quotes and chunk ids.

### Richer consensus and ownership signals

- `estimates` already keys on `period`; quarterly rows are stored from `analyst-estimates?period=quarter`.
  Metrics: `eps_estimate_next_q`, `revenue_estimate_next_q` (first quarter ending after as_of),
  `eps_estimate_next_q_revision_30d/_90d` (same rule as the annual revisions).
- `institutional (cik, quarter_end date, holders int, holders_change int, shares numeric, shares_change numeric, fetched_at)`
  from `symbol-positions-summary` for the latest two quarters; metrics `inst_holders_change_pct`, `inst_shares_change_pct`.
- `insiders (cik, year, quarter, acquired numeric, disposed numeric, purchases int, sales int)` from
  `insider-trading/statistics`; metrics `insider_net_shares_90d` (acquired − disposed of the latest quarter) and
  `insider_buy_sell_ratio`. The skeptic's `reads_most` names them.

### Watchlist and scans

- `watchlist (ticker pk, cik int, added_at, note, last_poll timestamptz)`. `scan` resolves `--watchlist` or `--tickers`,
  orders tickers by the age of their latest snapshot (stalest first, so an interrupted scan resumes where it stopped),
  runs `analyze` (cheap, default) or `analyze_full` (`--full`) three tickers at a time, each with its own DB connection,
  stops when `--max-usd` (default 100) is reached using the cost log, and prints one line per ticker (composite,
  dispersion, report path, or the error).
- Budget arithmetic the defaults assume: the weekly scan is the cheap run (~$1 per ticker); full runs happen on
  earnings events (about four per ticker per year at ~$5-6). A 20-ticker watchlist is therefore roughly $130 a month,
  inside the $500 monthly cap. A weekly `--full` scan of 20 tickers would be ~$450 a month and is Tariq's call.

### Triggers and scheduling

One earnings event produces up to three signals days apart (8-K Item 2.02, the transcript, the 10-Q). They map to one
run per fiscal quarter, executed once the transcript is available or three days after the first signal.

- `run_queue (id serial, ticker, cik, period_key text, reason text, accession text, queued_at, not_before timestamptz,
  started_at, finished_at, status text, error text, unique(cik, period_key))`. `period_key` is `FY{y}` for a 10-K and
  `Q{n}FY{y}` otherwise, derived with the existing fiscal labelling (`periods.fy_of/fq_of` with the company's offset)
  from the filing's report date or the transcript's fiscal year/quarter.
- `poll` (fast, no model calls): for each watchlist ticker, `list_filings` with the cache bypassed and
  `earning-call-transcript-dates`; new 10-K, 10-Q, 8-K with Item 2.02, or transcript entries since the last poll become
  queue rows (`ON CONFLICT (cik, period_key) DO NOTHING`, so the second and third signals of a quarter are no-ops).
  A row queued by an 8-K or 10-Q gets `not_before = now() + 3 days`; a transcript signal sets `not_before = now()` on
  the row (its own insert or the existing one). `last_poll` per ticker is kept on `watchlist`.
- `worker`: takes queued rows with `not_before <= now()` oldest first, one at a time, runs `analyze_full(force=True)`
  (a new filing or transcript means a new snapshot), records status; a `BudgetExceeded` leaves the row queued.
- `schedule install` writes three plists under `~/Library/LaunchAgents/`: `com.fundabot.poll` (StartInterval 600,
  `fundabot poll`), `com.fundabot.worker` (StartInterval 600, `fundabot worker`; launchd never overlaps a job with itself)
  and `com.fundabot.weekly` (StartCalendarInterval Sunday 06:00, `fundabot scan --watchlist`, the cheap run), loads them
  with `launchctl bootstrap gui/$UID`; `schedule uninstall` unloads and removes; `schedule status` lists them.

### Calibration

- `outcomes (snapshot_id, run_kind, horizon_days int, t0 date, t1 date, ticker_return numeric, spy_return numeric,
  excess_return numeric, next_eps_surprise numeric, max_drawdown numeric, computed_at, pk(snapshot_id, run_kind, horizon_days))`.
- `calibrate`: for every verdict (full) and every Phase 2 analysis set (cheap run; composite = unweighted mean) whose
  price date is at least `horizon` days old and has no outcome row, fetch prices for the ticker and SPY from t0 to
  t0 + horizon via yfinance, compute the row; `next_eps_surprise` is the first `surprises` row after t0.
- Statistics printed and stored in `calibration_runs (run_at, horizon, n, stats jsonb)`: Spearman rank correlation of
  `composite_score`, `quality_score`, `value_score`, `earnings_momentum_score` with excess return; the mean excess return
  of the top vs bottom tercile by composite; correlation of `accounting_risk_score` with max drawdown; hit rate of
  `earnings_momentum_score > 60` predicting a positive next surprise; Brier score of agent confidence where the forecast
  probability is `0.5 + (score / 10) × confidence / 2` and the outcome is `excess_return > 0`. With fewer than 10
  scored verdicts the command prints "insufficient history" and the n, never a statistic.

### Review loop

- `review` (no args): the 10 most recent verdicts ranked by `dispersion × (1 − judge_agreement) + contested_claims`,
  each with composite, dispersion, agreement and the top contested claim with both sides.
- `review T --agent bull/openai --vote up|down [--note ...]`: stores in `reviews (snapshot_id, agent, vote, note, reviewed_at)`.
  `review --labels` prints vote counts per persona × provider so prompt edits have evidence.

### Testing

Fixtures: one saved transcript (trimmed), transcript dates, quarterly estimates, institutional summary, insider
statistics, an EDGAR Atom sample. Fake adapter for guidance extraction. Pure tests for the guidance verifier
(quote-in-chunk, chunk-resolves), the new metrics, the queue de-duplication, the 8-K Item 2.02 filter, the launchd
plist content, the calibration statistics on hand-built outcomes (including the "insufficient history" gate), and the
review ranking. One end-to-end test runs `prepare` with transcripts through the existing fake pipeline and checks a
transcript chunk can be cited. The poll and schedule commands are tested without network or launchctl via injected
fetchers and a dry-run flag.

## Decisions left open (Tariq)

None blocking. Defaults: 4 transcripts per ticker; Haiku 5.5 for extraction; scan concurrency 3 and `--max-usd 100`;
weekly cheap scan Sunday 06:00 local; full runs on earnings events with a 3-day transcript wait; calibration horizons
30/90/180; review list size 10. Watchlist size drives cost (see "Watchlist and scans").

## Calls Claude made

- Transcripts enter the existing chunk/section machinery under a synthetic accession `T{fy}Q{fq}` rather than a parallel
  retrieval path, so every downstream piece (evidence ids, grounding, appendix) works unchanged.
- Guidance direction and tone are categorical metrics with quotes attached, never numbers.
- Batch APIs deferred (see Non-goals); universe scale is Tariq's call once scan costs are visible.
- Calibration composite for Phase 2 cheap runs is the unweighted mean the report already shows.
- Triggers are per earnings quarter, not per filing; polling diffs per-ticker submissions rather than the public feed;
  poll and worker are separate launchd jobs (builder review).
- Adding `T{fy}Q{fq}` keys to `snapshot.accessions` changes every snapshot id once; the first run after this phase is a
  fresh paid run for each ticker. `prepare` inserts a `filings` row for each transcript key (form `TRANSCRIPT`) so the
  appendix and the latest-10-K lookup keep working.
- The judge's consensus block admits `llm_extracted` metrics alongside `vendor_fmp`.
- SPY prices for calibration are stored under a reserved `cik = 0` row space keyed by ticker in a small `benchmark_prices`
  table rather than overloading `prices`.
