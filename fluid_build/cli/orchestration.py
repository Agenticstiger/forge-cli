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

"""
FLUID Orchestration Engine — Multi-phase data product deployment

Extracted from apply.py for maintainability. Contains:
- Data structures (enums, dataclasses) for execution plans
- FluidOrchestrationEngine — async multi-provider coordinator
- FluidPlanGenerator — contract analyser & plan builder

Internal module; apply.py is the public interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from ._common import CLIError, build_provider

# Rich imports for enhanced output
try:
    from rich.console import Console
    from rich.panel import Panel

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


# ==========================================
# Core Data Structures & Enums
# ==========================================


class ExecutionPhase(Enum):
    """Orchestration phases in dependency order"""

    VALIDATION = "validation"
    INFRASTRUCTURE = "infrastructure"
    DATA_INGESTION = "data_ingestion"
    TRANSFORMATION = "transformation"
    QUALITY_GATES = "quality_gates"
    GOVERNANCE = "governance"
    MONITORING = "monitoring"
    DISCOVERY = "discovery"
    FINALIZATION = "finalization"


class ActionStatus(Enum):
    """Status of individual actions"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


class RollbackStrategy(Enum):
    """Rollback strategies for failure scenarios"""

    NONE = "none"
    IMMEDIATE = "immediate"
    PHASE_COMPLETE = "phase_complete"
    FULL_ROLLBACK = "full_rollback"


@dataclass
class ExecutionAction:
    """Individual action within an execution phase"""

    id: str
    phase: ExecutionPhase
    provider: str
    operation: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    timeout_seconds: int = 300
    retry_count: int = 3
    rollback_operation: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error: Optional[str] = None
    output: Optional[Dict[str, Any]] = None


@dataclass
class PhaseExecution:
    """Execution state for a complete phase"""

    phase: ExecutionPhase
    actions: List[ExecutionAction]
    parallel_execution: bool = False
    continue_on_error: bool = False
    rollback_strategy: RollbackStrategy = RollbackStrategy.PHASE_COMPLETE
    status: ActionStatus = ActionStatus.PENDING
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    """Complete execution plan with orchestration logic"""

    contract_path: str
    environment: Optional[str]
    phases: List[PhaseExecution]
    global_timeout_minutes: int = 60
    rollback_strategy: RollbackStrategy = RollbackStrategy.PHASE_COMPLETE
    dry_run: bool = False
    parallel_phases: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionMetrics:
    """Comprehensive execution metrics"""

    total_actions: int = 0
    successful_actions: int = 0
    failed_actions: int = 0
    skipped_actions: int = 0
    total_duration_seconds: float = 0
    phase_durations: Dict[str, float] = field(default_factory=dict)
    resource_usage: Dict[str, Any] = field(default_factory=dict)
    performance_stats: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContext:
    """Runtime context for the execution"""

    execution_id: str
    contract: Dict[str, Any]
    plan: ExecutionPlan
    providers: Dict[str, Any] = field(default_factory=dict)
    workspace_dir: Path = Path(".")
    temp_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "fluid_build")
    artifacts_dir: Path = Path("runtime/artifacts")
    logs_dir: Path = Path("runtime/logs")
    state_file: Path = Path("runtime/apply_state.json")
    metrics: ExecutionMetrics = field(default_factory=ExecutionMetrics)
    console: Optional[Any] = None  # Rich console when available
    logger: Optional[logging.Logger] = None


# ==========================================
# Orchestration Engine
# ==========================================


class FluidOrchestrationEngine:
    """
    Advanced orchestration engine for data product deployment.

    Handles multi-provider coordination, dependency resolution, parallel
    execution, rollback, real-time progress tracking, and state management.
    """

    def __init__(self, context: ExecutionContext):
        self.context = context
        self.execution_state: Dict[str, Any] = {}
        self.rollback_stack: List[ExecutionAction] = []
        self.active_threads: Dict[str, Any] = {}

        if RICH_AVAILABLE:
            self.context.console = Console()

        self._ensure_directories()

    # ---- directory bootstrap ----

    def _ensure_directories(self):
        for dir_path in [
            self.context.artifacts_dir,
            self.context.logs_dir,
            self.context.temp_dir.parent,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)

    # ---- main entry ----

    async def execute_plan(self) -> Dict[str, Any]:
        start_time = datetime.now(timezone.utc)
        try:
            self._log_execution_start()
            await self._initialize_providers()

            for phase_exec in self.context.plan.phases:
                await self._execute_phase(phase_exec)
                if not self._should_continue_execution(phase_exec):
                    break

            await self._finalize_execution(success=True)
            return self._build_execution_summary(success=True)

        except Exception as e:
            self.context.logger.error(f"Execution failed: {e}")
            await self._handle_execution_failure(e)
            return self._build_execution_summary(success=False, error=str(e))
        finally:
            total_time = (datetime.now(timezone.utc) - start_time).total_seconds()
            self.context.metrics.total_duration_seconds = total_time

    # ---- provider init ----

    async def _initialize_providers(self):
        self._log_phase("🚀 Initializing Providers")
        provider_names: set[str] = set()
        for phase in self.context.plan.phases:
            for action in phase.actions:
                provider_names.add(action.provider)

        for provider_name in provider_names:
            try:
                provider = build_provider(
                    provider_name,
                    self.context.contract.get("project"),
                    self.context.contract.get("region"),
                    self.context.logger,
                )
                if hasattr(provider, "validate_readiness"):
                    await self._run_with_timeout(
                        provider.validate_readiness(),
                        30,
                        f"Provider {provider_name} readiness check",
                    )
                self.context.providers[provider_name] = provider
                self._log_success(f"✅ Provider '{provider_name}' initialized and ready")
            except Exception as e:
                self._log_error(f"❌ Failed to initialize provider '{provider_name}': {e}")
                raise CLIError(
                    1,
                    "provider_initialization_failed",
                    {"provider": provider_name, "error": str(e)},
                )

    # ---- phase execution ----

    async def _execute_phase(self, phase_exec: PhaseExecution):
        phase_name = phase_exec.phase.value
        phase_exec.start_time = datetime.now(timezone.utc)
        phase_exec.status = ActionStatus.RUNNING

        self._log_phase(f"🔄 Executing Phase: {phase_name.title()}")

        try:
            if phase_exec.parallel_execution:
                await self._execute_actions_parallel(phase_exec.actions)
            else:
                await self._execute_actions_sequential(phase_exec.actions)

            phase_exec.status = ActionStatus.SUCCESS
            phase_exec.end_time = datetime.now(timezone.utc)
            duration = (phase_exec.end_time - phase_exec.start_time).total_seconds()
            self.context.metrics.phase_durations[phase_name] = duration
            self._log_success(f"✅ Phase '{phase_name}' completed in {duration:.2f}s")

        except Exception as e:
            phase_exec.status = ActionStatus.FAILED
            phase_exec.end_time = datetime.now(timezone.utc)
            self._log_error(f"❌ Phase '{phase_name}' failed: {e}")
            await self._handle_phase_failure(phase_exec, e)
            raise

    async def _execute_actions_sequential(self, actions: List[ExecutionAction]):
        for action in actions:
            await self._wait_for_dependencies(action)
            await self._execute_single_action(action)
            self._update_action_metrics(action)

    async def _execute_actions_parallel(self, actions: List[ExecutionAction]):
        remaining_actions = set(actions)
        while remaining_actions:
            ready_actions = [
                a
                for a in remaining_actions
                if all(d not in {x.id for x in remaining_actions} for d in a.dependencies)
            ]
            if not ready_actions:
                raise CLIError(
                    1,
                    "circular_dependency_detected",
                    {"actions": [a.id for a in remaining_actions]},
                )
            tasks = [self._execute_single_action(a) for a in ready_actions]
            await asyncio.gather(*tasks)
            for a in ready_actions:
                remaining_actions.remove(a)
                self._update_action_metrics(a)

    # ---- single action ----

    async def _execute_single_action(self, action: ExecutionAction):
        action.start_time = datetime.now(timezone.utc)
        action.status = ActionStatus.RUNNING
        self._log_action_start(action)

        for attempt in range(action.retry_count + 1):
            try:
                if attempt > 0:
                    action.status = ActionStatus.RETRYING
                    self._log_info(f"🔄 Retrying action '{action.id}' (attempt {attempt + 1})")

                provider = self.context.providers.get(action.provider)
                if not provider:
                    raise CLIError(1, "provider_not_found", {"provider": action.provider})

                result = await self._run_with_timeout(
                    self._execute_operation(provider, action),
                    action.timeout_seconds,
                    f"Action {action.id}",
                )

                action.output = result
                action.status = ActionStatus.SUCCESS
                action.end_time = datetime.now(timezone.utc)
                if action.rollback_operation:
                    self.rollback_stack.append(action)
                self._log_action_success(action)
                return result

            except asyncio.TimeoutError:
                error_msg = f"Action '{action.id}' timed out after {action.timeout_seconds}s"
                self._log_error(error_msg)
                if attempt == action.retry_count:
                    action.status = ActionStatus.FAILED
                    action.error = error_msg
                    action.end_time = datetime.now(timezone.utc)
                    raise CLIError(1, "action_timeout", {"action": action.id})

            except Exception as e:
                error_msg = f"Action '{action.id}' failed: {e}"
                self._log_error(error_msg)
                if attempt == action.retry_count:
                    action.status = ActionStatus.FAILED
                    action.error = str(e)
                    action.end_time = datetime.now(timezone.utc)
                    raise CLIError(
                        1, "action_execution_failed", {"action": action.id, "error": str(e)}
                    )
                await asyncio.sleep(min(2**attempt, 30))

    async def _execute_operation(self, provider: Any, action: ExecutionAction) -> Dict[str, Any]:
        operation = action.operation
        metadata = action.metadata
        if hasattr(provider, operation):
            method = getattr(provider, operation)
            if asyncio.iscoroutinefunction(method):
                return await method(**metadata)
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: method(**metadata))
        raise CLIError(
            1, "operation_not_supported", {"provider": action.provider, "operation": operation}
        )

    # ---- rollback ----

    async def _handle_phase_failure(self, phase_exec: PhaseExecution, error: Exception):
        if phase_exec.rollback_strategy == RollbackStrategy.IMMEDIATE:
            await self._execute_rollback(phase_only=True, failed_phase=phase_exec.phase)
        elif phase_exec.rollback_strategy == RollbackStrategy.FULL_ROLLBACK:
            await self._execute_rollback(phase_only=False)

    async def _execute_rollback(
        self, phase_only: bool = False, failed_phase: ExecutionPhase = None
    ):
        self._log_phase("🔄 Executing Rollback Operations")
        if phase_only and failed_phase:
            actions = [a for a in self.rollback_stack if a.phase == failed_phase]
        else:
            actions = list(reversed(self.rollback_stack))

        for action in actions:
            try:
                provider = self.context.providers.get(action.provider)
                if provider and action.rollback_operation:
                    await self._run_with_timeout(
                        self._execute_rollback_operation(provider, action),
                        action.timeout_seconds,
                        f"Rollback for {action.id}",
                    )
                    self._log_info(f"✅ Rolled back action '{action.id}'")
            except Exception as e:
                self._log_error(f"❌ Failed to rollback action '{action.id}': {e}")

    async def _execute_rollback_operation(self, provider: Any, action: ExecutionAction):
        rollback_op = action.rollback_operation
        metadata = {**action.metadata, "original_action": action.id}
        if hasattr(provider, rollback_op):
            method = getattr(provider, rollback_op)
            if asyncio.iscoroutinefunction(method):
                return await method(**metadata)
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: method(**metadata))
        self._log_warning(
            f"Rollback operation '{rollback_op}' not found on provider '{action.provider}'"
        )

    # ---- helpers ----

    async def _run_with_timeout(self, coro, timeout_seconds: int, operation_name: str):
        try:
            return await asyncio.wait_for(coro, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self._log_error(f"Operation '{operation_name}' timed out after {timeout_seconds}s")
            raise

    def _build_dependency_graph(self, actions):
        return {a.id: a.dependencies.copy() for a in actions}

    async def _wait_for_dependencies(self, action: ExecutionAction):
        pass  # sequential execution handles order

    def _should_continue_execution(self, phase_exec: PhaseExecution) -> bool:
        if phase_exec.status == ActionStatus.FAILED:
            return phase_exec.continue_on_error
        return True

    def _update_action_metrics(self, action: ExecutionAction):
        m = self.context.metrics
        if action.status == ActionStatus.SUCCESS:
            m.successful_actions += 1
        elif action.status == ActionStatus.FAILED:
            m.failed_actions += 1
        elif action.status == ActionStatus.SKIPPED:
            m.skipped_actions += 1
        m.total_actions += 1

    # ---- finalization ----

    async def _finalize_execution(self, success: bool):
        self._log_phase("🏁 Finalizing Execution")
        await self._save_execution_state()
        self._log_success("✅ Execution finalized successfully")

    async def _save_execution_state(self):
        state = {
            "execution_id": self.context.execution_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "plan": {
                "contract_path": self.context.plan.contract_path,
                "environment": self.context.plan.environment,
                "phases": [
                    {
                        "phase": phase.phase.value,
                        "status": phase.status.value,
                        "start_time": phase.start_time.isoformat() if phase.start_time else None,
                        "end_time": phase.end_time.isoformat() if phase.end_time else None,
                        "actions": [
                            {
                                "id": a.id,
                                "status": a.status.value,
                                "start_time": a.start_time.isoformat() if a.start_time else None,
                                "end_time": a.end_time.isoformat() if a.end_time else None,
                                "error": a.error,
                            }
                            for a in phase.actions
                        ],
                    }
                    for phase in self.context.plan.phases
                ],
            },
            "metrics": {
                "total_actions": self.context.metrics.total_actions,
                "successful_actions": self.context.metrics.successful_actions,
                "failed_actions": self.context.metrics.failed_actions,
                "skipped_actions": self.context.metrics.skipped_actions,
                "total_duration_seconds": self.context.metrics.total_duration_seconds,
                "phase_durations": self.context.metrics.phase_durations,
            },
        }
        self.context.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.context.state_file, "w") as f:
            json.dump(state, f, indent=2)

    async def _handle_execution_failure(self, error: Exception):
        self._log_error(f"💥 Execution failed: {error}")
        if self.context.plan.rollback_strategy != RollbackStrategy.NONE:
            await self._execute_rollback()
        await self._save_execution_state()

    def _build_execution_summary(self, success: bool, error: str = None) -> Dict[str, Any]:
        return {
            "execution_id": self.context.execution_id,
            "success": success,
            "error": error,
            "metrics": {
                "total_actions": self.context.metrics.total_actions,
                "successful_actions": self.context.metrics.successful_actions,
                "failed_actions": self.context.metrics.failed_actions,
                "skipped_actions": self.context.metrics.skipped_actions,
                "total_duration_seconds": self.context.metrics.total_duration_seconds,
                "phase_durations": self.context.metrics.phase_durations,
            },
            "phases": [
                {
                    "phase": p.phase.value,
                    "status": p.status.value,
                    "action_count": len(p.actions),
                    "duration": self.context.metrics.phase_durations.get(p.phase.value, 0),
                }
                for p in self.context.plan.phases
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ---- logging helpers ----

    def _log_execution_start(self):
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(
                Panel(
                    f"🌊 Starting FLUID Data Product Deployment\n"
                    f"📋 Execution ID: {self.context.execution_id}\n"
                    f"📁 Contract: {self.context.plan.contract_path}\n"
                    f"🌍 Environment: {self.context.plan.environment or 'default'}\n"
                    f"📊 Total Phases: {len(self.context.plan.phases)}",
                    title="🚀 FLUID Apply Engine",
                    border_style="blue",
                )
            )
        else:
            self.context.logger.info(f"🌊 Starting FLUID deployment: {self.context.execution_id}")

    def _log_phase(self, message: str):
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(f"\n{message}", style="bold blue")
        else:
            self.context.logger.info(message)

    def _log_action_start(self, action: ExecutionAction):
        msg = f"  🔄 {action.description} ({action.provider})"
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(msg, style="yellow")
        else:
            self.context.logger.info(msg)

    def _log_action_success(self, action: ExecutionAction):
        dur = (
            (action.end_time - action.start_time).total_seconds()
            if action.end_time and action.start_time
            else 0
        )
        msg = f"  ✅ {action.description} completed in {dur:.2f}s"
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(msg, style="green")
        else:
            self.context.logger.info(msg)

    def _log_success(self, message: str):
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(message, style="green")
        else:
            self.context.logger.info(message)

    def _log_info(self, message: str):
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(message, style="blue")
        else:
            self.context.logger.info(message)

    def _log_warning(self, message: str):
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(message, style="yellow")
        else:
            self.context.logger.warning(message)

    def _log_error(self, message: str):
        if self.context.console and RICH_AVAILABLE:
            self.context.console.print(message, style="red")
        else:
            self.context.logger.error(message)


# ==========================================
# Plan Generation & Contract Analysis
# ==========================================


class FluidPlanGenerator:
    """
    Intelligent plan generator that analyzes FLUID contracts and creates
    comprehensive execution plans with proper dependency resolution.
    """

    def __init__(self, contract: Dict[str, Any], environment: Optional[str] = None):
        self.contract = contract
        self.environment = environment
        self.detected_providers: set[str] = set()
        self.resource_dependencies: Dict[str, Any] = {}

    def generate_execution_plan(self, contract_path: str) -> ExecutionPlan:
        self._analyze_contract()
        phases = self._generate_phases()
        return ExecutionPlan(
            contract_path=contract_path,
            environment=self.environment,
            phases=phases,
            global_timeout_minutes=self._calculate_total_timeout(),
            rollback_strategy=self._determine_rollback_strategy(),
            metadata=self._generate_plan_metadata(),
        )

    # ---- contract analysis ----

    def _analyze_contract(self):
        if "infrastructure" in self.contract:
            self.detected_providers.add("terraform")
            infra_str = str(self.contract["infrastructure"]).lower()
            if "gcp" in infra_str:
                self.detected_providers.add("gcp")
            if "aws" in infra_str:
                self.detected_providers.add("aws")
        if "transformations" in self.contract or "models" in self.contract:
            self.detected_providers.add("dbt")
        if "sources" in self.contract or "ingestion" in self.contract:
            self.detected_providers.add("airbyte")
        if "monitoring" in self.contract or "alerts" in self.contract:
            self.detected_providers.add("datadog")

    # ---- phase generation ----

    def _generate_phases(self) -> List[PhaseExecution]:
        phases: List[PhaseExecution] = []
        if "terraform" in self.detected_providers or "gcp" in self.detected_providers:
            phases.append(self._create_infrastructure_phase())
        if "sources" in self.contract:
            phases.append(self._create_ingestion_phase())
        if any(k in self.contract for k in ("transformations", "models", "build", "builds")):
            phases.append(self._create_transformation_phase())
        if "quality" in self.contract or "quality_expectations" in self.contract:
            phases.append(self._create_quality_gates_phase())
        if "governance" in self.contract or "governance_policies" in self.contract:
            phases.append(self._create_governance_phase())
        if "monitoring" in self.contract:
            phases.append(self._create_monitoring_phase())
        return phases

    # ---- individual phase creators (infrastructure, ingestion, etc.) ----

    def _create_infrastructure_phase(self) -> PhaseExecution:
        actions: List[ExecutionAction] = []
        if "infrastructure" in self.contract:
            actions.append(
                ExecutionAction(
                    id="provision_infrastructure",
                    phase=ExecutionPhase.INFRASTRUCTURE,
                    provider="terraform",
                    operation="apply",
                    description="Provision cloud infrastructure",
                    timeout_seconds=1800,
                    rollback_operation="destroy",
                    metadata={
                        "terraform_config": self.contract.get("infrastructure", {}),
                        "environment": self.environment,
                    },
                )
            )
        actions.append(
            ExecutionAction(
                id="configure_networking",
                phase=ExecutionPhase.INFRASTRUCTURE,
                provider="gcp",
                operation="configure_network",
                description="Configure network and security policies",
                dependencies=(
                    ["provision_infrastructure"]
                    if any(a.id == "provision_infrastructure" for a in actions)
                    else []
                ),
                timeout_seconds=600,
                rollback_operation="cleanup_network",
                metadata={"network_config": self.contract.get("network", {})},
            )
        )
        actions.append(
            ExecutionAction(
                id="configure_iam",
                phase=ExecutionPhase.INFRASTRUCTURE,
                provider="gcp",
                operation="configure_iam",
                description="Configure IAM roles and permissions",
                dependencies=["configure_networking"],
                timeout_seconds=300,
                rollback_operation="cleanup_iam",
                metadata={"iam_config": self.contract.get("iam", {})},
            )
        )
        return PhaseExecution(
            phase=ExecutionPhase.INFRASTRUCTURE,
            actions=actions,
            parallel_execution=False,
            continue_on_error=False,
            rollback_strategy=RollbackStrategy.IMMEDIATE,
        )

    def _create_ingestion_phase(self) -> PhaseExecution:
        actions: List[ExecutionAction] = []
        for i, source in enumerate(self.contract.get("sources", [])):
            src_name = source.get("name", f"source_{i}")
            actions.append(
                ExecutionAction(
                    id=f"ingest_source_{i}",
                    phase=ExecutionPhase.DATA_INGESTION,
                    provider="airbyte",
                    operation="create_connection",
                    description=f"Setup ingestion for {src_name}",
                    timeout_seconds=900,
                    rollback_operation="delete_connection",
                    metadata={
                        "source_config": source,
                        "destination": self.contract.get("destination", {}),
                        "sync_mode": source.get("sync_mode", "full_refresh"),
                    },
                )
            )
            actions.append(
                ExecutionAction(
                    id=f"sync_source_{i}",
                    phase=ExecutionPhase.DATA_INGESTION,
                    provider="airbyte",
                    operation="trigger_sync",
                    description=f"Sync data from {src_name}",
                    dependencies=[f"ingest_source_{i}"],
                    timeout_seconds=3600,
                    metadata={"connection_id": f"ingest_source_{i}", "wait_for_completion": True},
                )
            )
        return PhaseExecution(
            phase=ExecutionPhase.DATA_INGESTION,
            actions=actions,
            parallel_execution=True,
            continue_on_error=True,
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )

    def _create_transformation_phase(self) -> PhaseExecution:
        actions: List[ExecutionAction] = []
        has_dbt = "dbt_project_dir" in self.contract or "models" in self.contract
        if has_dbt:
            proj_dir = self.contract.get("dbt_project_dir", ".")
            actions.extend(
                [
                    ExecutionAction(
                        id="install_dbt_dependencies",
                        phase=ExecutionPhase.TRANSFORMATION,
                        provider="dbt",
                        operation="deps",
                        description="Install dbt dependencies",
                        timeout_seconds=300,
                        metadata={"project_dir": proj_dir},
                    ),
                    ExecutionAction(
                        id="run_dbt_seed",
                        phase=ExecutionPhase.TRANSFORMATION,
                        provider="dbt",
                        operation="seed",
                        description="Load seed data",
                        dependencies=["install_dbt_dependencies"],
                        timeout_seconds=600,
                        metadata={"project_dir": proj_dir},
                    ),
                    ExecutionAction(
                        id="run_dbt_models",
                        phase=ExecutionPhase.TRANSFORMATION,
                        provider="dbt",
                        operation="run",
                        description="Execute dbt transformations",
                        dependencies=["run_dbt_seed"],
                        timeout_seconds=3600,
                        metadata={
                            "project_dir": proj_dir,
                            "models": self.contract.get("models", []),
                            "environment": self.environment,
                        },
                    ),
                    ExecutionAction(
                        id="test_dbt_models",
                        phase=ExecutionPhase.TRANSFORMATION,
                        provider="dbt",
                        operation="test",
                        description="Run dbt tests",
                        dependencies=["run_dbt_models"],
                        timeout_seconds=1800,
                        metadata={"project_dir": proj_dir},
                    ),
                ]
            )
        else:
            try:
                from ..util.contract import get_primary_build

                build = get_primary_build(self.contract)
            except Exception:
                build = None
            if build:
                build_type = build.get("engine", "sql")
                actions.append(
                    ExecutionAction(
                        id="execute_build_transformation",
                        phase=ExecutionPhase.TRANSFORMATION,
                        provider="local",
                        operation="execute_build",
                        description=f"Execute {build_type} transformation",
                        timeout_seconds=3600,
                        metadata={"build": build, "environment": self.environment},
                    )
                )
        return PhaseExecution(
            phase=ExecutionPhase.TRANSFORMATION,
            actions=actions,
            parallel_execution=False,
            continue_on_error=False,
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )

    def _create_quality_gates_phase(self) -> PhaseExecution:
        actions = [
            ExecutionAction(
                id="data_quality_checks",
                phase=ExecutionPhase.QUALITY_GATES,
                provider="great_expectations",
                operation="run_checkpoint",
                description="Execute data quality validations",
                timeout_seconds=1800,
                metadata={
                    "expectations": self.contract.get("quality_expectations", []),
                    "checkpoints": self.contract.get("quality_checkpoints", []),
                },
            ),
            ExecutionAction(
                id="performance_tests",
                phase=ExecutionPhase.QUALITY_GATES,
                provider="builtin",
                operation="run_performance_tests",
                description="Execute performance benchmarks",
                timeout_seconds=900,
                metadata={
                    "performance_thresholds": self.contract.get("performance", {}),
                    "test_queries": self.contract.get("performance_tests", []),
                },
            ),
            ExecutionAction(
                id="security_scan",
                phase=ExecutionPhase.QUALITY_GATES,
                provider="builtin",
                operation="run_security_scan",
                description="Execute security compliance scan",
                timeout_seconds=600,
                metadata={
                    "security_policies": self.contract.get("security", {}),
                    "compliance_frameworks": self.contract.get("compliance", []),
                },
            ),
        ]
        return PhaseExecution(
            phase=ExecutionPhase.QUALITY_GATES,
            actions=actions,
            parallel_execution=True,
            continue_on_error=False,
            rollback_strategy=RollbackStrategy.FULL_ROLLBACK,
        )

    def _create_governance_phase(self) -> PhaseExecution:
        actions = [
            ExecutionAction(
                id="apply_data_policies",
                phase=ExecutionPhase.GOVERNANCE,
                provider="apache_ranger",
                operation="apply_policies",
                description="Apply data governance policies",
                timeout_seconds=600,
                rollback_operation="remove_policies",
                metadata={
                    "policies": self.contract.get("governance_policies", []),
                    "data_classification": self.contract.get("data_classification", {}),
                },
            ),
            ExecutionAction(
                id="setup_lineage_tracking",
                phase=ExecutionPhase.GOVERNANCE,
                provider="apache_atlas",
                operation="register_lineage",
                description="Register data lineage information",
                timeout_seconds=300,
                metadata={
                    "lineage": self.contract.get("lineage", {}),
                    "metadata": self.contract.get("metadata", {}),
                },
            ),
            ExecutionAction(
                id="configure_privacy_controls",
                phase=ExecutionPhase.GOVERNANCE,
                provider="privacera",
                operation="configure_privacy",
                description="Configure privacy and compliance controls",
                timeout_seconds=400,
                rollback_operation="remove_privacy_controls",
                metadata={
                    "privacy_policies": self.contract.get("privacy", {}),
                    "anonymization_rules": self.contract.get("anonymization", []),
                },
            ),
        ]
        return PhaseExecution(
            phase=ExecutionPhase.GOVERNANCE,
            actions=actions,
            parallel_execution=True,
            continue_on_error=True,
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )

    def _create_monitoring_phase(self) -> PhaseExecution:
        actions = [
            ExecutionAction(
                id="setup_data_monitoring",
                phase=ExecutionPhase.MONITORING,
                provider="datadog",
                operation="create_monitors",
                description="Setup data quality monitoring",
                timeout_seconds=300,
                rollback_operation="delete_monitors",
                metadata={
                    "monitors": self.contract.get("monitoring", {}),
                    "alerts": self.contract.get("alerts", []),
                },
            ),
            ExecutionAction(
                id="configure_dashboards",
                phase=ExecutionPhase.MONITORING,
                provider="grafana",
                operation="create_dashboards",
                description="Configure monitoring dashboards",
                dependencies=["setup_data_monitoring"],
                timeout_seconds=180,
                rollback_operation="delete_dashboards",
                metadata={
                    "dashboards": self.contract.get("dashboards", []),
                    "metrics": self.contract.get("metrics", {}),
                },
            ),
            ExecutionAction(
                id="setup_log_aggregation",
                phase=ExecutionPhase.MONITORING,
                provider="elastic",
                operation="configure_logging",
                description="Configure log aggregation and analysis",
                timeout_seconds=240,
                rollback_operation="cleanup_logging",
                metadata={
                    "log_config": self.contract.get("logging", {}),
                    "log_retention": self.contract.get("log_retention", "30d"),
                },
            ),
        ]
        return PhaseExecution(
            phase=ExecutionPhase.MONITORING,
            actions=actions,
            parallel_execution=False,
            continue_on_error=True,
            rollback_strategy=RollbackStrategy.PHASE_COMPLETE,
        )

    # ---- helpers ----

    def _extract_required_resources(self) -> List[str]:
        resources: List[str] = []
        if "infrastructure" in self.contract:
            resources.extend(self.contract["infrastructure"].get("resources", []))
        for source in self.contract.get("sources", []):
            resources.append(f"source:{source.get('name', 'unknown')}")
        for dest in self.contract.get("destinations", []):
            resources.append(f"destination:{dest.get('name', 'unknown')}")
        return resources

    def _calculate_total_timeout(self) -> int:
        base = 120
        if "infrastructure" in self.contract:
            base += 30
        if "transformations" in self.contract:
            base += min(len(self.contract.get("models", [])) * 2, 60)
        return base

    def _determine_rollback_strategy(self) -> RollbackStrategy:
        if self.environment in ("prod", "production"):
            return RollbackStrategy.IMMEDIATE
        return RollbackStrategy.PHASE_COMPLETE

    def _generate_plan_metadata(self) -> Dict[str, Any]:
        return {
            "contract_version": self.contract.get("version", "unknown"),
            "detected_providers": list(self.detected_providers),
            "environment": self.environment,
            "resource_count": len(self._extract_required_resources()),
            "estimated_duration_minutes": self._calculate_total_timeout(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
