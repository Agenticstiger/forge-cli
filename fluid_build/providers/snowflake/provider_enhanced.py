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
from .util.types import map_fluid_type_to_snowflake


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
        return [
            f"CREATE OR REPLACE TABLE {db_v}.{sch_v}.{tbl_v} " f"CLONE {db_v}.{sch_v}.{backup_v}"
        ]

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
                self._execute_sql_action(
                    {
                        "op": "sf.sql.execute",
                        "id": f"cleanup_{backup_v}",
                        "sql": stmt,
                        "account": self.account,
                        "database": db_v,
                        "schema": sch_v,
                    }
                )
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
                redacted_action.pop("id", None)  # Remove id to avoid duplicate (v0.7.x)
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
                # ``allow_failure`` actions (pre-flight CLONE snapshots
                # for replace mode) record a warning + continue. Any
                # other exception aborts the plan as a hard failure.
                if action.get("allow_failure"):
                    soft_result = {
                        "action_id": action_id,
                        "index": i,
                        "status": "skipped",
                        "op": op,
                        "reason": str(e),
                        "changed": False,
                        "soft_failure": True,
                    }
                    results.append(soft_result)
                    self.warn_kv(
                        event="action_soft_failed",
                        action_id=action_id,
                        op=op,
                        reason=str(e),
                    )
                    continue
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
           ``custom``). The provider-agnostic op names ``fluid plan``
           emits when the contract is fluidVersion >= 0.7.1. Each is
           translated into one or more synthetic sub-actions with
           ``sf.*`` ops and re-dispatched. Status is aggregated via
           :meth:`_aggregate_sub_status`.

        2. **Native ``sf.*`` ops** (``sf.database.ensure``,
           ``sf.schema.ensure``, etc.). The low-level routes used by
           the native planner directly, and as the target of
           abstract-op translation above.
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
        # Lookup table replaces the 12-way ``elif op.startswith(...)``
        # chain. Adding a new sf.<service>. prefix is one entry here +
        # the handler implementation. ``next(...)`` picks the longest
        # matching prefix so ``sf.database.ensure`` doesn't accidentally
        # route through a generic ``sf.`` fallback if one were added.
        prefix_handlers = {
            "sf.database.": self._execute_database_action,
            "sf.schema.": self._execute_schema_action,
            "sf.table.": self._execute_table_action,
            "sf.view.": self._execute_view_action,
            "sf.stream.": self._execute_stream_action,
            "sf.task.": self._execute_task_action,
            "sf.procedure.": self._execute_procedure_action,
            "sf.udf.": self._execute_udf_action,
            "sf.grant.": self._execute_grant_action,
            "sf.share.": self._execute_share_action,
            "sf.sql.": self._execute_sql_action,
        }
        handler = next(
            (
                h
                for prefix, h in sorted(prefix_handlers.items(), key=lambda x: -len(x[0]))
                if op.startswith(prefix)
            ),
            None,
        )
        if handler is not None:
            return handler(action)

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

        Side effect: registers the binding in
        ``self._provisioned_bindings`` keyed by exposeId so a downstream
        ``scheduleTask`` action with ``params.outputs == [exposeId]``
        can look up the target ``database/schema/table`` for INSERT
        wrapping.
        """
        loc = self._binding_location(action)
        # The plan stage emits actions with an ``action_id`` key (see
        # ``cli/plan.py::_plan_with_provider_actions`` — uses
        # ``"action_id": action.action_id``). Older code paths set
        # ``id`` instead, so check both. **Critical**: when this falls
        # back to None, the per-expose column-resolution fallback
        # below silently grabs the FIRST expose's schema — which leaks
        # cross-expose columns onto sibling tables (e.g. adds 7 cols
        # from ``subscriber360_core`` onto the
        # ``subscriber_health_scorecard`` table on every apply, even
        # though apply reports ``applied: 0``).
        action_id = action.get("action_id") or action.get("id")
        sub_results: List[Dict[str, Any]] = []
        # Stash the binding so a follow-on scheduleTask can resolve
        # the target without re-reading the contract.
        params = action.get("params") or {}
        binding = params.get("binding") if isinstance(params, dict) else None
        if isinstance(binding, dict):
            cache = getattr(self, "_provisioned_bindings", None)
            if cache is None:
                cache = {}
                self._provisioned_bindings = cache
            # Heuristic key: the action_id is shaped like
            # ``provision_<exposeId>`` — strip the prefix to recover
            # the expose name. Falls back to the action_id itself.
            expose_id = (
                action_id[len("provision_") :]
                if action_id and action_id.startswith("provision_")
                else action_id or ""
            )
            cache[expose_id] = binding

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
            # Also accept columns nested under
            # ``params.contract.exposes[].contract.schema`` — the shape
            # the high-level planner emits when the contract author
            # declared the schema on the expose. SQL builds that don't
            # use dbt rely on this path so the table is materialised
            # before the INSERT INTO runs.
            if not columns and isinstance(params.get("contract"), dict):
                # Recover the expose id we're provisioning by stripping
                # the ``provision_`` prefix from the action id, OR by
                # reading the explicit ``params.exposeId`` the legacy
                # planner emits at ``forge/core/provider_actions.py``
                # line 144. Without one of these we **refuse** to
                # guess — silently picking the first expose's schema
                # was the regression that leaked columns from
                # subscriber360_core onto subscriber_health_scorecard
                # in the biz-lab A1 demo.
                target_id = None
                if action_id and action_id.startswith("provision_"):
                    target_id = action_id[len("provision_") :]
                if not target_id:
                    target_id = params.get("exposeId")
                if target_id:
                    for ex in params["contract"].get("exposes") or []:
                        if not isinstance(ex, dict):
                            continue
                        if ex.get("exposeId") == target_id or ex.get("id") == target_id:
                            contract_block = ex.get("contract") or {}
                            cols = contract_block.get("schema") or []
                            if cols:
                                # Contract-shape → action-shape translation.
                                # Two normalisations matter:
                                #
                                # 1. ``required: true`` (FLUID-schema convention) →
                                #    ``nullable: false`` (action-handler convention).
                                #    Without this, contract NOT NULL guarantees
                                #    silently degrade to nullable Snowflake columns.
                                # 2. FLUID type → Snowflake type via
                                #    ``map_fluid_type_to_snowflake``. Without this,
                                #    ``type: NUMBER`` falls through to VARCHAR
                                #    instead of becoming NUMBER(38,0).
                                # ``nullable`` wins if explicitly set so a
                                # contract author can still override.
                                columns = [
                                    {
                                        "name": c.get("name"),
                                        "type": map_fluid_type_to_snowflake(
                                            c.get("type", "string")
                                        ),
                                        "nullable": c.get(
                                            "nullable",
                                            not c.get("required", False),
                                        ),
                                        **(
                                            {"comment": c["description"]}
                                            if c.get("description")
                                            else {}
                                        ),
                                        **(
                                            {"labels": c["labels"]}
                                            if c.get("labels")
                                            else {}
                                        ),
                                    }
                                    for c in cols
                                    if isinstance(c, dict) and c.get("name")
                                ]
                                break
                # When target_id can't be recovered, ``columns`` stays
                # empty and the table-create branch below records a
                # ``status: skipped`` sub-result with reason. That is
                # the safe failure mode — no cross-expose column leak.
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
        """scheduleTask handler.

        Two execution paths:

        * **Inline SQL execution** — when the build's
          ``engine == "sql"`` AND ``params.sql`` is present, the
          handler runs the SQL synchronously as part of apply. When
          ``params.outputs`` references a snowflake_table expose,
          the SQL is wrapped with ``INSERT INTO <db>.<schema>.<table>``
          so the rows land in the target.
        * **Deferred orchestration** — Path-A engines (airflow / prefect /
          dagster) are pushed by stage-11 ``fluid schedule-sync``;
          Path-B engines (snowflake_tasks / eventbridge) emit native
          schedule DDL via SchedulePlanner. Returns
          ``skipped-with-reason`` so apply doesn't silently no-op.
        """
        params = action.get("params") or {}
        engine = params.get("engine") or params.get("orchestration", {}).get("engine") or "unknown"
        action_id = action.get("id")

        sql_text = params.get("sql")
        # Inline-execute path: ``engine: sql`` builds with a SQL string.
        if engine == "sql" and sql_text:
            wrapped = self._wrap_inline_sql_for_outputs(
                sql_text=sql_text,
                outputs=params.get("outputs") or [],
                action=action,
            )
            # Resolve the target db/schema for this sub-action — required
            # by ``sf.sql.execute`` (the action handler reads
            # ``action["account"]`` and KeyErrors with ``'account'`` if
            # absent). Pull from the cached binding registered by
            # ``_handle_abstract_provision_dataset`` first, fall back
            # to the provider's session defaults.
            target_binding = (
                self._find_target_binding_for_outputs(
                    outputs=params.get("outputs") or [], action=action
                )
                or {}
            )
            location = target_binding.get("location") or {}
            from .util.config import resolve_env_templates as _resolve

            sub_db = _resolve(location.get("database")) or self.database
            sub_sch = _resolve(location.get("schema")) or self.schema
            sub = {
                "op": "sf.sql.execute",
                "id": f"{action_id}.run" if action_id else None,
                "sql": wrapped,
                "account": self.account,
                "database": sub_db,
                "schema": sub_sch,
                "comment": f"inline schedule_task build {params.get('buildId') or ''}",
            }
            sub_result = self._execute_sql_action(sub)
            return {
                "status": sub_result.get("status", "success"),
                "op": "scheduleTask",
                "action_id": action_id,
                "sub_results": [sub_result],
                "changed": sub_result.get("changed", False),
            }

        return {
            "status": "skipped",
            "op": "scheduleTask",
            "action_id": action_id,
            "reason": (
                f"schedule task deferred (engine={engine}). Path-B engines "
                "(snowflake_tasks, eventbridge) land schedule actions in "
                "plan.json for stage-7 apply via SchedulePlanner; Path-A "
                "engines (airflow, prefect, dagster) are pushed by stage-11 "
                "fluid schedule-sync."
            ),
            "changed": False,
        }

    def _wrap_inline_sql_for_outputs(
        self,
        *,
        sql_text: str,
        outputs: List[str],
        action: Dict[str, Any],
    ) -> str:
        """Wrap a build's SQL with ``INSERT INTO <target>`` when its
        ``outputs[]`` references a snowflake_table expose.

        Mirrors the wrap logic in ``planner._wrap_sql_for_target`` but
        runs against the live action so the abstract-op dispatcher can
        compose the right SQL even when the planner emitted a bare
        ``scheduleTask`` action.

        No-op (returns SQL unchanged) when:

        * No ``outputs`` declared.
        * The expose isn't snowflake_table.
        * The SQL already starts with INSERT/CREATE/MERGE/UPDATE/etc.
          — author declared their own sink.
        """
        from fluid_build.providers._sql_safety import validate_ident

        if not outputs:
            return sql_text
        # Find the matching expose in the action's contract context.
        # The high-level planner stamps ``params.binding`` on the
        # paired ``provisionDataset`` action with the same ``buildId``;
        # we look there for the target binding when available, else
        # fall back to the contract's exposes[] by ``exposeId``.
        params = action.get("params") or {}
        target_binding = self._find_target_binding_for_outputs(outputs=outputs, action=action)
        if not target_binding:
            return sql_text
        if (target_binding.get("format") or "").lower() not in (
            "snowflake_table",
            "snowflake-table",
        ):
            return sql_text
        location = target_binding.get("location") or {}
        from fluid_build.providers.snowflake.util.config import (
            resolve_env_templates as _resolve,
        )

        db = _resolve(location.get("database"))
        sch = _resolve(location.get("schema"))
        tbl = _resolve(location.get("table")) or outputs[0]
        if not (db and sch and tbl):
            return sql_text
        upper_head = sql_text.lstrip().upper()[:32]
        for kw in ("INSERT", "CREATE", "MERGE", "UPDATE", "DELETE", "COPY", "TRUNCATE"):
            if upper_head.startswith(kw):
                return sql_text
        body = sql_text.rstrip().rstrip(";")
        return (
            f"INSERT INTO {validate_ident(str(db))}."
            f"{validate_ident(str(sch))}.{validate_ident(str(tbl))}\n{body}"
        )

    def _find_target_binding_for_outputs(
        self, *, outputs: List[str], action: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Find the binding for the action's ``outputs[0]``.

        The plan.json passed to apply doesn't carry the full contract,
        so we have to look in two places:

        1. The matching ``provisionDataset`` action that ran earlier
           in the plan (it has ``params.binding``). The provider keeps
           a per-run cache of provisioned bindings keyed by exposeId.
        2. ``params.binding`` on this action itself (if the planner
           stamped it directly — newer plans).

        Returns the binding dict or ``None`` when nothing matches.
        """
        if not outputs:
            return None
        target_id = outputs[0]
        # Direct: action carries the binding (newer plans).
        params = action.get("params") or {}
        if isinstance(params.get("binding"), dict):
            return params["binding"]
        # Cache: provisionDataset registers per-exposeId.
        cache = getattr(self, "_provisioned_bindings", None) or {}
        return cache.get(target_id)

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
