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

"""Branch coverage tests for apply.py and orchestration.py."""

import argparse
import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---- Orchestration enums and dataclasses ----


class TestOrchestrationEnums:
    def test_execution_phase_values(self):
        from fluid_build.cli.orchestration import ExecutionPhase

        phases = list(ExecutionPhase)
        assert len(phases) >= 3

    def test_action_status_values(self):
        from fluid_build.cli.orchestration import ActionStatus

        statuses = list(ActionStatus)
        assert len(statuses) >= 3

    def test_rollback_strategy_values(self):
        from fluid_build.cli.orchestration import RollbackStrategy

        strategies = list(RollbackStrategy)
        assert len(strategies) >= 2

    def test_rollback_strategy_members(self):
        from fluid_build.cli.orchestration import RollbackStrategy

        vals = [s.value for s in RollbackStrategy]
        assert len(vals) >= 2


class TestExecutionAction:
    def test_create(self):
        from fluid_build.cli.orchestration import ActionStatus, ExecutionAction, ExecutionPhase

        action = ExecutionAction(
            id="act-1",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="ensure_table",
            description="Create table",
        )
        assert action.id == "act-1"
        assert action.status == ActionStatus.PENDING

    def test_with_optional_fields(self):
        from fluid_build.cli.orchestration import ExecutionAction, ExecutionPhase

        action = ExecutionAction(
            id="act-2",
            phase=ExecutionPhase.INFRASTRUCTURE,
            provider="aws",
            operation="create_bucket",
            description="Create S3 bucket",
            dependencies=["act-1"],
            timeout_seconds=600,
            retry_count=5,
            rollback_operation="delete_bucket",
            metadata={"region": "us-east-1"},
        )
        assert action.timeout_seconds == 600
        assert action.retry_count == 5


class TestExecutionMetrics:
    def test_create_default(self):
        from fluid_build.cli.orchestration import ExecutionMetrics

        metrics = ExecutionMetrics()
        assert metrics.total_actions == 0
        assert metrics.successful_actions == 0
        assert metrics.failed_actions == 0

    def test_create_with_values(self):
        from fluid_build.cli.orchestration import ExecutionMetrics

        metrics = ExecutionMetrics(total_actions=10, successful_actions=8, failed_actions=2)
        assert metrics.total_actions == 10


class TestPhaseExecution:
    def test_create(self):
        from fluid_build.cli.orchestration import ActionStatus, ExecutionPhase, PhaseExecution

        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[])
        assert phase.status == ActionStatus.PENDING

    def test_with_parallel(self):
        from fluid_build.cli.orchestration import ExecutionPhase, PhaseExecution, RollbackStrategy

        phase = PhaseExecution(
            phase=ExecutionPhase.INFRASTRUCTURE,
            actions=[],
            parallel_execution=True,
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )
        assert phase.parallel_execution is True


class TestExecutionPlan:
    def test_create(self):
        from fluid_build.cli.orchestration import ExecutionPlan

        plan = ExecutionPlan(contract_path="test.yaml", environment="dev", phases=[])
        assert plan.phases == []
        assert plan.global_timeout_minutes == 60

    def test_with_options(self):
        from fluid_build.cli.orchestration import ExecutionPlan, RollbackStrategy

        plan = ExecutionPlan(
            contract_path="test.yaml",
            environment="staging",
            phases=[],
            dry_run=True,
            parallel_phases=True,
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )
        assert plan.dry_run is True


class TestExecutionContext:
    def test_create(self, tmp_path):
        from fluid_build.cli.orchestration import ExecutionContext, ExecutionPlan

        plan = ExecutionPlan(contract_path="test.yaml", environment="dev", phases=[])
        ctx = ExecutionContext(execution_id="test-123", contract={"name": "test"}, plan=plan)
        assert ctx.execution_id == "test-123"


# ---- FluidPlanGenerator ----


class TestFluidPlanGenerator:
    def test_init(self):
        from fluid_build.cli.orchestration import FluidPlanGenerator

        gen = FluidPlanGenerator(contract={"name": "test", "version": "1.0"}, environment="dev")
        assert gen.environment == "dev"

    def test_generate_execution_plan(self):
        from fluid_build.cli.orchestration import FluidPlanGenerator

        gen = FluidPlanGenerator(
            contract={"name": "test", "version": "1.0", "schema": {"fields": []}}, environment="dev"
        )
        plan = gen.generate_execution_plan("test.yaml")
        assert plan is not None
        assert hasattr(plan, "phases")

    def test_generate_plan_with_provider(self):
        from fluid_build.cli.orchestration import FluidPlanGenerator

        gen = FluidPlanGenerator(
            contract={
                "name": "test",
                "version": "1.0",
                "schema": {"fields": [{"name": "id", "type": "integer"}]},
                "exposes": [{"binding": {"platform": "gcp"}}],
            },
            environment="staging",
        )
        plan = gen.generate_execution_plan("test.yaml")
        assert plan is not None

    def test_detected_providers(self):
        from fluid_build.cli.orchestration import FluidPlanGenerator

        gen = FluidPlanGenerator(
            contract={
                "name": "test",
                "version": "1.0",
                "exposes": [{"binding": {"platform": "aws"}}],
                "builds": [{"execution": {"runtime": {"platform": "gcp"}}}],
            },
            environment="prod",
        )
        plan = gen.generate_execution_plan("test.yaml")
        assert plan is not None


# ---- FluidOrchestrationEngine ----


class TestFluidOrchestrationEngine:
    def test_init(self):
        from fluid_build.cli.orchestration import (
            ExecutionContext,
            ExecutionPlan,
            FluidOrchestrationEngine,
        )

        plan = ExecutionPlan(contract_path="test.yaml", environment="dev", phases=[])
        ctx = ExecutionContext(
            execution_id="test-123",
            contract={"name": "test"},
            plan=plan,
            logger=logging.getLogger("test"),
        )
        engine = FluidOrchestrationEngine(ctx)
        assert engine is not None

    def test_execute_empty_plan(self):
        from fluid_build.cli.orchestration import (
            ExecutionContext,
            ExecutionPlan,
            FluidOrchestrationEngine,
        )

        plan = ExecutionPlan(contract_path="test.yaml", environment="dev", phases=[])
        ctx = ExecutionContext(
            execution_id="test-123",
            contract={"name": "test"},
            plan=plan,
            logger=logging.getLogger("test"),
        )
        engine = FluidOrchestrationEngine(ctx)
        result = asyncio.run(engine.execute_plan())
        assert result is not None
        assert "success" in result or "execution_id" in result


# ---- Apply CLI ----


class TestApplyRegister:
    def test_register(self):
        from fluid_build.cli.apply import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

    def test_register_has_args(self):
        from fluid_build.cli.apply import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        # Parse with apply subcommand
        args = parser.parse_args(["apply", "test.yaml", "--dry-run", "--yes"])
        assert args.dry_run is True


class TestActionsFromSource:
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_json_source(self, _mock_load):
        from fluid_build.cli.apply import _actions_from_source

        with patch("fluid_build.cli.apply.read_json", return_value={"actions": [{"op": "test"}]}):
            actions = _actions_from_source(
                "plan.json", None, MagicMock(), logging.getLogger("test")
            )
            assert actions == [{"op": "test"}]

    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_provider_plan_method(self, mock_load):
        from fluid_build.cli.apply import _actions_from_source

        mock_load.return_value = {"name": "test"}
        provider = MagicMock()
        provider.plan.return_value = [{"op": "create"}, {"op": "insert"}]
        actions = _actions_from_source("contract.yaml", "dev", provider, logging.getLogger("test"))
        assert len(actions) == 2

    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_provider_plan_fails_fallback(self, mock_load):
        from fluid_build.cli.apply import _actions_from_source

        mock_load.return_value = {"name": "test"}
        provider = MagicMock()
        provider.plan.side_effect = Exception("plan failed")
        actions = _actions_from_source("contract.yaml", "dev", provider, logging.getLogger("test"))
        # Falls back to action parser or final fallback
        assert actions is not None

    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_no_plan_method(self, mock_load):
        from fluid_build.cli.apply import _actions_from_source

        mock_load.return_value = {"name": "test"}
        provider = MagicMock(spec=[])  # no plan method
        actions = _actions_from_source("contract.yaml", "dev", provider, logging.getLogger("test"))
        assert actions is not None


def _make_simple_args(**overrides):
    """Helper to create args namespace for simple mode tests."""
    defaults = dict(
        contract="contract.yaml",
        env="dev",
        dry_run=False,
        yes=True,
        verbose=False,
        debug=False,
        report=None,
        report_format="html",
        rollback_strategy="none",
        parallel_phases=False,
        timeout=60,
        metrics_export="none",
        notify=None,
        config_override=None,
        provider_config=None,
        workspace_dir=Path("."),
        state_file=None,
        keep_temp_files=False,
        profile=False,
        max_workers=4,
        require_approval=False,
        backup_state=False,
        validate_dependencies=False,
    )
    defaults.update(overrides)
    args = argparse.Namespace(**defaults)
    return args


class TestApplyRunSimple:
    """Tests for simple mode (no complex config keys)."""

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_run_dry_run_simple(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_build.return_value = mock_provider
        args = _make_simple_args(dry_run=True)
        result = run(args, logging.getLogger("test"))
        assert result == 0

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_run_apply_success(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        args = _make_simple_args()
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                result = run(args, logging.getLogger("test"))
                assert result == 0

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_run_apply_failure(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 1, "applied": 0, "error": "failed"}
        mock_build.return_value = mock_provider
        args = _make_simple_args()
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                result = run(args, logging.getLogger("test"))
                assert result == 1

    @patch("fluid_build.cli.apply._actions_from_source", return_value=[])
    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_run_no_actions(self, mock_load, mock_build, _mock_actions):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_build.return_value = MagicMock()
        args = _make_simple_args()
        result = run(args, logging.getLogger("test"))
        assert result == 0

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_detect_aws_provider(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "name": "test",
            "version": "1.0",
            "exposes": [
                {
                    "binding": {
                        "platform": "aws",
                        "location": {"project": "123456", "region": "us-east-1"},
                    }
                }
            ],
        }
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        args = _make_simple_args()
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                run(args, logging.getLogger("test"))
                # Check aws was detected
                mock_build.assert_called_once()
                call_args = mock_build.call_args
                assert call_args[0][0] == "aws"

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_detect_gcp_provider(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "name": "test",
            "version": "1.0",
            "exposes": [{"binding": {"platform": "gcp", "location": {"project": "my-project"}}}],
        }
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        args = _make_simple_args()
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                run(args, logging.getLogger("test"))
                call_args = mock_build.call_args
                assert call_args[0][0] == "gcp"

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_detect_provider_from_builds(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "name": "test",
            "version": "1.0",
            "builds": [{"execution": {"runtime": {"platform": "snowflake"}}}],
        }
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        args = _make_simple_args()
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                run(args, logging.getLogger("test"))
                call_args = mock_build.call_args
                assert call_args[0][0] == "snowflake"

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_config_override(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        args = _make_simple_args(config_override='{"extra": "val"}')
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                result = run(args, logging.getLogger("test"))
                assert result == 0

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_report_json(self, mock_load, mock_build, tmp_path):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        report_path = tmp_path / "report.json"
        args = _make_simple_args(report=str(report_path), report_format="json")
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                result = run(args, logging.getLogger("test"))
                assert result == 0
                assert report_path.exists()

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_report_html(self, mock_load, mock_build, tmp_path):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.return_value = {"failed": 0, "applied": 1, "results": []}
        mock_build.return_value = mock_provider
        report_path = tmp_path / "report.html"
        args = _make_simple_args(report=str(report_path), report_format="html")
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_post_apply"):
                result = run(args, logging.getLogger("test"))
                assert result == 0
                assert report_path.exists()

    @patch("fluid_build.cli.apply.build_provider")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_apply_exception_on_error_hook(self, mock_load, mock_build):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0"}
        mock_provider = MagicMock()
        mock_provider.plan.return_value = [{"op": "ensure_table"}]
        mock_provider.apply.side_effect = RuntimeError("provider failed")
        mock_build.return_value = mock_provider
        args = _make_simple_args(debug=True)
        with patch("fluid_build.cli.hooks.run_pre_apply", return_value=[{"op": "ensure_table"}]):
            with patch("fluid_build.cli.hooks.run_on_error"):
                with pytest.raises(Exception):
                    run(args, logging.getLogger("test"))

    @patch(
        "fluid_build.cli.apply.load_contract_with_overlay",
        side_effect=FileNotFoundError("not found"),
    )
    def test_run_contract_not_found(self, _mock_load):
        from fluid_build.cli.apply import run

        args = _make_simple_args()
        with pytest.raises(Exception):
            run(args, logging.getLogger("test"))


class TestApplyRunComplex:
    """Tests for complex orchestration mode."""

    @patch("fluid_build.cli.apply.FluidOrchestrationEngine")
    @patch("fluid_build.cli.apply.FluidPlanGenerator")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_complex_dry_run(self, mock_load, mock_plangen, _mock_engine_cls):
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "name": "test",
            "version": "1.0",
            "infrastructure": {"provider": "aws"},
        }
        mock_plan = MagicMock()
        mock_plan.phases = []
        mock_plangen.return_value.generate_execution_plan.return_value = mock_plan
        args = _make_simple_args(dry_run=True)
        result = run(args, logging.getLogger("test"))
        assert result == 0

    @patch("asyncio.run")
    @patch("asyncio.get_event_loop")
    @patch("fluid_build.cli.apply.FluidOrchestrationEngine")
    @patch("fluid_build.cli.apply.FluidPlanGenerator")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_complex_success(self, mock_load, mock_plangen, _mock_engine_cls, mock_loop, mock_arun):
        from fluid_build.cli.apply import run

        mock_load.return_value = {
            "name": "test",
            "version": "1.0",
            "infrastructure": {"provider": "aws"},
        }
        mock_plan = MagicMock()
        mock_plan.phases = []
        mock_plangen.return_value.generate_execution_plan.return_value = mock_plan

        # Mock the event loop check
        mock_loop.return_value.is_running.return_value = False
        mock_arun.return_value = {"success": True, "phases_executed": 2}

        args = _make_simple_args()
        result = run(args, logging.getLogger("test"))
        assert result == 0

    @patch("asyncio.run")
    @patch("asyncio.get_event_loop")
    @patch("fluid_build.cli.apply.FluidOrchestrationEngine")
    @patch("fluid_build.cli.apply.FluidPlanGenerator")
    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_complex_failure(self, mock_load, mock_plangen, _mock_engine_cls, mock_loop, mock_arun):
        from fluid_build.cli.apply import run

        mock_load.return_value = {"name": "test", "version": "1.0", "sources": [{"type": "kafka"}]}
        mock_plan = MagicMock()
        mock_plan.phases = []
        mock_plangen.return_value.generate_execution_plan.return_value = mock_plan

        mock_loop.return_value.is_running.return_value = False
        mock_arun.return_value = {"success": False, "error": "deploy failed"}

        args = _make_simple_args()
        result = run(args, logging.getLogger("test"))
        assert result == 1

    def test_keyboard_interrupt(self):
        from fluid_build.cli.apply import run

        args = _make_simple_args()
        with patch(
            "fluid_build.cli.apply.load_contract_with_overlay", side_effect=KeyboardInterrupt()
        ):
            result = run(args, logging.getLogger("test"))
            assert result == 130

    @patch("fluid_build.cli.apply.load_contract_with_overlay")
    def test_json_plan_input(self, _mock_load):
        from fluid_build.cli.apply import run

        args = _make_simple_args(contract="plan.json", dry_run=True)
        plan_data = {
            "contract": {"name": "test"},
            "plan": {"contract_path": "test.yaml", "environment": "dev", "phases": []},
        }
        with patch("fluid_build.cli.apply.read_json", return_value=plan_data):
            result = run(args, logging.getLogger("test"))
            assert result == 0


class TestApplyDisplayHelpers:
    def test_display_execution_plan(self):
        from fluid_build.cli.apply import _display_execution_plan
        from fluid_build.cli.orchestration import ExecutionPlan

        plan = ExecutionPlan(contract_path="test.yaml", environment="dev", phases=[])
        _display_execution_plan(plan, None, logging.getLogger("test"))

    def test_display_dry_run_summary(self):
        from fluid_build.cli.apply import _display_dry_run_summary
        from fluid_build.cli.orchestration import ExecutionPlan

        plan = ExecutionPlan(contract_path="test.yaml", environment="dev", phases=[])
        _display_dry_run_summary(plan, None, logging.getLogger("test"))
