"""OpenAI adapter: Responses API with a strict json_schema text format."""
import json
import time

from openai import AsyncOpenAI

from fundabot.agents.base import CompletionResult
from fundabot.config import settings

PRICES = {  # USD per million tokens (input, output), openai.com pricing 2026-10-15
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-pro": (30.0, 180.0),
}


class OpenAIAdapter:
    provider = "openai"

    def __init__(self, model: str = "gpt-5.4", max_tokens: int = 8000):
        self.model, self.max_tokens = model, max_tokens
        self._client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=180.0, max_retries=3)

    @staticmethod
    def cost(model: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = PRICES.get(model, max(PRICES.values()))
        return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout

    def estimate_cost(self, input_chars: int) -> float:
        return self.cost(self.model, input_chars // 4, self.max_tokens // 2)

    async def complete(self, system: str, user: str, schema: dict) -> CompletionResult:
        t0 = time.monotonic()
        r = await self._client.responses.create(
            model=self.model, instructions=system, input=user, max_output_tokens=self.max_tokens,
            text={"format": {"type": "json_schema", "name": "analysis", "schema": schema, "strict": True}},
        )
        if r.status != "completed":
            raise RuntimeError(f"openai response {r.status}: {getattr(getattr(r, 'incomplete_details', None), 'reason', '')}")
        for item in r.output or []:
            if getattr(item, "type", "") == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") == "refusal":
                        raise RuntimeError(f"model refused: {getattr(c, 'refusal', '')[:200]}")
        return CompletionResult(json.loads(r.output_text), r.usage.input_tokens, r.usage.output_tokens, int((time.monotonic() - t0) * 1000),
                                self.cost(self.model, r.usage.input_tokens, r.usage.output_tokens), r.model or self.model)
