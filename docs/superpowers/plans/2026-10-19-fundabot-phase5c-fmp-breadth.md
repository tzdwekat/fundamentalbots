# Phase 5c FMP Breadth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the rest of the decision-relevant FMP data into the snapshot, the agents' metrics table and the web page: profile, peers and peer-relative valuation, revenue segments, dividends, volume, a TTM cross-check, insider trades and holders, scores and ratings, the 10-year yield, and batch quotes for the overview.

**Architecture:** One new ingest module (`fundabot/ingest/fmp_extra.py`) fetches and stores each dataset with its own refresh rule; one new analytics module (`fundabot/analytics/fmp_metrics.py`) turns stored rows into `vendor_fmp` metrics computed in code; `prepare()` calls both; the cross-check lives in `report/verify.py`; the page gains sections. Migration 009.

**Tech Stack:** existing `fmp.get`, psycopg, pandas; fixtures under `tests/fixtures/fmp_aapl_*.json` captured 2026-10-19.

**Spec:** docs/superpowers/specs/2026-10-19-fundabot-phase5c-fmp-breadth-design.md

## Global Constraints

- Every new number is copied from FMP (`flags=["vendor_fmp"]`, formula id names the endpoint) or computed in code from copied values.
- Every dataset has `fetched_at` and a refresh rule: 24 h profile/peers/segments/dividends/scores/holders; 6 h insider trades/ratings; 15 min quotes. A failed refresh logs and keeps stored rows.
- Peer filter: `^[A-Z.]+$` with no two-letter `.XX` suffix, minus the company; at most 8.
- Segment slug: lowercase, non-alphanumerics → `_`, collapsed, ≤ 24 chars, numeric suffix on collision; ≤ 8 product and ≤ 6 geographic segments (largest in the latest year).
- Cross-check tolerance 10 %; a mismatch never changes our metric.
- `altman_z_distress` skipped for sector `Financial Services` or industry containing `REIT`.
- Judge CONSENSUS_DATA, narrative EARNINGS, report earnings lines and `earnings_available` select on `earnings.IDS` membership, not the `vendor_fmp` flag.
- `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot`; `fundabot init-db` after the migration.

## Review Focus

1. A company with no FMP coverage (empty profile, no peers, no segments): `prepare()` succeeds, metrics carry `null_reason="no_coverage"`, the page renders (Task 2 test `test_fmp_metrics_without_coverage`, Task 4 test `test_page_without_fmp_data`).
2. A segment name that changes spelling between years ("Service" vs "Services") must not create a second slug with a false 100 % growth: slugs are matched on the normalised name and a segment missing in the prior year gets `seg_*_yoy = null(no_prior)` (Task 2 test `test_segment_rename_no_false_growth`).
3. Peer list after filtering is empty: the medians are `null(no_peers)` and the outlook falls back to the five-year percentile only (Task 2 test `test_peer_medians_empty`).
4. The volume join must not invent rows: FMP dates absent from stored closes are ignored, and closes stay yfinance's (Task 3 test `test_volume_join_keeps_closes`).
5. The cross-check never writes to `metrics`; a 25 % gap on `pe` appears as a mismatch line only (Task 3 test `test_crosscheck_reports_but_never_alters`).

---

### Task 1: Ingest and migration

**Files:** create `fundabot/ingest/fmp_extra.py`, `fundabot/db/schema/009_fmp_breadth.sql`; modify `tests/test_db.py` (9 migrations); test `tests/test_fmp_extra.py` (fixtures `tests/fixtures/fmp_aapl_{profile,peers,seg_product,seg_geo,dividends,eod,key_metrics_ttm,ratios_ttm,insider_trades,holders,batch_quote,treasury,scores,grades_hist,peer_ratios_msft,peer_km_msft}.json`).

**Interfaces:**
- `009_fmp_breadth.sql`:
  ```sql
  CREATE TABLE company_profile (cik int PRIMARY KEY, name text, sector text, industry text, exchange text, description text, employees int, beta numeric, ipo_date date, fetched_at timestamptz DEFAULT now());
  CREATE TABLE peers (cik int NOT NULL, peer text NOT NULL, name text, market_cap numeric, price numeric, year_high numeric, year_low numeric, pe_ttm numeric, ev_sales_ttm numeric, ev_ebitda_ttm numeric, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, peer));
  CREATE TABLE segments (cik int NOT NULL, kind text NOT NULL, fiscal_year int NOT NULL, name text NOT NULL, revenue numeric, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, kind, fiscal_year, name));
  CREATE TABLE dividends (cik int NOT NULL, ex_date date NOT NULL, payment_date date, declaration_date date, amount numeric, adj_amount numeric, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, ex_date));
  CREATE TABLE insider_trades (cik int NOT NULL, filing_date date NOT NULL, transaction_date date, reporting_name text NOT NULL, role text, transaction_type text, acquisition text, shares numeric, price numeric, url text, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, filing_date, reporting_name, transaction_type, shares));
  CREATE TABLE holders (cik int NOT NULL, quarter_end date NOT NULL, investor text NOT NULL, shares numeric, market_value numeric, change_pct numeric, ownership_pct numeric, fetched_at timestamptz DEFAULT now(), PRIMARY KEY (cik, quarter_end, investor));
  ALTER TABLE prices ADD COLUMN volume bigint;
  ```
- `fmp_extra.fetch_profile(ticker, get=fmp.get) -> dict | None`; `ingest_profile(conn, cik, ticker, get=, max_age_hours=24) -> bool`; `load_profile(conn, cik) -> dict | None`.
- `fmp_extra.us_peers(rows: list[dict], own: str) -> list[str]` (the filter); `ingest_peers(conn, cik, ticker, get=, max_age_hours=24) -> int` (peers list → batch-quote for price/mktcap/range, per peer `ratios-ttm` (`priceToEarningsRatioTTM`) and `key-metrics-ttm` (`evToSalesTTM`, `evToEBITDATTM`)); `load_peers(conn, cik) -> list[dict]`.
- `fmp_extra.ingest_segments(conn, cik, ticker, get=, max_age_hours=24) -> int` (both kinds, annual; rows `{fiscalYear, data: {name: revenue}}`); `load_segments(conn, cik) -> list[dict]`.
- `fmp_extra.ingest_dividends(conn, cik, ticker, get=, max_age_hours=24) -> int`; `load_dividends(conn, cik) -> list[dict]` (ex_date desc).
- `fmp_extra.ingest_volume(conn, cik, ticker, get=, days=400) -> int`: `historical-price-eod/full` from `today-days` to today; `UPDATE prices SET volume = %s WHERE cik = %s AND date = %s` only for dates that exist; returns rows updated.
- `fmp_extra.ingest_insider_trades(conn, cik, ticker, get=, max_age_hours=6, limit=20) -> int`; `load_insider_trades(conn, cik, limit=20)`.
- `fmp_extra.ingest_holders(conn, cik, ticker, get=, max_age_hours=24, today=None) -> int` (last completed quarter via `fmp._last_two_quarters`, first that returns rows; top 10 by market value); `load_holders(conn, cik)`.
- `fmp_extra.fetch_ttm(ticker, get=) -> dict` (`ratios-ttm` + `key-metrics-ttm` merged), `fetch_scores(ticker, get=) -> dict | None`, `fetch_ratings(ticker, get=) -> dict | None` (`grades-historical` latest row → `{date, strong_buy, buy, hold, sell, strong_sell}`), `fetch_treasury_10y(get=) -> tuple[date, float] | None`, `batch_quotes(tickers, get=) -> dict[str, dict]`; each cached through `ticker_facts` by `fmp_extra.fact(conn, cik, key, loader, max_age_hours)` (`cik=0` for treasury/quotes).

- [ ] **Step 1: Failing tests** (fixture-backed `get` fake; db): `test_profile_roundtrip`, `test_us_peers_filter` (`['SONY', 'PCRFF', 'SSNLF', 'XIACF']` from the AAPL fixture; `066570.KS` and `002241.SZ` gone; own symbol removed), `test_ingest_peers_stores_multiples` (MSFT fixtures used for every peer via the fake), `test_segments_both_kinds_latest_years`, `test_dividends_desc`, `test_volume_join_keeps_closes` (seed closes for 3 of the EOD dates → 3 updated, closes unchanged, no new rows), `test_insider_trades_and_holders`, `test_fact_cache_respects_age`, `test_batch_quotes_shape`.
- [ ] **Step 2:** FAIL. **Step 3:** implement; `fundabot init-db`. **Step 4:** `uv run pytest tests/test_fmp_extra.py tests/test_db.py -q` → PASS. **Step 5:** Commit `feat: FMP breadth ingest (profile, peers, segments, dividends, volume, insiders, holders, facts)`.

### Task 2: Metrics, red flag, prompt selections, prepare()

**Files:** create `fundabot/analytics/fmp_metrics.py`; modify `fundabot/report/table.py` (GROUPS: `segments (FMP)` built dynamically from metric ids, `scores (FMP)`; valuation group gains `pe_peer_median`, `pe_vs_peer_median`, `ev_sales_peer_median`, `ev_sales_vs_peer_median`; capital allocation gains `dividend_per_share_ttm`, `dividend_yield`, `dividend_growth_5y`), `fundabot/analytics/red_flags.py` (`numeric_flags(metrics, sector=None, industry=None)`), `fundabot/analytics/technicals.py` (`outlook` valuation fallback), `fundabot/orchestrate/pipeline.py` (`prepare` calls the ingests and `compute_fmp_metrics`), `fundabot/agents/context.py` (SNAPSHOT gains `company:` line), `fundabot/debate/judge.py`, `fundabot/analytics/moves.py`, `fundabot/report/render.py`, `pipeline.analyze_full` (`earnings.IDS` selection); tests `tests/test_fmp_metrics.py`, extend `tests/test_red_flags.py` (or wherever `numeric_flags` is tested), `tests/test_technicals.py`, `tests/test_context_pruning.py`.

**Interfaces:**
- `fmp_metrics.compute_fmp_metrics(peers: list[dict], segments: list[dict], dividends: list[dict], scores: dict | None, treasury: tuple | None, price: float | None, own: dict) -> list[Metric]` where `own = {"pe": value, "ev_sales": value}` from valuation. Ids: `pe_peer_median@current`, `ev_sales_peer_median@current`, `ev_ebitda_peer_median@current`, `pe_vs_peer_median@current`, `ev_sales_vs_peer_median@current` (formula `peers:median`, inputs list the peers used); `seg_<slug>_revenue@FY{y}`, `seg_<slug>_share@FY{y}`, `seg_<slug>_yoy@FY{y}` (formula `fmp:revenue-product-segmentation` / `...geographic...`, inputs `{"name": original}`); `dividend_per_share_ttm@current`, `dividend_yield@current`, `dividend_growth_5y@current` (formula `fmp:dividends`); `altman_z@current`, `piotroski@current` (`fmp:financial-scores`); `risk_free_10y@current` (`fmp:treasury-rates`, inputs `{"date"}`). Missing data → `value=None, null_reason="no_coverage"` for the fixed ids (segments produce no rows when absent). `fmp_metrics.slug(name) -> str`.
- `red_flags.numeric_flags(metrics, sector: str | None = None, industry: str | None = None)`: adds `RedFlag("altman_z_distress", "high", ["metric:altman_z@current"], "Altman Z 1.42 < 1.8")` unless skipped.
- `technicals.outlook`: valuation state from `pe_5y_percentile` → else `pe_vs_peer_median` → else `ev_sales_vs_peer_median` (1.25 / 0.8); components show all three.
- `context.shared_prefix` SNAPSHOT: `company: <name> · <sector> · <industry> · <employees> employees\nabout: <description[:200]>` when the profile exists (`snapshot` dict gains `profile`); `snapshot.load` unchanged, `prepare` stores nothing new on the snapshot row — `run_grid` loads the profile via `fmp_extra.load_profile`.
- `prepare()`: after `fmp.ingest`, call `ingest_profile, ingest_peers, ingest_segments, ingest_dividends, ingest_volume, ingest_insider_trades, ingest_holders` each in its own try/except (log, continue); `fmp_m = compute_fmp_metrics(...)`; `snapshot.store_metrics(conn, sid, fundamentals + val + earn + guid + fmp_m)`; `flags = numeric_flags(fundamentals + fmp_m, sector, industry)`.

- [ ] **Step 1: Failing tests:** `test_peer_medians_and_relative` (3 peers → medians, `pe_vs_peer_median == own/median`), `test_peer_medians_empty` (`null(no_peers)`), `test_segments_share_and_yoy` (AAPL fixture FY2025/2024: iPhone share ≈ 0.503, yoy computed), `test_segment_rename_no_false_growth`, `test_segment_caps_8_and_6`, `test_dividends_ttm_yield_growth` (AAPL fixture: 4 quarters sum 1.06 → yield 1.06/price; 5y CAGR), `test_scores_and_treasury`, `test_fmp_metrics_without_coverage`, `test_altman_flag_and_financials_skip`, `test_outlook_peer_fallback`, `test_snapshot_block_has_company_line`, `test_judge_and_narrative_blocks_use_earnings_ids_only` (a `seg_*` metric with `vendor_fmp` does not appear in `judge.build_message`'s CONSENSUS_DATA nor `moves.build_message`'s EARNINGS).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS (the pruning fixture test must still hold). **Step 5:** Commit `feat: FMP metrics — peers, segments, dividends, scores, risk-free; Altman flag; company line`.

### Task 3: Volume in moves, TTM cross-check

**Files:** modify `fundabot/analytics/technicals.py` (`largest_moves` adds `volume`, `rel_volume`), `fundabot/analytics/moves.py` (move lines say `on 2.3x average volume` when known), `fundabot/report/verify.py` (`crosscheck_fmp(metrics, ttm: dict) -> list[dict]` and the CLI table), `fundabot/cli.py` (`verify` prints both; `prepare` stores `ticker_facts('fmp_crosscheck')`), `fundabot/ingest/prices.py` (`load` returns `volume`); tests `tests/test_technicals.py`, `tests/test_moves.py`, `tests/test_verify_fmp.py`.

**Interfaces:**
- `prices.load(conn, cik) -> DataFrame[date, close, volume]`.
- `technicals.largest_moves(...)` rows gain `"volume": int | None, "rel_volume": float | None` (volume ÷ mean of the previous 20 volumes when all present).
- `verify.crosscheck_fmp(metrics: list[Metric], ttm: dict, tolerance=0.10) -> list[dict]` rows `{"metric", "ours", "fmp", "diff", "ok"}` for: `pe`, `ev_sales`, `ev_ebitda`, `fcf_yield` (our `@current`), `gross_margin_ttm`, `operating_margin_ttm`, `net_margin_ttm`, `sbc_to_revenue_ttm` (computed from the last four quarterly metrics in code), `net_debt_to_ebitda_ttm`, `current_ratio` (latest quarter). FMP keys: `priceToEarningsRatioTTM, evToSalesTTM, evToEBITDATTM, freeCashFlowYieldTTM, grossProfitMarginTTM, operatingProfitMarginTTM, netProfitMarginTTM, stockBasedCompensationToRevenueTTM, netDebtToEBITDATTM, currentRatioTTM`.
- `ticker_facts('fmp_crosscheck')` value `{"checked": at, "rows": [...], "ok": n, "total": m}`.

- [ ] **Step 1: Failing tests:** `test_largest_moves_relative_volume`, `test_move_lines_mention_volume`, `test_crosscheck_reports_but_never_alters` (metrics unchanged after the call; a 25 % `pe` gap → `ok False`), `test_crosscheck_ttm_margins_from_quarters`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS, full suite. **Step 5:** Commit `feat: relative volume on moves; FMP TTM cross-check`.

### Task 4: Page sections and overview quotes

**Files:** modify `fundabot/web/views.py`, `fundabot/web/app.py`, templates (`ticker.html.j2`, `overview.html.j2`, new `_peers.html.j2`, `_segments.html.j2`, `_ownership.html.j2`); tests `tests/test_web.py`.

**Interfaces:**
- `views.ticker_page` adds `profile`, `peers` (rows with the company's own row first, from our metrics), `segments` (latest two years per kind with share and yoy from metrics), `dividend_lines`, `scores` (altman, piotroski, ratings counts), `insider_trades`, `holders`, `crosscheck` (from `ticker_facts`), `risk_free`. Refreshes on load follow the dataset rules (24 h / 6 h) through the same wrapped helpers as news.
- `views.overview` adds `quote` per row from `batch_quotes` (15-min cache): live price, day change %, 52-week position.
- Page: header line `Apple Inc. · Technology · Consumer Electronics · 166,000 employees`; sections Peers, Revenue segments, Ownership (insider trades, top holders, 13F/insider summary lines), Scores and ratings, Cross-check (`N of M within 10 %`, mismatches listed); 10-year yield next to the DCF lines in the outlook block.

- [ ] **Step 1: Failing tests:** `test_page_sections_fmp` (seed profile/peers/segments/dividends/holders/insider rows + facts → all sections present with the right numbers), `test_page_without_fmp_data` (nothing seeded → 200, sections say "no data"), `test_overview_quotes` (batch_quotes patched → day change shown).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS, full suite. **Step 5:** Commit `feat: page sections for peers, segments, ownership, scores, cross-check; live quotes on the overview`.

### Task 5: Live run and docs

- [ ] **Step 1:** `fundabot analyze AAPL` (cheap, default profile) so `prepare()` runs the new ingests; `fundabot verify AAPL`; load `/t/AAPL` and `/`; read the cross-check and peers output for sanity (Apple's peers after filtering; segment shares sum to 1).
- [ ] **Step 2:** README: the FMP section lists every dataset in use, the refresh rules, the cross-check, and the answer on bulk/batch endpoints (API-call count, not model cost). Commit `docs: FMP breadth`.
