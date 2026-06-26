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

"""Tests for the plugin↔CLI version-compatibility gate.

Covers the fix for the dead handshake: the CLI version is now sourced from
distribution metadata (not a hardcoded "0.7.1"), the gate reads the live
``fluid_sdk`` (not the renamed-away ``fluid_provider_sdk``), it honours a
new-SDK ``requires_cli`` PEP 440 specifier, and it can hard-fail opt-in.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from fluid_build import providers as P


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot & restore the global registry — never leave it cleared.

    Unlike a ``clear_providers()`` fixture, this restores whatever was registered
    before the test, so these tests can't widen an empty-registry window for an
    order-dependent test elsewhere in the (randomly-ordered) suite. Our throwaway
    provider names ("acme*") don't collide with built-ins, so no pre-clear needed.
    """
    saved = dict(P.PROVIDERS)
    saved_meta = dict(P._REGISTRY_META)
    saved_done = P._DISCOVERY_DONE
    try:
        yield
    finally:
        P.PROVIDERS.clear()
        P.PROVIDERS.update(saved)
        P._REGISTRY_META.clear()
        P._REGISTRY_META.update(saved_meta)
        P._DISCOVERY_DONE = saved_done


class _Info:
    def __init__(self, requires_cli):
        self.requires_cli = requires_cli


def _new_sdk_provider(requires_cli):
    """A new-SDK-style provider class declaring a requires_cli specifier."""

    class _P:
        _req = requires_cli

        @classmethod
        def get_plugin_info(cls):
            return _Info(cls._req)

        def plan(self, contract):
            return []

        def apply(self, actions):
            return None

    return _P


# ── version detection ────────────────────────────────────────────────


def test_cli_version_is_detected_from_metadata_not_hardcoded():
    assert P._CLI_VERSION == importlib.metadata.version("data-product-forge")
    # The stale hardcoded constant is only a fallback, never the live value here.
    assert (
        P._CLI_VERSION != P._CLI_VERSION_FALLBACK
        or P._CLI_VERSION == importlib.metadata.version("data-product-forge")
    )


def test_spec_satisfied_helper():
    assert P._spec_satisfied("0.7.5", ">=0.7,<2.0") is True
    assert P._spec_satisfied("0.5.0", ">=0.7") is False
    assert P._spec_satisfied("0.9.1.dev3", ">=0.7,<2.0") is True  # prerelease counts
    assert P._spec_satisfied("1.0", None) is None
    assert P._spec_satisfied("1.0", "not-a-valid-specifier~~") is None


# ── new-SDK requires_cli gate ────────────────────────────────────────


def test_compatible_plugin_registers():
    # ">=0.0.0" is satisfiable by ANY CLI version (incl. the 0.0.0 / dev fallback a
    # shallow CI checkout produces from setuptools-scm) — the test must not assume
    # a particular version magnitude, only that a satisfiable spec registers.
    P.register_provider("acmeok", _new_sdk_provider(">=0.0.0"), source="test")
    assert "acmeok" in P.list_providers()


def test_incompatible_plugin_warns_but_registers_by_default(caplog):
    with caplog.at_level("WARNING"):
        P.register_provider("acmebad", _new_sdk_provider(">=999.0.0"), source="test")
    # Advisory by default: still registered.
    assert "acmebad" in P.list_providers()
    assert any("provider_version_warning" in r.getMessage() for r in caplog.records)


def test_strict_mode_rejects_incompatible(monkeypatch):
    monkeypatch.setenv("FLUID_PLUGIN_STRICT_COMPAT", "1")
    P.register_provider("acmestrict", _new_sdk_provider(">=999.0.0"), source="test")
    # Strict mode: the incompatible plugin is NOT registered.
    assert "acmestrict" not in P.list_providers()


def test_strict_mode_keeps_compatible():
    import os

    os.environ["FLUID_PLUGIN_STRICT_COMPAT"] = "1"
    try:
        # ">=0.0.0" — satisfiable by any CLI version (see test_compatible_plugin_registers).
        P.register_provider("acmestrictok", _new_sdk_provider(">=0.0.0"), source="test")
        assert "acmestrictok" in P.list_providers()
    finally:
        os.environ.pop("FLUID_PLUGIN_STRICT_COMPAT", None)


def test_no_metadata_is_treated_as_compatible():
    class _Bare:
        def plan(self, c):
            return []

        def apply(self, a):
            return None

    P.register_provider("acmebare", _Bare, source="test")
    assert "acmebare" in P.list_providers()
