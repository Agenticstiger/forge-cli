# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which resolver fetches each scheme: core, registered, and entry-point packs."""

from __future__ import annotations

import importlib.metadata
from types import SimpleNamespace

import pytest

from fluid_build.secrets import registry
from fluid_build.secrets._env import EnvResolver
from fluid_build.secrets.errors import SecretResolveError


class _PackResolver:
    scheme = "op"

    def resolve(self, ref, run):
        return "value"

    def exists(self, ref, run):
        return registry.Existence(True, "ok")


def _patch_entry_points(monkeypatch, entries):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return list(entries)

    monkeypatch.setattr(importlib.metadata, "entry_points", fake)
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("scheme", ["env", "file", "vault", "aws-sm"])
def test_core_schemes_cannot_be_registered_over(scheme):
    with pytest.raises(ValueError, match="core scheme"):
        registry.register_resolver(scheme, _PackResolver())


@pytest.mark.unit
def test_registered_resolver_is_served_and_unregister_undoes_it(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    pack = _PackResolver()
    registry.register_resolver("op", pack)
    assert registry.get_resolver("op") is pack
    registry.unregister_resolver("op")
    with pytest.raises(SecretResolveError):
        registry.get_resolver("op")


@pytest.mark.unit
def test_unknown_scheme_names_the_entry_point_group(monkeypatch):
    calls = _patch_entry_points(monkeypatch, [])
    with pytest.raises(SecretResolveError) as info:
        registry.get_resolver("zzz")
    exc = info.value
    assert exc.extras["reason"] == "no_resolver"
    assert "fluid_build.secret_resolvers" in exc.fix
    assert "zzz://" in exc.what
    assert calls == [{"group": registry.ENTRY_POINT_GROUP}]


@pytest.mark.unit
def test_known_pack_scheme_without_a_pack_says_no_resolver(monkeypatch):
    _patch_entry_points(monkeypatch, [])
    with pytest.raises(SecretResolveError) as info:
        registry.get_resolver("gcp-sm")
    assert info.value.extras["reason"] == "no_resolver"
    assert "gcp-sm://projects/<project>/secrets/<secret>" in info.value.why


@pytest.mark.unit
def test_entry_point_for_a_core_scheme_is_ignored(monkeypatch):
    def load():
        raise AssertionError("a pack must never be loaded over a core scheme")

    evil = SimpleNamespace(name="env", value="evil:Resolver", load=load)
    _patch_entry_points(monkeypatch, [evil])
    assert registry.installed_packs() == {}
    assert isinstance(registry.get_resolver("env"), EnvResolver)


@pytest.mark.unit
def test_pack_entry_point_is_loaded_and_instantiated(monkeypatch):
    entry = SimpleNamespace(name="op", value="pack:Resolver", load=lambda: _PackResolver)
    _patch_entry_points(monkeypatch, [entry])
    assert registry.installed_packs() == {"op": "pack:Resolver"}
    resolver = registry.get_resolver("op")
    assert isinstance(resolver, _PackResolver)
    assert registry.get_resolver("op") is resolver


@pytest.mark.unit
def test_pack_entry_point_that_raises_on_load_gives_a_clear_error(monkeypatch):
    def load():
        raise ImportError("secret-detail-in-traceback")

    entry = SimpleNamespace(name="op", value="pack:Broken", load=load)
    _patch_entry_points(monkeypatch, [entry])
    with pytest.raises(SecretResolveError) as info:
        registry.get_resolver("op")
    exc = info.value
    assert "failed to load" in exc.what
    assert "ImportError" in exc.why
    assert "pack:Broken" in exc.why
    assert exc.extras["reason"] == "no_resolver"
    assert "secret-detail-in-traceback" not in exc.as_json()


@pytest.mark.unit
def test_pack_object_without_resolve_is_treated_as_missing(monkeypatch):
    entry = SimpleNamespace(name="op", value="pack:Odd", load=object)
    _patch_entry_points(monkeypatch, [entry])
    with pytest.raises(SecretResolveError) as info:
        registry.get_resolver("op")
    assert info.value.extras["reason"] == "no_resolver"
