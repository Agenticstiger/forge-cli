# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""No provider selected: forge stops and says why, the same words as ``fluid doctor``.

Founder decision (2026-11-12): there is no silent fallback provider. The old
``gemini`` default sent ``FLUID_LLM_API_KEY`` to Google whatever key it was, and
with two keys set it asked for a third, gemini's, while ``fluid doctor`` said
"Not selected". Now forge raises ``copilot_no_llm_provider`` before any
provider is built, and readiness reports the same message.

Hermetic: no keyring, no saved ``~/.fluid`` config, no Ollama, no agent on PATH.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build._error_catalog import suggestions_for
from fluid_build.llm import providers
from fluid_build.llm.provider_resolve import _infer_provider_from_explicit_keys
from fluid_build.llm.providers import (
    CopilotGenerationError,
    check_llm_readiness,
    no_provider_message,
    resolve_llm_config,
)

pytestmark = pytest.mark.unit

_PLAIN = "No LLM provider configured. Run 'fluid ai setup' or set an API key env var."


@pytest.fixture(autouse=True)
def _nothing_configured(monkeypatch):
    for var in (
        "FLUID_LLM_PROVIDER",
        "FLUID_LLM_API_KEY",
        "FLUID_LLM_MODEL",
        "FLUID_LLM_ENDPOINT",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OLLAMA_HOST",
        "FLUID_COPILOT_AGENT_LOOP",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda name: None)
    monkeypatch.setattr(providers, "detect_ollama_available", lambda env: False)
    monkeypatch.setattr(
        "fluid_build.llm.provider_resolve._first_coding_agent_on_path", lambda env: None
    )
    monkeypatch.setattr("fluid_build._ai_config_shared.load_ai_config", lambda *a, **k: None)


def _args():
    return SimpleNamespace(llm_provider=None, llm_model=None, llm_endpoint=None)


# The env always holds something, so ``resolve_llm_config`` never swaps an
# empty mapping for ``os.environ``.
_NOTHING = {"PATH": "/nonexistent"}
_TWO_KEYS = {
    **_NOTHING,
    "OPENAI_API_KEY": "k1",  # pragma: allowlist secret
    "ANTHROPIC_API_KEY": "k2",  # pragma: allowlist secret
}
_GENERIC_KEY = {**_NOTHING, "FLUID_LLM_API_KEY": "sk-x"}  # pragma: allowlist secret


class TestForgeStops:
    def test_nothing_configured(self):
        with pytest.raises(CopilotGenerationError) as info:
            resolve_llm_config(_args(), _NOTHING)
        assert info.value.event == "copilot_no_llm_provider"
        assert info.value.message == _PLAIN
        assert any("--llm-provider" in item for item in info.value.suggestions)

    def test_two_keys_are_named_instead_of_asking_for_a_third(self):
        with pytest.raises(CopilotGenerationError) as info:
            resolve_llm_config(_args(), _TWO_KEYS)
        assert info.value.event == "copilot_no_llm_provider"
        assert "OPENAI_API_KEY, ANTHROPIC_API_KEY" in info.value.message
        assert "gemini" not in info.value.message.lower()

    def test_a_generic_key_is_never_sent_to_a_guessed_vendor(self, monkeypatch):
        # Stopping happens before any provider is built, so nothing can dial.
        monkeypatch.setattr(
            providers, "get_llm_provider", lambda name: pytest.fail(f"built {name}")
        )
        with pytest.raises(CopilotGenerationError) as info:
            resolve_llm_config(_args(), _GENERIC_KEY)
        assert info.value.event == "copilot_no_llm_provider"
        assert "FLUID_LLM_API_KEY" in info.value.message

    def test_a_named_provider_still_resolves(self):
        config = resolve_llm_config(
            SimpleNamespace(llm_provider="anthropic", llm_model=None, llm_endpoint=None),
            {**_TWO_KEYS},
        )
        assert config.provider == "anthropic"


class TestDoctorSaysTheSame:
    @pytest.mark.parametrize("env", [_NOTHING, _TWO_KEYS, _GENERIC_KEY])
    def test_readiness_error_is_the_forge_message(self, env):
        readiness = check_llm_readiness(env)
        assert readiness.provider is None
        assert readiness.error == no_provider_message(env)

    def test_readiness_names_the_two_keys(self):
        assert "OPENAI_API_KEY, ANTHROPIC_API_KEY" in check_llm_readiness(_TWO_KEYS).error


def test_gemini_and_google_keys_are_one_provider():
    env = {"GEMINI_API_KEY": "k", "GOOGLE_API_KEY": "k"}
    assert _infer_provider_from_explicit_keys(env) == "gemini"
    assert no_provider_message({**env, "OPENAI_API_KEY": "k"}).startswith(
        "No LLM provider is selected: OPENAI_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY"
    )


def test_the_error_has_catalog_suggestions():
    assert suggestions_for("copilot_no_llm_provider")


def test_a_saved_setup_choice_selects_its_provider(monkeypatch):
    # A run without a TTY reads the saved choice too, so what is missing is
    # that provider's key, not a provider.
    monkeypatch.setattr(
        "fluid_build._ai_config_shared.load_ai_config", lambda *a, **k: {"provider": "anthropic"}
    )
    with pytest.raises(CopilotGenerationError) as info:
        resolve_llm_config(_args(), _NOTHING)
    assert info.value.event == "copilot_missing_llm_api_key"
    assert "anthropic" in info.value.message
