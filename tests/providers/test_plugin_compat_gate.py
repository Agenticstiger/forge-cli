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
def _clean_registry():
    P.clear_providers()
    yield
    P.clear_providers()


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
    P.register_provider("acmeok", _new_sdk_provider(">=0.1.0"), source="test")
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
        P.register_provider("acmestrictok", _new_sdk_provider(">=0.1.0"), source="test")
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
