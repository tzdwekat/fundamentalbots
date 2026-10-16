"""Google Gemini adapter: JSON output constrained by response_json_schema."""
import asyncio
import json
import re
import time

from google import genai
from google.genai import types

from fundabot.agents.base import CompletionResult
from fundabot.config import settings

PRICES = {  # USD per million tokens (input, output) for prompts <= 200k, ai.google.dev pricing 2026-10-15
    "gemini-3.1-pro-preview": (2.0, 12.0),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-2.5-pro": (1.25, 10.0),
}
RETRYABLE = ("429", "500", "502", "503", "504", "timeout", "deadline", "unavailable", "resource exhausted", "resource_exhausted")
RETRY_IN = re.compile(r"retry in ([\d.]+)s", re.I)
CONCURRENCY = 4  # per event loop; a free-tier key still 429s above ~2 and is handled by the retry delay below
_locks: dict = {}  # one Semaphore per event loop (the CLI runs one loop per ticker)


def _lock() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    return _locks.setdefault(loop, asyncio.Semaphore(CONCURRENCY))


class GeminiAdapter:
    provider = "gemini"

    def __init__(self, model: str = "gemini-3.1-pro-preview", max_tokens: int = 16000):  # thinking tokens count against this cap
        self.model, self.max_tokens = model, max_tokens
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=180_000))

    @staticmethod
    def cost(model: str, input_tokens: int, output_tokens: int) -> float:
        pin, pout = PRICES.get(model, max(PRICES.values()))
        return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout

    def estimate_cost(self, input_chars: int) -> float:
        return self.cost(self.model, input_chars // 4, self.max_tokens // 2)

    async def complete(self, system: str, user: str, schema: dict) -> CompletionResult:
        t0 = time.monotonic()
        config = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json", response_json_schema=schema,
                                             max_output_tokens=self.max_tokens)
        async with _lock():
            for attempt in range(5):
                try:
                    r = await self._client.aio.models.generate_content(model=self.model, contents=user, config=config)
                    break
                except Exception as e:  # the SDK raises several error classes; retry on transient ones only
                    if attempt == 4 or not any(k in str(e).lower() for k in RETRYABLE):
                        raise
                    m = RETRY_IN.search(str(e))
                    await asyncio.sleep(float(m.group(1)) + 1 if m else 15 * (attempt + 1))
        reason = str(getattr(r.candidates[0], "finish_reason", "")) if getattr(r, "candidates", None) else "no candidates"
        if "MAX_TOKENS" in reason.upper():
            raise RuntimeError(f"output truncated at max_tokens ({self.max_tokens}, thinking included)")
        if not r.text:
            raise RuntimeError(f"gemini returned no text: {reason}")
        um = r.usage_metadata
        out_tokens = (um.candidates_token_count or 0) + (getattr(um, "thoughts_token_count", 0) or 0)
        return CompletionResult(json.loads(r.text), um.prompt_token_count or 0, out_tokens, int((time.monotonic() - t0) * 1000),
                                self.cost(self.model, um.prompt_token_count or 0, out_tokens), self.model)
