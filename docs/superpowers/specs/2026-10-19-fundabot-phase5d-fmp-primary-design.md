# Fundabot Phase 5d: FMP statements as the primary source, EDGAR as the fallback

> **Superseded on 2026-10-19 during execution.** Tariq: "use the more reliable source wherever available." The merge is
> per value, not FMP-primary: only one source has it → that one; EDGAR's row is derived → FMP's reported figure; both
> reported and within 2 % → the filed EDGAR value, confirmed; both reported and apart → the filed value, flagged
> `source_conflict` with FMP's figure attached. Goal 3 below describes the original FMP-primary merge; the code,
> tests, README and hand-check describe the per-value rule.

2026-10-19. Tariq: "FMP is trustworthy as well so use that as the primary and EDGAR as the secondary." Until now the
fundamentals came from EDGAR XBRL company facts (tag map, YTD derivation, restatement and split handling) and FMP was
a cross-check. This phase inverts that: FMP's standardised income, balance-sheet and cash-flow statements build the
series the metrics are computed from; EDGAR fills what FMP lacks and provides the cross-check.

## Intent

Same metrics, same formulas, same grounding and citations; only the base rows change source. A reader must still be
able to open the filing behind any number, and the system must still notice when its two sources disagree.

## Goals

1. **Statements ingest.** `income-statement`, `balance-sheet-statement`, `cash-flow-statement`, annual (limit 12) and
   quarterly (limit 24), stored raw in `fmp_statements(cik, statement, period_type, fiscal_year, period, date,
   filing_date, data jsonb, fetched_at)`, refreshed every 24 hours (or at `prepare(force=True)`). Fixtures captured
   2026-10-19 under `tests/fixtures/fmp_aapl_{income,balance,cashflow}_{annual,quarter}.json`.
2. **FMP series.** `fundabot/analytics/fmp_series.py: build_fmp_series(statements, filings) -> dict[str, DataFrame]`
   produces the same frame per item as `periods.build_series` (columns `period, fy, fq, start, end, value, accession,
   tag, restated, derived, split_adjusted`) for every tag-map item FMP carries:
   revenue, cost_of_revenue, gross_profit, operating_income, pretax_income, income_tax, net_income, eps_diluted (income);
   ocf, capex, da, sbc, dividends_paid, buybacks, share_issuance, acquisitions (cash flow; FMP signs outflows negative,
   our items are positive magnitudes, so capex, dividends, buybacks and acquisitions are stored as absolute values);
   interest_expense (income); total_assets, total_liabilities, equity (totalStockholdersEquity), cash
   (cashAndCashEquivalents), short_term_debt, long_term_debt, total_debt_reported (totalDebt), goodwill, intangibles
   (intangibleAssets), current_assets, current_liabilities (balance); diluted_shares (weightedAverageShsOutDil).
   `depreciation` and `amortization` are not carried (FMP has only the combined line, which maps to `da`).
   Period labels come from FMP's own `fiscalYear` and `period` (`FY`, `Q1`..`Q4`), which match the company's fiscal
   labels (verified on HD and NKE); no YTD derivation, no Q4 derivation. `start` is the previous period's end plus one
   day when stored, else `end` minus 91 days (quarter) or 364 days (year). `tag` is `fmp:<statement>:<field>`;
   `accession` is the EDGAR accession whose `filed` is within 3 days of FMP's `filingDate` and whose form is 10-K
   (annual, and FMP's Q4 rows, which carry the 10-K filing date) or 10-Q (quarterly), else `fmp:<filingDate>`;
   `restated=False`; `split_adjusted=True` for eps_diluted and diluted_shares (FMP adjusts), False otherwise.
   **An FMP zero is a missing line, not a value**: FMP emits 0 for lines a filer does not report (AAPL's
   interestExpense, goodwill, intangibleAssets, acquisitionsNet are all 0), so a 0 is dropped from the FMP frame for
   every item; the merge then takes EDGAR's value for that period when it has one, and otherwise the metric stays
   `null(tag_missing)` as today. The README's promise "null with a reason, never an invented zero" stands.
3. **Merge.** `fmp_series.merge_series(primary: dict, secondary: dict) -> dict`: per item, the primary frame; periods
   (fy, fq) the primary lacks are appended from the secondary frame (older history beyond FMP's window, Q4s FMP omits,
   items FMP does not carry at all, zeros dropped by the rule above); appended rows keep their EDGAR tag so the formula
   id still says `xbrl:…`. Every metric's formula therefore names its source: `fmp:income:revenue` or `xbrl:Revenues`.
   To make that true, `compute_fundamental_metrics` writes the formula as the row's tag when the tag already carries a
   source prefix (`fmp:`), else `xbrl:<tag>` as today, and `select_metrics` treats `fmp:` base items like `xbrl:` ones
   (fiscal years plus the newest quarter). These are the only two lines that change outside the new module.
4. **`prepare()`.** Fetches statements, builds the FMP series, builds the EDGAR series as today, merges with FMP primary;
   when FMP returns no statements the EDGAR series stands alone and a warning is logged. EDGAR company facts are still
   ingested (cover-page shares, the fallback series, the cross-check, the filings and filing text). Split adjustment
   applies to the EDGAR facts only. `_prefer_larger_revenue` applies to the EDGAR path only.
5. **Cross-check, now FMP vs EDGAR.** `report/verify.crosscheck_edgar(fmp_series, edgar_series, tolerance=0.02) ->
   list[dict]` compares every shared (item, period) over the last five fiscal years and eight quarters; `fundabot
   verify` prints it; `prepare()` stores `ticker_facts('edgar_crosscheck')` (`{ok, total, rows}`); the page's
   Cross-check section shows "FMP vs EDGAR: N of M statement lines within 2 %" with the mismatches listed, and keeps
   the yfinance and FMP-TTM tables in `verify`. A mismatch never changes a number.
6. **Citations unchanged.** `metric:<id>@<period>` ids are the same. The evidence drawer for a `metric:` id adds the
   source (formula id) and, when the metric's `inputs["accession"]` is a real EDGAR accession, a link to that filing;
   an `fmp:<filingDate>` pseudo-accession shows "filed <date> (FMP)" without a link.
7. **Docs.** README "Accuracy notes" states the new source order, what EDGAR still provides, and the cross-check; the
   hand-check document gets a before/after of the AAPL table (differences explained or fixed).

## Non-goals

- Filers with no usable EDGAR 10-K facts (a new CIK such as XOM's): EDGAR history is still required for the fiscal
  calendar, cover-page shares and the fallback series; running on FMP alone is a later phase.

- FMP "as reported" statements (their `data` blobs mirror XBRL and would re-create the tag-mapping problem).
- Replacing the DEI cover-page share count, the filings list, filing text, transcripts or prices.
- Changing any metric formula, the pruned table, grounding tolerances, or the report layout.

## Anti-goals

- A metric whose source cannot be told from its formula id.
- A value that silently changes between FMP and EDGAR without the cross-check reporting it.
- Losing history: a company with 10 years on EDGAR and 8 on FMP must still show the older fiscal years (from EDGAR).

## Constraints

- No change to metric formulas, `compute_valuation`, `red_flags` or `grounding`; `compute_fundamental_metrics` and
  `select_metrics` change only in the two source-prefix lines named in goal 3.
- `periods.COLUMNS` is the contract; `fmp_series` frames are validated against it in a test.
- Migration `010_fmp_statements.sql` for the raw statements table.
- Existing pipeline tests stub `fmp_extra.fetch_statements` with the fixtures so they run on the FMP path; one test
  stubs it to empty to prove the EDGAR-only fallback.

## Decided

- Sign convention: our positive-magnitude items; FMP's negative outflows are negated.
- `da` from the income statement's `depreciationAndAmortization` (falls back to the cash-flow line when the income
  one is null).
- Window: 12 annual rows, 24 quarterly rows per fetch.
- Tolerance 2 % for the FMP-vs-EDGAR statement check (these are the same filed numbers; a gap beyond rounding is a
  definitional difference worth a look).

## Known consequences to document in the hand-check

- FMP's `shortTermDebt` includes commercial paper (AAPL 20.3B) where EDGAR's `LongTermDebtCurrent` (12.4B) did not;
  net debt, EV and ROIC move by that amount and the cross-check will show it permanently. That is a definitional
  difference, documented, not an error.
- Items EDGAR stopped tagging (AAPL intangibles after 2017) have no shared period, so the cross-check cannot catch a
  bad FMP value there.

## Left to the builder

- Exact structure of the `start` derivation when FMP's quarterly rows have gaps.
- Which page element lists the mismatches (the existing Cross-check section is fine).
