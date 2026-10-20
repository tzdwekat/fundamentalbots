# Phase 5d FMP-Primary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the fundamentals series from FMP's standardised statements, fall back to EDGAR per period and item, keep every formula naming its source, and cross-check FMP against EDGAR.

**Architecture:** A raw statements table and ingest; `fundabot/analytics/fmp_series.py` turns statements into the same per-item frames `periods.build_series` produces and merges them with the EDGAR frames; `prepare()` uses the merged series; `verify.crosscheck_edgar` compares the two; the page and the drawer show source and link.

**Tech Stack:** pandas, psycopg; fixtures `tests/fixtures/fmp_aapl_{income,balance,cashflow}_{annual,quarter}.json`.

**Spec:** docs/superpowers/specs/2026-10-19-fundabot-phase5d-fmp-primary-design.md

## Global Constraints

- Frames follow `periods.COLUMNS` exactly: `period, fy, fq, start, end, value, accession, tag, restated, derived, split_adjusted`.
- FMP field map (item → statement.field): revenue→income.revenue, cost_of_revenue→income.costOfRevenue, gross_profit→income.grossProfit, operating_income→income.operatingIncome, pretax_income→income.incomeBeforeTax, income_tax→income.incomeTaxExpense, net_income→income.netIncome, eps_diluted→income.epsDiluted, interest_expense→income.interestExpense, da→income.depreciationAndAmortization (fallback cashflow.depreciationAndAmortization), diluted_shares→income.weightedAverageShsOutDil, ocf→cashflow.operatingCashFlow, capex→|cashflow.capitalExpenditure|, sbc→cashflow.stockBasedCompensation, dividends_paid→|cashflow.commonDividendsPaid|, buybacks→|cashflow.commonStockRepurchased|, share_issuance→cashflow.commonStockIssuance, acquisitions→|cashflow.acquisitionsNet|, total_assets→balance.totalAssets, total_liabilities→balance.totalLiabilities, equity→balance.totalStockholdersEquity, cash→balance.cashAndCashEquivalents, short_term_debt→balance.shortTermDebt, long_term_debt→balance.longTermDebt, total_debt_reported→balance.totalDebt, goodwill→balance.goodwill, intangibles→balance.intangibleAssets, current_assets→balance.totalCurrentAssets, current_liabilities→balance.totalCurrentLiabilities.
- An FMP value of 0 (or null) is a missing line: no row.
- `tag` = `fmp:<income|balance|cashflow>:<field>`; formula id = tag when it starts with `fmp:`, else `xbrl:<tag>`.
- `split_adjusted=True` only for eps_diluted and diluted_shares; `restated=False`; `derived=False`.
- `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot`; `fundabot init-db` after the migration.

## Review Focus

1. FMP returns no statements (or a 4xx): `prepare()` runs on EDGAR alone with a warning; metrics identical to before this phase (Task 3 test `test_prepare_without_fmp_statements_is_edgar_only`).
2. An FMP zero on an item EDGAR carries (interest_expense FY2024 for AAPL): the merged frame holds EDGAR's row (Task 2 test `test_zero_is_missing_and_edgar_fills`).
3. Older fiscal years beyond FMP's window come from EDGAR and keep `xbrl:` formulas (Task 2 test `test_merge_keeps_older_edgar_history`).
4. Quarterly `ttm()` still works on FMP rows (contiguous `start`/`end`) (Task 2 test `test_fmp_quarters_support_ttm`).
5. The agents' pruned table still shows FMP base items at fiscal years and the newest quarter (Task 2 test `test_select_metrics_shows_fmp_base_items`).

---

### Task 1: Statements ingest

**Files:** create `fundabot/db/schema/010_fmp_statements.sql`; modify `fundabot/ingest/fmp_extra.py` (`fetch_statements`, `ingest_statements`, `load_statements`), `tests/test_db.py` (10 migrations); test `tests/test_fmp_extra.py`.

**Interfaces:**
- `010_fmp_statements.sql`: `CREATE TABLE fmp_statements (cik int NOT NULL, statement text NOT NULL, period_type text NOT NULL, fiscal_year int NOT NULL, period text NOT NULL, date date NOT NULL, filing_date date, data jsonb NOT NULL, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, statement, period_type, fiscal_year, period));`
- `fmp_extra.fetch_statements(ticker, get=fmp.get, annual_limit=12, quarter_limit=24) -> dict[str, list[dict]]` keyed `income_annual, income_quarter, balance_annual, balance_quarter, cashflow_annual, cashflow_quarter`; a statement that fails raises `FMPError` only if all six fail, otherwise logs and returns the rest.
- `fmp_extra.ingest_statements(conn, cik, ticker, get=, max_age_hours=24, force=False) -> int` (rows upserted; `_attempted` stamp).
- `fmp_extra.load_statements(conn, cik) -> dict[str, list[dict]]` same keys, each row `{"fiscal_year", "period", "date", "filing_date", **data}` newest first.

- [ ] **Step 1: Failing tests** `test_statements_roundtrip` (fixture-backed `get`; 6 keys; income_annual has 8 rows with FY2025 revenue 416,161,000,000; `fiscal_year` int; second ingest returns 0 within 24 h; `force=True` refetches), `test_statements_partial_failure` (`get` raises for `cash-flow-statement` → the other four stored, no exception).
- [ ] **Step 2:** FAIL. **Step 3:** implement; `fundabot init-db`. **Step 4:** PASS. **Step 5:** Commit `feat: FMP statements ingest`.

### Task 2: FMP series and merge

**Files:** create `fundabot/analytics/fmp_series.py`; modify `fundabot/analytics/metrics.py:98` (formula id), `fundabot/agents/context.py` (`select_metrics` prefix check); tests `tests/test_fmp_series.py`, `tests/test_context_pruning.py`.

**Interfaces:**
- `fmp_series.FIELD_MAP: dict[str, tuple[str, str, bool]]` item → (statement, field, absolute).
- `fmp_series.build_fmp_series(statements: dict[str, list[dict]], filings: pd.DataFrame | None) -> dict[str, pd.DataFrame]`: per item one frame (annual rows fq=0 with period `FY{fy}`, quarterly rows fq 1..4 with `Q{n}FY{fy}`), zero/null dropped, `start` from the previous stored period of the same type (end + 1 day) else end − 91 / 364 days, `accession` matched from `filings` (`filed` within 3 days of `filing_date`, form 10-K for annual and Q4, 10-Q otherwise) else `fmp:<filing_date>`; empty items omitted.
- `fmp_series.merge_series(primary: dict, secondary: dict) -> dict`: union of items; for shared items primary rows plus secondary rows whose `(fy, fq)` the primary lacks, sorted by `end, fq`; `attrs` from the secondary (e.g. `tag_ambiguous`) kept only when the item comes wholly from the secondary.
- `metrics.py`: `formula = r.tag if r.tag.startswith("fmp:") else f"xbrl:{r.tag}"`.
- `context.select_metrics`: `m.formula_id.startswith(("xbrl:", "fmp:"))`.

- [ ] **Step 1: Failing tests:** `test_frames_match_columns_and_labels` (every frame has `periods.COLUMNS`; AAPL income annual → `FY2025` revenue 416.161B, `Q3FY2026` revenue 109.42B, fq labels from FMP's period), `test_signs_and_absolutes` (capex, dividends_paid, buybacks, acquisitions positive), `test_zero_is_missing_and_edgar_fills` (FMP interest_expense 0 in FY2025 → no FMP row; `merge_series` with an EDGAR frame holding FY2025 keeps EDGAR's), `test_accession_matching` (a filings frame with a 10-K filed 2025-10-31 → FY2025 and Q4FY2025 rows carry its accession; a quarter with no match gets `fmp:2026-07-31`), `test_fmp_quarters_support_ttm` (`periods.ttm(frame, date(2026, 7, 1))` on the revenue frame equals the sum of the last four quarters), `test_merge_keeps_older_edgar_history`, `test_split_adjusted_flags` (eps_diluted and diluted_shares rows flagged, revenue not), `test_formula_names_source` (`compute_fundamental_metrics` on a merged series → `revenue@FY2025` formula `fmp:income:revenue`, an EDGAR-only item `xbrl:…`), `test_select_metrics_shows_fmp_base_items`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS; `uv run pytest -q` → PASS. **Step 5:** Commit `feat: FMP statement series with EDGAR fallback; formulas name their source`.

### Task 3: prepare(), cross-check, page and drawer

**Files:** modify `fundabot/orchestrate/pipeline.py` (`prepare`), `fundabot/report/verify.py` (`crosscheck_edgar`, `format_edgar_check`), `fundabot/cli.py` (`verify`), `fundabot/web/views.py` (`fmp_sections` crosscheck block, `evidence` for metrics), `fundabot/web/templates/{ticker,_evidence}.html.j2`, `tests/test_pipeline.py` (fixture stubs `fmp_extra.fetch_statements` with the fixtures), `tests/test_verify_fmp.py`, `tests/test_web.py`.

**Interfaces:**
- `prepare()`: after EDGAR facts and filings: `ingest_statements` (try/except → warning); `fmp_s = build_fmp_series(load_statements(conn, cik), filings)`; `edgar_s = periods.build_series(facts, tag_map)`; `series = merge_series(fmp_s, edgar_s) if fmp_s else edgar_s`; everything downstream unchanged; store `ticker_facts('edgar_crosscheck')` from `crosscheck_edgar(fmp_s, edgar_s)`.
- `verify.crosscheck_edgar(fmp_series, edgar_series, tolerance=0.02, fys=5, quarters=8) -> list[dict]` rows `{"item", "period", "fmp", "edgar", "diff", "ok"}` over shared (item, period) within the last 5 fiscal years and 8 quarters; `format_edgar_check(rows) -> str`.
- `views.evidence` for `metric:` ids returns `{"kind": "metric", "id", "text", "source": formula_id, "accession", "url"}` with `url` the EDGAR filing index when the accession is a real one (`filings` row) else None, and `filed` text for `fmp:<date>` pseudo-accessions.
- Page Cross-check section gains "FMP vs EDGAR: N of M statement lines within 2 %" and the mismatch list.

- [ ] **Step 1: Failing tests:** `test_prepare_uses_fmp_primary` (fixture_prepare now stubs statements; metrics `revenue@FY2025` formula `fmp:income:revenue` and value 416.161B; `interest_expense@FY2024` comes from EDGAR), `test_prepare_without_fmp_statements_is_edgar_only` (stub returns {} → all formulas `xbrl:`), `test_crosscheck_edgar_rows_and_tolerance` (a 1 % gap ok, 5 % not; items without a shared period absent), `test_evidence_metric_links_filing` (web), `test_page_shows_edgar_crosscheck` (web).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS (fix any test that asserted `xbrl:` on FMP-carried items by asserting the source instead). **Step 5:** Commit `feat: FMP-primary prepare(); FMP vs EDGAR cross-check; source in the evidence drawer`.

### Task 4: Live run, hand-check, docs

- [ ] **Step 1:** `fundabot ingest AAPL --force` (no model calls); `fundabot metrics AAPL` saved beside the pre-change table (`/tmp/aapl_metrics.txt` from this session or `docs/handcheck-2026-10.md`); `fundabot verify AAPL`.
- [ ] **Step 2:** Explain every changed cell in `docs/handcheck-2026-10.md` (expected: short-term debt / net debt / EV / ROIC from FMP's commercial-paper inclusion; intangibles; anything else is a finding to fix before commit).
- [ ] **Step 3:** README "Accuracy notes": source order, what EDGAR still provides, the three cross-checks. Commit `docs: FMP-primary hand-check and accuracy notes`.
