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

# tests/providers/test_registry.py
"""Tests for provider registry, entry-point discovery (Phase 0), and SDK integration (Phase 1)."""

from __future__ import annotations

import logging
import warnings

import pytest

try:
    import fluid_provider_sdk  # noqa: F401

    HAS_SDK = True
except ImportError:
    HAS_SDK = False


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the canonical registry before each test."""
    from fluid_build.providers import clear_providers

    clear_providers()
    yield
    clear_providers()


# ---------------------------------------------------------------------------
# Canonical registry (providers/__init__.py)
# ---------------------------------------------------------------------------


class TestCanonicalRegistry:
    def test_register_and_get(self):
        from fluid_build.providers import PROVIDERS, get_provider, register_provider

        class FakeProvider:
            name = "fake"

        register_provider("fake", FakeProvider)
        assert "fake" in PROVIDERS
        assert get_provider("fake") is FakeProvider

    def test_normalize_name(self):
        from fluid_build.providers import get_provider, register_provider

        class FakeProvider:
            name = "my_cloud"

        register_provider("My-Cloud", FakeProvider)
        assert get_provider("my_cloud") is FakeProvider
        assert get_provider("My-Cloud") is FakeProvider
        assert get_provider("MY_CLOUD") is FakeProvider

    def test_reject_invalid_name(self):
        class FakeProvider:
            pass

        from fluid_build.providers import register_provider

        with pytest.raises(ValueError, match="Invalid provider name"):
            register_provider("has spaces", FakeProvider)

    def test_reject_banned_name(self):
        from fluid_build.providers import PROVIDERS, register_provider

        class FakeProvider:
            pass

        register_provider("unknown", FakeProvider)
        assert "unknown" not in PROVIDERS

    def test_first_write_wins(self):
        from fluid_build.providers import get_provider, register_provider

        class First:
            pass

        class Second:
            pass

        register_provider("dup", First)
        register_provider("dup", Second)
        assert get_provider("dup") is First

    def test_override(self):
        from fluid_build.providers import get_provider, register_provider

        class First:
            pass

        class Second:
            pass

        register_provider("dup", First)
        register_provider("dup", Second, override=True)
        assert get_provider("dup") is Second

    def test_list_providers(self):
        from fluid_build.providers import list_providers, register_provider

        class A:
            pass

        class B:
            pass

        register_provider("beta", B)
        register_provider("alpha", A)
        assert list_providers() == ["alpha", "beta"]

    def test_get_unknown_raises_keyerror(self):
        from fluid_build.providers import get_provider

        with pytest.raises(KeyError, match="Unknown provider"):
            get_provider("nonexistent")

    def test_registry_meta_tracks_source(self):
        from fluid_build.providers import _REGISTRY_META, register_provider

        class Ep:
            pass

        register_provider("ep_prov", Ep, source="entrypoint")
        meta = _REGISTRY_META.get("ep_prov", {})
        assert meta["source"] == "entrypoint"

    def test_clear_providers(self):
        from fluid_build.providers import PROVIDERS, clear_providers, register_provider

        class Fake:
            pass

        register_provider("x", Fake)
        assert len(PROVIDERS) > 0
        clear_providers()
        assert len(PROVIDERS) == 0


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


class TestEntryPointDiscovery:
    def test_discover_entrypoints_runs(self):
        """Entry-point discovery should execute without errors."""
        from fluid_build.providers import _discover_entrypoints

        _discover_entrypoints(logging.getLogger("test"))
        # If we get here, no exceptions were raised

    def test_full_discovery_finds_builtin_providers(self):
        from fluid_build.providers import PROVIDERS, discover_providers

        discover_providers()
        # At minimum these should be found:
        assert "local" in PROVIDERS
        # aws/gcp/snowflake/odps may fail if deps missing, but local should always work


# ---------------------------------------------------------------------------
# Deprecated base.py re-exports
# ---------------------------------------------------------------------------


class TestBaseReExports:
    def test_register_provider_warns_deprecation(self):
        from fluid_build.providers.base import BaseProvider

        class TestProv(BaseProvider):
            name = "test_dep"

            def plan(self, contract):
                return []

            def apply(self, actions):
                pass

        from fluid_build.providers.base import register_provider

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            register_provider("test_dep", TestProv)
            assert any("deprecated" in str(x.message).lower() for x in w)

    def test_base_imports_still_work(self):
        """Ensure existing import paths don't break."""
        from fluid_build.providers.base import (
            ApplyResult,
            BaseProvider,
            PlanAction,
        )

        assert BaseProvider is not None
        assert ApplyResult is not None
        assert PlanAction is not None


# ---------------------------------------------------------------------------
# LocalProvider migration
# ---------------------------------------------------------------------------


class TestLocalProviderMigration:
    def test_is_baseprovider_subclass(self):
        from fluid_build.providers.base import BaseProvider
        from fluid_build.providers.local.local import LocalProvider

        assert issubclass(LocalProvider, BaseProvider)

    def test_name_attribute(self):
        from fluid_build.providers.local.local import LocalProvider

        assert LocalProvider.name == "local"

    def test_init_keyword_only(self):
        from fluid_build.providers.local.local import LocalProvider

        p = LocalProvider(project="proj", region="us-east-1", persist=True)
        assert p.project == "proj"
        assert p.region == "us-east-1"
        assert p.persist is True

    def test_init_defaults(self):
        from fluid_build.providers.local.local import LocalProvider

        p = LocalProvider()
        assert p.project is None
        assert p.region is None
        assert p.persist is False
        assert p.logger is not None  # BaseProvider always creates a logger

    def test_capabilities(self):
        from fluid_build.providers.local.local import LocalProvider

        caps = LocalProvider().capabilities()
        assert caps["planning"] is True
        assert caps["apply"] is True


# ---------------------------------------------------------------------------
# CLI build_provider name normalization
# ---------------------------------------------------------------------------


class TestBuildProviderNormalization:
    def test_case_insensitive_lookup(self):
        from fluid_build.cli._common import build_provider

        logger = logging.getLogger("test")
        p = build_provider("Local", None, None, logger)
        assert type(p).__name__ == "LocalProvider"

    def test_hyphen_normalization(self):
        from fluid_build.providers import register_provider

        class FakeCloud:
            def __init__(self, **kw):
                pass

        register_provider("my_cloud", FakeCloud)

        from fluid_build.cli._common import build_provider

        logger = logging.getLogger("test")
        p = build_provider("my-cloud", None, None, logger)
        assert isinstance(p, FakeCloud)


# ---------------------------------------------------------------------------
# Phase 1: SDK integration
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_SDK, reason="fluid-provider-sdk not installed")
class TestSDKIntegration:
    """Verify that the standalone SDK package is wired into the CLI."""

    def test_has_sdk_flag(self):
        from fluid_build.providers.base import _HAS_SDK

        assert _HAS_SDK is True

    def test_sdk_version_exposed(self):
        from fluid_build.providers.base import SDK_VERSION

        assert SDK_VERSION == "0.1.0"

    def test_baseprovider_comes_from_sdk(self):
        from fluid_build.providers.base import BaseProvider

        assert "fluid_provider_sdk" in BaseProvider.__module__

    def test_provider_metadata_type(self):
        from fluid_build.providers.base import ProviderMetadata

        m = ProviderMetadata(name="test", display_name="Test", description="desc", version="1.0")
        assert m.name == "test"
        assert m.sdk_version == "0.1.0"  # auto-populated
        d = m.to_dict()
        assert d["display_name"] == "Test"

    def test_provider_capabilities_mapping(self):
        from fluid_build.providers.base import ProviderCapabilities

        c = ProviderCapabilities(planning=True, apply=True, render=False, graph=False, auth=False)
        assert c["planning"] is True
        assert "apply" in c
        assert c.get("nonexistent", 42) == 42
        assert set(c.keys()) >= {"planning", "apply", "render", "graph", "auth"}


@pytest.mark.skipif(not HAS_SDK, reason="fluid-provider-sdk not installed")
class TestProviderInfo:
    """Verify get_provider_info() on built-in providers."""

    def test_local_provider_info(self):
        from fluid_build.providers.local.local import LocalProvider

        info = LocalProvider.get_provider_info()
        assert info.name == "local"
        assert info.display_name == "Local (DuckDB)"
        assert info.sdk_version == "0.1.0"
        assert "duckdb" in info.tags

    def test_aws_provider_info(self):
        from fluid_build.providers.aws.provider import AwsProvider

        info = AwsProvider.get_provider_info()
        assert info.name == "aws"
        assert "aws" in info.supported_platforms

    def test_gcp_provider_info(self):
        from fluid_build.providers.gcp.provider import GcpProvider

        info = GcpProvider.get_provider_info()
        assert info.name == "gcp"
        assert "bigquery" in info.supported_platforms

    def test_snowflake_provider_info(self):
        from fluid_build.providers.snowflake.provider_enhanced import SnowflakeProviderEnhanced

        info = SnowflakeProviderEnhanced.get_provider_info()
        assert info.name == "snowflake"
        assert "data-warehouse" in info.tags

    def test_provider_info_to_dict_roundtrip(self):
        from fluid_build.providers.local.local import LocalProvider

        info = LocalProvider.get_provider_info()
        d = info.to_dict()
        assert isinstance(d, dict)
        assert d["name"] == "local"
        assert isinstance(d["tags"], list)
        assert isinstance(d["supported_platforms"], list)
