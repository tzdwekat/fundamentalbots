# Fundabot Phase 5c: the rest of the important FMP data

2026-10-19. Tariq pasted FMP Ultimate's endpoint catalogue: "we spent a good amount of money on FMP, maximize its use"
and "add in all the rest of the important FMP info". Phase 5b added news, press releases, analyst grades and the next
earnings date. This phase adds what else changes a buy/sell decision or makes our own numbers more trustworthy, and
nothing that merely decorates.

## Intent

Every addition must do one of three things: give the agents and the page a fact they could not derive from EDGAR
(segments, peers, dividends, insiders, holders, scores, volume), cross-check a number we compute (TTM ratios), or make the
overview faster for many tickers (batch quotes). All numbers stay computed in code; FMP values are stored with their
source and shown as `vendor_fmp`.

## Goals

1. **Company profile.** `profile` → `company_profile(cik, name, sector, industry, exchange, description, employees,
   beta, ipo_date, fetched_at)`, refreshed every 24 hours at `prepare()` and on page load. Shown in the ticker page
   header (sector · industry · employees) and as one line in the personas' SNAPSHOT block (sector, industry, a
   200-character description) so a persona knows what the company is before reading numbers.
2. **Peers and relative valuation.** `stock-peers` filtered to US-listed symbols (no exchange suffix), capped at 8;
   for the set, one `batch-quote` call (price, market cap, 52-week range) and one `ratios-ttm` call per peer (trailing
   P/E, P/S, EV/EBITDA), cached 24 hours in `peers(cik, peer, name, market_cap, pe_ttm, ps_ttm, ev_ebitda_ttm, price,
   year_high, year_low, fetched_at)`. Code computes `pe_peer_median@current`, `pe_vs_peer_median@current`,
   `ev_sales_peer_median@current` as metrics (`vendor_fmp`, formula `peers:median`) in the valuation group; the
   outlook's valuation component reports both the five-year percentile and the peer-relative multiples, and when the
   five-year percentile is `null` (young listings) falls back to `pe_vs_peer_median`, then `ev_sales_vs_peer_median`
   (above 1.25 = stretched, below 0.8 = cheap, else fair). Peer multiples come from each peer's `ratios-ttm` (P/E) and
   `key-metrics-ttm` (EV/sales, EV/EBITDA) so our EV-based ratios compare with EV-based peer ratios; the numerator of
   the relative multiples is our own `pe@current` / `ev_sales@current` (the number shown everywhere; the TTM cross-check
   in goal 6 is where the definitional gap to FMP's own figure is exposed). Page: a peers table.
3. **Revenue segmentation.** `revenue-product-segmentation` and `revenue-geographic-segmentation` (annual) →
   `segments(cik, kind, fiscal_year, name, revenue)`. Metrics per segment and fiscal year, slugged ids:
   `seg_<slug>_revenue`, `seg_<slug>_share` (share of the segments' total), `seg_<slug>_yoy`; at most 8 product and 6
   geographic segments (the largest in the latest year); a new report group `segments (FMP)` lists them at the fiscal
   year columns. The persona prompt shows them through the metrics table as any other metric; grounding resolves them
   as any other metric id. Page: a segment table for the latest two years with share and growth.
4. **Dividends.** `dividends` → `dividends(cik, ex_date, payment_date, amount, adj_amount)`; metrics
   `dividend_per_share_ttm@current`, `dividend_yield@current` (TTM amount ÷ price), `dividend_growth_5y@current`
   (CAGR of annual sums when five full years exist), `next_ex_date` as a ticker fact when a declared date is ahead.
   Shown in the capital-allocation group and on the page.
5. **Volume.** `historical-price-eod/full` → `prices.volume` (new column, filled by date for the stored closes; closes
   stay from yfinance, which is already split-adjusted and verified). `largest_moves` gains `volume` and `rel_volume`
   (volume ÷ trailing 20-day average); the narrative inputs say "on 2.3x average volume"; the page's moves table shows
   it. The chart does not change.
6. **TTM cross-check.** `key-metrics-ttm` and `ratios-ttm` → `fundabot verify T` prints a second table comparing
   like with like: our `@current` values for pe, ev_sales, ev_ebitda, fcf_yield; TTM margins (gross, operating, net)
   and sbc_to_revenue computed in code from the last four quarters for the comparison only (not stored as metrics);
   net_debt_to_ebitda as latest net debt over TTM EBITDA; current_ratio from the latest quarter. roe / roa / roic are
   not compared (FMP averages balances; we do not). A difference over 10 % is a mismatch. The
   result is stored in `ticker_facts('fmp_crosscheck')` at `prepare()` and the page shows "cross-check: N of M within
   10 %" with the mismatches listed; a mismatch never changes our number, it tells the reader to look.
7. **Insiders and holders.** `insider-trading/search` (latest 20 transactions: date, name, role, type, shares, price,
   link) → `insider_trades`; `institutional-ownership/extract-analytics/holder` for the last completed quarter (top 10
   by market value: name, shares, change %, ownership %) → `holders`. Both on the page under Ownership; the existing
   13F and insider summary metrics stay.
8. **Scores and ratings.** `financial-scores` → metrics `altman_z@current`, `piotroski@current` (`vendor_fmp`, new
   group `scores (FMP)`); `grades-historical` latest row → ratings counts (strong buy / buy / hold / sell / strong sell)
   stored in `ticker_facts('ratings')` and shown next to the price-target lines. The red-flag rules gain
   `altman_z_distress` (Z < 1.8, severity high) with the metric as evidence, skipped when the profile's sector is
   `Financial Services` or the industry contains `REIT` (the Z-score is not defined for them; a high flag caps the
   composite through the veto, so a wrong default would cap every bank on the watchlist). `numeric_flags` therefore
   takes the sector and industry as inputs.
9. **Risk-free rate.** `treasury-rates` latest 10-year yield → `risk_free_10y@current` metric and a line next to the
   DCF block ("10y Treasury 5.09 %"); the DCF's discount-rate assumptions are unchanged (a change there is its own
   accuracy exercise).
10. **Overview quotes.** One `batch-quote` call per overview load (cached 15 minutes in `ticker_facts('quote')`) gives
    live price, day change and 52-week range for every watchlist row; stored closes remain the source for returns.

## Non-goals

- Replacing EDGAR company facts with FMP statements (EDGAR stays the source of truth for fundamentals).
- FMP's DCF, ratings "recommendations", senate/house trades, ESG, crowdfunding, commodities, forex, crypto, intraday.
- Bulk endpoints: they cut API calls, not model cost, and we make about fifteen FMP calls per ticker.

## Anti-goals

- A segment, peer or dividend number that is not either copied from FMP (flagged `vendor_fmp`) or computed in code
  from copied values.
- A cross-check mismatch that silently alters our metric.
- A peer set containing the company itself or non-US listings whose multiples are in another currency.

## Constraints

- All FMP reads go through `fmp.get`; every new table has `fetched_at` and a refresh rule (24 h for profile, peers,
  segments, dividends, scores, holders; 6 h for insider trades and ratings; 15 min for quotes); a failed refresh logs
  and leaves stored data in place.
- Migration `009_fmp_breadth.sql`: `company_profile`, `peers`, `segments`, `dividends`, `insider_trades`, `holders`,
  `ALTER TABLE prices ADD COLUMN volume bigint`.
- New metrics carry `flags=["vendor_fmp"]` and a formula id naming the endpoint; they enter the snapshot at
  `prepare()` through `compute_earnings_metrics`'s sibling `compute_fmp_metrics(...)` in `fundabot/analytics/fmp_metrics.py`.
- `select_metrics` needs no change: `seg_*` ids are "other derived ids" shown at fiscal years; scores, peers,
  dividends and the rate are `current`.
- The judge's CONSENSUS_DATA block, the narrative's EARNINGS block, the report's earnings lines and
  `earnings_available` currently select on the `vendor_fmp` flag; they switch to the earnings-signals ids
  (`earnings.IDS`) so the new metrics reach the personas through the metrics table and the page only, not every judge
  and narrative prompt (100-250 extra lines per call otherwise).

## Decided

- Peer filter: symbols matching `^[A-Z.]+$` with no `.` suffix of two letters (so `BRK.B` stays, `066570.KS` goes),
  minus the company itself.
- Segment slug: lowercase, non-alphanumerics to `_`, collapsed, 24 characters max; collisions get a numeric suffix.
- Cross-check tolerance 10 % (FMP's TTM windows and share counts differ from ours; 10 % separates definition from error).
- `fundabot verify` keeps the yfinance table and appends the FMP table.

## Left to the builder

- Exact column names of `insider_trades` and `holders`.
- How the page lays out peers, segments and ownership (tables are fine).
- Whether `prepare()` fetches everything in one FMP pass or lazily on the page (both must respect the refresh rules).
