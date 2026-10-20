# Hand check, October 2026 (Phase 1 exit)

`fundabot verify T` compares our XBRL-derived FY values for revenue, net income, total assets, operating cash flow and
diluted EPS (split-adjusted) against yfinance's statements (an independent source) for the last 4 fiscal years. Tolerance 2%.
Run on 2026-10-15 after the review fix pass (stock-split adjustment, D&A composition, debt and share fallbacks).

| ticker | rows compared | max abs diff | null key metrics (latest FY / current) | note |
|---|---|---|---|---|
| AAPL | 20 | 0.00% | interest_coverage (tag_missing) | 4:1 split Aug 2020: pre-split EPS and share counts rescaled, flagged `split_adjusted`. Interest expense not reported separately after FY2023. |
| MSFT | 20 | 0.00% | - | D&A composed from separate lines (flagged). |
| NVDA | 20 | 2.30% | - | 10:1 split Jun 2024 and 4:1 Jul 2021: rescaled and flagged. EPS diff on FY2023 is yfinance rounding of 0.17. |
| AMZN | 20 | 0.00% | dcf_base (nonpositive_denominator) | DCF null: trailing FCF is negative (capex exceeds operating cash flow). 20:1 split Jun 2022 handled. |
| GOOGL | 19 | 0.00% | - | multi-class: cover-page shares are dimensioned and absent from companyfacts, so shares = diluted weighted average (flagged); D&A composed from separate lines (flagged). |
| META | 20 | 0.00% | - | shares and D&A as for GOOGL. |
| TSLA | 20 | 0.55% | - | small diffs on restated lines; flagged `restated`. Debt from the reported-total tag (flagged `debt_total_tag`). |
| COST | 20 | 0.00% | - | 12/12/12/16-week quarters; Q2-Q4 derived from year-to-date facts. |
| UNH | 20 | 0.00% | - |  |
| JPM | 20 | 4.79% | dcf_base (no_fcf_history), ev_ebitda (tag_missing), fcf_margin (tag_missing), fcf_yield (tag_missing), gross_margin (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), pe (tag_missing), roic (tag_missing) | revenue: ours is XBRL `Revenues` = reported total net revenue ($177.6B FY2024, matches the 10-K); yfinance uses a different construct. Not an error. Bank: no cost of revenue or operating income line, so those ratios are null by design. |
| CVX | 20 | 4.85% | ev_ebitda (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing), sbc_to_revenue (tag_missing) | revenue: both `Revenues` (total revenues and other income, incl. equity-affiliate income) and the ASC 606 sales tag are filed; the larger total wins and is flagged `tag_ambiguous`; yfinance uses the smaller. No operating income line (oil majors report costs); null by design. |
| PG | 20 | 0.00% | - |  |
| HD | 20 | 0.00% | - | fiscal-year labels follow Home Depot's convention (year ending Feb 2026 = FY2025); debt from lease-inclusive tags. |
| NFLX | 20 | 0.25% | - |  |
| AMD | 20 | 0.00% | ev_ebitda (tag_missing) |  |
| CRM | 20 | 0.00% | ev_ebitda (tag_missing) |  |
| PLTR | 20 | 0.15% | interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), roic (tag_missing) | shares as for GOOGL. |
| UBER | 20 | 0.00% | gross_margin (tag_missing) |  |
| DIS | 19 | 0.00% | dcf_base (no_fcf_history), gross_margin (tag_missing), interest_coverage (nonpositive_denominator) | DCF null: 3y and 5y FCF CAGR undefined (negative start). |
| NKE | 20 | 0.00% | ev_ebitda (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing) | no `OperatingIncomeLoss` tag in Nike's filings; operating margin and ROIC null by design. |

XOM was dropped: Exxon re-registered under a new CIK in 2026, so its companyfacts hold two quarters and no 10-K;
`fundabot ingest XOM` reports `InsufficientHistory`. CVX took its place.

Null key metrics are expected where the filer has no such line item. They are `null` with a reason, never zero.
Note: `verify` covers statement totals and EPS. Ratios, valuation ranges and the DCF are derived from those inputs plus
prices and share counts; their flags (`derived`, `split_adjusted`, `da_composed`, `debt_total_tag`, `shares_diluted_weighted`) mark
every value that rests on a fallback.

## 2026-10-19: FMP statements merged with EDGAR per value (Phase 5d)

Rule (code, `fundabot/analytics/fmp_series.merge_series`): only one source has the value → that one; EDGAR's row is
derived (Q4 or year-to-date subtraction) → FMP's reported quarter, confirmed by EDGAR when they agree; both reported
and within 2 % → the filed EDGAR value, confirmed by FMP; both reported and apart → the filed EDGAR value, flagged
`source_conflict` with FMP's figure in the metric's inputs. Every formula names its source (`xbrl:…` or `fmp:…`).

AAPL before/after on the shown table (5 fiscal years, 4 quarters, current): two cells changed, both for the better.

| cell | before | after | why |
|---|---|---|---|
| eps_diluted@Q4FY2025 | 1.84 (derived: FY minus nine months) | 1.85 (FMP, reported) | a per-share figure cannot be derived by subtraction when the share count moves; the reported quarter is right |
| revenue_cagr_5y@FY2021 | null (tag_missing) | 11.1 % | FMP's longer annual history supplies FY2016 revenue |

Base rows stored: 1,494; 183 from FMP (older history, reported Q4s, items EDGAR lacks), 1,311 from EDGAR; 812 of the
EDGAR rows confirmed by FMP within 2 %; 63 conflicts, all kept on the filed value and flagged:

- `short_term_debt` and `total_debt_reported`, every period: FMP's lines include commercial paper and lease liabilities
  (20.3B vs 12.4B at FY2025); EDGAR's `LongTermDebtCurrent` does not. Definitional, permanent, shown on the page.
- `intangibles` Q2/Q3 FY2026: FMP 21.3B / 20.3B vs filed 25.8B / 25.4B.
- `cash` Q2FY2026: FMP 36.3B vs filed 45.6B. A genuine FMP discrepancy; the filed value stands.
- `capex` FY2015–FY2017 and `da` FY2015: FMP's older figures differ from the filings by 2–20 %; outside the shown window.

`fundabot verify AAPL` prints the three checks: yfinance statements (FY values), FMP TTM ratios (10 of 10 within 4 %),
and FMP vs EDGAR statement lines (266 of 293 within 2 %, the 27 above).
