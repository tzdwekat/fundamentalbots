# fundabot

Sourced, deterministic fundamental analysis of US-listed stocks with persona agents.
See `docs/architecture.md` and `docs/superpowers/specs/` for the design.

## Setup

```
docker compose up -d        # Postgres 16 + pgvector on localhost:5433
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/fundabot   # see note below
uv sync
cp .env.example .env        # fill in keys
uv run fundabot init-db
```

**Why the venv lives outside the repo:** on this machine macOS marks every file under `./.venv` as
hidden within seconds, and Python 3.12 skips hidden `.pth` files, which silently breaks the editable
install (`ModuleNotFoundError: fundabot`). Keeping the environment under `~/.venvs` avoids it. Put the
`export` in your shell profile.

## Commands

```
uv run fundabot init-db                      # create/upgrade the schema
uv run fundabot ingest AAPL MSFT             # facts, prices, filing text; builds a snapshot with metrics and red flags
uv run fundabot metrics AAPL [--json]        # sourced metrics table (every value has a tag, filing and formula)
uv run fundabot verify AAPL                  # cross-check 4 statement items against yfinance (diagnostic)
uv run fundabot analyze NVDA AMD [--open]    # five persona agents on Claude -> reports/NVDA/<snapshot>.html
    --personas bull,bear  --model claude-sonnet-5-5  --force (refetch data)  --rerun (ignore cached analyses)
```

Keys: Phase 1 commands need none. `analyze` needs `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (embeddings).
A user-scoped Anthropic key (`sk-ant-usr-...`) also needs `ANTHROPIC_WORKSPACE_ID` (Console -> Settings -> Workspaces).

Costs: every model call is logged in `model_calls` with tokens and USD; `FUNDABOT_MONTHLY_BUDGET_USD` is a hard cap
(the adapter refuses calls once the month's logged spend reaches it). Measured on 2026-10-15 for AAPL, five personas on
`claude-sonnet-5-5`: about 84k input and 2.5-3.6k output tokens per persona, $0.98 per ticker, 2 minutes wall time
(personas run sequentially). Embeddings for a ticker's 13 filings: about $0.006 once. A re-run on the same snapshot costs $0.

## What the report contains

Red flags from the rules engine; a score strip (per persona, plus an unweighted mean clearly labelled as such: the
evidence-weighted composite, rebuttal round and judge are Phase 3); each persona's thesis and claims with evidence ids,
claims dropped for unresolved evidence, numbers flagged when they match neither a metric nor a retrieved filing
passage; the metrics table; DCF assumptions; and a sources appendix for every cited metric and chunk.

## Accuracy notes

See `docs/handcheck-2026-10.md` for the 20-ticker check and `docs/superpowers/specs/` for the period and tag rules.
Metrics are never guessed: a missing line item is `null` with a reason. Derived values (Q4 = FY - Q1..Q3, Q2/Q3 from
year-to-date cash-flow statements, composed D&A, reported-total debt, diluted-share fallback) carry flags that travel
into the agent inputs and the report.
