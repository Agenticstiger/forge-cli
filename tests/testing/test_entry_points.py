# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.testing.entry_points``: a real dist-info, discovery, and a clean exit."""

from __future__ import annotations

import importlib.metadata as md
import importlib.util
import py_compile
import sys
import types
from pathlib import Path

import pytest

from fluid_build.testing.entry_points import (
    IAC_GROUP,
    PROVIDER_GROUP,
    installed_plugin,
    write_dist,
)
from fluid_build.testing.env import isolated_env

pytestmark = pytest.mark.unit

IAC_MODULE = "acme_onprem_plugin"
IAC_SOURCE = """
class OnPremPlugin:
    name = "onprem"
    required_providers = {"onprem": {"source": "acme/onprem", "version": "~> 1.0"}}
    credential_env_vars = ()

    @classmethod
    def get_plugin_info(cls):
        class _Info:
            requires_cli = None

        return _Info()

    def emit(self, contract, actions=()):
        return {}

    def emit_data(self, contract, actions=()):
        return {}

    def credential_env(self, env):
        return {}

    def discover_imports(self, contract, actions=()):
        return []

    def provider_block(self):
        return {}
"""
PROVIDER_MODULE = "acme_dummy_provider"
PROVIDER_SOURCE = """
class DummyProvider:
    name = "dummycloud"

    def plan(self, contract):
        return []

    def apply(self, actions):
        return None
"""
IAC_GROUPS = {IAC_GROUP: {"onprem": f"{IAC_MODULE}:OnPremPlugin"}}


@pytest.fixture(autouse=True)
def _no_inherited_policy(tmp_path):
    """No allow or block list from the shell decides whether a plugin may load."""
    with isolated_env(tmp_path / "env"):
        yield


def _install(tmp_path: Path, **overrides):
    args = {
        "root": tmp_path / "site",
        "dist": "acme-onprem",
        "groups": IAC_GROUPS,
        "modules": {IAC_MODULE: IAC_SOURCE},
    }
    args.update(overrides)
    return installed_plugin(args["root"], args["dist"], args["groups"], args["modules"])


# ── writing the distribution ─────────────────────────────────────────────────


def test_write_dist_leaves_what_pip_would(tmp_path):
    info = write_dist(
        tmp_path / "site",
        "acme-onprem",
        {IAC_GROUP: {"onprem": "m:A", "OnPrem2": "m:B"}, "other.group": {"x": "m:C"}},
        {"m": "A = B = C = 1\n"},
    )
    assert info == tmp_path / "site" / "acme_onprem-1.0.dist-info"
    assert (tmp_path / "site" / "m.py").read_text(encoding="utf-8") == "A = B = C = 1\n"
    assert (info / "METADATA").read_text(encoding="utf-8") == (
        "Metadata-Version: 2.1\nName: acme-onprem\nVersion: 1.0\n"
    )
    assert (info / "entry_points.txt").read_text(encoding="utf-8") == (
        "[fluid_build.iac_providers]\nonprem = m:A\nOnPrem2 = m:B\n\n[other.group]\nx = m:C\n"
    )


def test_write_dist_again_in_the_same_directory_replaces_the_entry_points(tmp_path):
    write_dist(tmp_path, "acme-onprem", {IAC_GROUP: {"one": "m:A"}}, {})
    info = write_dist(tmp_path, "acme-onprem", {IAC_GROUP: {"two": "m:A"}}, {})
    assert "two = m:A" in (info / "entry_points.txt").read_text(encoding="utf-8")
    assert "one = m:A" not in (info / "entry_points.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"dist": ""},
        {"dist": "../escape"},
        {"dist": "a\\b"},
        {"dist": "two\nlines"},
        {"modules": {"pkg.mod": ""}},
        {"modules": {"../mod": ""}},
        {"modules": {"json": ""}},
        {"groups": {"bad\ngroup": {"x": "m:A"}}},
        {"groups": {IAC_GROUP: {"a\nb": "m:A"}}},
        {"groups": {IAC_GROUP: {"x": "m:A\n[evil]"}}},
    ],
)
def test_write_dist_refuses_names_that_leave_the_directory_or_split_a_line(tmp_path, kwargs):
    args = {"dist": "acme-onprem", "groups": IAC_GROUPS, "modules": {"m": ""}}
    args.update(kwargs)
    with pytest.raises(ValueError):
        write_dist(tmp_path / "site", args["dist"], args["groups"], args["modules"])
    assert not (tmp_path / "site").exists()  # refused before anything was written


# ── installing it ────────────────────────────────────────────────────────────


def test_the_plugin_is_discoverable_like_an_installed_package_only_inside_the_block(tmp_path):
    root = tmp_path / "site"
    with _install(tmp_path) as info:
        assert info.name == "acme_onprem-1.0.dist-info"
        assert sys.path[0] == str(root)
        points = {ep.name: ep.value for ep in md.entry_points(group=IAC_GROUP)}
        assert points["onprem"] == f"{IAC_MODULE}:OnPremPlugin"
        assert md.distribution("acme-onprem").version == "1.0"
        assert IAC_MODULE not in sys.modules  # an entry point is read by name, not imported
        module = __import__(IAC_MODULE)
        assert module.OnPremPlugin.name == "onprem"
    assert str(root) not in sys.path and IAC_MODULE not in sys.modules
    assert "onprem" not in {ep.name for ep in md.entry_points(group=IAC_GROUP)}
    with pytest.raises(md.PackageNotFoundError):
        md.distribution("acme-onprem")


def test_a_relative_root_stays_importable_after_the_working_directory_changes(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with installed_plugin(Path("site"), "acme-onprem", IAC_GROUPS, {IAC_MODULE: IAC_SOURCE}):
        assert sys.path[0] == str(tmp_path / "site")
        monkeypatch.chdir(elsewhere)
        assert __import__(IAC_MODULE).OnPremPlugin.name == "onprem"


def test_it_cleans_up_when_the_body_raises(tmp_path):
    with pytest.raises(RuntimeError, match="boom"):
        with _install(tmp_path):
            __import__(IAC_MODULE)
            raise RuntimeError("boom")
    assert str(tmp_path / "site") not in sys.path and IAC_MODULE not in sys.modules
    with pytest.raises(md.PackageNotFoundError):
        md.distribution("acme-onprem")


def test_a_rewritten_module_does_not_import_from_the_old_bytecode(tmp_path):
    module = tmp_path / f"{IAC_MODULE}.py"
    write_dist(tmp_path, "acme-onprem", IAC_GROUPS, {IAC_MODULE: "VALUE = 1\n"})
    compiled = Path(importlib.util.cache_from_source(str(module)))
    py_compile.compile(str(module), doraise=True)
    assert compiled.is_file()
    # Python accepts a compiled copy when the source's time (in seconds) and size match,
    # and this rewrite has the same size.
    write_dist(tmp_path, "acme-onprem", IAC_GROUPS, {IAC_MODULE: "VALUE = 2\n"})
    assert not compiled.exists()


def test_a_module_that_was_imported_before_is_put_back(tmp_path, monkeypatch):
    before = types.ModuleType(IAC_MODULE)
    monkeypatch.setitem(sys.modules, IAC_MODULE, before)
    with _install(tmp_path, modules={IAC_MODULE: "VALUE = 3\n"}):
        assert __import__(IAC_MODULE) is not before
    assert sys.modules[IAC_MODULE] is before


def test_a_module_left_over_from_an_earlier_use_is_not_reused(tmp_path):
    with _install(tmp_path, modules={IAC_MODULE: "VALUE = 1\n"}):
        assert __import__(IAC_MODULE).VALUE == 1
    with _install(tmp_path, modules={IAC_MODULE: "VALUE = 2\n"}):
        assert __import__(IAC_MODULE).VALUE == 2


# ── the plugin registries ────────────────────────────────────────────────────


def test_the_iac_registry_finds_the_plugin_and_is_put_back(tmp_path):
    from fluid_build.iac import registry

    plugins, from_entry_points = dict(registry.IAC_PLUGINS), set(registry.IAC_ENTRYPOINT_PLUGINS)
    with _install(tmp_path):
        assert registry.is_iac_entrypoint_platform("onprem")
        assert "onprem" in registry.known_iac_platforms()
        plugin = registry.get_iac_plugin("onprem")
        assert type(plugin).__name__ == "OnPremPlugin"
        assert "onprem" in registry.IAC_PLUGINS and "onprem" in registry.IAC_ENTRYPOINT_PLUGINS
    assert dict(registry.IAC_PLUGINS) == plugins
    assert set(registry.IAC_ENTRYPOINT_PLUGINS) == from_entry_points
    assert not registry.is_iac_entrypoint_platform("onprem")
    assert "onprem" not in registry.known_iac_platforms()


def test_a_second_install_in_the_same_directory_is_not_hidden_by_a_cache(tmp_path):
    from fluid_build.iac import registry

    for platform, other in (("onprem", "slowcloud"), ("slowcloud", "onprem")):
        groups = {IAC_GROUP: {platform: f"{IAC_MODULE}:OnPremPlugin"}}
        with _install(tmp_path, groups=groups):
            names = registry.known_iac_platforms()
            assert platform in names and other not in names


def test_a_provider_only_plugin_does_not_touch_the_iac_registry(tmp_path):
    from fluid_build.iac import registry

    plugins = dict(registry.IAC_PLUGINS)
    index = registry._INDEX
    groups = {PROVIDER_GROUP: {"dummycloud": f"{PROVIDER_MODULE}:DummyProvider"}}
    with _install(tmp_path, groups=groups, modules={PROVIDER_MODULE: PROVIDER_SOURCE}):
        assert registry._INDEX is index
    assert dict(registry.IAC_PLUGINS) == plugins


def test_the_provider_registry_scans_again_and_is_put_back(tmp_path, monkeypatch):
    from fluid_build import providers

    # A registry that discovery already ran for: without a reset, the next
    # ``discover_providers()`` returns early and never reads a new entry point.
    monkeypatch.setitem(providers.PROVIDERS, "already_registered", object)
    monkeypatch.setattr(providers, "_DISCOVERY_DONE", True)
    names = set(providers.PROVIDERS)
    groups = {PROVIDER_GROUP: {"dummycloud": f"{PROVIDER_MODULE}:DummyProvider"}}
    with _install(tmp_path, groups=groups, modules={PROVIDER_MODULE: PROVIDER_SOURCE}):
        providers.discover_providers()
        assert "dummycloud" in providers.PROVIDERS
        assert providers.PROVIDERS.pending("dummycloud") is not None  # declared by name
        assert PROVIDER_MODULE not in sys.modules
    assert set(providers.PROVIDERS) == names
    assert providers._DISCOVERY_DONE is True


def test_a_group_without_a_registry_is_left_to_the_caller(tmp_path):
    groups = {"acme.extensions": {"thing": f"{IAC_MODULE}:OnPremPlugin"}}
    with _install(tmp_path, groups=groups) as info:
        assert {ep.name for ep in md.entry_points(group="acme.extensions")} == {"thing"}
        assert info.is_dir()
