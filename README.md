# fundabot

Sourced, deterministic fundamental analysis of US-listed stocks with persona agents.
See `docs/architecture.md` and `docs/superpowers/specs/` for the design.

## Setup

```
docker compose up -d        # Postgres 16 + pgvector on localhost:5433
uv sync
cp .env.example .env        # fill in keys
uv run fundabot init-db
```
