import hashlib
from typing import Protocol

import numpy as np

from fundabot.config import settings

DIMS = 1536


class Embedder(Protocol):
    model: str
    total_tokens: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    """text-embedding-3-small, 1536 dims. Batches of 100. Accumulates token usage for the call log."""
    PRICE_PER_MTOK = 0.02

    def __init__(self, model: str = "text-embedding-3-small"):
        from openai import OpenAI

        self.model, self.total_tokens = model, 0
        self._client = OpenAI(api_key=settings.openai_api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for i in range(0, len(texts), 100):
            r = self._client.embeddings.create(model=self.model, input=texts[i:i + 100])
            self.total_tokens += r.usage.total_tokens
            out += [d.embedding for d in sorted(r.data, key=lambda d: d.index)]
        return out

    def cost_usd(self, tokens: int) -> float:
        return tokens / 1e6 * self.PRICE_PER_MTOK


class FakeEmbedder:
    """Deterministic hash-seeded vectors for tests; zero cost."""
    model, PRICE_PER_MTOK = "fake-embedding", 0.0

    def __init__(self):
        self.total_tokens = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha256(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(DIMS)
            out.append((v / np.linalg.norm(v)).tolist())
            self.total_tokens += len(t) // 4
        return out

    def cost_usd(self, tokens: int) -> float:
        return 0.0
