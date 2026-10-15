# Fundabot Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `fundabot analyze T --full` runs 5 personas × 3 providers, a rebuttal round, three reconciling judges and deterministic scoring, and renders a verdict report; FMP earnings signals feed the metrics and the earnings-momentum sub-score.

**Architecture:** Two more `Adapter` implementations (OpenAI Responses API, Google GenAI) behind the Phase 2 interface; a concurrent batch runner; a claim pool with embedding dedupe; rebuttal and judge rounds as structured-output calls stored per (snapshot, agent/judge, round); pure scoring functions produce the architecture's verdict JSON; the Phase 2 pipeline gains an `analyze_full` stage.

**Tech Stack:** as Phase 1–2 plus `openai` (already installed) Responses API, `google-genai`, FMP REST via httpx.

**Spec:** `docs/superpowers/specs/2026-10-15-fundabot-phase3-design.md` (and `docs/architecture.md`).

## Global Constraints

- Default model ids: personas `claude-sonnet-5-5` / `gpt-5.4` / `gemini-3.1-pro-preview`; judges `claude-opus-5-5` / `gpt-5.4` / `gemini-3.1-pro-preview`. All configurable per run.
- Prices (USD per 1M input/output, fetched 2026-10-15): `gpt-5.4` 2.50/15.00, `gpt-5.4-mini` 0.75/4.50, `gpt-5.4-pro` 30/180; `gemini-3.1-pro-preview` 2.00/12.00 (≤200k prompt), `gemini-3.8-flash` 0.75/3.75, `gemini-2.5-pro` 1.25/10.00. Unknown ids charge at the most expensive known tier of that provider.
- Structured JSON on every provider; `schemas.validate`-style range checks in code, never trusting the schema alone.
- Every earnings-signal metric carries flag `vendor_fmp`; FMP actuals and estimates are never divided by XBRL EPS.
- Concurrency: calls within a round run with `asyncio.gather`; the single psycopg connection is touched only before (budget check for the whole batch) and after (logging) the gather.
- Cache keys: analyses `(snapshot, persona, provider, model)`; rebuttals `(snapshot, persona, provider, model)`; judgements `(snapshot, provider, model, round)`; verdicts `(snapshot, run_kind)`. `--rerun` clears all four for the snapshot.
- Tests make no network calls; db-marked tests use `fundabot_test`. The existing 99 tests keep passing.
- Phase 2's `analyze T` (no `--full`) behaviour and report are unchanged.

## Review Focus

1. One provider failing mid-grid (timeout, 5xx after retries, refusal) must not lose the other cells: the run continues, the cell is recorded as failed and the report says so (Task 4 test `test_grid_survives_one_provider_failure`).
2. A rebuttal that names a `claim_id` not in its opposing list, or withdraws an id it does not own, is ignored rather than crashing or altering someone else's claim (Task 6 test `test_rebuttal_ignores_unknown_and_foreign_ids`).
3. A judge output missing some claim ids: those claims get the mean of the judges that did score them; a claim no judge scored gets strength 0 and is listed as unscored (Task 8 test `test_missing_judge_scores_use_available_judges`).
4. Every agent withdrawing every claim: composite falls back to the plain mean of revised scores with `weighting: "unweighted (no surviving claims)"` in the verdict (Task 8 test `test_all_withdrawn_falls_back_to_plain_mean`).
5. FMP returns empty lists for an uncovered small cap: the earnings group is null with reason `no_coverage`, the pipeline finishes, and the judge message says consensus data is unavailable (Task 2 test `test_no_fmp_coverage_is_null`, Task 7 test `test_judge_message_states_missing_consensus`).

---

## File structure

```
fundabot/db/schema/003_debate.sql          estimates, surprises, price_targets, rebuttals, judgements, verdicts
fundabot/ingest/fmp.py                     fetch + store + load for the three FMP endpoints
fundabot/analytics/earnings.py             compute_earnings_metrics()
fundabot/agents/adapters/openai.py         OpenAIAdapter
fundabot/agents/adapters/gemini.py         GeminiAdapter
fundabot/agents/base.py                    + run_batch()
fundabot/agents/runner.py                  + run_grid(), agent_key()
fundabot/debate/__init__.py
fundabot/debate/claims.py                  build_pool, evidence_text, dedupe, top_opposing
fundabot/debate/rebuttal.py                REBUTTAL_SCHEMA, message builder, run_rebuttals, apply_rebuttals
fundabot/debate/judge.py                   JUDGE_SCHEMA, precheck, message builders, run_judges, run_reconcile
fundabot/debate/scoring.py                 weights, composite, dispersion, agreement, veto, classify, build_verdict
fundabot/orchestrate/pipeline.py           + analyze_full()
fundabot/report/render.py, templates/verdict.md.j2
fundabot/report/table.py                   + earnings_signals group
fundabot/cli.py                            analyze --full/--providers/--judges, grid
tests/fixtures/fmp_aapl_*.json             saved responses
tests/test_fmp.py test_earnings.py test_adapters_openai_gemini.py test_grid.py test_claims.py test_rebuttal.py test_judge.py test_scoring.py test_full_pipeline.py
```

Shared claim dict (produced by Task 5, consumed by 6–9):

```python
{"id": "bear/openai#3", "persona": "bear", "provider": "openai", "agent": "bear/openai",
 "text": str, "evidence": [str], "direction": "positive|negative", "weight": int,
 "conceded_by": [agent keys], "refutations": [{"by": agent key, "reason": str, "evidence": [str]}], "withdrawn": bool}
```

`agent_key(persona, provider) -> f"{persona}/{provider}"` lives in `agents/runner.py`.

---

### Task 1: Schema 003 and FMP ingest

**Files:** Create `fundabot/db/schema/003_debate.sql`, `fundabot/ingest/fmp.py`, `scripts/make_fmp_fixture.py`, `tests/fixtures/fmp_aapl_estimates_annual.json`, `..._estimates_quarter.json`, `..._earnings.json`, `..._price_target.json`. Test `tests/test_fmp.py`.

**Interfaces:**
- Tables: `estimates(cik int, fetched_at timestamptz, period text 'annual'|'quarter', period_end date, revenue_avg numeric, revenue_low, revenue_high, eps_avg, eps_low, eps_high, analysts int, pk(cik, fetched_at, period, period_end))`; `surprises(cik, report_date date, eps_actual numeric, eps_estimated, revenue_actual, revenue_estimated, pk(cik, report_date))`; `price_targets(cik, fetched_at, consensus, median, high, low, pk(cik, fetched_at))`; `rebuttals(snapshot_id, persona, provider, model, output jsonb, grounded jsonb, pk(snapshot_id, persona, provider, model))`; `judgements(snapshot_id, provider, model, round int, output jsonb, pk(snapshot_id, provider, model, round))`; `verdicts(snapshot_id, run_kind text, verdict jsonb, created_at, pk(snapshot_id, run_kind))`; `analyses` gains `status text default 'ok'` and `error text`.
- `fmp.get(path, **params) -> list|dict` (httpx, `apikey` from settings, raises `FMPError` on non-200 or the "Restricted Endpoint" text).
- `fmp.fetch_all(ticker) -> dict` with keys `estimates_annual, estimates_quarter, earnings, price_target` (raw lists).
- `fmp.ingest(conn, cik, ticker, fetched_at=None) -> datetime` stores all three tables (estimates with `fetched_at`; surprises upserted by report_date; a `price_targets` row per fetch) and returns `fetched_at`.
- `fmp.load(conn, cik) -> dict` → `{"estimates": DataFrame[fetched_at, period, period_end, eps_avg, revenue_avg, ...], "surprises": DataFrame[report_date, eps_actual, eps_estimated, revenue_actual, revenue_estimated], "price_targets": DataFrame[fetched_at, consensus, median, high, low]}`.
- Field mapping: estimates `date`→period_end, `revenueAvg/Low/High`, `epsAvg/Low/High`, `numAnalystsEps`→analysts (0 if absent); earnings `date`→report_date and the four `eps*/revenue*` fields; price target `targetConsensus/targetMedian/targetHigh/targetLow`.

- [ ] Step 1: `scripts/make_fmp_fixture.py AAPL` saves the four raw responses (run once by hand; `limit=8` on estimates, `limit=12` on earnings).
- [ ] Step 2: Failing tests: `test_parse_estimates_fields` (fixture → rows with period_end `2030-09-27` present, eps_avg 12.905), `test_ingest_and_load_roundtrip` (db; monkeypatch `fetch_all`; two ingests with different `fetched_at` → two estimate fetches, surprises not duplicated), `test_restricted_endpoint_raises` (monkeypatch httpx to return the "Restricted Endpoint" text → `FMPError`).
- [ ] Step 3: FAIL, implement, PASS, `fundabot init-db` applies 003, commit `feat: fmp estimates, surprises and price targets`.

---

### Task 2: Earnings-signal metrics

**Files:** Create `fundabot/analytics/earnings.py`; modify `fundabot/orchestrate/pipeline.py` (prepare: `fmp.ingest` then metrics), `fundabot/report/table.py` (group `earnings_signals`). Test `tests/test_earnings.py`.

**Interfaces:**
- `earnings.compute_earnings_metrics(fmp: dict, as_of: date, price: float | None, fiscal_year_end: tuple[int,int], latest_fetch: datetime) -> list[Metric]`.
- Metric ids and periods: `eps_surprise`, `revenue_surprise` at period = `Q{n}FY{y}` resolved from `report_date` via the same fiscal labelling as periods (use `periods.fy_of`/`fq_of` on the quarter end = report_date − 30 days… no: use the surprise row's `date` as the quarter's *report* date and label by the quarter end nearest before it: `periods.fy_of(report_date - 35 days)`); `eps_beat_streak@current` (count); `eps_estimate_next_fy@current`, `revenue_estimate_next_fy@current`, `eps_actual_fmp_ttm@current`, `eps_growth_consensus_next_fy@current`, `forward_pe@current`, `price_target_upside@current`, `eps_estimate_revision_30d@current`, `_90d@current`.
- "next FY" = the annual estimate row with the smallest `period_end > as_of`. Revisions compare `eps_avg` of that period_end between the latest fetch and the stored fetch nearest to 30/90 days before it (tolerance ±7 days), else `null_reason="no_history"`.
- Empty `surprises` and `estimates` → every metric `None` with `null_reason="no_coverage"`. All metrics: `flags=["vendor_fmp"]`, `formula_id` as in the spec table, `inputs` naming the FMP rows used.
- `prepare` calls `fmp.ingest` inside the same try as prices (an `FMPError` logs a warning; metrics become `no_coverage`), then appends earnings metrics to the stored list. `table.GROUPS["earnings_signals"]` lists the ids above.

- [ ] Step 1: Failing tests on hand-built DataFrames: `test_surprise_and_streak` (4 surprises, 3 positive in a row → `eps_beat_streak == 3`, `eps_surprise` within 1e-9 of (actual−est)/|est|), `test_next_fy_growth_same_basis` (quarter actuals 2.0,2.0,2.1,2.2 → ttm 8.3; next-FY avg 9.13 → growth 0.1), `test_forward_pe_and_target_upside`, `test_revision_needs_history` (one fetch → `no_history`; a second fetch 31 days earlier with eps_avg 9.0 → `eps_estimate_revision_30d == 9.13/9.0 - 1`), `test_no_fmp_coverage_is_null`.
- [ ] Step 2–5: FAIL, implement, PASS, then `fundabot metrics AAPL` shows the `earnings_signals` group with real values; commit `feat: earnings-signal metrics from FMP`.

---

### Task 3: OpenAI and Gemini adapters, batch runner

**Files:** Create `fundabot/agents/adapters/openai.py`, `fundabot/agents/adapters/gemini.py`; modify `fundabot/agents/base.py`, `pyproject.toml` (+`google-genai>=1.0`), `.env.example`. Test `tests/test_adapters_openai_gemini.py`.

**Interfaces:**
- `OpenAIAdapter(model="gpt-5.4", max_tokens=8000)`: `provider="openai"`; `AsyncOpenAI(api_key=settings.openai_api_key, timeout=180, max_retries=3)`; `client.responses.create(model=, instructions=system, input=user, max_output_tokens=, text={"format": {"type": "json_schema", "name": "analysis", "schema": schema, "strict": True}})`; parse `r.output_text`; tokens from `r.usage.input_tokens/output_tokens`; `PRICES` per Global Constraints; `cost()`/`estimate_cost()` as the Anthropic adapter. Raise `RuntimeError` on `r.status != "completed"` or a refusal item.
- `GeminiAdapter(model="gemini-3.1-pro-preview", max_tokens=8000)`: `provider="gemini"`; `genai.Client(api_key=settings.gemini_api_key)`; `await client.aio.models.generate_content(model=, contents=user, config=types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", response_json_schema=schema, max_output_tokens=))`; parse `r.text`; tokens from `r.usage_metadata.prompt_token_count` / `candidates_token_count` (+ `thoughts_token_count` if present, into output); retries 3× with backoff on 429/5xx/timeouts (the SDK's `http_options` retry if available, else a loop).
- `base.run_batch(conn, sid, calls: list[tuple[Adapter, str, str, str, dict]]) -> list[CompletionResult | Exception]`: budget check once for `sum(estimate_cost)`; `asyncio.gather(*[a.complete(s, u, schema) ...], return_exceptions=True)`; log a `model_calls` row for each success; return in order. `call_logged` becomes a one-element `run_batch`.
- Settings: no new keys (gemini key already present).

- [ ] Step 1: Failing tests: `test_openai_adapter_parses_response` (monkeypatch `client.responses.create` with a stub returning `output_text`, `status`, `usage` → `CompletionResult` with cost `2.5*80e3/1e6 + 15*2e3/1e6`), `test_gemini_adapter_parses_response` (stub `generate_content` → result, cost at 2/12), `test_prices_tables`, `test_run_batch_logs_each_success_and_returns_exceptions` (db; three FakeAdapters, one raising → two rows logged, result list has the exception in position), `test_run_batch_budget_checks_whole_batch` (db; budget 1.0, three fakes each estimating 0.5 → `BudgetExceeded`, zero calls).
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: Smoke (by hand, prints cost): one `complete()` per provider with the agent schema and a 200-token prompt. If `gemini-3.1-pro-preview` rejects `response_json_schema`, switch the default to `gemini-pro-latest` and ledger it.
- [ ] Step 6: Commit `feat: openai and gemini adapters, concurrent batch runner`.

---

### Task 4: The grid

**Files:** Modify `fundabot/agents/runner.py`, `fundabot/cli.py`. Test `tests/test_grid.py`.

**Interfaces:**
- `runner.agent_key(persona, provider) -> str`.
- `runner.run_grid(conn, sid, persona_names, adapters: list[Adapter], embedder) -> list[dict]`: for each adapter, the personas without a cached `ok` analysis for that (provider, model); builds messages (retrieval once per persona, shared across providers), runs one `run_batch` per provider concurrently (`asyncio.gather` over providers), validates and grounds each result (`ground(...)` as in `pipeline.analyze`), stores `analyses` rows with `status='ok'`; an exception stores `status='failed', error=str(e)[:500]` and the cell is skipped. Returns all `ok` analyses for the requested cells, each dict gaining `agent`, `persona`, `provider`, `model`, `grounded`.
- `run_personas` becomes `run_grid` with one adapter (keep the name as a thin wrapper).
- CLI: `analyze --providers anthropic,openai,gemini` (default `anthropic`), `--full` implies all three; `fundabot grid T` prints, for the latest snapshot, a persona × provider table of `score / confidence / hallucination%` from `analyses.grounded`, with row and column standard deviations, and `failed` where `status='failed'`.

- [ ] Step 1: Failing tests (db, fakes, fixture-backed snapshot as in `tests/test_pipeline.py`): `test_grid_runs_15_cells_and_caches` (3 fakes × 5 personas → 15 calls; second run 0), `test_grid_survives_one_provider_failure` (one fake whose `complete` raises for persona "bear" → 14 `ok` rows, one `failed` row, return length 14), `test_grid_output_prints_table` (capsys).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: persona x provider grid`.

---

### Task 5: Claim pool and dedupe

**Files:** Create `fundabot/debate/__init__.py`, `fundabot/debate/claims.py`. Test `tests/test_claims.py`.

**Interfaces:**
- `claims.build_pool(analyses: list[dict]) -> list[dict]`: one claim dict (shape above) per kept claim in `a["grounded"]["claims"]`, id `{agent}#{index in grounded claims}`; `conceded_by=[]`, `refutations=[]`, `withdrawn=False`.
- `claims.evidence_text(ids: list[str], metrics: list[Metric], chunks_by_id: dict[str, Chunk], accessions: list[str]) -> dict[str, str]`: `metric:` ids → the `context.metric_line` of every metric with that id (and period if given); `chunk:` ids → the chunk text; `filing:` ids → `"filing {accession}"`. Missing ids are omitted.
- `claims.dedupe(items: list[dict], embedder, key: str = "text", threshold: float = 0.85) -> list[dict]`: greedy, embed all `item[key]`, iterate by descending `weight` (or `len(evidence)` when no weight), keep an item if its max cosine to kept items < threshold.
- `claims.top_opposing(pool, agent: str, score: int, embedder, k: int = 10) -> list[dict]`: claims from other agents with `direction == "negative"` if `score > 0`, `"positive"` if `score < 0`, both if 0; dedupe; sort by `weight` desc; first k.

- [ ] Step 1: Failing tests with FakeEmbedder (identical texts embed identically): `test_pool_ids_and_shape`, `test_dedupe_keeps_highest_weight_of_identical_texts`, `test_top_opposing_filters_direction_and_self`, `test_evidence_text_lookup`.
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: claim pool, dedupe and opposing-claim selection`.

---

### Task 6: Rebuttal round

**Files:** Create `fundabot/debate/rebuttal.py`. Test `tests/test_rebuttal.py`.

**Interfaces:**
- `REBUTTAL_SCHEMA` (strict; fields `responses[claim_id, verdict enum concede|refute, reason, evidence[]]`, `withdraws[]`, `revised_score int`, `revised_confidence number`); `validate_rebuttal(output) -> dict` with range checks.
- `rebuttal.system_prompt(persona: dict) -> str`: the persona's role, the hard rules from `context.HARD_RULES`, and: "You will read the strongest claims made against your position by other analysts. For at least the two strongest, either concede (you accept the claim is true) or refute it with a citation. Withdraw any of your own claims you no longer stand behind. Then give a revised score and confidence."
- `rebuttal.build_message(analysis: dict, opposing: list[dict], evidence: dict[str, str]) -> str`: sections `YOUR_THESIS`, `YOUR_CLAIMS` (with ids), `OPPOSING_CLAIMS` (id, author, weight, text, evidence ids), `EVIDENCE` (id → text).
- `rebuttal.run_rebuttals(conn, sid, analyses, pool, adapters_by_provider: dict[str, Adapter], embedder, metrics, chunks_by_id, accessions) -> list[dict]`: for each `ok` analysis without a cached rebuttal, build the message, run all via one `run_batch` per provider concurrently, validate, store `rebuttals(output, grounded)` where `grounded` records which response evidence ids resolved. Returns rebuttal dicts with `agent`, `persona`, `provider`, `output`.
- `rebuttal.apply_rebuttals(pool, rebuttals, resolvable: callable[[str], bool]) -> tuple[list[dict], dict[str, tuple[int, float]]]`: marks `conceded_by`, appends refutations whose evidence ids all resolve (drop others), sets `withdrawn=True` only for ids owned by the withdrawing agent; ignores unknown ids. Returns the pool and `{agent: (revised_score, revised_confidence)}`; agents without a rebuttal keep their original score/confidence.

- [ ] Step 1: Failing tests: `test_schema_validation`, `test_message_contains_opposing_and_evidence`, `test_apply_concede_refute_withdraw`, `test_rebuttal_ignores_unknown_and_foreign_ids`, `test_refutation_without_resolvable_evidence_dropped`, `test_run_rebuttals_caches` (db; fakes; 15 analyses → 15 calls then 0).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: rebuttal round`.

---

### Task 7: Judges and reconciliation

**Files:** Create `fundabot/debate/judge.py`. Test `tests/test_judge.py`.

**Interfaces:**
- `JUDGE_SCHEMA` per the spec (claims[claim_id, evidence_strength int, materiality int, note], four 0–100 integer sub-scores, catalysts[what, when, evidence[]], one_paragraph_summary); `validate_judgement(output)`.
- `judge.precheck(pool, resolvable) -> list[dict]`: drops claims with any unresolvable evidence id (should be none after grounding; belt and braces) and withdrawn claims.
- `judge.system_prompt() -> str`: no persona; "You do not form a thesis. Score each claim's evidence strength (1 = asserted, 5 = directly shown by the cited metric or passage) and materiality (1 = trivia, 5 = decides the investment case)…", the sub-score definitions (quality = moat/ROIC/durability; value = price vs own history and DCF; earnings_momentum = surprises, revisions, growth acceleration; accounting_risk = 0 clean … 100 likely misstatement), catalysts must cite evidence ids, summary ≤ 120 words.
- `judge.build_message(pool, flags, metrics, earnings_available: bool, evidence: dict[str, str], revised: dict) -> str`: `CLAIMS` (id, author, direction, weight, text, evidence ids, conceded_by, refutations with reasons), `RED_FLAGS`, `METRICS_SUMMARY` (the last-FY and current rows of the `table.GROUPS` ids), `CONSENSUS_DATA` (earnings signal lines, or "Consensus estimates are unavailable for this company; score earnings_momentum from reported results only and say so."), `EVIDENCE`, `AGENT_SCORES` (revised scores per agent).
- `judge.build_reconcile_message(own: dict, others: list[tuple[str, dict]]) -> str`: the judge's own round-1 output and the two others labelled by provider, with "Revise where persuaded; where you keep a different score, say why in note."
- `judge.run_judges(conn, sid, judges: list[Adapter], message: str) -> list[dict]` (round 1, cached per judge) and `judge.run_reconcile(conn, sid, judges, round1: list[dict]) -> list[dict]` (round 2). Each returns `[{"provider", "model", "round", "output"}]`.
- `judge.mean_scores(reconciled: list[dict]) -> dict[str, dict]`: per claim id `{"evidence_strength": mean, "materiality": mean, "n_judges": int, "spread": max−min of strength, "notes": [...]}`; claims no judge scored get strength 0, materiality 0, `n_judges 0`.

- [ ] Step 1: Failing tests: `test_precheck_drops_unresolvable_and_withdrawn`, `test_judge_message_states_missing_consensus`, `test_mean_scores_with_missing_judge` (3 judges, one omits a claim → mean of two, `n_judges 2`), `test_run_judges_and_reconcile_cache` (db; three fakes → 3 + 3 calls, then 0).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: three judges with a reconcile round`.

---

### Task 8: Scoring and verdict

**Files:** Create `fundabot/debate/scoring.py`. Test `tests/test_scoring.py`.

**Interfaces (pure functions):**
- `agent_weights(pool, judge_means) -> dict[str, float]`: Σ over the agent's non-withdrawn claims of `weight × strength × (1.5 if conceded_by else 1)`.
- `composite(revised: dict[str, tuple[int, float]], weights) -> tuple[float, str]`: weighted mean and `"evidence-weighted"`, or plain mean and `"unweighted (no surviving claims)"` when all weights are 0.
- `dispersion(revised, cells: list[tuple[str, str]]) -> dict`: `{"overall": pstd/10 clipped, "by_persona": {persona: pstd across providers /10}, "by_provider": {provider: pstd across personas /10}}`.
- `implied_composite(pool, judge_output, revised) -> float`: the composite using that one judge's strengths.
- `judge_agreement(implied: list[float]) -> float` = `1 − (max−min)/20`; `median_judge(implied, providers) -> str`.
- `veto(composite_value, accounting_risk) -> tuple[float, str | None]`: `> 85` → `min(v, -3)`, `> 70` → `min(v, 0)`, note text otherwise None.
- `classify_claims(pool, judge_means) -> tuple[consensus, contested]` per the spec rules.
- `build_verdict(ticker, sid, pool, revised, judge_means, reconciled, implied, cells, catalysts, summary_by_provider) -> dict`: the architecture schema fields exactly (`ticker, snapshot_id, composite_score, dispersion, quality_score, value_score, earnings_momentum_score, accounting_risk_score, consensus_claims, contested_claims, red_flags, catalysts, one_paragraph_summary`) plus `grid` (cell → score/confidence/hallucination_rate), `dispersion_by_persona`, `dispersion_by_provider`, `judge_agreement`, `median_judge`, `summaries` (provider → text), `veto_note`, `weighting`. `composite_score` is rounded to 1 decimal, sub-scores to integers.

- [ ] Step 1: Failing tests with hand-built pools: `test_weights_and_concession_bonus`, `test_composite_weighted_vs_plain`, `test_all_withdrawn_falls_back_to_plain_mean`, `test_dispersion_rows_and_columns` (known std), `test_veto_caps`, `test_classify_consensus_and_contested`, `test_missing_judge_scores_use_available_judges` (through `judge.mean_scores` + `agent_weights`), `test_verdict_schema_keys`.
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: scoring and verdict assembly`.

---

### Task 9: Full pipeline, verdict report, CLI, first live run

**Files:** Modify `fundabot/orchestrate/pipeline.py`, `fundabot/report/render.py`, `fundabot/cli.py`, `README.md`; create `fundabot/report/templates/verdict.md.j2`. Test `tests/test_full_pipeline.py`.

**Interfaces:**
- `pipeline.analyze_full(conn, ticker, personas, adapters: list[Adapter], judges: list[Adapter], embedder, force=False, rerun=False, out_dir=Path("reports")) -> Path`: `prepare` → `embed_missing` → (`rerun`: delete analyses/rebuttals/judgements/verdicts for the snapshot) → `run_grid` → `build_pool` → `run_rebuttals` → `apply_rebuttals` → `precheck` → `run_judges` → `run_reconcile` → `mean_scores` → scoring → `build_verdict` → store `verdicts(run_kind='full')` → `render_verdict`. Catalysts: union of reconciled judges' lists, `claims.dedupe(key="what")`.
- `render.render_verdict(ticker, snapshot, verdict, analyses, rebuttals, metrics, flags, chunks_by_id, filings, out_dir) -> tuple[Path, Path]`: verdict sections per the spec (summary from the median judge; composite with dispersion, judge agreement and veto note; 5 × 3 grid; sub-scores; three most contested claims with both sides and judge notes; consensus claims; red flags; catalysts; earnings signals; all three judge summaries), then the Phase 2 per-agent sections (now 15, grouped by persona, each with its rebuttal responses) and the sources appendix. File names `reports/{T}/{sid}-full.md/.html`.
- CLI: `analyze --full` (adapters `AnthropicAdapter("claude-sonnet-5-5"), OpenAIAdapter("gpt-5.4"), GeminiAdapter("gemini-3.1-pro-preview")`, judges `AnthropicAdapter("claude-opus-5-5"), OpenAIAdapter("gpt-5.4"), GeminiAdapter("gemini-3.1-pro-preview")`), `--judges 1` (Anthropic judge only; agreement 1.0, median judge anthropic), `--providers` subset for the grid.

- [ ] Step 1: Failing tests (db; fixture-backed `prepare` as in `tests/test_pipeline.py` plus monkeypatched `fmp.fetch_all` returning the fixtures; three persona fakes and three judge fakes with canned, id-aware outputs): `test_full_run_call_counts_and_verdict` (15 + 15 + 3 + 3 calls; `verdicts` row exists; verdict keys; report files exist and contain "Judge agreement", the grid header and "contested"), `test_full_rerun_zero_calls`, `test_full_with_one_judge`.
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: First live run: `fundabot analyze AAPL --full --open`. Record per-round cost and wall time in README; eyeball the verdict for anything that reads wrong and ledger what you find.
- [ ] Step 6: Commit `feat: full multi-provider debate with three judges and verdict report`.
