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

"""Tests for fluid_build.cli.orchestration — data structures and pure logic."""

from unittest.mock import MagicMock, patch

from fluid_build.cli.orchestration import (
    ActionStatus,
    ExecutionAction,
    ExecutionContext,
    ExecutionMetrics,
    ExecutionPhase,
    ExecutionPlan,
    FluidOrchestrationEngine,
    PhaseExecution,
    RollbackStrategy,
)

# ── Enum tests ──


class TestEnums:
    def test_execution_phases(self):
        assert ExecutionPhase.VALIDATION.value == "validation"
        assert ExecutionPhase.FINALIZATION.value == "finalization"
        assert len(ExecutionPhase) == 9

    def test_action_status(self):
        assert ActionStatus.PENDING.value == "pending"
        assert ActionStatus.CANCELLED.value == "cancelled"
        assert len(ActionStatus) == 7

    def test_rollback_strategy(self):
        assert RollbackStrategy.NONE.value == "none"
        assert RollbackStrategy.FULL_ROLLBACK.value == "full_rollback"


# ── Dataclass tests ──


class TestExecutionAction:
    def test_defaults(self):
        a = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="validate",
            description="test",
        )
        assert a.dependencies == []
        assert a.timeout_seconds == 300
        assert a.retry_count == 3
        assert a.status == ActionStatus.PENDING
        assert a.rollback_operation is None

    def test_custom_values(self):
        a = ExecutionAction(
            id="a2",
            phase=ExecutionPhase.INFRASTRUCTURE,
            provider="gcp",
            operation="create_dataset",
            description="Create BQ dataset",
            dependencies=["a1"],
            timeout_seconds=600,
            retry_count=5,
        )
        assert a.dependencies == ["a1"]
        assert a.timeout_seconds == 600


class TestPhaseExecution:
    def test_defaults(self):
        pe = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
        )
        assert pe.parallel_execution is False
        assert pe.continue_on_error is False
        assert pe.rollback_strategy == RollbackStrategy.PHASE_COMPLETE
        assert pe.status == ActionStatus.PENDING

    def test_custom(self):
        pe = PhaseExecution(
            phase=ExecutionPhase.DATA_INGESTION,
            actions=[],
            parallel_execution=True,
            continue_on_error=True,
            rollback_strategy=RollbackStrategy.FULL_ROLLBACK,
        )
        assert pe.parallel_execution is True


class TestExecutionPlan:
    def test_defaults(self):
        plan = ExecutionPlan(
            contract_path="/tmp/c.yaml",
            environment=None,
            phases=[],
        )
        assert plan.global_timeout_minutes == 60
        assert plan.dry_run is False
        assert plan.parallel_phases is False


class TestExecutionMetrics:
    def test_defaults(self):
        m = ExecutionMetrics()
        assert m.total_actions == 0
        assert m.successful_actions == 0
        assert m.failed_actions == 0
        assert m.total_duration_seconds == 0


class TestExecutionContext:
    def test_basic_creation(self):
        plan = ExecutionPlan("/tmp/c.yaml", None, [])
        ctx = ExecutionContext(
            execution_id="exec-1",
            contract={"id": "test"},
            plan=plan,
        )
        assert ctx.execution_id == "exec-1"
        assert ctx.providers == {}


# ── Engine pure methods ──


class TestOrchestrationEnginePureMethods:
    def _make_engine(self):
        plan = ExecutionPlan("/tmp/c.yaml", None, [])
        ctx = ExecutionContext(
            execution_id="test-exec",
            contract={"id": "test"},
            plan=plan,
            logger=MagicMock(),
        )
        with patch.object(FluidOrchestrationEngine, "_ensure_directories"):
            engine = FluidOrchestrationEngine(ctx)
        return engine

    def test_should_continue_success(self):
        engine = self._make_engine()
        pe = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
            status=ActionStatus.SUCCESS,
        )
        assert engine._should_continue_execution(pe) is True

    def test_should_continue_failed_no_continue(self):
        engine = self._make_engine()
        pe = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
            status=ActionStatus.FAILED,
            continue_on_error=False,
        )
        assert engine._should_continue_execution(pe) is False

    def test_should_continue_failed_with_continue(self):
        engine = self._make_engine()
        pe = PhaseExecution(
            phase=ExecutionPhase.VALIDATION,
            actions=[],
            status=ActionStatus.FAILED,
            continue_on_error=True,
        )
        assert engine._should_continue_execution(pe) is True

    def test_update_action_metrics_success(self):
        engine = self._make_engine()
        action = ExecutionAction(
            id="a",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="x",
            description="d",
            status=ActionStatus.SUCCESS,
        )
        engine._update_action_metrics(action)
        assert engine.context.metrics.successful_actions == 1

    def test_update_action_metrics_failed(self):
        engine = self._make_engine()
        action = ExecutionAction(
            id="a",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="x",
            description="d",
            status=ActionStatus.FAILED,
        )
        engine._update_action_metrics(action)
        assert engine.context.metrics.failed_actions == 1

    def test_update_action_metrics_skipped(self):
        engine = self._make_engine()
        action = ExecutionAction(
            id="a",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="x",
            description="d",
            status=ActionStatus.SKIPPED,
        )
        engine._update_action_metrics(action)
        assert engine.context.metrics.skipped_actions == 1

    def test_build_dependency_graph(self):
        engine = self._make_engine()
        actions = [
            ExecutionAction(
                id="a",
                phase=ExecutionPhase.VALIDATION,
                provider="local",
                operation="x",
                description="d",
                dependencies=["b", "c"],
            ),
            ExecutionAction(
                id="b",
                phase=ExecutionPhase.VALIDATION,
                provider="local",
                operation="y",
                description="d",
                dependencies=[],
            ),
        ]
        graph = engine._build_dependency_graph(actions)
        assert graph == {"a": ["b", "c"], "b": []}
