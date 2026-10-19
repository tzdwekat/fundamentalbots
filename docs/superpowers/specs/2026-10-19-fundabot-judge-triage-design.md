# Fundabot: judge triage (screen panel, escalation panel)

2026-10-19. After Phase 5a the judge rounds are the largest line of a full run: $1.75 of $3.54 on AAPL (round 1 $1.08 for
three calls that each read 60-90k tokens of claims and evidence and write ~10k tokens of notes; round 2 $0.67 to re-send
the whole schema). Tariq: "could we use a lower model for the first round then round 2 a higher model then output the
unanimous decisions?"

## Intent

Cut the judge stage to roughly $0.70 per full run without weakening the verdict: a cheap panel scores everything, claims
the cheap panel agrees on and that do not decide the case pass through as the unanimous set, and only the rest go to the
strong panel with their evidence. The strong panel must always see evidence; it never merely averages the cheap panel's
opinions.

## Goals

1. **Round 1, screen panel.** Three cheap judges, one per provider (`claude-haiku-5-5`, `gpt-5.4-mini`, `gemini-3.8-flash`),
   receive exactly today's round-1 message (claims after rebuttal, red flags, metrics summary, consensus data, agent
   scores, evidence) and return today's schema.
2. **Escalation rule** (deterministic, in code). A claim escalates when any of: the screen judges' evidence_strength
   spread is ≥ 2; any screen judge gave materiality ≥ 4; the claim has refutations; fewer than `min(2, size of the
   screen panel)` screen judges scored it (so `--judges 1` escalates by materiality and refutation only, not by
   construction). Everything else is *unanimous*: its final strength and materiality are the screen panel's means, its spread the
   screen panel's spread, and its path is recorded as `screen`.
3. **Round 2, escalation panel.** Three strong judges (today's `JUDGE_MODELS`: `claude-opus-5-5`, `gpt-5.4`,
   `gemini-3.1-pro-preview`) receive the escalated claims only, with their evidence, refutations and concessions, the red
   flags, metrics summary, consensus data and agent scores as in round 1, plus two new blocks: `SCREEN_PANEL_SCORES` (each
   screen judge's strength/materiality/note per escalated claim) and `UNANIMOUS_CLAIMS` (id, agent, direction, text and the
   pass-through scores, no evidence) so the sub-scores and summary account for the whole pool. They return today's schema
   scoring the escalated claims only; scores for ids outside the escalated set are dropped with a warning. Final scores for
   escalated claims are the strong panel's means and spread; path `escalated`.
4. **Verdict inputs.** Sub-scores, catalysts and the summary come from the strong panel. Implied composites and judge
   agreement are computed per strong judge over the whole pool: that judge's scores on escalated claims plus the
   pass-through means on unanimous claims. The reconcile round (judges re-reading each other) is removed; disagreement
   among the strong panel is reported through per-claim spread and judge agreement as today.
5. **Caching and invalidation.** Screen outputs are stored as round 1 and strong outputs as round 2 in `judgements`, keyed
   by (provider, model, round) as today. A stored round-2 output carries the sorted list of escalated ids it was produced
   for (`_escalated`); it is reused only when that list equals the current one, so a change in round 1 (a judge that
   failed before and succeeds now) re-runs round 2, and round-2 rows written by the old reconcile pipeline never pass as
   strong-panel outputs.
6. **Degraded panels.** No screen judge succeeded: every claim escalates (the strong panel judges everything, as before
   the change, minus reconcile). No strong judge succeeded: no verdict, raise (as today). `--judges 1` keeps one judge
   per panel, both on Anthropic.
7. **Report.** The verdict header names both panels and prints "N of M claims escalated". Every claim in the consensus
   and contested lists shows which panel judged it. The verdict JSON stores `path` per claim under its `judge` dict and
   `escalated_ids`.
8. **A/B gate.** `fundabot judge-ab TICKER` re-runs only the judge stage on the latest snapshot's stored grid and rebuttals
   with the triage pipeline, stores the result as `run_kind = 'triage'` (the existing `full` verdict is untouched) and
   prints: composite and sub-scores before/after, judge agreement before/after, escalated count, claims whose
   consensus/contested classification changed, and the largest per-claim strength deltas between the pass-through screen
   means and the previous strong-panel means. `judge-ab` takes a snapshot id or the ticker's latest snapshot that has a
   `full` verdict and a stored grid; it never calls `prepare()` (a re-ingest would mint a new snapshot and turn the
   $0.70 A/B into a full run). Before the strong panel overwrites the old round-2 rows, `judge-ab` copies their per-claim
   means into the triage verdict as `baseline_claim_means`; a re-run reads the baseline from the stored triage verdict.
   The decision rule: ship when the triage `composite_score` (veto-capped, as reported) is within 1.5 points of the
   previous one on AAPL, NVDA and HALO, and no claim with materiality ≥ 4 moved between consensus and contested;
   otherwise lower `ESCALATE_MATERIALITY` to 3 and re-run. (The stored `dispersion` is pstdev/10 on the −10..10 scale,
   so "within dispersion" was ambiguous; 1.5 points is about half a persona standard deviation on the runs so far.)
9. **Cost visibility.** `fundabot cost` rows stay `judge:r1` and `judge:r2`; the README records the measured judge cost
   before and after on AAPL.

After this ships, `analyze --full` writes triage verdicts under `run_kind = 'full'` and `review` / `calibrate` read them;
the reconcile pipeline is gone, so old-style baselines exist only on snapshots judged before the change.

## Non-goals

- Caching the judge prompts across the three providers (their system prompts are identical but the saving is small
  next to the triage itself; revisit if the strong panel's input stays large).
- Changing the persona or rebuttal rounds, the scoring formulas, or the veto.
- Shortening judge notes.
- A fourth "tie-break" round.

## Anti-goals (what would make this a failure)

- A strong judge scoring a claim without its evidence.
- A unanimous claim that decides the case (materiality ≥ 4 by any screen judge) never seen by the strong panel.
- Reusing a round-2 output produced for a different escalated set.
- A verdict whose composite moves outside the previous dispersion band on the A/B without a recorded reason.

## Constraints

- Everything numeric stays in code: the escalation rule, the merge of panel scores, the implied composites.
- No schema migration: `judgements` and `verdicts` already carry what is needed; `_escalated` lives inside the stored
  output JSON, `run_kind = 'triage'` is a new value in an existing text column.
- Costs are logged per call as today; the cheap panel's models must be in each adapter's price table
  (`gpt-5.4-mini` and `gemini-3.8-flash` already are; `claude-haiku-5-5` is).
- Max output tokens for both panels: `JUDGE_MAX_TOKENS` (20,000); Gemini Flash's thinking tokens count against it.

## Decided

- `build_adapters` returns `(persona_adapters, screen_judges, judges)`; `analyze_full` takes both panels.
- The escalation thresholds are module constants in `fundabot/debate/judge.py`: `ESCALATE_SPREAD = 2`,
  `ESCALATE_MATERIALITY = 4`.
- `mean_scores` gains the path merge: `merge_panels(screen_means, strong_means, escalated_ids)`; `classify_claims` is
  unchanged (it reads `spread`, which is now the judging panel's spread).
- `judge-ab` reuses `analyze_full` with a `run_kind` parameter; the stored grid and rebuttals are cached, so it costs
  only the judge calls (about $0.70).

## Left to the builder

- Exact wording of the two new message blocks.
- Whether the strong panel's `UNANIMOUS_CLAIMS` block lists every unanimous claim or caps it (the pool is ~70 claims;
  one line each is fine).
- Where the per-claim panel label sits in the Markdown template.
