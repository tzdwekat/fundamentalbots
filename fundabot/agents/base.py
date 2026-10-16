import asyncio
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


Call = tuple[Adapter, str, str, str, dict]  # (adapter, persona, system, user, schema)


def check_budget(conn: psycopg.Connection, planned: list[tuple]) -> None:
    """Fail closed before a round starts: `planned` holds (adapter, system, user) for every call the round will make."""
    spent = month_spend(conn)
    estimate = sum(a.estimate_cost(len(s) + len(u)) for a, s, u in planned)
    if planned and spent + estimate >= settings.fundabot_monthly_budget_usd:
        raise BudgetExceeded(f"month spend ${spent:.2f} + est ${estimate:.2f} exceeds budget ${settings.fundabot_monthly_budget_usd:.2f}")


async def run_batch(conn: psycopg.Connection, snapshot_id: str, calls: list[Call]) -> list[CompletionResult | Exception]:
    """Budget check for the whole batch (fail closed), concurrent calls, then one model_calls row per success.
    The DB connection is used only before and after the gather. Failures come back as exceptions in position."""
    spent = month_spend(conn)
    estimate = sum(a.estimate_cost(len(s) + len(u)) for a, _, s, u, _ in calls)
    if spent + estimate >= settings.fundabot_monthly_budget_usd:
        raise BudgetExceeded(f"month spend ${spent:.2f} + est ${estimate:.2f} exceeds budget ${settings.fundabot_monthly_budget_usd:.2f}")
    results = await asyncio.gather(*[a.complete(s, u, schema) for a, _, s, u, schema in calls], return_exceptions=True)
    with conn.cursor() as cur:
        for (adapter, persona, system, user, _), r in zip(calls, results):
            if isinstance(r, Exception):
                continue
            cur.execute(
                "INSERT INTO model_calls (snapshot_id, persona, provider, model, prompt_sha256, input_tokens, output_tokens, latency_ms, cost_usd) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (snapshot_id, persona, adapter.provider, r.model, hashlib.sha256((system + user).encode()).hexdigest(),
                 r.input_tokens, r.output_tokens, r.latency_ms, r.cost_usd),
            )
    conn.commit()
    return list(results)


async def call_logged(conn: psycopg.Connection, adapter: Adapter, snapshot_id: str, persona: str, system: str, user: str, schema: dict) -> CompletionResult:
    """One call through run_batch; re-raises the adapter's exception."""
    (r,) = await run_batch(conn, snapshot_id, [(adapter, persona, system, user, schema)])
    if isinstance(r, Exception):
        raise r
    return r
