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

# fluid_build/providers/gcp/provider.py
"""
Production-grade GCP Provider for FLUID Build.

Implements comprehensive GCP integration across BigQuery, Cloud Storage,
Pub/Sub, Cloud Composer, Dataflow, and more. Supports planning, idempotent
application, and rich error reporting with proper auth handling.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fluid_build.providers.base import (
    ApplyResult,
    BaseProvider,
    ProviderError,
)

from .plan.planner import plan_actions
from .util.auth import get_auth_report


class GcpProvider(BaseProvider):
    """
    Production GCP provider with comprehensive service support.

    Features:
    - Complete BigQuery integration (datasets, tables, views, routines)
    - Cloud Storage lifecycle management
    - Pub/Sub messaging infrastructure
    - Cloud Composer DAG deployment
    - Dataflow pipeline orchestration
    - IAM policy compilation and binding
    - Comprehensive monitoring and error handling
    """

    name = "gcp"

    @classmethod
    def get_provider_info(cls):
        from fluid_build.providers.base import ProviderMetadata

        return ProviderMetadata(
            name="gcp",
            display_name="Google Cloud Platform",
            description="Production GCP provider — BigQuery, Cloud Storage, Pub/Sub, Composer, Dataflow, IAM",
            version="0.7.1",
            author="Agentics AI / DustLabs",
            supported_platforms=["gcp", "bigquery", "gcs", "pubsub"],
            tags=["gcp", "cloud", "bigquery", "gcs", "pubsub", "dataflow"],
        )

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        region: Optional[str] = "us-central1",
        logger=None,
        **kwargs: Any,
    ) -> None:
        super().__init__(project=project, region=region, logger=logger, **kwargs)

        # Import auth utilities to validate configuration early
        from .util.config import resolve_project_and_region

        self.project, self.region = resolve_project_and_region(project, region)

        self.info_kv(
            event="provider_initialized", provider="gcp", project=self.project, region=self.region
        )

    def capabilities(self) -> Mapping[str, bool]:
        """Advertise comprehensive GCP provider capabilities."""
        return {
            "planning": True,
            "apply": True,
            "render": True,  # OPDS export support
            "graph": True,  # Resource dependency graphing
            "auth": True,  # Auth context reporting
        }

    def restore_ddl(self, snapshot: Mapping[str, Any]) -> List[str]:
        """BigQuery restore via CTAS (BigQuery has no CLONE).

        ``CREATE OR REPLACE TABLE <orig> AS SELECT * FROM <backup>``
        is atomic. Storage cost applies (the backup is a real copy
        of the data, not a metadata pointer).

        Every component of both fully-qualified names is validated
        before interpolation: the project component via the GCP
        project-ID shape check, and the dataset / table components via
        ``_sql_safety.validate_ident``. Invalid snapshot metadata yields
        an empty DDL list rather than an unsafe statement.
        """
        from .plan.planner import _validated_bq_fqn

        location = snapshot.get("location") or {}
        db = location.get("database")
        sch = location.get("schema")
        tbl = location.get("table")
        backup = location.get("backup_table") or snapshot.get("backup_name")
        if not (db and sch and tbl and backup):
            return []
        try:
            orig_fqn = _validated_bq_fqn(db, sch, tbl)
            backup_fqn = _validated_bq_fqn(db, sch, backup)
        except ValueError as exc:
            self.warn_kv(event="restore_ddl_invalid_identifier", error=str(exc))
            return []
        return [f"CREATE OR REPLACE TABLE {orig_fqn} AS SELECT * FROM {backup_fqn}"]

    def cleanup_backups(self, snapshots: List[Mapping[str, Any]]) -> None:
        """Drop BigQuery backup tables (best-effort)."""
        if not snapshots:
            return
        try:
            from google.cloud import bigquery  # type: ignore
        except Exception:  # pragma: no cover
            return
        client = bigquery.Client(project=self.project)
        for snap in snapshots:
            loc = snap.get("location") or {}
            db = loc.get("database")
            sch = loc.get("schema")
            backup = loc.get("backup_table") or snap.get("backup_name")
            if not (db and sch and backup):
                continue
            try:
                client.delete_table(f"{db}.{sch}.{backup}", not_found_ok=True)
            except Exception as exc:  # noqa: BLE001
                self.warn_kv(
                    event="backup_cleanup_drop_failed",
                    table=f"{db}.{sch}.{backup}",
                    error=str(exc),
                )

    def plan(
        self,
        contract: Mapping[str, Any],
        *,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate GCP actions from FLUID contract.

        Converts contract specifications into concrete GCP resource operations:
        - BigQuery datasets, tables, views
        - GCS buckets and lifecycle policies
        - Pub/Sub topics and subscriptions
        - Composer DAGs and schedules
        - IAM policy bindings

        The optional ``mode`` argument carries the apply-time mode.
        For destructive modes (``replace`` / ``replace-and-build``)
        the BigQuery actions emit ``CREATE OR REPLACE TABLE … AS SELECT``
        instead of additive ``INSERT INTO``, with a pre-flight backup
        CTAS for rollback.
        """
        self.debug_kv(
            event="plan_started",
            contract_id=contract.get("id"),
            contract_name=contract.get("name"),
            mode=mode,
        )

        try:
            try:
                actions = plan_actions(contract, self.project, self.region, self.logger, mode=mode)
            except TypeError:
                # Older planner signature without mode kwarg.
                actions = plan_actions(contract, self.project, self.region, self.logger)

            self.info_kv(
                event="plan_completed",
                contract_id=contract.get("id"),
                actions_count=len(actions),
                mode=mode,
            )

            return actions

        except Exception as e:
            self.err_kv(event="plan_failed", contract_id=contract.get("id"), error=str(e))
            raise ProviderError(f"Failed to plan GCP deployment: {e}") from e

    def apply(self, actions: List[Dict[str, Any]], **kwargs: Any) -> ApplyResult:
        """Native GCP apply is retired — GCP uses the OpenTofu engine.

        GCP was cut over to the OpenTofu autogenerator (see
        ``AUTOGEN_SPIKE.md``): ``fluid apply`` compiles the contract to
        ``.tf.json`` and runs ``tofu``. The native per-service apply
        path was removed.
        """
        raise ProviderError(
            "native GCP apply is retired — GCP uses the OpenTofu engine; "
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
        try:
            return get_auth_report(self.project, self.region)
        except Exception as e:
            return {"status": "error", "error": str(e), "provider": "gcp"}

    def apply_policy(self, policy_data: Dict[str, Any], mode: str = "check") -> Dict[str, Any]:
        """Report IAM bindings — GCP IAM is provisioned declaratively.

        forge-cli compiles ``metadata.policies`` to OpenTofu IAM resources
        (BigQuery dataset ``access`` entries, ``google_bigquery_table_iam_member``
        and ``google_storage_bucket_iam_member``); ``fluid apply`` provisions
        them via ``tofu``. This stage no longer mutates GCP IAM through the
        google-cloud SDK — it reports the compiled bindings for visibility in
        both ``check`` and ``enforce`` modes.
        """
        bindings = policy_data.get("bindings", [])
        results = []
        for binding in bindings:
            resource_type = binding.get("resource_type", "")
            principal = binding.get("principal", "")
            roles = binding.get("roles", [])
            target = (
                binding.get("dataset")
                or binding.get("bucket")
                or binding.get("project")
                or resource_type
            )
            results.append(f"{resource_type} {target}: {roles} -> {principal}")
        return {
            "status": "ok",
            "mode": mode,
            "applied": 0,
            "bindings": len(bindings),
            "message": (
                "GCP IAM is provisioned declaratively by `fluid apply` (the "
                "OpenTofu engine) from metadata.policies; this stage no longer "
                "mutates IAM through the google-cloud SDK."
            ),
            "results": results,
        }

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
        Export contract as executable DAG/pipeline code for GCP.

        Generates ready-to-run orchestration code for the specified engine.
        Supports Airflow (Cloud Composer), Dagster, and Prefect workflows.

        Args:
            contract: FLUID contract with orchestration section
            engine: Target orchestration engine ("airflow", "dagster", "prefect")
            output_dir: Directory to write generated file (default: current directory)
            **kwargs: Additional parameters for code generation

        Returns:
            Path to generated file

        Raises:
            ProviderError: If export fails or engine is unsupported
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

        self.info_kv(
            event="export_started", contract_id=contract_id, engine=engine, output_dir=output_dir
        )

        # Sanitize contract_id for safe use in filenames (prevent path traversal)
        import re

        safe_id = re.sub(r"[^a-zA-Z0-9_\-.]", "_", contract_id)

        try:
            # Generate code based on engine
            if engine == "airflow" or engine == "composer":
                from .codegen import generate_airflow_dag

                code = generate_airflow_dag(contract, self.project, self.region)
                filename = f"{safe_id}_dag.py"

            elif engine == "dagster":
                from .codegen import generate_dagster_pipeline

                code = generate_dagster_pipeline(contract, self.project, self.region)
                filename = f"{safe_id}_pipeline.py"

            elif engine == "prefect":
                from .codegen import generate_prefect_flow

                code = generate_prefect_flow(contract, self.project, self.region)
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

            # Log success
            code_lines = code.count("\n") + 1
            file_size = len(code.encode("utf-8"))

            self.info_kv(
                event="export_completed",
                contract_id=contract_id,
                engine=engine,
                output_file=output_path,
                code_lines=code_lines,
                file_size=file_size,
            )

            return output_path

        except Exception as e:
            self.err_kv(event="export_failed", contract_id=contract_id, engine=engine, error=str(e))
            raise ProviderError(f"Export failed: {e}") from e
