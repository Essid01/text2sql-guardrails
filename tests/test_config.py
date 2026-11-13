"""Tests for environment-based configuration."""

import pytest
from pydantic import ValidationError

from text2sql.config import Settings


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    monkeypatch.setenv("LLM_CACHE_ENABLED", "false")
    monkeypatch.setenv("LLM_EFFORT", "medium")
    settings = Settings(_env_file=None)
    assert settings.require_llm() == ("sk-test", "some-model")
    assert settings.llm_cache_enabled is False
    assert settings.llm_effort == "medium"


def test_defaults_are_cost_conscious(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_EFFORT", raising=False)
    assert Settings(_env_file=None).llm_effort == "low"


def test_invalid_effort_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_EFFORT", "ultra")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_api_key_is_not_leaked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    settings = Settings(_env_file=None)
    assert "sk-secret-value" not in repr(settings)


def test_missing_llm_config_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        Settings(_env_file=None).require_llm()


def test_cost_estimate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PRICE_INPUT_PER_MTOK", "0.10")
    monkeypatch.setenv("LLM_PRICE_OUTPUT_PER_MTOK", "0.50")
    settings = Settings(_env_file=None)
    assert settings.estimate_cost_usd(1_000_000, 1_000_000) == pytest.approx(0.60)
    assert settings.estimate_cost_usd(2_000, 500) == pytest.approx(0.00045)
