"""Anthropic adapter: structured JSON via output_config.format (forced tool choice is rejected on Sonnet 5.5+)."""
import json
import time

import anthropic

from fundabot.agents.base import CompletionResult
from fundabot.config import settings

PRICES = {  # USD per million tokens (input, output), Anthropic first-party rates
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(self, model: str = "claude-sonnet-5-5", max_tokens: int = 8000):
        self.model, self.max_tokens = model, max_tokens
        headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else {}
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, default_headers=headers, timeout=120.0, max_retries=3)

    CACHED_RATE, WRITE_RATE, MIN_CACHE_CHARS = 0.1, 1.25, 1024 * 4  # cache reads 0.1x, writes 1.25x of input; ~1,024-token minimum prefix

    @staticmethod
    def cost(model: str, input_tokens: int, output_tokens: int, cached: int = 0, cache_write: int = 0) -> float:
        """input_tokens is the full prompt; cached and cache_write are the parts billed at their own rates."""
        pin, pout = PRICES.get(model, (10.0, 50.0))  # unknown model: assume the most expensive tier
        plain = max(0, input_tokens - cached - cache_write)
        return (plain / 1e6 * pin + cached / 1e6 * pin * AnthropicAdapter.CACHED_RATE + cache_write / 1e6 * pin * AnthropicAdapter.WRITE_RATE
                + output_tokens / 1e6 * pout)

    def estimate_cost(self, input_chars: int) -> float:
        return self.cost(self.model, input_chars // 4, self.max_tokens // 2)

    async def complete(self, system: str, user: str, schema: dict, prefix: str | None = None) -> CompletionResult:
        t0 = time.monotonic()
        if prefix and len(prefix) >= self.MIN_CACHE_CHARS:
            system_arg = [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": system}]
        else:
            system_arg = (prefix + "\n\n" + system) if prefix else system
        r = await self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system_arg,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        if r.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(r.stop_details, 'category', None)}")
        if r.stop_reason == "max_tokens":
            raise RuntimeError("output truncated at max_tokens")
        text = next(b.text for b in r.content if b.type == "text")
        cached = getattr(r.usage, "cache_read_input_tokens", 0) or 0
        written = getattr(r.usage, "cache_creation_input_tokens", 0) or 0
        total_in = r.usage.input_tokens + cached + written  # the API reports uncached input separately from cache reads/writes
        return CompletionResult(json.loads(text), total_in, r.usage.output_tokens, int((time.monotonic() - t0) * 1000),
                                self.cost(self.model, total_in, r.usage.output_tokens, cached, written), r.model, cached, written)
