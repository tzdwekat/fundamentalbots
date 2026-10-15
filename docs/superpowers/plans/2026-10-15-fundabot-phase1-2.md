# Fundabot Phases 1 and 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A CLI that ingests SEC XBRL facts and filings for a ticker, computes a sourced metrics table and red flags deterministically, runs five persona agents on Claude over a frozen snapshot, grounds every claim, and renders a report.

**Architecture:** Postgres (pgvector) holds raw facts, filing text, chunks, snapshots, metrics and agent outputs. Pure functions turn companyfacts JSON into period series and metrics (pandas). An asyncio pipeline per ticker fans out persona calls through one adapter interface, then a grounding pass and a Jinja2 report.

**Tech Stack:** Python 3.13, uv, Typer, pandas, psycopg 3 + pgvector, edgartools, yfinance, tiktoken, openai (embeddings only), anthropic, pydantic-settings, Jinja2, markdown, pytest.

**Spec:** `docs/superpowers/specs/2026-10-15-fundabot-phase1-2-design.md` (and `docs/architecture.md` it implements).

## Global Constraints

- Python `>=3.12`; environment via `uv`; package name `fundabot`, CLI entry point `fundabot`.
- EDGAR requests carry `User-Agent` from `EDGAR_USER_AGENT` and never exceed 10 requests/second (use a 0.15s minimum gap).
- Numbers are produced only in `fundabot/analytics/`. No model output is ever parsed for a number that then feeds a metric.
- A metric with missing input is `value=None` with `null_reason`, never 0.
- Every model call is logged to `model_calls` and refused if this month's cost would exceed `FUNDABOT_MONTHLY_BUDGET_USD`.
- Tests make no network calls. Tests that need Postgres are marked `db` and skip if `DATABASE_URL` is unreachable.
- Secrets only from `.env` (already present, gitignored). `.env.example` lists every variable.
- Output schema field names (`thesis, claims[text, evidence, direction, weight], key_risks, what_would_change_my_mind, score, confidence`) are fixed; Phase 3 consumes them.
- Evidence id formats: `metric:{metric_id}`, `metric:{metric_id}@{period}`, `chunk:{accession}#{item}-{n}`.
- Period labels: `FY2024`, `Q3FY2025`, `TTM`, `current`.

## Review Focus

1. A 10-Q nine-month year-to-date value must never appear as a quarter (Task 3 test `test_ytd_excluded`).
2. A company with no `CostOfRevenue` tag (banks) must yield `gross_margin=None, null_reason="tag_missing"` and the run must still finish (Task 4 test `test_missing_tag_is_null`).
3. Negative TTM earnings must give `pe=None, null_reason="negative_denominator"`, not a negative multiple (Task 5 test `test_pe_negative_earnings`).
4. An agent quoting a figure that exists only in a filing chunk (e.g. "$2.5 billion" from a note) must not be flagged as a hallucination (Task 12 test `test_number_in_chunk_text_is_grounded`).
5. A second `analyze` on the same day must make zero model calls (Task 13 test `test_rerun_same_snapshot_no_model_calls`).

---

## File structure

```
pyproject.toml  docker-compose.yml  .env.example  README.md
fundabot/
  config.py                 Settings (pydantic-settings) from .env
  cli.py                    Typer app: init-db, ingest, metrics, verify, analyze
  db/
    session.py              connect(), run_migrations(), small query helpers
    schema/001_init.sql     all tables
  ingest/
    edgar_client.py         rate-limited GET with User-Agent, disk cache
    edgar_facts.py          ticker->CIK, companyfacts fetch, normalize to facts rows
    edgar_filings.py        submissions list, filing download, Item extraction
    prices.py               yfinance daily closes
  analytics/
    tag_map.yaml            base item -> ordered us-gaap tags
    periods.py              facts -> per-item period series (annual, quarterly, derived Q4)
    metrics.py              Metric dataclass; growth/profitability/balance/cash/capital
    valuation.py            multiples, 5y range, DCF, reverse DCF
    red_flags.py            rules engine
    bounds.py               sanity bounds per metric id
  orchestrate/
    snapshot.py             create_snapshot(), content-addressed id
    pipeline.py             ingest -> snapshot -> metrics -> flags -> retrieve -> agents -> ground -> render
  retrieval/
    chunk.py                section text -> chunks with ids
    embed.py                Embedder protocol; OpenAIEmbedder; FakeEmbedder (tests)
    query.py                retrieve(snapshot, persona queries) -> chunks, capped
  agents/
    schemas.py              AGENT_OUTPUT_SCHEMA (JSON schema), validate()
    base.py                 Adapter protocol, CompletionResult, budget check, call logging
    adapters/anthropic.py   AnthropicAdapter
    adapters/fake.py        FakeAdapter (tests)
    context.py              build_user_message(metrics, flags, chunks)
    personas/{bull,bear,skeptic,quality,macro}.yaml
    runner.py               run_personas(snapshot, personas, adapter) -> analyses
  eval/grounding.py         ground(output, metrics, chunks) -> grounded output + stats
  report/render.py, report/templates/report.md.j2
scripts/make_fixture.py     trims a companyfacts JSON to tag_map tags
tests/fixtures/{AAPL,MSFT,JPM}_companyfacts.json, tests/fixtures/sections/*.txt
tests/test_*.py
```

---

### Task 1: Scaffold, database, migrations

**Files:**
- Create: `pyproject.toml`, `docker-compose.yml`, `README.md`, `fundabot/__init__.py`, `fundabot/config.py`, `fundabot/db/session.py`, `fundabot/db/schema/001_init.sql`, `fundabot/cli.py`, `tests/conftest.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `config.settings: Settings` with fields `anthropic_api_key, anthropic_workspace_id, openai_api_key, gemini_api_key, fmp_api_key, edgar_user_agent, database_url, fundabot_monthly_budget_usd: float, cache_dir: Path = ".cache"`.
- Produces: `db.session.connect() -> psycopg.Connection` (autocommit off, `pgvector.psycopg.register_vector` applied), `db.session.run_migrations(conn) -> list[str]` (filenames applied this call), `db.session.execute_values(conn, sql, rows)` helper for bulk upsert.
- Produces: `tests/conftest.py` fixture `db_conn` that connects to `DATABASE_URL`, runs migrations, truncates all tables, yields; marker `db` registered; skip when connection fails.

- [ ] **Step 1: `pyproject.toml`** with dependencies `typer, pandas, psycopg[binary], pgvector, pydantic-settings, pyyaml, httpx, edgartools, yfinance, tiktoken, openai, anthropic, jinja2, markdown, numpy`; dev `pytest, pytest-asyncio`; `[project.scripts] fundabot = "fundabot.cli:app"`; pytest marker `db`. Run `uv sync` and confirm `uv run fundabot --help` prints the app (empty command list is fine).

- [ ] **Step 2: `docker-compose.yml`**: service `db`, image `pgvector/pgvector:pg16`, env `POSTGRES_USER/PASSWORD/DB = fundabot`, port `5433:5432`, named volume. Run `docker compose up -d` and `docker compose ps` shows healthy.

- [ ] **Step 3: Write failing test** `tests/test_db.py::test_migrations_idempotent` (marker `db`): `run_migrations` on a fresh DB returns `["001_init.sql"]`; a second call returns `[]`; `SELECT count(*) FROM schema_migrations` is 1.

- [ ] **Step 4: Run** `uv run pytest tests/test_db.py -v` → FAIL (import error).

- [ ] **Step 5: `001_init.sql`** with `CREATE EXTENSION IF NOT EXISTS vector;` and tables:

```
schema_migrations(filename text pk, applied_at timestamptz default now())
raw_companyfacts(id serial pk, cik int, fetched_at timestamptz default now(), payload jsonb)
facts(cik int, taxonomy text, tag text, unit text, start_date date, end_date date, value numeric, fy int, fp text, form text, accession text, filed date,
      UNIQUE NULLS NOT DISTINCT (cik, taxonomy, tag, unit, start_date, end_date, accession))
filings(accession text pk, cik int, form text, filed date, period_end date, primary_doc_url text, items_ok bool)
filing_sections(accession text, item text, text text, pk(accession, item))
prices(cik int, date date, close numeric, pk(cik, date))
chunks(id text pk, accession text, item text, n int, text text, token_count int, embedding vector(1536))
snapshots(id text pk, ticker text, cik int, companyfacts_fetch_id int, accessions jsonb, price_date date, latest_close numeric, shares_outstanding numeric, code_sha text, created_at timestamptz default now())
metrics(snapshot_id text, metric_id text, period text, value numeric, unit text, formula_id text, inputs jsonb, flags jsonb, tag_used text, null_reason text, pk(snapshot_id, metric_id, period))
red_flags(snapshot_id text, flag_id text, severity text, evidence jsonb, detail text, pk(snapshot_id, flag_id))
analyses(snapshot_id text, persona text, provider text, model text, output jsonb, grounded jsonb, chunk_ids jsonb, created_at timestamptz default now(), pk(snapshot_id, persona, provider, model))
model_calls(id serial pk, snapshot_id text, persona text, provider text, model text, prompt_sha256 text, input_tokens int, output_tokens int, latency_ms int, cost_usd numeric, created_at timestamptz default now())
```

- [ ] **Step 6: Implement `config.py`, `db/session.py`** (migrations: read `schema/*.sql` sorted, skip those in `schema_migrations`, apply each in one transaction and insert the row). `cli.py`: Typer app with `init-db` calling `run_migrations` and printing applied files.

- [ ] **Step 7: Run** `uv run pytest tests/test_db.py -v` → PASS. `uv run fundabot init-db` prints `001_init.sql`.

- [ ] **Step 8: Commit** `chore: scaffold fundabot with postgres schema and migrations`.

---

### Task 2: EDGAR client and companyfacts ingest

**Files:**
- Create: `fundabot/ingest/edgar_client.py`, `fundabot/ingest/edgar_facts.py`, `scripts/make_fixture.py`, `tests/fixtures/AAPL_companyfacts.json`, `tests/fixtures/MSFT_companyfacts.json`, `tests/fixtures/JPM_companyfacts.json`
- Test: `tests/test_edgar_facts.py`

**Interfaces:**
- Produces: `edgar_client.get_json(url: str, max_age_hours: float = 24) -> dict` (rate-limited, disk-cached under `settings.cache_dir`, raises `EdgarError` on non-200).
- Produces: `edgar_facts.ticker_to_cik(ticker: str) -> int` (raises `UnknownTicker`), `edgar_facts.fetch_companyfacts(cik) -> dict`, `edgar_facts.normalize(payload: dict) -> pd.DataFrame` with columns `taxonomy, tag, unit, start_date, end_date, value, fy, fp, form, accession, filed` (dates as `datetime.date`, `start_date` None for instants), `edgar_facts.ingest(conn, ticker) -> tuple[int, int]` returning `(cik, companyfacts_fetch_id)` after inserting `raw_companyfacts` and upserting `facts`.

- [ ] **Step 1: `scripts/make_fixture.py TICKER`**: downloads companyfacts (real network, run once by hand), keeps only `us-gaap` tags listed in `fundabot/analytics/tag_map.yaml` (Task 4 writes it; for now use the tag list inline in the script and move it later) plus all `dei` tags, writes `tests/fixtures/{TICKER}_companyfacts.json`. Run it for AAPL, MSFT, JPM. Each file should be under 2 MB.

- [ ] **Step 2: Write failing tests** in `tests/test_edgar_facts.py`:
  - `test_normalize_columns_and_instants`: AAPL fixture → DataFrame has the listed columns; rows for `Assets` have `start_date is None`; rows for `Revenues`/`RevenueFromContractWithCustomerExcludingAssessedTax` have a start date.
  - `test_normalize_known_value`: a row with tag `RevenueFromContractWithCustomerExcludingAssessedTax`, `end_date == 2024-09-28`, `start_date == 2023-10-01` has `value == 391035000000`.
  - `test_ingest_upsert_idempotent` (marker `db`): monkeypatch `fetch_companyfacts` to return the fixture and `ticker_to_cik` to return 320193; `ingest` twice → `facts` row count unchanged on second call, `raw_companyfacts` has 2 rows.

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Implement** the three modules. `ticker_to_cik` uses `https://www.sec.gov/files/company_tickers.json`; companyfacts URL `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json`.

- [ ] **Step 5: Run** → PASS. **Commit** `feat: edgar companyfacts ingest with fixtures`.

---

### Task 3: Period series (the accuracy core)

**Files:**
- Create: `fundabot/analytics/periods.py`, `fundabot/analytics/tag_map.yaml`
- Test: `tests/test_periods.py`

**Interfaces:**
- Consumes: `edgar_facts.normalize` DataFrame.
- Produces: `periods.load_tag_map() -> dict[str, list[str]]`; `periods.build_series(facts: pd.DataFrame, tag_map) -> dict[str, pd.DataFrame]` keyed by base item; each DataFrame has columns `period, fy, fq, start, end, value, accession, tag, restated, derived` where `fq` is 0 for annual, 1..4 for quarters, and `period` is `FY{fy}` or `Q{fq}FY{fy}`. Also `periods.fiscal_year_end(facts) -> tuple[int, int]` (month, day) and `periods.ttm(series: pd.DataFrame, as_of: date) -> float | None` (sum of last four quarters ending on or before `as_of`; None if fewer than four).

**Rules (from the spec, pinned here):**
- Duration facts: annual = 350–380 days, quarterly = 80–100 days; others dropped.
- For each `(tag, start, end)`: value from the latest `filed`; `restated = abs(latest − earliest)/abs(earliest) > 0.005`.
- Fiscal year end `(month, day)` = end date of the most recently filed annual fact of the first available item in tag map order. FY label of any period end `e`: `e.year` if `e <= fye(e.year) + 10 days`, else `e.year + 1`. Quarter number = ordinal of the quarter's end within its FY, computed as `round(months between FY start and quarter end / 3)`.
- Q4 derived = FY − (Q1+Q2+Q3) when all three exist, `derived=True`; else absent.
- Instant items (balance sheet) are labelled by matching `end` to a known annual or quarter end (±10 days); the fiscal-period end for Q4 equals the FY end.
- Tag precedence: first tag in the list that has any annual fact wins for the whole series; `tag` column records it. Do not mix tags within an item.

`tag_map.yaml` base items and tags (first match wins):

```yaml
revenue: [RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, SalesRevenueNet]
cost_of_revenue: [CostOfRevenue, CostOfGoodsAndServicesSold, CostOfGoodsSold]
gross_profit: [GrossProfit]
operating_income: [OperatingIncomeLoss]
pretax_income: [IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest, IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments]
income_tax: [IncomeTaxExpenseBenefit]
net_income: [NetIncomeLoss, ProfitLoss]
eps_diluted: [EarningsPerShareDiluted]
ocf: [NetCashProvidedByUsedInOperatingActivities]
capex: [PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets]
da: [DepreciationDepletionAndAmortization, DepreciationAndAmortization, DepreciationAmortizationAndAccretionNet]
sbc: [ShareBasedCompensation, AllocatedShareBasedCompensationExpense]
interest_expense: [InterestExpense, InterestExpenseNonoperating, InterestExpenseDebt]
total_assets: [Assets]
total_liabilities: [Liabilities]
equity: [StockholdersEquity, StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest]
cash: [CashAndCashEquivalentsAtCarryingValue, CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents]
short_term_debt: [LongTermDebtCurrent, DebtCurrent, ShortTermBorrowings]
long_term_debt: [LongTermDebtNoncurrent, LongTermDebt]
goodwill: [Goodwill]
intangibles: [IntangibleAssetsNetExcludingGoodwill]
current_assets: [AssetsCurrent]
current_liabilities: [LiabilitiesCurrent]
dividends_paid: [PaymentsOfDividendsCommonStock, PaymentsOfDividends]
buybacks: [PaymentsForRepurchaseOfCommonStock]
share_issuance: [ProceedsFromIssuanceOfCommonStock]
acquisitions: [PaymentsToAcquireBusinessesNetOfCashAcquired]
```

Gross profit, when the `GrossProfit` tag is absent but revenue and cost of revenue exist, is computed in Task 4, not here.

- [ ] **Step 1: Write failing tests** `tests/test_periods.py`:
  - `test_aapl_fiscal_year_end`: `fiscal_year_end(aapl) == (9, 28)` ± allow day in 24..30 (52/53-week years): assert month 9 and day ≥ 24.
  - `test_aapl_annual_revenue`: `series["revenue"]` row `period == "FY2024"` has `value == 391035000000` and `tag == "RevenueFromContractWithCustomerExcludingAssessedTax"`.
  - `test_ytd_excluded`: no row in `series["revenue"]` has `end − start` between 150 and 300 days.
  - `test_q4_derived`: AAPL `Q4FY2024` revenue == FY2024 − (Q1+Q2+Q3 FY2024) and `derived is True`; Q1FY2024 is `derived is False` and ends 2023-12-30.
  - `test_msft_june_fiscal_year`: MSFT `FY2024` revenue ends 2024-06-30 with `value == 245122000000`; the quarter ending 2024-03-31 is `Q3FY2024`.
  - `test_jpm_uses_revenues_tag`: JPM `series["revenue"].tag.iloc[0] == "Revenues"`; `"cost_of_revenue" not in series`.
  - `test_restated_flag`: build a minimal DataFrame by hand with two filings of the same period differing by 2% → `restated is True` and value is the later one.
  - `test_ttm`: `ttm(series["revenue"], date(2024, 9, 28)) == 391035000000`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: period series builder with Q4 derivation and restatement flags`.

---

### Task 4: Metrics (growth, profitability, balance sheet, cash quality, capital allocation) and bounds

**Files:**
- Create: `fundabot/analytics/metrics.py`, `fundabot/analytics/bounds.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `periods.build_series` output.
- Produces: `@dataclass Metric(metric_id: str, period: str, value: float | None, unit: str, formula_id: str, inputs: dict, flags: list[str], tag_used: str | None, null_reason: str | None)` and `metrics.compute_fundamental_metrics(series: dict[str, pd.DataFrame]) -> list[Metric]`. `unit` ∈ `{"USD", "ratio", "USD/share", "shares"}`. `formula_id` is the formula text, e.g. `"gross_profit / revenue"`. `inputs` maps each input to `{"item": ..., "period": ..., "value": ..., "tag": ...}`. `bounds.check(metric_id, value) -> bool` and `bounds.BOUNDS: dict[str, tuple[float, float]]`.

**Metric ids and definitions:**
- Base items emitted as metrics for every FY and quarter period: every key of the tag map plus `gross_profit` (fallback `revenue − cost_of_revenue`, formula recorded), `fcf = ocf − capex`, `ebitda = operating_income + da`, `total_debt = short_term_debt + long_term_debt` (missing short-term treated as 0 only if long-term exists; flag `partial_debt`), `net_debt = total_debt − cash`.
- Growth, FY periods: `{item}_yoy`, `{item}_cagr_3y`, `{item}_cagr_5y` for `item ∈ {revenue, gross_profit, operating_income, eps_diluted, fcf}`; quarterly `{item}_yoy` for the last 8 quarters (same quarter prior year). CAGR is None if either endpoint ≤ 0 (`null_reason="nonpositive_endpoint"`).
- Profitability, FY: `gross_margin, operating_margin, net_margin, fcf_margin` (÷ revenue); `roe = net_income / avg(equity FY, equity FY−1)`; `roa` likewise over total_assets; `roic = operating_income × (1 − tax_rate) / avg(equity + total_debt − cash)` with `tax_rate = clamp(income_tax / pretax_income, 0, 0.35)` (flag `tax_rate_clamped` if clamped).
- Balance sheet, FY and latest quarter: `net_debt_to_ebitda` (ebitda TTM for quarters), `interest_coverage = operating_income / interest_expense`, `current_ratio`, `goodwill_to_assets`.
- Cash quality, FY: `fcf_to_net_income`, `accruals_ratio = (net_income − ocf) / avg total_assets`, `sbc_to_revenue`, `capex_to_da`.
- Capital allocation, FY: `net_buybacks = buybacks − share_issuance` (USD), `dividend_coverage = fcf / dividends_paid`, `acquisitions_to_ocf`.
- Any ratio with denominator ≤ 0 → `None`, `null_reason="nonpositive_denominator"`; missing input → `None`, `null_reason="tag_missing"` (and `inputs` names the missing item).
- `bounds.BOUNDS`: margins `(−2, 1)`, `roe/roa/roic (−2, 2)`, `current_ratio (0, 50)`, `goodwill_to_assets (0, 1)`, `accruals_ratio (−1, 1)`, `sbc_to_revenue (0, 1)`, `capex_to_da (0, 20)`, yoy `(−1, 10)`. Out of range → keep value, add flag `out_of_bounds`. Flags from the series (`restated`, `derived`) propagate to any metric that used that period.

- [ ] **Step 1: Write failing tests**:
  - `test_aapl_gross_margin_fy2024`: value within 0.001 of `180683/391035`; `tag_used` is the revenue tag; `inputs` has keys `gross_profit, revenue`.
  - `test_aapl_fcf_fy2024`: `fcf@FY2024 == 118254000000 − 9447000000`.
  - `test_missing_tag_is_null`: JPM `gross_margin@FY2024` is `None` with `null_reason == "tag_missing"`; the list still contains `net_income@FY2024` with a value.
  - `test_cagr_nonpositive_endpoint`: hand-built series with a negative FY−3 operating income → `operating_income_cagr_3y` None, reason `nonpositive_endpoint`.
  - `test_out_of_bounds_flag`: hand-built series with revenue 1 and gross profit 5 → `gross_margin` has flag `out_of_bounds` and value 5.0.
  - `test_derived_flag_propagates`: AAPL `revenue_yoy@Q4FY2024` has flag `derived`.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: fundamental metrics with provenance and bounds`.

---

### Task 5: Prices, shares, valuation and DCF

**Files:**
- Create: `fundabot/ingest/prices.py`, `fundabot/analytics/valuation.py`
- Test: `tests/test_valuation.py`

**Interfaces:**
- Produces: `prices.fetch_daily_closes(ticker: str, years: int = 5) -> pd.DataFrame[date, close]` (yfinance; raises `PriceUnavailable`), `prices.ingest(conn, cik, ticker) -> date | None` (upserts `prices`, returns latest date), `prices.close_on_or_after(prices_df, d: date) -> tuple[date, float] | None`.
- Produces: `valuation.shares_outstanding(facts: pd.DataFrame) -> pd.DataFrame[end_date, shares, accession]` from `dei:EntityCommonStockSharesOutstanding`: group by `end_date`, keep the latest `accession` for that date, sum all entries with that accession and date (multi-class).
- Produces: `valuation.compute_valuation(series, facts, prices_df, filings: pd.DataFrame[accession, form, filed], as_of: date) -> list[Metric]`:
  - `current` period: `market_cap, ev, pe, ev_ebitda, ev_sales, fcf_yield, peg` using the latest close on or before `as_of`, latest cover-page shares, TTM metrics as of the latest quarter end. `peg = pe / (eps_diluted_cagr_3y × 100)`.
  - One row per 10-K/10-Q filing in the last 5 years for `pe, ev_ebitda, ev_sales, fcf_yield` at period = that filing's period label, price = close on or after filed date, shares = that filing's cover-page count, TTM as of the filing's period end.
  - `{ratio}_5y_min, _5y_median, _5y_max, _5y_percentile` at period `current` (percentile of current among history, 0–1).
  - `dcf_bear, dcf_base, dcf_bull` (USD/share) and `reverse_dcf_implied_growth` (ratio) at `current`, with `inputs` recording `discount_rate=0.10, terminal_growth=0.025, years=10, fcf0, growth, net_debt, shares`. Base growth = `fcf_cagr_5y` clamped to [0, 0.25] (fallback `fcf_cagr_3y`, else None with `null_reason="no_fcf_history"`); bear = base − 0.05; bull = base + 0.05. Reverse DCF: bisection on growth in [−0.5, 1.0] to match current `ev`, tolerance 1e-4.
  - Denominator ≤ 0 → `None`, `null_reason="negative_denominator"`. No price → every valuation metric `None`, `null_reason="price_unavailable"`.

- [ ] **Step 1: Write failing tests** (prices_df and filings built by hand; no network):
  - `test_shares_multi_class_summed`: hand-built facts with two `dei` entries same date, same accession, 100 and 50 → 150; an older accession same date ignored.
  - `test_pe_current`: AAPL fixture, prices_df with a single close 200.0 on `as_of`, shares from fixture → `pe@current == 200 / ttm eps` within 1e-6, and `inputs["price"]["value"] == 200.0`.
  - `test_pe_negative_earnings`: hand-built series with negative TTM EPS → `pe` None, reason `negative_denominator`.
  - `test_history_range`: three filings with closes 100, 200, 300 and constant TTM EPS 10 → `pe_5y_min == 10, pe_5y_max == 30, pe_5y_median == 20`; with current close 250 → `pe_5y_percentile == 2/3` (fraction of history ≤ current, using `<=` over 3 samples gives 2/3).
  - `test_dcf_reverse_roundtrip`: pick growth 0.08, compute `ev_dcf` with the base formula, set current ev to it → `reverse_dcf_implied_growth` within 1e-3 of 0.08.
  - `test_price_unavailable`: `prices_df=None` → all valuation metrics None with reason `price_unavailable`, list not empty.

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat: valuation multiples, 5y ranges and DCF`.

---

### Task 6: Numeric red flags

**Files:**
- Create: `fundabot/analytics/red_flags.py`
- Test: `tests/test_red_flags.py`

**Interfaces:**
- Produces: `@dataclass RedFlag(flag_id: str, severity: str, evidence: list[str], detail: str)` with `severity ∈ {"high", "medium"}`; `red_flags.numeric_flags(metrics: list[Metric]) -> list[RedFlag]`. Evidence ids use `metric:{id}@{period}`. Text and filing-list flags are added in Task 8 via `red_flags.text_flags(sections, filings) -> list[RedFlag]`.

Rules: `accruals_high` (high) if `accruals_ratio` latest FY > 0.10; `fcf_below_ni` (medium) if `fcf_to_net_income` < 1 in ≥ 3 of last 5 FY; `goodwill_heavy` (medium) if `goodwill_to_assets` latest FY > 0.5. Skip a rule when its inputs are None.

- [ ] **Step 1: Tests**: `test_accruals_flag` (hand-built metrics 0.12 → one flag with evidence `metric:accruals_ratio@FY2024`), `test_fcf_below_ni_three_of_five`, `test_none_inputs_skip` (all None → `[]`).
- [ ] **Step 2–5:** FAIL, implement, PASS, commit `feat: numeric red flags`.

---

### Task 7: Snapshot and Phase 1 CLI (`ingest`, `metrics`, `verify`)

**Files:**
- Create: `fundabot/orchestrate/snapshot.py`, `fundabot/orchestrate/pipeline.py`; modify `fundabot/cli.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Produces: `snapshot.create_snapshot(conn, ticker, cik, companyfacts_fetch_id, accessions: list[str], price_date, latest_close, shares) -> str` (id = sha256 hex[:16] of `f"{cik}|{fetch_id}|{','.join(sorted(accessions))}|{price_date}"`; `code_sha` from `git rev-parse HEAD` or `"unknown"`; upsert). `snapshot.latest_for(conn, ticker) -> dict | None`.
- Produces: `snapshot.store_metrics(conn, snapshot_id, metrics: list[Metric])` (delete-then-insert for the snapshot) and `snapshot.store_flags(conn, snapshot_id, flags)`; `snapshot.load_metrics(conn, snapshot_id) -> list[Metric]`.
- Produces: `pipeline.prepare(conn, ticker, force: bool) -> str` (snapshot id): ingest facts (reuse the latest `raw_companyfacts` fetch under 24h old unless `force`), prices, list filings from the submissions JSON (last 5 10-K and 8 10-Q accessions; a latest annual form of `20-F`/`40-F` raises `UnsupportedFiler`), create snapshot, compute all metrics (Tasks 4 and 5) and numeric flags, store. Filing text ingest is added to `prepare` in Task 8.
- CLI: `fundabot ingest T...`, `fundabot metrics T [--json]` (table grouped by metric id, periods as columns, flags as suffix `*`; `--json` dumps `Metric` list), `fundabot verify T` (compare `revenue, net_income, total_assets, ocf` for the last 4 FY against `yfinance.Ticker(t).income_stmt / balance_sheet / cashflow`; print a table with pct diff; mark rows over 2%).

- [ ] **Step 1: Tests**: `test_snapshot_id_deterministic` (same inputs same id; different price date different id); `test_store_load_metrics_roundtrip` (marker `db`: store 3 metrics incl. one None value, load, equal).
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5: Hand check.** Run `uv run fundabot ingest` then `metrics` and `verify` for: AAPL MSFT NVDA AMZN GOOGL META TSLA COST UNH JPM XOM PG HD NFLX AMD CRM PLTR UBER DIS NKE. Record in `docs/handcheck-2026-10.md` a table of ticker, 4 checked items, max pct diff, and notes on any `tag_missing`. Fix tag map gaps found (add tags, re-run). Phase 1 exit is this table with no unexplained diff over 2%.
- [ ] **Step 6: Commit** `feat: snapshot, metrics and verify CLI; hand-check results`.

---

### Task 8: Filings ingest, Item extraction, text flags

**Files:**
- Create: `fundabot/ingest/edgar_filings.py`, `tests/fixtures/sections/aapl_10k_item1a.txt` (first ~3,000 chars of a real Item 1A, saved by hand), modify `red_flags.py`, `pipeline.prepare`
- Test: `tests/test_edgar_filings.py`, extend `tests/test_red_flags.py`

**Interfaces:**
- Produces: `edgar_filings.list_filings(cik) -> pd.DataFrame[accession, form, filed, period_end, primary_doc_url]` from `https://data.sec.gov/submissions/CIK{cik:010d}.json` (all forms, used for NT detection too); `edgar_filings.select_for_snapshot(filings_df) -> list[str]` (last 5 `10-K`/`10-K/A` by filed and last 8 `10-Q`/`10-Q/A`); `edgar_filings.extract_items(accession, cik) -> dict[str, str]` using `edgartools` (`set_identity(settings.edgar_user_agent)`, `Filing(...).obj()`; verify the exact item access API in the installed version at task start). Item keys normalized: 10-K `item1, item1a, item1c, item3, item7, item7a, item8, item9a`; 10-Q `p1item2, p1item3, p1item4, p2item1, p2item1a`. If extraction raises or returns no items, return `{"full": full_text}` and set `filings.items_ok = false`.
- Produces: `edgar_filings.ingest(conn, cik, accessions) -> None` (skips accessions already in `filing_sections`).
- Produces: `red_flags.text_flags(sections: dict[str, str], filings_df) -> list[RedFlag]`: `going_concern` (high) if regex `substantial doubt(?s).{0,300}going concern|going concern(?s).{0,300}substantial doubt` (case-insensitive) matches any of `item7, item8, item9a` of the latest 10-K, evidence `chunk:{accession}#{item}-0`; `late_filing` (medium) if any form starts with `NT 10-` filed within 2 years of the latest filing, evidence listing those accessions.

- [ ] **Step 1: Tests**: `test_item_key_normalization` (`"Item 1A"→"item1a"`, `"Part II Item 1A"→"p2item1a"`), `test_select_for_snapshot_counts` (hand-built filings_df with 7 10-Ks and 12 10-Qs → 5 and 8, newest first), `test_going_concern_regex` (positive sample and the AAPL fixture negative), `test_late_filing_flag`.
- [ ] **Step 2–4:** FAIL, implement, PASS. Then run `uv run fundabot ingest AAPL` and confirm `filing_sections` has rows for `item1a` and `item7` for the latest 10-K.
- [ ] **Step 5: Commit** `feat: filing text ingest with item extraction and text red flags`.

---

### Task 9: Chunking, embeddings, retrieval

**Files:**
- Create: `fundabot/retrieval/chunk.py`, `fundabot/retrieval/embed.py`, `fundabot/retrieval/query.py`
- Test: `tests/test_retrieval.py`

**Interfaces:**
- Produces: `chunk.chunk_section(accession, item, text, size=800, overlap=100) -> list[Chunk]` with `@dataclass Chunk(id, accession, item, n, text, token_count)`, tokens by `tiktoken.get_encoding("cl100k_base")`, id `f"{accession}#{item}-{n}"`.
- Produces: `embed.Embedder` protocol `embed(texts: list[str]) -> list[list[float]]`; `OpenAIEmbedder(model="text-embedding-3-small")` batching 100 texts per request, cost logged to `model_calls` with `persona="embed"` at the price from the claude-api skill's pricing notes or OpenAI's published price (record the number used in a constant); `FakeEmbedder` returning a deterministic 1536-dim vector from a hash of the text (used in tests).
- Produces: `query.embed_missing(conn, embedder, accessions)` (chunk and embed every section of the accessions whose chunks are absent); `query.retrieve(conn, embedder, accessions: list[str], queries: list[str], top_k: int = 8, max_tokens: int = 30000) -> list[Chunk]` (cosine via pgvector `<=>` restricted to the accessions, union over queries, dedupe by id, keep best-score order, stop when the token cap is reached).

- [ ] **Step 1: Tests**: `test_chunk_ids_and_overlap` (a 2,000-token text → 3 chunks; ids `...-0, -1, -2`; chunk 1 starts with the last 100 tokens of chunk 0), `test_retrieve_cap_and_dedupe` (marker `db`: insert 50 fake chunks of 1,000 tokens with FakeEmbedder; `retrieve` with two queries returns ≤ 30 chunks, no duplicate ids).
- [ ] **Step 2–5:** FAIL, implement, PASS, commit `feat: chunking, embeddings and retrieval`.

---

### Task 10: Adapter interface, Anthropic adapter, call log and budget

**Files:**
- Create: `fundabot/agents/schemas.py`, `fundabot/agents/base.py`, `fundabot/agents/adapters/anthropic.py`, `fundabot/agents/adapters/fake.py`
- Test: `tests/test_adapters.py`

Before implementing, load the `claude-api` skill for the current structured-output pattern, model id and prices.

**Interfaces:**
- Produces: `schemas.AGENT_OUTPUT_SCHEMA` (JSON schema with `additionalProperties: false`, `direction` enum `positive|negative`, `weight` integer 1–5, `score` integer −10..10, `confidence` number 0..1, all fields required) and `schemas.validate(output: dict) -> dict` (raises `SchemaError`).
- Produces: `@dataclass CompletionResult(output: dict, input_tokens, output_tokens, latency_ms, cost_usd, model)`; `class Adapter(Protocol): provider: str; model: str; async def complete(self, system: str, user: str, schema: dict) -> CompletionResult`.
- Produces: `base.call_logged(conn, adapter, snapshot_id, persona, system, user, schema) -> CompletionResult`: computes `prompt_sha256` over `system + user`, checks `SELECT coalesce(sum(cost_usd),0) FROM model_calls WHERE created_at >= date_trunc('month', now())` + an estimate (input tokens ≈ len/4 at the model's input price) against `settings.fundabot_monthly_budget_usd` and raises `BudgetExceeded` before calling; inserts the `model_calls` row after.
- Produces: `AnthropicAdapter(model: str = "claude-sonnet-5-5")`: `AsyncAnthropic(api_key=..., default_headers={"anthropic-workspace-id": settings.anthropic_workspace_id} if set)`; forces output with one tool `report_analysis` whose `input_schema` is the schema and `tool_choice={"type": "tool", "name": "report_analysis"}`; `max_tokens=4000`; retries 3× with exponential backoff on 429/5xx/timeout; 120s timeout; cost from a `PRICES = {model: (input_per_mtok, output_per_mtok)}` table.
- Produces: `FakeAdapter(outputs: list[dict] | callable)` returning canned outputs with zero cost and counting calls.

- [ ] **Step 1: Tests**: `test_schema_rejects_extra_field_and_bad_weight`, `test_budget_exceeded_refuses` (marker `db`: insert a `model_calls` row with cost = budget; `call_logged` with FakeAdapter raises `BudgetExceeded` and FakeAdapter call count is 0), `test_call_logged_inserts_row` (marker `db`).
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5:** Smoke test by hand only if `ANTHROPIC_WORKSPACE_ID` is set: `uv run python -c` calling `AnthropicAdapter().complete("You return JSON.", "Give a trivial analysis of a company with revenue 1.", AGENT_OUTPUT_SCHEMA)` and print tokens and cost. If the workspace id is missing, note it in the commit message and continue.
- [ ] **Step 6: Commit** `feat: adapter interface, anthropic adapter, call log and budget cap`.

---

### Task 11: Personas, context assembly, agent runner

**Files:**
- Create: `fundabot/agents/personas/{bull,bear,skeptic,quality,macro}.yaml`, `fundabot/agents/context.py`, `fundabot/agents/runner.py`
- Test: `tests/test_agents.py`

**Interfaces:**
- Persona YAML fields: `name, bias (one paragraph, stated plainly), reads_most (list), retrieval_queries (list of 4–6 strings), extra_rules (list, optional)`. Shared base queries live in `context.BASE_QUERIES = ["business overview and segments", "results of operations latest period", "guidance and outlook", "liquidity and capital resources", "risk factors"]`.
- Produces: `context.system_prompt(persona: dict) -> str`: role and bias; the hard rules verbatim: "Cite a metric id or chunk id for every claim. Never state a number that is not in the inputs. If the inputs do not support a judgement, write 'insufficient data' rather than guess. Metrics marked with flags (restated, derived, out_of_bounds) are less reliable; say so when you use them."; the evidence id formats; the output schema description.
- Produces: `context.build_user_message(ticker, snapshot: dict, metrics: list[Metric], flags: list[RedFlag], chunks: list[Chunk], notes: list[str]) -> str`: sections `SNAPSHOT` (ticker, price date, filings used), `METRICS` (one line per metric: `metric:{id}@{period} = {value} {unit} [flags] <- {formula_id}`; None values shown as `null ({null_reason})`), `RED_FLAGS`, `NOTES` (e.g. for macro: "No macro series or peer medians are available in this build."), `FILING_EXCERPTS` (`chunk:{id}` header then text).
- Produces: `runner.run_personas(conn, snapshot_id, persona_names: list[str], adapter, embedder) -> list[dict]`: for each persona without an `analyses` row for `(snapshot_id, persona, adapter.provider, adapter.model)`: retrieve chunks (persona queries + base), build messages, `call_logged`, `schemas.validate`, insert `analyses` with `output` and `chunk_ids`. Personas run concurrently with `asyncio.gather`. Returns all analyses for the snapshot (cached plus new).

- [ ] **Step 1: Tests**: `test_personas_load_all_five` (each has the required fields); `test_user_message_metric_lines` (a None metric renders `null (tag_missing)`, a flagged one shows `[derived]`); `test_runner_skips_cached` (marker `db`: FakeAdapter, run twice → adapter call count equals 5 after first, still 5 after second).
- [ ] **Step 2–5:** FAIL, implement (write the five persona files with care; the bias paragraphs are the product), PASS, commit `feat: personas, context assembly and agent runner`.

---

### Task 12: Grounding

**Files:**
- Create: `fundabot/eval/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Produces: `grounding.ground(output: dict, metrics: list[Metric], chunks: list[Chunk]) -> dict`: returns a copy of `output` where each claim gains `evidence_resolved: bool`, `unsupported_numbers: list[str]`; claims with any unresolved evidence id are moved to `dropped_claims` (list) and removed from `claims`; `thesis_unsupported_numbers`, `risks_unsupported_numbers`; and `stats = {claims_total, claims_dropped, numbers_total, numbers_unsupported, hallucination_rate}`.
- Number extraction regex: `\$?\s?-?\d[\d,]*(?:\.\d+)?\s?(%|x|bn|b|billion|mm|m|million|k|thousand|trillion|tn)?` case-insensitive. Normalize to a float (`%` → ÷100; `k/m/b/tn` multipliers). Ignore: 4-digit integers 1900–2100 with no unit, bare integers < 1000 with no unit and no `$`, and tokens inside an evidence id. A number is supported if it is within 1% relative (or absolute 0.005 for values with `%`) of any metric value in the list, or its raw text (digits and separators) appears verbatim in any retrieved chunk's text.

- [ ] **Step 1: Tests**: `test_unresolved_evidence_drops_claim`, `test_metric_period_evidence_resolves` (`metric:revenue@FY2024`), `test_number_in_metrics_supported` (`"gross margin of 46.2%"` vs 0.4621), `test_number_in_chunk_text_is_grounded` (`"$2.5 billion"` absent from metrics, present in a chunk → supported), `test_years_and_small_ints_ignored` (`"in 2024, over 3 quarters"` → numbers_total 0), `test_hallucinated_number_flagged` (`"revenue of $420 billion"` vs 391.035B → unsupported).
- [ ] **Step 2–5:** FAIL, implement, PASS, commit `feat: grounding check for evidence ids and numbers`.

---

### Task 13: Report, `analyze` CLI, end-to-end

**Files:**
- Create: `fundabot/report/render.py`, `fundabot/report/templates/report.md.j2`, modify `fundabot/orchestrate/pipeline.py`, `fundabot/cli.py`, `README.md`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `render.render_report(ticker, snapshot: dict, metrics, flags, analyses: list[dict] (grounded), chunks_by_id: dict[str, Chunk], out_dir: Path) -> tuple[Path, Path]` (md, html). Sections in order: header (ticker, snapshot id, price date, close, filings used with form and filed date), red flags, score strip (per persona score and confidence, plus "unweighted mean, no judge (Phase 3)"), per-persona section (thesis, claims as a list with evidence ids rendered as links to the appendix and `⚠ unsupported: ...` where applicable, key risks, what would change my mind, grounding stats, dropped claims count), metrics table (grouped: growth, profitability, balance sheet, cash quality, capital allocation, valuation, intrinsic value; last 5 FY and last 4 quarters as columns; flags as `*` with a legend), DCF assumptions, sources appendix (every cited metric id with formula and tag; every cited chunk id with accession, item and first 120 chars). HTML via `markdown.markdown(md, extensions=["tables"])` wrapped in a minimal page with a `<style>` for tables.
- Produces: `pipeline.analyze(conn, ticker, personas, adapter, embedder, force=False, rerun=False, out_dir=Path("reports")) -> Path` (html path): `prepare` → `embed_missing` → (if `rerun`: delete analyses for the snapshot) → `run_personas` → `ground` each (store in `analyses.grounded`) → `render_report` into `reports/{TICKER}/{snapshot_id}.{md,html}`.
- CLI: `fundabot analyze T... [--personas bull,bear,...] [--model claude-sonnet-5-5] [--open] [--force] [--rerun]`; tickers sequential; `--open` calls `webbrowser.open`. Errors per ticker are printed and the loop continues.

- [ ] **Step 1: Tests** (marker `db`, fixtures + FakeAdapter + FakeEmbedder, `prepare` monkeypatched to use fixture facts, sections from `tests/fixtures/sections`, and a hand-built prices_df):
  - `test_end_to_end_report_written`: html and md exist; md contains `metric:gross_margin@FY2024` in the appendix and the persona names.
  - `test_rerun_same_snapshot_no_model_calls`: second `analyze` → FakeAdapter call count unchanged, `model_calls` row count unchanged.
  - `test_rerun_flag_calls_again`: `rerun=True` → call count doubles.
- [ ] **Step 2–4:** FAIL, implement, PASS.
- [ ] **Step 5:** README: setup (`docker compose up -d`, `uv sync`, `.env`), the five commands, the workspace-id note, cost expectations placeholder to be filled after the first real run.
- [ ] **Step 6:** If `ANTHROPIC_WORKSPACE_ID` is set, run `uv run fundabot analyze AAPL --open` once and record tokens and cost per persona in README.
- [ ] **Step 7: Commit** `feat: report rendering and analyze pipeline`.
