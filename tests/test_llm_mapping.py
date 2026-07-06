"""Tests for the per-repo LLM mapping and review temperature."""

import pytest
from faker import Faker

from shiva_agent.review import (
    DEFAULT_TEMPERATURE,
    LLM_APIS,
    ConfigError,
    resolve_llm,
    validate_config,
)

fake = Faker()


# --- resolve_llm per-repo mapping -----------------------------------------


def test_default_when_repo_not_in_mapping():
    config = {
        "llm": {"provider": "ollama"},
        "llm_by_repo": {"ice1x/graphbook": {"provider": "openai", "model": "gpt-4o"}},
    }
    llm = resolve_llm(config, repo="ice1x/other")
    assert llm["provider"] == "ollama"


def test_repo_mapping_wins_over_default():
    config = {
        "llm": {"provider": "ollama"},
        "llm_by_repo": {"ice1x/graphbook": {"provider": "openai", "model": "gpt-4o"}},
    }
    llm = resolve_llm(config, repo="ice1x/graphbook")
    assert llm["provider"] == "openai"
    assert llm["model"] == "gpt-4o"
    assert llm["api"] == "openai"
    assert llm["auth"] == "bearer"
    assert llm["endpoint"] == "https://api.openai.com/v1/chat/completions"


def test_repo_ignored_when_no_repo_arg():
    config = {
        "llm": {"provider": "ollama"},
        "llm_by_repo": {"ice1x/graphbook": {"provider": "openai", "model": "gpt-4o"}},
    }
    assert resolve_llm(config)["provider"] == "ollama"


def test_explicit_override_beats_repo_mapping():
    config = {
        "llm": {"provider": "ollama"},
        "llm_by_repo": {"ice1x/graphbook": {"provider": "openai", "model": "gpt-4o"}},
    }
    override = {"llm": {"provider": "deepseek"}}
    llm = resolve_llm(config, override=override, repo="ice1x/graphbook")
    assert llm["provider"] == "deepseek"


# --- temperature -----------------------------------------------------------


def test_temperature_defaults_to_zero():
    assert resolve_llm({"llm": {"provider": "ollama"}})["temperature"] == DEFAULT_TEMPERATURE
    assert DEFAULT_TEMPERATURE == 0


def test_temperature_override_respected():
    llm = resolve_llm({"llm": {"provider": "ollama", "temperature": 0.7}})
    assert llm["temperature"] == 0.7


def test_openai_body_includes_temperature():
    body = LLM_APIS["openai"].request_body("gpt-4o", temperature=0)
    assert body["temperature"] == 0


def test_anthropic_body_omits_temperature_for_thinking():
    # Adaptive thinking is incompatible with a fixed temperature.
    body = LLM_APIS["anthropic"].request_body("claude-opus-4-8", temperature=0)
    assert "temperature" not in body
    assert body["thinking"] == {"type": "adaptive"}


# --- validation ------------------------------------------------------------


def test_validate_rejects_non_mapping_llm_by_repo():
    with pytest.raises(ConfigError, match="llm_by_repo must be a mapping"):
        validate_config({"categories": [], "llm_by_repo": ["nope"]})


def test_validate_rejects_bad_repo_key():
    with pytest.raises(ConfigError, match="owner/repo"):
        validate_config({"categories": [], "llm_by_repo": {"noslash": {"provider": "openai", "model": "x"}}})


def test_validate_propagates_inner_llm_error_with_repo_context():
    with pytest.raises(ConfigError, match="ice1x/graphbook"):
        validate_config(
            {"categories": [], "llm_by_repo": {"ice1x/graphbook": {"auth": "nonsense"}}}
        )


@pytest.mark.parametrize("bad", [-1, 2.5, "hot", True])
def test_validate_rejects_bad_temperature(bad):
    with pytest.raises(ConfigError, match="temperature"):
        validate_config({"categories": [], "llm": {"provider": "ollama", "temperature": bad}})


def test_validate_accepts_good_temperature():
    validate_config({"categories": [], "llm": {"provider": "ollama", "temperature": 0}})
    validate_config({"categories": [], "llm": {"provider": "ollama", "temperature": 1.5}})
