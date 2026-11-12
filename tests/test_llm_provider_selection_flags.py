# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Provider selection: key variables for two or more providers stop the run.

1. Key variables for two or more providers fell through to a running Ollama,
   which then answered in their place. Founder decision (2026-11-12): stop and
   name the keys. The saved choice and one keyring key still come first.

Hermetic: the keyring, the Ollama probe, the saved ``fluid ai setup`` choice,
HOME and PATH are patched, so nothing reads the real ``~/.fluid``, finds an
agent CLI, or dials a daemon.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, Optional

import pytest

import fluid_build.llm.providers as providers
from fluid_build.llm.providers import CopilotGenerationError

pytestmark = pytest.mark.unit

OPENAI_KEY = "openai-env-key-test-0001"  # pragma: allowlist secret
ANTHROPIC_KEY = "anthropic-env-key-test-0002"  # pragma: allowlist secret
GEMINI_KEY = "gemini-env-key-test-0003"  # pragma: allowlist secret


_VARS = (
    "FLUID_LLM_PROVIDER",
    "FLUID_FORGE_AGENT",
    "FLUID_LLM_API_KEY",
    "FLUID_LLM_MODEL",
    "FLUID_LLM_ENDPOINT",
    "FLUID_LLM_ROUTING_MODEL",
    "FLUID_LLM_ROUTING_ENDPOINT",
    "FLUID_COPILOT_AGENT_LOOP",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_API_KEY",
    "GITHUB_TOKEN",
    "OLLAMA_HOST",
)


class _World:
    """What the machine holds: keyring keys, a running Ollama, a saved provider."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
        self.keyring: Dict[str, str] = {}
        self.ollama_up = False
        self.ollama_probes = 0
        self.saved: Optional[dict] = None
        self.monkeypatch = monkeypatch
        self.bin_dir = bin_dir

    def env(self, **values: str) -> Dict[str, str]:
        """An environment with only *values*; PATH finds no coding-agent CLI."""
        return {"PATH": str(self.bin_dir), **values}

    def tty(self, value: bool) -> None:
        self.monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: value))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_World]:
    import fluid_build._ai_config_shared as ai_config
    import fluid_build.cli.ai_setup as ai_setup

    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    w = _World(monkeypatch, bin_dir)

    def _ollama(env: object) -> bool:
        w.ollama_probes += 1
        return w.ollama_up

    monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda name: w.keyring.get(name))
    monkeypatch.setattr(providers, "detect_ollama_available", _ollama)
    monkeypatch.setattr(providers, "resolve_ollama_model", lambda env, endpoint=None: "llama3.2")
    monkeypatch.setattr(ai_config, "load_ai_config", lambda config_file=None: w.saved)
    monkeypatch.setattr(
        ai_config,
        "load_ai_config_for",
        lambda name, config_file=None: w.saved if (w.saved or {}).get("provider") == name else None,
    )
    monkeypatch.setattr(ai_setup, "_load_ai_config", lambda *a, **k: w.saved)
    monkeypatch.setattr(ai_setup, "_load_key_from_keyring", lambda name: w.keyring.get(name))
    monkeypatch.setattr(ai_setup, "detect_ollama_available", _ollama)
    monkeypatch.setattr(ai_setup, "_ai_setup_skipped", False)
    w.tty(False)
    providers.reset_llm_caches()
    yield w
    providers.reset_llm_caches()


def _args(**values: object) -> SimpleNamespace:
    base = {"llm_provider": None, "llm_model": None, "llm_endpoint": None}
    return SimpleNamespace(**{**base, **values})


# ── 1. key variables for two or more providers stop the run ─────────────────


_TWO_KEYS = {"OPENAI_API_KEY": OPENAI_KEY, "ANTHROPIC_API_KEY": ANTHROPIC_KEY}


def test_two_keys_never_fall_through_to_a_running_ollama(world: _World) -> None:
    world.ollama_up = True
    env = world.env(**_TWO_KEYS)

    assert providers._provider_name_from(_args(), env) is None
    with pytest.raises(CopilotGenerationError) as info:
        providers.resolve_llm_config(_args(), env)
    assert info.value.event == "copilot_no_llm_provider"
    assert "OPENAI_API_KEY, ANTHROPIC_API_KEY" in info.value.message
    assert world.ollama_probes == 0  # not even asked


def test_doctor_reports_the_same(world: _World) -> None:
    world.ollama_up = True
    env = world.env(**_TWO_KEYS)

    readiness = providers.check_llm_readiness(env)

    assert readiness.ready is False
    assert readiness.provider is None
    assert readiness.error == providers.no_provider_message(env)
    assert "OPENAI_API_KEY, ANTHROPIC_API_KEY" in readiness.error


def test_ollama_host_and_a_cloud_key_are_two_providers(world: _World) -> None:
    world.ollama_up = True
    env = world.env(OPENAI_API_KEY=OPENAI_KEY, OLLAMA_HOST="http://127.0.0.1:11500")

    assert providers._provider_name_from(_args(), env) is None
    assert "OPENAI_API_KEY, OLLAMA_HOST" in providers.check_llm_readiness(env).error


def test_gemini_s_two_variables_are_one_provider(world: _World) -> None:
    world.ollama_up = True
    env = world.env(GEMINI_API_KEY=GEMINI_KEY, GOOGLE_API_KEY=GEMINI_KEY)

    assert providers._provider_name_from(_args(), env) == "gemini"


def test_the_saved_choice_still_breaks_the_tie(world: _World) -> None:
    world.ollama_up = True
    world.saved = {"provider": "anthropic", "model": "claude-saved-test"}
    env = world.env(**_TWO_KEYS)

    config = providers.resolve_llm_config(_args(), env)

    assert (config.provider, config.model, config.api_key) == (
        "anthropic",
        "claude-saved-test",
        ANTHROPIC_KEY,
    )
    assert providers.check_llm_readiness(env).provider == "anthropic"


def test_one_keyring_key_still_comes_before_the_stop(world: _World) -> None:
    world.ollama_up = True
    world.keyring["gemini"] = GEMINI_KEY
    env = world.env(**_TWO_KEYS)

    assert providers._provider_name_from(_args(), env) == "gemini"
    assert providers.check_llm_readiness(env).provider == "gemini"


def test_two_keyring_keys_still_reach_a_running_ollama(world: _World) -> None:
    # Only key variables stop the ambient rung; keyring keys are not per-run signals.
    world.ollama_up = True
    world.keyring.update(openai=OPENAI_KEY, anthropic=ANTHROPIC_KEY)

    assert providers._provider_name_from(_args(), world.env()) == "ollama"


def test_a_named_provider_is_unaffected(world: _World) -> None:
    world.ollama_up = True

    config = providers.resolve_llm_config(_args(llm_provider="anthropic"), world.env(**_TWO_KEYS))

    assert (config.provider, config.api_key) == ("anthropic", ANTHROPIC_KEY)
