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

"""Tests for orchestration.py dataclasses and enums."""

from fluid_build.cli.orchestration import (
    ActionStatus,
    ExecutionAction,
    ExecutionContext,
    ExecutionMetrics,
    ExecutionPhase,
    ExecutionPlan,
    PhaseExecution,
    RollbackStrategy,
)


class TestOrchestrationEnums:
    def test_execution_phases(self):
        assert ExecutionPhase.VALIDATION.value == "validation"
        assert ExecutionPhase.INFRASTRUCTURE.value == "infrastructure"
        assert ExecutionPhase.FINALIZATION.value == "finalization"
        assert len(ExecutionPhase) == 9

    def test_action_status(self):
        assert ActionStatus.PENDING.value == "pending"
        assert ActionStatus.SUCCESS.value == "success"
        assert ActionStatus.CANCELLED.value == "cancelled"

    def test_rollback_strategy(self):
        assert RollbackStrategy.NONE.value == "none"
        assert RollbackStrategy.FULL_ROLLBACK.value == "full_rollback"


class TestExecutionAction:
    def test_defaults(self):
        a = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="gcp",
            operation="validate",
            description="Test",
        )
        assert a.status == ActionStatus.PENDING
        assert a.timeout_seconds == 300
        assert a.retry_count == 3
        assert a.dependencies == []
        assert a.start_time is None
        assert a.error is None

    def test_custom_values(self):
        a = ExecutionAction(
            id="a2",
            phase=ExecutionPhase.DATA_INGESTION,
            provider="aws",
            operation="ingest",
            description="Load data",
            timeout_seconds=600,
            retry_count=5,
            rollback_operation="delete_table",
            metadata={"table": "orders"},
        )
        assert a.timeout_seconds == 600
        assert a.rollback_operation == "delete_table"
        assert a.metadata["table"] == "orders"


class TestPhaseExecution:
    def test_defaults(self):
        pe = PhaseExecution(phase=ExecutionPhase.TRANSFORMATION, actions=[])
        assert pe.parallel_execution is False
        assert pe.continue_on_error is False
        assert pe.rollback_strategy == RollbackStrategy.PHASE_COMPLETE
        assert pe.status == ActionStatus.PENDING

    def test_with_actions(self):
        actions = [
            ExecutionAction(
                id="a1",
                phase=ExecutionPhase.TRANSFORMATION,
                provider="local",
                operation="transform",
                description="T1",
            ),
            ExecutionAction(
                id="a2",
                phase=ExecutionPhase.TRANSFORMATION,
                provider="local",
                operation="transform",
                description="T2",
            ),
        ]
        pe = PhaseExecution(
            phase=ExecutionPhase.TRANSFORMATION, actions=actions, parallel_execution=True
        )
        assert len(pe.actions) == 2
        assert pe.parallel_execution is True


class TestExecutionPlan:
    def test_defaults(self):
        plan = ExecutionPlan(contract_path="/c.yaml", environment="dev", phases=[])
        assert plan.global_timeout_minutes == 60
        assert plan.rollback_strategy == RollbackStrategy.PHASE_COMPLETE
        assert plan.dry_run is False
        assert plan.parallel_phases is False

    def test_with_phases(self):
        pe = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[])
        plan = ExecutionPlan(contract_path="/c.yaml", environment="prod", phases=[pe], dry_run=True)
        assert len(plan.phases) == 1
        assert plan.dry_run is True


class TestExecutionMetrics:
    def test_defaults(self):
        m = ExecutionMetrics()
        assert m.total_actions == 0
        assert m.successful_actions == 0
        assert m.total_duration_seconds == 0
        assert m.phase_durations == {}

    def test_update(self):
        m = ExecutionMetrics(
            total_actions=10, successful_actions=8, failed_actions=2, total_duration_seconds=45.5
        )
        assert m.total_actions == 10
        assert m.failed_actions == 2


class TestExecutionContext:
    def test_defaults(self):
        plan = ExecutionPlan(contract_path="/c.yaml", environment="dev", phases=[])
        ctx = ExecutionContext(execution_id="e1", contract={"id": "test"}, plan=plan)
        assert ctx.execution_id == "e1"
        assert ctx.contract["id"] == "test"
        assert ctx.providers == {}
        assert ctx.metrics.total_actions == 0

    def test_custom_dirs(self):
        from pathlib import Path

        plan = ExecutionPlan(contract_path="/c.yaml", environment="prod", phases=[])
        ctx = ExecutionContext(
            execution_id="e2",
            contract={},
            plan=plan,
            workspace_dir=Path("/my/ws"),
            artifacts_dir=Path("/my/artifacts"),
        )
        assert ctx.workspace_dir == Path("/my/ws")
        assert ctx.artifacts_dir == Path("/my/artifacts")
