# Hand check, October 2026 (Phase 1 exit)

`fundabot verify T` compares our XBRL-derived FY values for revenue, net income, total assets and operating cash flow
against yfinance's statements (an independent source) for the last 4 fiscal years. Tolerance 2%.

| ticker | rows compared | max abs diff | null key metrics (latest FY / current) | note |
|---|---|---|---|---|
| AAPL | 16 | 0.00% | interest_coverage (tag_missing) |  |
| MSFT | 16 | 0.00% | ev_ebitda (tag_missing), net_debt_to_ebitda (tag_missing) |  |
| NVDA | 16 | 0.00% | dcf_base (no_fcf_history) |  |
| AMZN | 16 | 0.00% | dcf_base (nonpositive_denominator) |  |
| GOOGL | 15 | 0.00% | dcf_base (tag_missing), ev_ebitda (tag_missing), fcf_yield (tag_missing), net_debt_to_ebitda (tag_missing) | multi-class shares summed from the cover page. |
| META | 16 | 0.00% | dcf_base (tag_missing), ev_ebitda (tag_missing), fcf_yield (tag_missing) |  |
| TSLA | 16 | 0.55% | net_debt_to_ebitda (tag_missing) | 0.55% on one restated line; flagged `restated` in the metrics table. |
| COST | 16 | 0.00% | dcf_base (tag_missing), fcf_yield (tag_missing) |  |
| UNH | 16 | 0.00% | - |  |
| JPM | 16 | 4.79% | dcf_base (no_fcf_history), ev_ebitda (tag_missing), fcf_margin (tag_missing), fcf_yield (tag_missing), gross_margin (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing) | revenue: ours is XBRL `Revenues` = reported total net revenue ($177.6B FY2024, matches the 10-K); yfinance uses a different construct. Not an error. Bank: no cost of revenue, so gross margin is null by design. |
| CVX | 16 | 0.00% | dcf_base (tag_missing), ev_ebitda (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing), sbc_to_revenue (tag_missing) |  |
| PG | 16 | 0.00% | - |  |
| HD | 16 | 0.00% | dcf_base (tag_missing) |  |
| NFLX | 16 | 0.00% | - |  |
| AMD | 16 | 0.00% | net_debt_to_ebitda (tag_missing) |  |
| CRM | 16 | 0.00% | ev_ebitda (tag_missing) |  |
| PLTR | 16 | 0.00% | dcf_base (no_fcf_history), ev_ebitda (tag_missing), fcf_yield (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), roic (tag_missing) |  |
| UBER | 16 | 0.00% | dcf_base (no_fcf_history), gross_margin (tag_missing) |  |
| DIS | 15 | 0.00% | dcf_base (no_fcf_history), gross_margin (tag_missing), interest_coverage (nonpositive_denominator) |  |
| NKE | 16 | 0.00% | ev_ebitda (tag_missing), interest_coverage (tag_missing), net_debt_to_ebitda (tag_missing), operating_margin (tag_missing), roic (tag_missing) |  |

XOM was dropped: Exxon re-registered under a new CIK in 2026, so its companyfacts hold two quarters and no 10-K;
`fundabot ingest XOM` reports `InsufficientHistory`. CVX took its place.

Null key metrics are expected where the filer has no such line item (banks: cost of revenue; many filers: no separate
interest expense or goodwill tag). They are `null` with a reason, never zero.
