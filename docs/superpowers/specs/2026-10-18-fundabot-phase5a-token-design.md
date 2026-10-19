# Fundabot Phase 5a: cut the cost per analysis

2026-10-18. Tariq: "a dollar per call seems a bit excessive." Measured on AAPL (2026-10-18): each persona call sends
~56k tokens by our count (~85k by Anthropic's): 24.9k of metrics table (786 lines, 325 of them raw XBRL line items the
agents rarely cite) and 30.6k of filing excerpts (39 chunks). Across the five personas 19 chunks (~14k tokens) are
identical, and 14 of the 98 distinct chunks are near-duplicate boilerplate repeated across quarterly filings.

## Intent

Make the cheap run cost about a quarter of today's $1 and the full run about half of today's $5-6, without changing
what the agents can cite or weakening the grounding, so the web app in Phase 5b can add its own calls on top.

## Goals

1. Persona prompts shrink to ~30k tokens: the metrics table shows only the metric ids the report groups print
   (`table.GROUPS`) at the last 5 fiscal years, last 4 quarters and `current`; raw XBRL base items appear only for
   fiscal years; the excerpt budget drops from 30k to 20k tokens after near-duplicate removal.
2. Every provider call carries the shared inputs (snapshot header, metrics, red flags, base-query excerpts) as a
   stable prefix ahead of anything persona-specific, and the Anthropic adapter marks that prefix for prompt caching;
   OpenAI and Gemini cache such prefixes automatically. The five persona calls on one provider therefore pay full
   price once and the cached rate four times.
3. `model_calls` records cached input tokens separately and costs them at the cached rate, so the saving is visible
   in the log and the README's measured figures are honest.
4. A `--tier screen` option runs the persona round on each provider's cheapest tier (`claude-haiku-5-5`,
   `gpt-5.4-mini`, `gemini-3.8-flash`) for watchlist screening; the default tier is unchanged.
5. Nothing an agent could cite before becomes uncitable: a metric id outside the shown set still resolves in grounding
   (the snapshot's full table remains the evidence universe), and the appendix still lists every cited id.

## Non-goals

- Caching across providers or across days (the snapshot changes daily by design).
- Caching the rebuttal and judge rounds (their inputs differ per agent; the shared share is small).
- Batch APIs (still deferred until the universe scan exists).
- Changing persona prompts, personas, or the judge.

## Anti-goals

- Hiding a metric the agents would have needed. The pruned set must contain every id the Phase 2 and 3 reports group,
  all red-flag evidence ids, and every metric cited by the agents in the stored AAPL and NVDA runs (checked by test).
- A cache that silently never hits. The call log must show cached tokens, and the end-to-end test asserts the prefix
  is byte-identical across the five persona messages.

## Constraints

- Anthropic: `cache_control: {"type": "ephemeral"}` on the shared system block; minimum cacheable prefix 1,024+ tokens
  (ours is ~25k); cache write 1.25x, read 0.1x of input price. Read from `usage.cache_read_input_tokens` and
  `usage.cache_creation_input_tokens`.
- OpenAI Responses API: automatic prefix caching above 1,024 tokens; `usage.input_tokens_details.cached_tokens`;
  cached input for `gpt-5.4` is $0.25/M (90% off). The shared prefix must be at the start of `instructions`.
- Gemini 3.1 Pro: implicit caching above 4,096 tokens, discount applied automatically; `usage_metadata.cached_content_token_count`.
- Prices for `gpt-5.4-mini` (0.75/4.50) and `claude-haiku-5-5` (0.10/0.50) are already in the adapters.

## Design

### Message layout

`context.build_messages(ticker, snapshot, metrics, flags, base_chunks, persona_chunks, persona, notes) -> (prefix, system_tail, user)`:
- `prefix` (shared, identical for every persona on a snapshot): `SNAPSHOT`, `METRICS` (pruned), `RED_FLAGS`,
  `SHARED_EXCERPTS` (the base-query chunks), and the hard rules. Deterministic ordering (metric id, period; chunk id).
- `system_tail`: the persona role and bias, reads-most, output guide.
- `user`: `NOTES` and `PERSONA_EXCERPTS` (chunks from the persona's own queries not already in the shared set), then
  the instruction line "Write your analysis as JSON."
- `Adapter.complete(system, user, schema, prefix=None)`: Anthropic sends `system=[{text: prefix, cache_control}, {text: system_tail}]`;
  OpenAI and Gemini send `prefix + "\n\n" + system_tail` as the instruction/system text. `CompletionResult` gains
  `cached_input_tokens`; cost = (input − cached) × input price + cached × cached price + output × output price, with
  cached prices per model in each adapter's table (Anthropic 0.1x, OpenAI per its page, Gemini 0.25x of input as the
  documented implicit-cache rate at build time; verify from the pricing page and note the date).

### Pruning

- `context.select_metrics(metrics) -> list[Metric]`: ids in `table.GROUPS` at the periods from `select_periods`, plus
  base XBRL items only at fiscal-year periods, plus every id referenced by a red flag. Expected ~250 lines.
- `retrieval.query.retrieve(..., max_tokens=20000)` default; `dedupe_boilerplate(chunks)` drops a chunk whose first 200
  normalized characters match an already-kept chunk from a newer filing (keeps the most recent accession).
- Base-query retrieval happens once per grid run; persona retrieval excludes the shared ids before the cap.

### Screening tier

`--tier screen` maps each provider to its cheapest model for the persona round only; rebuttal/judge rounds keep their
models. The weekly scan plist stays on the default tier; Tariq can switch it by editing the plist or passing the flag.

### Measurement

`fundabot cost TICKER` prints the last run's calls grouped by round with input, cached, output tokens and USD. The
README records AAPL cheap and full costs before and after.

## Testing

Unit: `select_metrics` contains every `GROUPS` id present, red-flag evidence ids, and no quarterly raw XBRL items;
the AAPL/NVDA stored analyses' cited metric ids are all in the pruned set (fixture of cited ids captured from the DB);
`dedupe_boilerplate` keeps the newest of two near-identical chunks; `build_messages` returns a byte-identical prefix
for two personas; adapter tests assert the Anthropic system block carries `cache_control` and that cost uses the
cached rate from a stubbed usage; the end-to-end fake run asserts prefix identity across the five calls. Live: one
cheap AAPL run with `--rerun`, then `fundabot cost AAPL` showing cache reads on calls 2-5.

### Warm-up call (builder review)

A provider's cache is populated only once the first response has begun, so five simultaneous calls all miss (and on
Anthropic each pays the 1.25x write rate). `run_grid` therefore sends, per provider, the first persona call alone,
then the remaining four concurrently (`run_batch(..., warm_first=True)`). Providers still run in parallel. Expected
wall time for the cheap run: about 2.5 minutes instead of 2.

### Budget split, periods, allowlist (builder review)

- The excerpt budget is 20k tokens per persona in total: shared base-query chunks first (at most 12k, deduplicated,
  retrieved once per grid), then the persona's own chunks fill the remainder, excluding ids already in the shared set.
- `select_periods` (8 quarters) is unchanged for the judge; `select_metrics` uses the report's 4-quarter window, and
  `grounding.ground` takes its "shown" values from `select_metrics` so support means "shown to the agent".
- The shown set is defined by an allowlist in `context.py`, not by widening `table.GROUPS`: all GROUPS ids at the
  shown periods; raw XBRL base items at fiscal years only; TTM items, every `*_5y_*` statistic and the DCF ids at
  `current`; `*_cagr_3y/5y` at fiscal years; plus red-flag evidence ids. `tests/fixtures/cited_metric_ids.json`
  (captured 2026-10-18 from the stored AAPL and NVDA runs: 326 evidence ids, 104 metric ids) must resolve inside it.

## Decisions left open (Tariq)

None. Defaults: 20k excerpt budget (12k shared cap), screen tier models as listed, cached rates 0.1x of input for all
three providers (OpenAI $0.25/M and Gemini $0.20/M per third-party pricing pages on 2026-10-18; Anthropic 0.1x documented).
