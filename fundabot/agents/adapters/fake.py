from fundabot.agents.base import CompletionResult


class FakeAdapter:
    """Canned outputs for tests: cycles through `outputs`, or calls it with (system, user) if callable."""
    provider, model = "fake", "fake-model"

    def __init__(self, outputs):
        self.outputs, self.calls, self.last_prefix = outputs, 0, None

    async def complete(self, system: str, user: str, schema: dict, prefix: str | None = None) -> CompletionResult:
        self.last_prefix = prefix
        full_system = (prefix + "\n\n" + system) if prefix else system
        out = self.outputs(full_system, user) if callable(self.outputs) else self.outputs[self.calls % len(self.outputs)]
        self.calls += 1
        return CompletionResult(out, len(full_system + user) // 4, 100, 1, 0.0, self.model)

    def estimate_cost(self, input_chars: int) -> float:
        return 0.0
