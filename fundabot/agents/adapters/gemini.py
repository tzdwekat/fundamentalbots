"""Google Gemini adapter: JSON output constrained by response_json_schema."""
import asyncio
import hashlib
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
MIN_CACHE_CHARS = 4096 * 4  # explicit caches need ~4k tokens on Pro; storage for a 25k-token prefix over a grid is well under $0.01
CACHE_TTL = "900s"
_locks: dict = {}  # one Semaphore per event loop (the CLI runs one loop per ticker)
_cache_locks: dict = {}


def _lock() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    return _locks.setdefault(loop, asyncio.Semaphore(CONCURRENCY))


def _cache_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    return _cache_locks.setdefault(loop, asyncio.Lock())


class GeminiAdapter:
    provider = "gemini"

    def __init__(self, model: str = "gemini-3.1-pro-preview", max_tokens: int = 16000):  # thinking tokens count against this cap
        self.model, self.max_tokens = model, max_tokens
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=180_000))
        self._caches: dict[str, tuple[str, float]] = {}  # sha256(prefix) -> (cache name, expiry monotonic)

    CACHED_RATE = 0.1  # implicit-cache reads $0.20/M for gemini-3.1-pro (third-party pricing pages, 2026-10-18)

    @staticmethod
    def cost(model: str, input_tokens: int, output_tokens: int, cached: int = 0, cache_write: int = 0) -> float:
        """cache_write: tokens sent to caches.create, billed once at the normal input rate (storage is not metered here)."""
        pin, pout = PRICES.get(model, max(PRICES.values()))
        return (input_tokens - cached) / 1e6 * pin + cached / 1e6 * pin * GeminiAdapter.CACHED_RATE + cache_write / 1e6 * pin + output_tokens / 1e6 * pout

    def estimate_cost(self, input_chars: int) -> float:
        return self.cost(self.model, input_chars // 4, self.max_tokens // 2)

    async def _cache_for(self, prefix: str) -> tuple[str, int]:
        """One explicit cache per (adapter, prefix); Gemini's implicit cache misses whenever the user turn differs.
        Returns (cache name, tokens billed by creating it now; 0 when reused)."""
        key = hashlib.sha256(prefix.encode()).hexdigest()
        async with _cache_lock():
            name, expiry = self._caches.get(key, (None, 0.0))
            if name is not None and time.monotonic() < expiry - 60:
                return name, 0
            c = await self._client.aio.caches.create(model=self.model, config=types.CreateCachedContentConfig(system_instruction=prefix, ttl=CACHE_TTL))
            self._caches[key] = (c.name, time.monotonic() + int(CACHE_TTL.rstrip("s")))
            return c.name, (getattr(c.usage_metadata, "total_token_count", 0) or 0)

    async def complete(self, system: str, user: str, schema: dict, prefix: str | None = None) -> CompletionResult:
        t0, written = time.monotonic(), 0
        if prefix and len(prefix) >= MIN_CACHE_CHARS:
            name, written = await self._cache_for(prefix)
            config = types.GenerateContentConfig(cached_content=name, response_mime_type="application/json",
                                                 response_json_schema=schema, max_output_tokens=self.max_tokens)
            contents = system + "\n\n" + user  # the persona tail rides with the user turn; cached_content excludes system_instruction
        else:
            config = types.GenerateContentConfig(system_instruction=(prefix + "\n\n" + system) if prefix else system, response_mime_type="application/json",
                                                 response_json_schema=schema, max_output_tokens=self.max_tokens)
            contents = user
        async with _lock():
            for attempt in range(5):
                try:
                    r = await self._client.aio.models.generate_content(model=self.model, contents=contents, config=config)
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
        cached = getattr(um, "cached_content_token_count", 0) or 0
        return CompletionResult(json.loads(r.text), um.prompt_token_count or 0, out_tokens, int((time.monotonic() - t0) * 1000),
                                self.cost(self.model, um.prompt_token_count or 0, out_tokens, cached, written), self.model, cached, written)
