# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Provider selection: what the operator states wins, and ambiguous keys stop.

1. Key variables for two or more providers fell through to a running Ollama,
   which then answered in their place. Founder decision (2026-11-12): stop and
   name the keys. The saved choice and one keyring key still come first.
2. Inline setup inferred ``github`` from ``GITHUB_API_KEY``, and then failed to
   build it, though ``PROVIDER_ENV_VARS`` says github is used only when named.
   Setup and ``fluid ai test`` now read the key variables a run without a TTY
   reads, and treat keys for two or more providers as that run does.
3. Interactive ``fluid forge`` and ``fluid init`` put inline setup's pick over
   ``--llm-provider``, ``--llm-model``, ``--llm-endpoint`` and their
   ``FLUID_LLM_*`` twins. A named provider now skips setup, and setup fills
   only what the run leaves unstated.
4. ``fluid ai test`` ignored ``FLUID_LLM_PROVIDER`` and ``FLUID_FORGE_AGENT``.

Hermetic: the keyring, the Ollama probe, the saved ``fluid ai setup`` choice,
HOME and PATH are patched, so nothing reads the real ``~/.fluid``, finds an
agent CLI, or dials a daemon.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, List, Optional, Tuple
from unittest import mock

import pytest

import fluid_build.llm.providers as providers
from fluid_build.llm.providers import CopilotGenerationError, LlmConfig

pytestmark = pytest.mark.unit

OPENAI_KEY = "openai-env-key-test-0001"  # pragma: allowlist secret
ANTHROPIC_KEY = "anthropic-env-key-test-0002"  # pragma: allowlist secret
GEMINI_KEY = "gemini-env-key-test-0003"  # pragma: allowlist secret
GITHUB_KEY = "github-env-key-test-0004"  # pragma: allowlist secret
ONPREM = "https://llm.internal.test/v1"
GENERIC_KEY = "generic-llm-key-test-0005"  # pragma: allowlist secret


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


# ── 3. interactive forge and init: what the operator states wins ────────────


def _openai_setup_pick() -> LlmConfig:
    """What inline setup returns for a saved OpenAI choice with its keyring key."""
    return LlmConfig(provider="openai", model="gpt-saved-test", endpoint="", api_key=OPENAI_KEY)


def _claude_code_setup_pick() -> LlmConfig:
    return LlmConfig(
        provider="claude-code",
        model="claude-code",
        endpoint="coding-agent://claude-code",
        api_key=None,
    )


def _interactive_forge(
    world: _World, tmp_path: Path, pick: LlmConfig, **flags: object
) -> Tuple[Optional[Namespace], List[str], List[str]]:
    """Run interactive ``fluid forge`` in AI mode with setup returning *pick*.

    Returns the args the copilot ran with, setup's calls and the providers
    whose key setup's pick put into the session.
    """
    from fluid_build.cli.forge import run as forge_run

    world.tty(True)
    world.monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: tmp_path / "home")
    world.monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    seen: Dict[str, Namespace] = {}
    setup_calls: List[str] = []
    session: List[str] = []

    def _copilot(args: Namespace, _logger: logging.Logger) -> int:
        seen["args"] = args
        return 0

    def _setup(_console: object) -> LlmConfig:
        setup_calls.append("setup")
        return pick

    values = {
        "command": "forge",
        "forge_subcommand": None,
        "blank": False,
        "target_dir": str(tmp_path / "product"),
        "non_interactive": False,
        "dry_run": False,
        "llm_provider": None,
        "llm_model": None,
        "llm_endpoint": None,
        "agent_loop": False,
        "refine": None,
        "from_product": [],
        "from_product_list": None,
        "template": None,
        "scaffold": None,
    }
    values.update(flags)
    with (
        mock.patch("fluid_build.cli._forge_mode_picker.pick_mode", lambda a, **k: "ai"),
        mock.patch("fluid_build.cli._forge_mode_picker.should_show_picker", lambda a: True),
        mock.patch("fluid_build.cli.forge.run_ai_copilot_mode", _copilot),
        mock.patch("fluid_build.cli.ai_setup.run_ai_setup_inline", _setup),
        mock.patch(
            "fluid_build.cli.ai_setup.set_session_env",
            lambda provider, key: session.append(provider),
        ),
    ):
        forge_run(Namespace(**values), logging.getLogger("test"))
    return seen.get("args"), setup_calls, session


def test_a_named_provider_flag_is_not_replaced_by_setup_s_pick(
    world: _World, tmp_path: Path
) -> None:
    ran, setup_calls, session = _interactive_forge(
        world, tmp_path, _openai_setup_pick(), llm_provider="anthropic"
    )

    assert setup_calls == []  # setup cannot know the name, so it is not asked
    assert ran.llm_provider == "anthropic"
    assert ran.llm_model is None  # the named provider's catalog model, as without a TTY
    assert session == []


@pytest.mark.parametrize(
    "var,value", [("FLUID_LLM_PROVIDER", "anthropic"), ("FLUID_FORGE_AGENT", "codex")]
)
def test_a_provider_named_by_a_variable_skips_setup(
    world: _World, tmp_path: Path, var: str, value: str
) -> None:
    world.monkeypatch.setenv(var, value)

    ran, setup_calls, _ = _interactive_forge(world, tmp_path, _openai_setup_pick())

    assert setup_calls == []
    assert ran.llm_provider is None
    assert providers._provider_name_from(ran, os.environ) == value


def test_setup_keeps_a_stated_model(world: _World, tmp_path: Path) -> None:
    ran, setup_calls, session = _interactive_forge(
        world, tmp_path, _openai_setup_pick(), llm_model="gpt-stated-test"
    )

    assert setup_calls == ["setup"]
    assert ran.llm_provider == "openai"  # nothing named one, so setup's pick is used
    assert ran.llm_model == "gpt-stated-test"
    assert session == ["openai"]


def test_setup_keeps_a_stated_endpoint(world: _World, tmp_path: Path) -> None:
    ran, _, _ = _interactive_forge(world, tmp_path, _openai_setup_pick(), llm_endpoint=ONPREM)

    assert ran.llm_provider == "openai"
    assert ran.llm_endpoint == ONPREM


def test_setup_keeps_the_model_and_endpoint_variables(world: _World, tmp_path: Path) -> None:
    world.monkeypatch.setenv("FLUID_LLM_MODEL", "gpt-env-test")
    world.monkeypatch.setenv("FLUID_LLM_ENDPOINT", ONPREM)

    ran, _, _ = _interactive_forge(world, tmp_path, _openai_setup_pick())
    config = providers.resolve_llm_config(
        ran,
        world.env(
            OPENAI_API_KEY=OPENAI_KEY, FLUID_LLM_MODEL="gpt-env-test", FLUID_LLM_ENDPOINT=ONPREM
        ),
    )

    assert (ran.llm_model, ran.llm_endpoint) == (None, None)
    assert config.provider == "openai"
    assert config.model == "gpt-env-test"
    assert config.endpoint == ONPREM


def test_setup_fills_what_the_run_leaves_unstated(world: _World, tmp_path: Path) -> None:
    pick = LlmConfig(
        provider="openai",
        model="gpt-saved-test",
        endpoint="https://saved.example.test/v1",
        api_key=OPENAI_KEY,
    )

    ran, _, session = _interactive_forge(world, tmp_path, pick)

    assert (ran.llm_provider, ran.llm_model, ran.llm_endpoint) == (
        "openai",
        "gpt-saved-test",
        "https://saved.example.test/v1",
    )
    assert ran.llm_provider_named is None  # setup's pick never counts as named
    assert session == ["openai"]


def test_setup_s_agent_never_takes_a_stated_endpoint(world: _World, tmp_path: Path) -> None:
    # An agent cannot call the endpoint, so it is not used, as without a TTY.
    world.saved = {"provider": "claude-code"}

    ran, setup_calls, _ = _interactive_forge(
        world, tmp_path, _claude_code_setup_pick(), llm_endpoint=ONPREM
    )

    assert setup_calls == ["setup"]
    assert ran.llm_provider is None
    assert ran.llm_endpoint == ONPREM
    with pytest.raises(CopilotGenerationError) as info:
        providers.resolve_llm_config(ran, world.env())
    assert info.value.event == "copilot_no_llm_provider"
    assert "saved claude-code" in info.value.message


def test_setup_s_agent_is_used_for_test_double_args(world: _World) -> None:
    # A MagicMock invents llm_endpoint, llm_model and agent_loop; none is stated.
    args = mock.MagicMock()

    assert providers.fill_llm_args_from_setup(args, _claude_code_setup_pick(), world.env())
    assert args.llm_provider == "claude-code"


def test_named_llm_provider_reads_the_flag_then_the_variables(world: _World) -> None:
    env = world.env(FLUID_LLM_PROVIDER="anthropic", FLUID_FORGE_AGENT="codex")

    assert providers.named_llm_provider(_args(llm_provider="openai"), env) == "openai"
    assert providers.named_llm_provider(_args(), env) == "anthropic"
    assert providers.named_llm_provider(_args(), world.env(FLUID_FORGE_AGENT="codex")) == "codex"
    assert providers.named_llm_provider(mock.MagicMock(), world.env()) is None


def _init_ai_mode(
    world: _World, tmp_path: Path, pick: LlmConfig
) -> Tuple[Optional[Namespace], List[str]]:
    """``fluid init``'s AI path with setup returning *pick*; the copilot is stubbed."""
    from fluid_build.cli import init as init_mod

    world.monkeypatch.chdir(tmp_path)
    seen: Dict[str, Namespace] = {}
    setup_calls: List[str] = []

    def _copilot(args: Namespace, _logger: logging.Logger) -> int:
        seen["args"] = args
        return 1  # no success panel

    def _setup(_console: object) -> LlmConfig:
        setup_calls.append("setup")
        return pick

    with (
        mock.patch("fluid_build.cli.ai_setup.run_ai_setup_inline", _setup),
        mock.patch("fluid_build.cli.ai_setup.set_session_env", lambda provider, key: None),
        mock.patch("fluid_build.cli.forge.run_ai_copilot_mode", _copilot),
    ):
        init_mod._ai_mode(Namespace(name="demo-product", yes=True), logging.getLogger("test"))
    return seen.get("args"), setup_calls


def test_init_keeps_a_provider_named_by_a_variable(world: _World, tmp_path: Path) -> None:
    world.monkeypatch.setenv("FLUID_LLM_PROVIDER", "anthropic")

    ran, setup_calls = _init_ai_mode(world, tmp_path, _openai_setup_pick())

    assert setup_calls == []
    assert getattr(ran, "llm_provider", None) is None
    assert providers._provider_name_from(ran, os.environ) == "anthropic"


def test_init_keeps_the_model_variable(world: _World, tmp_path: Path) -> None:
    world.monkeypatch.setenv("FLUID_LLM_MODEL", "gpt-env-test")

    ran, setup_calls = _init_ai_mode(world, tmp_path, _openai_setup_pick())

    assert setup_calls == ["setup"]
    assert ran.llm_provider == "openai"
    assert getattr(ran, "llm_model", None) is None  # FLUID_LLM_MODEL then applies


def test_init_counts_a_named_provider_as_ai_configured(world: _World) -> None:
    # Forge runs no setup for it, so init offers none either.
    from fluid_build.cli.init import _is_ai_configured

    world.monkeypatch.setenv("FLUID_LLM_PROVIDER", "anthropic")

    assert _is_ai_configured() is True


def test_the_interview_panel_names_the_run_s_provider(
    world: _World, tmp_path: Path, deterministic_welcome_scan: None
) -> None:
    # The scan reads the saved choice; the run was named anthropic.
    from fluid_build.cli._world_class_interview import collect_signals

    world.monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: tmp_path / "home")
    world.saved = {"provider": "openai", "model": "gpt-saved-test"}

    assert collect_signals(target_dir=tmp_path).ai_provider == "openai"
    sig = collect_signals(target_dir=tmp_path, ai_provider="anthropic")

    assert (sig.ai_configured, sig.ai_provider) == (True, "anthropic")


def test_the_welcome_panel_names_the_run_s_provider(world: _World) -> None:
    from fluid_build.cli._welcome_scan import WelcomeFindings
    from fluid_build.cli.forge_modes import _with_run_provider

    def _saved_openai() -> WelcomeFindings:
        return WelcomeFindings(ai_configured=True, ai_provider_hint="openai")

    named = _with_run_provider(_saved_openai(), Namespace(llm_provider="anthropic"))
    unnamed = _with_run_provider(_saved_openai(), Namespace(llm_provider=None))

    assert named.ai_provider_hint == "anthropic"
    assert unnamed.ai_provider_hint == "openai"


# ── 4. fluid ai test honours the provider the run names ─────────────────────


def test_ai_test_honours_fluid_llm_provider(world: _World) -> None:
    world.saved = {"provider": "openai", "model": "gpt-saved-test"}
    world.keyring["openai"] = OPENAI_KEY
    world.monkeypatch.setenv("FLUID_LLM_PROVIDER", "anthropic")
    world.monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)

    config, error = _ai_test()

    assert error is None
    assert (config.provider, config.api_key) == ("anthropic", ANTHROPIC_KEY)


def test_ai_test_gives_a_provider_named_by_a_variable_the_generic_key(world: _World) -> None:
    world.monkeypatch.setenv("FLUID_LLM_PROVIDER", "openai")
    world.monkeypatch.setenv("FLUID_LLM_API_KEY", GENERIC_KEY)

    config, error = _ai_test()

    assert error is None
    assert (config.provider, config.api_key) == ("openai", GENERIC_KEY)


def test_ai_test_honours_fluid_forge_agent(world: _World) -> None:
    # It tested the saved provider, which this run would not use.
    world.saved = {"provider": "openai", "model": "gpt-saved-test"}
    world.keyring["openai"] = OPENAI_KEY
    world.monkeypatch.setenv("FLUID_FORGE_AGENT", "codex")

    config, error = _ai_test()

    assert config is None
    assert "codex" in error


def test_ai_test_flag_beats_the_variable(world: _World) -> None:
    world.monkeypatch.setenv("FLUID_LLM_PROVIDER", "anthropic")
    world.monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)

    config, error = _ai_test(llm_provider="openai")

    assert error is None
    assert config.provider == "openai"


@pytest.mark.parametrize("value", ["claude-code", "codex"])
def test_ai_test_says_it_does_not_test_a_named_agent(world: _World, value: str) -> None:
    world.monkeypatch.setenv("FLUID_FORGE_AGENT", value)

    config, error = _ai_test()

    assert config is None
    assert f"does not test {value}" in error


# ── 5. review follow-up: the helpers load in a fresh interpreter ───────────


_FRESH_CALLS = (
    "from fluid_build.cli.init import _is_ai_configured; print(_is_ai_configured())",
    "from argparse import Namespace; from fluid_build.cli.ai_setup import _resolve_ai_test_config; "
    "print(_resolve_ai_test_config(Namespace(llm_provider=None, llm_model=None, "
    "llm_endpoint=None, llm_timeout_seconds=None))[1])",
    "from fluid_build.cli.ai_setup import run_ai_setup_inline; print(run_ai_setup_inline(None))",
)


@pytest.mark.parametrize("call", _FRESH_CALLS, ids=["init", "ai-test", "setup"])
def test_the_helpers_work_in_a_fresh_interpreter(tmp_path: Path, call: str) -> None:
    # ``provider_resolve`` and ``providers`` import each other, so importing
    # ``provider_resolve`` first fails. ``fluid init`` loads neither before its
    # post-init check, which then raised and lost the "first data product" offer.
    import subprocess

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        # Two providers' keys: every call stops before it could probe an Ollama.
        "OPENAI_API_KEY": OPENAI_KEY,
        "ANTHROPIC_API_KEY": ANTHROPIC_KEY,
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
    }
    result = subprocess.run(
        [sys.executable, "-c", call],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize("spelling", ["MCP-Sampling", "Mcp_Sampling", "Claude-Code", "claude_code"])
def test_ai_test_knows_an_agent_in_any_spelling(world: _World, spelling: str) -> None:
    world.monkeypatch.setenv("FLUID_LLM_PROVIDER", spelling)

    config, error = _ai_test()

    assert config is None
    assert "does not test" in error
    assert spelling.lower().replace("_", "-") in error
