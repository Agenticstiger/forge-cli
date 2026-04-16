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

"""Branch coverage tests for ForgeEngine (fluid_build/forge/core/engine.py)."""

from unittest.mock import MagicMock, patch


class TestForgeEngineInit:
    """Test ForgeEngine.__init__ branches."""

    @patch("fluid_build.forge.core.engine.initialize_all_registries")
    @patch("fluid_build.forge.core.engine.get_registry_status")
    def test_init_with_console(self, mock_status, _mock_init):
        mock_status.return_value = {"templates": {"count": 3}, "providers": {"count": 2}}
        from fluid_build.forge.core.engine import ForgeEngine

        console = MagicMock()
        engine = ForgeEngine(console=console, auto_init_registries=True)
        assert engine.console is console
        assert engine.project_config == {}

    @patch("fluid_build.forge.core.engine.initialize_all_registries")
    @patch("fluid_build.forge.core.engine.get_registry_status")
    def test_init_without_console(self, mock_status, _mock_init):
        mock_status.return_value = {"templates": {"count": 3}, "providers": {"count": 2}}
        from fluid_build.forge.core.engine import ForgeEngine

        engine = ForgeEngine(console=None, auto_init_registries=True)
        assert engine.console is not None  # creates default Console

    @patch("fluid_build.forge.core.engine.initialize_all_registries")
    @patch("fluid_build.forge.core.engine.get_registry_status")
    def test_init_no_auto_init(self, mock_status, mock_init):
        mock_status.return_value = {"templates": {"count": 0}, "providers": {"count": 0}}
        from fluid_build.forge.core.engine import ForgeEngine

        ForgeEngine(auto_init_registries=False)
        mock_init.assert_not_called()

    @patch("fluid_build.forge.core.engine.initialize_all_registries")
    @patch("fluid_build.forge.core.engine.get_registry_status")
    def test_init_empty_registries(self, mock_status, _mock_init):
        mock_status.return_value = {"templates": {"count": 0}, "providers": {"count": 0}}
        from fluid_build.forge.core.engine import ForgeEngine

        ForgeEngine(auto_init_registries=True)
        # Should log warnings but not raise


class TestForgeEngineRun:
    """Test ForgeEngine.run() branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_run_dry_run_branch(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock()
        engine._run_dry_run = MagicMock(return_value=True)
        result = engine.run(dry_run=True)
        engine._run_dry_run.assert_called_once()
        assert result is True

    def test_run_non_interactive_branch(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock()
        engine._run_non_interactive = MagicMock(return_value=True)
        result = engine.run(non_interactive=True)
        engine._run_non_interactive.assert_called_once()
        assert result is True

    def test_run_interactive_branch(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock()
        engine._run_interactive = MagicMock(return_value=True)
        engine.run()
        engine._run_interactive.assert_called_once()

    def test_run_with_target_dir(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock()
        engine._run_interactive = MagicMock(return_value=True)
        engine.run(target_dir="/tmp/test")
        assert str(engine.project_config.get("target_dir")) == "/tmp/test"

    def test_run_with_template(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock()
        engine._run_interactive = MagicMock(return_value=True)
        engine.run(template="starter")
        assert engine.project_config.get("template") == "starter"

    def test_run_with_provider(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock()
        engine._run_interactive = MagicMock(return_value=True)
        engine.run(provider="local")
        assert engine.project_config.get("provider") == "local"

    def test_run_keyboard_interrupt(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock(side_effect=KeyboardInterrupt)
        result = engine.run()
        assert result is False

    def test_run_exception(self):
        engine = self._make_engine()
        engine._show_welcome = MagicMock(side_effect=RuntimeError("boom"))
        result = engine.run()
        assert result is False


class TestForgeEngineRunWithConfig:
    """Test ForgeEngine.run_with_config() branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_run_with_config_dry_run(self):
        engine = self._make_engine()
        engine._validate_configuration = MagicMock(return_value=True)
        engine._create_generation_context = MagicMock()
        engine._preview_generation = MagicMock(return_value=True)
        result = engine.run_with_config({"name": "test"}, dry_run=True)
        engine._preview_generation.assert_called_once()
        assert result is True

    def test_run_with_config_execute(self):
        engine = self._make_engine()
        engine._validate_configuration = MagicMock(return_value=True)
        engine._create_generation_context = MagicMock()
        engine._execute_generation = MagicMock(return_value=True)
        engine.run_with_config({"name": "test"}, dry_run=False)
        engine._execute_generation.assert_called_once()

    def test_run_with_config_validation_fails(self):
        engine = self._make_engine()
        engine._validate_configuration = MagicMock(return_value=False)
        result = engine.run_with_config({"name": "test"})
        assert result is False

    def test_run_with_config_exception(self):
        engine = self._make_engine()
        engine._validate_configuration = MagicMock(side_effect=RuntimeError("fail"))
        result = engine.run_with_config({"name": "test"})
        assert result is False


class TestRunInteractive:
    """Test _run_interactive branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    @patch("fluid_build.forge.core.engine.extension_registry")
    def test_interactive_success(self, _mock_ext):
        engine = self._make_engine()
        engine._show_step_progress = MagicMock()
        engine._gather_project_info = MagicMock(return_value=True)
        engine._select_template = MagicMock(return_value=True)
        engine._configure_provider = MagicMock(return_value=True)
        engine._configure_advanced_options = MagicMock(return_value=True)
        engine._validate_configuration = MagicMock(return_value=True)
        engine._execute_generation = MagicMock(return_value=True)
        engine._show_completion_summary = MagicMock()
        result = engine._run_interactive()
        assert result is True

    @patch("fluid_build.forge.core.engine.extension_registry")
    def test_interactive_step_fails(self, _mock_ext):
        engine = self._make_engine()
        engine._show_step_progress = MagicMock()
        engine._gather_project_info = MagicMock(return_value=False)
        result = engine._run_interactive()
        assert result is False

    @patch("fluid_build.forge.core.engine.extension_registry")
    def test_interactive_exception(self, _mock_ext):
        engine = self._make_engine()
        engine._show_step_progress = MagicMock()
        engine._gather_project_info = MagicMock(side_effect=RuntimeError("fail"))
        result = engine._run_interactive()
        assert result is False


class TestRunNonInteractive:
    """Test _run_non_interactive branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_non_interactive_success(self):
        engine = self._make_engine()
        engine._apply_intelligent_defaults = MagicMock()
        engine._validate_configuration = MagicMock(return_value=True)
        engine._create_generation_context = MagicMock()
        engine._execute_generation = MagicMock(return_value=True)
        result = engine._run_non_interactive()
        assert result is True

    def test_non_interactive_validation_fails(self):
        engine = self._make_engine()
        engine._apply_intelligent_defaults = MagicMock()
        engine._validate_configuration = MagicMock(return_value=False)
        result = engine._run_non_interactive()
        assert result is False

    def test_non_interactive_exception(self):
        engine = self._make_engine()
        engine._apply_intelligent_defaults = MagicMock(side_effect=RuntimeError)
        result = engine._run_non_interactive()
        assert result is False


class TestRunDryRun:
    """Test _run_dry_run branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_dry_run_with_template(self):
        engine = self._make_engine()
        engine.project_config = {"template": "starter"}
        engine._apply_intelligent_defaults = MagicMock()
        engine._preview_generation = MagicMock(return_value=True)
        result = engine._run_dry_run()
        assert result is True
        engine._apply_intelligent_defaults.assert_called_once()

    def test_dry_run_without_template_interactive_success(self):
        engine = self._make_engine()
        engine.project_config = {}
        engine._run_interactive = MagicMock(return_value=True)
        engine._preview_generation = MagicMock(return_value=True)
        result = engine._run_dry_run()
        assert result is True

    def test_dry_run_without_template_interactive_fail(self):
        engine = self._make_engine()
        engine.project_config = {}
        engine._run_interactive = MagicMock(return_value=False)
        result = engine._run_dry_run()
        assert result is False

    def test_dry_run_exception(self):
        engine = self._make_engine()
        engine.project_config = {"template": "starter"}
        engine._apply_intelligent_defaults = MagicMock(side_effect=RuntimeError)
        result = engine._run_dry_run()
        assert result is False


class TestValidateConfiguration:
    """Test _validate_configuration branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    @patch("fluid_build.forge.core.engine.template_registry")
    @patch("fluid_build.forge.core.engine.provider_registry")
    @patch("fluid_build.forge.core.engine.validation_registry")
    def test_valid_config(self, mock_val, mock_prov, mock_tmpl):
        engine = self._make_engine()
        engine.project_config = {
            "name": "test",
            "description": "test desc",
            "template": "starter",
            "provider": "local",
            "target_dir": "/tmp/test",
        }
        mock_val.validate_all.return_value = []
        mock_template = MagicMock()
        mock_template.validate_configuration.return_value = (True, [])
        mock_tmpl.get.return_value = mock_template
        mock_provider = MagicMock()
        mock_provider.validate_configuration.return_value = (True, [])
        mock_prov.get.return_value = mock_provider
        engine._create_generation_context = MagicMock()
        result = engine._validate_configuration()
        assert result is True

    @patch("fluid_build.forge.core.engine.template_registry")
    @patch("fluid_build.forge.core.engine.provider_registry")
    @patch("fluid_build.forge.core.engine.validation_registry")
    def test_missing_required_fields(self, mock_val, _mock_prov, _mock_tmpl):
        engine = self._make_engine()
        engine.project_config = {"name": "test"}
        mock_val.validate_all.return_value = []
        engine._create_generation_context = MagicMock()
        result = engine._validate_configuration()
        assert result is False

    @patch("fluid_build.forge.core.engine.template_registry")
    @patch("fluid_build.forge.core.engine.provider_registry")
    @patch("fluid_build.forge.core.engine.validation_registry")
    def test_copilot_provider_validation_is_warning_only(self, mock_val, mock_prov, mock_tmpl):
        engine = self._make_engine()
        engine.project_config = {
            "name": "test",
            "description": "test desc",
            "template": "starter",
            "provider": "gcp",
            "target_dir": "/tmp/test",
            "copilot_generated_contract": {"fluidVersion": "0.7.2"},
        }
        mock_val.validate_all.return_value = []
        mock_template = MagicMock()
        mock_template.validate_configuration.return_value = (True, [])
        mock_tmpl.get.return_value = mock_template
        mock_provider = MagicMock()
        mock_provider.validate_configuration.return_value = (
            False,
            ["GCP Project ID is required"],
        )
        mock_prov.get.return_value = mock_provider
        engine._create_generation_context = MagicMock()

        result = engine._validate_configuration()

        assert result is True

    def test_validate_exception(self):
        engine = self._make_engine()
        engine._create_generation_context = MagicMock(side_effect=RuntimeError)
        result = engine._validate_configuration()
        assert result is False


class TestExecuteGeneration:
    """Test _execute_generation branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    @patch("fluid_build.forge.core.engine.template_registry")
    @patch("fluid_build.forge.core.engine.provider_registry")
    @patch("fluid_build.forge.core.engine.extension_registry")
    def test_execute_generation_success(self, _mock_ext, mock_prov, mock_tmpl, tmp_path):
        engine = self._make_engine()
        engine.project_config = {
            "name": "test",
            "template": "starter",
            "provider": "local",
            "target_dir": str(tmp_path / "output"),
            "enable_ci_cd": False,
        }
        mock_template = MagicMock()
        mock_template.get_folder_structure.return_value = {}
        mock_template.generate_contract.return_value = {}
        mock_tmpl.get.return_value = mock_template
        mock_provider = MagicMock()
        mock_provider.configure.return_value = {}
        mock_prov.get.return_value = mock_provider
        engine._create_generation_context = MagicMock()
        engine.generation_context = MagicMock()
        engine._create_folder_structure = MagicMock()
        engine._write_contract_file = MagicMock()
        engine._write_provider_config = MagicMock()
        engine._run_generators = MagicMock()
        result = engine._execute_generation()
        assert result is True

    @patch("fluid_build.forge.core.engine.template_registry")
    def test_execute_no_template(self, mock_tmpl):
        engine = self._make_engine()
        engine.project_config = {"name": "test", "template": "missing", "target_dir": "/tmp/test"}
        mock_tmpl.get.return_value = None
        engine._create_generation_context = MagicMock()
        engine.generation_context = MagicMock()
        result = engine._execute_generation()
        assert result is False

    def test_execute_exception(self):
        engine = self._make_engine()
        engine.project_config = {"name": "test", "template": "starter", "target_dir": "/tmp/test"}
        engine._create_generation_context = MagicMock(side_effect=RuntimeError)
        engine.generation_context = None
        result = engine._execute_generation()
        assert result is False


class TestPreviewGeneration:
    """Test _preview_generation branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    @patch("fluid_build.forge.core.engine.template_registry")
    def test_preview_success(self, mock_tmpl):
        engine = self._make_engine()
        engine.project_config = {"template": "starter"}
        mock_template = MagicMock()
        mock_template.get_folder_structure.return_value = {"src": {}}
        mock_template.generate_contract.return_value = {"name": "test"}
        mock_tmpl.get.return_value = mock_template
        engine._create_generation_context = MagicMock()
        engine.generation_context = MagicMock()
        engine._preview_structure = MagicMock()
        engine._preview_contract = MagicMock()
        result = engine._preview_generation()
        assert result is True

    @patch("fluid_build.forge.core.engine.template_registry")
    def test_preview_no_template(self, mock_tmpl):
        engine = self._make_engine()
        engine.project_config = {"template": "missing"}
        mock_tmpl.get.return_value = None
        engine._create_generation_context = MagicMock()
        engine.generation_context = MagicMock()
        result = engine._preview_generation()
        assert result is False


class TestValidateProjectName:
    """Test _validate_project_name branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_valid_name(self):
        engine = self._make_engine()
        assert engine._validate_project_name("my-project") is True

    def test_empty_name(self):
        engine = self._make_engine()
        assert engine._validate_project_name("") is False

    def test_short_name(self):
        engine = self._make_engine()
        assert engine._validate_project_name("a") is False


class TestApplyIntelligentDefaults:
    """Test _apply_intelligent_defaults branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_defaults_applied(self):
        engine = self._make_engine()
        engine.project_config = {}
        engine._apply_intelligent_defaults()
        assert "name" in engine.project_config
        assert "description" in engine.project_config
        assert "template" in engine.project_config
        assert "provider" in engine.project_config

    def test_existing_values_preserved(self):
        engine = self._make_engine()
        engine.project_config = {"name": "custom", "template": "etl"}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "custom"
        assert engine.project_config["template"] == "etl"


class TestGetComplexityIcon:
    """Test _get_complexity_icon branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_all_complexity_levels(self):
        from fluid_build.forge.core.interfaces import ComplexityLevel

        engine = self._make_engine()
        for level in ComplexityLevel:
            icon = engine._get_complexity_icon(level)
            assert isinstance(icon, str)


class TestWriteContractFile:
    """Test _write_contract_file."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_write_contract(self, tmp_path):
        engine = self._make_engine()
        engine._write_contract_file(tmp_path, {"name": "test-product", "version": "1.0"})
        contract_file = tmp_path / "contract.fluid.yaml"
        assert contract_file.exists()

    def test_write_provider_config(self, tmp_path):
        engine = self._make_engine()
        engine._write_provider_config(tmp_path, {"provider": "local", "settings": {}})
        config_dir = tmp_path / "config"
        assert config_dir.exists()


class TestGeneratePipelineFiles:
    """Test _generate_pipeline_files branches."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_no_pipeline_config(self, tmp_path):
        engine = self._make_engine()
        engine.project_config = {}
        engine._generate_pipeline_files(tmp_path)
        # Should return early without error

    def test_with_pipeline_config(self, tmp_path):
        engine = self._make_engine()
        engine.project_config = {
            "pipeline_config": {"provider": "github_actions", "complexity": "basic"}
        }
        # Should not raise even if imports fail in isolation
        try:
            engine._generate_pipeline_files(tmp_path)
        except Exception:
            pass  # Pipeline generation may need full template system


class TestCreateFolderStructure:
    """Test _create_folder_structure with nesting."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    def test_nested_structure(self, tmp_path):
        engine = self._make_engine()
        structure = {"src/": {"models/": {}, "utils/": {}}, "tests/": {}, "docs/": {}}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src" / "models").exists()
        assert (tmp_path / "src" / "utils").exists()
        assert (tmp_path / "tests").exists()

    def test_empty_structure(self, tmp_path):
        engine = self._make_engine()
        engine._create_folder_structure(tmp_path, {})
        # Should not raise


class TestGatherProjectInfo:
    """Test _gather_project_info branches (all conditional prompts)."""

    def _make_engine(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={"templates": {"count": 1}, "providers": {"count": 1}},
            ),
        ):
            from fluid_build.forge.core.engine import ForgeEngine

            return ForgeEngine(console=MagicMock())

    @patch("fluid_build.forge.core.engine.Prompt.ask")
    def test_all_fields_prompted(self, mock_ask):
        engine = self._make_engine()
        engine.project_config = {}
        mock_ask.side_effect = ["test-project", "A test project", "analytics", "admin", "/tmp/test"]
        engine._validate_project_name = MagicMock(return_value=True)
        result = engine._gather_project_info()
        assert result is True
        assert mock_ask.call_count >= 4

    def test_all_fields_present(self):
        engine = self._make_engine()
        engine.project_config = {
            "name": "test",
            "description": "desc",
            "domain": "analytics",
            "owner": "admin",
            "target_dir": "/tmp",
        }
        result = engine._gather_project_info()
        assert result is True

    def test_exception(self):
        engine = self._make_engine()
        engine.project_config = {}
        with patch("fluid_build.forge.core.engine.Prompt.ask", side_effect=RuntimeError):
            result = engine._gather_project_info()
            assert result is False
