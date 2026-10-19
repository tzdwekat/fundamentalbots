"""Provider profiles (fundabot/profiles.yaml): provider -> model per role, chosen at run time with --profile."""
from pathlib import Path

import yaml

ROLES = ("personas", "screen", "escalation", "guidance")
PATH = Path(__file__).with_name("profiles.yaml")


def _all() -> dict:
    return yaml.safe_load(PATH.read_text())


def names() -> list[str]:
    return list(_all())


def load(name: str) -> dict:
    """{role: {provider: model}}; every role must be present and non-empty."""
    profiles = _all()
    if name not in profiles:
        raise KeyError(f"unknown profile {name!r}; choose one of {', '.join(profiles)}")
    p = profiles[name]
    missing = [r for r in ROLES if not p.get(r)]
    if missing:
        raise ValueError(f"profile {name!r} lacks roles: {missing}")
    return {r: dict(p[r]) for r in ROLES}
