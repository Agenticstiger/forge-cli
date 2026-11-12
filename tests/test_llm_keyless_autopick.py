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
    for var in _SELECTOR_ENV + _KEY_ENV:
        monkeypatch.delenv(var, raising=False)
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

    def test_saved_config_is_still_configured(self, world):
        world.install("claude-code")
        world.monkeypatch.setattr(
            "fluid_build._ai_config_shared.load_ai_config",
            lambda *a, **k: {"provider": "openai", "model": "gpt-4.1-mini"},
        )
        assert _provider_name_from(_args(), dict(world.env)) is None


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


class TestReadinessAgreesWithSelection:
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

    def test_mcp_sampling_is_not_ready_through_this_check(self, world):
        # It only works inside an MCP host that selects it explicitly.
        ready = check_llm_readiness(
            {"FLUID_LLM_PROVIDER": "mcp-sampling", "PATH": str(world.bindir)}
        )
        assert ready.ready is False

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
