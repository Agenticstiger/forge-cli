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

"""Tests for SimplifiedForge in forge/simple_forge.py."""

from unittest.mock import MagicMock, patch

from fluid_build.forge.simple_forge import SimplifiedForge, get_forge


class TestSimplifiedForgeInit:
    def test_default_state(self):
        forge = SimplifiedForge()
        assert forge.context is None
        assert forge._initialized is False

    @patch("fluid_build.forge.simple_forge.initialize_registries")
    def test_initialize_success(self, mock_init):
        forge = SimplifiedForge()
        assert forge.initialize() is True
        assert forge._initialized is True
        mock_init.assert_called_once()

    @patch("fluid_build.forge.simple_forge.initialize_registries", side_effect=RuntimeError("fail"))
    def test_initialize_failure(self, _mock_init):
        forge = SimplifiedForge()
        assert forge.initialize() is False
        assert forge._initialized is False


class TestGetAvailableTemplatesProviders:
    @patch(
        "fluid_build.forge.simple_forge.list_templates",
        return_value=["analytics-basic", "ml-pipeline"],
    )
    @patch("fluid_build.forge.simple_forge.initialize_registries")
    def test_get_available_templates(self, _mock_init, _mock_list):
        forge = SimplifiedForge()
        result = forge.get_available_templates()
        assert result == ["analytics-basic", "ml-pipeline"]
        # Should auto-initialize
        assert forge._initialized is True

    @patch("fluid_build.forge.simple_forge.list_providers", return_value=["local", "gcp"])
    @patch("fluid_build.forge.simple_forge.initialize_registries")
    def test_get_available_providers(self, _mock_init, _mock_list):
        forge = SimplifiedForge()
        result = forge.get_available_providers()
        assert result == ["local", "gcp"]


class TestGetTemplateInfo:
    @patch("fluid_build.forge.simple_forge.get_template")
    def test_template_not_found(self, mock_get):
        mock_get.return_value = None
        forge = SimplifiedForge()
        assert forge.get_template_info("nonexistent") is None

    @patch("fluid_build.forge.simple_forge.get_template")
    def test_template_info(self, mock_get):
        meta = MagicMock()
        meta.display_name = "Analytics Basic"
        meta.description = "A basic analytics template"
        meta.complexity = "simple"
        meta.use_cases = ["dashboards"]
        meta.provider_support = ["local", "gcp"]

        template = MagicMock()
        template.get_metadata.return_value = meta
        mock_get.return_value = template

        forge = SimplifiedForge()
        info = forge.get_template_info("analytics-basic")
        assert info["name"] == "analytics-basic"
        assert info["display_name"] == "Analytics Basic"
        assert info["complexity"] == "simple"
        assert info["use_cases"] == ["dashboards"]

    @patch("fluid_build.forge.simple_forge.get_template")
    def test_template_metadata_error(self, mock_get):
        template = MagicMock()
        template.get_metadata.side_effect = RuntimeError("broken")
        mock_get.return_value = template

        forge = SimplifiedForge()
        info = forge.get_template_info("broken")
        assert "error" in info


class TestGetProviderInfo:
    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_provider_not_found(self, mock_get):
        mock_get.return_value = None
        forge = SimplifiedForge()
        assert forge.get_provider_info("nonexistent") is None

    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_provider_info(self, mock_get):
        provider = MagicMock()
        provider.check_prerequisites.return_value = (True, [])
        provider.get_required_tools.return_value = ["docker"]
        provider.get_environment_variables.return_value = {"API_KEY": "required"}
        mock_get.return_value = provider

        forge = SimplifiedForge()
        info = forge.get_provider_info("gcp")
        assert info["name"] == "gcp"
        assert info["available"] is True
        assert info["required_tools"] == ["docker"]

    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_provider_check_error(self, mock_get):
        provider = MagicMock()
        provider.check_prerequisites.side_effect = RuntimeError("fail")
        mock_get.return_value = provider

        forge = SimplifiedForge()
        info = forge.get_provider_info("broken")
        assert info["available"] is False
        assert "error" in info


class TestCreateProject:
    @patch("fluid_build.forge.simple_forge.initialize_registries")
    @patch("fluid_build.forge.simple_forge.get_template")
    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_template_not_found(self, _mock_prov, mock_tmpl, _mock_init):
        mock_tmpl.return_value = None
        forge = SimplifiedForge()
        assert forge.create_project("bad", "local", "proj", "/tmp/out") is False

    @patch("fluid_build.forge.simple_forge.initialize_registries")
    @patch("fluid_build.forge.simple_forge.get_template")
    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_provider_not_found(self, mock_prov, mock_tmpl, _mock_init):
        mock_tmpl.return_value = MagicMock()
        mock_prov.return_value = None
        forge = SimplifiedForge()
        assert forge.create_project("tmpl", "bad", "proj", "/tmp/out") is False

    @patch("fluid_build.forge.simple_forge.initialize_registries")
    @patch("fluid_build.forge.simple_forge.get_template")
    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_provider_unavailable(self, mock_prov, mock_tmpl, _mock_init):
        mock_tmpl.return_value = MagicMock()
        provider = MagicMock()
        provider.check_prerequisites.return_value = (False, ["docker not found"])
        mock_prov.return_value = provider

        forge = SimplifiedForge()
        assert forge.create_project("tmpl", "local", "proj", "/tmp/out") is False

    @patch("fluid_build.forge.simple_forge.initialize_registries")
    @patch("fluid_build.forge.simple_forge.get_template")
    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_successful_create(self, mock_prov, mock_tmpl, _mock_init):
        template = MagicMock()
        template.get_metadata.return_value = MagicMock()
        template.generate_project.return_value = True
        mock_tmpl.return_value = template

        provider = MagicMock()
        provider.check_prerequisites.return_value = (True, [])
        mock_prov.return_value = provider

        forge = SimplifiedForge()
        assert forge.create_project("tmpl", "local", "proj", "/tmp/out") is True
        template.generate_project.assert_called_once()


class TestListAllComponents:
    @patch("fluid_build.forge.simple_forge.initialize_registries")
    @patch(
        "fluid_build.forge.simple_forge.get_registry_status",
        return_value={"templates": ["a"], "providers": ["b"], "extensions": [], "generators": []},
    )
    def test_list_all(self, _mock_status, _mock_init):
        forge = SimplifiedForge()
        result = forge.list_all_components()
        assert result["templates"] == ["a"]
        assert result["providers"] == ["b"]


class TestGetSystemStatus:
    @patch("fluid_build.forge.simple_forge.initialize_registries")
    @patch(
        "fluid_build.forge.simple_forge.get_registry_status",
        return_value={"templates": ["t1"], "providers": ["p1"]},
    )
    @patch("fluid_build.forge.simple_forge.get_provider")
    def test_system_status(self, mock_get_prov, _mock_status, _mock_init):
        provider = MagicMock()
        provider.check_prerequisites.return_value = (True, [])
        provider.get_required_tools.return_value = []
        provider.get_environment_variables.return_value = {}
        mock_get_prov.return_value = provider

        forge = SimplifiedForge()
        status = forge.get_system_status()
        assert status["initialized"] is True
        assert "provider_availability" in status
        assert status["provider_availability"]["p1"] is True


class TestGetForge:
    def test_singleton(self):
        import fluid_build.forge.simple_forge as mod

        mod._global_forge = None
        f1 = get_forge()
        f2 = get_forge()
        assert f1 is f2
        mod._global_forge = None  # cleanup
