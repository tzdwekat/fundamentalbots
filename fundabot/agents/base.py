import hashlib
from dataclasses import dataclass
from typing import Protocol

import psycopg

from fundabot.config import settings


@dataclass
class CompletionResult:
    output: dict
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float
    model: str


class Adapter(Protocol):
    provider: str
    model: str

    async def complete(self, system: str, user: str, schema: dict) -> CompletionResult: ...

    def estimate_cost(self, input_chars: int) -> float: ...


class BudgetExceeded(RuntimeError):
    pass


def month_spend(conn: psycopg.Connection) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT coalesce(sum(cost_usd), 0) FROM model_calls WHERE created_at >= date_trunc('month', now())")
        return float(cur.fetchone()[0])


async def call_logged(conn: psycopg.Connection, adapter: Adapter, snapshot_id: str, persona: str, system: str, user: str, schema: dict) -> CompletionResult:
    """Budget check (fail closed), then the call, then a model_calls row."""
    spent = month_spend(conn)
    estimate = adapter.estimate_cost(len(system) + len(user))
    if spent + estimate >= settings.fundabot_monthly_budget_usd:
        raise BudgetExceeded(f"month spend ${spent:.2f} + est ${estimate:.2f} exceeds budget ${settings.fundabot_monthly_budget_usd:.2f}")
    result = await adapter.complete(system, user, schema)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_calls (snapshot_id, persona, provider, model, prompt_sha256, input_tokens, output_tokens, latency_ms, cost_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (snapshot_id, persona, adapter.provider, result.model, hashlib.sha256((system + user).encode()).hexdigest(),
             result.input_tokens, result.output_tokens, result.latency_ms, result.cost_usd),
        )
    conn.commit()
    return result
