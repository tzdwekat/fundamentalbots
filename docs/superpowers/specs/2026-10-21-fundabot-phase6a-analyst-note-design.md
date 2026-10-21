# Fundabot Phase 6a: the analyst note (readable reports and page)

2026-10-21. Tariq, after pasting a fundamental analyst's framework (business, finances, red flags, strategy, valuation):
"at the moment it's a bit too convoluted for me." The current full report is 1,455 lines ordered by how the pipeline
works: score grid, contested claims, fifteen persona sections, the whole metrics table, sources. Every fact is written
as a metric id. An analyst reads the other way round: what the business is, how the finances look, what could go wrong,
what management is doing, what it is worth, and only then who argued what.

## Intent

One document, the analyst note, produced from the data we already compute, that a reader can take in top to bottom in
five minutes, with every number still one click from its source. The same note feeds the HTML report and the ticker
page, so the two never disagree. Nothing new is computed; nothing is written by a model; the note is assembled in code
from stored structures.

## Goals

1. **Note data.** `fundabot/report/note.py: build_note(ticker, snapshot, metrics, flags, verdict | None, analyses,
   extras) -> dict` assembles sections of *facts*: each fact is `{"text": plain sentence, "value": formatted, "ids":
   [evidence ids], "tone": "good" | "bad" | "neutral" | None}`. Sentences come from a label map in code
   (`fundabot/report/labels.py`: metric id → human label, formatter, and a sentence template such as
   "Revenue grew {v} in {period}"), never from a model. `extras` carries the profile, segments, peers, dividends,
   scores, ratings, next earnings date, cross-checks and the stored narrative when present.
2. **Sections, in the framework's order.**
   - *Headline*: company line (name, sector, industry), price and market cap, the composite score with a one-line
     reading ("+1.9 on a −10..10 scale: mildly positive; the three judges agreed, 0.998"), the outlook label, and three
     anchor numbers (revenue growth last quarter, P/E against its five-year range, FCF yield).
   - *Business*: the first 300 characters of the profile description, the top product and geographic segments with
     share and growth, employees, next earnings date.
   - *Finances*: growth (revenue, EPS, FCF: last fiscal year, last quarter year over year, 3y and 5y CAGR), margins
     (gross, operating, net, FCF, each with the five-year change), returns (ROIC, ROE), position (net debt to EBITDA,
     interest coverage, current ratio, cash), cash quality (FCF to net income, accruals, SBC share), capital allocation
     (buybacks, dividends, share-count change); the three trend charts and the valuation range bars sit here.
   - *Red flags*: the rules-engine flags in words, then "what the bears say": the strongest surviving negative claims
     (consensus negative first, then contested negative), each with its sources and, when refuted, the refutation.
   - *Strategy*: guidance direction and management tone with their quotes, catalysts, estimate revisions, insider and
     institutional signals, capital intensity (capex to D&A, acquisitions to OCF), the narrative's one-liner when stored.
   - *Valuation*: each multiple against its own five-year range and against the peer median in one sentence, forward
     P/E and the DCF range against the price, analyst target upside and rating counts (vendor, flagged).
   - *The debate*: bull case and bear case (three to five claims each, consensus first, in prose with sources as
     footnote links), "where they disagree" (the two most contested claims with both sides and the judge's note), the
     median judge's summary, and the sub-scores in words.
   - *Appendix*, collapsed in HTML (`<details>`): score grid and heatmap, every persona's full output and rebuttal, the
     full metrics table, DCF assumptions, cross-checks, sources (cited metric lines and excerpts).
3. **Evidence as footnotes.** In the note every fact's ids render as small numbered links `[1]` to the appendix entry
   (metric line or excerpt); the raw `metric:…` / `chunk:…` ids appear only in the appendix. Markdown output keeps the
   ids in backticks after the sentence so grep-ability is not lost.
4. **Both run kinds.** A cheap run (no verdict) renders the same note from the persona analyses: the debate's bull and
   bear cases come from the positive and negative claims by weight; the headline shows the mean persona score with
   the reading "no judge verdict yet".
5. **The page follows the note.** The ticker page's sections take the note's order and headings; the chart and the
   "what moved it" narrative go under Finances and Strategy respectively; the decision panel becomes "The debate"; the
   FMP sections (peers, segments, ownership, scores, cross-check) land under their headings. Overview rows add the
   headline's three anchor numbers.
6. **Plain words with the number.** Every sentence names the thing, the number and the period: "Operating margin 32.0 %
   in FY2025, up 2.2 points over five years." Flags become words ("derived", "restated", "FMP and the filing disagree")
   in a muted suffix, never bare flag names. Ids and model names never appear outside the appendix.
7. **Length.** The note before the appendix fits in roughly 120 lines of Markdown for a full run; a test pins it under
   200.

## Non-goals

- New computation, new data sources or new model calls (the business brief, proxy statements, adjustments, long
  horizons and industry KPIs are Phase 6b and 6c).
- Changing the verdict, scoring, personas or judges.
- PDF export.

## Anti-goals

- A sentence whose number cannot be traced to a stored metric or excerpt.
- A fact written by a model: the note is templates over data; the only model text is quoted (claims, summaries,
  guidance quotes, the narrative), always attributed.
- Hiding a flag: a derived, restated or conflicting value keeps its marker in words.

## Constraints

- `render_verdict` and `render_report` keep their file names and positional signatures and gain one optional keyword,
  `extras: dict | None`; they build the note and render `note.md.j2`; the old templates are deleted. Rendering never
  opens a connection or calls a network: `note.load_extras(conn, cik, ticker)` reads stored rows only (profile,
  segments, peers, dividends, holders, insider trades, facts, narrative; `fact()` with a very long max age so nothing is
  fetched) and the pipeline passes the result. The pipeline's filings list adds `primary_doc_url` for the sources.
- The ticker page builds the note from the verdict's snapshot (or the newest analysed snapshot when there is no
  verdict) so numbers and debate agree; when a newer data snapshot exists the headline carries a banner ("fundamentals
  refreshed <date>; the note is from the <date> run; Analyze to update"). The page renders the note body only, with
  footnotes opening the evidence drawer for that snapshot; the appendix lives in the report file, linked.
- Run diagnostics are not dropped: providers, judge panels, "N of M claims escalated", the unscored-escalation warning,
  the non-median judges' summaries, unsupported-number callouts and per-persona grounding statistics move to an
  appendix section "Run details".
- `labels.py` covers every id in `table.GROUPS` plus the `seg_*` pattern; an unknown id falls back to the id itself
  (never crashes) and a test lists ids without a label.
- Jinja autoescape stays on for the page; the Markdown report is rendered through `markdown` as today with footnote
  anchors as plain HTML.

## Decided

- Footnote numbering restarts per note, assigned in order of first citation; the appendix "Sources" lists them in that
  order, each with its metric line or excerpt and a link to the filing when known.
- Tone colouring: good/bad from the fact's sign and sense (growth up is good, debt up is bad), only on the headline
  anchors and the margins/returns lines; everything else neutral.
- The cheap run's "composite" is the mean persona score, labelled as such.

## Left to the builder

- Exact wording of each sentence template.
- How the HTML collapses the appendix (one `<details>` per appendix section is fine).
- Chart placement inside Finances.
