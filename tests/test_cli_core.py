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

"""Tests for fluid_build.cli.core."""

import time
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.core import (
    CLIContext,
    CLIMetrics,
    ContractLoadError,
    ContractNotFoundError,
    ContractValidationError,
    ExecutionError,
    FluidCLIError,
    PlanGenerationError,
    ProgressManager,
    ProviderError,
    ProviderNotFoundError,
    confirm_action,
    display_json_pretty,
    get_cache_dir,
    get_config_dir,
    get_runtime_dir,
    import_module_safe,
)

# ── FluidCLIError and subclasses ──────────────────────────────────────


class TestFluidCLIError:
    def test_basic_creation(self):
        err = FluidCLIError(1, "test_event", "something went wrong")
        assert err.exit_code == 1
        assert err.event == "test_event"
        assert err.message == "something went wrong"

    def test_auto_suggestions_for_known_events(self):
        err = FluidCLIError(1, "contract_not_found")
        assert len(err.suggestions) > 0
        assert any("contract file path" in s.lower() for s in err.suggestions)

    def test_custom_suggestions_not_overridden(self):
        custom = ["Try this instead"]
        err = FluidCLIError(1, "contract_not_found", suggestions=custom)
        assert err.suggestions == custom

    def test_unknown_event_no_suggestions(self):
        err = FluidCLIError(1, "some_unknown_event")
        assert err.suggestions == []

    def test_docs_url(self):
        err = FluidCLIError(1, "test", docs_url="https://example.com")
        assert err.docs_url == "https://example.com"

    def test_context(self):
        err = FluidCLIError(1, "test", context={"key": "value"})
        assert err.context == {"key": "value"}


class TestContractNotFoundError:
    def test_message_includes_path(self):
        err = ContractNotFoundError("/tmp/missing.yaml")
        assert "/tmp/missing.yaml" in err.message
        assert err.exit_code == 1
        assert err.event == "contract_not_found"


class TestContractLoadError:
    def test_message_includes_reason(self):
        err = ContractLoadError("/tmp/bad.yaml", "invalid YAML syntax")
        assert "invalid YAML syntax" in err.message
        assert err.context["path"] == "/tmp/bad.yaml"


class TestContractValidationError:
    def test_with_errors_list(self):
        err = ContractValidationError("validation failed", errors=["err1", "err2"])
        assert err.context["validation_errors"] == ["err1", "err2"]


class TestProviderNotFoundError:
    def test_message_includes_provider(self):
        err = ProviderNotFoundError("azure")
        assert "azure" in err.message


class TestProviderError:
    def test_includes_suggestions(self):
        err = ProviderError("gcp", "plan", "auth failed")
        assert "gcp" in err.message
        assert "plan" in err.message
        assert len(err.suggestions) > 0


class TestPlanGenerationError:
    def test_message(self):
        err = PlanGenerationError("no resources found")
        assert "no resources found" in err.message


class TestExecutionError:
    def test_message_and_suggestions(self):
        err = ExecutionError("create_table", "timeout")
        assert "create_table" in err.message
        assert "timeout" in err.message
        assert len(err.suggestions) > 0

    def test_format_for_user(self):
        err = ExecutionError("create_table", "timeout")
        err.docs_url = "https://docs.example.com"
        console = MagicMock()
        err.format_for_user(console)
        assert console.print.call_count >= 3  # error + suggestions + docs


# ── CLIMetrics ────────────────────────────────────────────────────────


class TestCLIMetrics:
    def test_duration_with_end_time(self):
        m = CLIMetrics(command="validate", start_time=100.0, end_time=105.5)
        assert m.duration == 5.5

    def test_duration_without_end_time(self):
        m = CLIMetrics(command="validate", start_time=time.time() - 1.0)
        assert m.duration >= 1.0

    def test_to_dict(self):
        m = CLIMetrics(
            command="plan",
            start_time=100.0,
            end_time=102.0,
            success=True,
            provider="gcp",
            contract_path="/tmp/c.yaml",
        )
        d = m.to_dict()
        assert d["command"] == "plan"
        assert d["duration"] == 2.0
        assert d["success"] is True
        assert d["provider"] == "gcp"

    def test_defaults(self):
        m = CLIMetrics(command="test", start_time=0.0)
        assert m.success is False
        assert m.provider is None


# ── CLIContext ────────────────────────────────────────────────────────


class TestCLIContext:
    def test_start_command(self):
        ctx = CLIContext(console=MagicMock())
        ctx.start_command("validate", provider="gcp")
        assert ctx.metrics is not None
        assert ctx.metrics.command == "validate"

    def test_finish_command(self):
        ctx = CLIContext(console=MagicMock())
        ctx.start_command("plan")
        ctx.finish_command(success=True)
        assert ctx.metrics.success is True
        assert ctx.metrics.end_time is not None

    def test_handle_fluid_error(self):
        ctx = CLIContext(console=MagicMock())
        ctx.start_command("apply")
        err = ExecutionError("create_table", "boom")
        code = ctx.handle_error(err)
        assert code == 1

    def test_handle_keyboard_interrupt(self):
        ctx = CLIContext(console=MagicMock())
        ctx.start_command("apply")
        code = ctx.handle_error(KeyboardInterrupt())
        assert code == 1

    def test_handle_unexpected_error(self):
        ctx = CLIContext(console=MagicMock())
        ctx.start_command("apply")
        code = ctx.handle_error(RuntimeError("unexpected"))
        assert code == 2


# ── Utility functions ─────────────────────────────────────────────────


class TestImportModuleSafe:
    def test_import_existing_module(self):
        result = import_module_safe("json")
        import json

        assert result is json

    def test_import_with_attr(self):
        result = import_module_safe("json", "dumps")
        import json

        assert result is json.dumps

    def test_import_missing_module(self):
        with pytest.raises(FluidCLIError) as exc_info:
            import_module_safe("nonexistent_module_xyz")
        assert exc_info.value.event == "module_import_failed"


class TestConfirmAction:
    def test_yes_response(self):
        console = MagicMock()
        console.input.return_value = "y"
        assert confirm_action("Proceed?", console=console) is True

    def test_no_response(self):
        console = MagicMock()
        console.input.return_value = "n"
        assert confirm_action("Proceed?", console=console) is False

    def test_empty_response_uses_default_false(self):
        console = MagicMock()
        console.input.return_value = ""
        assert confirm_action("Proceed?", default=False, console=console) is False

    def test_empty_response_uses_default_true(self):
        console = MagicMock()
        console.input.return_value = ""
        assert confirm_action("Proceed?", default=True, console=console) is True

    def test_keyboard_interrupt(self):
        console = MagicMock()
        console.input.side_effect = KeyboardInterrupt()
        assert confirm_action("Proceed?", console=console) is False


class TestDisplayJsonPretty:
    def test_displays_json(self):
        console = MagicMock()
        display_json_pretty({"key": "value"}, console=console)
        console.print.assert_called_once()


class TestConfigDirs:
    def test_get_config_dir(self, tmp_path):
        with patch("fluid_build.cli.core.Path.home", return_value=tmp_path):
            result = get_config_dir()
            assert result == tmp_path / ".fluid"
            assert result.exists()

    def test_get_cache_dir(self, tmp_path):
        with patch("fluid_build.cli.core.Path.home", return_value=tmp_path):
            result = get_cache_dir()
            assert result == tmp_path / ".fluid" / "cache"
            assert result.exists()


class TestProgressManager:
    def test_context_manager(self):
        console = MagicMock()
        pm = ProgressManager(console=console)
        with pm as progress:
            assert progress is not None
