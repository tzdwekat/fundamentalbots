"""Provider profiles: one flag picks the provider set for every role; personas and both judge rounds are unchanged."""
import pytest

from fundabot import profiles
from fundabot.config import settings


def test_every_profile_defines_every_role():
    names = profiles.names()
    assert {"multi", "anthropic", "openai", "gemini"} <= set(names)
    for n in names:
        p = profiles.load(n)
        assert set(p) == set(profiles.ROLES) and all(p[r] for r in profiles.ROLES)


def test_unknown_profile_lists_choices():
    with pytest.raises(KeyError, match="multi"):
        profiles.load("nope")


def test_build_adapters_single_provider_profile(monkeypatch):
    from fundabot.cli import build_adapters

    monkeypatch.setattr(settings, "gemini_api_key", "")  # a single-provider profile must not need the other keys
    adapters, screen, judges, guidance = build_adapters(True, "anthropic", None, 3, profile="anthropic")
    assert [(a.provider, a.model) for a in adapters] == [("anthropic", "claude-sonnet-5-5")]
    assert [(j.provider, j.model) for j in screen] == [("anthropic", "claude-haiku-5-5")]
    assert [(j.provider, j.model) for j in judges] == [("anthropic", "claude-opus-5-5")]
    assert (guidance.provider, guidance.model) == ("anthropic", "claude-haiku-5-5")
    _, _, _, g2 = build_adapters(False, "openai", None, 3, profile="openai")
    assert (g2.provider, g2.model) == ("openai", "gpt-5.4-mini")


def test_build_adapters_multi_matches_today(monkeypatch):
    from fundabot.cli import JUDGE_MODELS, SCREEN_JUDGE_MODELS, build_adapters

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    adapters, screen, judges, guidance = build_adapters(True, "", None, 3)
    assert [a.model for a in adapters] == ["claude-sonnet-5-5", "gpt-5.4", "gemini-3.1-pro-preview"]
    assert [j.model for j in screen] == [SCREEN_JUDGE_MODELS[p] for p in ("anthropic", "openai", "gemini")]
    assert [j.model for j in judges] == [JUDGE_MODELS[p] for p in ("anthropic", "openai", "gemini")]
    assert guidance.model == "claude-haiku-5-5"
    only_two, screen2, judges2, _ = build_adapters(True, "anthropic,openai", None, 3)
    assert [a.provider for a in only_two] == ["anthropic", "openai"] and len(screen2) == 3 and len(judges2) == 3  # --providers narrows personas only
    one, s1, j1, _ = build_adapters(True, "", "claude-sonnet-5-5", 1)
    assert len(one) == 3 and [j.model for j in s1] == ["claude-haiku-5-5"] and [j.model for j in j1] == ["claude-opus-5-5"]


def test_empty_providers_means_the_profile(monkeypatch):
    """`--providers` defaults to the profile: every provider on --full, the first one on a cheap run; the worker default works on any profile."""
    from fundabot.cli import build_adapters

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    cheap, _, _, _ = build_adapters(False, "", None, 3, profile="openai")
    assert [(a.provider, a.model) for a in cheap] == [("openai", "gpt-5.4")]
    full, screen, judges, _ = build_adapters(True, "", None, 3, profile="openai")
    assert [a.provider for a in full] == ["openai"] and len(screen) == 1 and len(judges) == 1
    multi_cheap, _, _, _ = build_adapters(False, "", None, 3)
    assert [a.provider for a in multi_cheap] == ["anthropic"]
    multi_full, _, _, _ = build_adapters(True, "", None, 3)
    assert [a.provider for a in multi_full] == ["anthropic", "openai", "gemini"]
    named, _, _, _ = build_adapters(True, "anthropic", None, 3)
    assert [a.provider for a in named] == ["anthropic"]  # an explicit --providers is honoured as written
    with pytest.raises(Exception, match="no persona provider"):
        build_adapters(True, "anthropic", None, 3, profile="openai")
