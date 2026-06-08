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

# fluid_build/providers/snowflake/provider.py
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

        # Store resolved kwargs for later use by auth and the rollback
        # connection setup (``cleanup_backups``).
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

    def restore_ddl(self, snapshot: Mapping[str, Any]) -> List[str]:
        """Snowflake restore via zero-copy CLONE.

        ``CREATE OR REPLACE TABLE <orig> CLONE <backup>`` is atomic
        and metadata-only — no storage cost beyond the CLONE pointer.

        Security: identifiers come from ``.fluid/rollback-state.json``
        which is documented as PR-reviewable / attacker-authorable —
        every component is routed through ``validate_ident`` before
        being interpolated into the DDL string. The pattern mirrors
        ``cli/rollback.py::_restore_snowflake``. A single tampered
        record yields ``[]`` rather than poisoning the caller; this
        keeps legitimate snapshots restorable when one stale entry is
        malformed.
        """
        from fluid_build.providers._sql_safety import validate_ident

        location = snapshot.get("location") or {}
        db = location.get("database")
        sch = location.get("schema")
        tbl = location.get("table")
        backup = location.get("backup_table") or snapshot.get("backup_name")
        if not (db and sch and tbl and backup):
            return []
        try:
            db_v = validate_ident(str(db))
            sch_v = validate_ident(str(sch))
            tbl_v = validate_ident(str(tbl))
            backup_v = validate_ident(str(backup))
        except ValueError as exc:
            self.warn_kv(
                event="backup_restore_invalid_identifier",
                error=str(exc),
                database=db,
                schema=sch,
                table=tbl,
                backup=backup,
            )
            return []
        return [f"CREATE OR REPLACE TABLE {db_v}.{sch_v}.{tbl_v} CLONE {db_v}.{sch_v}.{backup_v}"]

    def cleanup_backups(self, snapshots: List[Mapping[str, Any]]) -> None:
        """Drop Snowflake backup tables for snapshots aged out of state.

        Best-effort: per-table failures log a warning and continue.
        ``DROP TABLE IF EXISTS`` is idempotent so repeat runs are safe.

        Security: identifiers come from ``.fluid/rollback-state.json``
        which is documented as PR-reviewable / attacker-authorable —
        every component is routed through ``validate_ident`` before
        being interpolated into the DDL string (mirrors the same
        defence applied at ``cli/rollback.py::_restore_snowflake``).
        Records with invalid identifiers are skipped + logged so a
        single tampered entry doesn't poison the whole cleanup pass.
        """
        from fluid_build.providers._sql_safety import validate_ident

        if not snapshots:
            return
        for snap in snapshots:
            loc = snap.get("location") or {}
            db = loc.get("database")
            sch = loc.get("schema")
            backup = loc.get("backup_table") or snap.get("backup_name")
            if not (db and sch and backup):
                continue
            try:
                db_v = validate_ident(str(db))
                sch_v = validate_ident(str(sch))
                backup_v = validate_ident(str(backup))
            except ValueError as exc:
                self.warn_kv(
                    event="backup_cleanup_invalid_identifier",
                    error=str(exc),
                    database=db,
                    schema=sch,
                    backup=backup,
                )
                continue
            stmt = f"DROP TABLE IF EXISTS {db_v}.{sch_v}.{backup_v}"
            try:
                # Backup cleanup is part of the retained rollback machinery,
                # not the retired apply path — it opens its own connection
                # rather than routing through a provider action dispatcher.
                from .connection import SnowflakeConnection
                from .util.config import get_connection_params

                params = get_connection_params(
                    account=self.account,
                    warehouse=self.warehouse,
                    database=db_v,
                    schema=sch_v,
                    **self._kwargs,
                )
                with SnowflakeConnection(**params) as conn:
                    conn.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                self.warn_kv(
                    event="backup_cleanup_drop_failed",
                    table=f"{db_v}.{sch_v}.{backup_v}",
                    error=str(exc),
                )

    def plan(
        self,
        contract: Mapping[str, Any],
        *,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate Snowflake actions from FLUID contract.

        Converts contract specifications into concrete Snowflake operations:
        - Databases and schemas
        - Tables, views, materialized views
        - Streams and tasks
        - Stored procedures and UDFs
        - RBAC grants

        The optional ``mode`` carries the apply-time mode and triggers
        destructive semantics (``CREATE OR REPLACE TABLE AS SELECT``
        for SQL builds, plus a pre-flight CLONE snapshot for rollback)
        when set to ``replace`` / ``replace-and-build``.
        """
        self.debug_kv(
            event="plan_started",
            contract_id=contract.get("id"),
            contract_name=contract.get("name"),
            mode=mode,
        )

        try:
            actions = plan_actions(
                contract,
                self.account,
                self.warehouse,
                self.database,
                self.schema,
                self.logger,
                mode=mode,
            )

            self.info_kv(
                event="plan_completed",
                contract_id=contract.get("id"),
                actions_count=len(actions),
                mode=mode,
            )

            return actions

        except Exception as e:
            self.err_kv(event="plan_failed", contract_id=contract.get("id"), error=str(e))
            raise ProviderError(f"Failed to plan Snowflake deployment: {e}") from e

    def apply(self, actions: List[Dict[str, Any]], **kwargs: Any) -> ApplyResult:
        """Native Snowflake apply is retired — uses the OpenTofu engine.

        Snowflake was cut over to the OpenTofu autogenerator
        (AUTOGEN_SPIKE.md): ``fluid apply`` compiles the contract to
        ``.tf.json`` and runs ``tofu``. The native per-service DDL
        apply path was removed.
        """
        raise ProviderError(
            "native Snowflake apply is retired — uses the OpenTofu engine; "
            "`fluid apply` provisions it automatically"
        )

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
