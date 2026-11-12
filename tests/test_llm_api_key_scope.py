# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``FLUID_LLM_API_KEY`` goes only to a provider the operator named.

The key does not say which vendor it is for. Forge used to send it to any
provider it picked, so an OpenAI key went to a running Ollama, to the host
``OLLAMA_HOST`` names, or to Anthropic when the keyring held one Anthropic key.
Named means ``--llm-provider`` or ``FLUID_LLM_PROVIDER`` names the provider the
run uses; every other way forge picks one leaves the key unused.

Hermetic: the keyring, the Ollama probe, the saved ``fluid ai setup`` config and
``HOME`` are patched, so nothing reads the real ``~/.fluid`` or dials a daemon
(the one listener is a loopback stand-in).
"""

from __future__ import annotations

import logging
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, Optional
from unittest import mock

import pytest

import fluid_build.llm.providers as providers
from tests._dotenv_pairing_support import Listener, refuse_outbound_dns, send_if_loopback

GENERIC_KEY = "generic-llm-key-test-0001"  # pragma: allowlist secret
ANTHROPIC_KEY = "anthropic-keyring-key-test-0002"  # pragma: allowlist secret
OPENAI_KEY = "openai-env-key-test-0003"  # pragma: allowlist secret


class _World:
    """What the machine holds: keyring keys, a running Ollama, a saved provider."""

    def __init__(self, bin_dir: Path) -> None:
        self.keyring: Dict[str, str] = {}
        self.ollama_up = False
        self.saved: Optional[dict] = None
        self.bin_dir = bin_dir

    def env(self, **values: str) -> Dict[str, str]:
        """An environment with only *values*; PATH finds no coding-agent CLI."""
        return {"PATH": str(self.bin_dir), **values}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_World]:
    import fluid_build._ai_config_shared as ai_config

    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    w = _World(bin_dir)
    monkeypatch.setattr(providers, "_get_api_key_from_keyring", lambda name: w.keyring.get(name))
    monkeypatch.setattr(providers, "detect_ollama_available", lambda env: w.ollama_up)
    monkeypatch.setattr(providers, "resolve_ollama_model", lambda env, endpoint=None: "llama3.2")
    monkeypatch.setattr(ai_config, "load_ai_config", lambda config_file=None: w.saved)
    monkeypatch.setattr(
        ai_config,
        "load_ai_config_for",
        lambda name, config_file=None: w.saved if (w.saved or {}).get("provider") == name else None,
    )
    providers.reset_llm_caches()
    yield w
    providers.reset_llm_caches()


def _args(**values: object) -> SimpleNamespace:
    base = {"llm_provider": None, "llm_model": None, "llm_endpoint": None}
    return SimpleNamespace(**{**base, **values})


# ── a provider forge picks itself never gets the key ───────────────────────


def test_a_running_ollama_never_gets_the_generic_key(world: _World) -> None:
    world.ollama_up = True

    config = providers.resolve_llm_config(_args(), world.env(FLUID_LLM_API_KEY=GENERIC_KEY))

    assert config.provider == "ollama"
    assert config.api_key is None
    assert any("FLUID_LLM_API_KEY is set but not used" in n for n in config.model_resolution_notes)


def test_the_host_ollama_host_names_never_gets_the_generic_key(world: _World) -> None:
    # OLLAMA_HOST alone picks ollama; it does not name it. The key would go out as
    # "Authorization: Bearer", here in cleartext to another machine.
    remote = "http://gpu-box.example.test:11434"
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, OLLAMA_HOST=remote)

    config = providers.resolve_llm_config(_args(), env)
    with mock.patch.dict(os.environ, {"OLLAMA_HOST": remote}):
        _headers, payload = providers.get_llm_provider("ollama").build_request(config, "s", "u")

    assert config.provider == "ollama"
    assert config.api_key is None
    assert "api_key" not in payload
    assert payload["api_base"] == remote


def test_the_one_keyring_key_s_provider_gets_that_key(world: _World) -> None:
    world.keyring["anthropic"] = ANTHROPIC_KEY

    config = providers.resolve_llm_config(_args(), world.env(FLUID_LLM_API_KEY=GENERIC_KEY))

    assert config.provider == "anthropic"
    assert config.api_key == ANTHROPIC_KEY


def test_the_one_key_variable_s_provider_gets_that_key(world: _World) -> None:
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, OPENAI_API_KEY=OPENAI_KEY)

    config = providers.resolve_llm_config(_args(), env)

    assert config.provider == "openai"
    assert config.api_key == OPENAI_KEY


def test_the_saved_choice_s_provider_gets_its_own_key(world: _World) -> None:
    # A run without a TTY uses the provider ``fluid ai setup`` saved. That
    # provider is picked, not named, so it gets its keyring key, never the
    # generic one. Two keyring keys, so the keyring rung picks nothing itself.
    world.saved = {"provider": "anthropic", "model": "claude-sonnet-4-5"}
    world.keyring.update(anthropic=ANTHROPIC_KEY, openai=OPENAI_KEY)
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY)

    config = providers.resolve_llm_config(_args(non_interactive=True), env)
    readiness = providers.check_llm_readiness(env)

    assert config.provider == "anthropic"
    assert config.api_key == ANTHROPIC_KEY
    assert any("FLUID_LLM_API_KEY is set but not used" in n for n in config.model_resolution_notes)
    assert readiness.provider == "anthropic"


def test_the_saved_choice_without_a_key_of_its_own_is_not_ready(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The generic key does not make the saved provider ready either.
    monkeypatch.setattr("fluid_build._optional.is_available", lambda name: True)
    world.saved = {"provider": "anthropic", "model": "claude-sonnet-4-5"}
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY)

    with pytest.raises(providers.CopilotGenerationError) as info:
        providers.resolve_llm_config(_args(non_interactive=True), env)
    readiness = providers.check_llm_readiness(env)

    assert info.value.event == "copilot_missing_llm_api_key"
    assert readiness.provider == "anthropic" and readiness.ready is False


def test_the_key_never_reaches_an_ollama_on_the_wire(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's capture: litellm's ollama/ route sends the api_key it is
    # handed as "Authorization: Bearer". A loopback listener stands in for Ollama.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no price-map fetch
    refuse_outbound_dns(monkeypatch)
    litellm = pytest.importorskip("litellm")
    listener = Listener()
    try:
        monkeypatch.setenv("OLLAMA_HOST", listener.url)
        env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, OLLAMA_HOST=listener.url)
        config = providers.resolve_llm_config(_args(), env)
        _headers, payload = providers.get_llm_provider("ollama").build_request(config, "s", "u")
        send_if_loopback(
            payload.get("api_base"),
            lambda: litellm.completion(**{**payload, "num_retries": 0, "timeout": 5}),
        )

        if not listener.requests:
            pytest.skip("litellm sent no request to the listener, so it proves nothing")
        assert listener.saw(GENERIC_KEY) == []
    finally:
        listener.close()


# ── a named provider still gets it ─────────────────────────────────────────


@pytest.mark.parametrize("how", ["flag", "env"])
@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
def test_a_named_provider_gets_the_generic_key_first(
    world: _World, how: str, provider: str
) -> None:
    # A named Ollama may be ollama.com or a token proxy; a named vendor's own
    # variable still comes second.
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, ANTHROPIC_API_KEY=ANTHROPIC_KEY)
    if how == "env":
        env["FLUID_LLM_PROVIDER"] = provider
        args = _args()
    else:
        args = _args(llm_provider=provider)

    config = providers.resolve_llm_config(args, env)

    assert config.provider == provider
    assert config.api_key == GENERIC_KEY
    assert not any("FLUID_LLM_API_KEY" in n for n in config.model_resolution_notes)


def test_a_provider_with_no_key_of_its_own_says_why_the_generic_one_was_not_used(
    world: _World,
) -> None:
    # Forge seldom picks a provider that then has no key of its own; patch the
    # pick to reach that error, whose first suggestion is the way out.
    with mock.patch.object(providers, "_provider_name_from", return_value="openai"):
        with pytest.raises(providers.CopilotGenerationError) as info:
            providers.resolve_llm_config(_args(), world.env(FLUID_LLM_API_KEY=GENERIC_KEY))

    assert info.value.event == "copilot_missing_llm_api_key"
    assert "Pass --llm-provider openai" in info.value.suggestions[0]


# ── interactive setup's pick is not a name ─────────────────────────────────


def test_interactive_setup_s_pick_does_not_count_as_named(world: _World) -> None:

    args = _args()
    providers.remember_named_llm_provider(args, {})
    args.llm_provider = "openai"  # what forge does with run_ai_setup_inline's config
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, OPENAI_API_KEY=OPENAI_KEY)

    config = providers.resolve_llm_config(args, env)

    assert config.api_key == OPENAI_KEY


def test_setup_replacing_a_named_provider_does_not_carry_the_key_over(world: _World) -> None:

    args = _args(llm_provider="anthropic")
    providers.remember_named_llm_provider(args, {})
    args.llm_provider = "openai"  # the saved provider, loaded over the flag
    env = world.env(FLUID_LLM_API_KEY=ANTHROPIC_KEY, OPENAI_API_KEY=OPENAI_KEY)

    config = providers.resolve_llm_config(args, env)

    assert config.api_key == OPENAI_KEY
    notes = " ".join(config.model_resolution_notes)
    assert "it is for anthropic, the provider you named" in notes
    assert "Pass --llm-provider" not in notes


@pytest.mark.parametrize("named,picked", [("claude", "anthropic"), ("openai", "openai")])
def test_setup_keeping_the_named_provider_keeps_the_key(
    world: _World, named: str, picked: str
) -> None:

    args = _args(llm_provider=named)
    providers.remember_named_llm_provider(args, {})
    args.llm_provider = picked

    config = providers.resolve_llm_config(args, world.env(FLUID_LLM_API_KEY=GENERIC_KEY))

    assert config.api_key == GENERIC_KEY


def test_the_first_record_is_kept(world: _World) -> None:

    args = _args()
    providers.remember_named_llm_provider(args, {})
    args.llm_provider = "openai"
    providers.remember_named_llm_provider(args, {})  # a second setup must not name its own pick

    assert args.llm_provider_named is None


def test_the_copilot_options_carry_the_record(world: _World) -> None:
    from fluid_build.cli.forge import get_cli_arg
    from fluid_build.cli.forge_modes import _forge_build_copilot_options

    args = Namespace(llm_provider="openai", llm_provider_named=None)
    options = _forge_build_copilot_options(
        args, get_cli_arg_fn=get_cli_arg, is_non_interactive=False
    )
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, OPENAI_API_KEY=OPENAI_KEY)

    config = providers.resolve_llm_config(SimpleNamespace(**options), env)

    assert options["llm_provider_named"] is None
    assert config.api_key == OPENAI_KEY
    # without a record nothing is added, so a flag still names the provider
    plain = _forge_build_copilot_options(
        Namespace(llm_provider="openai"), get_cli_arg_fn=get_cli_arg, is_non_interactive=False
    )
    assert "llm_provider_named" not in plain


@pytest.fixture
def fake_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


@pytest.mark.parametrize("flag", [None, "anthropic"])
def test_interactive_forge_records_what_was_named_before_setup_s_pick(
    world: _World,
    fake_tty: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: Optional[str],
) -> None:
    from fluid_build.cli.forge import run as forge_run

    monkeypatch.setattr("fluid_build.cli._welcome_scan.Path.home", lambda: tmp_path / "home")
    monkeypatch.setenv("FLUID_FORGE_NO_PREVIEW", "1")
    for name in ("FLUID_LLM_PROVIDER", "FLUID_FORGE_AGENT"):
        monkeypatch.delenv(name, raising=False)
    seen: Dict[str, Namespace] = {}

    def _copilot(args: Namespace, _logger: logging.Logger) -> int:
        seen["args"] = args
        return 0

    def _setup(_console: object) -> providers.LlmConfig:
        return providers.LlmConfig(
            provider="openai", model="gpt-4o", endpoint="", api_key=OPENAI_KEY
        )

    args = Namespace(
        command="forge",
        forge_subcommand=None,
        blank=False,
        target_dir=str(tmp_path),
        non_interactive=False,
        dry_run=False,
        llm_provider=flag,
        llm_model=None,
        llm_endpoint=None,
        agent_loop=False,
        refine=None,
        from_product=[],
        from_product_list=None,
        template=None,
        scaffold=None,
    )
    with (
        mock.patch("fluid_build.cli._forge_mode_picker.pick_mode", lambda a, **k: "ai"),
        mock.patch("fluid_build.cli._forge_mode_picker.should_show_picker", lambda a: True),
        mock.patch("fluid_build.cli.forge.run_ai_copilot_mode", _copilot),
        mock.patch("fluid_build.cli.ai_setup.run_ai_setup_inline", _setup),
        mock.patch("fluid_build.cli.ai_setup.set_session_env", lambda provider, key: None),
    ):
        forge_run(args, logging.getLogger("test"))

    ran_with = seen["args"]
    assert ran_with.llm_provider == "openai"
    assert ran_with.llm_provider_named == flag
    env = world.env(FLUID_LLM_API_KEY=GENERIC_KEY, OPENAI_API_KEY=OPENAI_KEY)
    assert providers.resolve_llm_config(ran_with, env).api_key == OPENAI_KEY


# ── fluid doctor and fluid ai test ─────────────────────────────────────────


def test_readiness_counts_the_key_only_for_a_named_provider(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fluid_build._optional.is_available", lambda name: True)
    world.saved = {"provider": "openai", "model": "gpt-4o"}

    picked = providers.check_llm_readiness(world.env(FLUID_LLM_API_KEY=GENERIC_KEY))
    named = providers.check_llm_readiness(
        world.env(FLUID_LLM_API_KEY=GENERIC_KEY, FLUID_LLM_PROVIDER="openai")
    )

    assert picked.provider == "openai"
    assert picked.ready is False
    assert named.ready is True


def _ai_test_config(args: Namespace, env: Dict[str, str], saved: Optional[dict], keyring: str):
    from fluid_build.cli.ai_setup import _resolve_ai_test_config

    with (
        mock.patch("fluid_build.cli.ai_setup._load_ai_config", return_value=saved),
        mock.patch("fluid_build.cli.ai_setup._load_key_from_keyring", return_value=keyring),
        mock.patch("fluid_build.cli.ai_setup.detect_ollama_available", return_value=False),
        # keep the fixture's HOME: without one, ``~`` is the real home again
        mock.patch.dict(os.environ, {**env, "HOME": os.environ["HOME"]}, clear=True),
    ):
        return _resolve_ai_test_config(args)


def _ai_test_args(provider: Optional[str]) -> Namespace:
    return Namespace(
        llm_provider=provider, llm_model=None, llm_endpoint=None, llm_timeout_seconds=None
    )


def test_ai_test_uses_the_saved_provider_s_own_key(world: _World) -> None:
    config, error = _ai_test_config(
        _ai_test_args(None),
        {"FLUID_LLM_API_KEY": GENERIC_KEY},
        {"provider": "anthropic", "model": "claude-sonnet-4-5"},
        ANTHROPIC_KEY,
    )

    assert error is None
    assert config.provider == "anthropic"
    assert config.api_key == ANTHROPIC_KEY


def test_ai_test_uses_the_key_variable_s_own_key(world: _World) -> None:
    config, error = _ai_test_config(
        _ai_test_args(None),
        {"FLUID_LLM_API_KEY": GENERIC_KEY, "OPENAI_API_KEY": OPENAI_KEY},
        None,
        "",
    )

    assert error is None
    assert config.provider == "openai"
    assert config.api_key == OPENAI_KEY


def test_ai_test_with_llm_provider_uses_the_generic_key(world: _World) -> None:
    config, error = _ai_test_config(
        _ai_test_args("openai"), {"FLUID_LLM_API_KEY": GENERIC_KEY}, None, OPENAI_KEY
    )

    assert error is None
    assert config.api_key == GENERIC_KEY
