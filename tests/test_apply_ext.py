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

"""Extended tests for apply.py: run() paths, display helpers, reports, notifications.

Note: basic _actions_from_source and register tests are in test_apply.py.
This file covers run() integration paths, display helpers, report generation,
notification dispatch, and metric export.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

import fluid_build.cli.apply as _apply_mod
from fluid_build.cli.apply import COMMAND, _actions_from_source

LOG = logging.getLogger("test_apply_ext")


# ---------------------------------------------------------------------------
# run() with simple mode
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for run() paths not covered in test_apply.py::TestRunSimpleMode.

    Basic dry_run, no_actions, config_override, and provider detection are
    tested in test_apply.py. This class covers execute success/failure paths.
    """

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.hooks.run_pre_apply", side_effect=lambda p, a, l: a)
    @patch("fluid_build.cli.hooks.run_post_apply")
    @patch("fluid_build.cli.hooks.run_on_error")
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("os.isatty", return_value=False)
    def test_run_simple_execute_success(
        self,
        _mock_isatty,
        _mock_metric,
        _mock_success,
        _mock_start,
        _mock_on_error,
        _mock_post,
        _mock_pre,
        mock_actions,
        mock_build,
        mock_load,
    ):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"id": "test"}
        mock_provider = MagicMock()
        mock_provider.apply.return_value = {"failed": 0, "status": "success"}
        mock_build.return_value = mock_provider
        mock_actions.return_value = [{"op": "ensure_dataset"}]

        args = MagicMock()
        args.contract = "test.yaml"
        args.env = None
        args.dry_run = False
        args.yes = True
        args.timeout = 120
        args.parallel_phases = False
        args.rollback_strategy = "phase_complete"
        args.config_override = None
        args.provider_config = None
        args.verbose = False
        args.debug = False
        args.report = "report.html"

        result = run(args, LOG)
        assert result == 0
        mock_provider.apply.assert_called_once()

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.hooks.run_pre_apply", side_effect=lambda p, a, l: a)
    @patch("fluid_build.cli.hooks.run_post_apply")
    @patch("fluid_build.cli.hooks.run_on_error")
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_failure")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("os.isatty", return_value=False)
    def test_run_simple_execute_failure(
        self,
        _mock_isatty,
        _mock_metric,
        _mock_failure,
        _mock_start,
        _mock_on_error,
        _mock_post,
        _mock_pre,
        mock_actions,
        mock_build,
        mock_load,
    ):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"id": "test"}
        mock_provider = MagicMock()
        mock_provider.apply.return_value = {"failed": 1, "status": "error"}
        mock_build.return_value = mock_provider
        mock_actions.return_value = [{"op": "ensure_dataset"}]

        args = MagicMock()
        args.contract = "test.yaml"
        args.env = None
        args.dry_run = False
        args.yes = True
        args.timeout = 120
        args.parallel_phases = False
        args.rollback_strategy = "phase_complete"
        args.config_override = None
        args.provider_config = None
        args.verbose = False
        args.debug = False
        args.report = "report.html"

        result = run(args, LOG)
        assert result == 1
        mock_provider.apply.assert_called_once()


# ---------------------------------------------------------------------------
# Missing lines 67-68 – JSON plan loading path in _actions_from_source
# ---------------------------------------------------------------------------


class TestActionsFromSourceJsonPath:
    def test_json_returns_empty_when_no_actions_key(self, tmp_path):
        """Lines 67-68: JSON plan with empty dict returns []."""
        plan = tmp_path / "plan.json"
        plan.write_text("{}")

        actions = _actions_from_source(str(plan), None, MagicMock(), LOG)
        assert actions == []

    def test_json_returns_actions_list(self, tmp_path):
        """Lines 67-68: JSON plan with actions list is returned directly."""
        plan = tmp_path / "plan.json"
        plan.write_text('{"actions": [{"op": "A"}, {"op": "B"}]}')

        actions = _actions_from_source(str(plan), None, MagicMock(), LOG)
        assert [a["op"] for a in actions] == ["A", "B"]


# ---------------------------------------------------------------------------
# Missing lines 295-310 – run() JSON plan loading branch
# ---------------------------------------------------------------------------


class TestRunJsonPlanLoading:
    """Lines 295-310: run() loading a .json execution plan."""

    def _make_args(self, contract="plan.json", **kwargs):
        from unittest.mock import MagicMock

        args = MagicMock()
        args.contract = contract
        args.env = None
        args.dry_run = kwargs.get("dry_run", True)
        args.yes = True
        args.timeout = 120
        args.parallel_phases = False
        args.rollback_strategy = "phase_complete"
        args.config_override = None
        args.provider_config = None
        args.verbose = False
        args.debug = False
        args.report = None
        args.workspace_dir = MagicMock()
        args.state_file = None
        args.notify = None
        args.metrics_export = "none"
        args.report_format = "html"
        return args

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    def test_json_plan_dry_run(self, _mock_metric, _mock_success, _mock_start, tmp_path):
        """run() with a .json contract loads ExecutionPlan and returns 0 in dry-run."""
        from fluid_build.cli.apply import run
        from fluid_build.cli.orchestration import ExecutionPlan

        plan = tmp_path / "plan.json"
        plan.write_text('{"contract": {"id": "t"}, "plan": {}}')

        args = self._make_args(contract=str(plan), dry_run=True)

        with (
            patch(
                "fluid_build.cli.apply.read_json",
                return_value={"contract": {"id": "t"}, "plan": {}},
            ),
            patch("fluid_build.cli.apply.ExecutionPlan", return_value=MagicMock(phases=[])),
            patch("fluid_build.cli.apply._display_execution_plan"),
            patch("fluid_build.cli.apply._display_dry_run_summary"),
        ):
            result = run(args, LOG)
        assert result == 0


# ---------------------------------------------------------------------------
# Missing lines 411-498 – simple mode provider detection
# ---------------------------------------------------------------------------


class TestRunSimpleModeProviderDetection:
    """Lines 411-498: simple-mode provider/project/region detection from contract."""

    def _make_args(self, **kwargs):
        args = MagicMock()
        args.contract = "test.yaml"
        args.env = None
        args.dry_run = kwargs.get("dry_run", True)
        args.yes = True
        args.timeout = 120
        args.parallel_phases = False
        args.rollback_strategy = "phase_complete"
        args.config_override = None
        args.provider_config = None
        args.verbose = False
        args.debug = False
        args.report = None
        return args

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.apply._actions_from_source", return_value=[{"op": "ensure_dataset"}])
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_gcp_provider_detected(
        self, mock_load, mock_bp, _mock_actions, _mock_metric, _mock_success, _mock_start
    ):
        """Lines 411-450: GCP provider detected from exposes.binding.platform."""
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "id": "proj-id",
            "exposes": [
                {
                    "binding": {
                        "platform": "gcp",
                        "location": {"project": "my-project"},
                    }
                }
            ],
        }
        mock_bp.return_value = MagicMock()

        result = run(self._make_args(dry_run=True), LOG)
        assert result == 0
        # GCP detected – build_provider called with gcp
        call_args = mock_bp.call_args
        assert call_args[0][0] == "gcp"

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.apply._actions_from_source", return_value=[{"op": "ensure_dataset"}])
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_builds_fallback_provider_detection(
        self, mock_load, mock_bp, _mock_actions, _mock_metric, _mock_success, _mock_start
    ):
        """Lines 419-424: provider detected from builds[].execution.runtime.platform."""
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "id": "proj",
            "builds": [{"execution": {"runtime": {"platform": "spark"}}}],
        }
        mock_bp.return_value = MagicMock()

        result = run(self._make_args(dry_run=True), LOG)
        assert result == 0
        call_args = mock_bp.call_args
        assert call_args[0][0] == "spark"

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.apply._actions_from_source", return_value=[])
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_no_actions_returns_0(
        self, mock_load, mock_bp, _mock_actions, _mock_metric, _mock_success, _mock_start
    ):
        """Lines 455-457: no actions returns 0 early."""
        from fluid_build.cli.apply import run

        mock_load.return_value = {"id": "x"}
        mock_bp.return_value = MagicMock()

        result = run(self._make_args(dry_run=False), LOG)
        assert result == 0

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.apply._actions_from_source", return_value=[{"op": "sf.table.ensure"}])
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_snowflake_does_not_use_contract_id_as_project(
        self, mock_load, mock_bp, _mock_actions, _mock_metric, _mock_success, _mock_start
    ):
        """Snowflake should resolve account/database from binding and env, not contract id."""
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "id": "snowflake.contract.id",
            "exposes": [
                {
                    "binding": {
                        "platform": "snowflake",
                        "location": {
                            "account": "acme-account",
                            "database": "ANALYTICS",
                            "schema": "CURATED",
                            "table": "CUSTOMERS",
                        },
                    }
                }
            ],
        }
        mock_bp.return_value = MagicMock()

        result = run(self._make_args(dry_run=True), LOG)
        assert result == 0
        call_args = mock_bp.call_args
        assert call_args[0][0] == "snowflake"
        assert call_args[0][1] is None


# ---------------------------------------------------------------------------
# Missing lines 560-610 – dry_run display in simple mode
# ---------------------------------------------------------------------------


class TestRunSimpleModeDryRunDisplay:
    """Lines 560-610: dry-run table/logging output in simple mode."""

    def _make_args(self, **kwargs):
        args = MagicMock()
        args.contract = "test.yaml"
        args.env = None
        args.dry_run = True
        args.yes = True
        args.timeout = 120
        args.parallel_phases = False
        args.rollback_strategy = "phase_complete"
        args.config_override = None
        args.provider_config = None
        args.verbose = False
        args.debug = False
        args.report = None
        return args

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_dry_run_plain_logging(
        self, mock_load, mock_bp, mock_actions, _mock_metric, _mock_success, _mock_start
    ):
        """Lines 514-518: plain-text dry-run logs each action."""
        from fluid_build.cli.apply import run

        mock_load.return_value = {"id": "x"}
        mock_bp.return_value = MagicMock()
        mock_actions.return_value = [
            {"op": "ensure_dataset", "metadata": {}},
            {"op": "ensure_table", "metadata": {"schema": "s"}},
        ]

        result = run(self._make_args(), LOG)
        assert result == 0

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", True)
    @patch("fluid_build.cli.apply.Console")
    @patch("fluid_build.cli.apply.Panel")
    @patch("fluid_build.cli.apply.Table")
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_dry_run_rich_table(
        self,
        mock_load,
        mock_bp,
        mock_actions,
        _mock_metric,
        _mock_success,
        _mock_start,
        _mock_table,
        _mock_panel,
        mock_console_cls,
    ):
        """Lines 500-518: rich dry-run shows Panel and Table."""
        from fluid_build.cli.apply import run

        mock_console = MagicMock()
        mock_console_cls.return_value = mock_console
        mock_load.return_value = {"id": "x"}
        mock_bp.return_value = MagicMock()
        mock_actions.return_value = [{"op": "create_table", "metadata": {}}]

        result = run(self._make_args(), LOG)
        assert result == 0
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# Missing lines 676-677, 700-769 – report generation + complex mode
# ---------------------------------------------------------------------------


class TestRunReportGeneration:
    """Lines 618-677: HTML/JSON report generation in simple mode."""

    def _make_args(self, report_format="html", report="runtime/test_report.html"):
        args = MagicMock()
        args.contract = "test.yaml"
        args.env = None
        args.dry_run = False
        args.yes = True
        args.timeout = 120
        args.parallel_phases = False
        args.rollback_strategy = "phase_complete"
        args.config_override = None
        args.provider_config = None
        args.verbose = False
        args.debug = False
        args.report = report
        args.report_format = report_format
        return args

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_operation_failure")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.hooks.run_pre_apply", side_effect=lambda p, a, l: a)
    @patch("fluid_build.cli.hooks.run_post_apply")
    @patch("fluid_build.cli.hooks.run_on_error")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_html_report_generated(
        self,
        mock_load,
        mock_bp,
        mock_actions,
        _mock_on_error,
        _mock_post,
        _mock_pre,
        _mock_metric,
        _mock_failure,
        _mock_success,
        _mock_start,
        tmp_path,
    ):
        """Lines 628-656: HTML report is written to disk after successful apply."""
        from fluid_build.cli.apply import run

        report_path = tmp_path / "report.html"
        args = self._make_args(report_format="html", report=str(report_path))

        mock_load.return_value = {"id": "test", "name": "MyProduct"}
        provider = MagicMock()
        provider.apply.return_value = {"failed": 0, "applied": 1, "status": "success"}
        mock_bp.return_value = provider
        mock_actions.return_value = [{"op": "ensure_dataset"}]

        result = run(args, LOG)
        assert result == 0
        assert report_path.exists()
        content = report_path.read_text()
        assert "FLUID Apply Report" in content

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_operation_failure")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.hooks.run_pre_apply", side_effect=lambda p, a, l: a)
    @patch("fluid_build.cli.hooks.run_post_apply")
    @patch("fluid_build.cli.hooks.run_on_error")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_json_report_generated(
        self,
        mock_load,
        mock_bp,
        mock_actions,
        _mock_on_error,
        _mock_post,
        _mock_pre,
        _mock_metric,
        _mock_failure,
        _mock_success,
        _mock_start,
        tmp_path,
    ):
        """Lines 657-673: JSON report is written with execution metadata."""
        import json

        from fluid_build.cli.apply import run

        report_path = tmp_path / "report.json"
        args = self._make_args(report_format="json", report=str(report_path))

        mock_load.return_value = {"id": "test", "name": "MyProduct"}
        provider = MagicMock()
        provider.apply.return_value = {"failed": 0, "applied": 2, "status": "success"}
        mock_bp.return_value = provider
        mock_actions.return_value = [{"op": "ensure_dataset"}]

        result = run(args, LOG)
        assert result == 0
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert data["success"] is True
        assert data["applied"] == 2

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.apply.log_operation_start")
    @patch("fluid_build.cli.apply.log_operation_success")
    @patch("fluid_build.cli.apply.log_operation_failure")
    @patch("fluid_build.cli.apply.log_metric")
    @patch("fluid_build.cli.hooks.run_pre_apply", side_effect=lambda p, a, l: a)
    @patch("fluid_build.cli.hooks.run_post_apply")
    @patch("fluid_build.cli.hooks.run_on_error")
    @patch("fluid_build.cli.apply._actions_from_source")
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_failure_result_returns_1(
        self,
        mock_load,
        mock_bp,
        mock_actions,
        _mock_on_error,
        _mock_post,
        _mock_pre,
        _mock_metric,
        _mock_failure,
        _mock_success,
        _mock_start,
    ):
        """Lines 688-695: failed apply returns exit code 1."""
        from fluid_build.cli.apply import run

        args = self._make_args(report=None)
        args.report = None

        mock_load.return_value = {"id": "test"}
        provider = MagicMock()
        provider.apply.return_value = {"failed": 1, "status": "error", "error": "boom"}
        mock_bp.return_value = provider
        mock_actions.return_value = [{"op": "ensure_dataset"}]

        result = run(args, LOG)
        assert result == 1


# ---------------------------------------------------------------------------
# Missing lines 779-905 – helper display functions
# ---------------------------------------------------------------------------


class TestDisplayHelpers:
    """Lines 832-905: _display_execution_plan, _confirm_execution, _display_dry_run_summary."""

    def _make_plan(self, phases=None):
        from fluid_build.cli.orchestration import ExecutionPlan

        plan = MagicMock(spec=ExecutionPlan)
        if phases is None:
            phase = MagicMock()
            phase.actions = [MagicMock(), MagicMock()]
            phase.parallel_execution = True
            phase.rollback_strategy = MagicMock(value="phase_complete")
            phase.phase = MagicMock(value="infrastructure")
            phases = [phase]
        plan.phases = phases
        plan.global_timeout_minutes = 30
        return plan

    def test_display_execution_plan_no_console(self):
        """Lines 854-855: plain logger path in _display_execution_plan."""
        from fluid_build.cli.apply import _display_execution_plan

        plan = self._make_plan()
        _display_execution_plan(plan, None, LOG)  # Should not raise

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", True)
    def test_display_execution_plan_with_console(self):
        """Lines 836-853: rich console path in _display_execution_plan."""
        from fluid_build.cli.apply import _display_execution_plan

        console = MagicMock()
        plan = self._make_plan()
        _display_execution_plan(plan, console, LOG)
        console.print.assert_called()

    def test_confirm_execution_no_console_yes(self):
        """Lines 867-871: _confirm_execution returns True when user types 'y'."""
        from fluid_build.cli.apply import _confirm_execution

        plan = self._make_plan()
        with patch("builtins.input", return_value="y"), patch("fluid_build.cli.apply.cprint"):
            result = _confirm_execution(plan, None)
        assert result is True

    def test_confirm_execution_no_console_no(self):
        """Lines 867-871: _confirm_execution returns False when user presses Enter."""
        from fluid_build.cli.apply import _confirm_execution

        plan = self._make_plan()
        with patch("builtins.input", return_value=""), patch("fluid_build.cli.apply.cprint"):
            result = _confirm_execution(plan, None)
        assert result is False

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", True)
    def test_confirm_execution_with_console_yes(self):
        """Lines 862-871: rich console path prompts and returns True."""
        from fluid_build.cli.apply import _confirm_execution

        console = MagicMock()
        plan = self._make_plan()
        with patch("builtins.input", return_value="yes"):
            result = _confirm_execution(plan, console)
        assert result is True

    def test_display_dry_run_summary_no_console(self):
        """Lines 882-887: plain logger path in _display_dry_run_summary."""
        from fluid_build.cli.apply import _display_dry_run_summary

        plan = self._make_plan()
        _display_dry_run_summary(plan, None, LOG)  # Should not raise

    @patch("fluid_build.cli.apply.RICH_AVAILABLE", True)
    def test_display_dry_run_summary_with_console(self):
        """Lines 877-881: rich console path in _display_dry_run_summary."""
        from fluid_build.cli.apply import _display_dry_run_summary

        console = MagicMock()
        plan = self._make_plan()
        _display_dry_run_summary(plan, console, LOG)
        console.print.assert_called()


# ---------------------------------------------------------------------------
# Missing lines 1037-1072 – _send_notifications and _export_metrics
# ---------------------------------------------------------------------------


class TestNotificationsAndMetrics:
    """Lines 1033-1072: _send_notifications and _export_metrics."""

    def _make_result(self, success=True):
        return {
            "success": success,
            "execution_id": "test-exec-1",
            "metrics": {
                "total_actions": 5,
                "successful_actions": 5,
                "failed_actions": 0,
                "total_duration_seconds": 12.5,
            },
        }

    def test_send_notifications_slack(self):
        """Lines 1045-1047: slack notification path logs correctly."""
        from fluid_build.cli.apply import _send_notifications

        _send_notifications(self._make_result(), "slack:data-team", LOG)

    def test_send_notifications_email(self):
        """Lines 1048-1050: email notification path logs correctly."""
        from fluid_build.cli.apply import _send_notifications

        _send_notifications(self._make_result(), "email:dev@example.com", LOG)

    def test_send_notifications_invalid_format(self):
        """Lines 1052-1053: invalid format raises no exception (caught internally)."""
        from fluid_build.cli.apply import _send_notifications

        _send_notifications(self._make_result(), "no-colon-here", LOG)

    def test_export_metrics_prometheus(self):
        """Lines 1061-1062: prometheus export path."""
        from fluid_build.cli.apply import _export_metrics

        _export_metrics(self._make_result(), "prometheus", LOG)

    def test_export_metrics_datadog(self):
        """Lines 1063-1065: datadog export path."""
        from fluid_build.cli.apply import _export_metrics

        _export_metrics(self._make_result(), "datadog", LOG)

    def test_export_metrics_cloudwatch(self):
        """Lines 1066-1068: cloudwatch export path."""
        from fluid_build.cli.apply import _export_metrics

        _export_metrics(self._make_result(), "cloudwatch", LOG)


# ---------------------------------------------------------------------------
# Missing lines – _generate_final_report (html / json / markdown formats)
# ---------------------------------------------------------------------------


class TestGenerateFinalReport:
    """Lines 890-1030: _generate_final_report with html, json, markdown outputs."""

    def _make_context(self, tmp_path):
        from fluid_build.cli.orchestration import ExecutionContext

        ctx = MagicMock(spec=ExecutionContext)
        ctx.execution_id = "exec-001"
        ctx.workspace_dir = tmp_path
        plan = MagicMock()
        plan.contract_path = str(tmp_path / "c.yaml")
        plan.environment = "test"
        ctx.plan = plan
        return ctx

    def _make_execution_result(self, success=True):
        return {
            "success": success,
            "metrics": {
                "total_actions": 3,
                "successful_actions": 3,
                "failed_actions": 0,
                "skipped_actions": 0,
                "total_duration_seconds": 5.5,
            },
            "phases": [
                {"phase": "infrastructure", "status": "success", "action_count": 2, "duration": 2.0}
            ],
        }

    def test_html_report_written(self, tmp_path):
        """Lines 898-899: HTML report branch writes file."""
        from fluid_build.cli.apply import _generate_final_report

        report_path = tmp_path / "report.html"
        args = MagicMock()
        args.report = str(report_path)
        args.report_format = "html"
        ctx = self._make_context(tmp_path)

        _generate_final_report(self._make_execution_result(), args, ctx, LOG)
        assert report_path.exists()
        assert "FLUID" in report_path.read_text()

    def test_json_report_written(self, tmp_path):
        """Lines 900-901: JSON report branch writes valid JSON."""
        import json

        from fluid_build.cli.apply import _generate_final_report

        report_path = tmp_path / "report.json"
        args = MagicMock()
        args.report = str(report_path)
        args.report_format = "json"
        ctx = self._make_context(tmp_path)

        _generate_final_report(self._make_execution_result(), args, ctx, LOG)
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert "execution_id" in data

    def test_markdown_report_written(self, tmp_path):
        """Lines 902-903: Markdown report branch writes file."""
        from fluid_build.cli.apply import _generate_final_report

        report_path = tmp_path / "report.md"
        args = MagicMock()
        args.report = str(report_path)
        args.report_format = "markdown"
        ctx = self._make_context(tmp_path)

        _generate_final_report(self._make_execution_result(), args, ctx, LOG)
        assert report_path.exists()
        assert "FLUID" in report_path.read_text()

    def test_report_generation_exception_is_swallowed(self, tmp_path):
        """Lines 906-907: exception in report generation is caught and warned."""
        from fluid_build.cli.apply import _generate_final_report

        args = MagicMock()
        args.report = "/nonexistent/path/that/cannot/be/created/report.html"
        args.report_format = "html"
        ctx = self._make_context(tmp_path)
        ctx.plan.contract_path = "c.yaml"

        # Should not raise
        with patch("fluid_build.cli.apply.Path") as mock_path_cls:
            mock_path_instance = MagicMock()
            mock_path_instance.parent.mkdir.side_effect = PermissionError("no access")
            mock_path_cls.return_value = mock_path_instance
            _generate_final_report(self._make_execution_result(), args, ctx, LOG)
