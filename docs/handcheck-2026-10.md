# Hand check, October 2026 (Phase 1 exit)

`fundabot verify T` compares our XBRL-derived FY values for revenue, net income, total assets and operating cash flow
against yfinance's statements (an independent source) for the last 4 fiscal years. Tolerance 2%. Run on 2026-10-15.

| ticker | rows compared | max abs diff | null key metrics (latest FY / current) | note |
|---|---|---|---|---|
| AAPL | 16 | 0.00% | interest_coverage (tag_missing) | Apple stopped reporting interest expense separately after FY2023; null by design. |
| MSFT | 16 | 0.00% | - | D&A composed from separate lines (flagged). |
| NVDA | 16 | 0.00% | - |  |
| AMZN | 16 | 0.00% | dcf_base (nonpositive_denominator) | DCF null: trailing FCF is negative (capex exceeds operating cash flow). |
| GOOGL | 15 | 0.00% | - | multi-class: cover-page shares are dimensioned and absent from companyfacts, so shares = diluted weighted average (flagged `shares_diluted_weighted`); D&A composed from separate lines (flagged). |
| META | 16 | 0.00% | - | shares and D&A as for GOOGL. |
| TSLA | 16 | 0.55% | - | 0.55% on one restated line; flagged `restated`. Debt from the reported-total tag (flagged `debt_total_tag`). |
| COST | 16 | 0.00% | - | 12/12/12/16-week quarters; Q2-Q4 derived from year-to-date facts. |
| UNH | 16 | 0.00% | - |  |
| JPM | 16 | 4.79% | dcf_base (no_fcf_history), ev_ebitda (tag_missing), fcf_margin (tag_missing), fcf_yield (tag_missing), gross_margin (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), pe (tag_missing), roic (tag_missing) | revenue: ours is XBRL `Revenues` = reported total net revenue ($177.6B FY2024, matches the 10-K); yfinance uses a different construct. Not an error. Bank: no cost of revenue or operating income line, so those ratios are null by design. |
| CVX | 16 | 0.00% | ev_ebitda (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing), sbc_to_revenue (tag_missing) | no operating income line (oil majors report costs, not operating income); null by design. |
| PG | 16 | 0.00% | - |  |
| HD | 16 | 0.00% | - | fiscal-year labels follow Home Depot's convention (year ending Feb 2026 = FY2025); debt from lease-inclusive tags. |
| NFLX | 16 | 0.00% | - |  |
| AMD | 16 | 0.00% | ev_ebitda (tag_missing) |  |
| CRM | 16 | 0.00% | ev_ebitda (tag_missing) |  |
| PLTR | 16 | 0.00% | interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), roic (tag_missing) | shares as for GOOGL. |
| UBER | 16 | 0.00% | gross_margin (tag_missing) |  |
| DIS | 15 | 0.00% | dcf_base (no_fcf_history), gross_margin (tag_missing), interest_coverage (nonpositive_denominator) | DCF null: 3y and 5y FCF CAGR undefined (negative start). |
| NKE | 16 | 0.00% | ev_ebitda (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing) | no `OperatingIncomeLoss` tag in Nike's filings; operating margin and ROIC null by design. |

XOM was dropped: Exxon re-registered under a new CIK in 2026, so its companyfacts hold two quarters and no 10-K;
`fundabot ingest XOM` reports `InsufficientHistory`. CVX took its place.

Null key metrics are expected where the filer has no such line item. They are `null` with a reason, never zero.
