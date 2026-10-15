# Fundabot Phase 3: multi-provider debate, three judges, scoring, earnings signals

2026-10-15. Builds on Phases 1 and 2 (`2026-10-15-fundabot-phase1-2-design.md`) and implements the
"Debate and synthesis" and "Provider as a dimension" sections of [docs/architecture.md](../../architecture.md),
plus the earnings-signals metrics group that feeds `earnings_momentum_score`.

## Intent

Turn five single-provider opinions into one scored verdict with its uncertainty preserved. Three AI
providers argue over the same snapshot, a rebuttal round kills unsupported claims, three judges
(one per provider) score what survives and reconcile, and code turns that into a composite score,
four sub-scores and dispersion numbers. Tariq's decisions: bring FMP estimates in now; be liberal with
cost and refine later; three judges that reconcile toward a consensus rather than one judge.

## Goals

1. `fundabot analyze T --full` runs the 5 personas × 3 providers grid (15 agents), the rebuttal
   round (15 calls), three judges and one reconcile round (6 calls), and renders the verdict report.
2. The verdict carries `composite_score`, `dispersion` (overall, per persona across providers, per
   provider across personas), judge agreement, four sub-scores, consensus and contested claims,
   red flags, catalysts and a one-paragraph summary, in the schema of the architecture doc.
3. Earnings signals from FMP: EPS and revenue surprise for the last 4 quarters, next-FY consensus
   and forward P/E, price-target consensus, and estimate revisions over 30/90 days once history exists.
4. Hallucination rate and dispersion are stored per persona × provider so provider bias can be read
   off the database (`fundabot grid T` prints the 5 × 3 score grid with those numbers).
5. The 5-agent, one-provider run from Phase 2 keeps working unchanged (`analyze T` without `--full`).

## Non-goals

- Transcripts: FMP restricts the transcript endpoint on this plan. Guidance-change and management-tone
  signals wait until a transcript source exists (IR-page scraping or a plan upgrade). Phase 4.
- Peer/sector medians, FRED macro series (macro persona still runs on filings and own history).
- Calibration, EDGAR RSS triggers, batch APIs, the weekly human-review list. Phase 4.
- Prompt tuning per provider. Same prompt contract everywhere; that is what makes provider bias measurable.

## Anti-goals

- A verdict that hides disagreement. If the judges or agents split, the report says so with numbers.
- A judge inventing claims. Judges only score claims that agents made and that resolved to evidence.
- Costs that run away silently. Every call is logged; the monthly cap still fails closed.

## Constraints

- Providers: Anthropic (`claude-sonnet-5-5` personas, `claude-opus-5-5` judge), OpenAI (`gpt-5.4`
  personas and judge), Google (`gemini-3.1-pro-preview` personas and judge; `gemini-pro-latest` if the
  preview rejects structured output at build time). Model ids are config; these are the defaults.
- Prices per model live in each adapter's `PRICES` table, taken from the providers' pricing pages at
  build time with the date noted. Unknown model ids are charged at the most expensive known tier.
- Structured JSON on every provider: Anthropic `output_config.format`, OpenAI Responses API
  `text.format` json_schema (strict), Gemini `response_mime_type` + `response_json_schema`.
- Concurrency: persona and rebuttal calls run concurrently per provider (asyncio.gather); the single
  psycopg connection is used only before (budget check for the whole batch) and after (logging).
- Expected full-run cost: about $8–15 per ticker at the chosen tiers. Measure on the first run and record it.

## Architecture additions

```
fundabot/
  agents/adapters/openai.py, gemini.py     two more Adapter implementations
  agents/runner.py                          run_grid(): personas × providers, concurrent per provider
  ingest/fmp.py                             estimates, surprises, price targets -> estimates tables
  analytics/earnings.py                     earnings-signal metrics from stored FMP rows
  debate/claims.py                          claim ids, pooling, dedupe by embedding similarity
  debate/rebuttal.py                        rebuttal round (schema, prompt, runner)
  debate/judge.py                           three judges + reconcile round
  debate/scoring.py                         composite, sub-scores, dispersion, veto (pure functions)
  report/templates/verdict.md.j2            verdict report (extends the Phase 2 report)
  cli.py                                    analyze --full/--providers, grid
  db/schema/003_debate.sql                  estimates, surprises, price_targets, rebuttals, judgements, verdicts
```

### Earnings signals (FMP)

Ingest on every `prepare`, stored raw and keyed by fetch time so revisions can be computed later:

- `estimates (cik, fetched_at, period_end, revenue_avg/low/high, eps_avg/low/high, analysts)` from
  `stable/analyst-estimates?period=annual` and `period=quarter`.
- `surprises (cik, report_date, eps_actual, eps_estimated, revenue_actual, revenue_estimated)` from `stable/earnings`.
- `price_targets (cik, fetched_at, consensus, median, high, low)` from `stable/price-target-consensus`.

Metrics (group `earnings_signals`, all `current` or the quarter's period label):

| metric | formula |
|---|---|
| `eps_surprise@Q…`, `revenue_surprise@Q…` | (actual − estimate) / abs(estimate), last 4 reported quarters |
| `eps_beat_streak` | consecutive quarters with positive EPS surprise |
| `eps_estimate_next_fy`, `revenue_estimate_next_fy` | consensus avg for the first FY ending after as_of |
| `eps_actual_fmp_ttm` | sum of the last 4 FMP quarterly `eps_actual` (adjusted basis, same as estimates) |
| `eps_growth_consensus_next_fy` | next-FY EPS avg / `eps_actual_fmp_ttm` − 1 (same basis; never divided by XBRL GAAP EPS) |
| `forward_pe` | price / `eps_estimate_next_fy`; labelled "FMP adjusted basis" next to the GAAP `pe` |
| `price_target_upside` | consensus target / price − 1 |
| `eps_estimate_revision_30d`, `_90d` | next-FY EPS avg now vs the stored fetch nearest 30/90 days earlier; null (`no_history`) until one exists |

FMP figures are vendor data, not filings: every earnings-signal metric carries the flag `vendor_fmp` and the
report labels the group "consensus (FMP)". The surprise metric uses FMP's own actuals so estimate and actual
are on the same basis; our XBRL EPS is not mixed in.

### The grid

`run_grid(conn, sid, personas, adapters, embedder)` runs every (persona, provider) pair that has no cached
analysis, concurrently within a provider, providers in parallel. Each result is grounded as in Phase 2.
Output per cell: score, confidence, grounded claims, hallucination rate.

### Claim pool and rebuttal

- Every surviving claim gets an id `{persona}/{provider}#{n}`.
- Pooling for agent A: all claims from the other 14 agents whose `direction` opposes A's score sign
  (for a score of 0, both directions). Dedupe: embed claim texts (same embedder), greedy cluster at
  cosine ≥ 0.85, keep the highest-weight claim per cluster; rank by weight × (1 if evidence resolved);
  take the top 10.
- Rebuttal prompt: A's own thesis and claims, the 10 opposing claims with their evidence ids and the
  evidence text (metric lines and chunk excerpts, fetched by id), the same hard rules. A must respond to
  the two strongest, may respond to more. Schema:

```json
{"responses": [{"claim_id": "bear/openai#3", "verdict": "concede|refute", "reason": "...", "evidence": ["metric:...", "chunk:..."]}],
 "withdraws": ["bull/anthropic#2"],
 "revised_score": -10..10, "revised_confidence": 0..1}
```

- Semantics: `concede` means the responding agent accepts the opposing claim as true; the claim is marked
  `conceded_by: [agent ids]` and counts *more* in scoring (it is agreed across the aisle). `refute` attaches a
  counter-argument with evidence; a refutation whose evidence ids do not resolve is dropped (the claim keeps no
  refutation from that agent). `withdraws` lists the agent's own claim ids it no longer stands behind after
  reading the opposition; withdrawn claims leave the pool and the agent's weight.
- Stored in `rebuttals (snapshot_id, persona, provider, model, output, grounded)`; one row per agent.

### Judges and reconciliation

Three judges, one per provider, strongest tier. Judge input: the full claim list after rebuttal (each claim
with its evidence text, weight, author agent, concessions and refutations), red flags, the earnings
signals, the metrics summary. Code pre-check drops any claim whose evidence ids do not resolve before the
judge sees it. The judge does not form a thesis. Schema:

```json
{"claims": [{"claim_id": "...", "evidence_strength": 1-5, "materiality": 1-5, "note": "..."}],
 "quality_score": 0-100, "value_score": 0-100, "earnings_momentum_score": 0-100, "accounting_risk_score": 0-100,
 "catalysts": [{"what": "...", "when": "...", "evidence": ["..."]}],
 "one_paragraph_summary": "..."}
```

Reconcile round: each judge receives the other two judges' outputs and returns the same schema again,
revising where persuaded and explaining in `note` where not. Final per-claim scores are the mean of the
three reconciled judges; sub-scores the mean; `judge_agreement` = 1 − (max − min of the three implied
composites) / 20, where a judge's implied composite is the agent-weighted composite computed with that
judge's strengths alone. `one_paragraph_summary` in the verdict is the reconciled summary of the judge whose
implied composite is the median ("the median judge", named in the verdict); all three summaries appear in the
report labelled by provider. `catalysts` is the union of the three lists deduplicated by the same embedding
rule as claims (cosine ≥ 0.85), keeping the entry with the most resolved evidence ids. Stored in
`judgements (snapshot_id, provider, model, round, output)`.

### Scoring (pure code, `debate/scoring.py`)

- Agent weight = Σ over the agent's surviving, non-withdrawn claims of `weight × evidence_strength × (1.5 if
  conceded by at least one opponent else 1)`.
- `composite_score` = Σ(agent revised_score × agent weight) / Σ(agent weight) over all agents in the run.
  If every agent conceded everything, composite = plain mean and `dispersion` note says so.
- `dispersion` = population std of agent revised scores / 10, clipped to [0, 1]; also `dispersion_by_persona`
  (std across providers for each persona) and `dispersion_by_provider` (std across personas for each provider).
- `accounting_risk_score` veto: above 70, composite is capped at 0; above 85, capped at −3. The cap and the
  reason appear in the report.
- `consensus_claims`: non-withdrawn claims with mean evidence_strength ≥ 3, materiality ≥ 3 and no refutation;
  `contested_claims`: claims with at least one refutation, or judge strength spread ≥ 2. A claim can be both
  conceded by one opponent and refuted by another; it is listed as contested with both sides.
- The 5 × 3 grid: each cell's revised score, confidence, hallucination rate; row and column spreads.
- Verdict row stored in `verdicts (snapshot_id, run_kind, verdict jsonb, created_at)`; the architecture's
  final-verdict schema is the `verdict` JSON exactly.

### Report

`--full` renders the verdict report: summary paragraph, composite with dispersion and judge agreement,
the 5 × 3 grid, sub-scores with the veto note, the three most contested claims with both sides and the
judges' notes, consensus claims, red flags, catalysts, earnings signals, then the Phase 2 per-agent
sections and sources appendix. The 5-agent run keeps the Phase 2 report.

### CLI

```
fundabot analyze T... [--full] [--providers anthropic,openai,gemini] [--judges 1|3] [--model ...] [--open] [--force] [--rerun]
fundabot grid T          # latest verdict's 5x3 grid, dispersion by persona/provider, hallucination rates
```

### Testing

Fake adapters per provider (same `FakeAdapter`, distinct `provider` strings). Pure scoring tests with
hand-built claim sets (veto, weighting, dispersion, consensus/contested rules). Rebuttal pooling tests
with a FakeEmbedder (dedupe, opposing-direction filter, top-10). Judge pre-check test. One end-to-end
`--full` test through the real pipeline with fakes: 15 + 15 + 6 fake calls, verdict stored, report
rendered, second run makes zero calls. FMP ingest tests on saved JSON fixtures; earnings metrics tests
on hand-built rows. Adapter tests for OpenAI and Gemini use their SDK clients with a stub transport or a
monkeypatched `create` call; one manual smoke call per provider at build time with cost printed.

## Decisions left open (Tariq)

None blocking. FMP `analyst-estimates`, `earnings` and `price-target-consensus` were confirmed available on the
current plan on 2026-10-15 (transcripts are not). `FUNDABOT_MONTHLY_BUDGET_USD` is raised to 500 in `.env`
for Phase 3 testing; a full run is $8–15. Defaults chosen: model ids above; veto thresholds 70/85; dedupe threshold 0.85;
top-10 opposing claims; reconcile round always on with `--judges 3`.

## Calls Claude made

- Earnings signals use FMP actuals for surprises (same basis as estimates) and are flagged `vendor_fmp`.
- Revisions need stored history, so they are null until the second fetch 30+ days later; no backfill.
- Judges reconcile once (one extra round), then code takes the mean; no third round.
- Concurrency via asyncio per provider; DB access stays on one connection outside the calls.
