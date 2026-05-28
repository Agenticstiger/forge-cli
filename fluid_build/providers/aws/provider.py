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

# fluid_build/providers/aws/provider.py
"""
Production-grade AWS Provider for FLUID Build.

Implements comprehensive AWS integration across S3, Glue, Athena, Redshift,
EventBridge, Lambda, and more. Supports planning, idempotent application,
and rich error reporting with proper auth handling.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fluid_build.providers.base import (
    ApplyResult,
    BaseProvider,
    ProviderError,
)

from .plan.planner import plan_actions
from .util.auth import get_auth_report
from .util.circuit_breaker import CircuitBreakerOpenError, get_circuit_breaker
from .util.dependencies import order_actions_by_dependencies
from .util.logging import redact_dict
from .util.validation import ResourceValidator, validate_actions_strict


class AwsProvider(BaseProvider):
    """
    Production AWS provider with comprehensive service support.

    Features:
    - Complete S3 integration (buckets, lifecycle policies, versioning)
    - AWS Glue Data Catalog (databases, tables, crawlers)
    - Amazon Athena query execution
    - Amazon Redshift data warehousing
    - AWS Lambda function deployment
    - EventBridge scheduling and events
    - IAM policy compilation and binding
    - Comprehensive monitoring and error handling
    """

    name = "aws"

    @classmethod
    def get_provider_info(cls):
        from fluid_build.providers.base import ProviderMetadata

        return ProviderMetadata(
            name="aws",
            display_name="Amazon Web Services",
            description="Production AWS provider — S3, Glue, Athena, Redshift, Lambda, EventBridge, IAM",
            version="0.7.1",
            author="Agentics AI / DustLabs",
            supported_platforms=["aws", "s3", "redshift", "athena", "glue"],
            tags=["aws", "cloud", "s3", "glue", "athena", "redshift"],
        )

    def __init__(
        self,
        *,
        account_id: Optional[str] = None,
        region: Optional[str] = None,
        project: Optional[str] = None,  # Alias for account_id
        logger=None,
        **kwargs: Any,
    ) -> None:
        # Normalize account_id/project (for compatibility with GCP patterns)
        account_id = account_id or project

        # Import auth utilities to validate configuration early
        from .util.config import resolve_account_and_region

        self.account_id, self.region = resolve_account_and_region(account_id, region)

        super().__init__(project=account_id, region=self.region, logger=logger, **kwargs)

        self.info_kv(
            event="provider_initialized",
            provider="aws",
            account_id=self.account_id,
            region=self.region,
        )

    def capabilities(self) -> Mapping[str, bool]:
        """Advertise comprehensive AWS provider capabilities."""
        return {
            "planning": True,
            "apply": True,
            "render": True,  # OPDS export support
            "graph": True,  # Resource dependency graphing
            "auth": True,  # Auth context reporting
        }

    # ── Rollback surface ────────────────────────────────────────────

    def restore_ddl(self, snapshot: Mapping[str, Any]) -> List[str]:
        """AWS rollback uses S3 prefix-copy semantics, not SQL DDL.

        Returns an empty list — the rollback CLI's S3 path handles the
        prefix copy directly via ``aws.s3.copy_prefix``. The empty
        list signals the rollback CLI to use the per-provider S3
        executor instead of running SQL. Redshift snapshots route to
        the dedicated :class:`RedshiftProvider` which does emit DDL.
        """
        return []

    def cleanup_backups(self, snapshots: List[Mapping[str, Any]]) -> None:
        """Delete S3 backup prefixes for aged-out snapshots."""
        if not snapshots:
            return
        try:
            import boto3  # type: ignore
        except Exception:  # pragma: no cover
            return
        s3 = boto3.client("s3")
        for snap in snapshots:
            loc = snap.get("location") or {}
            bucket = loc.get("database")  # bucket stored in ``database`` key
            backup_prefix = loc.get("backup_table") or snap.get("backup_name")
            if not (bucket and backup_prefix):
                continue
            try:
                paginator = s3.get_paginator("list_objects_v2")
                pages = paginator.paginate(Bucket=bucket, Prefix=f"{backup_prefix}/")
                for page in pages:
                    objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if objects:
                        s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
            except Exception as exc:  # noqa: BLE001
                self.warn_kv(
                    event="backup_cleanup_drop_failed",
                    location=f"s3://{bucket}/{backup_prefix}",
                    error=str(exc),
                )

    def plan(
        self,
        contract: Mapping[str, Any],
        *,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate AWS actions from FLUID contract.

        ``mode`` is the apply-time mode (``replace`` /
        ``replace-and-build`` trigger pre-flight S3 prefix-copy backups).
        Converts contract specifications into concrete AWS resource
        operations: S3 buckets, Glue databases / tables / crawlers,
        Athena queries / workgroups, Redshift schemas, Lambda functions,
        EventBridge rules / schedules, IAM roles / policies.
        """
        self.debug_kv(
            event="plan_started",
            contract_id=contract.get("id"),
            contract_name=contract.get("name"),
            mode=mode,
        )

        try:
            # Validate sovereignty constraints (FLUID 0.7.1)
            self._validate_sovereignty(contract)

            # ``plan_actions`` constructs an :class:`AwsPlanner` and
            # calls :meth:`BasePlanner.plan` which already runs the
            # 6-phase scaffold (infrastructure / IAM / replace-snapshots
            # / expose / build / schedule). Snapshots emit only when
            # ``mode`` is destructive — :func:`is_destructive_mode`
            # gates them inside :meth:`BasePlanner.plan`.
            actions = plan_actions(contract, self.account_id, self.region, self.logger, mode=mode)

            # Add orchestration actions (FLUID 0.7.1)
            orchestration_actions = self._plan_orchestration(contract)
            actions.extend(orchestration_actions)

            # Add schedule actions (FLUID 0.7.1)
            schedule_actions = self._plan_schedule(contract)
            actions.extend(schedule_actions)

            # Order actions by dependencies
            actions = order_actions_by_dependencies(actions)

            # Validate actions before returning
            validator = ResourceValidator(self.account_id, self.region)
            validation_result = validator.validate_actions(actions)

            if not validation_result["valid"]:
                error_msg = "Plan validation failed:\n" + "\n".join(validation_result["errors"])
                raise ProviderError(error_msg)

            # Log warnings if any
            for warning in validation_result.get("warnings", []):
                self.warn_kv(event="validation_warning", message=warning)

            self.info_kv(
                event="plan_completed",
                contract_id=contract.get("id"),
                actions_count=len(actions),
                validated=True,
                resource_counts=validation_result.get("resource_counts", {}),
            )

            return actions

        except ProviderError:
            # Re-raise ProviderError as-is
            raise
        except Exception as e:
            self.err_kv(event="plan_failed", contract_id=contract.get("id"), error=str(e))
            # Wrap all other exceptions in ProviderError
            raise ProviderError(f"Failed to plan AWS deployment: {e}") from e

    def apply(self, actions: List[Dict[str, Any]], **kwargs: Any) -> ApplyResult:
        """Native AWS apply is retired — AWS uses the OpenTofu engine.

        AWS was cut over to the OpenTofu autogenerator (AUTOGEN_SPIKE.md):
        ``fluid apply`` compiles the contract to ``.tf.json`` and runs
        ``tofu``. The native per-service apply path was removed.
        """
        raise ProviderError(
            "native AWS apply is retired — AWS uses the OpenTofu engine; "
            "`fluid apply` provisions it automatically"
        )

    def render(
        self,
        src: Mapping[str, Any] | List[Mapping[str, Any]],
        *,
        out: Optional[str] = None,
        fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Export FLUID contracts to external formats.

        Supported formats:
        - 'opds': Open Data Product Standard JSON
        - 'dot': GraphViz dependency graph
        """
        if fmt == "opds":
            from .plan.export import export_opds

            return export_opds(src)
        elif fmt == "dot":
            from .plan.export import export_dot_graph

            return export_dot_graph(src)
        else:
            raise ProviderError(f"Unsupported render format: {fmt}. Supported: opds, dot")

    def auth_report(self) -> Dict[str, Any]:
        """Generate authentication and environment report for diagnostics."""
        report = get_auth_report(self.account_id, self.region)
        # Ensure status is set if there's an error
        if "error" in report and "status" not in report:
            report["status"] = "error"
        return report

    def _validate_sovereignty(self, contract: Mapping[str, Any]) -> None:
        """
        Validate sovereignty constraints (FLUID 0.7.1).

        Ensures AWS region matches jurisdiction and data residency requirements.
        """
        sovereignty = contract.get("sovereignty")
        if not sovereignty:
            return

        from .util.sovereignty import SovereigntyViolationError, validate_sovereignty

        # Build binding from provider configuration
        binding = {"location": {"region": self.region}}

        try:
            validate_sovereignty(contract, binding)
            self.info_kv(
                event="sovereignty_validated",
                region=self.region,
                jurisdiction=sovereignty.get("jurisdiction"),
                data_residency=sovereignty.get("dataResidency"),
            )
        except SovereigntyViolationError as e:
            self.err_kv(event="sovereignty_violation", error=str(e))
            raise ProviderError(str(e)) from e

    def _plan_orchestration(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Plan orchestration tasks from contract (FLUID 0.7.1).

        Parses orchestration.tasks with type: provider_action and
        converts them to AWS provider actions.
        """
        orchestration = contract.get("orchestration")
        if not orchestration:
            return []

        from .plan.orchestration import OrchestrationError, plan_orchestration_tasks

        try:
            actions = plan_orchestration_tasks(contract, self.account_id, self.region, self.logger)

            if actions:
                self.info_kv(
                    event="orchestration_planned",
                    task_count=len(actions),
                    engine=orchestration.get("engine"),
                )

            return actions

        except OrchestrationError as e:
            self.err_kv(event="orchestration_planning_failed", error=str(e))
            raise ProviderError(f"Orchestration planning failed: {e}") from e

    def _plan_schedule(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Plan scheduling actions from contract (FLUID 0.7.1).

        Parses orchestration.schedule and orchestration.triggers to create
        AWS scheduling infrastructure (EventBridge, MWAA, Lambda).
        """
        orchestration = contract.get("orchestration")
        if not orchestration:
            return []

        # Only plan schedules if schedule or triggers are present
        if not orchestration.get("schedule") and not orchestration.get("triggers"):
            return []

        from .plan.schedule import plan_schedule_actions

        try:
            actions = plan_schedule_actions(contract, self.account_id, self.region, self.logger)

            if actions:
                self.info_kv(
                    event="schedule_planned",
                    action_count=len(actions),
                    has_schedule=bool(orchestration.get("schedule")),
                    has_triggers=bool(orchestration.get("triggers")),
                )

            return actions

        except Exception as e:
            self.err_kv(event="schedule_planning_failed", error=str(e))
            raise ProviderError(f"Schedule planning failed: {e}") from e

    def _utc_timestamp(self) -> str:
        """Generate UTC timestamp string."""
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def export(
        self,
        contract: Mapping[str, Any],
        engine: str = "airflow",
        output_dir: str = ".",
        **kwargs: Any,
    ) -> str:
        """
        Export contract as executable DAG/pipeline code.

        Generates ready-to-run orchestration code for the specified engine.
        Supports Airflow, Dagster, and Prefect workflows.

        Args:
            contract: FLUID contract with orchestration section
            engine: Target orchestration engine ("airflow", "dagster", "prefect")
            output_dir: Directory to write generated file (default: current directory)
            **kwargs: Additional parameters for code generation

        Returns:
            Path to generated file

        Raises:
            ProviderError: If export fails or engine is unsupported

        Example:
            >>> provider = AwsProvider(account_id="YOUR_AWS_ACCOUNT_ID", region="us-east-1")
            >>> dag_file = provider.export(contract, engine="airflow", output_dir="./dags")
            >>> print(f"Generated: {dag_file}")
        """
        import os

        from fluid_build.providers.common.codegen_utils import (
            detect_circular_dependencies,
            validate_contract_for_export,
        )

        # Validate contract structure
        try:
            validate_contract_for_export(contract)
        except ValueError as e:
            raise ProviderError(f"Invalid contract: {e}") from e

        # Check for circular dependencies
        tasks = contract["orchestration"]["tasks"]
        cycles = detect_circular_dependencies(tasks)
        if cycles:
            raise ProviderError(f"Circular dependencies detected in tasks: {', '.join(cycles)}")

        orchestration = contract.get("orchestration")
        if not orchestration:
            raise ProviderError("Contract missing orchestration section - cannot export DAG")

        contract_id = contract.get("id", "unnamed")
        # Sanitize contract_id for safe use in filenames (prevent path traversal)
        import re

        safe_id = re.sub(r"[^a-zA-Z0-9_\-.]", "_", contract_id)

        self.info_kv(
            event="export_started", contract_id=contract_id, engine=engine, output_dir=output_dir
        )

        try:
            # Generate code based on engine
            if engine == "airflow" or engine == "mwaa":
                from .codegen import generate_airflow_dag

                code = generate_airflow_dag(contract, self.account_id, self.region)
                filename = f"{safe_id}_dag.py"

            elif engine == "dagster":
                from .codegen import generate_dagster_pipeline

                code = generate_dagster_pipeline(contract, self.account_id, self.region)
                filename = f"{safe_id}_pipeline.py"

            elif engine == "prefect":
                from .codegen import generate_prefect_flow

                code = generate_prefect_flow(contract, self.account_id, self.region)
                filename = f"{safe_id}_flow.py"

            else:
                raise ProviderError(
                    f"Unsupported orchestration engine: {engine}. "
                    f"Supported: airflow, dagster, prefect"
                )

            # Ensure output directory exists
            os.makedirs(output_dir, exist_ok=True)

            # Write generated code to file
            output_path = os.path.join(output_dir, filename)
            with open(output_path, "w") as f:
                f.write(code)

            self.info_kv(
                event="export_completed",
                contract_id=contract_id,
                engine=engine,
                output_file=output_path,
                code_lines=len(code.splitlines()),
            )

            return output_path

        except ProviderError:
            raise
        except Exception as e:
            self.err_kv(event="export_failed", contract_id=contract_id, engine=engine, error=str(e))
            raise ProviderError(f"Failed to export {engine} DAG: {e}") from e
