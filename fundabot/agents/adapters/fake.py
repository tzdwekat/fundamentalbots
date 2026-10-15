from fundabot.agents.base import CompletionResult


class FakeAdapter:
    """Canned outputs for tests: cycles through `outputs`, or calls it with (system, user) if callable."""
    provider, model = "fake", "fake-model"

    def __init__(self, outputs):
        self.outputs, self.calls = outputs, 0

    async def complete(self, system: str, user: str, schema: dict) -> CompletionResult:
        out = self.outputs(system, user) if callable(self.outputs) else self.outputs[self.calls % len(self.outputs)]
        self.calls += 1
        return CompletionResult(out, len(system + user) // 4, 100, 1, 0.0, self.model)

    def estimate_cost(self, input_chars: int) -> float:
        return 0.0
