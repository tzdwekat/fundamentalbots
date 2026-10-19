# Fundabot Phase 5a Implementation Plan: cost per analysis

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut the cheap run to about a quarter of its cost and the full run to about half by pruning the prompt, deduplicating excerpts, and caching a shared prefix across the five persona calls per provider, with cached tokens visible in the call log and a screening tier available.

**Architecture:** `context.build_messages` splits every persona prompt into a byte-identical shared prefix and a persona tail; adapters accept the prefix (Anthropic marks it with `cache_control`); `run_batch` warms the cache with one call per provider before fanning out; `select_metrics` and `dedupe_boilerplate` shrink the inputs; `model_calls` records cached tokens.

**Tech Stack:** unchanged.

**Spec:** `docs/superpowers/specs/2026-10-18-fundabot-phase5a-token-design.md`.

## Global Constraints

- The shared prefix must be byte-identical across the five personas of one grid run (deterministic ordering).
- Cached-token accounting: `cost = (input − cached) × in_price + cached × cached_price + output × out_price`; cached price 0.1x input for all providers; Anthropic cache writes charged at 1.25x on `cache_creation_input_tokens`.
- `select_periods` unchanged; `select_metrics` is the single definition of "shown" for prompts and grounding.
- Every id in `tests/fixtures/cited_metric_ids.json` resolves against `select_metrics` output for a snapshot that has those metrics (the fixture lists `metric:id@period`; period-less ids resolve if the id is shown at any period).
- 227 existing tests keep passing; migration `006_cached_tokens.sql` adds `cached_input_tokens int default 0` and `cache_write_tokens int default 0` to `model_calls`.

## Review Focus

1. A persona whose own retrieval returns only chunks already in the shared set must still get a non-empty, valid user message (Task 2 test `test_persona_with_no_unique_chunks`).
2. A metric an agent cites that lives outside the shown set (e.g. `metric:ocf@Q3FY2026`) must still resolve in grounding as evidence (it exists in the snapshot) even though its *value* no longer counts as "shown" (Task 2 test `test_unshown_metric_still_resolves_as_evidence`).
3. If the warm-up call fails (provider down), the other four must still be attempted rather than skipped (Task 3 test `test_warm_first_failure_still_runs_rest`).
4. The Anthropic adapter must not send `cache_control` when the prefix is empty or shorter than 1,024 tokens of text (API rejects/ignores; keep the request valid) (Task 3 test `test_anthropic_no_cache_control_for_short_prefix`).
5. `--tier screen` must change persona models only; judges and guidance keep theirs (Task 4 test `test_screen_tier_only_affects_personas`).

---

### Task 1: Pruned metrics, excerpt dedupe, split budget

**Files:** modify `fundabot/agents/context.py`, `fundabot/retrieval/query.py`, `fundabot/eval/grounding.py`. Tests: `tests/test_context_pruning.py`, extend `tests/test_retrieval.py`.

**Interfaces:**
- `context.select_metrics(metrics: list[Metric], flags: list[RedFlag] = ()) -> list[Metric]` per the spec allowlist; periods from `table.columns_for` (5 FY, 4 Q, current).
- `query.dedupe_boilerplate(chunks: list[Chunk], newest_first_accessions: list[str]) -> list[Chunk]`: drop a chunk whose first 200 normalized chars equal a kept chunk's; keep the one from the newest accession (order given by `filings.filed` desc; transcripts `T…` count as newest by call date). Signature: `dedupe_boilerplate(chunks, accession_rank: dict[str, int])` where lower rank = newer.
- `query.retrieve(..., max_tokens=20000, exclude_ids: set[str] = frozenset())`.
- `grounding.ground` uses `select_metrics(metrics)` values for number support (evidence resolution unchanged: any metric in the snapshot).

- [ ] Step 1: Failing tests: `test_select_metrics_allowlist` (GROUPS ids at shown periods present; `ocf@Q3FY2026` absent; `ocf@FY2025` present; `fcf_ttm@current`, `pe_5y_max@current`, `revenue_cagr_3y@FY2025` present; red-flag evidence id present even if outside), `test_cited_ids_fixture_resolves` (load AAPL fixture snapshot metrics via `periods`/`metrics`/`valuation` on the test fixtures, assert every fixture id whose metric exists in that snapshot is in `select_metrics` output), `test_dedupe_boilerplate_keeps_newest`, `test_retrieve_excludes_ids` (db), `test_ground_uses_shown_values` (a number matching only an unshown quarter's value is unsupported).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: pruned metrics, excerpt dedupe, split retrieval budget`.

---

### Task 2: Prefix / tail / user message layout

**Files:** modify `fundabot/agents/context.py`, `fundabot/agents/runner.py` (shared retrieval once per grid; persona retrieval with `exclude_ids`), `fundabot/agents/base.py` (`Call` gains `prefix`). Tests: `tests/test_context_pruning.py` (extend).

**Interfaces:**
- `context.build_messages(ticker, snapshot, metrics, flags, shared_chunks, persona_chunks, persona: dict, notes) -> tuple[str, str, str]` = `(prefix, system_tail, user)`; `prefix` = SNAPSHOT + METRICS (select_metrics, sorted) + RED_FLAGS + SHARED_EXCERPTS + HARD_RULES; `system_tail` = role/bias/reads_most/OUTPUT_GUIDE; `user` = NOTES + PERSONA_EXCERPTS + "Write your analysis as JSON following the schema."; `build_user_message` is kept as a thin wrapper for existing callers (rebuttal evidence text uses `metric_line` only).
- `Call = (adapter, persona, system, user, schema, prefix)`; `run_batch` passes `prefix` to `adapter.complete(system, user, schema, prefix=prefix)`; the prompt hash covers `prefix + system + user`.
- `run_grid`: `shared = dedupe(retrieve(base queries, max_tokens=12000))` once; per persona `own = retrieve(persona queries, max_tokens=20000 − shared_tokens, exclude_ids=shared ids)`; `chunk_ids` stored = shared + own.

- [ ] Step 1: Failing tests: `test_prefix_identical_across_personas` (two personas, same snapshot → identical prefix, different tails), `test_persona_with_no_unique_chunks` (user message still valid and says "no additional excerpts"), `test_unshown_metric_still_resolves_as_evidence`, `test_grid_stores_shared_plus_own_chunk_ids` (db; FakeEmbedder).
- [ ] Step 2–5: FAIL, implement, PASS, commit `feat: shared prefix and persona tail message layout`.

---

### Task 3: Adapter caching, cached-token accounting, warm-up call

**Files:** modify `fundabot/agents/adapters/{anthropic,openai,gemini,fake}.py`, `fundabot/agents/base.py`, `fundabot/db/schema/006_cached_tokens.sql`. Tests: `tests/test_adapters.py`, `tests/test_adapters_openai_gemini.py` (extend), `tests/test_grid.py` (extend).

**Interfaces:**
- `CompletionResult` gains `cached_input_tokens: int = 0`, `cache_write_tokens: int = 0`.
- `AnthropicAdapter.complete(system, user, schema, prefix=None)`: `system=[{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": system}]` when `prefix` has ≥ 1,024 tokens by a cheap estimate (`len(prefix) // 4 >= 1024`), else plain concatenation; reads `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens`; `cost()` takes `(model, input, output, cached=0, cache_write=0)` with cached at 0.1x and write at 1.25x.
- `OpenAIAdapter`: `instructions = prefix + "\n\n" + system`; cached from `usage.input_tokens_details.cached_tokens` (attribute access guarded); cost cached at 0.1x.
- `GeminiAdapter`: `system_instruction = prefix + "\n\n" + system`; cached from `usage_metadata.cached_content_token_count` (guarded); cached at 0.1x.
- `FakeAdapter.complete(..., prefix=None)` accepts and records `self.last_prefix`.
- `run_batch(conn, sid, calls, warm_first=False)`: when `warm_first`, await the first call alone, then gather the rest; a failed warm-up does not stop the rest. `model_calls` insert includes the two new columns. `run_grid` passes `warm_first=True`.

- [ ] Step 1: Failing tests: `test_anthropic_sends_cached_system_block` (stub `messages.create` captures kwargs → two system blocks, first with cache_control), `test_anthropic_no_cache_control_for_short_prefix`, `test_anthropic_cost_with_cached_and_write_tokens`, `test_openai_prefix_in_instructions_and_cached_cost`, `test_gemini_prefix_in_system_instruction_and_cached_cost`, `test_run_batch_warm_first_order` (db; fakes record call start order: first completes before others start), `test_warm_first_failure_still_runs_rest`, `test_model_calls_records_cached_tokens` (db).
- [ ] Step 2–5: FAIL, implement, `init-db`, PASS, commit `feat: prompt caching with warm-up call and cached-token accounting`.

---

### Task 4: Screening tier, cost command, measurement

**Files:** modify `fundabot/cli.py`, `README.md`. Tests: `tests/test_review_fixes.py` (extend with `test_screen_tier_only_affects_personas`), `tests/test_cost_cmd.py`.

**Interfaces:**
- `cli.build_adapters(full, providers, model, judges, tier="default")`: `tier="screen"` maps persona adapters to `claude-haiku-5-5` / `gpt-5.4-mini` / `gemini-3.8-flash`; judges unchanged. `analyze`, `scan` and `worker` accept `--tier`.
- `cli.cost_table(conn, sid) -> str`: rows per round (`persona`, `rebuttal`, `judge:r1`, `judge:r2`, `guidance`, `embed`) with calls, input, cached, write, output tokens and USD, plus a total; `fundabot cost T` prints it for the latest snapshot.

- [ ] Step 1: Failing tests: `test_screen_tier_only_affects_personas`, `test_cost_table_groups_rounds` (db; insert rows, assert grouping and total).
- [ ] Step 2–4: FAIL, implement, PASS.
- [ ] Step 5: Live measurement: `fundabot analyze AAPL --rerun` then `fundabot cost AAPL`; record before/after in the README (before: $1.00 cheap, $5.59 full). If cache reads are zero on calls 2-5 for any provider, stop and investigate before committing the README numbers.
- [ ] Step 6: Commit `feat: screening tier and cost command; measured savings`.
