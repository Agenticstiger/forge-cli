# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Keyless auto-pick: a non-interactive run with no provider configured.

Order: a running local Ollama (data stays local), then the first coding-agent CLI
on PATH in ``CODING_AGENT_NAMES`` order, and otherwise no provider at all:
forge then stops with ``copilot_no_llm_provider``, the same "Not selected"
``fluid doctor`` shows. Interactive runs keep "offer, don't force" (the ``fluid ai setup``
inline offer). ``check_llm_readiness`` and the real selection share one helper,
so ``fluid doctor`` never disagrees with what forge then does.

Hermetic: stub executables on a tmp PATH, stdin's ``isatty`` patched, and no
read of the real ``~/.fluid`` config or keyring.
"""

from __future__ import annotations

import logging
import stat
import sys
from types import SimpleNamespace
from unittest import mock

import pytest

from fluid_build.llm import providers
from fluid_build.llm.coding_agent import _AGENT_SPECS, CODING_AGENT_NAMES
from fluid_build.llm.providers import (
    _provider_name_from,
    check_llm_readiness,
    resolve_llm_config,
)

pytestmark = pytest.mark.unit

_SELECTOR_ENV = (
    "FLUID_LLM_PROVIDER",
    "FLUID_FORGE_AGENT",
    "FLUID_LLM_API_KEY",
    "FLUID_LLM_ENDPOINT",
    "FLUID_LLM_MODEL",
    "FLUID_LLM_ROUTING_ENDPOINT",
    "FLUID_LLM_ROUTING_MODEL",
    "OLLAMA_HOST",
)
_KEY_ENV = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_API_KEY",
)


def _args(**kw):
    base = {"llm_provider": None, "llm_model": None, "llm_endpoint": None}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """An empty world: no keys, no saved config, no keyring, no Ollama, empty PATH."""
    for var in _SELECTOR_ENV + _KEY_ENV + ("FLUID_COPILOT_AGENT_LOOP",):
        monkeypatch.delenv(var, raising=False)
    # Anything else that reads ``~/.fluid`` (the model catalog's user override)
    # finds an empty home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda name: None)
    monkeypatch.setattr(providers, "detect_ollama_available", lambda env: False)
    monkeypatch.setattr("fluid_build._ai_config_shared.load_ai_config", lambda *a, **k: None)
    return SimpleNamespace(
        bindir=bindir,
        env={"PATH": str(bindir)},
        monkeypatch=monkeypatch,
        install=lambda *names: [_install(bindir, n) for n in names],
    )


def _install(bindir, agent):
    exe = bindir / _AGENT_SPECS[agent].binary
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)


def _tty(world, value):
    world.monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: value))


def _saved(world, **entry):
    """``fluid ai setup`` saved *entry* as the active provider."""
    world.monkeypatch.setattr(
        "fluid_build._ai_config_shared.load_ai_config", lambda *a, **k: dict(entry)
    )


def _keyring(world, **keys):
    """The OS keyring holds *keys* (provider name to key)."""
    world.monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda name: keys.get(name))


class TestPick:
    def test_claude_only(self, world):
        world.install("claude-code")
        assert _provider_name_from(_args(), dict(world.env)) == "claude-code"

    def test_codex_only(self, world):
        world.install("codex")
        assert _provider_name_from(_args(), dict(world.env)) == "codex"

    @pytest.mark.parametrize("agent", CODING_AGENT_NAMES)
    def test_each_agent_is_found_by_its_own_binary(self, world, agent):
        world.install(agent)
        assert _provider_name_from(_args(), dict(world.env)) == agent

    def test_first_in_coding_agent_order_wins(self, world):
        world.install(*reversed(CODING_AGENT_NAMES))
        assert _provider_name_from(_args(), dict(world.env)) == CODING_AGENT_NAMES[0]

    def test_codex_beats_later_agents(self, world):
        world.install("kiro", "cursor", "codex")
        assert _provider_name_from(_args(), dict(world.env)) == "codex"

    def test_running_ollama_beats_an_agent(self, world):
        world.install("claude-code")
        world.monkeypatch.setattr(providers, "detect_ollama_available", lambda env: True)
        assert _provider_name_from(_args(), dict(world.env)) == "ollama"

    def test_nothing_detected_selects_no_provider(self, world):
        assert _provider_name_from(_args(), dict(world.env)) is None

    def test_non_interactive_flag_overrides_a_tty(self, world):
        world.install("claude-code")
        _tty(world, True)
        assert _provider_name_from(_args(non_interactive=True), dict(world.env)) == "claude-code"

    def test_path_comes_from_the_env_mapping_when_given(self, world, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        _install(other, "codex")
        assert _provider_name_from(_args(), {**world.env, "PATH": str(other)}) == "codex"


class TestInteractiveKeepsOfferDontForce:
    def test_tty_does_not_auto_pick_an_agent(self, world):
        world.install("claude-code")
        _tty(world, True)
        assert _provider_name_from(_args(), dict(world.env)) is None

    def test_tty_still_picks_a_running_ollama(self, world):
        world.install("claude-code")
        _tty(world, True)
        world.monkeypatch.setattr(providers, "detect_ollama_available", lambda env: True)
        assert _provider_name_from(_args(), dict(world.env)) == "ollama"

    def test_closed_stdin_counts_as_non_interactive(self, world):
        world.install("claude-code")
        world.monkeypatch.setattr(sys, "stdin", None)
        assert _provider_name_from(_args(), dict(world.env)) == "claude-code"


class TestConfiguredMeansNoAutoPick:
    def test_explicit_flag_gemini_beats_claude_on_path(self, world):
        world.install("claude-code")
        assert _provider_name_from(_args(llm_provider="gemini"), dict(world.env)) == "gemini"

    def test_explicit_gemini_key_beats_claude_on_path(self, world):
        world.install("claude-code")
        assert _provider_name_from(_args(), {**world.env, "GEMINI_API_KEY": "k"}) == "gemini"

    def test_resolve_with_gemini_key_stays_gemini(self, world):
        world.install("claude-code")
        config = resolve_llm_config(_args(), {"GEMINI_API_KEY": "k"})
        assert config.provider == "gemini"

    @pytest.mark.parametrize("var", ["FLUID_LLM_PROVIDER", "FLUID_FORGE_AGENT"])
    def test_env_selector_wins(self, world, var):
        world.install("claude-code")
        assert _provider_name_from(_args(), {**world.env, var: "codex"}) == "codex"

    def test_two_explicit_keys_are_still_configured(self, world):
        # Ambiguous keys select no provider, and never an agent.
        world.install("claude-code")
        env = {**world.env, "OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}
        assert _provider_name_from(_args(), env) is None

    def test_stated_ollama_host_is_never_swapped_for_an_agent(self, world):
        # Selection treats OLLAMA_HOST as an explicit Ollama choice; the agent
        # rung must agree, in both the real pick and the lean (no litellm) pick.
        world.install("claude-code")
        env = {**world.env, "OLLAMA_HOST": "http://127.0.0.1:11500"}
        assert _provider_name_from(_args(), env) == "ollama"
        assert providers._lean_keyless_autopick(env, non_interactive=True) is None

    def test_generic_api_key_is_configured(self, world):
        world.install("claude-code")
        assert _provider_name_from(_args(), {**world.env, "FLUID_LLM_API_KEY": "k"}) is None

    def test_keyring_key_wins(self, world):
        world.install("claude-code")
        world.monkeypatch.setattr(
            providers, "_get_api_key_from_keyring", lambda n: "k" if n == "anthropic" else None
        )
        assert _provider_name_from(_args(), dict(world.env)) == "anthropic"

    def test_ambiguous_keyring_is_still_configured(self, world):
        world.install("claude-code")
        world.monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda n: "k")
        assert _provider_name_from(_args(), dict(world.env)) is None

    def test_saved_config_beats_an_agent_on_path(self, world):
        world.install("claude-code")
        _saved(world, provider="openai", model="gpt-4.1-mini")
        assert _provider_name_from(_args(), dict(world.env)) == "openai"


class TestSaysWhichWasPicked:
    def test_resolve_keyless_pick_logs_one_line_naming_the_override(self, world, caplog):
        world.install("claude-code")
        with caplog.at_level(logging.WARNING, logger=providers.LOG.name):
            config = resolve_llm_config(_args(), {})
        assert config.provider == "claude-code"
        lines = [r.getMessage() for r in caplog.records if "claude-code" in r.getMessage()]
        assert len(lines) == 1
        assert "--llm-provider" in lines[0]
        assert any("--llm-provider" in note for note in config.model_resolution_notes)

    def test_explicit_agent_choice_is_not_announced(self, world, caplog):
        world.install("claude-code")
        with caplog.at_level(logging.WARNING, logger=providers.LOG.name):
            resolve_llm_config(_args(llm_provider="claude-code"), {})
        assert not [r for r in caplog.records if "--llm-provider" in r.getMessage()]


class TestSavedChoiceSelects:
    """The provider ``fluid ai setup`` saved is one rung of the selection ladder.

    It comes after a named provider (``--llm-provider``, ``FLUID_LLM_PROVIDER``,
    ``FLUID_FORGE_AGENT``) and exactly one key variable, and before a keyring
    key, a running Ollama and the keyless auto-pick: the order
    ``check_llm_readiness`` uses, so a run without a TTY uses the choice
    ``fluid doctor`` shows.
    """

    def test_the_saved_provider_is_selected_without_a_tty(self, world):
        _saved(world, provider="anthropic")
        assert _provider_name_from(_args(non_interactive=True), dict(world.env)) == "anthropic"

    def test_one_key_variable_beats_the_saved_provider(self, world):
        _saved(world, provider="anthropic")
        env = {**world.env, "OPENAI_API_KEY": "k"}  # pragma: allowlist secret
        assert _provider_name_from(_args(), env) == "openai"

    def test_the_saved_provider_breaks_a_tie_between_two_keys(self, world):
        _saved(world, provider="anthropic")
        env = {**world.env, "OPENAI_API_KEY": "k", "GEMINI_API_KEY": "k"}
        assert _provider_name_from(_args(), env) == "anthropic"

    def test_the_saved_provider_beats_a_keyring_key(self, world):
        _saved(world, provider="anthropic")
        _keyring(world, openai="k")
        assert _provider_name_from(_args(), dict(world.env)) == "anthropic"

    def test_the_saved_provider_beats_a_running_ollama(self, world):
        _saved(world, provider="anthropic")
        world.monkeypatch.setattr(providers, "detect_ollama_available", lambda env: True)
        assert _provider_name_from(_args(), dict(world.env)) == "anthropic"

    @pytest.mark.parametrize(
        "flags, env, named",
        [
            ({"llm_provider": "gemini"}, {}, "gemini"),
            ({}, {"FLUID_LLM_PROVIDER": "gemini"}, "gemini"),
            ({}, {"FLUID_FORGE_AGENT": "codex"}, "codex"),
        ],
    )
    def test_a_named_provider_beats_the_saved_one(self, world, flags, env, named):
        _saved(world, provider="anthropic")
        assert _provider_name_from(_args(**flags), {**world.env, **env}) == named


class TestSavedChoiceResolves:
    """The saved model and Ollama host go with the saved provider, and only with it."""

    def test_the_saved_model_and_the_keyring_key(self, world):
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, anthropic="kr-test")  # pragma: allowlist secret
        config = resolve_llm_config(_args(non_interactive=True), dict(world.env))
        assert (config.provider, config.model) == ("anthropic", "claude-saved-test")
        assert config.api_key == "kr-test"  # pragma: allowlist secret

    @pytest.mark.parametrize(
        "flags, env",
        [({"llm_model": "claude-stated"}, {}), ({}, {"FLUID_LLM_MODEL": "claude-stated"})],
    )
    def test_a_stated_model_beats_the_saved_model(self, world, flags, env):
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, anthropic="kr-test")  # pragma: allowlist secret
        config = resolve_llm_config(_args(**flags), {**world.env, **env})
        assert config.model == "claude-stated"

    def test_the_saved_model_goes_only_with_the_saved_provider(self, world):
        _saved(world, provider="anthropic", model="claude-saved-test")
        env = {**world.env, "OPENAI_API_KEY": "k"}  # pragma: allowlist secret
        config = resolve_llm_config(_args(), env)
        assert config.provider == "openai"
        assert config.model_source == "catalog"

    @pytest.mark.parametrize(
        "flags, env",
        [({"llm_provider": "anthropic"}, {}), ({}, {"FLUID_LLM_PROVIDER": "anthropic"})],
    )
    def test_a_named_provider_takes_nothing_from_the_saved_choice(self, world, flags, env):
        # Named means stated for this run: the catalog model, not the saved one.
        _saved(world, provider="anthropic", model="claude-saved-test")
        env = {**world.env, "ANTHROPIC_API_KEY": "k", **env}  # pragma: allowlist secret
        config = resolve_llm_config(_args(**flags), env)
        assert (config.provider, config.model_source) == ("anthropic", "catalog")
        assert config.model != "claude-saved-test"

    @pytest.mark.parametrize("spelling", ["claude", "Anthropic"])
    def test_a_saved_alias_is_its_provider(self, world, spelling):
        # A hand-written ~/.fluid/config.yaml may spell the provider another way.
        _saved(world, provider=spelling, model="claude-saved-test")
        _keyring(world, anthropic="kr-test")  # pragma: allowlist secret
        config = resolve_llm_config(_args(), dict(world.env))
        assert (config.provider, config.model) == ("anthropic", "claude-saved-test")
        assert check_llm_readiness(dict(world.env)).model == "claude-saved-test"

    def test_the_saved_ollama_host_is_where_the_call_goes(self, world):
        _saved(
            world,
            provider="ollama",
            model="llama3.2:saved",
            ollama_host="http://gpu-box.internal:11434",
        )
        config = resolve_llm_config(_args(), dict(world.env))
        assert (config.provider, config.model) == ("ollama", "llama3.2:saved")
        assert config.endpoint == "http://gpu-box.internal:11434"

    def test_an_ollama_host_in_the_environment_beats_the_saved_one(self, world):
        _saved(
            world,
            provider="ollama",
            model="llama3.2:saved",
            ollama_host="http://gpu-box.internal:11434",
        )
        config = resolve_llm_config(_args(), {**world.env, "OLLAMA_HOST": "http://127.0.0.1:11500"})
        assert config.endpoint == "http://127.0.0.1:11500"
        assert config.model == "llama3.2:saved"

    def test_a_saved_agent_is_not_announced_as_auto_picked(self, world, caplog):
        world.install("claude-code")
        _saved(world, provider="claude-code")
        with caplog.at_level(logging.WARNING, logger=providers.LOG.name):
            config = resolve_llm_config(_args(), dict(world.env))
        assert config.provider == "claude-code"
        assert not [r for r in caplog.records if "first coding agent" in r.getMessage()]
        assert not [n for n in config.model_resolution_notes if "first coding agent" in n]

    def test_a_saved_provider_without_a_key_asks_for_the_key(self, world):
        _saved(world, provider="anthropic")
        with pytest.raises(providers.CopilotGenerationError) as info:
            resolve_llm_config(_args(), dict(world.env))
        assert info.value.event == "copilot_missing_llm_api_key"

    def test_a_saved_key_provider_calls_a_stated_endpoint(self, world):
        # As with a keyring key or a key variable: the operator stated where to call.
        _saved(world, provider="openai")
        _keyring(world, openai="kr-test")  # pragma: allowlist secret
        config = resolve_llm_config(_args(llm_endpoint="http://proxy.internal/v1"), dict(world.env))
        assert (config.provider, config.endpoint) == ("openai", "http://proxy.internal/v1")


class TestSavedAgentNeverTakesAStatedDestination:
    """A saved coding agent is passed over wherever the auto-picked one is.

    It cannot call a stated endpoint, cannot be trusted with a stated (perhaps
    on-prem) model and has no tool use for the agent loop. The run goes on
    down the ladder, and stops with ``copilot_no_llm_provider`` naming the
    saved agent when nothing else selects.
    """

    _ONPREM = "http://onprem-llm.internal:8000/v1"

    @pytest.mark.parametrize(
        "flags, env",
        [
            ({"llm_endpoint": _ONPREM}, {}),
            ({"llm_model": "my-model"}, {}),
            ({"agent_loop": True}, {}),
            ({}, {"FLUID_LLM_ENDPOINT": _ONPREM}),
            ({}, {"FLUID_LLM_MODEL": "my-model"}),
            ({}, {"FLUID_COPILOT_AGENT_LOOP": "1"}),
        ],
    )
    def test_the_run_stops_and_names_the_saved_agent(self, world, flags, env):
        world.install("claude-code")
        _saved(world, provider="claude-code")
        with pytest.raises(providers.CopilotGenerationError) as info:
            resolve_llm_config(_args(**flags), {**world.env, **env})
        assert info.value.event == "copilot_no_llm_provider"
        assert "claude-code" in info.value.message
        assert "--llm-provider" in info.value.message

    def test_the_run_goes_on_down_the_ladder(self, world):
        world.install("claude-code")
        _saved(world, provider="claude-code")
        world.monkeypatch.setattr(providers, "detect_ollama_available", lambda env: True)
        assert _provider_name_from(_args(llm_model="llama3.2"), dict(world.env)) == "ollama"

    def test_readiness_says_the_same(self, world):
        world.install("claude-code")
        _saved(world, provider="claude-code")
        ready = check_llm_readiness({**world.env, "FLUID_COPILOT_AGENT_LOOP": "1"})
        assert ready.ready is False
        assert "claude-code" in ready.error

    def test_an_underscore_spelling_is_the_same_agent(self, world, caplog):
        world.install("claude-code")
        _saved(world, provider="claude_code")
        assert _provider_name_from(_args(llm_model="my-model"), dict(world.env)) is None
        with caplog.at_level(logging.WARNING, logger=providers.LOG.name):
            config = resolve_llm_config(_args(), dict(world.env))
        assert config.provider == "claude-code"
        assert not [r for r in caplog.records if "first coding agent" in r.getMessage()]

    def test_a_mission_never_gets_a_saved_agent(self, world):
        # ``fluid mission run`` always runs the tool-use agent loop.
        import argparse

        from fluid_build.cli.mission import _resolve_mission_llm_config

        world.install("claude-code")
        _saved(world, provider="claude-code")
        args = argparse.Namespace(llm_provider=None, llm_model=None, llm_endpoint=None)
        config, message = _resolve_mission_llm_config(args)
        assert config is None
        assert "copilot_no_llm_provider" in message  # stopped before any agent ran
        assert not hasattr(args, "agent_loop")  # the caller's args are left alone


def _ladder_world(world, case):
    """Set up one ladder case; returns the environment to resolve against."""
    env = dict(world.env)
    if case == "saved":
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, anthropic="k")
    elif case == "saved_vs_one_key":
        _saved(world, provider="anthropic", model="claude-saved-test")
        env["OPENAI_API_KEY"] = "k"  # pragma: allowlist secret
    elif case == "saved_vs_two_keys":
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, anthropic="k")
        env.update(OPENAI_API_KEY="k", GEMINI_API_KEY="k")  # pragma: allowlist secret
    elif case == "saved_vs_keyring":
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, openai="k", anthropic="k")
    elif case == "saved_vs_ollama":
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, anthropic="k")
        world.monkeypatch.setattr(providers, "detect_ollama_available", lambda env: True)
    elif case == "saved_vs_agent":
        world.install("claude-code")
        _saved(world, provider="openai", model="gpt-saved-test")
        _keyring(world, openai="k")
    elif case == "saved_agent":
        world.install("claude-code")
        _saved(world, provider="claude-code")
    elif case == "forge_agent_var_vs_key":
        world.install("codex")
        env.update(FLUID_FORGE_AGENT="codex", OPENAI_API_KEY="k")  # pragma: allowlist secret
    elif case == "agent_loop_var":
        world.install("claude-code")
        env["FLUID_COPILOT_AGENT_LOOP"] = "1"
    elif case == "named_var_vs_saved":
        _saved(world, provider="anthropic", model="claude-saved-test")
        _keyring(world, anthropic="k")
        env["FLUID_LLM_PROVIDER"] = "anthropic"
    return env


_LADDER_CASES = (
    "saved",
    "saved_vs_one_key",
    "saved_vs_two_keys",
    "saved_vs_keyring",
    "saved_vs_ollama",
    "saved_vs_agent",
    "saved_agent",
    "forge_agent_var_vs_key",
    "agent_loop_var",
    "named_var_vs_saved",
)


class TestReadinessAgreesWithSelection:
    @pytest.mark.parametrize("case", _LADDER_CASES)
    def test_one_ladder(self, world, case):
        # ``fluid doctor`` names the provider a run without a TTY then uses.
        env = _ladder_world(world, case)
        assert check_llm_readiness(env).provider == _provider_name_from(_args(), env)

    @pytest.mark.parametrize("case", ["saved", "saved_vs_two_keys", "saved_vs_ollama"])
    def test_the_saved_model_is_the_model_both_report(self, world, case):
        env = _ladder_world(world, case)
        ready = check_llm_readiness(env)
        assert ready.ready is True
        assert ready.model == resolve_llm_config(_args(), env).model == "claude-saved-test"

    def test_a_named_provider_reports_its_catalog_model_in_both(self, world):
        env = _ladder_world(world, "named_var_vs_saved")
        ready = check_llm_readiness(env)
        config = resolve_llm_config(_args(), env)
        assert ready.model == config.model != "claude-saved-test"

    def test_readiness_reports_the_same_provider_non_interactive(self, world):
        world.install("codex")
        ready = check_llm_readiness({})
        assert ready.provider == _provider_name_from(_args(), dict(world.env)) == "codex"
        assert ready.ready is True
        assert ready.auth_available is True

    def test_readiness_ready_without_litellm_for_an_agent(self, world):
        world.install("claude-code")
        with mock.patch("fluid_build._optional.is_available", return_value=False):
            ready = check_llm_readiness({})
        assert ready.provider == "claude-code"
        assert ready.ready is True

    def test_readiness_on_a_tty_reports_not_configured(self, world):
        world.install("claude-code")
        _tty(world, True)
        ready = check_llm_readiness({})
        assert ready.ready is False
        assert ready.provider is None

    def test_readiness_prefers_running_ollama(self, world):
        world.install("claude-code")
        world.monkeypatch.setattr(providers, "detect_ollama_available", lambda env: True)
        assert check_llm_readiness({}).provider == "ollama"

    def test_readiness_nothing_detected(self, world):
        assert check_llm_readiness({}).ready is False


class TestStatedDestinationBlocksAutoPick:
    """A stated endpoint, model or provider key is never swapped for a cloud agent."""

    _ONPREM = "http://onprem-llm.internal:8000/v1"

    def test_llm_endpoint_flag(self, world):
        world.install("claude-code")
        assert _provider_name_from(_args(llm_endpoint=self._ONPREM), dict(world.env)) != (
            "claude-code"
        )

    @pytest.mark.parametrize("flag", ["llm_routing_endpoint", "llm_routing_model"])
    def test_routing_flags(self, world, flag):
        world.install("claude-code")
        assert _provider_name_from(_args(**{flag: self._ONPREM}), dict(world.env)) != (
            "claude-code"
        )

    def test_llm_model_flag(self, world):
        world.install("claude-code")
        assert _provider_name_from(_args(llm_model="gpt-4o"), dict(world.env)) != "claude-code"

    def test_resolve_keeps_the_stated_endpoint_off_the_agent(self, world):
        # The security repro: the agent is not picked, so with no provider
        # selected the run stops before any call leaves the machine.
        world.install("claude-code")
        with pytest.raises(providers.CopilotGenerationError) as info:
            resolve_llm_config(_args(llm_endpoint=self._ONPREM, llm_model="my-model"), {})
        assert info.value.event == "copilot_no_llm_provider"

    @pytest.mark.parametrize(
        "var, value",
        [
            ("FLUID_LLM_ENDPOINT", _ONPREM),
            ("FLUID_LLM_MODEL", "my-model"),
            ("FLUID_LLM_ROUTING_ENDPOINT", _ONPREM),
            ("FLUID_LLM_ROUTING_MODEL", "my-model"),
            ("GITHUB_API_KEY", "ghp-test"),
        ],
    )
    def test_env_destination_or_provider_key(self, world, var, value):
        world.install("claude-code")
        env = {**world.env, var: value}
        assert _provider_name_from(_args(), env) != "claude-code"
        world.monkeypatch.setenv(var, value)
        assert check_llm_readiness({}).provider != "claude-code"


class TestAgentLoopNeverAutoPicksAnAgent:
    """The agent loop needs tool use, which no coding agent offers.

    It is requested by ``--agent-loop`` or ``FLUID_COPILOT_AGENT_LOOP``, and the
    provider is resolved from the options ``_forge_build_copilot_options`` builds,
    so both routes must reach the auto-pick.
    """

    @staticmethod
    def _options(**kw):
        from fluid_build.cli.forge_modes import _forge_build_copilot_options

        options = _forge_build_copilot_options(
            SimpleNamespace(**kw),
            get_cli_arg_fn=lambda a, k, d=None: getattr(a, k, d),
            is_non_interactive=True,
        )
        return type("Args", (), options)()

    def test_the_options_carry_the_flag(self):
        assert self._options(agent_loop=True).agent_loop is True
        assert self._options().agent_loop is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_the_options_carry_the_env_route(self, world, value):
        world.monkeypatch.setenv("FLUID_COPILOT_AGENT_LOOP", value)
        assert self._options().agent_loop is True

    def test_flag_route_ends_at_the_no_provider_error(self, world):
        world.install("claude-code")
        with pytest.raises(providers.CopilotGenerationError) as info:
            resolve_llm_config(self._options(agent_loop=True), {})
        assert info.value.event == "copilot_no_llm_provider"

    def test_env_route_ends_at_the_no_provider_error(self, world):
        world.install("claude-code")
        world.monkeypatch.setenv("FLUID_COPILOT_AGENT_LOOP", "1")
        with pytest.raises(providers.CopilotGenerationError) as info:
            resolve_llm_config(self._options(), {})
        assert info.value.event == "copilot_no_llm_provider"

    def test_env_route_blocks_the_pick_in_selection(self, world):
        world.install("claude-code")
        env = {**world.env, "FLUID_COPILOT_AGENT_LOOP": "1"}
        assert _provider_name_from(_args(), env) != "claude-code"

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_a_falsey_env_value_keeps_the_pick(self, world, value):
        world.install("claude-code")
        env = {**world.env, "FLUID_COPILOT_AGENT_LOOP": value}
        assert _provider_name_from(_args(), env) == "claude-code"

    def test_the_loop_check_is_one_rule(self):
        from fluid_build.llm.providers import agent_loop_requested

        assert agent_loop_requested(True, {}) is True
        assert agent_loop_requested(False, {"FLUID_COPILOT_AGENT_LOOP": "on"}) is True
        assert agent_loop_requested(False, {"FLUID_COPILOT_AGENT_LOOP": "0"}) is False

    def test_a_mission_never_auto_picks_an_agent(self, world):
        # ``fluid mission run`` always runs the loop, without the flag.
        import argparse

        from fluid_build.cli.mission import _resolve_mission_llm_config

        world.install("claude-code")
        args = argparse.Namespace(llm_provider=None, llm_model=None, llm_endpoint=None)
        config, message = _resolve_mission_llm_config(args)
        assert config is None
        assert "copilot_no_llm_provider" in message  # stopped before any agent ran


class TestAgentReadinessNeedsTheBinary:
    """``fluid doctor`` says "Ready" for a coding agent only when its CLI is on PATH.

    Auth stays each CLI's own job, so no key is looked for.
    """

    @pytest.mark.parametrize("agent", CODING_AGENT_NAMES)
    def test_uninstalled_agent_is_not_ready(self, world, agent):
        ready = check_llm_readiness({"FLUID_LLM_PROVIDER": agent, "PATH": "/usr/bin:/bin"})
        assert ready.ready is False
        assert ready.provider == agent
        assert ready.auth_available is False
        assert _AGENT_SPECS[agent].binary in ready.error

    @pytest.mark.parametrize("agent", CODING_AGENT_NAMES)
    def test_installed_agent_is_ready_without_a_key(self, world, agent):
        world.install(agent)
        ready = check_llm_readiness({**world.env, "FLUID_LLM_PROVIDER": agent})
        assert ready.ready is True
        assert ready.auth_available is True

    def test_installed_agent_is_ready_without_litellm(self, world):
        world.install("codex")
        with mock.patch("fluid_build._optional.is_available", return_value=False):
            ready = check_llm_readiness({**world.env, "FLUID_LLM_PROVIDER": "codex"})
        assert ready.ready is True

    def test_the_binary_is_searched_on_the_given_environments_path(self, world):
        # Installed on the process PATH, absent from the environment handed in.
        world.install("codex")
        ready = check_llm_readiness({"FLUID_LLM_PROVIDER": "codex", "PATH": "/usr/bin:/bin"})
        assert ready.ready is False

    def test_ollama_is_unchanged(self, world):
        with mock.patch("fluid_build._optional.is_available", return_value=True):
            ready = check_llm_readiness({"FLUID_LLM_PROVIDER": "ollama", "PATH": "/usr/bin:/bin"})
        assert ready.ready is True
        assert ready.provider == "ollama"

    def test_a_plugin_declaring_keyless_is_still_ready(self, world):
        class _Plugin:
            name = "myplugin"
            default_model = "m"
            keyless = True

            def default_endpoint(self, model, env):
                return ""

        world.monkeypatch.setitem(providers.BUILTIN_LLM_PROVIDERS, "myplugin", _Plugin())
        with mock.patch("fluid_build._optional.is_available", return_value=True):
            ready = check_llm_readiness(
                {"FLUID_LLM_PROVIDER": "myplugin", "PATH": str(world.bindir)}
            )
        assert ready.ready is True


class TestCommandCenterDraftingIsUnchanged:
    """Command Center's drafting call keeps its provider and its catalog model.

    ``fluid_cc_backend/app/services/fluid_cli_service.py::forge_from_intent`` runs
    ``fluid forge data-model from-intent <intent> --output <f> --technique
    dimensional --quiet --llm-provider <p>`` with the key in the environment and
    no ``--llm-model``. Its container has no saved ``fluid ai setup`` choice, and
    a saved one would change nothing: the provider is named.
    """

    @staticmethod
    def _cc_args(provider):
        import argparse

        from fluid_build.cli import forge as forge_mod

        parser = argparse.ArgumentParser()
        forge_mod.register(parser.add_subparsers())
        argv = ["forge", "data-model", "from-intent", "intent.fluid.yaml"]
        argv += ["--output", "contract.fluid.yaml", "--technique", "dimensional", "--quiet"]
        return parser.parse_args([*argv, "--llm-provider", provider])

    @pytest.mark.parametrize("saved", [None, "another provider", "the same provider"])
    @pytest.mark.parametrize(
        "provider, key_vars, canonical",
        [
            ("openai", ("OPENAI_API_KEY",), "openai"),
            ("anthropic", ("ANTHROPIC_API_KEY",), "anthropic"),
            ("claude", ("ANTHROPIC_API_KEY",), "anthropic"),
            ("gemini", ("GOOGLE_API_KEY", "GEMINI_API_KEY"), "gemini"),
        ],
    )
    def test_provider_and_model(self, world, saved, provider, key_vars, canonical):
        from fluid_build.llm.model_catalog import get_catalog_default

        if saved == "another provider":
            _saved(world, provider="ollama", model="llama3.2:saved")
        elif saved == "the same provider":
            # A named provider takes nothing from the saved choice.
            _saved(world, provider=canonical, model="saved-model-test")
        env = {**world.env, **{var: "k" for var in key_vars}}
        config = resolve_llm_config(self._cc_args(provider), env)
        assert config.provider == canonical
        expected = (
            get_catalog_default(canonical) or providers.get_llm_provider(canonical).default_model
        )
        assert (config.model, config.model_source) == (expected, "catalog")
