# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the per-provider apply-engine cutover registry."""

from __future__ import annotations

import pytest

from fluid_build.iac import cutover

pytestmark = [pytest.mark.unit, pytest.mark.provider]


class TestDefaultEngine:
    def test_all_three_providers_cut_over_to_opentofu(self):
        # GCP, AWS, and Snowflake are all cut over (see AUTOGEN_SPIKE.md).
        assert {"aws", "gcp", "snowflake"} <= cutover.OPENTOFU_DEFAULT_PROVIDERS
        assert cutover.default_engine("gcp") == "opentofu"
        assert cutover.default_engine("aws") == "opentofu"
        assert cutover.default_engine("snowflake") == "opentofu"

    def test_unknown_provider_defaults_to_native(self):
        # A provider not in the registry falls back to the native engine.
        assert cutover.default_engine("azure") == "native"
        assert cutover.default_engine("local") == "native"

    def test_cutover_provider_defaults_to_opentofu(self, monkeypatch):
        monkeypatch.setattr(cutover, "OPENTOFU_DEFAULT_PROVIDERS", frozenset({"gcp"}))
        assert cutover.default_engine("gcp") == "opentofu"
        # other providers are untouched — cutover is strictly per-provider
        assert cutover.default_engine("aws") == "native"


class TestResolveEngine:
    def test_explicit_engine_always_wins(self, monkeypatch):
        monkeypatch.setattr(cutover, "OPENTOFU_DEFAULT_PROVIDERS", frozenset({"gcp"}))
        # explicit native overrides a cut-over provider's opentofu default
        assert cutover.resolve_engine("native", "gcp") == "native"
        # explicit opentofu overrides a not-yet-cut-over provider
        assert cutover.resolve_engine("opentofu", "aws") == "opentofu"

    def test_auto_uses_the_per_provider_default(self, monkeypatch):
        monkeypatch.setattr(cutover, "OPENTOFU_DEFAULT_PROVIDERS", frozenset({"gcp"}))
        assert cutover.resolve_engine("auto", "gcp") == "opentofu"
        assert cutover.resolve_engine("auto", "aws") == "native"

    def test_none_is_treated_as_auto(self, monkeypatch):
        monkeypatch.setattr(cutover, "OPENTOFU_DEFAULT_PROVIDERS", frozenset({"gcp"}))
        assert cutover.resolve_engine(None, "gcp") == "opentofu"
        assert cutover.resolve_engine(None, "aws") == "native"
