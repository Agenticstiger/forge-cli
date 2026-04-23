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

        Two dispatch modes are supported, in this order:

        1. **0.7.1 abstract ops** (``provisionDataset``, ``grantAccess``,
           ``revokeAccess``, ``scheduleTask``, ``registerSchema``,
           ``createView``, ``updatePolicy``, ``publishEvent``,
           ``custom``). These are the provider-agnostic op names that
           ``fluid plan`` emits into ``plan.json`` when the contract is
           fluidVersion >= 0.7.1. Each is translated into one or more
           synthetic sub-actions with ``sf.*`` ops and re-dispatched.
           Status is aggregated via :meth:`_aggregate_sub_status`.

        2. **Native ``sf.*`` ops** (``sf.database.ensure``,
           ``sf.schema.ensure``, etc.). These are the low-level routes
           used by the native planner directly, and as the target of
           abstract-op translation above.

        Phase 6F: previously only mode 2 was implemented; abstract ops
        fell through to the ``unknown_action_op`` branch and returned
        silent no-ops while apply reported SUCCESS. This dispatcher now
        handles both.
        """
        op = action.get("op")

        if not op:
            raise ProviderError("Action missing required 'op' field")

        # ── 1. Abstract-op dispatch (0.7.1) ──────────────────────────
        abstract_handlers = {
            "provisionDataset": self._handle_abstract_provision_dataset,
            "registerSchema": self._handle_abstract_register_schema,
            "createView": self._handle_abstract_create_view,
            "grantAccess": self._handle_abstract_grant_access,
            "revokeAccess": self._handle_abstract_revoke_access,
            "scheduleTask": self._handle_abstract_schedule_task,
            "updatePolicy": self._handle_abstract_update_policy,
            "publishEvent": self._handle_abstract_publish_event,
            "custom": self._handle_abstract_custom,
        }
        if op in abstract_handlers:
            return abstract_handlers[op](action)

        # ── 2. Native sf.* prefix dispatch ───────────────────────────
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

    # -------------------------------------------------------------------------
    # 0.7.1 abstract-op handlers (Phase 6F)
    #
    # Each handler translates an abstract op into one or more synthetic
    # sub-actions with native ``sf.*`` ops, dispatches each through the
    # existing service-specific handlers, then aggregates sub-results.
    # Kept as small single-purpose methods so each op's translation is
    # auditable in isolation.
    # -------------------------------------------------------------------------

    @staticmethod
    def _binding_location(action: Dict[str, Any]) -> Dict[str, Any]:
        """Extract ``params.binding.location`` with ``{{ env.X }}`` resolved.

        The 0.7.1 ActionType spec nests the target identity under
        ``params.binding.location`` for every dataset-touching op. The
        planner emits ``{{ env.SNOWFLAKE_DATABASE }}``-style templates
        for the account/database/schema values (so ``plan.json`` is
        environment-agnostic); apply-time resolution uses the Snowflake
        provider's canonical resolver. Returning a safe empty dict lets
        handlers do ``loc.get("database")`` without a guard at the call
        site.
        """
        from .util.config import resolve_env_templates

        params = action.get("params") or {}
        binding = params.get("binding") or {}
        raw = binding.get("location") or {}
        return {k: resolve_env_templates(v) for k, v in raw.items()}

    @staticmethod
    def _aggregate_sub_status(sub_results: List[Dict[str, Any]]) -> str:
        """Roll up status across sub-actions.

        - any ``error`` → overall ``error`` (first one wins for reason)
        - any ``success`` → overall ``success``
        - otherwise → ``skipped``

        Matches the semantics of the Phase 6F test suite in
        :mod:`tests.providers.test_snowflake_abstract_ops`.
        """
        if not sub_results:
            return "skipped"
        if any(r.get("status") == "error" for r in sub_results):
            return "error"
        if any(r.get("status") == "success" for r in sub_results):
            return "success"
        return "skipped"

    def _handle_abstract_provision_dataset(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """provisionDataset → ensure_database + ensure_schema (+ ensure_table).

        Field flow — ``ensure_database`` / ``ensure_schema`` /
        ``ensure_table`` read action keys at the **top level** (not nested
        under ``params``). The synthetic sub-actions built here flatten
        accordingly so the downstream handlers find their inputs.

        The ``ensure_table`` sub-action is emitted only when the caller
        supplies ``params.table.columns`` (or ``params.columns``). For
        reference-style data products that delegate table shape to dbt,
        the table step is skipped and only the db + schema are ensured.
        The dbt runner then creates the table during stage-7's
        ``amend-and-build`` mode.
        """
        loc = self._binding_location(action)
        action_id = action.get("id")
        sub_results: List[Dict[str, Any]] = []

        account = loc.get("account")
        database = loc.get("database")
        schema = loc.get("schema")
        table = loc.get("table")

        if not database or not schema:
            return {
                "status": "error",
                "op": "provisionDataset",
                "action_id": action_id,
                "reason": (
                    "params.binding.location must include database and schema "
                    f"(got database={database!r}, schema={schema!r})"
                ),
                "changed": False,
            }

        # Sub-action 1: ensure_database (fields at top level)
        sub_db = {
            "op": "sf.database.ensure",
            "id": f"{action_id}.db" if action_id else None,
            "account": account,
            "database": database,
        }
        sub_results.append(self._execute_database_action(sub_db))
        if sub_results[-1].get("status") == "error":
            return {
                "status": "error",
                "op": "provisionDataset",
                "action_id": action_id,
                "sub_results": sub_results,
                "reason": "ensure_database failed",
                "changed": any(r.get("changed") for r in sub_results),
            }

        # Sub-action 2: ensure_schema
        sub_sc = {
            "op": "sf.schema.ensure",
            "id": f"{action_id}.schema" if action_id else None,
            "account": account,
            "database": database,
            "schema": schema,
        }
        sub_results.append(self._execute_schema_action(sub_sc))
        if sub_results[-1].get("status") == "error":
            return {
                "status": "error",
                "op": "provisionDataset",
                "action_id": action_id,
                "sub_results": sub_results,
                "reason": "ensure_schema failed",
                "changed": any(r.get("changed") for r in sub_results),
            }

        # Sub-action 3 (optional): ensure_table. Only emitted when the
        # caller supplied an explicit column spec; reference contracts
        # defer table creation to dbt in stage-7's build.
        if table:
            params = action.get("params") or {}
            table_spec = params.get("table") or params.get("tableSpec") or {}
            columns = table_spec.get("columns") or params.get("columns")
            if columns:
                sub_tb = {
                    "op": "sf.table.ensure",
                    "id": f"{action_id}.table" if action_id else None,
                    "account": account,
                    "database": database,
                    "schema": schema,
                    "table": table,
                    "columns": columns,
                    **{
                        k: v
                        for k, v in table_spec.items()
                        if k in {"comment", "cluster_by", "tags"}
                    },
                }
                sub_results.append(self._execute_table_action(sub_tb))
            else:
                # Record an informational skipped result so the apply
                # report carries evidence that table creation was
                # deferred — important for operators debugging why a
                # table didn't appear.
                sub_results.append(
                    {
                        "status": "skipped",
                        "op": "sf.table.ensure",
                        "table": table,
                        "reason": (
                            "no columns in params.table.columns / params.columns; "
                            "deferring table creation to dbt build"
                        ),
                        "changed": False,
                    }
                )

        return {
            "status": self._aggregate_sub_status(sub_results),
            "op": "provisionDataset",
            "action_id": action_id,
            "sub_results": sub_results,
            "changed": any(r.get("changed") for r in sub_results),
        }

    def _handle_abstract_register_schema(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """registerSchema → ensure_database + ensure_schema only."""
        loc = self._binding_location(action)
        action_id = action.get("id")
        sub_results: List[Dict[str, Any]] = []

        account = loc.get("account")
        database = loc.get("database")
        schema = loc.get("schema")

        if not database or not schema:
            return {
                "status": "error",
                "op": "registerSchema",
                "action_id": action_id,
                "reason": "params.binding.location must include database and schema",
                "changed": False,
            }

        sub_results.append(
            self._execute_database_action(
                {
                    "op": "sf.database.ensure",
                    "id": f"{action_id}.db" if action_id else None,
                    "account": account,
                    "database": database,
                }
            )
        )
        if sub_results[-1].get("status") == "error":
            return {
                "status": "error",
                "op": "registerSchema",
                "action_id": action_id,
                "sub_results": sub_results,
                "changed": any(r.get("changed") for r in sub_results),
            }

        sub_results.append(
            self._execute_schema_action(
                {
                    "op": "sf.schema.ensure",
                    "id": f"{action_id}.schema" if action_id else None,
                    "account": account,
                    "database": database,
                    "schema": schema,
                }
            )
        )

        return {
            "status": self._aggregate_sub_status(sub_results),
            "op": "registerSchema",
            "action_id": action_id,
            "sub_results": sub_results,
            "changed": any(r.get("changed") for r in sub_results),
        }

    def _handle_abstract_create_view(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """createView → ensure_view. ``params.sql`` / ``params.view.sql``
        carries the view body; flattened to top-level ``query`` on the
        sub-action because :func:`ensure_view` reads ``action["query"]``.
        """
        loc = self._binding_location(action)
        action_id = action.get("id")
        params = action.get("params") or {}

        sql = params.get("sql") or (params.get("view") or {}).get("sql")
        if not sql:
            return {
                "status": "error",
                "op": "createView",
                "action_id": action_id,
                "reason": "params.sql (or params.view.sql) is required for createView",
                "changed": False,
            }

        view_name = loc.get("table") or loc.get("view") or params.get("name")
        sub = {
            "op": "sf.view.ensure",
            "id": f"{action_id}.view" if action_id else None,
            "account": loc.get("account"),
            "database": loc.get("database"),
            "schema": loc.get("schema"),
            "name": view_name,
            "query": sql,
        }
        result = self._execute_view_action(sub)
        return {
            "status": result.get("status", "success"),
            "op": "createView",
            "action_id": action_id,
            "sub_results": [result],
            "changed": result.get("changed", False),
        }

    def _handle_abstract_grant_access(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """grantAccess → sf.grant.privilege (or sf.grant.role).

        Dispatches on ``params.type``: "role" → grant_role,
        otherwise → grant_privilege.
        """
        params = action.get("params") or {}
        action_id = action.get("id")
        grant_type = params.get("type", "privilege").lower()

        if grant_type == "role":
            sub = {
                "op": "sf.grant.role",
                "id": f"{action_id}.grant" if action_id else None,
                "params": params,
            }
        else:
            sub = {
                "op": "sf.grant.privilege",
                "id": f"{action_id}.grant" if action_id else None,
                "params": params,
            }
        result = self._execute_grant_action(sub)
        return {
            "status": result.get("status", "success"),
            "op": "grantAccess",
            "action_id": action_id,
            "sub_results": [result],
            "changed": result.get("changed", False),
        }

    def _handle_abstract_revoke_access(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """revokeAccess — deferred.

        Revocation requires a REVOKE DDL path that the current action
        handlers in ``snowflake.actions.grants`` do not yet expose. We
        deliberately return ``skipped`` with a machine-readable reason
        rather than error-out, because a revocation that doesn't happen
        is a visibility issue (fail loud in audit logs) rather than a
        pipeline halt.
        """
        return {
            "status": "skipped",
            "op": "revokeAccess",
            "action_id": action.get("id"),
            "reason": (
                "revokeAccess is not yet implemented in the enhanced "
                "Snowflake provider; grants are not auto-revoked. "
                "Track under trello-verify-revoke-access."
            ),
            "changed": False,
        }

    def _handle_abstract_schedule_task(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """scheduleTask → Path-B handled by SchedulePlanner; Path-A uses
        stage-11 fluid schedule-sync. Return skipped-with-reason so apply
        reports an explicit no-op rather than silently succeed."""
        params = action.get("params") or {}
        engine = params.get("engine") or params.get("orchestration", {}).get("engine") or "unknown"
        return {
            "status": "skipped",
            "op": "scheduleTask",
            "action_id": action.get("id"),
            "reason": (
                f"schedule task deferred (engine={engine}). Path-B engines "
                "(snowflake_tasks, eventbridge) land schedule actions in "
                "plan.json for stage-7 apply via SchedulePlanner; Path-A "
                "engines (airflow, prefect, dagster) are pushed by stage-11 "
                "fluid schedule-sync."
            ),
            "changed": False,
        }

    def _handle_abstract_update_policy(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """updatePolicy — deferred.

        Policy update in Snowflake maps to SECURITY.* DDL that is
        primarily driven by the dedicated ``fluid policy-apply`` stage-8
        command. Return skipped so this op appears in apply's report
        without firing a duplicate or conflicting DDL path.
        """
        return {
            "status": "skipped",
            "op": "updatePolicy",
            "action_id": action.get("id"),
            "reason": (
                "policy updates are driven by stage-8 ``fluid policy-apply`` "
                "against policy/bindings.json; stage-7 apply treats "
                "updatePolicy as a declarative marker."
            ),
            "changed": False,
        }

    def _handle_abstract_publish_event(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """publishEvent — skipped silently.

        Snowflake has no native event-publish primitive. The op is
        accepted without warning so contracts that emit events for
        cross-provider consumers (e.g. BigQuery subscribers) don't
        trigger false alarms in Snowflake apply.
        """
        return {
            "status": "skipped",
            "op": "publishEvent",
            "action_id": action.get("id"),
            "reason": "publishEvent has no Snowflake primitive; no-op by design",
            "changed": False,
        }

    def _handle_abstract_custom(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """custom → sf.sql.execute on params.sql.

        Provides an escape hatch for contracts that embed raw SQL (e.g.
        warehouse sizing, stream-append statements) that no other op
        covers. Fails loud if ``params.sql`` is missing — a custom op
        without SQL is always a contract authoring bug.
        """
        action_id = action.get("id")
        params = action.get("params") or {}
        sql = params.get("sql")
        if not sql:
            return {
                "status": "error",
                "op": "custom",
                "action_id": action_id,
                "reason": "custom op requires params.sql",
                "changed": False,
            }
        sub = {
            "op": "sf.sql.execute",
            "id": f"{action_id}.sql" if action_id else None,
            "params": {"sql": sql},
        }
        result = self._execute_sql_action(sub)
        return {
            "status": result.get("status", "success"),
            "op": "custom",
            "action_id": action_id,
            "sub_results": [result],
            "changed": result.get("changed", False),
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
