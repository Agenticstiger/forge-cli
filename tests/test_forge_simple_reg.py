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

"""Unit tests for fluid_build.forge.simple_registration module."""

import logging
from unittest.mock import Mock, patch

from fluid_build.forge.simple_registration import get_registration_summary, register_all_components

logger = logging.getLogger("test_forge_simple_reg")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry(available=None):
    registry = Mock()
    registry.list_available = Mock(return_value=available or [])
    registry.register = Mock()
    return registry


def _make_registries(
    template_available=None,
    provider_available=None,
    ext_available=None,
    gen_available=None,
):
    templates = _make_registry(template_available or [])
    providers = _make_registry(provider_available or [])
    extensions = _make_registry(ext_available or [])
    generators = _make_registry(gen_available or [])
    return templates, providers, extensions, generators


# ---------------------------------------------------------------------------
# get_registration_summary
# ---------------------------------------------------------------------------


class TestGetRegistrationSummary:
    def test_returns_dict(self):
        summary = get_registration_summary()
        assert isinstance(summary, dict)

    def test_has_templates_key(self):
        summary = get_registration_summary()
        assert "templates" in summary

    def test_has_providers_key(self):
        summary = get_registration_summary()
        assert "providers" in summary

    def test_has_extensions_key(self):
        summary = get_registration_summary()
        assert "extensions" in summary

    def test_has_generators_key(self):
        summary = get_registration_summary()
        assert "generators" in summary

    def test_templates_includes_analytics(self):
        summary = get_registration_summary()
        assert "analytics" in summary["templates"]

    def test_templates_includes_all_expected(self):
        summary = get_registration_summary()
        expected = {"analytics", "starter", "ml_pipeline", "etl_pipeline", "streaming"}
        assert expected.issubset(set(summary["templates"]))

    def test_providers_includes_local(self):
        summary = get_registration_summary()
        assert "local" in summary["providers"]

    def test_providers_includes_cloud(self):
        summary = get_registration_summary()
        assert "gcp" in summary["providers"]
        assert "aws" in summary["providers"]

    def test_extensions_includes_ai_assistant(self):
        summary = get_registration_summary()
        assert "ai_assistant" in summary["extensions"]

    def test_generators_includes_contract(self):
        summary = get_registration_summary()
        assert "contract" in summary["generators"]


# ---------------------------------------------------------------------------
# register_all_components - registries are called
# ---------------------------------------------------------------------------


class TestRegisterAllComponentsBasic:
    def test_register_always_calls_list_available(self):
        templates, providers, extensions, generators = _make_registries()
        register_all_components(templates, providers, extensions, generators)
        templates.list_available.assert_called()
        providers.list_available.assert_called()
        extensions.list_available.assert_called()
        generators.list_available.assert_called()

    def test_register_runs_without_exception(self):
        templates, providers, extensions, generators = _make_registries()
        # Should not raise even if some imports succeed and some fail
        register_all_components(templates, providers, extensions, generators)

    def test_register_logs_summary(self):
        templates, providers, extensions, generators = _make_registries(
            template_available=["analytics"],
            provider_available=["local"],
            ext_available=[],
            gen_available=[],
        )
        reg_logger = logging.getLogger("fluid_build.forge.simple_registration")
        with patch.object(reg_logger, "info") as mock_log:
            register_all_components(templates, providers, extensions, generators)
            assert mock_log.call_count >= 1


# ---------------------------------------------------------------------------
# register_all_components - import error handling
# ---------------------------------------------------------------------------


class TestRegisterAllComponentsImportErrors:
    def test_import_errors_are_silently_ignored(self):
        templates, providers, extensions, generators = _make_registries()

        with patch.dict(
            "sys.modules",
            {
                "fluid_build.forge.templates.analytics": None,
                "fluid_build.forge.templates.starter": None,
                "fluid_build.forge.templates.ml_pipeline": None,
                "fluid_build.forge.templates.etl_pipeline": None,
                "fluid_build.forge.templates.streaming": None,
                "fluid_build.forge.providers.local": None,
                "fluid_build.forge.providers.gcp": None,
                "fluid_build.forge.providers.aws": None,
                "fluid_build.forge.providers.snowflake": None,
                "fluid_build.forge.extensions.ai_assistant": None,
                "fluid_build.forge.extensions.environment_validator": None,
                "fluid_build.forge.extensions.project_history": None,
                "fluid_build.forge.generators.contract_generator": None,
                "fluid_build.forge.generators.readme_generator": None,
                "fluid_build.forge.generators.config_generator": None,
            },
        ):
            register_all_components(templates, providers, extensions, generators)

    def test_partial_import_failures_handled(self):
        templates, providers, extensions, generators = _make_registries()

        with patch.dict(
            "sys.modules",
            {
                "fluid_build.forge.templates.ml_pipeline": None,
                "fluid_build.forge.providers.snowflake": None,
            },
        ):
            register_all_components(templates, providers, extensions, generators)

    def test_summary_reported_even_with_partial_failures(self):
        templates, providers, extensions, generators = _make_registries(
            template_available=["analytics"],
            provider_available=[],
            ext_available=[],
            gen_available=[],
        )

        with patch.dict(
            "sys.modules",
            {
                "fluid_build.forge.templates.ml_pipeline": None,
            },
        ):
            register_all_components(templates, providers, extensions, generators)

        templates.list_available.assert_called()
