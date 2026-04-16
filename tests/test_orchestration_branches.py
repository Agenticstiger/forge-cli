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

"""Branch-coverage tests for fluid_build.cli.orchestration"""

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fluid_build.cli.orchestration import (
    ActionStatus,
    ExecutionAction,
    ExecutionContext,
    ExecutionMetrics,
    ExecutionPhase,
    ExecutionPlan,
    FluidOrchestrationEngine,
    FluidPlanGenerator,
    PhaseExecution,
    RollbackStrategy,
)

# ===================== Enums =====================


class TestEnums:
    def test_execution_phase_values(self):
        assert ExecutionPhase.VALIDATION.value == "validation"
        assert ExecutionPhase.INFRASTRUCTURE.value == "infrastructure"
        assert ExecutionPhase.FINALIZATION.value == "finalization"

    def test_action_status_values(self):
        assert ActionStatus.PENDING.value == "pending"
        assert ActionStatus.RUNNING.value == "running"
        assert ActionStatus.RETRYING.value == "retrying"
        assert ActionStatus.CANCELLED.value == "cancelled"

    def test_rollback_strategy_values(self):
        assert RollbackStrategy.NONE.value == "none"
        assert RollbackStrategy.IMMEDIATE.value == "immediate"
        assert RollbackStrategy.FULL_ROLLBACK.value == "full_rollback"


# ===================== Dataclasses =====================


class TestDataclasses:
    def test_execution_action_defaults(self):
        a = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="desc",
        )
        assert a.status == ActionStatus.PENDING
        assert a.retry_count == 3
        assert a.timeout_seconds == 300

    def test_phase_execution_defaults(self):
        p = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[])
        assert p.status == ActionStatus.PENDING
        assert not p.parallel_execution

    def test_execution_plan_defaults(self):
        p = ExecutionPlan(contract_path="test.yaml", environment=None, phases=[])
        assert p.global_timeout_minutes == 60
        assert p.rollback_strategy == RollbackStrategy.PHASE_COMPLETE

    def test_execution_metrics_defaults(self):
        m = ExecutionMetrics()
        assert m.total_actions == 0
        assert m.total_duration_seconds == 0

    def test_execution_context_defaults(self):
        plan = ExecutionPlan(contract_path="test.yaml", environment=None, phases=[])
        ctx = ExecutionContext(execution_id="test-id", contract={}, plan=plan)
        assert ctx.workspace_dir == Path(".")


# ===================== FluidOrchestrationEngine =====================


def _make_engine(phases=None, rollback_strategy=RollbackStrategy.PHASE_COMPLETE):
    plan = ExecutionPlan(
        contract_path="test.yaml",
        environment="dev",
        phases=phases or [],
        rollback_strategy=rollback_strategy,
    )
    ctx = ExecutionContext(
        execution_id="test-exec-001",
        contract={"project": "test-proj"},
        plan=plan,
        logger=logging.getLogger("test"),
    )
    with patch.object(FluidOrchestrationEngine, "_ensure_directories"):
        engine = FluidOrchestrationEngine(ctx)
    # Disable rich console
    engine.context.console = None
    return engine


class TestOrchestrationEngine:
    def test_should_continue_success(self):
        engine = _make_engine()
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[])
        phase.status = ActionStatus.SUCCESS
        assert engine._should_continue_execution(phase) is True

    def test_should_continue_failed_no_continue(self):
        engine = _make_engine()
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[], continue_on_error=False)
        phase.status = ActionStatus.FAILED
        assert engine._should_continue_execution(phase) is False

    def test_should_continue_failed_with_continue(self):
        engine = _make_engine()
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[], continue_on_error=True)
        phase.status = ActionStatus.FAILED
        assert engine._should_continue_execution(phase) is True

    def test_update_action_metrics_success(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        action.status = ActionStatus.SUCCESS
        engine._update_action_metrics(action)
        assert engine.context.metrics.successful_actions == 1
        assert engine.context.metrics.total_actions == 1

    def test_update_action_metrics_failed(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        action.status = ActionStatus.FAILED
        engine._update_action_metrics(action)
        assert engine.context.metrics.failed_actions == 1

    def test_update_action_metrics_skipped(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        action.status = ActionStatus.SKIPPED
        engine._update_action_metrics(action)
        assert engine.context.metrics.skipped_actions == 1

    def test_build_execution_summary_success(self):
        engine = _make_engine()
        summary = engine._build_execution_summary(success=True)
        assert summary["success"] is True
        assert summary["error"] is None

    def test_build_execution_summary_failure(self):
        engine = _make_engine()
        summary = engine._build_execution_summary(success=False, error="boom")
        assert summary["success"] is False
        assert summary["error"] == "boom"

    def test_build_dependency_graph(self):
        engine = _make_engine()
        a1 = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        a2 = ExecutionAction(
            id="a2",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="d",
            dependencies=["a1"],
        )
        graph = engine._build_dependency_graph([a1, a2])
        assert graph["a1"] == []
        assert graph["a2"] == ["a1"]

    def test_logging_helpers_no_console(self):
        engine = _make_engine()
        engine.context.console = None
        engine._log_phase("phase msg")
        engine._log_success("ok msg")
        engine._log_info("info msg")
        engine._log_warning("warn msg")
        engine._log_error("error msg")

    def test_logging_helpers_with_console(self):
        engine = _make_engine()
        engine.context.console = MagicMock()
        with patch("fluid_build.cli.orchestration.RICH_AVAILABLE", True):
            engine._log_phase("phase msg")
            engine._log_success("ok msg")
            engine._log_info("info msg")
            engine._log_warning("warn msg")
            engine._log_error("error msg")
        assert engine.context.console.print.call_count == 5

    def test_log_execution_start_no_console(self):
        engine = _make_engine()
        engine.context.console = None
        engine._log_execution_start()

    def test_log_execution_start_with_console(self):
        engine = _make_engine()
        engine.context.console = MagicMock()
        with patch("fluid_build.cli.orchestration.RICH_AVAILABLE", True):
            engine._log_execution_start()
        engine.context.console.print.assert_called()

    def test_log_action_start(self):
        engine = _make_engine()
        engine.context.console = None
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="Test action",
        )
        engine._log_action_start(action)

    def test_log_action_success(self):
        engine = _make_engine()
        engine.context.console = None
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="Test action",
        )
        action.start_time = datetime.now(timezone.utc)
        action.end_time = datetime.now(timezone.utc)
        engine._log_action_success(action)

    def test_log_action_success_no_times(self):
        engine = _make_engine()
        engine.context.console = None
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="Test action",
        )
        engine._log_action_success(action)

    def test_execute_plan_success(self):
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="test_p",
            operation="validate",
            description="Validate",
        )
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[action])
        engine = _make_engine(phases=[phase])

        mock_provider = MagicMock()
        mock_provider.validate.return_value = {"status": "ok"}

        async def run_test():
            with (
                patch.object(engine, "_initialize_providers"),
                patch.object(engine, "_execute_phase"),
                patch.object(engine, "_finalize_execution"),
                patch.object(engine, "_save_execution_state"),
            ):
                result = await engine.execute_plan()
            return result

        result = asyncio.run(run_test())
        assert result["success"] is True

    def test_execute_plan_failure(self):
        engine = _make_engine()

        async def run_test():
            with (
                patch.object(engine, "_initialize_providers", side_effect=RuntimeError("boom")),
                patch.object(engine, "_handle_execution_failure"),
            ):
                result = await engine.execute_plan()
            return result

        result = asyncio.run(run_test())
        assert result["success"] is False

    def test_execute_phase_sequential(self):
        action = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        phase = PhaseExecution(
            phase=ExecutionPhase.VALIDATION, actions=[action], parallel_execution=False
        )
        engine = _make_engine()

        async def run_test():
            with patch.object(engine, "_execute_actions_sequential"):
                await engine._execute_phase(phase)
            assert phase.status == ActionStatus.SUCCESS

        asyncio.run(run_test())

    def test_execute_phase_parallel(self):
        action = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        phase = PhaseExecution(
            phase=ExecutionPhase.VALIDATION, actions=[action], parallel_execution=True
        )
        engine = _make_engine()

        async def run_test():
            with patch.object(engine, "_execute_actions_parallel"):
                await engine._execute_phase(phase)
            assert phase.status == ActionStatus.SUCCESS

        asyncio.run(run_test())

    def test_execute_phase_failure_triggers_rollback(self):
        action = ExecutionAction(
            id="a1", phase=ExecutionPhase.VALIDATION, provider="p", operation="op", description="d"
        )
        phase = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[action],
            rollback_strategy=RollbackStrategy.IMMEDIATE,
        )
        engine = _make_engine()

        async def run_test():
            with (
                patch.object(
                    engine, "_execute_actions_sequential", side_effect=RuntimeError("fail")
                ),
                patch.object(engine, "_handle_phase_failure") as mock_rollback,
            ):
                with pytest.raises(RuntimeError):
                    await engine._execute_phase(phase)
            mock_rollback.assert_called_once()

        asyncio.run(run_test())

    def test_execute_single_action_success(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="do_thing",
            description="d",
            rollback_operation="undo_thing",
        )
        mock_provider = MagicMock()
        mock_provider.do_thing.return_value = {"ok": True}
        engine.context.providers["p"] = mock_provider

        async def run_test():
            with patch.object(engine, "_execute_operation", return_value={"ok": True}):
                await engine._execute_single_action(action)
            assert action.status == ActionStatus.SUCCESS
            assert action in engine.rollback_stack

        asyncio.run(run_test())

    def test_execute_single_action_provider_not_found(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="missing",
            operation="op",
            description="d",
            retry_count=0,
        )

        async def run_test():
            with pytest.raises(Exception):
                await engine._execute_single_action(action)

        asyncio.run(run_test())

    def test_execute_single_action_timeout(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="d",
            timeout_seconds=1,
            retry_count=0,
        )
        engine.context.providers["p"] = MagicMock()

        async def run_test():
            async def slow_op(*args, **kwargs):
                await asyncio.sleep(10)

            with patch.object(engine, "_execute_operation", side_effect=slow_op):
                with pytest.raises(Exception):
                    await engine._execute_single_action(action)
            assert action.status == ActionStatus.FAILED

        asyncio.run(run_test())

    def test_execute_operation_sync(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="do_sync",
            description="d",
            metadata={"key": "val"},
        )
        mock_provider = MagicMock()
        mock_provider.do_sync.return_value = {"result": "ok"}

        async def run_test():
            result = await engine._execute_operation(mock_provider, action)
            assert result == {"result": "ok"}

        asyncio.run(run_test())

    def test_execute_operation_async(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="do_async",
            description="d",
            metadata={},
        )
        mock_provider = MagicMock()
        mock_provider.do_async = AsyncMock(return_value={"async": True})

        async def run_test():
            result = await engine._execute_operation(mock_provider, action)
            assert result == {"async": True}

        asyncio.run(run_test())

    def test_execute_operation_not_found(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="nonexistent",
            description="d",
            metadata={},
        )
        mock_provider = MagicMock(spec=[])  # No methods

        async def run_test():
            with pytest.raises(Exception):
                await engine._execute_operation(mock_provider, action)

        asyncio.run(run_test())

    def test_handle_phase_failure_immediate(self):
        engine = _make_engine()
        phase = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
            rollback_strategy=RollbackStrategy.IMMEDIATE,
        )

        async def run_test():
            with patch.object(engine, "_execute_rollback") as mock_rb:
                await engine._handle_phase_failure(phase, RuntimeError("fail"))
            mock_rb.assert_called_once_with(phase_only=True, failed_phase=ExecutionPhase.VALIDATION)

        asyncio.run(run_test())

    def test_handle_phase_failure_full_rollback(self):
        engine = _make_engine()
        phase = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
            rollback_strategy=RollbackStrategy.FULL_ROLLBACK,
        )

        async def run_test():
            with patch.object(engine, "_execute_rollback") as mock_rb:
                await engine._handle_phase_failure(phase, RuntimeError("fail"))
            mock_rb.assert_called_once_with(phase_only=False)

        asyncio.run(run_test())

    def test_handle_phase_failure_phase_complete(self):
        engine = _make_engine()
        phase = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )

        async def run_test():
            with patch.object(engine, "_execute_rollback") as mock_rb:
                await engine._handle_phase_failure(phase, RuntimeError("fail"))
            mock_rb.assert_not_called()

        asyncio.run(run_test())

    def test_execute_rollback_phase_only(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="d",
            rollback_operation="undo",
        )
        engine.rollback_stack.append(action)
        mock_provider = MagicMock()
        engine.context.providers["p"] = mock_provider

        async def run_test():
            with patch.object(engine, "_execute_rollback_operation"):
                await engine._execute_rollback(
                    phase_only=True, failed_phase=ExecutionPhase.VALIDATION
                )

        asyncio.run(run_test())

    def test_execute_rollback_failure(self):
        engine = _make_engine()
        action = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="d",
            rollback_operation="undo",
        )
        engine.rollback_stack.append(action)
        mock_provider = MagicMock()
        engine.context.providers["p"] = mock_provider

        async def run_test():
            with patch.object(
                engine, "_execute_rollback_operation", side_effect=RuntimeError("rb fail")
            ):
                # Should not raise despite rollback failure
                await engine._execute_rollback(phase_only=False)

        asyncio.run(run_test())

    def test_save_execution_state(self, tmp_path):
        engine = _make_engine()
        engine.context.state_file = tmp_path / "state.json"

        async def run_test():
            await engine._save_execution_state()

        asyncio.run(run_test())
        assert engine.context.state_file.exists()

    def test_handle_execution_failure_with_rollback(self):
        engine = _make_engine(rollback_strategy=RollbackStrategy.FULL_ROLLBACK)

        async def run_test():
            with (
                patch.object(engine, "_execute_rollback") as mock_rb,
                patch.object(engine, "_save_execution_state"),
            ):
                await engine._handle_execution_failure(RuntimeError("fail"))
            mock_rb.assert_called_once()

        asyncio.run(run_test())

    def test_handle_execution_failure_no_rollback(self):
        engine = _make_engine(rollback_strategy=RollbackStrategy.NONE)

        async def run_test():
            with (
                patch.object(engine, "_execute_rollback") as mock_rb,
                patch.object(engine, "_save_execution_state"),
            ):
                await engine._handle_execution_failure(RuntimeError("fail"))
            mock_rb.assert_not_called()

        asyncio.run(run_test())

    def test_execute_actions_parallel_circular_dependency(self):
        engine = _make_engine()
        a1 = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="d",
            dependencies=["a2"],
        )
        a2 = ExecutionAction(
            id="a2",
            phase=ExecutionPhase.VALIDATION,
            provider="p",
            operation="op",
            description="d",
            dependencies=["a1"],
        )

        async def run_test():
            with pytest.raises(Exception):
                await engine._execute_actions_parallel([a1, a2])

        asyncio.run(run_test())


# ===================== FluidPlanGenerator =====================


class TestFluidPlanGenerator:
    def test_generate_empty_contract(self):
        gen = FluidPlanGenerator(contract={}, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        assert plan.contract_path == "test.yaml"
        assert len(plan.phases) == 0

    def test_generate_with_infrastructure(self):
        contract = {"infrastructure": {"resources": ["vpc", "subnet"]}}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.INFRASTRUCTURE in phases

    def test_generate_with_gcp_infra(self):
        contract = {"infrastructure": {"resources": [{"type": "gcp_bigquery_dataset"}]}}
        gen = FluidPlanGenerator(contract, environment="dev")
        gen._analyze_contract()
        assert "gcp" in gen.detected_providers

    def test_generate_with_aws_infra(self):
        contract = {"infrastructure": {"resources": [{"type": "aws_s3_bucket"}]}}
        gen = FluidPlanGenerator(contract, environment="dev")
        gen._analyze_contract()
        assert "aws" in gen.detected_providers

    def test_generate_with_sources(self):
        contract = {"sources": [{"name": "src1", "sync_mode": "incremental"}]}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.DATA_INGESTION in phases
        # Should have ingest + sync actions per source
        ingestion_phase = next(p for p in plan.phases if p.phase == ExecutionPhase.DATA_INGESTION)
        assert len(ingestion_phase.actions) == 2

    def test_generate_with_transformations_dbt(self):
        contract = {"models": [{"name": "model1"}], "dbt_project_dir": "."}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.TRANSFORMATION in phases

    def test_generate_with_build(self):
        contract = {"builds": [{"engine": "sql", "sql": "SELECT 1"}]}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.TRANSFORMATION in phases

    def test_generate_with_quality(self):
        contract = {"quality": {"expectations": []}}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.QUALITY_GATES in phases

    def test_generate_with_governance(self):
        contract = {"governance": {"policies": []}}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.GOVERNANCE in phases

    def test_generate_with_monitoring(self):
        contract = {"monitoring": {"alerts": []}}
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("test.yaml")
        phases = [p.phase for p in plan.phases]
        assert ExecutionPhase.MONITORING in phases

    def test_rollback_strategy_prod(self):
        gen = FluidPlanGenerator(contract={}, environment="prod")
        assert gen._determine_rollback_strategy() == RollbackStrategy.IMMEDIATE

    def test_rollback_strategy_production(self):
        gen = FluidPlanGenerator(contract={}, environment="production")
        assert gen._determine_rollback_strategy() == RollbackStrategy.IMMEDIATE

    def test_rollback_strategy_dev(self):
        gen = FluidPlanGenerator(contract={}, environment="dev")
        assert gen._determine_rollback_strategy() == RollbackStrategy.PHASE_COMPLETE

    def test_calculate_total_timeout_basic(self):
        gen = FluidPlanGenerator(contract={}, environment="dev")
        assert gen._calculate_total_timeout() == 120

    def test_calculate_total_timeout_with_infra(self):
        gen = FluidPlanGenerator(contract={"infrastructure": {}}, environment="dev")
        assert gen._calculate_total_timeout() == 150

    def test_calculate_total_timeout_with_transforms(self):
        contract = {"transformations": [], "models": [{"name": f"m{i}"} for i in range(50)]}
        gen = FluidPlanGenerator(contract, environment="dev")
        timeout = gen._calculate_total_timeout()
        assert timeout >= 120

    def test_generate_plan_metadata(self):
        contract = {"version": "1.2.3"}
        gen = FluidPlanGenerator(contract, environment="staging")
        meta = gen._generate_plan_metadata()
        assert meta["contract_version"] == "1.2.3"
        assert meta["environment"] == "staging"

    def test_extract_required_resources(self):
        contract = {
            "infrastructure": {"resources": ["vpc"]},
            "sources": [{"name": "s1"}],
            "destinations": [{"name": "d1"}],
        }
        gen = FluidPlanGenerator(contract, environment="dev")
        resources = gen._extract_required_resources()
        assert "vpc" in resources
        assert "source:s1" in resources
        assert "destination:d1" in resources

    def test_analyze_contract_datadog(self):
        contract = {"monitoring": {"backend": "datadog"}}
        gen = FluidPlanGenerator(contract, environment="dev")
        gen._analyze_contract()
        assert "datadog" in gen.detected_providers

    def test_analyze_contract_airbyte(self):
        contract = {"ingestion": {"engine": "airbyte"}}
        gen = FluidPlanGenerator(contract, environment="dev")
        gen._analyze_contract()
        assert "airbyte" in gen.detected_providers

    def test_full_pipeline_contract(self):
        """Test a contract that triggers all phases"""
        contract = {
            "infrastructure": {"resources": [{"type": "gcp_dataset"}]},
            "sources": [{"name": "taps"}],
            "builds": [{"engine": "sql"}],
            "quality": {"checks": []},
            "governance": {"policies": []},
            "monitoring": {"alerts": []},
        }
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("full.yaml")
        phase_names = [p.phase for p in plan.phases]
        assert ExecutionPhase.INFRASTRUCTURE in phase_names
        assert ExecutionPhase.DATA_INGESTION in phase_names
        assert ExecutionPhase.TRANSFORMATION in phase_names
        assert ExecutionPhase.QUALITY_GATES in phase_names
        assert ExecutionPhase.GOVERNANCE in phase_names
        assert ExecutionPhase.MONITORING in phase_names
