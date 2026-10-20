# fundabot

Sourced, deterministic fundamental analysis of US-listed stocks with persona agents.
See `docs/architecture.md` and `docs/superpowers/specs/` for the design.

## Setup

```
docker compose up -d        # Postgres 16 + pgvector on localhost:5433
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot   # see note below
uv sync
cp .env.example .env        # fill in keys
uv run fundabot init-db
```

**Why the venv lives outside the repo:** on this machine macOS marks every file under `./.venv` as
hidden within seconds, and Python 3.12 skips hidden `.pth` files, which silently breaks the editable
install (`ModuleNotFoundError: fundabot`). Keeping the environment under `~/.venvs` avoids it. Put the
`export` in your shell profile.

## Commands

```
uv run fundabot init-db                      # create/upgrade the schema
uv run fundabot ingest AAPL MSFT             # facts, prices, filing text; builds a snapshot with metrics and red flags
uv run fundabot metrics AAPL [--json]        # sourced metrics table (every value has a tag, filing and formula)
uv run fundabot verify AAPL                  # cross-check 4 statement items against yfinance (diagnostic)
uv run fundabot analyze NVDA AMD [--open]    # five persona agents on Claude -> reports/NVDA/<snapshot>.html
    --personas bull,bear  --model claude-sonnet-5-5  --force (refetch data)  --rerun (ignore cached analyses)
    --providers anthropic,openai,gemini          # the same personas on more providers (one report, 10 or 15 agents)
uv run fundabot analyze NVDA --full          # Phase 3: 5 personas x 3 providers, rebuttal round, 3 judges + reconcile,
                                             #   scored verdict -> reports/NVDA/<snapshot>-full.html   (--judges 1 for one judge)
uv run fundabot grid NVDA                    # persona x provider score grid with row/column spreads and hallucination rates
uv run fundabot cost NVDA                    # per-round tokens (input / cached / cache-write / output) and USD for the latest snapshot
uv run fundabot judge-ab NVDA                # re-judge the stored snapshot with the screen/escalation panels and compare with its verdict
    --profile anthropic|openai|gemini|multi  # analyze/scan/worker/judge-ab: which provider(s) fill every role (fundabot/profiles.yaml)
    --tier screen                            # analyze/scan/worker: personas on Haiku 5.5 / gpt-5.4-mini / Gemini Flash; judges unchanged
```

Provider defaults: personas on `claude-sonnet-5-5`, `gpt-5.4`, `gemini-3.1-pro-preview`; judges on `claude-opus-5-5`, `gpt-5.4`,
`gemini-3.1-pro-preview`. The Gemini key needs billing enabled (a free-tier key only serves `gemini-3.8-flash`, slowly).
Every round is cached per snapshot: a repeated `--full` run on the same day costs $0; `--rerun` discards all rounds.

Measured on 2026-10-18 for AAPL `--full` (15 agents, 3 judges, Gemini Pro): $3.54 and 5 minutes 18 seconds. By round:
personas $1.06 (15 calls), rebuttals $0.72 (15 calls), judge round 1 $1.08, judge round 2 $0.67. Before the token work
(2026-10-16, NVDA, same setup) a full run was $5.59; the judges are now the biggest line and the next thing to trim.

Keys: Phase 1 commands need none. `analyze` needs `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (embeddings).
A user-scoped Anthropic key (`sk-ant-usr-...`) also needs `ANTHROPIC_WORKSPACE_ID` (Console -> Settings -> Workspaces).

Costs: every model call is logged in `model_calls` with tokens, cached tokens and USD; `FUNDABOT_MONTHLY_BUDGET_USD` is a
hard cap (the adapter refuses calls once the month's logged spend reaches it). Measured on 2026-10-18 for AAPL, five personas
on `claude-sonnet-5-5`: $0.43 per ticker and 1-1.5 minutes (2026-10-15, before the token work: $1.00 and 2 minutes).
Embeddings for a ticker's 13 filings: about $0.006 once. A re-run on the same snapshot costs $0. `fundabot cost T` sums
every run logged against the latest snapshot, so after `--rerun` it includes the discarded rounds too.

How the judges work (judge triage, 2026-10-19): a screen panel of one cheap judge per provider (`claude-haiku-5-5`,
`gpt-5.4-mini`, `gemini-3.8-flash`) scores every surviving claim with its evidence. A claim escalates to the strong panel
(`claude-opus-5-5`, `gpt-5.4`, `gemini-3.1-pro-preview`) when the screen judges' strength spread is 2 or more, any screen
judge rated it materiality 4 or more, it was refuted, or fewer than two screen judges scored it. The strong panel sees the
escalated claims with their evidence, the screen panel's scores on them and a one-line list of the unanimous claims, and
supplies the sub-scores, catalysts and summary. Every claim in the verdict says which panel judged it; the rule and the
merge are code, not model output. `fundabot judge-ab T` re-judges a stored snapshot this way and prints the before/after
(composite, sub-scores, escalation count, reclassified claims, pass-through deltas) with a ship/widen line.

Measured on 2026-10-19 against the stored verdicts: composites unchanged on AAPL (1.9), NVDA (3.2) and HALO (1.9; its
strong panel ran on OpenAI and Gemini only because the Anthropic account was out of credits);
79%, 69% and 66% of claims escalated (cheap judges call most claims material); judge cost $1.17 / $1.28 per run
against $1.75 before (screen panel $0.17-0.20, strong panel $1.0). Tighter rules were simulated on the same screen
outputs: requiring two screen judges at materiality 4 escalates 50-62% but lets one strong-panel dispute per ticker pass
through on HALO; a mean of 4 escalates 36-40% but passes 10-12 claims the previous strong panel had rated material. The
rule stays "any screen judge at 4" (`judge.ESCALATE_MATERIALITY`); the larger saving comes from running one provider
(see profiles below).

Provider profiles (2026-10-19): the default profile is `anthropic` (`FUNDABOT_PROFILE` in `.env`; the most reliable on
the stored runs: 0.6% of its numbers fail the grounding check against 6.9% for OpenAI and 6.6% for Gemini, no failed
cells, and judges rate its claims highest). Set `FUNDABOT_PROFILE=openai|gemini|multi` to change it everywhere, or pass
`--profile` / pick it on a page for one run. `fundabot/profiles.yaml` maps each role (personas and their rebuttals, screen judge
panel, escalation judge panel, guidance extraction) to provider and model; `--profile` picks one at run time and the
pipeline is otherwise identical. `multi` is the three-provider layout above. `--providers` defaults to the profile's providers
(all of them on `--full`, the first one on a cheap run). `anthropic`, `openai` and `gemini` run the
whole debate on one provider: five persona cells, rebuttals on the same models, one screen judge and one escalation
judge. Add a profile by adding a key to the file; every role is required. Embeddings stay on OpenAI. Measured on 2026-10-19:
`analyze MSFT --full --profile openai` cost $0.78 and took 2 minutes 18 seconds (personas $0.36, rebuttals $0.25, screen
judge $0.04, escalation judge $0.10, guidance $0.03; 30 of 46 claims escalated); `analyze GOOGL --full --profile anthropic`
cost $0.92 (personas $0.46 with 177k cached tokens, rebuttals $0.27, judges $0.20) in about 4 minutes; the same runs on
`multi` are about $3.00. A single-provider verdict loses the cross-provider dispersion and judge-agreement signals, so the grid shows one
column and judge agreement is always 1.0.

How the persona prompt got cheaper (Phase 5a): the metrics table is pruned to what the report shows (about 13k tokens instead
of 25k, raw XBRL items at fiscal-year columns only); boilerplate excerpts that repeat across filings are kept once (newest
filing wins); retrieval is split into a shared set from the base queries (12k tokens, identical for every persona) and a
per-persona set (8k); and the shared part of the prompt (snapshot, metrics, red flags, shared excerpts, hard rules) is one
cacheable prefix. The first call per provider runs alone to warm the cache, then the rest fan out: Anthropic caches it with
`cache_control` (reads 0.1x, writes 1.25x), OpenAI caches the identical prefix automatically (0.1x), and Gemini gets one
explicit cache per grid (its implicit cache misses whenever the user turn differs; creation is billed once at the normal
rate and logged as cache-write tokens on the first call). On the AAPL run above each follow-up persona call read 33-40k
tokens from cache. `--tier screen` puts the personas on Haiku 5.5 / gpt-5.4-mini / Gemini Flash for a cheap first pass
while the judges keep the strong models.

## Web app

`uv run fundabot web` serves http://127.0.0.1:8787 (no login; localhost only). Pages:

- `/` overview: every watchlist ticker with close, 1/3/6-month returns, the outlook label, the latest composite and date,
  the worker queue status, and Analyze cheap / full buttons that show the measured estimate for the selected profile.
- `/t/AAPL` the stock picture: price chart for 30 / 90 / 180 days with the 50- and 200-day averages, markers for
  filings (every EDGAR form, 8-Ks included), earnings, calls, splits, analyst upgrades/downgrades and the news near the
  ten largest daily moves; the outlook block (momentum, valuation, trend and the rule that produced the label, all
  computed in code); the decision panel from the latest verdict (composite, bull and bear columns with every claim's
  evidence as a link that opens the metric cell, excerpt or article in a drawer, red flags, catalysts, judge summary,
  link to the full report); next earnings date; ownership and estimate-revision lines; news and press releases for
  7 / 30 / 90 days. "What moved it" is a six-sentence narrative written by the profile's cheap model from the stats,
  moves, news and the window's filing excerpts, generated only when you press *Explain moves* (about $0.01 measured;
  the button says so) and stored until the inputs change; every sentence cites ids you can open and every number is
  checked against the inputs, failing sentences are dropped and counted.
- `/review`, `/queue` (worker queue, web jobs, month-to-date spend against the budget, spend by day), `/calibration`.

Analyze and Refresh buttons start background jobs in the server process (one per ticker, three at a time); a job is
refused when the launchd worker already holds the ticker. Reports are served under `/reports/`. The page never runs
`prepare()` or a model on its own; it refreshes prices, news, grades and the S&P series from stored tables at most
every six hours.

FMP data in use (Phase 5c, 2026-10-19), each stored with `fetched_at` and refreshed on its own rule, a failed refresh keeping
the stored rows: analyst estimates, earnings (surprises and the next report date), price targets, transcripts, 13F
summaries, insider statistics, stock news and press releases (6 h), analyst grades (6 h), company profile (24 h; sector,
industry, employees, beta; one line in the personas' snapshot block), peers with trailing multiples (24 h; US listings
only; the peer-median P/E and EV/sales and our ratio to them are metrics, and the outlook falls back to them when the
five-year percentile is missing), revenue segments by product and geography (24 h; `seg_<name>_revenue/_share/_yoy`
metrics at fiscal years, at most 8 and 6 segments), dividends (24 h; TTM per share, yield, 5-year growth), daily volume
joined to the stored closes (relative volume on the largest moves), insider transactions (6 h) and top 13F holders
(24 h), Altman Z and Piotroski (24 h; `altman_z_distress` red flag under 1.8 except for financials and REITs), analyst
rating counts, the 10-year Treasury yield, and one batch quote per overview load (15 min). `fundabot verify T` prints a
second table comparing our TTM ratios with FMP's (10 % tolerance; a mismatch means look, never that ours is overwritten);
on AAPL all ten agree within 4 %. The ticker page shows all of it under Peers, Revenue segments, Dividend, Ownership,
Scores and ratings, and Cross-check.

Bulk and batch endpoints: they reduce the number of API calls, not the bill. FMP is a flat subscription and the
pipeline makes about fifteen calls per ticker, far below the plan's rate limit; the money goes to model tokens, which
these endpoints do not touch. The one batch call that earns its place is the overview's batch quote (one call for the
whole watchlist instead of one per row). Not in use: FMP's own DCF and recommendations (we compute our own), senate and
house trades, ESG, crowdfunding, commodities, forex, crypto, intraday bars.

## What the report contains

Charts (inline SVG, no dependencies, light and dark mode): revenue and free cash flow, margins and ROIC, quarterly
revenue growth, and each valuation ratio marked on its own 5-year range. The full-run verdict adds the persona × provider
score grid as a diverging heatmap. Every value also appears in the metrics table, which is the accessible table view.

Red flags from the rules engine; a score strip (per persona, plus an unweighted mean clearly labelled as such: the
evidence-weighted composite, rebuttal round and judge are Phase 3); each persona's thesis and claims with evidence ids,
claims dropped for unresolved evidence, numbers flagged when they match neither a metric nor a retrieved filing
passage; the metrics table; DCF assumptions; and a sources appendix for every cited metric and chunk.

## Accuracy notes

See `docs/handcheck-2026-10.md` for the 20-ticker check and `docs/superpowers/specs/` for the period and tag rules.
Metrics are never guessed: a missing line item is `null` with a reason. Derived values (Q4 = FY - Q1..Q3, Q2/Q3 from
year-to-date cash-flow statements, composed D&A, reported-total debt, diluted-share fallback) carry flags that travel
into the agent inputs and the report.

## Running it on a watchlist (Phase 4)

```
uv run fundabot watch add AAPL NVDA          # watchlist in the database (watch list / watch remove)
uv run fundabot poll                         # diff each ticker's EDGAR filings and FMP transcript dates into the run queue
uv run fundabot worker                       # run one queued full analysis whose wait has elapsed
uv run fundabot scan --watchlist [--full] [--max-usd 100]   # stalest first, 3 at a time, stops at the per-scan budget
uv run fundabot schedule install|status|uninstall           # launchd: poll + worker every 10 min, cheap scan Sunday 06:00
uv run fundabot calibrate [--horizon 30,90,180]             # verdicts vs forward excess returns; needs 10 scored verdicts
uv run fundabot review                       # 10 most contested verdicts; review NVDA --agent bear/openai --vote up --note ...
uv run fundabot review --labels              # thumbs up/down counts per persona x provider, for prompt edits
```

**Trigger model.** One earnings quarter produces up to three signals days apart: the 8-K (Item 2.02), the call transcript on
FMP, and the 10-Q. They collapse onto one queued full run per quarter (plus one per 10-K). A filing signal waits up to 3 days
for the transcript; a transcript signal runs at once. The poller reads each watchlist ticker's own submissions and transcript
dates (not the public 100-entry EDGAR feed), so nothing is missed on busy days, and the worker is a separate job so polling
never blocks behind a 5-minute run.

**Transcripts and guidance.** The last 4 earnings calls are stored as retrievable sections (`chunk:T2026Q3#call-12`) and a
Haiku extraction records guidance statements and management tone as verbatim quotes, each verified against its chunk. The
metrics table gets `guidance_direction` and `management_tone` (-1/0/+1, flagged `llm_extracted`); numbers from models never
enter the table. Quarterly consensus, 13F ownership change and insider net activity are also in (flagged `vendor_fmp`).

**Cost arithmetic.** Weekly scan is the cheap run (~$0.45 per ticker); full runs happen on earnings events (~4 per ticker per
year at ~$3.50). A 20-ticker watchlist is roughly $60 a month, inside the $500 monthly cap in `.env`. A weekly `--full` scan
of 20 tickers would be ~$300 a month.

**Calibration caveat.** It needs at least 10 verdicts older than the horizon before it prints any statistic; before then it
says "insufficient history". The first 30-day read is possible about a month after the watchlist starts running.
