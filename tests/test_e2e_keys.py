"""Per-provider LLM API-key resolution + deployment readiness in the driver."""

import pytest

import e2e
from e2e import check_providers, resolve_llm_provider, resolve_llm_token  # noqa: F401

from shiva_agent.e2e import hosted_providers_in_config, mapped_providers

KEY_VARS = [
    "SHIVA_LLM_API_KEY",
    "SHIVA_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "SHIVA_DEEPSEEK_API_KEY",
]

# Default local (ollama) + two hosted providers.
MIXED = {
    "llm": {"provider": "ollama"},
    "llm_by_repo": {
        "o/a": {"provider": "openai", "model": "gpt-4o"},
        "o/b": {"provider": "deepseek", "model": "deepseek-chat"},
    },
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_generic_fallback_when_no_provider(monkeypatch):
    monkeypatch.setenv("SHIVA_LLM_API_KEY", "generic")
    assert resolve_llm_token() == "generic"
    assert resolve_llm_token("openai") == "generic"


def test_provider_specific_wins_over_generic(monkeypatch):
    monkeypatch.setenv("SHIVA_LLM_API_KEY", "generic")
    monkeypatch.setenv("SHIVA_OPENAI_API_KEY", "openai-key")
    assert resolve_llm_token("openai") == "openai-key"
    # A different provider still falls back to generic.
    assert resolve_llm_token("deepseek") == "generic"


def test_bare_vendor_var_is_ignored(monkeypatch):
    # Only SHIVA_-namespaced names count — a bare OPENAI_API_KEY must not leak in.
    monkeypatch.setenv("OPENAI_API_KEY", "not-mine")
    assert resolve_llm_token("openai") == ""


def test_two_providers_two_keys(monkeypatch):
    monkeypatch.setenv("SHIVA_OPENAI_API_KEY", "k-openai")
    monkeypatch.setenv("SHIVA_DEEPSEEK_API_KEY", "k-deepseek")
    assert resolve_llm_token("openai") == "k-openai"
    assert resolve_llm_token("deepseek") == "k-deepseek"


def test_empty_when_nothing_set():
    assert resolve_llm_token("openai") == ""
    assert resolve_llm_token() == ""


def test_resolve_provider_reads_config_mapping():
    # The committed config maps ice1x/graphbook to a hosted model; any other
    # repo falls back to the default (ollama). Precedence itself is covered by
    # test_llm_mapping with synthetic configs.
    assert resolve_llm_provider("ice1x/graphbook") == "openai"
    assert resolve_llm_provider("ice1x/anything") == "ollama"


def test_resolve_provider_default_when_no_repo():
    # No repo → the default provider, so `setup` (which has no repo) still
    # resolves the right per-provider key.
    assert resolve_llm_provider() == "ollama"


# --- mapped_providers (pure, hosted + local) ------------------------------


def test_mapped_providers_covers_local_and_hosted():
    by_name = {p["provider"]: p for p in mapped_providers(MIXED)}
    assert set(by_name) == {"ollama", "openai", "deepseek"}
    assert by_name["ollama"]["needs_key"] is False  # local
    assert by_name["ollama"]["labels"] == ["<default>"]
    assert by_name["openai"]["needs_key"] is True
    assert by_name["openai"]["labels"] == ["o/a"]


def test_hosted_view_omits_local():
    assert hosted_providers_in_config(MIXED) == {"openai": ["o/a"], "deepseek": ["o/b"]}


# --- check_providers (covers local reachability + hosted keys) ------------


def test_check_flags_missing_key_and_down_local(monkeypatch):
    # Local server down, openai key set, deepseek key missing.
    monkeypatch.setattr(e2e, "local_server_reachable", lambda endpoint: False)
    monkeypatch.setenv("SHIVA_OPENAI_API_KEY", "k")
    rows = {r[0]: r for r in check_providers(MIXED)}
    assert rows["ollama"][2] is False  # local unreachable → not ready
    assert rows["openai"][2] is True   # key present
    assert rows["deepseek"][2] is False  # key missing
    assert "SHIVA_DEEPSEEK_API_KEY" in rows["deepseek"][3]


def test_check_all_ready(monkeypatch):
    monkeypatch.setattr(e2e, "local_server_reachable", lambda endpoint: True)
    monkeypatch.setenv("SHIVA_OPENAI_API_KEY", "k1")
    monkeypatch.setenv("SHIVA_DEEPSEEK_API_KEY", "k2")
    assert all(r[2] for r in check_providers(MIXED))


def test_check_local_only_needs_reachable_server(monkeypatch):
    monkeypatch.setattr(e2e, "local_server_reachable", lambda endpoint: True)
    rows = check_providers({"llm": {"provider": "ollama"}})
    assert [r[0] for r in rows] == ["ollama"]
    assert rows[0][2] is True


def test_only_provider_scopes_the_check(monkeypatch):
    # Deploying an openai repo must NOT be blocked by a down local default.
    monkeypatch.setattr(e2e, "local_server_reachable", lambda endpoint: False)
    monkeypatch.setenv("SHIVA_OPENAI_API_KEY", "k")
    rows = check_providers(MIXED, only_provider="openai")
    assert [r[0] for r in rows] == ["openai"]  # ollama (down) not checked
    assert rows[0][2] is True


def test_no_filter_checks_everything(monkeypatch):
    monkeypatch.setattr(e2e, "local_server_reachable", lambda endpoint: True)
    monkeypatch.setenv("SHIVA_OPENAI_API_KEY", "k")
    monkeypatch.setenv("SHIVA_DEEPSEEK_API_KEY", "k")
    providers = {r[0] for r in check_providers(MIXED)}
    assert providers == {"ollama", "openai", "deepseek"}
