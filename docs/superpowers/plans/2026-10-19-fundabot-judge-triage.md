# Judge Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A cheap screen panel scores every claim; only disputed or case-deciding claims go to the strong panel with evidence; the verdict shows which panel judged each claim; an A/B command compares against stored verdicts.

**Architecture:** `judge.py` gains the deterministic escalation rule, the escalation-round message and a round runner that keys round-2 caching on the escalated set. `scoring.py` gains a merge of the two panels' means and an implied composite from means. `pipeline.analyze_full` takes both panels, drops reconcile, and accepts `run_kind` / `snapshot_id` so `judge-ab` can re-judge a stored snapshot. CLI builds both panels and prints the A/B table.

**Tech Stack:** Python 3.12, psycopg, typer, existing adapters and FakeAdapter.

**Spec:** docs/superpowers/specs/2026-10-19-fundabot-judge-triage-design.md

## Global Constraints

- No schema migration. `_escalated` lives in the stored round-2 output JSON; `run_kind = 'triage'` is a new value.
- Screen models: `claude-haiku-5-5`, `gpt-5.4-mini`, `gemini-3.8-flash`; strong models unchanged (`JUDGE_MODELS`). Both panels use `JUDGE_MAX_TOKENS` (20000).
- `ESCALATE_SPREAD = 2`, `ESCALATE_MATERIALITY = 4`; a claim also escalates when refuted or scored by fewer than `min(2, len(screen_panel))` screen judges.
- A strong judge is never asked to score a claim without that claim's evidence in the message.
- `venv`: `export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot` before `uv run`.

## Review Focus

1. A strong judge returns scores for ids outside the escalated set (it read them in `UNANIMOUS_CLAIMS`): they must be dropped, not merged (Task 2 test `test_escalation_round_drops_unescalated_scores`).
2. Round 1 changes between runs (a screen judge that failed now succeeds, so the escalated set differs): the cached round-2 output must not be reused (Task 2 test `test_round2_cache_keyed_on_escalated_set`).
3. No screen judge succeeds: every claim escalates and the strong panel still produces a verdict (Task 3 test `test_full_with_screen_panel_down_escalates_everything`).
4. `--judges 1`: the lone screen judge's claims are not all escalated by the "fewer than two judges" rule (Task 1 test `test_escalate_single_judge_panel`).
5. `judge-ab` on a ticker whose newest snapshot has no grid must pick the newest snapshot that has a `full` verdict, never call `prepare` (Task 4 test `test_judge_ab_picks_snapshot_with_verdict`).

---

### Task 1: Escalation rule and panel merge

**Files:** modify `fundabot/debate/judge.py` (after `mean_scores`), `fundabot/debate/scoring.py` (after `implied_composite`); tests `tests/test_judge.py`, `tests/test_scoring.py`.

**Interfaces:**
- Produces `judge.ESCALATE_SPREAD = 2`, `judge.ESCALATE_MATERIALITY = 4`.
- Produces `judge.escalate(screen: list[dict], pool: list[dict], panel_size: int) -> list[str]`: sorted claim ids from `pool` (non-withdrawn) that meet any rule; `screen` is the list of round-1 judgement dicts (`{"provider","model","round","output"}`). Spread and materiality come from `mean_scores(screen, ids)` plus the raw per-judge materiality (any judge ≥ 4). "Fewer than N judges" uses `n_judges < min(2, panel_size)`.
- Produces `judge.merge_panels(screen_means: dict, strong_means: dict, escalated: list[str]) -> dict`: per claim id the strong entry when escalated else the screen entry, each with `"path": "escalated" | "screen"`; an escalated id missing from `strong_means` falls back to the screen entry with path `"screen"` and `"fallback": True`.
- Produces `scoring.implied_from_means(pool, means: dict, revised) -> float` (same arithmetic as `implied_composite`, taking a means dict).

- [ ] **Step 1: Failing tests** in `tests/test_judge.py`:

```python
def jrow(provider, scores, mats=None):  # a stored round-1 judgement row
    out = jout(scores)
    for c in out["claims"]:
        c["materiality"] = (mats or {}).get(c["claim_id"], 3)
    return {"provider": provider, "model": "m", "round": 1, "output": out}

POOL = [{"id": i, "withdrawn": False, "refutations": [], "conceded_by": [], "agent": "bull/openai", "direction": "positive", "weight": 3, "text": i, "evidence": []} for i in ("a", "b", "c", "d", "e")]

def test_escalate_rules():
    screen = [jrow("anthropic", {"a": 4, "b": 2, "c": 4, "d": 4, "e": 4}, {"c": 4}),
              jrow("openai", {"a": 4, "b": 4, "c": 4, "d": 4}),
              jrow("gemini", {"a": 4, "b": 3, "c": 4, "d": 4})]
    pool = [dict(c) for c in POOL]
    pool[3]["refutations"] = [{"by": "bear/openai", "reason": "r", "evidence": []}]
    # b: spread 2; c: one judge materiality 4; d: refuted; e: scored by one judge only; a: unanimous
    assert judge.escalate(screen, pool, 3) == ["b", "c", "d", "e"]

def test_escalate_single_judge_panel():
    screen = [jrow("anthropic", {"a": 4, "b": 2}, {"b": 4})]
    assert judge.escalate(screen, POOL[:2], 1) == ["b"]  # one judge: no "fewer than two" escalation, materiality still counts

def test_merge_panels_paths_and_fallback():
    screen = {"a": {"evidence_strength": 4.0, "materiality": 3.0, "n_judges": 3, "spread": 0, "notes": []},
              "b": {"evidence_strength": 3.0, "materiality": 4.0, "n_judges": 3, "spread": 2, "notes": []},
              "c": {"evidence_strength": 2.0, "materiality": 4.0, "n_judges": 3, "spread": 0, "notes": []}}
    strong = {"b": {"evidence_strength": 5.0, "materiality": 5.0, "n_judges": 3, "spread": 1, "notes": ["n"]}}
    m = judge.merge_panels(screen, strong, ["b", "c"])
    assert m["a"]["path"] == "screen" and m["a"]["evidence_strength"] == 4.0
    assert m["b"]["path"] == "escalated" and m["b"]["evidence_strength"] == 5.0
    assert m["c"]["path"] == "screen" and m["c"]["fallback"] is True
```

and in `tests/test_scoring.py`:

```python
def test_implied_from_means_matches_implied_composite():
    pool, revised = <reuse the fixtures of test_implied_composite_and_agreement>
    jo = jout-like output scoring every pool claim 4
    means = {c["claim_id"]: {"evidence_strength": float(c["evidence_strength"])} for c in jo["claims"]}
    assert scoring.implied_from_means(pool, means, revised) == scoring.implied_composite(pool, jo, revised)
```

- [ ] **Step 2:** `uv run pytest tests/test_judge.py tests/test_scoring.py -q` → FAIL (`escalate` not defined).
- [ ] **Step 3:** Implement the three functions and the two constants. `escalate` uses `mean_scores(screen, [c["id"] for c in pool if not c["withdrawn"]])` for spread and n_judges and scans `screen[*]["output"]["claims"]` for materiality ≥ `ESCALATE_MATERIALITY`.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: judge escalation rule and panel merge`.

### Task 2: Escalation round (message, runner, cache keyed on the escalated set)

**Files:** modify `fundabot/debate/judge.py` (`build_escalation_message`, `_run_round` gains `escalated: list[str] | None`, `run_escalation` replaces `run_reconcile`; delete `build_reconcile_message`); tests `tests/test_judge.py` (replace `test_run_judges_and_reconcile_cache`).

**Interfaces:**
- Consumes Task 1 `escalate`, `mean_scores`.
- Produces `judge.build_escalation_message(escalated_pool, screen: list[dict], unanimous: list[tuple[dict, dict]], flags, metrics, earnings_available, evidence, revised) -> str`: `build_message(escalated_pool, ...)` text, then a `SCREEN_PANEL_SCORES` block (one line per escalated claim: `<id>: <provider> strength S materiality M note "…"` per judge) and an `UNANIMOUS_CLAIMS (scored by the screen panel; do not score these)` block (one line per claim: `<id> by <agent> (<direction>, weight W): <text> → strength S materiality M`), placed before `EVIDENCE`. `evidence` must contain only ids cited by the escalated claims and their refutations (the caller computes it).
- Produces `judge.run_escalation(conn, sid, judges, messages: dict[str, str], escalated: list[str]) -> list[dict]`: round 2; each stored output gets `"_escalated": escalated`; claim scores whose id is not in `escalated` are dropped with `log.warning` before validation; a cached round-2 row is reused only if `row["output"].get("_escalated") == escalated`.
- Produces `judge.run_judges` unchanged (round 1).

- [ ] **Step 1: Failing tests** (db):

```python
@pytest.mark.db
async def test_escalation_round_drops_unescalated_scores(db_conn):
    sid = snapshot.create_snapshot(...)
    a = FakeAdapter([jout({"a": 5, "z": 1})]); a.provider, a.model = "anthropic", "strong"
    r2 = await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    assert [c["claim_id"] for c in r2[0]["output"]["claims"]] == ["a"] and r2[0]["output"]["_escalated"] == ["a"] and r2[0]["round"] == 2

@pytest.mark.db
async def test_round2_cache_keyed_on_escalated_set(db_conn):
    sid = ...; a = FakeAdapter([jout({"a": 5}), jout({"a": 4, "b": 4})]); a.provider, a.model = "anthropic", "strong"
    await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    assert a.calls == 1  # same set: cached
    r = await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a", "b"])
    assert a.calls == 2 and r[0]["output"]["_escalated"] == ["a", "b"]

@pytest.mark.db
async def test_old_reconcile_row_is_not_reused_as_strong_panel(db_conn):
    sid = ...; insert a judgements row (anthropic, strong, round 2) whose output has no _escalated
    a = FakeAdapter([jout({"a": 5})]); ...
    await judge.run_escalation(db_conn, sid, [a], {"anthropic": "MSG"}, ["a"])
    assert a.calls == 1

def test_escalation_message_blocks():
    msg = judge.build_escalation_message([POOL[1]], [jrow("anthropic", {"b": 2}), jrow("openai", {"b": 4})],
                                         [(POOL[0], {"evidence_strength": 4.0, "materiality": 3.0})], [], [], False, {"metric:revenue@FY2024": "…"}, {"bull/openai": (2, 0.6)})
    assert "SCREEN_PANEL_SCORES" in msg and "anthropic strength 2" in msg and "UNANIMOUS_CLAIMS" in msg and "a by bull/openai" in msg
    assert msg.index("UNANIMOUS_CLAIMS") < msg.index("EVIDENCE") and "b by bull/openai" in msg.split("SCREEN_PANEL_SCORES")[0]
```

- [ ] **Step 2:** FAIL (`run_escalation` not defined).
- [ ] **Step 3:** Implement; delete `run_reconcile`, `build_reconcile_message` and the old cache test.
- [ ] **Step 4:** `uv run pytest tests/test_judge.py -q` → PASS.
- [ ] **Step 5:** Commit `feat: escalation round keyed on the escalated set`.

### Task 3: Pipeline: two panels, no reconcile, run_kind and snapshot_id

**Files:** modify `fundabot/orchestrate/pipeline.py` (`analyze_full`), `fundabot/report/render.py` (`render_verdict` output name), `fundabot/report/templates/verdict.md.j2`; tests `tests/test_full_pipeline.py`, `tests/test_review_fixes.py` (callers).

**Interfaces:**
- Produces `analyze_full(conn, ticker, personas, adapters, screen_judges: list, judges: list, embedder, force=False, rerun=False, out_dir=Path("reports"), guidance_adapter=None, run_kind: str = "full", snapshot_id: str | None = None) -> Path`. With `snapshot_id` given: no `prepare`, no guidance, that snapshot is used. Judge stage: `round1 = run_judges(screen_judges, message)`; `escalated = escalate(round1, judged, len(screen_judges))` or every judged id when `round1` is empty; `screen_means = mean_scores(round1, ids)`; evidence for the escalation message restricted to ids cited by escalated claims/refutations; `strong = run_escalation(judges, messages, escalated)`; raise when `strong` is empty; `means = merge_panels(screen_means, mean_scores(strong, escalated), escalated)`; `implied = [implied_from_means(judged, {**{k: v for k, v in means.items() if v["path"] == "screen"}, **{c["claim_id"]: {"evidence_strength": float(c["evidence_strength"])} for c in j["output"]["claims"]}}, revised) for j in strong]`; `build_verdict(..., reconciled=strong, ...)`. Verdict gains `escalated_ids`, `screen_judges: [{"provider","model"}]`, `n_claims` (len(judged)); `judges` lists the strong panel. Verdict stored under `run_kind`; report file `<sid>-<run_kind>.html`.
- `render_verdict(..., run_kind="full")` names the file `f"{sid}-{run_kind}.html"`.
- Template: header `· screen panel {{ screen_judges }} · escalation panel {{ judges }} · {{ escalated_ids|length }} of {{ n_claims }} claims escalated`; each consensus/contested claim line ends with `· {{ c.judge.path }} panel`.

- [ ] **Step 1: Failing tests** in `tests/test_full_pipeline.py`. Change the judge fake: it scores ids from `CLAIMS (after rebuttal)` lines only (never from `UNANIMOUS_CLAIMS`), with materiality 3 for every claim except ids containing `bear` get materiality 4 (so some claims escalate deterministically). Add a `screen_fake(provider)` = same fake with model `f"{provider}-screen"`.

```python
async def test_full_run_call_counts_and_verdict(...):
    screen = [screen_fake(p) for p in PROVIDERS]; judges = [rebuttal_or_judge(p) for p in PROVIDERS]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, FakeEmbedder(), out_dir=tmp_path)
    assert calls(personas) == 30 and calls(screen) == 3 and calls(judges) == 3
    v = stored verdict; assert v["escalated_ids"] and set(v["escalated_ids"]) < {c["id"] for c in v["consensus_claims"] + v["contested_claims"]} or True
    assert all(c["judge"]["path"] in ("screen", "escalated") for c in v["consensus_claims"] + v["contested_claims"])
    assert any(c["judge"]["path"] == "escalated" for c in ...) and any(c["judge"]["path"] == "screen" for c in ...)
    assert v["screen_judges"][0]["model"] == "anthropic-screen" and v["judges"][0]["model"] == "anthropic-judge"
    md/html contains "claims escalated" and "escalation panel"

async def test_full_with_screen_panel_down_escalates_everything(...):
    screen = [FakeAdapter([RuntimeError("down")]) with provider/model set]; judges = [rebuttal_or_judge("anthropic")]
    html = await pipeline.analyze_full(conn, "AAPL", PERSONAS, personas, screen, judges, ...)
    v = stored verdict; assert len(v["escalated_ids"]) == v["n_claims"] and all(c["judge"]["path"] == "escalated" for c in consensus+contested)

async def test_full_with_one_judge(...):  # update: screen=[screen_fake("anthropic")], judges=[rebuttal_or_judge("anthropic")]; calls(screen)==1, calls(judges)==1

async def test_full_caches_every_round(...):  # update signature; second run adds no calls; rerun doubles

async def test_snapshot_id_skips_prepare_and_writes_run_kind(...):
    first full run; then monkeypatch pipeline.prepare to raise; html = await analyze_full(conn, "AAPL", ..., snapshot_id=sid, run_kind="triage", rerun=False)
    assert html.name.endswith("-triage.html") and a verdicts row with run_kind='triage' exists and the 'full' row is unchanged
```

Update `tests/test_review_fixes.py` callers (`test_full_with_no_surviving_judge_raises_and_stores_no_verdict`, `test_new_cells_invalidate_rebuttals_and_judgements`, `test_catalysts_and_summaries_are_checked`) to pass a screen panel too; the no-surviving-judge test keeps a working screen panel and a failing strong panel and must still raise.

- [ ] **Step 2:** FAIL (signature).
- [ ] **Step 3:** Implement pipeline, render and template changes.
- [ ] **Step 4:** `uv run pytest tests/test_full_pipeline.py tests/test_review_fixes.py -q` → PASS; then `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: screen and escalation panels in the full pipeline`.

### Task 4: CLI panels and judge-ab

**Files:** modify `fundabot/cli.py` (`SCREEN_JUDGE_MODELS`, `build_adapters`, `analyze`, `scan_runner`, new `judge_ab_table`, new command `judge-ab`), `README.md`; tests `tests/test_review_fixes.py` (`test_phase2_command_builds_no_judges_without_gemini_key`, `test_screen_tier_only_affects_personas`), new `tests/test_judge_ab.py`.

**Interfaces:**
- `SCREEN_JUDGE_MODELS = {"anthropic": "claude-haiku-5-5", "openai": "gpt-5.4-mini", "gemini": "gemini-3.8-flash"}`.
- `build_adapters(full, providers, model, judges, tier="default") -> (adapters, screen_judges, judge_adapters)`; `--judges 1` → one Anthropic adapter per panel; not full → `(adapters, [], [])`.
- `judge_ab_table(old: dict, new: dict) -> str`: lines `composite  <old> -> <new>  (Δ …)`, one per sub-score, `judge_agreement`, `escalated  <n> of <m>`, `reclassified: <id> consensus->contested (materiality X)` per changed id, and the 10 largest `|screen mean − baseline mean|` over `new["baseline_claim_means"]` ∩ screen-path claims, then a final line `verdict: ship` / `verdict: widen (reason)` applying the spec's rule (1.5 points; no reclassified claim with materiality ≥ 4).
- Command `judge-ab TICKER [--snapshot SID]`: picks `--snapshot` or the newest snapshot of the ticker that has a `full` verdict; computes `baseline = judge.mean_scores(old round-2 rows without _escalated, their claim ids)`; if a `triage` verdict already exists its `baseline_claim_means` wins; runs `analyze_full(..., run_kind="triage", snapshot_id=sid)`; `UPDATE verdicts SET verdict = verdict || jsonb({"baseline_claim_means": baseline})` for the triage row; prints `judge_ab_table`.

- [ ] **Step 1: Failing tests:** `test_screen_tier_only_affects_personas` asserts the 3-tuple and `[j.model for j in screen] == ["claude-haiku-5-5", "gpt-5.4-mini", "gemini-3.8-flash"]` on both tiers; `tests/test_judge_ab.py::test_judge_ab_table_rule` builds two verdict dicts (old composite 2.6, new 1.5 → ship; new 0.9 → widen; a reclassified materiality-4 claim → widen) and asserts the final line; `test_judge_ab_picks_snapshot_with_verdict` (db): two snapshots for AAPL, the older with a `full` verdict row, newer without → the helper `latest_with_verdict(conn, "AAPL") == older`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** README: judge-triage paragraph under costs (how the panels work, the escalation rule, `judge-ab`). Commit `feat: screen/escalation judge panels in the CLI; judge-ab comparison`.

### Task 5: Live A/B and measured cost

- [ ] **Step 1:** `fundabot judge-ab AAPL`, `fundabot judge-ab NVDA`, `fundabot judge-ab HALO`; save the three tables to the plan workspace.
- [ ] **Step 2:** Apply the decision rule. If any says `widen`, set `ESCALATE_MATERIALITY = 3`, re-run the failing ticker(s), record both results.
- [ ] **Step 3:** `fundabot cost AAPL`: judge rows for the triage run (from `model_calls` after the run's start) vs the $1.75 before. README: measured judge cost and escalation counts per ticker. Commit `docs: judge triage measured`.

### Task 6: Provider profiles (switch the whole debate between providers)

Tariq, 2026-10-19: "due to the expenses lets switch to a multi provider format to a way we can switch between them using
connectors (keep the personas and judging rounds etc.)". Read as: the adapters are the connectors; a run-time profile
picks which provider(s) fill each role, with personas, rebuttals and both judge rounds unchanged.

**Files:** create `fundabot/profiles.yaml`, `fundabot/profiles.py`; modify `fundabot/cli.py` (`build_adapters` reads a profile; `--profile` on `analyze`, `scan`, `worker`), `README.md`; tests `tests/test_profiles.py`.

**Interfaces:**
- `profiles.yaml`:
  ```yaml
  multi:      {personas: {anthropic: claude-sonnet-5-5, openai: gpt-5.4, gemini: gemini-3.1-pro-preview},
               screen:   {anthropic: claude-haiku-5-5, openai: gpt-5.4-mini, gemini: gemini-3.8-flash},
               escalation: {anthropic: claude-opus-5-5, openai: gpt-5.4, gemini: gemini-3.1-pro-preview},
               guidance: {anthropic: claude-haiku-5-5}}
  anthropic:  {personas: {anthropic: claude-sonnet-5-5}, screen: {anthropic: claude-haiku-5-5}, escalation: {anthropic: claude-opus-5-5}, guidance: {anthropic: claude-haiku-5-5}}
  openai:     {personas: {openai: gpt-5.4}, screen: {openai: gpt-5.4-mini}, escalation: {openai: gpt-5.4}, guidance: {openai: gpt-5.4-mini}}
  gemini:     {personas: {gemini: gemini-3.1-pro-preview}, screen: {gemini: gemini-3.8-flash}, escalation: {gemini: gemini-3.1-pro-preview}, guidance: {gemini: gemini-3.8-flash}}
  ```
- `profiles.load(name: str) -> dict` (raises `KeyError` naming the available profiles); `profiles.ROLES = ("personas", "screen", "escalation", "guidance")`; every profile must define every role (validated on load).
- `cli.build_adapters(full, providers, model, judges, tier="default", profile="multi") -> (adapters, screen_judges, judge_adapters, guidance_adapter)`: the profile is the source of models; `--providers` narrows the persona set within the profile; `--tier screen` substitutes the screen models for the persona role; `--model` overrides the Anthropic persona model (kept for compatibility); `--judges 1` keeps the first provider of each judge role. The guidance adapter comes from the profile's `guidance` role (today it is hard-coded to Anthropic Haiku in two places).
- `--profile` default `multi`; the README documents the per-profile cost.

- [ ] **Step 1: Failing tests** `tests/test_profiles.py`: `test_every_profile_defines_every_role`; `test_unknown_profile_lists_choices`; `test_build_adapters_single_provider_profile` (`build_adapters(True, "anthropic", None, 3, profile="anthropic")` → personas `[claude-sonnet-5-5]`, screen `[claude-haiku-5-5]`, escalation `[claude-opus-5-5]`, guidance model `claude-haiku-5-5`, all provider `anthropic`); `test_build_adapters_multi_matches_today` (models equal `JUDGE_MODELS` / `SCREEN_JUDGE_MODELS` / the persona defaults).
- [ ] **Step 2:** FAIL. **Step 3:** implement; `JUDGE_MODELS` and `SCREEN_JUDGE_MODELS` become views of the `multi` profile. **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Live: `fundabot analyze AAPL --full --profile anthropic --rerun` (expected about $1); `fundabot cost AAPL`; README. Commit `feat: provider profiles; whole debate switchable per provider`.
