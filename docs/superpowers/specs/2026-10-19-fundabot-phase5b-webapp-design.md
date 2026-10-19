# Fundabot Phase 5b: the stock picture (web app)

2026-10-19. Tariq: "I want to be able to get the entire picture of the stock - what made it grow/drop in the last 1, 3,
or 6 months? what's the outlook on the stock (is it still growing? bottomed out? more to go?)? what recent news is
relevant to this stock? and whatever else I need to make a decision on buying or selling a stock." And: "the main thing
is running the numbers programmatically so we're not inferring calculations, and having a reasoning for every point of
view." Agreed in chat: a local web app, no login, FastAPI + Jinja + htmx, price chart with event markers, a cited move
narrative, an outlook block built from measured trend and fundamental momentum with no price targets, an FMP news list
(30-day default), a two-column bull/bear decision panel, and an overview page with Analyze buttons that show the cost.

## Intent

One page per ticker that answers the four questions above from data the pipeline already computes, plus two new inputs
(daily price statistics and news), with every number produced in code and every sentence of narrative citing an id the
reader can open. The app is the front door to what exists (reports, verdicts, review, queue, calibration); it does not
replace the CLI.

## Goals

1. **Price picture, in code.** From the stored daily closes (`prices`, refreshed on page load if older than one trading
   day): returns over 1, 3, 6 and 12 months, distance from the 52-week high and low, 50- and 200-day moving averages and
   the price's position against them, the 50-day average's 20-day slope, 20-day realized volatility, and the ten largest
   absolute daily moves in the selected window. Each daily move gets an id `price:<date>` and is matched in code to
   events within two trading days: filings (`filings`), earnings reports (`surprises`), transcripts, splits, and news
   (goal 2). These become the chart's markers and the narrative's evidence.
2. **News.** FMP stock news and press releases for the ticker (endpoints verified by the builder against the FMP
   docs; `fmp.get` already wraps the key and base URL), stored in a new `news` table keyed by FMP id with published
   time, publisher, title, url and text, fetched on page load when the newest stored item is older than six hours.
   Each item has id `news:<id>`. Default list window 30 days, selectable 7 / 30 / 90.
3. **What moved it (cited narrative).** For the selected window (1, 3 or 6 months) a cheap model (the profile's
   `guidance` role) writes at most six sentences, each citing one or more ids from: `price:<date>` moves, `news:<id>`,
   earnings ids from the metrics table (`metric:eps_surprise@…`), `chunk:` ids from filings and transcripts in the
   window, and `metric:` ids. The inputs are the stats table, the matched moves with their events, the news titles and
   first 300 characters, and the window's filing and transcript excerpts (retrieval on "what changed this quarter"
   queries, 6k tokens). Grounding, reused from `eval.grounding`: every id must resolve; every number must match the stats
   table, a metric, or appear in the cited news or chunk text at quoted precision; a sentence that fails is dropped and
   counted, and the page shows the count. Stored in `narratives(cik, window_days, inputs_sha, output, created_at)` and
   reused while the inputs hash is unchanged. Cost target: under $0.03 per window.
4. **Outlook block, in code.** Three measured components and a label, each with the numbers that produced it:
   - Fundamental momentum: revenue and EPS year-over-year for the last four quarters and their direction (accelerating /
     steady / decelerating by comparing the last two to the previous two), EPS surprise streak, and the change in the
     next-fiscal-year consensus estimate over the stored history (`estimates`), when present.
   - Valuation stretch: current P/E and EV/sales against their own five-year ranges (percentiles already computed).
   - Trend: the price picture's moving-average position, slope and drawdown from the 52-week high.
   - Label from a fixed rule table (in code, with the rule shown on hover): `growing / extended`, `growing / fair`,
     `decelerating`, `declining`, `bottoming` (price below the 200-day average, 50-day slope turned positive, drawdown
     > 20%), `recovering`, `insufficient data`. No price targets, no forecasts, no model involvement.
5. **Decision panel.** From the latest `full` verdict (or the latest cheap run's analyses when there is no verdict):
   composite score with the veto note, bull column (positive consensus claims, strongest first) and bear column
   (negative consensus claims and contested claims with their refutations), each claim with its evidence ids as links
   that open the metric cell or excerpt in a drawer; red flags; catalysts; the judge summary; a link to the full report.
   Every number on this panel is copied from stored structures, never recomputed by a model.
6. **Pages.** `/` overview: watchlist rows (last close, 1m / 3m / 6m returns, outlook label, latest composite and its
   date, queue status) with add / remove, and per row Analyze buttons (cheap, full) that show the estimated cost for
   the selected profile and start a run in the background; `/t/{ticker}` the page above; `/review` the contested-claim
   labelling the CLI does; `/queue` run queue, running jobs, month-to-date spend against the budget; `/calibration` the
   latest calibration statistics. Reports under `reports/` are served as static files and linked.
7. **Running analyses from the page.** `fundabot web` starts uvicorn on 127.0.0.1:8787. A run is an in-process
   background task (one at a time per ticker, three at a time overall) that calls the existing pipeline with the
   chosen profile; the page polls its status with htmx every five seconds and links the report when done. The
   existing budget check applies; a refusal is shown, not swallowed. Cost estimates per profile and run kind come from
   `profiles.yaml` (`estimate_usd: {cheap: …, full: …}` per profile, from the README's measured numbers).
8. **Charts.** One new SVG helper `charts.price_chart(closes, events, window)` in the existing style (validated
   palette, light and dark): the close line, the two moving averages, event markers by kind with a hover tooltip (date,
   close, event titles), and the ten largest moves highlighted. The existing line charts of the report are reused on the
   ticker page for revenue / FCF / margins.

## Non-goals

- Authentication, multi-user, hosting anywhere but localhost.
- Intraday or real-time quotes; options; portfolio tracking; alerts.
- Price targets, forecasts or a buy / sell recommendation beyond the composite score and the outlook label's rule.
- Editing personas, prompts or profiles from the UI.
- Mobile layout beyond "readable on a phone" (single column under 800 px).

## Anti-goals

- A number on any page that a model produced rather than code (narrative numbers must match the stats or quoted text).
- A narrative sentence without a resolvable citation.
- An outlook label without the three component numbers visible next to it.
- A page that triggers model spend without showing the estimate first.
- News shown without publisher and time.

## Constraints

- Keep the existing modules as the source of truth: metrics, verdicts, analyses, reports, review, calibration, watchlist,
  triggers. The app reads them; new computation lives in `fundabot/analytics/technicals.py` (price stats, move matching,
  outlook rule) and `fundabot/ingest/news.py` (FMP news); the narrative in `fundabot/analytics/moves.py`.
- Migration `007_webapp.sql`: `news`, `narratives`, `jobs` (id, ticker, kind, profile, status, started, finished, error,
  report_path, cost_usd).
- Dependencies added: `fastapi`, `uvicorn`, `jinja2` (already present for reports), `python-multipart`; htmx vendored as a
  single file under `fundabot/web/static/`.
- Prices for the chart come from `prices` (daily closes, five years); the page refresh calls `prices.ingest` when the
  latest stored date is older than the last trading day.
- All model calls go through the adapters and `run_batch` so they are logged and budgeted.

## Decided

- Package layout: `fundabot/web/app.py` (FastAPI app, routes), `fundabot/web/jobs.py` (background runner),
  `fundabot/web/templates/*.html.j2`, `fundabot/web/static/{htmx.min.js, app.css}`; `fundabot web` command.
- Templates extend one base with the report's CSS variables so reports and pages match.
- Window selector and news window are query parameters; htmx swaps the chart, narrative and news fragments.
- The narrative model is the profile's `guidance` role (Haiku 5.5 on `multi`).
- Event matching window: two trading days on either side of the move.

## Left to the builder

- Exact FMP news endpoints and their pagination.
- The rule table's thresholds beyond those stated (document them in code next to the rule).
- Tooltip and drawer implementation details; how much of a chunk the drawer shows.
- Whether the overview computes returns on request or caches them per day.
