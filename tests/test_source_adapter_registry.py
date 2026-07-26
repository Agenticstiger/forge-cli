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

"""Tests for the pluggable metadata-source registry (issue #247).

``fluid forge data-model from-source`` and the ``forge_from_source`` MCP tool
used to hardcode their source list in an argparse ``choices`` enum, an MCP
``Literal``, and two duplicated dispatch dicts. The
``copilot.catalog.source_registry`` makes the source list a registry that
merges built-ins with ``fluid_build.source_adapters`` entry-point plugins,
mirroring ``fluid_build.providers``.

These tests use the same ``FakeEntryPoint`` monkeypatch idiom as
``tests/test_cli_plugin_hooks.py`` so no real package install is needed.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from fluid_build.copilot.catalog import source_registry as R

_BUILTINS = {
    "snowflake",
    "unity",
    "bigquery",
    "dataplex",
    "glue",
    "datahub",
    "datamesh_manager",
    "postgres",
    "postgresql",
    "mysql",
    "sqlite",
}


class _FakeEntryPoint:
    def __init__(self, name: str, load_value: Any) -> None:
        self.name = name
        self._load_value = load_value

    def load(self) -> Any:
        if isinstance(self._load_value, BaseException):
            raise self._load_value
        return self._load_value


class _FakeEntryPoints(list):
    """Mimics importlib.metadata's EntryPoints (3.10+ ``.select`` + <3.10 ``.get``)."""

    def __init__(self, mapping):
        super().__init__()
        self._mapping = mapping

    def select(self, group=None):
        return list(self._mapping.get(group, []))

    def get(self, group, default=None):
        return list(self._mapping.get(group, default or []))


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: List[_FakeEntryPoint]) -> None:
    import importlib.metadata as md

    def fake_entry_points(*args, **kwargs):
        mapping = {R.EP_GROUP: eps}
        group = kwargs.get("group")
        if group is not None:
            return mapping.get(group, [])
        return _FakeEntryPoints(mapping)

    monkeypatch.setattr(md, "entry_points", fake_entry_points)


class _MyAdapter:
    name = "myhub"

    @classmethod
    def from_resolver(cls, resolver, **kwargs):
        return cls()


@pytest.fixture(autouse=True)
def _reset_registry():
    """Re-seed the module-global registry to built-ins before and after each
    test so a monkeypatched plugin never leaks into another test."""
    R.discover_source_adapters(force=True)
    yield
    R.discover_source_adapters(force=True)


def test_builtins_present_and_classified():
    assert set(R.list_source_adapters()) == _BUILTINS
    assert set(R.list_jdbc_sources()) == {"postgres", "postgresql", "mysql", "sqlite"}
    assert set(R.list_catalog_sources()) == _BUILTINS - set(R.list_jdbc_sources())
    assert R.is_jdbc_source("postgres") and not R.is_jdbc_source("snowflake")


def test_plugin_appears_in_choices_and_dispatches(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("myhub", _MyAdapter)])
    R.discover_source_adapters(force=True)
    assert "myhub" in R.list_source_adapters()
    assert "myhub" in R.list_catalog_sources()  # plugins are catalog-kind
    assert R.resolve_catalog_adapter_class("myhub") is _MyAdapter


def test_plugin_cannot_shadow_builtin(monkeypatch):
    evil = type("EvilSnowflake", (), {})
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("snowflake", evil)])
    R.discover_source_adapters(force=True)
    # Built-in wins: the resolved class is the real adapter, not the plugin.
    cls = R.resolve_catalog_adapter_class("snowflake")
    assert cls is not evil
    assert cls.__name__ == "SnowflakeCatalogAdapter"


def test_discovery_failure_is_fail_open(monkeypatch):
    import importlib.metadata as md

    def boom(*a, **k):
        raise RuntimeError("entry-point backend exploded")

    monkeypatch.setattr(md, "entry_points", boom)
    R.discover_source_adapters(force=True)  # must NOT raise
    assert set(R.list_source_adapters()) == _BUILTINS  # built-ins still seeded


def test_resolve_rejects_jdbc_and_unknown():
    with pytest.raises(RuntimeError):
        R.resolve_catalog_adapter_class("postgres")  # jdbc, not a catalog adapter
    with pytest.raises(RuntimeError):
        R.resolve_catalog_adapter_class("does-not-exist")


def test_cli_choices_are_registry_driven(monkeypatch):
    """The argparse ``--source`` choices reflect the registry, so a plugin
    shows up without editing the CLI."""
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("myhub", _MyAdapter)])
    R.discover_source_adapters(force=True)
    assert "myhub" in R.list_source_adapters()


# ── allow/block policy honesty (#297) ─────────────────────────────────
#
# Only ``resolve_catalog_adapter_class`` enforced the operator allow/block
# policy. Code execution was correctly prevented, but every *listing* surface
# still advertised a blocklisted plugin adapter as ``status: "available"`` and
# offered it as a valid ``--source`` choice — so a caller was handed a source
# that raises the moment it is selected. ``fluid plugins`` has always marked
# blocked entries; these surfaces did not.


def test_a_blocked_plugin_adapter_reports_status_blocked(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("myhub", _MyAdapter)])
    R.discover_source_adapters(force=True)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "myhub")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)

    entry = next(d for d in R.source_adapter_inventory() if d["name"] == "myhub")
    assert entry["status"] == "blocked"
    assert R.is_source_adapter_blocked("myhub") is True

    # …and the code path that actually loads it agrees.
    with pytest.raises(RuntimeError, match="blocked by the operator allow/block policy"):
        R.resolve_catalog_adapter_class("myhub")


def test_an_unblocked_plugin_adapter_is_still_available(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("myhub", _MyAdapter)])
    R.discover_source_adapters(force=True)
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)

    entry = next(d for d in R.source_adapter_inventory() if d["name"] == "myhub")
    assert entry["status"] == "available"


def test_builtins_are_never_policy_gated(monkeypatch):
    """Built-ins are not entry-point plugins; an allowlist must not hide them."""
    _patch_entry_points(monkeypatch, [])
    R.discover_source_adapters(force=True)
    monkeypatch.setenv("FLUID_PLUGINS_ALLOWLIST", "nothing-matches")
    monkeypatch.delenv("FLUID_PLUGINS_BLOCKLIST", raising=False)

    entry = next(d for d in R.source_adapter_inventory() if d["name"] == "snowflake")
    assert entry["status"] == "available"
    assert R.is_source_adapter_blocked("snowflake") is False


def test_the_source_choice_list_omits_blocked_adapters(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("myhub", _MyAdapter)])
    R.discover_source_adapters(force=True)
    monkeypatch.setenv("FLUID_PLUGINS_BLOCKLIST", "myhub")
    monkeypatch.delenv("FLUID_PLUGINS_ALLOWLIST", raising=False)

    assert "myhub" not in R.list_source_adapters(include_blocked=False)
    # The full listing still names it — the "Supported: ..." hint on an
    # unknown-source error should say what is installed.
    assert "myhub" in R.list_source_adapters()
    assert set(R.list_source_adapters(include_blocked=False)) >= _BUILTINS


def test_the_cli_registers_source_choices_without_blocked_adapters():
    """Pin the wiring: the argparse choices come from the filtered list."""
    import inspect

    from fluid_build.cli import _forge_data_model_register as reg

    source = inspect.getsource(reg)
    assert "list_source_adapters(include_blocked=False)" in source
