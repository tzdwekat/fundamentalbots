# Fundamentals Bot — Architecture

Oct 12, 2026 · Tariq Dwekat

## Purpose and design principles

The bot produces a sourced, structured fundamental assessment of any US-listed stock on demand or on a schedule, using three AI providers as deliberately disagreeing analysts rather than one model's opinion.

1. **Numbers come from code, not models.** Every ratio, growth rate and valuation is computed deterministically from filed XBRL data. Models interpret; they never calculate or recall financials.
2. **Every claim cites a source.** An agent's statement must point to a filing section, a transcript line, or a computed metric id. Uncited claims are dropped by the judge.
3. **Bias is a feature, assigned on purpose.** Each agent gets a persona with a stated bias (bull, bear, skeptic, quality, macro) so disagreements surface the real uncertainty instead of three models converging on the same consensus.
4. **Provider-agnostic.** Any persona can run on Claude, OpenAI or Gemini. The prompt contract, not the vendor, defines the role, so you can rotate providers to measure provider bias separately from persona bias.
5. **Reproducible runs.** Each analysis is pinned to a data snapshot (filing accession numbers, price date) and model versions, so a report can be re-run and compared.
6. **Scope is screening and conviction-building, not autonomous trading.** Output is a report and a score; a human (you) makes the trade.

## System overview

Data flows top to bottom: filings and vendor data land in Postgres, code computes every number, five persona agents on three providers argue over the same snapshot, and a judge turns the surviving claims into one scored report.

## Data layer

SEC EDGAR is the backbone because it is free, complete and structured; paid APIs fill in estimates and transcripts. All sources land in Postgres as raw snapshots first, then get normalized.

| Source | What it gives | Cost | Notes |
| --- | --- | --- | --- |
| [EDGAR XBRL companyfacts](https://www.sec.gov/edgar/sec-api-documentation) | Every reported line item per filing, as JSON | Free | Rate limit 10 req/s; set a User-Agent header |
| EDGAR full-text filings (10-K, 10-Q, 8-K) | Risk factors, MD&A, notes, segment data | Free | Parse by Item number; store section text per accession |
| Earnings call transcripts | Management tone, guidance, Q&A | Paid (FMP, Finnhub, or scraped IR pages) | Needed for the earnings agent |
| Consensus estimates | EPS/revenue expectations, revisions | Paid (FMP, Finnhub) | Drives surprise and revision signals |
| Prices | Daily OHLCV, shares outstanding | Free (yfinance) or paid | Only for market cap and multiples; this is not a chart bot |
| Insider and institutional (Forms 4, 13F) | Buying/selling, ownership changes | Free (EDGAR) | Secondary signal for the skeptic agent |
| Macro (FRED) | Rates, CPI, PMI, sector indices | Free | Feeds the macro agent only |

**Ingestion.** One job per source, idempotent, keyed on accession number or date. A new 10-K/10-Q/8-K from the EDGAR RSS feed triggers a full re-analysis for that ticker.

**Storage.**

- `filings` and `filing_sections`: raw text by accession and Item
- `facts`: normalized XBRL facts (tag, period, value, unit, filed date)
- `metrics`: computed values from the analytics layer, versioned by snapshot id
- `embeddings`: chunked filing text with pgvector, so agents retrieve relevant passages instead of reading 200 pages
- `analyses`: every agent output, judge verdict and final score, keyed to the snapshot

**Snapshot.** Before any agent runs, the orchestrator freezes a snapshot: the accession numbers used, the price date, and the estimate pull time. Every output references the snapshot id.

## Deterministic analytics layer

This layer turns XBRL facts into a metrics table in pure Python (pandas), and it is the only place numbers are produced. Agents receive this table as structured input and may quote a metric by its id but never recompute it.

| Group | Metrics | Lookback |
| --- | --- | --- |
| Growth | Revenue, gross profit, operating income, EPS, FCF: YoY and 3y/5y CAGR | 5 years, 8 quarters |
| Profitability | Gross, operating, net, FCF margins; ROIC, ROE, ROA | 5 years |
| Balance sheet | Net debt / EBITDA, interest coverage, current ratio, goodwill share of assets | Latest + trend |
| Cash quality | FCF / net income, accruals ratio, SBC as share of revenue, capex / D&A | 5 years |
| Capital allocation | Buybacks net of dilution, dividend coverage, acquisitions spend | 5 years |
| Valuation | P/E, EV/EBITDA, EV/Sales, FCF yield, PEG; each vs own 5y range and sector median | Current |
| Earnings signals | EPS and revenue surprise, guidance change, estimate revision 30/90 days | Last 4 quarters |
| Intrinsic value | DCF with three scenarios (bear/base/bull) and reverse DCF implied growth | Current |

**Rules.**

- XBRL tags vary by company (e.g. `Revenues` vs `RevenueFromContractWithCustomerExcludingAssessedTax`); keep a tag-priority map per metric and log which tag was used.
- Flag any metric computed from restated or missing data; the flag travels into the agent input.
- Store every metric with its formula id and inputs, so a report can show how a number was derived.
- Sector medians come from running the same pipeline across a peer set (same SIC code, or a hand-maintained peer list).

**Red-flag detector.** A rules engine runs before the agents and emits hard flags: accruals ratio above 10%, FCF persistently below net income, goodwill above 50% of assets, auditor change, going-concern language, late filings. Flags are passed to every agent and weighted by the judge.

## Agent layer

Five persona agents run in parallel over the same snapshot, each run on all three providers (15 agent calls), and each returns the same JSON schema so outputs are comparable. The persona is defined by its system prompt; the provider is a config field, so every persona × provider pair is a first-class agent you can enable or disable.

| Persona | Bias (stated in the prompt) | Runs on | What it reads most |
| --- | --- | --- | --- |
| Bull | Find the strongest case to own this; assume management executes | Claude, OpenAI, Gemini | MD&A, growth metrics, guidance |
| Bear | Find the strongest case to short or avoid; assume the base rate of failure | Claude, OpenAI, Gemini | Risk factors, debt, red flags |
| Forensic skeptic | Distrust reported earnings; hunt for accounting games and incentive misalignment | Claude, OpenAI, Gemini | Notes to financials, cash-quality metrics, Form 4, SBC |
| Quality / long-term | Judge the business as a 10-year owner would: moat, ROIC, reinvestment runway | Claude, OpenAI, Gemini | Segment data, ROIC trend, capital allocation |
| Macro / sector | Judge the company against its cycle and peers, not in isolation | Claude, OpenAI, Gemini | FRED series, peer medians, industry commentary |

**Provider as a dimension.** With all 15 agents running, every verdict carries a 5 × 3 grid of scores. Spread across a row (same persona, three providers) is provider bias; spread down a column (same provider, five personas) is the intended persona bias. Both are logged per run, so you can see which provider is most contrarian, which is most prone to consensus, and which hallucinates most under a given persona. Agents are config-toggled, so a cheap run can use one provider per persona and a full run uses all 15.

**Prompt contract.** Every persona prompt has four fixed parts:

1. Role and bias, stated plainly ("You are paid to find reasons this is a short").
2. Inputs: the metrics table, red flags, and retrieved filing/transcript chunks with citation ids.
3. Hard rules: cite a metric id or chunk id for every claim; never state a number not in the inputs; say "insufficient data" rather than guess.
4. Output schema (strict JSON, enforced with each provider's structured-output mode).

**Output schema (per agent).**

```json
{
  "thesis": "one paragraph",
  "claims": [{"text": "...", "evidence": ["metric:fcf_yield", "chunk:0001193125-26-123456#item1a-14"], "direction": "positive|negative", "weight": 1-5}],
  "key_risks": ["..."],
  "what_would_change_my_mind": ["..."],
  "score": -10..10,
  "confidence": 0..1
}
```

**Retrieval.** Each agent gets a persona-specific retrieval query set (the bear retrieves risk-factor and litigation chunks; the quality agent retrieves segment and competition passages) plus a shared base set. Top-k chunks by cosine similarity, capped at roughly 30k tokens per agent.

**Provider adapters.** One thin interface (`complete(system, user, schema) -> dict`) with three implementations: Anthropic SDK (tool-use for schema), OpenAI SDK (response_format json_schema), Google GenAI SDK (response_schema). Retries, timeouts, token accounting and cost are handled in the adapter.

## Debate and synthesis

After the five personas report, a rebuttal round and a judge turn the disagreement into one scored verdict with its uncertainty preserved.

**Round 2: rebuttal.** Each agent receives the strongest opposing claims pooled from the other 14 agents (deduplicated by a cheap pre-pass so it reads about ten claims, not 14 theses) and must respond to the two strongest opposing claims, either conceding (with reason) or refuting (with a citation). Conceded claims are marked; refuted claims keep both sides. This round is where most hallucinated claims die, because a rival agent on a different provider is checking them.

**Judge.** One final call with no persona bias; the judge's provider rotates per run (or run three judges, one per provider, and report their agreement as a second dispersion number). It does not form its own thesis. It only:

- Verifies every surviving claim's evidence id exists in the snapshot (a code check runs first; the judge handles semantic mismatch).
- Scores each claim on evidence strength and materiality.
- Produces the final verdict.

**Final verdict schema.**

```json
{
  "ticker": "...", "snapshot_id": "...",
  "composite_score": -10..10,
  "dispersion": 0..1,
  "quality_score": 0..100, "value_score": 0..100, "earnings_momentum_score": 0..100, "accounting_risk_score": 0..100,
  "consensus_claims": [...], "contested_claims": [...],
  "red_flags": [...],
  "catalysts": [{"what": "...", "when": "...", "evidence": [...]}],
  "one_paragraph_summary": "..."
}
```

**Scoring rules.**

- `composite_score` is the evidence-weighted mean of agent scores, not a simple average, so a bear with three strongly cited claims outweighs a bull with vibes.
- `dispersion` is the spread of the 15 agent scores; it is reported overall, across personas, and across providers within a persona. High dispersion with a high composite is a "contested but interesting" bucket worth your own reading; low dispersion is a screenable signal.
- `accounting_risk_score` comes mainly from the skeptic and the rules engine, and it can veto: above a threshold, the composite is capped regardless of bull strength.
- The four sub-scores are kept separate so you can screen on your own weighting (for a Qullamaggie-style catalyst filter, `earnings_momentum_score` matters most).

**Report.** The verdict renders to a Markdown/HTML report: summary, score grid, the three most contested claims with both sides, red flags, catalysts, and a sources appendix listing every metric id and filing chunk cited.

## Orchestration, scheduling and cost

A single Python orchestrator runs the pipeline as a DAG per ticker: ingest, compute metrics, run flags, build retrieval context, fan out 15 agents, rebuttal, judge, render. This runs on your own machine for one user, so a plain `asyncio` runner plus cron (or launchd on macOS) is enough; add Prefect only if you want a UI for run history.

**Local setup.** Docker Compose with one Postgres + pgvector container; the bot itself runs as a local Python package. Provider and vendor keys live in a `.env` file that is gitignored. Entry point is `fundabot analyze TICKER` and `fundabot scan`; reports render to a local `reports/` folder you open in a browser. No server, no auth, no hosting.

**Triggers.**

- On demand: `analyze TICKER` from the CLI.
- Event: a new 10-K, 10-Q or 8-K (earnings) for a watchlist ticker, from the EDGAR RSS feed polled every 10 minutes.
- Scheduled: weekly re-score of the full watchlist, monthly re-score of the broader universe (e.g. Russell 1000) for screening.

**Cost controls.**

- Cache retrieval contexts and metric tables per snapshot; a re-run on the same snapshot costs zero model calls.
- Use each provider's cheaper tier for the persona round and the strongest tier only for the judge. A full run is 15 persona calls + 15 rebuttal calls + 1 judge = 31 model calls; at ~40k input tokens each, expect on the order of $3 to $10 per ticker on mid tiers, so use the full 15-agent run for your watchlist and a 5-agent run (one provider per persona) for screening. Measure this in week one and put the real number here.
- Batch APIs (Anthropic and OpenAI both offer ~50% off for async batches) for the monthly universe scan.
- Hard monthly budget cap in the adapter layer; fail closed.

**Secrets and keys.** Three provider keys plus data-vendor keys in environment variables or a secrets manager; never in the repo.

**Observability.** Log every model call with prompt hash, model version, tokens, latency, cost and the snapshot id. Store agent JSON outputs verbatim so you can replay the judge without re-running agents.

## Evaluation and guardrails

The bot is only worth running if you can show its claims are grounded and its scores mean something, so evaluation is built in from v1, not added later.

**Grounding checks (automatic, every run).**

- Every `evidence` id must resolve to a real metric or chunk in the snapshot; unresolved claims are dropped and counted.
- Every number in an agent's text is regex-extracted and matched against the metrics table within a tolerance; mismatches are flagged as hallucinations.
- Track hallucination rate per persona per provider over time. This is also your cleanest provider-comparison metric.

**Calibration (monthly).**

- Store each verdict with its date. After 30/90/180 days, join to forward returns and earnings outcomes.
- Check: does `earnings_momentum_score` predict positive surprises? Does `accounting_risk_score` predict restatements or drawdowns? Does `composite_score` rank-order forward returns at all? Expect weak signal; the point is to know which sub-scores carry it.
- Keep a Brier score on the agents' stated `confidence`.

**Known failure modes and the countermeasure.**

| Failure | Countermeasure |
| --- | --- |
| Agents converge on the same consensus take | Persona bias is explicit; reward disagreement in the rebuttal prompt; measure dispersion |
| Model recalls stale facts from training (old CEO, old guidance) | Inputs-only rule; the grounding check catches unsupported claims |
| XBRL tag mismatch produces a wrong metric | Tag-priority map with logged tag choice; sanity bounds per metric |
| Judge over-trusts the most verbose agent | Judge scores claims, not theses; evidence weight is per claim |
| Look-ahead bias in backtests | Snapshot pins filing dates; never use data filed after the as-of date |

**Human review loop.** A weekly list of the 10 most contested verdicts goes to you for a manual read, with a thumbs up/down on each agent's thesis. Those labels feed prompt revisions, not model fine-tuning.

## Tech stack and repo layout

Python end to end, Postgres with pgvector, and the three official provider SDKs behind one adapter interface. Nothing exotic; the value is in the data model and the prompts.

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12 | SDKs, pandas, your existing stack |
| Database | Postgres 16 + pgvector | One store for facts, text, embeddings and outputs |
| Transforms | pandas for metrics; dbt optional for the metrics marts once stable | Matches how you already work |
| Orchestration | Local: `asyncio` + cron or launchd | DAG per ticker, retries, observability |
| Embeddings | One provider's embedding model (e.g. OpenAI text-embedding-3-large or Voyage) | Keep it to one so similarity is comparable |
| LLM SDKs | `anthropic`, `openai`, `google-genai` | Structured output in all three |
| API/CLI | Typer CLI (FastAPI only if you later want a UI) | On-demand local runs |
| Reports | Jinja2 to Markdown/HTML | Easy to diff and version |
| Config | Pydantic settings + YAML persona files | Personas editable without code changes |

```
fundabot/
  ingest/        edgar_facts.py, edgar_filings.py, transcripts.py, estimates.py, prices.py, fred.py
  analytics/     metrics.py, tag_map.py, valuation.py, red_flags.py
  retrieval/     chunk.py, embed.py, query.py
  agents/        base.py, adapters/{anthropic,openai,gemini}.py, personas/*.yaml, schemas.py
  debate/        rebuttal.py, judge.py, scoring.py
  orchestrate/   pipeline.py, triggers.py, snapshot.py
  report/        templates/, render.py
  eval/          grounding.py, calibration.py
  db/            models.py, migrations/
  cli.py
```

## Build phases

Four phases, each shippable on its own; the data and metrics layers come first because every later phase is only as good as they are.

1. **Data and metrics (weeks 1–3).** EDGAR companyfacts and filings ingestion, tag map, metrics table, red-flag rules, snapshot. Exit: `fundabot metrics AAPL` prints a correct, sourced metrics table for 20 hand-checked tickers.
2. **Single-agent analysis (weeks 4–5).** Chunking, embeddings, retrieval, one persona on one provider, grounding check, report render. Exit: a report where every claim resolves to an evidence id.
3. **Multi-provider debate (weeks 6–8).** Three adapters × five personas (15 agents), rebuttal round, judge, scoring, the 5 × 3 score grid. Exit: dispersion and hallucination rate tracked per persona and provider.
4. **Scale and evaluate (weeks 9+).** EDGAR RSS triggers, batch universe scans, calibration job, weekly human-review list. Exit: first 90-day calibration read.

**Decisions still open.**

- [ ] Transcript and estimates vendor (FMP vs Finnhub vs scraping IR pages)
- [ ] Universe for the monthly scan (watchlist only, Russell 1000, or everything with XBRL)
- [ ] Whether the earnings-momentum sub-score should feed the swing-trading catalyst filter directly
