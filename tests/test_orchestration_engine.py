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

"""Tests for cli/orchestration.py — enums, dataclasses, plan generator, engine helpers."""

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


# ── Enums ────────────────────────────────────────────────────────────
class TestExecutionPhase:
    def test_all_values(self):
        assert len(ExecutionPhase) == 9
        assert ExecutionPhase.VALIDATION.value == "validation"
        assert ExecutionPhase.FINALIZATION.value == "finalization"


class TestActionStatus:
    def test_all_values(self):
        assert len(ActionStatus) == 7
        assert ActionStatus.PENDING.value == "pending"
        assert ActionStatus.CANCELLED.value == "cancelled"


class TestRollbackStrategy:
    def test_all_values(self):
        assert len(RollbackStrategy) == 4
        assert RollbackStrategy.NONE.value == "none"
        assert RollbackStrategy.FULL_ROLLBACK.value == "full_rollback"


# ── Dataclasses ──────────────────────────────────────────────────────
class TestExecutionAction:
    def test_defaults(self):
        a = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="validate",
            description="desc",
        )
        assert a.dependencies == []
        assert a.timeout_seconds == 300
        assert a.retry_count == 3
        assert a.status is ActionStatus.PENDING
        assert a.error is None
        assert a.output is None
        assert a.metadata == {}


class TestPhaseExecution:
    def test_defaults(self):
        p = PhaseExecution(phase=ExecutionPhase.INFRASTRUCTURE, actions=[])
        assert p.parallel_execution is False
        assert p.continue_on_error is False
        assert p.rollback_strategy is RollbackStrategy.PHASE_COMPLETE
        assert p.status is ActionStatus.PENDING


class TestExecutionPlan:
    def test_defaults(self):
        plan = ExecutionPlan(contract_path="c.yaml", environment="dev", phases=[])
        assert plan.global_timeout_minutes == 60
        assert plan.dry_run is False
        assert plan.parallel_phases is False


class TestExecutionMetrics:
    def test_defaults(self):
        m = ExecutionMetrics()
        assert m.total_actions == 0
        assert m.phase_durations == {}

    def test_increment(self):
        m = ExecutionMetrics()
        m.successful_actions += 1
        m.total_actions += 1
        assert m.successful_actions == 1


class TestExecutionContext:
    def test_defaults(self):
        plan = ExecutionPlan(contract_path="c.yaml", environment=None, phases=[])
        ctx = ExecutionContext(execution_id="x1", contract={}, plan=plan)
        assert ctx.providers == {}
        assert ctx.metrics.total_actions == 0


# ── Engine helper methods ────────────────────────────────────────────
class TestShouldContinueExecution:
    """Test _should_continue_execution via the engine."""

    def _make_engine(self):
        plan = ExecutionPlan(contract_path="c.yaml", environment=None, phases=[])
        ctx = ExecutionContext(execution_id="x1", contract={}, plan=plan)
        import logging

        ctx.logger = logging.getLogger("test")
        return FluidOrchestrationEngine(ctx)

    def test_success_continues(self):
        engine = self._make_engine()
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[])
        phase.status = ActionStatus.SUCCESS
        assert engine._should_continue_execution(phase) is True

    def test_failed_no_continue(self):
        engine = self._make_engine()
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[], continue_on_error=False)
        phase.status = ActionStatus.FAILED
        assert engine._should_continue_execution(phase) is False

    def test_failed_continue_on_error(self):
        engine = self._make_engine()
        phase = PhaseExecution(phase=ExecutionPhase.VALIDATION, actions=[], continue_on_error=True)
        phase.status = ActionStatus.FAILED
        assert engine._should_continue_execution(phase) is True


class TestUpdateActionMetrics:
    def _make_engine(self):
        plan = ExecutionPlan(contract_path="c.yaml", environment=None, phases=[])
        ctx = ExecutionContext(execution_id="x1", contract={}, plan=plan)
        import logging

        ctx.logger = logging.getLogger("test")
        return FluidOrchestrationEngine(ctx)

    def test_success(self):
        engine = self._make_engine()
        a = ExecutionAction(
            id="a",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="op",
            description="d",
        )
        a.status = ActionStatus.SUCCESS
        engine._update_action_metrics(a)
        assert engine.context.metrics.successful_actions == 1
        assert engine.context.metrics.total_actions == 1

    def test_failed(self):
        engine = self._make_engine()
        a = ExecutionAction(
            id="a",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="op",
            description="d",
        )
        a.status = ActionStatus.FAILED
        engine._update_action_metrics(a)
        assert engine.context.metrics.failed_actions == 1

    def test_skipped(self):
        engine = self._make_engine()
        a = ExecutionAction(
            id="a",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="op",
            description="d",
        )
        a.status = ActionStatus.SKIPPED
        engine._update_action_metrics(a)
        assert engine.context.metrics.skipped_actions == 1


class TestBuildDependencyGraph:
    def _make_engine(self):
        plan = ExecutionPlan(contract_path="c.yaml", environment=None, phases=[])
        ctx = ExecutionContext(execution_id="x1", contract={}, plan=plan)
        import logging

        ctx.logger = logging.getLogger("test")
        return FluidOrchestrationEngine(ctx)

    def test_basic(self):
        engine = self._make_engine()
        a1 = ExecutionAction(
            id="a1",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="op",
            description="d",
        )
        a2 = ExecutionAction(
            id="a2",
            phase=ExecutionPhase.VALIDATION,
            provider="local",
            operation="op",
            description="d",
            dependencies=["a1"],
        )
        graph = engine._build_dependency_graph([a1, a2])
        assert graph == {"a1": [], "a2": ["a1"]}


class TestBuildExecutionSummary:
    def _make_engine(self):
        plan = ExecutionPlan(contract_path="c.yaml", environment=None, phases=[])
        ctx = ExecutionContext(execution_id="x1", contract={}, plan=plan)
        import logging

        ctx.logger = logging.getLogger("test")
        return FluidOrchestrationEngine(ctx)

    def test_success(self):
        engine = self._make_engine()
        summary = engine._build_execution_summary(success=True)
        assert summary["success"] is True
        assert summary["error"] is None
        assert "metrics" in summary

    def test_failure(self):
        engine = self._make_engine()
        summary = engine._build_execution_summary(success=False, error="boom")
        assert summary["success"] is False
        assert summary["error"] == "boom"


# ── FluidPlanGenerator ───────────────────────────────────────────────
class TestFluidPlanGenerator:
    def test_empty_contract(self):
        gen = FluidPlanGenerator({})
        plan = gen.generate_execution_plan("c.yaml")
        assert plan.contract_path == "c.yaml"
        assert plan.phases == []

    def test_analyze_detects_infrastructure(self):
        gen = FluidPlanGenerator({"infrastructure": {"provider": "gcp", "resources": []}})
        gen._analyze_contract()
        assert "terraform" in gen.detected_providers
        assert "gcp" in gen.detected_providers

    def test_analyze_detects_aws(self):
        gen = FluidPlanGenerator({"infrastructure": {"provider": "aws"}})
        gen._analyze_contract()
        assert "aws" in gen.detected_providers

    def test_analyze_detects_dbt(self):
        gen = FluidPlanGenerator({"transformations": True, "models": []})
        gen._analyze_contract()
        assert "dbt" in gen.detected_providers

    def test_analyze_detects_airbyte(self):
        gen = FluidPlanGenerator({"sources": [{"name": "pg"}]})
        gen._analyze_contract()
        assert "airbyte" in gen.detected_providers

    def test_analyze_detects_datadog(self):
        gen = FluidPlanGenerator({"monitoring": {}})
        gen._analyze_contract()
        assert "datadog" in gen.detected_providers

    def test_generate_phases_infrastructure(self):
        gen = FluidPlanGenerator({"infrastructure": {"provider": "gcp"}})
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = [p.phase.value for p in plan.phases]
        assert "infrastructure" in phase_names

    def test_generate_phases_ingestion(self):
        gen = FluidPlanGenerator({"sources": [{"name": "s1"}]})
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = [p.phase.value for p in plan.phases]
        assert "data_ingestion" in phase_names

    def test_generate_phases_transformation_dbt(self):
        gen = FluidPlanGenerator({"transformations": True, "models": [{"name": "m1"}]})
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = [p.phase.value for p in plan.phases]
        assert "transformation" in phase_names

    def test_generate_phases_quality(self):
        gen = FluidPlanGenerator({"quality": True})
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = [p.phase.value for p in plan.phases]
        assert "quality_gates" in phase_names

    def test_generate_phases_governance(self):
        gen = FluidPlanGenerator({"governance": True})
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = [p.phase.value for p in plan.phases]
        assert "governance" in phase_names

    def test_generate_phases_monitoring(self):
        gen = FluidPlanGenerator({"monitoring": {}})
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = [p.phase.value for p in plan.phases]
        assert "monitoring" in phase_names

    def test_calculate_total_timeout_base(self):
        gen = FluidPlanGenerator({})
        assert gen._calculate_total_timeout() == 120

    def test_calculate_total_timeout_with_infra(self):
        gen = FluidPlanGenerator({"infrastructure": {}})
        assert gen._calculate_total_timeout() == 150

    def test_calculate_total_timeout_with_transforms(self):
        gen = FluidPlanGenerator({"transformations": True, "models": [1, 2, 3]})
        timeout = gen._calculate_total_timeout()
        assert timeout == 120 + 6  # 3 models * 2

    def test_determine_rollback_production(self):
        gen = FluidPlanGenerator({}, environment="production")
        assert gen._determine_rollback_strategy() is RollbackStrategy.IMMEDIATE

    def test_determine_rollback_prod(self):
        gen = FluidPlanGenerator({}, environment="prod")
        assert gen._determine_rollback_strategy() is RollbackStrategy.IMMEDIATE

    def test_determine_rollback_dev(self):
        gen = FluidPlanGenerator({}, environment="dev")
        assert gen._determine_rollback_strategy() is RollbackStrategy.PHASE_COMPLETE

    def test_generate_plan_metadata(self):
        gen = FluidPlanGenerator(
            {"version": "1.0", "sources": [{"name": "s1"}]}, environment="staging"
        )
        gen._analyze_contract()
        meta = gen._generate_plan_metadata()
        assert meta["contract_version"] == "1.0"
        assert meta["environment"] == "staging"
        assert "airbyte" in meta["detected_providers"]
        assert meta["resource_count"] > 0

    def test_extract_required_resources(self):
        gen = FluidPlanGenerator(
            {
                "infrastructure": {"resources": ["vpc", "subnet"]},
                "sources": [{"name": "pg"}, {"name": "mysql"}],
                "destinations": [{"name": "bq"}],
            }
        )
        resources = gen._extract_required_resources()
        assert "vpc" in resources
        assert "source:pg" in resources
        assert "destination:bq" in resources

    def test_full_contract_generates_all_phases(self):
        contract = {
            "infrastructure": {"provider": "gcp"},
            "sources": [{"name": "s"}],
            "transformations": True,
            "models": [],
            "quality": True,
            "governance": True,
            "monitoring": {},
        }
        gen = FluidPlanGenerator(contract, environment="dev")
        plan = gen.generate_execution_plan("c.yaml")
        phase_names = {p.phase.value for p in plan.phases}
        assert phase_names == {
            "infrastructure",
            "data_ingestion",
            "transformation",
            "quality_gates",
            "governance",
            "monitoring",
        }
