# Fundabot Phases 1 and 2: data, metrics, single-provider analysis

2026-10-15. Design for the first shippable build of the Fundamentals Bot described in
[docs/architecture.md](../../architecture.md) (Oct 12, 2026). This document covers Phase 1 (data and
metrics) and Phase 2 (single-provider agent analysis with grounding and a rendered report).
Phases 3 and 4 (multi-provider debate, judge, triggers, calibration) are out of scope here
but nothing in this design should block them.

## Intent

Tariq wants to type a ticker, or several, and get an accurate, sourced fundamental read of
each. The number one requirement is that nothing in the output gives a wrong impression of
a stock. A missing number is acceptable; a wrong number, an unsupported claim, or a stale
fact presented as current is not.

Usage picture: on-demand from the terminal, `fundabot analyze NVDA AMD`, report opens in a
browser a few minutes later. Scheduled and event-triggered runs come in Phase 4.

## Goals

1. `fundabot metrics TICKER` prints a metrics table where every value traces to the XBRL
   tag, filing accession and formula that produced it.
2. `fundabot analyze TICKER [TICKER ...]` runs the persona agents on one provider over a
   frozen snapshot and writes a Markdown + HTML report to `reports/`.
3. Every claim in a report cites an evidence id that resolves to a metric or a filing
   chunk in the snapshot. Claims that do not are dropped and counted.
4. Every number an agent writes is checked against the metrics table. Mismatches are
   flagged in the report as hallucinations.
5. A run on an existing snapshot makes zero model calls.

## Non-goals (this build)

- Rebuttal round, judge, composite score, dispersion. Phase 3.
- OpenAI and Gemini adapters. The adapter interface exists; only Anthropic is implemented.
- Earnings call transcripts, consensus estimates, earnings-surprise metrics, Forms 4/13F,
  FRED macro data, peer/sector medians. Each feeds a Phase 3 persona or score.
- Auditor-change red flag (needs 8-K Item 4.01 parsing). Deferred.
- EDGAR RSS polling, cron, batch APIs, calibration. Phase 4.
- Any UI or server. CLI only.

## Anti-goals

- An agent stating a number that is not in its inputs and the report passing it through.
- A metric computed from the wrong XBRL tag (e.g. YTD nine-month revenue shown as a
  quarter) and shown without a flag.
- Training-data recall (old CEO, old guidance) presented as a cited fact.
- Silent failure. If a data source is missing, the metric is `null` with a reason, never
  a guess or a zero.

## Constraints

- Runs on Tariq's Mac. Python 3.13 (installed), `uv` for the environment, Docker Compose
  for Postgres 16 + pgvector. Docker Desktop must be running.
- Secrets in a gitignored `.env`. Phase 1 needs no keys. Phase 2 needs `ANTHROPIC_API_KEY`
  and an embeddings key (see "Decisions left open").
- SEC EDGAR fair-use rules: a descriptive `User-Agent` with contact email, at most 10
  requests per second. We will stay well under it.
- Hard monthly spend cap enforced in the adapter before each call. Fails closed.

## Architecture as built in this phase

The eight layers of the architecture doc, trimmed to what Phases 1 and 2 need:

```
CLI (Typer)
  └─ orchestrate/pipeline.py      per ticker: ingest → snapshot → metrics → flags →
                                    chunk+embed → retrieve → agents → grounding → render
       ├─ ingest/    edgar_facts.py  edgar_filings.py  prices.py
       ├─ analytics/ tag_map.yaml  metrics.py  valuation.py  red_flags.py
       ├─ retrieval/ chunk.py  embed.py  query.py
       ├─ agents/    base.py  adapters/anthropic.py  personas/*.yaml  schemas.py
       ├─ eval/      grounding.py
       ├─ report/    render.py  templates/report.md.j2
       └─ db/        schema/*.sql  session.py
```

### Data layer

**Ticker to CIK.** `https://www.sec.gov/files/company_tickers.json`, cached locally for
a day. Unknown ticker is a hard error.

**Facts.** `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`. Stored raw in
`raw_companyfacts` (one JSON per CIK per fetch) and normalized into `facts`:
`(cik, taxonomy, tag, unit, start, end, value, fy, fp, form, accession, filed)`.
Idempotent on `(cik, tag, unit, start, end, accession)`.

**Filings.** `https://data.sec.gov/submissions/CIK##########.json` lists filings. We keep
the last 5 annual 10-Ks and last 8 10-Qs (plus 10-K/A, 10-Q/A). For each, download the
primary document, extract plain text per Item using the `edgartools` library
(`Filing.obj()` gives Item-level text for 10-K and 10-Q). Stored in `filings`
(accession, form, filed, period, primary doc URL) and `filing_sections`
(accession, item, text). Items kept: 10-K 1, 1A, 1C, 3, 7, 7A, 8, 9A; 10-Q Part I 2, 3, 4
and Part II 1, 1A. If Item splitting fails for a document, the whole text is stored as
item `full` and flagged; the pipeline continues.

**Prices.** `yfinance` daily close for the last 5 years plus the latest close. Only used
for market cap and valuation multiples. Shares outstanding come from XBRL
(`dei:EntityCommonStockSharesOutstanding`), not yfinance, so the denominator is filed data.
yfinance is a scraper and breaks occasionally; a price fetch failure makes the valuation
group `null` with reason `price_unavailable`, and the run continues.

**Prices table.** `prices (cik, date, close)` for the 5-year daily history, so the
valuation range is reproducible from the database.

**Snapshot.** Before metrics, `snapshots` row: id, ticker, cik, companyfacts fetch id,
accession list, price date, latest close, code git sha, created_at. The id is
sha256 of (cik, companyfacts fetch id, sorted accessions, price date), so a second run
on the same data the same day resolves to the same snapshot. Scope of a snapshot and
what is reused:

- Metrics and red flags are recomputed on every run and upserted under the snapshot id.
  They are cheap, and this means a metrics bug fix shows up without a new snapshot.
- Analyses are keyed on (snapshot id, persona, provider, model). `analyze` only calls the
  model for combinations that have no stored analysis. Changing `--model` or adding a
  persona triggers only the missing calls.
- `--force` re-fetches all data (new companyfacts fetch id, new price date), which yields
  a new snapshot id. `--rerun` discards stored analyses for the snapshot and calls the
  models again. Both are explicit; the default never spends.
- A cached analysis cites metric ids, not values, so if a metrics fix changes a value the
  cached thesis may describe the old number. The report shows the grounding check against
  the current metrics, so such a mismatch surfaces as a flagged number. Use `--rerun`.

### Deterministic analytics layer

**Period selection from companyfacts.** This is the part most likely to produce a wrong
number, so the rules are explicit:

1. A fact's duration is `end - start`. Annual: 350 to 380 days. Quarterly: 80 to 100
   days. Instant (balance sheet) facts have no start. Everything else (6-month and
   9-month YTD) is ignored for the series.
2. Each 10-K restates prior years. For a given `(tag, start, end)` keep the value from the
   latest filing. If it differs from the earliest filed value by more than 0.5%, mark the
   period `restated=true`; the flag travels into the metric.
3. Q4 is not filed as a quarter for income-statement items. Derive Q4 = FY minus
   (Q1 + Q2 + Q3) of the same fiscal year, mark `derived=true`. If any of the three
   quarters is missing, Q4 is `null`.
4. Fiscal years are taken from the filing's `fy`/`fp` fields but verified against the
   period end date; mismatches (common for off-calendar fiscal years) resolve by end date.

**Tag map.** `tag_map.yaml` maps each base line item to an ordered list of us-gaap tags.
First tag with data wins; the chosen tag is stored with the metric. Example:

```yaml
revenue: [RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, SalesRevenueNet]
```

Base line items in this build: revenue, cost of revenue, gross profit, operating income,
net income, EPS diluted, operating cash flow, capex, D&A, SBC, interest expense, income
tax, total assets, total liabilities, equity, cash, total debt (short + long), goodwill,
intangibles, current assets, current liabilities, shares outstanding, dividends paid,
buybacks, shares issued (for dilution), acquisitions.

**Metrics table.** Every row: `(snapshot_id, metric_id, period, value, unit, formula_id,
inputs (json of metric/tag ids and values), flags (json), tag_used)`. Groups computed:

| Group | Metrics |
|---|---|
| Growth | revenue, gross profit, operating income, EPS, FCF: YoY per year, 3y and 5y CAGR, last 8 quarters YoY |
| Profitability | gross, operating, net, FCF margin; ROIC, ROE, ROA; 5 years |
| Balance sheet | net debt/EBITDA, interest coverage, current ratio, goodwill/assets; latest and 5y |
| Cash quality | FCF/net income, accruals ratio ((NI − OCF)/avg assets), SBC/revenue, capex/D&A; 5 years |
| Capital allocation | net buybacks (buybacks − issuance), dividend coverage (FCF/dividends), acquisitions/OCF; 5 years |
| Valuation | P/E, EV/EBITDA, EV/Sales, FCF yield, PEG (3y EPS CAGR); current and each vs own 5y range (min, median, max, percentile of current) |
| Intrinsic value | DCF bear/base/bull and reverse-DCF implied growth |

**Valuation history.** The 5-year range is sampled once per filing: at each 10-Q and
10-K filing date in the window, trailing-twelve-month metrics are built from the
quarterly series (last four quarters including derived Q4), the price is the close on
the filing date (next trading day if the date is not one), and shares are the cover-page
`dei` count from that filing. Multiple share classes with the same cover date are summed.
That gives about 20 samples per ratio, which is enough for min, median, max and the
percentile of the current value. Current ratios use the latest close and the latest
cover-page share count.

**Filer support.** This build supports US domestic filers of 10-K/10-Q. A ticker whose
latest annual filing is a 20-F or 40-F is a hard error ("foreign private issuer: not
supported"). Companies without a line item (banks have no cost of revenue; many have no
interest expense tag) get `null` with reason `tag_missing` for that item and every metric
that depends on it, never zero.

Sector medians and earnings signals are not computed in this build.

**DCF assumptions** are fixed, documented in `valuation.py`, and printed in the report:
10% discount rate, 2.5% terminal growth, 10-year horizon; base growth = trailing 5y FCF
CAGR clamped to [0%, 25%]; bear = base − 5 points, bull = base + 5 points. Reverse DCF
solves for the 10-year growth that equates DCF to current EV. These are a starting point
Tariq can tune; the report labels them as assumptions.

**Sanity bounds.** Each metric has a plausible range (margins in [−200%, 100%], ratios
non-negative where applicable, etc.). Out-of-range values are kept but flagged
`out_of_bounds`; the flag reaches the agent input and the report.

**Cross-check.** `fundabot verify TICKER` compares revenue, net income, total assets and
operating cash flow for the last 4 fiscal years against yfinance's financial statements
(an independent source). Differences over 2% are printed. This is a diagnostic for the
20-ticker hand-check, not part of the analysis run.

**Red flags** (`red_flags.py`, rules only, no model):

- accruals ratio above 10% in the latest fiscal year
- FCF below net income in 3 or more of the last 5 fiscal years
- goodwill above 50% of total assets
- going-concern language in the latest 10-K (regex on "substantial doubt" near
  "going concern" in Items 7, 8, 9A)
- late filing: any NT 10-K or NT 10-Q in the last 2 years in the submissions list

Each flag: `(snapshot_id, flag_id, severity, evidence ids, detail)`.

### Retrieval

Chunk each filing section into ~800-token pieces with 100-token overlap, id
`{accession}#{item}-{n}` (e.g. `0000320193-25-000073#item1a-14`). Embed with one
model (decision below), store in `chunks` with a pgvector column. No ANN index; brute-force
cosine over a few thousand chunks per ticker is instant at this scale.

Each persona YAML lists its retrieval queries. A shared base set (business overview,
latest results, guidance, liquidity) plus persona-specific queries (bear: risk factors,
litigation, debt covenants). Top-k by cosine per query, deduplicated, capped at 30k tokens
per agent. The retrieved chunk ids are stored with the analysis so the context is
reproducible.

### Agent layer

**Adapter interface.** `complete(system, user, schema) -> dict` plus token counts, latency
and cost. One implementation: Anthropic, using tool-use with the JSON schema as the single
tool's input schema to force structured output. Retries with backoff on 429/5xx, 120s
timeout. Every call logged to `model_calls` (snapshot id, persona, provider, model, prompt
sha256, input/output tokens, latency ms, cost usd, created_at). Before each call the
adapter sums this month's cost and refuses if it would exceed `FUNDABOT_MONTHLY_BUDGET_USD`.

**Personas.** Five YAML files (bull, bear, skeptic, quality, macro), each with role and
bias text, retrieval queries, and the shared hard rules. All five run on the one provider
by default; `--personas bull,bear` narrows. The macro persona runs without FRED or peer
data in this build and is told so in its inputs; it will lean on industry commentary in
the filings. This is Claude's call; see "Decisions left open" if you would rather drop it
until Phase 3.

**Prompt contract.** Four fixed parts: role and bias; inputs (metrics table as compact
JSON with metric ids, red flags, chunks with ids); hard rules (cite an id per claim, no
numbers not in the inputs, say "insufficient data" rather than guess); the output schema.

**Output schema** (the contract Phase 3's rebuttal and judge consume; do not change
field names):

```json
{
  "thesis": "one paragraph",
  "claims": [
    {"text": "...",
     "evidence": ["metric:fcf_yield", "chunk:0000320193-25-000073#item1a-14"],
     "direction": "positive|negative",
     "weight": 1}
  ],
  "key_risks": ["..."],
  "what_would_change_my_mind": ["..."],
  "score": 0,
  "confidence": 0.0
}
```

`weight` is an integer 1 to 5, `score` an integer −10 to 10, `confidence` a float 0 to 1.
Evidence ids are `metric:{metric_id}` (optionally `metric:{metric_id}@{period}`) or
`chunk:{accession}#{item}-{n}`. Each metric row in the table given to the agent carries
its id in exactly this form.

**Models.** Default persona model `claude-sonnet-5-5` (cheaper tier, per the cost rule in
the architecture doc). Configurable per run.

### Grounding (`eval/grounding.py`)

Runs on every agent output before rendering:

1. Every `evidence` id must exist in the snapshot's metrics or chunks. A claim with any
   unresolved id is dropped; the count is stored on the analysis and shown in the report.
2. Numbers in `thesis`, claim text, risks: regex-extracted with unit handling (`$94.5B`,
   `23.4%`, `1.2x`, `391,035 million`) and matched against any metric value in the
   snapshot within 1% relative tolerance (after unit normalization, and allowing a
   percentage to match a ratio). Unmatched numbers are listed as suspected hallucinations
   on the claim and in the report. The claim is kept but marked, since the number may be a
   legitimate figure quoted from a filing chunk; the reader sees the flag.
3. Hallucination rate (unmatched numbers / total numbers) and dropped-claim rate are
   stored per analysis, per persona.

### Report

Jinja2 to Markdown, then to HTML via `markdown`. Sections: header (ticker, snapshot id,
price date, filings used), red flags, metrics table (grouped, with flags), one section per
persona (thesis, claims with evidence links, risks, what would change my mind, score,
confidence, grounding stats), DCF assumptions, sources appendix (every metric id with
formula and tag; every chunk id with accession, item and first line). Written to
`reports/{TICKER}/{snapshot_id}.md` and `.html`. `--open` opens the HTML in the browser.

No composite score in this build. The report shows the per-persona scores side by side
and a plain unweighted mean labelled "unweighted mean, no judge (Phase 3)" so it is not
mistaken for the architecture's evidence-weighted composite.

### Storage

Postgres 16 + pgvector in Docker Compose (`docker-compose.yml`, one service, a named
volume). Schema as numbered SQL files under `db/schema/` applied in order by
`fundabot init-db`, tracked in `schema_migrations`. psycopg 3 with plain SQL, no ORM. Tables: `raw_companyfacts`, `facts`, `filings`,
`filing_sections`, `chunks`, `snapshots`, `metrics`, `red_flags`, `analyses`,
`model_calls`.

### CLI

```
fundabot init-db
fundabot ingest TICKER [TICKER ...]          # facts, filings, prices
fundabot metrics TICKER [--json]             # prints the table, runs ingest if needed
fundabot verify TICKER                       # cross-check vs yfinance statements
fundabot analyze TICKER [TICKER ...] [--personas ...] [--model ...] [--open] [--force]
```

`analyze` runs tickers sequentially, each as an async pipeline with the five persona
calls in parallel. `--force` creates a new snapshot even if today's exists.

### Testing

`pytest`. Fixtures are trimmed real companyfacts JSON (only tags in the tag map) for
three tickers with different shapes: AAPL (September fiscal year, clean tags), MSFT (June
fiscal year), and one company that uses the legacy `Revenues` tag. Tests assert known
published figures (e.g. AAPL FY2024 revenue 391,035M) and the period rules: Q4 derivation,
YTD exclusion, restatement flag, off-calendar fiscal year mapping. Grounding tests cover
each number format and the unresolved-id drop. Adapter tests use a fake adapter; no
network in tests. One end-to-end test runs the pipeline with the fake adapter against
fixtures and renders a report.

## Build order

1. Repo skeleton, `pyproject.toml`, Docker Compose, schema, `init-db`.
2. EDGAR facts ingest and `facts` normalization with fixtures and tests.
3. Period selection, tag map, metrics, valuation, red flags, `metrics` and `verify` CLI.
   Phase 1 exit: hand-check 20 tickers with `verify` and the printed table.
4. Filings ingest and Item extraction, chunking, embeddings, retrieval.
5. Anthropic adapter, personas, schema enforcement, model_calls log and budget cap.
6. Grounding, report render, `analyze` CLI. Phase 2 exit: a report where every surviving
   claim resolves to an evidence id, with grounding stats shown.

## Decisions left open (Tariq)

- **Embeddings provider.** OpenAI `text-embedding-3-small` (1536 dims, needs
  `OPENAI_API_KEY`) or Voyage `voyage-3` (needs `VOYAGE_API_KEY`). Pick whichever key you
  have; the code takes the choice from config. Claude's default if unanswered: OpenAI.
- **Macro persona in this build.** Run it without FRED and peer data (Claude's default),
  or leave it out until Phase 3.
- **Hand-check tickers.** Which 20 tickers go on the Phase 1 hand-check list. Test
  fixtures will match them. If any are banks, insurers, multi-class filers or foreign
  issuers, say so; see "Filer support".

## Calls Claude made (check these while reading)

- `edgartools` as the one non-trivial dependency, for 10-K/10-Q Item extraction. Writing an
  HTML item splitter that works across filers is weeks of work; this library is the
  standard one. Facts still come from raw companyfacts JSON so the tag map stays ours.
- Shares outstanding from XBRL `dei` facts, not yfinance.
- Fixed DCF assumptions as listed; tunable later.
- All five personas on Anthropic in Phase 2 rather than one, because the extra cost is
  four YAML files and it makes `analyze` useful before the judge exists.
- Hallucinated numbers mark the claim rather than drop it; unresolved evidence ids drop it.
- Plain SQL migrations instead of Alembic.
- Snapshot reuse rules, valuation sampling per filing date, and filer support as written
  above (settled after the builder review).
