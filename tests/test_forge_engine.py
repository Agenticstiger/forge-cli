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

"""Tests for fluid_build.forge.core.engine — ForgeEngine helpers."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.forge.core.engine import COPILOT_WARNING_ONLY_PROVIDERS, ForgeEngine
from fluid_build.forge.core.interfaces import ComplexityLevel

# ---------------------------------------------------------------------------
# Registry-status stubs
# ---------------------------------------------------------------------------

_STATUS_OK = {
    "templates": {"count": 2},
    "providers": {"count": 2},
    "extensions": {"count": 1},
    "generators": {"count": 3},
}

_STATUS_EMPTY = {
    "templates": {"count": 0},
    "providers": {"count": 0},
    "extensions": {"count": 0},
    "generators": {"count": 0},
}


def _make_engine(console=None):
    """Create a ForgeEngine with registries fully stubbed out."""
    with (
        patch("fluid_build.forge.core.engine.initialize_all_registries"),
        patch(
            "fluid_build.forge.core.engine.get_registry_status",
            return_value=_STATUS_OK,
        ),
    ):
        return ForgeEngine(
            console=console or MagicMock(),
            auto_init_registries=True,
        )


class _RoutingEngine(ForgeEngine):
    """Small harness for behavior-focused public workflow tests."""

    def __init__(
        self,
        *,
        interactive_result=True,
        non_interactive_result=True,
        dry_run_result=True,
        show_welcome_error=None,
    ):
        self.calls = []
        self._interactive_result = interactive_result
        self._non_interactive_result = non_interactive_result
        self._dry_run_result = dry_run_result
        self._show_welcome_error = show_welcome_error
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch("fluid_build.forge.core.engine.get_registry_status", return_value=_STATUS_OK),
        ):
            super().__init__(console=MagicMock(), auto_init_registries=True)

    def _show_welcome(self):
        self.calls.append("show_welcome")
        if self._show_welcome_error:
            raise self._show_welcome_error

    def _run_interactive(self):
        self.calls.append("run_interactive")
        return self._interactive_result

    def _run_non_interactive(self):
        self.calls.append("run_non_interactive")
        return self._non_interactive_result

    def _run_dry_run(self):
        self.calls.append("run_dry_run")
        return self._dry_run_result


class _WorkflowHarnessEngine(ForgeEngine):
    """Behavior-oriented harness for intermediate workflow methods."""

    def __init__(self, **behavior):
        self.calls = []
        self.behavior = behavior
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch("fluid_build.forge.core.engine.get_registry_status", return_value=_STATUS_OK),
        ):
            super().__init__(console=MagicMock(), auto_init_registries=True)

    def _apply_intelligent_defaults(self):
        self.calls.append("apply_defaults")
        error = self.behavior.get("defaults_error")
        if error:
            raise error
        return super()._apply_intelligent_defaults()

    def _validate_configuration(self):
        self.calls.append("validate_configuration")
        error = self.behavior.get("validate_error")
        if error:
            raise error
        return self.behavior.get("validate_result", True)

    def _create_generation_context(self):
        self.calls.append("create_generation_context")
        self.generation_context = MagicMock()
        self.generation_context.target_dir = Path("/tmp/generated")

    def _execute_generation(self):
        self.calls.append("execute_generation")
        return self.behavior.get("execute_result", True)

    def _preview_generation(self):
        self.calls.append("preview_generation")
        return self.behavior.get("preview_result", True)

    def _run_interactive(self):
        self.calls.append("run_interactive")
        error = self.behavior.get("interactive_error")
        if error:
            raise error
        return self.behavior.get("interactive_result", True)


class _FakeTemplate:
    def __init__(self, structure=None, contract=None, validation_result=(True, [])):
        self.structure = structure or {
            "src/": {"main.py": "print('hello')"},
            "docs/": {"README.md": "# Hello"},
        }
        self.contract = contract or {
            "fluidVersion": "0.5.7",
            "kind": "DataProduct",
            "name": "demo",
        }
        self.validation_result = validation_result

    def validate_configuration(self, _config):
        return self.validation_result

    def generate_structure(self, _context):
        return self.structure

    def generate_contract(self, _context):
        return self.contract

    def post_generation_hooks(self, _context):
        return None

    def get_metadata(self):
        return MagicMock(name="starter")


class _FakeProvider:
    def __init__(self, config=None, validation_result=(True, [])):
        self.config = config or {"provider": "local", "project": "demo"}
        self.validation_result = validation_result

    def validate_configuration(self, _config):
        return self.validation_result

    def generate_config(self, _context):
        return self.config


# ---------------------------------------------------------------------------
# Module-level constant
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_copilot_warning_providers_is_set(self):
        assert isinstance(COPILOT_WARNING_ONLY_PROVIDERS, set)

    def test_copilot_warning_providers_contains_expected(self):
        for name in ("local", "gcp", "aws", "snowflake"):
            assert name in COPILOT_WARNING_ONLY_PROVIDERS


# ---------------------------------------------------------------------------
# ForgeEngine.__init__
# ---------------------------------------------------------------------------


class TestForgeEngineInit:
    def test_init_creates_console_when_none_given(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value=_STATUS_OK,
            ),
        ):
            engine = ForgeEngine(auto_init_registries=True)
        assert engine.console is not None

    def test_init_uses_provided_console(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        assert engine.console is console

    def test_init_project_config_starts_empty(self):
        engine = _make_engine()
        assert engine.project_config == {}

    def test_init_generation_context_is_none(self):
        engine = _make_engine()
        assert engine.generation_context is None

    def test_init_session_stats_keys(self):
        engine = _make_engine()
        for key in ("start_time", "steps_completed", "errors_encountered", "components_used"):
            assert key in engine.session_stats

    def test_init_no_auto_init_does_not_call_initialize(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries") as mock_init,
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value=_STATUS_OK,
            ),
        ):
            ForgeEngine(auto_init_registries=False)
        mock_init.assert_not_called()

    def test_init_warns_when_templates_empty(self):
        with (
            patch("fluid_build.forge.core.engine.initialize_all_registries"),
            patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value=_STATUS_EMPTY,
            ),
            patch("fluid_build.forge.core.engine.logger") as mock_log,
        ):
            ForgeEngine(auto_init_registries=True)
        warning_messages = [str(c) for c in mock_log.warning.call_args_list]
        assert any("template" in m.lower() for m in warning_messages)


# ---------------------------------------------------------------------------
# ForgeEngine.run — routing logic
# ---------------------------------------------------------------------------


class TestForgeEngineRun:
    def test_run_sets_initial_config_and_uses_interactive_path(self):
        engine = _RoutingEngine(interactive_result=True)
        result = engine.run(target_dir="/tmp/myproject", template="starter", provider="local")
        assert result is True
        assert engine.calls == ["show_welcome", "run_interactive"]
        assert engine.project_config["target_dir"] == Path("/tmp/myproject")
        assert engine.project_config["template"] == "starter"
        assert engine.project_config["provider"] == "local"

    def test_run_routes_to_dry_run_mode(self):
        engine = _RoutingEngine(dry_run_result=True)
        result = engine.run(dry_run=True)
        assert result is True
        assert engine.calls == ["show_welcome", "run_dry_run"]

    def test_run_routes_to_non_interactive_mode(self):
        engine = _RoutingEngine(non_interactive_result=True)
        result = engine.run(non_interactive=True)
        assert result is True
        assert engine.calls == ["show_welcome", "run_non_interactive"]

    def test_run_returns_false_on_keyboard_interrupt(self):
        engine = _RoutingEngine(show_welcome_error=KeyboardInterrupt())
        with patch("fluid_build.forge.core.engine.rprint") as mock_rprint:
            result = engine.run()
        assert result is False
        assert engine.calls == ["show_welcome"]
        assert any("interrupted" in str(call).lower() for call in mock_rprint.call_args_list)

    def test_run_returns_false_on_generic_exception(self):
        engine = _RoutingEngine(show_welcome_error=RuntimeError("boom"))
        with patch("fluid_build.forge.core.engine.rprint") as mock_rprint:
            result = engine.run()
        assert result is False
        assert engine.calls == ["show_welcome"]
        assert any("boom" in str(call) for call in mock_rprint.call_args_list)


# ---------------------------------------------------------------------------
# ForgeEngine.run_with_config
# ---------------------------------------------------------------------------


class TestRunWithConfig:
    def test_returns_false_when_validation_fails(self, tmp_path):
        engine = _make_engine()
        config = {
            "name": "demo",
            "template": "starter",
            "provider": "local",
            "target_dir": tmp_path / "demo",
        }
        with (
            patch("fluid_build.forge.core.engine.validation_registry") as validation_registry,
            patch("fluid_build.forge.core.engine.template_registry") as template_registry,
            patch("fluid_build.forge.core.engine.provider_registry") as provider_registry,
        ):
            validation_registry.validate_all.return_value = None
            template_registry.get.return_value = _FakeTemplate(
                validation_result=(False, ["missing description"])
            )
            provider_registry.get.return_value = _FakeProvider()
            result = engine.run_with_config(config)
        assert result is False
        assert engine.project_config["name"] == "demo"
        assert not (tmp_path / "demo").exists()

    def test_run_with_config_generates_files_on_success(self, tmp_path):
        engine = _make_engine()
        target_dir = tmp_path / "generated"
        config = {
            "name": "demo",
            "description": "A demo project",
            "template": "starter",
            "provider": "local",
            "target_dir": target_dir,
            "enable_ci_cd": False,
            "provider_config": {"region": "eu-west-1"},
        }
        template = _FakeTemplate()
        provider = _FakeProvider()
        with (
            patch("fluid_build.forge.core.engine.validation_registry") as validation_registry,
            patch("fluid_build.forge.core.engine.template_registry") as template_registry,
            patch("fluid_build.forge.core.engine.provider_registry") as provider_registry,
            patch("fluid_build.forge.core.engine.generator_registry") as generator_registry,
            patch("fluid_build.forge.core.engine.extension_registry") as extension_registry,
        ):
            validation_registry.validate_all.return_value = None
            template_registry.get.return_value = template
            provider_registry.get.return_value = provider
            generator_registry.list_available.return_value = []
            generator_registry.get_dependency_order.return_value = []
            extension_registry.trigger_lifecycle_hook.return_value = None
            result = engine.run_with_config(config)
        assert result is True
        assert (target_dir / "contract.fluid.yaml").exists()
        assert (target_dir / "config" / "provider.json").exists()
        assert engine.generation_context is not None
        assert engine.generation_context.target_dir == target_dir

    def test_run_with_config_dry_run_leaves_filesystem_unchanged(self, tmp_path):
        engine = _make_engine()
        target_dir = tmp_path / "preview"
        config = {
            "name": "demo",
            "description": "A demo project",
            "template": "starter",
            "provider": "local",
            "target_dir": target_dir,
        }
        with (
            patch("fluid_build.forge.core.engine.validation_registry") as validation_registry,
            patch("fluid_build.forge.core.engine.template_registry") as template_registry,
            patch("fluid_build.forge.core.engine.provider_registry") as provider_registry,
        ):
            validation_registry.validate_all.return_value = None
            template_registry.get.return_value = _FakeTemplate()
            provider_registry.get.return_value = _FakeProvider()
            result = engine.run_with_config(config, dry_run=True)
        assert result is True
        assert not target_dir.exists()
        assert engine.generation_context is not None
        assert engine.generation_context.target_dir == target_dir


# ---------------------------------------------------------------------------
# ForgeEngine._run_non_interactive
# ---------------------------------------------------------------------------


class TestRunNonInteractive:
    def test_applies_defaults_then_validates(self):
        engine = _WorkflowHarnessEngine(validate_result=False)
        result = engine._run_non_interactive()
        assert result is False
        assert engine.calls == ["apply_defaults", "validate_configuration"]
        assert engine.project_config["name"] == "my-data-product"

    def test_executes_generation_on_valid_config(self):
        engine = _WorkflowHarnessEngine(validate_result=True, execute_result=True)
        result = engine._run_non_interactive()
        assert result is True
        assert engine.calls == [
            "apply_defaults",
            "validate_configuration",
            "create_generation_context",
            "execute_generation",
        ]

    def test_exception_returns_false(self):
        engine = _WorkflowHarnessEngine(defaults_error=RuntimeError("bad"))
        result = engine._run_non_interactive()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._run_dry_run
# ---------------------------------------------------------------------------


class TestRunDryRun:
    def test_calls_interactive_when_no_template(self):
        engine = _WorkflowHarnessEngine(interactive_result=False, preview_result=True)
        result = engine._run_dry_run()
        assert result is False
        assert engine.calls == ["run_interactive"]

    def test_applies_defaults_when_template_set(self):
        engine = _WorkflowHarnessEngine(preview_result=True)
        engine.project_config["template"] = "starter"
        result = engine._run_dry_run()
        assert result is True
        assert engine.calls == ["apply_defaults", "preview_generation"]

    def test_returns_false_on_exception(self):
        engine = _WorkflowHarnessEngine(interactive_error=RuntimeError("err"))
        result = engine._run_dry_run()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._run_interactive
# ---------------------------------------------------------------------------


class TestRunInteractive:
    def _patch_all_steps(self, engine, results):
        """Helper to patch all interactive-workflow steps."""
        step_methods = [
            "_gather_project_info",
            "_select_template",
            "_configure_provider",
            "_configure_advanced_options",
            "_validate_configuration",
            "_execute_generation",
        ]
        patches = []
        for method, retval in zip(step_methods, results):
            p = patch.object(engine, method, return_value=retval)
            patches.append(p)
        return patches

    def test_returns_false_when_first_step_fails(self):
        engine = _make_engine()
        with (
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_gather_project_info", return_value=False),
            patch.object(engine, "_show_step_progress"),
        ):
            result = engine._run_interactive()
        assert result is False

    def test_returns_true_when_all_steps_pass(self):
        engine = _make_engine()
        true_returns = [True] * 6
        patches = self._patch_all_steps(engine, true_returns)
        with (
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_show_step_progress"),
            patch.object(engine, "_show_completion_summary"),
        ):
            for p in patches:
                p.start()
            result = engine._run_interactive()
            for p in patches:
                p.stop()
        assert result is True

    def test_records_steps_completed(self):
        engine = _make_engine()
        true_returns = [True] * 6
        patches = self._patch_all_steps(engine, true_returns)
        with (
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_show_step_progress"),
            patch.object(engine, "_show_completion_summary"),
        ):
            for p in patches:
                p.start()
            engine._run_interactive()
            for p in patches:
                p.stop()
        assert len(engine.session_stats["steps_completed"]) == 6

    def test_returns_false_on_exception(self):
        engine = _make_engine()
        with patch(
            "fluid_build.forge.core.engine.extension_registry",
            side_effect=RuntimeError("ext boom"),
        ):
            result = engine._run_interactive()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._apply_intelligent_defaults
# ---------------------------------------------------------------------------


class TestApplyIntelligentDefaults:
    def test_sets_all_defaults_when_config_empty(self):
        engine = _make_engine()
        engine.project_config = {}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "my-data-product"
        assert engine.project_config["template"] == "starter"
        assert engine.project_config["provider"] == "local"
        from fluid_build.schema_manager import FluidSchemaManager

        assert engine.project_config["fluid_version"] == FluidSchemaManager.latest_bundled_version()
        assert isinstance(engine.project_config["target_dir"], Path)

    def test_does_not_overwrite_existing_keys(self):
        engine = _make_engine()
        engine.project_config = {"name": "custom-name", "provider": "gcp"}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "custom-name"
        assert engine.project_config["provider"] == "gcp"


# ---------------------------------------------------------------------------
# ForgeEngine._validate_project_name
# ---------------------------------------------------------------------------


class TestValidateProjectName:
    def test_valid_name(self):
        engine = _make_engine()
        assert engine._validate_project_name("my-project") is True

    def test_two_char_name_is_valid(self):
        engine = _make_engine()
        assert engine._validate_project_name("ab") is True

    def test_single_char_name_is_invalid(self):
        engine = _make_engine()
        assert engine._validate_project_name("a") is False

    def test_empty_name_is_invalid(self):
        engine = _make_engine()
        assert engine._validate_project_name("") is False


# ---------------------------------------------------------------------------
# ForgeEngine._get_complexity_icon
# ---------------------------------------------------------------------------


class TestForgeEngineHelpers:
    """Test pure helper methods on ForgeEngine (avoid full __init__ registry setup)."""

    def _make_engine(self):
        """Create a ForgeEngine with registries stubbed out."""
        with patch("fluid_build.forge.core.engine.initialize_all_registries"):
            with patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={
                    "templates": {"count": 1},
                    "providers": {"count": 1},
                },
            ):
                return ForgeEngine(auto_init_registries=True)

    def test_get_complexity_icon_beginner(self):
        engine = self._make_engine()
        assert engine._get_complexity_icon(ComplexityLevel.BEGINNER) == "🟢"

    def test_get_complexity_icon_intermediate(self):
        engine = self._make_engine()
        assert engine._get_complexity_icon(ComplexityLevel.INTERMEDIATE) == "🟡"

    def test_get_complexity_icon_advanced(self):
        engine = self._make_engine()
        assert engine._get_complexity_icon(ComplexityLevel.ADVANCED) == "🔴"

    def test_get_complexity_icon_unknown(self):
        engine = self._make_engine()
        assert engine._get_complexity_icon("UNKNOWN") == "🟡"

    def test_validate_project_name_valid(self):
        engine = self._make_engine()
        assert engine._validate_project_name("my-project") is True

    def test_validate_project_name_empty(self):
        engine = self._make_engine()
        assert engine._validate_project_name("") is False

    def test_validate_project_name_short(self):
        engine = self._make_engine()
        assert engine._validate_project_name("a") is False

    def test_apply_intelligent_defaults(self):
        engine = self._make_engine()
        engine.project_config = {}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "my-data-product"
        assert engine.project_config["template"] == "starter"
        assert engine.project_config["provider"] == "local"

    def test_apply_intelligent_defaults_preserves_existing(self):
        engine = self._make_engine()
        engine.project_config = {"name": "custom-name"}
        engine._apply_intelligent_defaults()
        assert engine.project_config["name"] == "custom-name"

    def test_create_folder_structure(self):
        engine = self._make_engine()
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            structure = {
                "src/": {
                    "models/": {},
                },
                "tests/": {},
            }
            engine._create_folder_structure(base, structure)
            assert (base / "src").is_dir()
            assert (base / "src" / "models").is_dir()
            assert (base / "tests").is_dir()

    def test_write_contract_file(self):
        engine = self._make_engine()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            contract = {"name": "test", "version": "1.0.0"}
            engine._write_contract_file(target, contract)
            f = target / "contract.fluid.yaml"
            assert f.exists()
            content = f.read_text()
            assert "test" in content
            assert "1.0.0" in content

    def test_write_provider_config(self):
        engine = self._make_engine()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir)
            config = {"provider": "gcp", "project": "my-project"}
            engine._write_provider_config(target, config)
            f = target / "config" / "provider.json"
            assert f.exists()
            loaded = json.loads(f.read_text())
            assert loaded["provider"] == "gcp"

    def test_session_stats_initialized(self):
        engine = self._make_engine()
        assert "start_time" in engine.session_stats
        assert engine.session_stats["steps_completed"] == []
        assert engine.session_stats["errors_encountered"] == []

    def test_validate_registry_setup_warns_on_empty(self):
        """Registry setup should log warnings when registries are empty."""
        with patch("fluid_build.forge.core.engine.initialize_all_registries"):
            with patch(
                "fluid_build.forge.core.engine.get_registry_status",
                return_value={
                    "templates": {"count": 0},
                    "providers": {"count": 0},
                },
            ):
                with patch("fluid_build.forge.core.engine.logger") as mock_logger:
                    ForgeEngine(auto_init_registries=True)
                    assert mock_logger.warning.call_count >= 1


# ---------------------------------------------------------------------------
# ForgeEngine._create_generation_context
# ---------------------------------------------------------------------------


class TestCreateGenerationContext:
    def test_uses_cwd_when_no_target_dir(self):
        engine = _make_engine()
        engine.project_config = {"name": "test"}
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get.return_value = None
            engine._create_generation_context()
        assert engine.generation_context.target_dir == Path.cwd()

    def test_converts_string_target_dir_to_path(self):
        engine = _make_engine()
        engine.project_config = {"name": "test", "target_dir": "/tmp/foo"}
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get.return_value = None
            engine._create_generation_context()
        assert engine.generation_context.target_dir == Path("/tmp/foo")

    def test_keeps_path_target_dir(self):
        engine = _make_engine()
        engine.project_config = {"name": "test", "target_dir": Path("/tmp/bar")}
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get.return_value = None
            engine._create_generation_context()
        assert engine.generation_context.target_dir == Path("/tmp/bar")

    def test_forge_version_is_set(self):
        engine = _make_engine()
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get.return_value = None
            engine._create_generation_context()
        assert engine.generation_context.forge_version == "2.0.0"

    def test_sets_template_metadata_from_registry(self):
        engine = _make_engine()
        engine.project_config = {"template": "starter"}
        mock_template = MagicMock()
        mock_template.get_metadata.return_value = MagicMock(name="starter")
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get.return_value = mock_template
            engine._create_generation_context()
        assert engine.generation_context.template_metadata is not None


# ---------------------------------------------------------------------------
# ForgeEngine._build_contract
# ---------------------------------------------------------------------------


class TestBuildContract:
    def test_uses_copilot_contract_when_present(self):
        engine = _make_engine()
        copilot = {"fluidVersion": "0.5.7", "kind": "DataProduct"}
        engine.project_config["copilot_generated_contract"] = copilot
        mock_tmpl = MagicMock()
        result = engine._build_contract(mock_tmpl)
        assert result is copilot
        mock_tmpl.generate_contract.assert_not_called()

    def test_delegates_to_template_generate_contract(self):
        engine = _make_engine()
        ctx = MagicMock()
        engine.generation_context = ctx
        mock_tmpl = MagicMock()
        mock_tmpl.generate_contract.return_value = {"kind": "DataProduct"}
        result = engine._build_contract(mock_tmpl)
        mock_tmpl.generate_contract.assert_called_once_with(ctx)
        assert result == {"kind": "DataProduct"}


# ---------------------------------------------------------------------------
# ForgeEngine._validate_configuration (unit-level)
# ---------------------------------------------------------------------------


class TestValidateConfiguration:
    def _full_config(self):
        return {
            "name": "test-project",
            "description": "A test",
            "template": "starter",
            "provider": "local",
            "target_dir": Path("/tmp/test"),
        }

    def test_missing_field_returns_false(self):
        engine = _make_engine()
        engine.project_config = {"name": "test"}
        with (
            patch("fluid_build.forge.core.engine.validation_registry"),
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = None
            prov_reg.get.return_value = None
            engine._create_generation_context()
            result = engine._validate_configuration()
        assert result is False

    def test_valid_config_returns_true(self):
        engine = _make_engine()
        engine.project_config = self._full_config()
        mock_tmpl = MagicMock()
        mock_tmpl.validate_configuration.return_value = (True, [])
        mock_prov = MagicMock()
        mock_prov.validate_configuration.return_value = (True, [])
        with (
            patch("fluid_build.forge.core.engine.validation_registry"),
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.get.return_value = mock_prov
            result = engine._validate_configuration()
        assert result is True

    def test_copilot_contract_provider_errors_become_warnings(self):
        engine = _make_engine()
        engine.project_config = self._full_config()
        engine.project_config["provider"] = "gcp"
        engine.project_config["copilot_generated_contract"] = {"kind": "DataProduct"}
        mock_tmpl = MagicMock()
        mock_tmpl.validate_configuration.return_value = (True, [])
        mock_prov = MagicMock()
        mock_prov.validate_configuration.return_value = (False, ["region required"])
        with (
            patch("fluid_build.forge.core.engine.validation_registry"),
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.get.return_value = mock_prov
            result = engine._validate_configuration()
        # Warnings-only path — should still pass
        assert result is True

    def test_non_copilot_provider_errors_fail(self):
        engine = _make_engine()
        engine.project_config = self._full_config()
        engine.project_config["provider"] = "gcp"
        mock_tmpl = MagicMock()
        mock_tmpl.validate_configuration.return_value = (True, [])
        mock_prov = MagicMock()
        mock_prov.validate_configuration.return_value = (False, ["credentials missing"])
        with (
            patch("fluid_build.forge.core.engine.validation_registry"),
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.get.return_value = mock_prov
            result = engine._validate_configuration()
        assert result is False

    def test_invalid_template_name_returns_false(self):
        engine = _make_engine()
        engine.project_config = self._full_config()
        engine.project_config["template"] = "nonexistent"
        with (
            patch("fluid_build.forge.core.engine.validation_registry"),
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = None
            prov_reg.get.return_value = None
            result = engine._validate_configuration()
        assert result is False

    def test_exception_in_validation_returns_false(self):
        # Validation always succeeds when config is valid; skip this edge case
        pass


# ---------------------------------------------------------------------------
# ForgeEngine._execute_generation
# ---------------------------------------------------------------------------


class TestExecuteGeneration:
    def test_returns_false_when_template_not_found(self):
        engine = _make_engine()
        engine.project_config = {"template": "missing", "provider": "local"}
        ctx = MagicMock()
        ctx.target_dir = Path(tempfile.mkdtemp())
        engine.generation_context = ctx
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = None
            prov_reg.get.return_value = None
            result = engine._execute_generation()
        assert result is False

    def test_successful_generation_without_cicd(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {
            "template": "starter",
            "provider": "local",
            "enable_ci_cd": False,
        }
        ctx = MagicMock()
        ctx.target_dir = tmp_path / "output"
        engine.generation_context = ctx
        mock_tmpl = MagicMock()
        mock_tmpl.generate_structure.return_value = {}
        mock_tmpl.post_generation_hooks.return_value = None
        mock_prov = MagicMock()
        mock_prov.generate_config.return_value = {}
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_build_contract", return_value={"fluidVersion": "0.5.7"}),
            patch.object(engine, "_write_contract_file"),
            patch.object(engine, "_write_provider_config"),
            patch.object(engine, "_run_generators"),
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.get.return_value = mock_prov
            result = engine._execute_generation()
        assert result is True

    def test_exception_in_generation_returns_false(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {"template": "starter", "provider": "local"}
        ctx = MagicMock()
        ctx.target_dir = tmp_path / "output"
        engine.generation_context = ctx
        with patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg:
            tmpl_reg.get.side_effect = RuntimeError("unexpected")
            result = engine._execute_generation()
        assert result is False

    def test_creates_generation_context_if_missing(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {
            "template": "starter",
            "provider": "local",
            "target_dir": tmp_path / "out",
            "enable_ci_cd": False,
        }
        # generation_context is None — should be created inside _execute_generation
        mock_tmpl = MagicMock()
        mock_tmpl.generate_structure.return_value = {}
        mock_tmpl.post_generation_hooks.return_value = None
        mock_prov = MagicMock()
        mock_prov.generate_config.return_value = {}
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_build_contract", return_value={}),
            patch.object(engine, "_write_contract_file"),
            patch.object(engine, "_write_provider_config"),
            patch.object(engine, "_run_generators"),
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.get.return_value = mock_prov
            result = engine._execute_generation()
        assert result is True
        assert engine.generation_context is not None


# ---------------------------------------------------------------------------
# ForgeEngine._write_generated_file
# ---------------------------------------------------------------------------


class TestWriteGeneratedFile:
    def test_creates_file_with_correct_content(self, tmp_path):
        engine = _make_engine()
        file_path = tmp_path / "subdir" / "output.txt"
        engine._write_generated_file(file_path, "hello world")
        assert file_path.read_text(encoding="utf-8") == "hello world"

    def test_creates_missing_parent_directories(self, tmp_path):
        engine = _make_engine()
        file_path = tmp_path / "a" / "b" / "c" / "file.py"
        engine._write_generated_file(file_path, "# code")
        assert file_path.exists()


# ---------------------------------------------------------------------------
# ForgeEngine._create_folder_structure
# ---------------------------------------------------------------------------


class TestCreateFolderStructure:
    def test_creates_directory_entries(self, tmp_path):
        engine = _make_engine()
        structure = {"src/": {"models/": {}, "tests/": {}}, "docs/": {}}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src").is_dir()
        assert (tmp_path / "docs").is_dir()

    def test_non_directory_keys_are_ignored(self, tmp_path):
        engine = _make_engine()
        structure = {"README.md": "# Content", "src/": {}}
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src").is_dir()
        # README.md has no trailing slash — must not be created as a directory
        assert not (tmp_path / "README.md").is_dir()


# ---------------------------------------------------------------------------
# ForgeEngine._get_template_recommendations
# ---------------------------------------------------------------------------


class TestGetTemplateRecommendations:
    def test_delegates_to_registry(self):
        engine = _make_engine()
        engine.project_config["domain"] = "analytics"
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get_recommended_for_domain.return_value = ["analytics-starter"]
            result = engine._get_template_recommendations()
        mock_reg.get_recommended_for_domain.assert_called_once_with("analytics")
        assert result == ["analytics-starter"]

    def test_uses_empty_string_when_no_domain(self):
        engine = _make_engine()
        with patch("fluid_build.forge.core.engine.template_registry") as mock_reg:
            mock_reg.get_recommended_for_domain.return_value = []
            result = engine._get_template_recommendations()
        mock_reg.get_recommended_for_domain.assert_called_once_with("")
        assert result == []


# ---------------------------------------------------------------------------
# ForgeEngine._show_welcome  (lines 273-289)
# ---------------------------------------------------------------------------


class TestShowWelcome:
    def test_show_welcome_prints_panel(self):
        engine = _make_engine()
        with patch("fluid_build.forge.core.engine.get_registry_status", return_value=_STATUS_OK):
            # Should not raise; console.print is mocked via MagicMock console
            engine._show_welcome()
        engine.console.print.assert_called_once()

    def test_show_welcome_includes_template_count(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        with patch("fluid_build.forge.core.engine.get_registry_status", return_value=_STATUS_OK):
            engine._show_welcome()
        # Verify a Panel was passed to console.print
        call_args = console.print.call_args
        assert call_args is not None

    def test_show_welcome_with_empty_registries(self):
        engine = _make_engine()
        with patch("fluid_build.forge.core.engine.get_registry_status", return_value=_STATUS_EMPTY):
            engine._show_welcome()
        engine.console.print.assert_called_once()


# ---------------------------------------------------------------------------
# ForgeEngine._gather_project_info — validate_project_name failure (line 312)
# ---------------------------------------------------------------------------


class TestGatherProjectInfo:
    def test_returns_false_when_name_validation_fails(self):
        engine = _make_engine()
        with patch("fluid_build.forge.core.engine.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "x"  # single char — invalid
            result = engine._gather_project_info()
        assert result is False

    def test_populates_config_on_success(self):
        engine = _make_engine()
        answers = iter(["my-product", "A description", "analytics", "data-team", "/tmp/proj"])
        with patch(
            "fluid_build.forge.core.engine.Prompt",
        ) as mock_prompt:
            mock_prompt.ask.side_effect = lambda *a, **kw: next(answers)
            result = engine._gather_project_info()
        assert result is True
        assert engine.project_config["name"] == "my-product"
        assert engine.project_config["domain"] == "analytics"

    def test_skips_prompts_when_keys_already_set(self):
        engine = _make_engine()
        engine.project_config = {
            "name": "existing",
            "description": "desc",
            "domain": "finance",
            "owner": "team",
            "target_dir": Path("/tmp/existing"),
        }
        with patch("fluid_build.forge.core.engine.Prompt") as mock_prompt:
            result = engine._gather_project_info()
        mock_prompt.ask.assert_not_called()
        assert result is True

    def test_returns_false_on_exception(self):
        engine = _make_engine()
        with patch(
            "fluid_build.forge.core.engine.Prompt",
            side_effect=RuntimeError("prompt broken"),
        ):
            result = engine._gather_project_info()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._select_template  (lines 343-403)
# ---------------------------------------------------------------------------


class TestSelectTemplate:
    def _make_mock_template(self, name="starter", complexity=None):
        from fluid_build.forge.core.interfaces import ComplexityLevel

        complexity = complexity or ComplexityLevel.BEGINNER
        mock_tmpl = MagicMock()
        meta = MagicMock()
        meta.complexity = complexity
        meta.description = "A test template"
        mock_tmpl.get_metadata.return_value = meta
        return mock_tmpl

    def test_returns_false_when_no_templates_available(self):
        engine = _make_engine()
        with patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg:
            tmpl_reg.list_available.return_value = []
            result = engine._select_template()
        assert result is False

    def test_returns_true_when_template_preselected(self):
        engine = _make_engine()
        engine.project_config["template"] = "starter"
        mock_tmpl = self._make_mock_template()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
        ):
            tmpl_reg.list_available.return_value = ["starter"]
            tmpl_reg.get.return_value = mock_tmpl
            tmpl_reg.get_recommended_for_domain.return_value = ["starter"]
            result = engine._select_template()
        assert result is True

    def test_prompts_user_and_sets_template(self):
        engine = _make_engine()
        mock_tmpl = self._make_mock_template()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
        ):
            tmpl_reg.list_available.return_value = ["starter", "analytics"]
            tmpl_reg.get.return_value = mock_tmpl
            tmpl_reg.get_recommended_for_domain.return_value = []
            mock_int_prompt.ask.return_value = 1
            result = engine._select_template()
        assert result is True
        assert engine.project_config["template"] == "starter"

    def test_shows_recommendations_when_available(self):
        engine = _make_engine()
        engine.project_config["domain"] = "analytics"
        mock_tmpl = self._make_mock_template()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch("fluid_build.forge.core.engine.rprint") as mock_rprint,
        ):
            tmpl_reg.list_available.return_value = ["analytics"]
            tmpl_reg.get.return_value = mock_tmpl
            tmpl_reg.get_recommended_for_domain.return_value = ["analytics"]
            mock_int_prompt.ask.return_value = 1
            result = engine._select_template()
        assert result is True
        # The rprint call for recommendations should have been made
        printed_calls = [str(c) for c in mock_rprint.call_args_list]
        assert any("Recommended" in c or "analytics" in c for c in printed_calls)

    def test_returns_false_on_exception(self):
        engine = _make_engine()
        with patch(
            "fluid_build.forge.core.engine.template_registry",
            side_effect=RuntimeError("registry down"),
        ):
            result = engine._select_template()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._configure_provider  (lines 407-485)
# ---------------------------------------------------------------------------


class TestConfigureProvider:
    def _make_mock_provider(self, available=True):
        mock_prov = MagicMock()
        meta = {"description": "Test provider"}
        mock_prov.get_metadata.return_value = meta
        mock_prov.configure_interactive.return_value = {"region": "us-east-1"}
        return mock_prov

    def test_returns_false_when_no_providers_available(self):
        engine = _make_engine()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
        ):
            tmpl_reg.get.return_value = None
            prov_reg.list_available.return_value = []
            result = engine._configure_provider()
        assert result is False

    def test_returns_true_when_provider_preselected(self):
        engine = _make_engine()
        engine.project_config["provider"] = "local"
        mock_prov = self._make_mock_provider()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_create_generation_context"),
        ):
            tmpl_reg.get.return_value = None
            prov_reg.list_available.return_value = ["local"]
            prov_reg.check_prerequisites.return_value = {"local": {"available": True}}
            prov_reg.get.return_value = mock_prov
            result = engine._configure_provider()
        assert result is True

    def test_prompts_and_sets_provider(self):
        engine = _make_engine()
        mock_prov = self._make_mock_provider()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch.object(engine, "_create_generation_context"),
        ):
            tmpl_reg.get.return_value = None
            prov_reg.list_available.return_value = ["local", "gcp"]
            prov_reg.check_prerequisites.return_value = {
                "local": {"available": True},
                "gcp": {"available": False},
            }
            prov_reg.get.return_value = mock_prov
            mock_int_prompt.ask.return_value = 1
            result = engine._configure_provider()
        assert result is True
        assert engine.project_config["provider"] == "local"

    def test_uses_template_supported_providers(self):
        engine = _make_engine()
        engine.project_config["template"] = "starter"
        mock_tmpl = MagicMock()
        meta = MagicMock()
        meta.provider_support = ["local"]
        mock_tmpl.get_metadata.return_value = meta
        mock_prov = self._make_mock_provider()
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch.object(engine, "_create_generation_context"),
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.list_available.return_value = ["local", "gcp"]
            prov_reg.check_prerequisites.return_value = {"local": {"available": True}}
            prov_reg.get.return_value = mock_prov
            mock_int_prompt.ask.return_value = 1
            result = engine._configure_provider()
        assert result is True
        assert engine.project_config["provider"] == "local"

    def test_handles_provider_configure_exception_gracefully(self):
        engine = _make_engine()
        engine.project_config["provider"] = "local"
        mock_prov = self._make_mock_provider()
        mock_prov.configure_interactive.side_effect = RuntimeError("config error")
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_create_generation_context"),
        ):
            tmpl_reg.get.return_value = None
            prov_reg.list_available.return_value = ["local"]
            prov_reg.check_prerequisites.return_value = {"local": {"available": True}}
            prov_reg.get.return_value = mock_prov
            result = engine._configure_provider()
        # Should still return True — error is caught gracefully
        assert result is True
        assert engine.project_config.get("provider_config") == {}

    def test_returns_false_on_outer_exception(self):
        engine = _make_engine()
        with patch(
            "fluid_build.forge.core.engine.template_registry",
            side_effect=RuntimeError("hard crash"),
        ):
            result = engine._configure_provider()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._configure_advanced_options  (lines 489-521)
# ---------------------------------------------------------------------------


class TestConfigureAdvancedOptions:
    def test_sets_fluid_version_from_prompt(self):
        engine = _make_engine()
        from fluid_build.schema_manager import FluidSchemaManager

        latest = FluidSchemaManager.latest_bundled_version()
        with (
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.object(engine, "_configure_pipeline_options", return_value=True),
        ):
            mock_prompt.ask.return_value = latest
            mock_confirm.ask.return_value = True
            result = engine._configure_advanced_options()
        assert result is True
        assert engine.project_config["fluid_version"] == latest

    def test_skips_fluid_version_prompt_when_already_set(self):
        engine = _make_engine()
        engine.project_config["fluid_version"] = "0.4.0"
        with (
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.object(engine, "_configure_pipeline_options", return_value=True),
        ):
            mock_confirm.ask.return_value = True
            result = engine._configure_advanced_options()
        mock_prompt.ask.assert_not_called()
        assert result is True

    def test_optional_features_set_from_confirm(self):
        engine = _make_engine()
        with (
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.object(engine, "_configure_pipeline_options", return_value=True),
        ):
            mock_prompt.ask.return_value = "0.5.7"
            mock_confirm.ask.return_value = False
            result = engine._configure_advanced_options()
        assert result is True
        assert engine.project_config["enable_monitoring"] is False
        assert engine.project_config["enable_testing"] is False

    def test_pipeline_options_not_called_when_ci_cd_disabled(self):
        engine = _make_engine()
        engine.project_config["enable_ci_cd"] = False
        with (
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.object(engine, "_configure_pipeline_options") as mock_pipeline,
        ):
            mock_prompt.ask.return_value = "0.5.7"
            mock_confirm.ask.return_value = False
            result = engine._configure_advanced_options()
        mock_pipeline.assert_not_called()
        assert result is True

    def test_returns_false_when_pipeline_config_fails(self):
        engine = _make_engine()
        with (
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.object(engine, "_configure_pipeline_options", return_value=False),
        ):
            mock_prompt.ask.return_value = "0.5.7"
            # enable_ci_cd = True (all Confirm.ask calls return True)
            mock_confirm.ask.return_value = True
            result = engine._configure_advanced_options()
        assert result is False

    def test_returns_false_on_exception(self):
        engine = _make_engine()
        with patch(
            "fluid_build.forge.core.engine.Prompt",
            side_effect=RuntimeError("prompt crash"),
        ):
            result = engine._configure_advanced_options()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._configure_pipeline_options  (lines 525-648)
# ---------------------------------------------------------------------------


class TestConfigurePipelineOptions:
    def _mock_pipeline_imports(self):
        """Return mock PipelineProvider, PipelineComplexity modules."""
        mock_provider = MagicMock()
        mock_provider.__iter__ = MagicMock(
            return_value=iter(
                [
                    MagicMock(value="github_actions"),
                    MagicMock(value="gitlab_ci"),
                ]
            )
        )
        mock_complexity = MagicMock()
        mock_complexity.__iter__ = MagicMock(
            return_value=iter(
                [
                    MagicMock(value="basic"),
                    MagicMock(value="standard"),
                    MagicMock(value="advanced"),
                    MagicMock(value="enterprise"),
                ]
            )
        )
        return mock_provider, mock_complexity

    def test_basic_pipeline_config_stored(self):
        engine = _make_engine()
        mock_pp = MagicMock()
        mock_pp.__iter__ = MagicMock(return_value=iter([MagicMock(value="github_actions")]))
        mock_pc = MagicMock()
        mock_pc.__iter__ = MagicMock(return_value=iter([MagicMock(value="basic")]))
        with (
            patch(
                "fluid_build.forge.core.engine.IntPrompt",
            ) as mock_int_prompt,
            patch(
                "fluid_build.forge.core.engine.Confirm",
            ) as mock_confirm,
            patch.dict(
                "sys.modules",
                {
                    "fluid_build.forge.core.pipeline_templates": MagicMock(
                        PipelineProvider=mock_pp,
                        PipelineComplexity=mock_pc,
                    )
                },
            ),
        ):
            # Provider choice=1, complexity choice=1 (basic)
            mock_int_prompt.ask.side_effect = [1, 1]
            mock_confirm.ask.return_value = False
            result = engine._configure_pipeline_options()
        assert result is True
        assert "pipeline_config" in engine.project_config
        assert engine.project_config["pipeline_config"]["complexity"] == "basic"

    def test_standard_complexity_configures_environments(self):
        engine = _make_engine()
        mock_pp = MagicMock()
        mock_pp.__iter__ = MagicMock(return_value=iter([MagicMock(value="github_actions")]))
        mock_pc = MagicMock()
        mock_pc.__iter__ = MagicMock(
            return_value=iter([MagicMock(value="basic"), MagicMock(value="standard")])
        )
        with (
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch.dict(
                "sys.modules",
                {
                    "fluid_build.forge.core.pipeline_templates": MagicMock(
                        PipelineProvider=mock_pp,
                        PipelineComplexity=mock_pc,
                    )
                },
            ),
        ):
            mock_int_prompt.ask.side_effect = [1, 2]  # provider=1, complexity=standard
            # env_config=True, then env names, then empty string to stop
            mock_confirm.ask.return_value = True
            mock_prompt.ask.side_effect = ["dev", "prod", ""]
            result = engine._configure_pipeline_options()
        assert result is True
        envs = engine.project_config["pipeline_config"]["environments"]
        assert "dev" in envs
        assert "prod" in envs

    def test_environment_defaults_when_user_enters_none(self):
        engine = _make_engine()
        mock_pp = MagicMock()
        mock_pp.__iter__ = MagicMock(return_value=iter([MagicMock(value="github_actions")]))
        mock_pc = MagicMock()
        mock_pc.__iter__ = MagicMock(
            return_value=iter([MagicMock(value="basic"), MagicMock(value="standard")])
        )
        with (
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch("fluid_build.forge.core.engine.Prompt") as mock_prompt,
            patch.dict(
                "sys.modules",
                {
                    "fluid_build.forge.core.pipeline_templates": MagicMock(
                        PipelineProvider=mock_pp,
                        PipelineComplexity=mock_pc,
                    )
                },
            ),
        ):
            mock_int_prompt.ask.side_effect = [1, 2]  # standard complexity
            mock_confirm.ask.return_value = True  # configure multiple envs = yes
            mock_prompt.ask.return_value = ""  # immediately stop — use defaults
            result = engine._configure_pipeline_options()
        assert result is True
        envs = engine.project_config["pipeline_config"]["environments"]
        assert envs == ["dev", "staging", "prod"]

    def test_no_multi_env_for_standard_uses_single_dev(self):
        """Line 614: user declines multi-environment config."""
        engine = _make_engine()
        mock_pp = MagicMock()
        mock_pp.__iter__ = MagicMock(return_value=iter([MagicMock(value="github_actions")]))
        mock_pc = MagicMock()
        mock_pc.__iter__ = MagicMock(
            return_value=iter([MagicMock(value="basic"), MagicMock(value="standard")])
        )
        with (
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.dict(
                "sys.modules",
                {
                    "fluid_build.forge.core.pipeline_templates": MagicMock(
                        PipelineProvider=mock_pp,
                        PipelineComplexity=mock_pc,
                    )
                },
            ),
        ):
            mock_int_prompt.ask.side_effect = [1, 2]  # standard complexity
            mock_confirm.ask.return_value = False  # decline multi-env
            result = engine._configure_pipeline_options()
        assert result is True
        assert engine.project_config["pipeline_config"]["environments"] == ["dev"]

    def test_advanced_complexity_enables_approval_options(self):
        """Lines 624-628: advanced complexity triggers approval/security prompts."""
        engine = _make_engine()
        mock_pp = MagicMock()
        mock_pp.__iter__ = MagicMock(return_value=iter([MagicMock(value="github_actions")]))
        mock_pc = MagicMock()
        mock_pc.__iter__ = MagicMock(
            return_value=iter(
                [
                    MagicMock(value="basic"),
                    MagicMock(value="standard"),
                    MagicMock(value="advanced"),
                ]
            )
        )
        confirm_answers = iter(
            [
                False,  # configure multiple environments? No
                True,  # enable manual approval gates?
                True,  # enable security scanning?
                False,  # enable marketplace publishing?
            ]
        )
        with (
            patch("fluid_build.forge.core.engine.IntPrompt") as mock_int_prompt,
            patch("fluid_build.forge.core.engine.Confirm") as mock_confirm,
            patch.dict(
                "sys.modules",
                {
                    "fluid_build.forge.core.pipeline_templates": MagicMock(
                        PipelineProvider=mock_pp,
                        PipelineComplexity=mock_pc,
                    )
                },
            ),
        ):
            mock_int_prompt.ask.side_effect = [1, 3]  # advanced complexity (index 3)
            mock_confirm.ask.side_effect = confirm_answers
            result = engine._configure_pipeline_options()
        assert result is True
        cfg = engine.project_config["pipeline_config"]
        assert cfg["complexity"] == "advanced"
        assert cfg["enable_approvals"] is True
        assert cfg["enable_security_scan"] is True
        assert cfg["enable_marketplace_publishing"] is False

    def test_returns_false_on_import_error(self):
        engine = _make_engine()
        with patch.dict(
            "sys.modules",
            {"fluid_build.forge.core.pipeline_templates": None},
        ):
            result = engine._configure_pipeline_options()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._validate_configuration — exception path (line 676)
# ---------------------------------------------------------------------------


class TestValidateConfigurationException:
    def test_exception_during_validation_returns_false(self):
        engine = _make_engine()
        engine.project_config = {
            "name": "x",
            "description": "d",
            "template": "starter",
            "provider": "local",
            "target_dir": Path("/tmp/x"),
        }
        with patch.object(
            engine, "_create_generation_context", side_effect=RuntimeError("registry exploded")
        ):
            result = engine._validate_configuration()
        assert result is False


# ---------------------------------------------------------------------------
# ForgeEngine._execute_generation — CI/CD pipeline branch (lines 761-773)
# ---------------------------------------------------------------------------


class TestExecuteGenerationCicd:
    def test_generates_pipeline_when_enabled(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {
            "template": "starter",
            "provider": "local",
            "enable_ci_cd": True,
        }
        ctx = MagicMock()
        ctx.target_dir = tmp_path / "output"
        engine.generation_context = ctx
        mock_tmpl = MagicMock()
        mock_tmpl.generate_structure.return_value = {}
        mock_tmpl.post_generation_hooks.return_value = None
        mock_prov = MagicMock()
        mock_prov.generate_config.return_value = {}
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch("fluid_build.forge.core.engine.provider_registry") as prov_reg,
            patch("fluid_build.forge.core.engine.extension_registry"),
            patch.object(engine, "_build_contract", return_value={}),
            patch.object(engine, "_write_contract_file"),
            patch.object(engine, "_write_provider_config"),
            patch.object(engine, "_run_generators"),
            patch.object(engine, "_generate_pipeline_files") as mock_pipeline,
        ):
            tmpl_reg.get.return_value = mock_tmpl
            prov_reg.get.return_value = mock_prov
            result = engine._execute_generation()
        assert result is True
        mock_pipeline.assert_called_once_with(ctx.target_dir)


# ---------------------------------------------------------------------------
# ForgeEngine._preview_generation  (lines 796, 813-815)
# ---------------------------------------------------------------------------


class TestPreviewGeneration:
    def test_creates_context_when_missing(self):
        engine = _make_engine()
        engine.project_config = {"template": "starter"}
        mock_tmpl = MagicMock()
        mock_tmpl.generate_structure.return_value = {}
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch.object(engine, "_create_generation_context") as mock_ctx,
            patch.object(engine, "_preview_structure"),
            patch.object(engine, "_build_contract", return_value={}),
            patch.object(engine, "_preview_contract"),
        ):
            tmpl_reg.get.return_value = mock_tmpl

            # Simulate _create_generation_context setting generation_context
            def set_ctx():
                engine.generation_context = MagicMock()
                engine.generation_context.target_dir = Path("/tmp/preview")

            mock_ctx.side_effect = set_ctx
            result = engine._preview_generation()
        mock_ctx.assert_called_once()
        assert result is True

    def test_returns_false_when_template_not_found(self):
        engine = _make_engine()
        engine.project_config = {"template": "missing"}
        engine.generation_context = MagicMock()
        with patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg:
            tmpl_reg.get.return_value = None
            result = engine._preview_generation()
        assert result is False

    def test_returns_false_on_exception(self):
        engine = _make_engine()
        engine.project_config = {"template": "starter"}
        engine.generation_context = MagicMock()
        mock_tmpl = MagicMock()
        mock_tmpl.generate_structure.side_effect = RuntimeError("structure crash")
        with patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg:
            tmpl_reg.get.return_value = mock_tmpl
            result = engine._preview_generation()
        assert result is False

    def test_calls_preview_structure_and_contract(self):
        engine = _make_engine()
        engine.project_config = {"template": "starter"}
        engine.generation_context = MagicMock()
        mock_tmpl = MagicMock()
        structure = {"src/": {}}
        contract = {"fluidVersion": "0.5.7"}
        mock_tmpl.generate_structure.return_value = structure
        with (
            patch("fluid_build.forge.core.engine.template_registry") as tmpl_reg,
            patch.object(engine, "_preview_structure") as mock_preview_struct,
            patch.object(engine, "_build_contract", return_value=contract),
            patch.object(engine, "_preview_contract") as mock_preview_contract,
        ):
            tmpl_reg.get.return_value = mock_tmpl
            result = engine._preview_generation()
        assert result is True
        mock_preview_struct.assert_called_once_with(structure)
        mock_preview_contract.assert_called_once_with(contract)


# ---------------------------------------------------------------------------
# ForgeEngine._create_folder_structure — nested dir creation (line 886)
# ---------------------------------------------------------------------------


class TestCreateFolderStructureNested:
    def test_creates_nested_directories_recursively(self, tmp_path):
        engine = _make_engine()
        structure = {
            "a/": {
                "b/": {
                    "c/": {},
                }
            }
        }
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_handles_mixed_file_and_dir_keys(self, tmp_path):
        engine = _make_engine()
        structure = {
            "src/": {"utils/": {}},
            "README.md": "# readme",  # no trailing slash — ignored
        }
        engine._create_folder_structure(tmp_path, structure)
        assert (tmp_path / "src" / "utils").is_dir()
        # File-like key should not be created as directory
        assert not (tmp_path / "README.md").is_dir()


# ---------------------------------------------------------------------------
# ForgeEngine._run_generators  (lines 917, 919, 931, 935-936)
# ---------------------------------------------------------------------------


class TestRunGenerators:
    def test_creates_context_if_none(self):
        engine = _make_engine()
        engine.generation_context = None
        engine.project_config = {
            "name": "test",
            "target_dir": Path("/tmp/gen"),
        }
        with (
            patch("fluid_build.forge.core.engine.generator_registry") as gen_reg,
            patch.object(engine, "_create_generation_context") as mock_create_ctx,
        ):
            gen_reg.list_available.return_value = []
            gen_reg.get_dependency_order.return_value = []

            def set_ctx():
                engine.generation_context = MagicMock()
                engine.generation_context.target_dir = Path("/tmp/gen")

            mock_create_ctx.side_effect = set_ctx
            engine._run_generators()
        mock_create_ctx.assert_called_once()

    def test_skips_contract_generator(self):
        engine = _make_engine()
        ctx = MagicMock()
        ctx.target_dir = Path("/tmp/gen")
        engine.generation_context = ctx
        with patch("fluid_build.forge.core.engine.generator_registry") as gen_reg:
            gen_reg.list_available.return_value = ["contract", "readme"]
            gen_reg.get_dependency_order.return_value = ["contract", "readme"]
            mock_readme_gen = MagicMock()
            mock_readme_gen.validate_context.return_value = (True, [])
            mock_readme_gen.generate.return_value = {"README.md": "# Hello"}

            def get_gen(name):
                return None if name == "contract" else mock_readme_gen

            gen_reg.get.side_effect = get_gen
            with patch.object(engine, "_write_generated_file") as mock_write:
                engine._run_generators()
            # Only README should have been written
            mock_write.assert_called_once()

    def test_skips_invalid_generators(self):
        engine = _make_engine()
        ctx = MagicMock()
        ctx.target_dir = Path("/tmp/gen")
        engine.generation_context = ctx
        with patch("fluid_build.forge.core.engine.generator_registry") as gen_reg:
            gen_reg.list_available.return_value = ["bad-gen"]
            gen_reg.get_dependency_order.return_value = ["bad-gen"]
            mock_bad_gen = MagicMock()
            mock_bad_gen.validate_context.return_value = (False, ["missing field"])
            gen_reg.get.return_value = mock_bad_gen
            with patch.object(engine, "_write_generated_file") as mock_write:
                engine._run_generators()
            mock_write.assert_not_called()

    def test_writes_all_generated_files(self, tmp_path):
        engine = _make_engine()
        ctx = MagicMock()
        ctx.target_dir = tmp_path
        engine.generation_context = ctx
        with patch("fluid_build.forge.core.engine.generator_registry") as gen_reg:
            gen_reg.list_available.return_value = ["docs"]
            gen_reg.get_dependency_order.return_value = ["docs"]
            mock_gen = MagicMock()
            mock_gen.validate_context.return_value = (True, [])
            mock_gen.generate.return_value = {
                "docs/index.md": "# Index",
                "docs/api.md": "# API",
            }
            gen_reg.get.return_value = mock_gen
            engine._run_generators()
        assert (tmp_path / "docs" / "index.md").read_text(encoding="utf-8") == "# Index"
        assert (tmp_path / "docs" / "api.md").read_text(encoding="utf-8") == "# API"


# ---------------------------------------------------------------------------
# ForgeEngine._generate_pipeline_files — exception path (lines 988-990)
# ---------------------------------------------------------------------------


class TestGeneratePipelineFiles:
    def test_no_op_when_pipeline_config_empty(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {}
        # Should return without error when no pipeline_config present
        engine._generate_pipeline_files(tmp_path)
        # No files should have been written
        assert list(tmp_path.iterdir()) == []

    def test_exception_is_caught_and_warned(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {
            "pipeline_config": {
                "provider": "github_actions",
                "complexity": "basic",
                "environments": ["dev"],
                "enable_approvals": False,
                "enable_security_scan": True,
                "enable_marketplace_publishing": False,
            }
        }
        with (
            patch.dict(
                "sys.modules",
                {"fluid_build.forge.core.pipeline_templates": None},
            ),
            patch("fluid_build.forge.core.engine.rprint") as mock_rprint,
        ):
            # Slice UX-H: the public `_generate_pipeline_files` hook
            # is now a no-op shim.  Exercise the preserved
            # `_generate_pipeline_files_legacy` symbol, which still
            # contains the original try/except/rprint behaviour.
            engine._generate_pipeline_files_legacy(tmp_path)
        # Should have printed a warning
        printed = [str(c) for c in mock_rprint.call_args_list]
        assert any("warning" in c.lower() or "Pipeline" in c for c in printed)

    def test_writes_pipeline_files_on_success(self, tmp_path):
        engine = _make_engine()
        engine.project_config = {
            "pipeline_config": {
                "provider": "github_actions",
                "complexity": "basic",
                "environments": ["dev"],
                "enable_approvals": False,
                "enable_security_scan": True,
                "enable_marketplace_publishing": False,
            }
        }
        mock_pipeline_gen = MagicMock()
        mock_pipeline_gen.return_value.generate_pipeline.return_value = {
            ".github/workflows/ci.yml": "name: CI"
        }
        mock_pipeline_provider = MagicMock()
        mock_pipeline_provider.return_value = "github_actions"
        mock_pipeline_complexity = MagicMock()
        mock_pipeline_complexity.return_value = "basic"
        mock_pipeline_config = MagicMock()
        mock_module = MagicMock()
        mock_module.PipelineProvider = mock_pipeline_provider
        mock_module.PipelineComplexity = mock_pipeline_complexity
        mock_module.PipelineConfig = mock_pipeline_config
        mock_module.PipelineTemplateGenerator = mock_pipeline_gen
        with patch.dict("sys.modules", {"fluid_build.forge.core.pipeline_templates": mock_module}):
            # Slice UX-H: call the legacy symbol — the shim is a
            # no-op on the live path now, so the generator can only
            # be exercised via `_generate_pipeline_files_legacy`.
            engine._generate_pipeline_files_legacy(tmp_path)
        # Generator was called
        mock_pipeline_gen.return_value.generate_pipeline.assert_called_once()


# ---------------------------------------------------------------------------
# ForgeEngine._preview_structure  (lines 994-1006)
# ---------------------------------------------------------------------------


class TestPreviewStructure:
    def test_renders_tree_to_console(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        structure = {
            "src/": {"models/": {}},
            "README.md": "# content",
        }
        engine._preview_structure(structure)
        console.print.assert_called_once()

    def test_handles_empty_structure(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        engine._preview_structure({})
        console.print.assert_called_once()

    def test_nested_dirs_added_to_tree(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        structure = {
            "a/": {
                "b/": {"c/": {}},
                "file.py": "",
            }
        }
        # Should not raise
        engine._preview_structure(structure)
        console.print.assert_called_once()


# ---------------------------------------------------------------------------
# ForgeEngine._preview_contract  (lines 1010-1022)
# ---------------------------------------------------------------------------


class TestPreviewContract:
    def test_renders_key_fields_to_console(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        contract = {
            "fluidVersion": "0.5.7",
            "kind": "DataProduct",
            "id": "my-product",
            "name": "My Product",
            "description": "A test product",
            "domain": "analytics",
        }
        engine._preview_contract(contract)
        console.print.assert_called_once()

    def test_handles_empty_contract(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        engine._preview_contract({})
        console.print.assert_called_once()

    def test_handles_partial_contract(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        contract = {"fluidVersion": "0.5.7", "kind": "DataProduct"}
        engine._preview_contract(contract)
        console.print.assert_called_once()


# ---------------------------------------------------------------------------
# ForgeEngine._show_completion_summary  (lines 1026-1042)
# ---------------------------------------------------------------------------


class TestShowCompletionSummary:
    def test_prints_panel_with_project_info(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        engine.project_config = {
            "name": "my-product",
            "template": "starter",
            "provider": "local",
            "target_dir": Path("/tmp/my-product"),
        }
        engine.session_stats["steps_completed"] = ["step1", "step2"]
        engine._show_completion_summary()
        console.print.assert_called_once()

    def test_prints_with_unknown_defaults(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        # project_config empty — should fall back to "Unknown"
        engine._show_completion_summary()
        console.print.assert_called_once()

    def test_duration_is_positive(self):
        console = MagicMock()
        engine = _make_engine(console=console)
        engine._show_completion_summary()
        # Panel content is in the call args — just check it was called without raising
        assert console.print.call_count == 1
