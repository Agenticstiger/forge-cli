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

# fluid_build/providers/snowflake/provider_enhanced.py
"""
Production-grade Snowflake Provider for FLUID Build.

Implements comprehensive Snowflake integration with:
- Database, schema, and table management
- View and materialized view support
- Stored procedure and UDF management
- Stream and task orchestration
- Role-based access control (RBAC)
- Data sharing and secure views
- Performance monitoring and optimization
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderError

from .plan.planner import plan_actions
from .util.auth import get_auth_report
from .util.config import resolve_snowflake_settings
from .util.logging import redact_dict
from .util.retry import with_retry


class SnowflakeProviderEnhanced(BaseProvider):
    """
    Production Snowflake provider with comprehensive service support.

    Features:
    - Complete database/schema/table management
    - View and materialized view support
    - Stored procedures and UDFs
    - Stream processing and tasks
    - RBAC and data governance
    - Data sharing and secure views
    - Cost optimization and monitoring
    """

    name = "snowflake"

    @classmethod
    def get_provider_info(cls):
        from fluid_build.providers.base import ProviderMetadata

        return ProviderMetadata(
            name="snowflake",
            display_name="Snowflake",
            description="Production Snowflake provider — databases, schemas, tables, views, RBAC, data sharing",
            version="0.7.1",
            author="Agentics AI / DustLabs",
            supported_platforms=["snowflake"],
            tags=["snowflake", "cloud", "data-warehouse", "sql"],
        )

    def __init__(
        self,
        *,
        account: Optional[str] = None,
        warehouse: Optional[str] = None,
        database: Optional[str] = None,
        schema: Optional[str] = None,
        project: Optional[str] = None,  # Alias for database
        region: Optional[str] = None,  # Snowflake region
        logger=None,
        **kwargs: Any,
    ) -> None:
        # Normalize database/project
        database = database or project

        super().__init__(project=database, region=region, logger=logger, **kwargs)

        resolved = resolve_snowflake_settings(
            account=account,
            warehouse=warehouse,
            database=database,
            schema=schema,
            user=kwargs.get("user"),
            role=kwargs.get("role"),
            authenticator=kwargs.get("authenticator"),
            password=kwargs.get("password"),
            private_key_path=kwargs.get("private_key_path"),
            private_key_passphrase=kwargs.get("private_key_passphrase"),
            oauth_token=kwargs.get("oauth_token"),
            project_root=kwargs.get("project_root"),
            environment=kwargs.get("environment"),
        )

        # Store resolved kwargs for later use in actions / auth checks.
        self._kwargs = dict(kwargs)
        for key in [
            "user",
            "password",
            "private_key_path",
            "private_key_passphrase",
            "oauth_token",
            "role",
            "authenticator",
            "project_root",
            "environment",
        ]:
            if resolved.get(key) is not None:
                self._kwargs[key] = resolved[key]

        self.account = resolved.get("account")
        self.warehouse = resolved.get("warehouse")
        self.database = resolved.get("database") or database
        self.schema = resolved.get("schema") or "PUBLIC"
        self.user = resolved.get("user")
        self.role = resolved.get("role")
        self.authenticator = resolved.get("authenticator")
        self.region = region
        self._resolved_config = resolved

        self.info_kv(
            event="provider_initialized",
            provider="snowflake",
            account=self.account,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema,
            role=self.role,
        )

    def capabilities(self) -> Mapping[str, bool]:
        """Advertise comprehensive Snowflake provider capabilities."""
        return {
            "planning": True,
            "apply": True,
            "render": True,
            "graph": True,
            "auth": True,
        }

    def plan(self, contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate Snowflake actions from FLUID contract.

        Converts contract specifications into concrete Snowflake operations:
        - Databases and schemas
        - Tables, views, materialized views
        - Streams and tasks
        - Stored procedures and UDFs
        - RBAC grants
        """
        self.debug_kv(
            event="plan_started", contract_id=contract.get("id"), contract_name=contract.get("name")
        )

        try:
            actions = plan_actions(
                contract, self.account, self.warehouse, self.database, self.schema, self.logger
            )

            self.info_kv(
                event="plan_completed", contract_id=contract.get("id"), actions_count=len(actions)
            )

            return actions

        except Exception as e:
            self.err_kv(event="plan_failed", contract_id=contract.get("id"), error=str(e))
            raise ProviderError(f"Failed to plan Snowflake deployment: {e}") from e

    def apply(self, actions: List[Dict[str, Any]], **kwargs: Any) -> ApplyResult:
        """
        Execute Snowflake actions with idempotent semantics.

        Dispatches actions to appropriate service handlers with:
        - Retry logic for transient failures
        - Proper error categorization
        - Structured result reporting
        - Secret redaction in logs
        """
        start_time = time.time()
        results: List[Dict[str, Any]] = []
        applied = 0
        failed = 0

        self.info_kv(event="apply_started", actions_count=len(actions), provider="snowflake")

        for i, action in enumerate(actions):
            op = action.get("op")
            action_id = action.get("id", f"action_{i}")

            try:
                # Redact action before logging (removes 'op' from spread to avoid duplicate)
                redacted_action = redact_dict(action)
                redacted_action.pop("op", None)  # Remove op to avoid duplicate with explicit op=op
                redacted_action.pop("id", None)  # Remove id to avoid duplicate (0.5.7)
                redacted_action.pop(
                    "action_id", None
                )  # Remove action_id to avoid duplicate (0.7.1)

                self.debug_kv(event="action_started", action_id=action_id, op=op, **redacted_action)

                result = self._execute_action(action)
                result["action_id"] = action_id
                result["index"] = i

                results.append(result)

                if result.get("status") == "changed" or (
                    result.get("status") == "ok" and not result.get("skipped", False)
                ):
                    applied += 1

                self.debug_kv(
                    event="action_completed",
                    action_id=action_id,
                    status=result.get("status"),
                    changed=result.get("changed", False),
                    duration_ms=result.get("duration_ms", 0),
                )

            except Exception as e:
                failed += 1
                error_result = {
                    "action_id": action_id,
                    "index": i,
                    "status": "error",
                    "op": op,
                    "error": str(e),
                    "changed": False,
                }
                results.append(error_result)

                self.err_kv(event="action_failed", action_id=action_id, op=op, error=str(e))

        duration_sec = round(time.time() - start_time, 3)

        apply_result = ApplyResult(
            provider="snowflake",
            applied=applied,
            failed=failed,
            duration_sec=duration_sec,
            timestamp=self._utc_timestamp(),
            results=results,
        )

        self.info_kv(
            event="apply_completed", applied=applied, failed=failed, duration_sec=duration_sec
        )

        return apply_result

    def render(
        self,
        src: Mapping[str, Any] | List[Mapping[str, Any]],
        *,
        out: Optional[str] = None,
        fmt: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Export FLUID contracts to external formats."""
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
            return get_auth_report(self._resolved_config)
        except Exception as e:
            return {"status": "error", "error": str(e), "provider": "snowflake"}

    def _execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch action to appropriate service handler.

        Routes actions based on operation prefix:
        - sf.database.*: Database operations
        - sf.schema.*: Schema operations
        - sf.table.*: Table operations
        - sf.view.*: View operations
        - sf.stream.*: Stream operations
        - sf.task.*: Task operations
        - sf.procedure.*: Stored procedure operations
        - sf.udf.*: User-defined function operations
        - sf.grant.*: RBAC operations
        - sf.share.*: Data sharing operations
        """
        op = action.get("op")

        if not op:
            raise ProviderError("Action missing required 'op' field")

        # Route to service-specific handlers
        if op.startswith("sf.database."):
            return self._execute_database_action(action)
        elif op.startswith("sf.schema."):
            return self._execute_schema_action(action)
        elif op.startswith("sf.table."):
            return self._execute_table_action(action)
        elif op.startswith("sf.view."):
            return self._execute_view_action(action)
        elif op.startswith("sf.stream."):
            return self._execute_stream_action(action)
        elif op.startswith("sf.task."):
            return self._execute_task_action(action)
        elif op.startswith("sf.procedure."):
            return self._execute_procedure_action(action)
        elif op.startswith("sf.udf."):
            return self._execute_udf_action(action)
        elif op.startswith("sf.grant."):
            return self._execute_grant_action(action)
        elif op.startswith("sf.share."):
            return self._execute_share_action(action)
        elif op.startswith("sf.sql."):
            return self._execute_sql_action(action)
        else:
            self.warn_kv(event="unknown_action_op", op=op, action_id=action.get("id"))
            return {
                "status": "skipped",
                "op": op,
                "reason": f"Unknown operation: {op}",
                "changed": False,
            }

    def _execute_database_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute database operations."""
        from .actions import database

        op = action.get("op")

        if op == "sf.database.ensure":
            return with_retry(lambda: database.ensure_database(action, self), self)
        elif op == "sf.database.drop":
            return database.drop_database(action, self)
        else:
            raise ProviderError(f"Unknown database operation: {op}")

    def _execute_schema_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute schema operations."""
        from .actions import schema

        op = action.get("op")

        if op == "sf.schema.ensure":
            return with_retry(lambda: schema.ensure_schema(action, self), self)
        elif op == "sf.schema.drop":
            return schema.drop_schema(action, self)
        else:
            raise ProviderError(f"Unknown schema operation: {op}")

    def _execute_table_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute table operations."""
        from .actions import table

        op = action.get("op")

        if op == "sf.table.ensure":
            return with_retry(lambda: table.ensure_table(action, self), self)
        elif op == "sf.table.alter":
            return table.alter_table(action, self)
        elif op == "sf.table.drop":
            return table.drop_table(action, self)
        else:
            raise ProviderError(f"Unknown table operation: {op}")

    def _execute_view_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute view operations."""
        from .actions import view

        op = action.get("op")

        if op == "sf.view.ensure":
            return view.ensure_view(action, self)
        elif op == "sf.view.materialized.ensure":
            return view.ensure_materialized_view(action, self)
        else:
            raise ProviderError(f"Unknown view operation: {op}")

    def _execute_stream_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute stream operations."""
        from .actions import stream

        op = action.get("op")

        if op == "sf.stream.ensure":
            return stream.ensure_stream(action, self)
        else:
            raise ProviderError(f"Unknown stream operation: {op}")

    def _execute_task_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute task operations."""
        from .actions import task

        op = action.get("op")

        if op == "sf.task.ensure":
            return task.ensure_task(action, self)
        elif op == "sf.task.resume":
            return task.resume_task(action, self)
        elif op == "sf.task.suspend":
            return task.suspend_task(action, self)
        else:
            raise ProviderError(f"Unknown task operation: {op}")

    def _execute_procedure_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute stored procedure operations."""
        from .actions import procedure

        op = action.get("op")

        if op == "sf.procedure.ensure":
            return procedure.ensure_procedure(action, self)
        else:
            raise ProviderError(f"Unknown procedure operation: {op}")

    def _execute_udf_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute UDF operations."""
        from .actions import udf

        op = action.get("op")

        if op == "sf.udf.ensure":
            return udf.ensure_udf(action, self)
        else:
            raise ProviderError(f"Unknown UDF operation: {op}")

    def _execute_grant_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute RBAC grant operations."""
        from .actions import grants

        op = action.get("op")

        if op == "sf.grant.role":
            return grants.grant_role(action, self)
        elif op == "sf.grant.privilege":
            return grants.grant_privilege(action, self)
        else:
            raise ProviderError(f"Unknown grant operation: {op}")

    def _execute_share_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute data sharing operations."""
        from .actions import share

        op = action.get("op")

        if op == "sf.share.ensure":
            return share.ensure_share(action, self)
        else:
            raise ProviderError(f"Unknown share operation: {op}")

    def _execute_sql_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute arbitrary SQL."""
        from .actions import sql

        op = action.get("op")

        if op == "sf.sql.execute":
            return sql.execute_sql(action, self)
        else:
            raise ProviderError(f"Unknown SQL operation: {op}")

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
        Export contract as executable DAG/pipeline code for Snowflake.

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
            if engine == "airflow":
                from .codegen import generate_airflow_dag

                code = generate_airflow_dag(contract, self.account, self.database, self.warehouse)
                filename = f"{safe_id}_dag.py"

            elif engine == "dagster":
                from .codegen import generate_dagster_pipeline

                code = generate_dagster_pipeline(
                    contract, self.account, self.database, self.warehouse
                )
                filename = f"{safe_id}_pipeline.py"

            elif engine == "prefect":
                from .codegen import generate_prefect_flow

                code = generate_prefect_flow(contract, self.account, self.database, self.warehouse)
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
