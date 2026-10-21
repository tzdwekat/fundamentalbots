# Phase 6a Analyst Note Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the pipeline-ordered reports with an analyst note (headline, business, finances, red flags, strategy, valuation, the debate, collapsed appendix) assembled in code from stored data, and make the ticker page follow it.

**Architecture:** `report/labels.py` (id → label, formatter, sentence template) and `report/note.py` (`build_note`, `load_extras`, footnote numbering) produce one data structure; `note.md.j2` renders it for both run kinds; `render_report` / `render_verdict` call it; `views.ticker_page` builds the same note for the page.

**Tech Stack:** Jinja2, markdown, existing charts.

**Spec:** docs/superpowers/specs/2026-10-21-fundabot-phase6a-analyst-note-design.md

## Global Constraints

- No new computation, data source or model call. Every sentence is a template over a stored value; the only model text is quoted and attributed.
- Rendering takes no connection: `load_extras(conn, cik, ticker)` reads stored rows only (`fact(..., max_age_hours=10**6)`), the pipeline passes `extras=`.
- Ids and model names appear only in the appendix. Footnotes `[n]` numbered by first citation.
- The note body (before "Appendix") is under 200 Markdown lines for a full run (test).
- Unknown metric id → label falls back to the id; never a crash.
- `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot`.

## Review Focus

1. A metric that is null (`tag_missing`) must produce no sentence, not "Revenue grew null" (Task 1 test `test_null_metrics_make_no_fact`).
2. A cheap run (no verdict, one provider) renders the full note with "no judge verdict yet" and bull/bear cases from the persona claims (Task 2 test `test_cheap_note_renders_without_verdict`).
3. A claim citing a chunk id must get a footnote whose appendix entry shows the excerpt and the filing link (Task 2 test `test_footnotes_resolve_to_sources`).
4. A snapshot with no FMP extras (profile None, no segments, no peers) renders with those lines omitted, not "None · None" (Task 2 test `test_note_without_extras`).
5. The page uses the verdict's snapshot for the note and shows the banner when a newer data snapshot exists (Task 3 test `test_page_note_uses_verdict_snapshot_with_banner`).

---

### Task 1: Labels and the note builder (pure)

**Files:** create `fundabot/report/labels.py`, `fundabot/report/note.py`; test `tests/test_note.py` (uses `tests/test_context_pruning.aapl_metrics` for metrics and a hand-built verdict dict in the shape of `scoring.build_verdict`).

**Interfaces:**
- `labels.LABELS: dict[str, tuple[str, str]]` id → (label, kind) with kind in `usd, pct, x, count, shares, usd_share, score, ratio`; `labels.label(mid) -> str` (falls back to `mid.replace("_", " ")`); `labels.fmt(value, kind) -> str` ("$416.2B", "6.4%", "39.4x", "14,776M shares"); `labels.seg_label(mid, inputs) -> str` for `seg_*` ids; `labels.flag_words(flags) -> str` ("derived", "restated", "FMP and the filing disagree", "split-adjusted", "vendor data"); `labels.unlabelled(ids) -> list[str]` for the test.
- `note.Fact = dict(text, value, ids, tone, flags_text)`; `note.fact(metrics_lookup, mid, period, template, tone_rule=None) -> Fact | None` (None when the value is null).
- `note.Footnotes`: `.ref(ids) -> str` returns `[1]`/`[1,2]` markers and records order; `.entries(metrics, chunks_by_id, filings) -> list[dict(n, id, text, url)]`.
- `note.build_note(ticker, snapshot, metrics, flags, verdict, analyses, extras, chunks_by_id=None, filings=None, run_kind="full") -> dict` with keys `headline, business, finances, red_flags, strategy, valuation, debate, appendix, footnotes, charts_html`; each section is `{"title", "facts": [Fact], ...}` with section-specific extras (`finances.tables`, `debate.bull/bear/contested/summary/subscores`, `appendix.run_details/grid/personas/metrics_table/dcf/crosschecks/sources`).
- `note.load_extras(conn, cik, ticker) -> dict` keys `profile, segments, peers, dividends, holders, insider_trades, scores, ratings, next_earnings, crosscheck, edgar_crosscheck, narrative, risk_free`.
- `note.reading(composite, agreement) -> str` ("+1.9 on a −10..10 scale: mildly positive; judges agreed (0.998)"); bands: ≥ 5 strongly positive, ≥ 2 positive, > −2 mixed/mildly, ≤ −2 negative, ≤ −5 strongly negative.

- [ ] **Step 1: Failing tests:** `test_labels_cover_groups` (`unlabelled(all GROUPS ids) == []`), `test_fmt_kinds`, `test_fact_sentence_and_tone` (revenue_yoy FY2025 → "Revenue grew 6.4% in FY2025", tone good; net_debt rising → bad), `test_null_metrics_make_no_fact`, `test_footnotes_number_by_first_citation` (`ref(["metric:a","chunk:b"])` → "[1,2]", `ref(["chunk:b"])` → "[2]"), `test_build_note_sections_full` (headline reading text, business has segments, finances has growth/margins/position tables, red flags lists `altman`-style flags in words and bears' claims, valuation sentences include "5-year range" and "peer median", debate bull/bear non-empty, appendix has run_details with panels and escalation count), `test_build_note_cheap_run` (verdict None → headline says "no judge verdict yet", debate from analyses), `test_note_without_extras`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS. **Step 5:** Commit `feat: analyst note data — labels, facts, footnotes, sections`.

### Task 2: Render the note for both run kinds

**Files:** create `fundabot/report/templates/note.md.j2`; modify `fundabot/report/render.py` (`render_report(..., extras=None)`, `render_verdict(..., extras=None)` both via `build_note`; delete `verdict.md.j2` and `report.md.j2`), `fundabot/orchestrate/pipeline.py` (filings query adds `primary_doc_url`; `extras = note.load_extras(conn, cik, ticker)` passed to both renderers), `tests/test_full_pipeline.py` and `tests/test_pipeline.py` (re-point assertions: "escalation panel", "screen panel", "claims escalated", "openai judge summary", "eps_surprise" now in the appendix; "Judge agreement" wording → "judges agreed"), `tests/test_charts.py` if it reads the report; new tests in `tests/test_note.py`.

**Interfaces:**
- `note.md.j2` renders: `# {{ ticker }} — {{ headline.company }}`, headline block, `## Business`, `## Finances` (tables + `charts_html`), `## Red flags`, `## Strategy`, `## Valuation`, `## The debate` (Bull case / Bear case / Where they disagree / Judge's summary / Scores in words), `---`, `# Appendix` with `<details>` blocks: Run details, Score grid, Persona outputs (the old per-persona section verbatim), Metrics table, DCF assumptions, Cross-checks, Sources (`[n]` entries).
- HTML: footnote markers `<a class="fn" href="#src-n">[n]</a>`; sources `<p id="src-n">`.

- [ ] **Step 1: Failing tests:** `test_cheap_note_renders_without_verdict`, `test_footnotes_resolve_to_sources`, `test_note_without_extras`, `test_note_body_length` (lines before "# Appendix" < 200 on the full fixture run), `test_no_ids_outside_appendix` (no `metric:` or `chunk:` before "# Appendix"), plus the re-pointed pipeline assertions.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS. **Step 5:** Commit `feat: analyst note report for full and cheap runs`.

### Task 3: The page follows the note

**Files:** modify `fundabot/web/views.py` (`ticker_page` builds the note from the verdict's snapshot via `build_note`, `newer_snapshot` banner, keeps stats/chart/narrative/news), `fundabot/web/templates/ticker.html.j2` (sections in note order; `_claim.html.j2` reused inside The debate; footnotes → `hx-get` drawer), `overview.html.j2` (three anchor numbers), tests `tests/test_web.py`.

- [ ] **Step 1: Failing tests:** `test_page_note_uses_verdict_snapshot_with_banner` (two snapshots: older with verdict, newer with metrics only → note numbers from the older, banner present), `test_page_sections_in_framework_order` (headings appear in order Business, Finances, Red flags, Strategy, Valuation, The debate), `test_page_footnote_opens_drawer` (a `[1]` with `hx-get="/evidence/<verdict sid>/..."`), overview anchors test.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS. **Step 5:** Commit `feat: ticker page follows the analyst note`.

### Task 4: Live render, look, docs

- [ ] **Step 1:** Re-render AAPL's stored full verdict (`fundabot analyze AAPL --full` is cached: every round reuses, only the render runs) and open the HTML; read the note top to bottom for wording and order; fix anything that reads wrong.
- [ ] **Step 2:** Open `/t/AAPL` and the overview in the browser.
- [ ] **Step 3:** README: "What the report contains" rewritten for the note. Commit `docs: analyst note`.
