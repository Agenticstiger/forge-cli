# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the custom LLM provider plugin system (Trello 69d4c9ce).

Pins the entry-point discovery + registration contract:

* a ``fluid_build.llm_providers`` entry point resolves via
  ``get_llm_provider('<name>')`` and ``--llm-provider <name>`` exactly like a
  built-in;
* a plugin name that clashes with a built-in NEVER shadows it (built-ins win);
* a broken / invalid plugin is skipped with a warning and never crashes the CLI;
* discovery is lazy — ``fluid --help`` / ``build_parser()`` never scan the group;
* the operator allow/block policy governs the group, and ``fluid plugins`` +
  ``fluid doctor`` surface it.

Discovery is exercised for real (through ``plugin_manager.iter_plugins``); only
``importlib.metadata.entry_points`` is faked so no package needs installing.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import logging
from typing import Any, List, Mapping

import pytest

from fluid_build.cli import _llm_provider_plugins as plugins
from fluid_build.cli._llm_provider_plugins import (
    LLM_PROVIDER_GROUP,
    LlmProviderChoices,
    discovered_plugin_names,
    get_plugin_llm_provider,
    get_plugin_registry,
    is_plugin_provider,
    reset_plugin_registry,
)
from fluid_build.cli.forge_copilot_llm_providers import LlmProvider, get_llm_provider

# ---------------------------------------------------------------------------
# Stub providers + fake entry points
# ---------------------------------------------------------------------------


class _StubProvider(LlmProvider):
    """A minimal third-party ``LlmProvider`` returned by a plugin entry point."""

    name = "azure-openai"
    default_model = "azure-gpt-4o"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        return "https://stub.invalid/openai"

    def build_request(self, config, system_prompt, user_prompt):
        return ({}, {})

    def extract_text(self, response_json):
        return str(response_json.get("text", ""))

    def invoke_blocking(self, config, system_prompt, user_prompt, *, extra_payload=None) -> str:
        # A canned contract-JSON envelope — proves the plugin is wired without a key.
        return '{"apiVersion": "fluid/v0.7.3", "kind": "DataProduct"}'


class _KeylessProvider(_StubProvider):
    name = "azure-keyless"
    default_model = "azure-keyless-model"
    keyless = True


class _ShadowOpenAI(_StubProvider):
    """A malicious/careless plugin that tries to register as ``openai``."""

    name = "openai"


class FakeEntryPoint:
    """Stand-in for ``importlib.metadata.EntryPoint``.

    ``load()`` returns ``load_value`` directly, unless it's an ``Exception``
    instance, in which case ``load()`` raises it (models a broken plugin).
    """

    def __init__(self, name: str, load_value: Any) -> None:
        self.name = name
        self._load_value = load_value

    def load(self) -> Any:
        if isinstance(self._load_value, BaseException):
            raise self._load_value
        return self._load_value


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Reset the module-level plugin registry around every test."""
    reset_plugin_registry()
    yield
    reset_plugin_registry()


def _install_eps(monkeypatch: pytest.MonkeyPatch, eps: List[FakeEntryPoint]) -> None:
    """Patch ``importlib.metadata.entry_points`` so the llm_providers group
    yields ``eps`` and every other group yields ``[]``."""

    def fake_entry_points(group: str | None = None, **_: Any):
        if group == LLM_PROVIDER_GROUP:
            return list(eps)
        return []

    monkeypatch.setattr(md, "entry_points", fake_entry_points)


# ---------------------------------------------------------------------------
# (1) A plugin entry point resolves like a built-in
# ---------------------------------------------------------------------------


def test_class_entry_point_resolves_via_get_llm_provider(monkeypatch):
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])

    provider = get_llm_provider("azure-openai")
    assert isinstance(provider, _StubProvider)
    assert provider.name == "azure-openai"
    # The canned envelope proves invoke_blocking is the plugin's.
    assert "fluid/v0.7.3" in provider.invoke_blocking(None, "sys", "usr")

    assert "azure-openai" in discovered_plugin_names()
    assert is_plugin_provider("azure-openai")
    assert "azure-openai" in get_plugin_registry()


def test_factory_entry_point_is_supported(monkeypatch):
    """An entry point may resolve to a zero-arg factory returning an instance."""
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", lambda: _StubProvider())])
    provider = get_llm_provider("azure-openai")
    assert isinstance(provider, _StubProvider)


def test_instance_entry_point_is_supported(monkeypatch):
    """An entry point may resolve directly to an ``LlmProvider`` instance."""
    inst = _StubProvider()
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", inst)])
    assert get_llm_provider("azure-openai") is inst


def test_hyphen_underscore_lookup_tolerance(monkeypatch):
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    # Registered under the hyphen name; resolves for the underscore form too.
    assert get_plugin_llm_provider("azure_openai") is not None
    assert get_plugin_llm_provider("azure-openai") is not None


# ---------------------------------------------------------------------------
# (3) A plugin must NOT shadow a built-in — built-ins win
# ---------------------------------------------------------------------------


def test_plugin_cannot_shadow_builtin(monkeypatch, caplog):
    _install_eps(monkeypatch, [FakeEntryPoint("openai", _ShadowOpenAI)])

    with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot.llm.plugins"):
        registry = get_plugin_registry()

    # The shadowing plugin never enters the registry.
    assert "openai" not in registry
    assert any("shadows a built-in" in r.message for r in caplog.records)

    # get_llm_provider('openai') resolves the built-in (litellm shim), not the plugin.
    provider = get_llm_provider("openai")
    assert not isinstance(provider, _ShadowOpenAI)
    assert provider.name == "openai"


def test_plugin_cannot_shadow_builtin_via_provider_name(monkeypatch):
    """An entry point named innocuously but whose provider.name is a built-in
    does not get aliased onto the built-in name."""
    _install_eps(monkeypatch, [FakeEntryPoint("sneaky", _ShadowOpenAI)])
    registry = get_plugin_registry()
    # The entry-point name registers, but the built-in 'openai' alias is refused.
    assert "sneaky" in registry
    assert "openai" not in registry


# ---------------------------------------------------------------------------
# (4) Broken / invalid plugins are skipped with a warning; CLI still works
# ---------------------------------------------------------------------------


def test_broken_entry_point_is_skipped(monkeypatch, caplog):
    eps = [
        FakeEntryPoint("broken", ImportError("no such module")),
        FakeEntryPoint("azure-openai", _StubProvider),
    ]
    _install_eps(monkeypatch, eps)

    with caplog.at_level(logging.WARNING):
        names = discovered_plugin_names()

    # The good plugin still resolves; the broken one is dropped.
    assert names == ["azure-openai"]
    assert get_plugin_llm_provider("broken") is None
    assert get_plugin_llm_provider("azure-openai") is not None
    assert any("failed to load" in r.message for r in caplog.records)


def test_non_provider_object_is_skipped(monkeypatch, caplog):
    """A loaded object that isn't an LlmProvider is skipped, not crashed on."""
    _install_eps(monkeypatch, [FakeEntryPoint("bogus", object)])
    with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot.llm.plugins"):
        names = discovered_plugin_names()
    assert names == []
    assert any("not a valid LlmProvider" in r.message for r in caplog.records)


def test_unknown_name_falls_through_to_litellm(monkeypatch):
    """A name that is neither a built-in nor a plugin still returns a provider
    (litellm's catch-all) — get_llm_provider never raises for an unknown name."""
    _install_eps(monkeypatch, [])
    provider = get_llm_provider("some-litellm-native-provider")
    assert isinstance(provider, LlmProvider)


# ---------------------------------------------------------------------------
# (5) Discovery is lazy — --help / build_parser never scans the group
# ---------------------------------------------------------------------------


def test_build_parser_does_not_scan_llm_provider_group(monkeypatch):
    scanned_groups: List[str | None] = []
    _orig = md.entry_points

    def tracing_entry_points(group: str | None = None, **kw: Any):
        scanned_groups.append(group)
        return _orig(group=group, **kw) if group is not None else _orig()

    monkeypatch.setattr(md, "entry_points", tracing_entry_points)
    reset_plugin_registry()

    from fluid_build.cli import build_parser

    build_parser()

    assert LLM_PROVIDER_GROUP not in scanned_groups
    # The registry was never built as a side effect of parser construction.
    assert plugins._registry is None


def test_choices_iteration_yields_builtins_without_discovery(monkeypatch):
    # Iterating (what --help does) must not trigger discovery.
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    reset_plugin_registry()
    choices = LlmProviderChoices()
    listed = list(choices)
    assert "openai" in listed and "claude-code" in listed
    assert "azure-openai" not in listed
    assert plugins._registry is None  # iteration did not discover

    # Membership for a built-in also short-circuits before discovery.
    assert "gemini" in choices
    assert plugins._registry is None

    # Membership for a non-built-in DOES consult the registry (lazy discovery).
    assert "azure-openai" in choices
    assert plugins._registry is not None


def test_choices_rejects_unknown_but_accepts_plugin(monkeypatch):
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    choices = LlmProviderChoices()
    assert "azure-openai" in choices
    assert "totally-unknown-xyz" not in choices


# ---------------------------------------------------------------------------
# argparse integration — --llm-provider accepts a plugin name
# ---------------------------------------------------------------------------


def test_argparse_accepts_plugin_and_rejects_typo(monkeypatch):
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-provider", choices=LlmProviderChoices())

    ns = parser.parse_args(["--llm-provider", "azure-openai"])
    assert ns.llm_provider == "azure-openai"

    ns2 = parser.parse_args(["--llm-provider", "ollama"])
    assert ns2.llm_provider == "ollama"

    with pytest.raises(SystemExit):
        parser.parse_args(["--llm-provider", "not-a-provider"])


# ---------------------------------------------------------------------------
# Allow/block policy governs the group
# ---------------------------------------------------------------------------


def test_blocklist_suppresses_plugin(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "azure-openai")
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    assert get_plugin_llm_provider("azure-openai") is None
    assert discovered_plugin_names() == []


def test_allowlist_limits_plugins(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "some-other-plugin")
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    assert get_plugin_llm_provider("azure-openai") is None


# ---------------------------------------------------------------------------
# resolve_llm_config — keyless plugin hook + key gate
# ---------------------------------------------------------------------------


# A non-empty env with no API keys — ``resolve_llm_config`` does
# ``dict(environ or os.environ)``, so an empty dict would fall back to the real
# process env. This keeps the resolve tests hermetic.
_NO_KEYS_ENV = {"FLUID_NO_KEYS": "1"}


def _no_keyring(monkeypatch):
    """Neutralise the OS-keyring fallback so resolve tests are deterministic."""
    import fluid_build.cli.forge_copilot_llm_providers as host

    monkeypatch.setattr(host, "_get_api_key_from_keyring", lambda provider: None)


def test_resolve_llm_config_keyless_plugin(monkeypatch):
    from fluid_build.cli.forge_copilot_llm_providers import resolve_llm_config

    _no_keyring(monkeypatch)
    _install_eps(monkeypatch, [FakeEntryPoint("azure-keyless", _KeylessProvider)])
    args = argparse.Namespace(llm_provider="azure-keyless")
    cfg = resolve_llm_config(args, environ=_NO_KEYS_ENV)
    assert cfg.provider == "azure-keyless"
    assert cfg.model == "azure-keyless-model"


def test_resolve_llm_config_non_keyless_plugin_requires_key(monkeypatch):
    from fluid_build.cli.forge_copilot_llm_providers import (
        CopilotGenerationError,
        resolve_llm_config,
    )

    _no_keyring(monkeypatch)
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    args = argparse.Namespace(llm_provider="azure-openai")
    with pytest.raises(CopilotGenerationError) as exc:
        resolve_llm_config(args, environ=_NO_KEYS_ENV)
    assert exc.value.event == "copilot_missing_llm_api_key"


# ---------------------------------------------------------------------------
# Inspection surfaces — fluid plugins + governed groups
# ---------------------------------------------------------------------------


def test_installed_plugins_includes_llm_provider_group(monkeypatch):
    from fluid_build.plugin_manager import (
        LLM_PROVIDER_GROUP_KEY,
        governed_groups,
        installed_plugins,
    )

    assert LLM_PROVIDER_GROUP_KEY in governed_groups()
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    data = installed_plugins(LLM_PROVIDER_GROUP_KEY)
    names = [e["name"] for e in data.get(LLM_PROVIDER_GROUP_KEY, [])]
    assert "azure-openai" in names


def test_doctor_surfaces_installed_provider_plugins_by_name_only(monkeypatch):
    """`fluid doctor` lists provider plugins by NAME without loading them."""
    from fluid_build.cli.doctor import _check_fluid_features

    loaded: list = []

    class _TracingEP(FakeEntryPoint):
        def load(self):  # pragma: no cover - asserted NOT called
            loaded.append(self.name)
            return super().load()

    _install_eps(monkeypatch, [_TracingEP("azure-openai", _StubProvider)])
    _all_ok, checks = _check_fluid_features()

    plugin_check = next((c for c in checks if c["check"] == "LLM provider plugins"), None)
    assert plugin_check is not None
    assert "azure-openai" in plugin_check["details"]
    # The health check read entry-point names only — it never executed plugin code.
    assert loaded == []


class _OtherProvider(_StubProvider):
    name = "other-provider"
    default_model = "other-model"


def test_reset_plugin_registry_refreshes(monkeypatch):
    _install_eps(monkeypatch, [FakeEntryPoint("azure-openai", _StubProvider)])
    assert discovered_plugin_names() == ["azure-openai"]

    # Swap the installed entry points and reset — the new set is discovered.
    _install_eps(monkeypatch, [FakeEntryPoint("other-provider", _OtherProvider)])
    reset_plugin_registry()
    assert discovered_plugin_names() == ["other-provider"]
