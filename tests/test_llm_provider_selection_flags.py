# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Provider selection: setup reads the ladder's key variables; ambiguous keys stop.

1. Key variables for two or more providers fell through to a running Ollama,
   which then answered in their place. Founder decision (2026-11-12): stop and
   name the keys. The saved choice and one keyring key still come first.
2. Inline setup inferred ``github`` from ``GITHUB_API_KEY``, and then failed to
   build it, though ``PROVIDER_ENV_VARS`` says github is used only when named.
   Setup and ``fluid ai test`` now read the key variables a run without a TTY
   reads, and treat keys for two or more providers as that run does.

Hermetic: the keyring, the Ollama probe, the saved ``fluid ai setup`` choice,
HOME and PATH are patched, so nothing reads the real ``~/.fluid``, finds an
agent CLI, or dials a daemon.
"""

from __future__ import annotations

import io
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, Optional, Tuple

import pytest

import fluid_build.llm.providers as providers
from fluid_build.llm.providers import CopilotGenerationError, LlmConfig

pytestmark = pytest.mark.unit

OPENAI_KEY = "openai-env-key-test-0001"  # pragma: allowlist secret
ANTHROPIC_KEY = "anthropic-env-key-test-0002"  # pragma: allowlist secret
GEMINI_KEY = "gemini-env-key-test-0003"  # pragma: allowlist secret
GITHUB_KEY = "github-env-key-test-0004"  # pragma: allowlist secret


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


# ── 2. inline setup and fluid ai test read the ladder's key variables ───────


def test_setup_does_not_pick_github_from_its_key(world: _World) -> None:
    # It used to pick github here, then raise KeyError: 'github' building it.
    from fluid_build.cli.ai_setup import run_ai_setup_inline

    world.monkeypatch.setenv("GITHUB_API_KEY", GITHUB_KEY)

    assert run_ai_setup_inline(None) is None


def test_setup_still_reads_one_provider_s_key(world: _World) -> None:
    from fluid_build.cli.ai_setup import run_ai_setup_inline

    world.monkeypatch.setenv("GITHUB_API_KEY", GITHUB_KEY)
    world.monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)

    config = run_ai_setup_inline(None)

    assert (config.provider, config.api_key) == ("anthropic", ANTHROPIC_KEY)


def test_setup_reads_the_gemini_alias_like_the_resolver(world: _World) -> None:
    from fluid_build.cli.ai_setup import run_ai_setup_inline

    world.monkeypatch.setenv("GEMINI_API_KEY", GEMINI_KEY)

    config = run_ai_setup_inline(None)

    assert (config.provider, config.api_key) == ("gemini", GEMINI_KEY)


def test_init_does_not_count_github_s_key_as_ai_configured(world: _World) -> None:
    from fluid_build.cli.init import _is_ai_configured

    world.monkeypatch.setenv("GITHUB_API_KEY", GITHUB_KEY)

    assert _is_ai_configured() is False


def test_interactive_setup_names_two_keys_and_picks_neither(world: _World) -> None:
    from rich.console import Console

    from fluid_build.cli.ai_setup import run_ai_setup_inline

    for name, value in _TWO_KEYS.items():
        world.monkeypatch.setenv(name, value)
    out = io.StringIO()

    assert run_ai_setup_inline(Console(file=out, width=250)) is None
    assert "OPENAI_API_KEY, ANTHROPIC_API_KEY are all set" in out.getvalue()


def test_setup_lets_one_keyring_key_break_the_tie_as_the_ladder_does(world: _World) -> None:
    # Without a TTY the one keyring key selects gemini; setup must agree.
    from fluid_build.cli.ai_setup import run_ai_setup_inline

    for name, value in _TWO_KEYS.items():
        world.monkeypatch.setenv(name, value)
    world.keyring["gemini"] = GEMINI_KEY

    config = run_ai_setup_inline(None)

    assert (config.provider, config.api_key) == ("gemini", GEMINI_KEY)
    assert providers.check_llm_readiness(dict(os.environ)).provider == "gemini"


def test_init_s_check_agrees_with_setup_on_two_keys(world: _World) -> None:
    from fluid_build.cli.init import _is_ai_configured

    for name, value in _TWO_KEYS.items():
        world.monkeypatch.setenv(name, value)

    assert _is_ai_configured() is False  # setup would stop
    world.keyring["gemini"] = GEMINI_KEY
    assert _is_ai_configured() is True  # setup would use gemini


def _ai_test(**flags: object) -> Tuple[Optional[LlmConfig], Optional[str]]:
    from fluid_build.cli.ai_setup import _resolve_ai_test_config

    values = {
        "llm_provider": None,
        "llm_model": None,
        "llm_endpoint": None,
        "llm_timeout_seconds": None,
    }
    values.update(flags)
    return _resolve_ai_test_config(Namespace(**values))


def test_ai_test_does_not_pick_github_from_its_key(world: _World) -> None:
    world.monkeypatch.setenv("GITHUB_API_KEY", GITHUB_KEY)

    config, error = _ai_test()

    assert config is None
    assert "github" not in error.lower()


def test_ai_test_names_two_keys_and_tests_neither(world: _World) -> None:
    for name, value in _TWO_KEYS.items():
        world.monkeypatch.setenv(name, value)

    config, error = _ai_test()

    assert config is None
    assert "OPENAI_API_KEY, ANTHROPIC_API_KEY are all set" in error


def test_ai_test_still_tests_the_saved_choice_over_two_keys(world: _World) -> None:
    world.saved = {"provider": "anthropic", "model": "claude-saved-test"}
    for name, value in _TWO_KEYS.items():
        world.monkeypatch.setenv(name, value)

    config, error = _ai_test()

    assert error is None
    assert config.provider == "anthropic"


def test_ai_test_lets_one_keyring_key_break_the_tie(world: _World) -> None:
    for name, value in _TWO_KEYS.items():
        world.monkeypatch.setenv(name, value)
    world.keyring["gemini"] = GEMINI_KEY

    config, error = _ai_test()

    assert error is None
    assert (config.provider, config.api_key) == ("gemini", GEMINI_KEY)
