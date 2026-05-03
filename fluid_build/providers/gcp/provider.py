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
from .util.logging import redact_dict
from .util.retry import with_retry


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
        """
        location = snapshot.get("location") or {}
        db = location.get("database")
        sch = location.get("schema")
        tbl = location.get("table")
        backup = location.get("backup_table") or snapshot.get("backup_name")
        if not (db and sch and tbl and backup):
            return []
        return [
            f"CREATE OR REPLACE TABLE `{db}.{sch}.{tbl}` AS " f"SELECT * FROM `{db}.{sch}.{backup}`"
        ]

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
        """
        Execute GCP actions with idempotent semantics.

        Dispatches actions to appropriate service handlers with:
        - Retry logic for transient failures
        - Proper error categorization
        - Structured result reporting
        - Secret redaction in logs

        Args:
            actions: List of actions to execute
            **kwargs: Additional parameters (plan, contract, etc.) - ignored for GCP provider
        """
        start_time = time.time()
        results: List[Dict[str, Any]] = []
        applied = 0
        failed = 0

        self.info_kv(event="apply_started", actions_count=len(actions), provider="gcp")

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

                # Check if action succeeded and made changes
                if result.get("status") == "error":
                    failed += 1
                elif result.get("changed", False):
                    # Only count as applied if resources were actually modified
                    applied += 1

                # No spreading here, so no duplicate issue
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

                # No spreading here, so no duplicate issue
                self.err_kv(event="action_failed", action_id=action_id, op=op, error=str(e))

        duration_sec = round(time.time() - start_time, 3)

        apply_result = ApplyResult(
            provider="gcp",
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

    # ========================================================================
    # 0.7.1 Action Handlers (Normalize to service-specific operations)
    # ========================================================================

    def _provision_dataset_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle provisionDataset action from 0.7.1 contracts.
        Normalizes to bq.ensure_dataset / bq.ensure_table operations.
        """
        try:
            params = action.get("params", {})
            binding = params.get("binding", {})
            location_info = binding.get("location", {})
            kind = params.get("kind", "table")

            # Extract dataset/table info
            dataset = location_info.get("dataset")
            table = location_info.get("table")

            if not dataset:
                raise ProviderError("provisionDataset requires 'dataset' in binding.location")

            # If it's a table/view, create both dataset and table
            if table and kind in ("table", "view"):
                # First ensure dataset exists
                dataset_action = {
                    "op": "bq.ensure_dataset",
                    "dataset": dataset,
                    "project": location_info.get("project") or self.project,
                    "location": location_info.get("region") or self.region,
                }
                self._execute_bigquery_action(dataset_action)

                # Then create table/view
                if kind == "table":
                    table_action = {
                        "op": "bq.ensure_table",
                        "dataset": dataset,
                        "table": table,
                        "project": location_info.get("project") or self.project,
                        "location": location_info.get("region") or self.region,
                        "schema": params.get("schema", []),
                        "contract": params.get("contract"),
                        "labels": params.get("labels", {}),
                    }
                    return self._execute_bigquery_action(table_action)
                else:  # view
                    self.warn_kv(
                        event="view_creation_skipped",
                        reason="View SQL query not provided in contract",
                    )
                    return {
                        "status": "ok",
                        "op": "provisionDataset",
                        "changed": False,
                        "skipped": True,
                        "message": "View creation delegated to dbt/SQL transformations",
                    }
            else:
                # Just dataset
                dataset_action = {
                    "op": "bq.ensure_dataset",
                    "dataset": dataset,
                    "project": location_info.get("project") or self.project,
                    "location": location_info.get("region") or self.region,
                }
                return self._execute_bigquery_action(dataset_action)

        except Exception as e:
            self.err_kv(event="provision_dataset_failed", error=str(e))
            return {"status": "error", "op": "provisionDataset", "error": str(e), "changed": False}

    def _schedule_task_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle scheduleTask action from 0.7.1 contracts.
        Tasks are delegated to orchestration engines (Airflow/Composer).
        """
        params = action.get("params", {})
        build_id = params.get("buildId", "unknown")

        self.info_kv(
            event="schedule_task_skipped",
            build_id=build_id,
            reason="Task scheduling delegated to orchestration engine",
        )

        return {
            "status": "ok",
            "op": "scheduleTask",
            "changed": False,
            "skipped": True,
            "message": "Task scheduling handled by orchestration engine (Airflow/Composer)",
        }

    def _register_schema_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``registerSchema`` (v0.7.1 abstract op) into a
        BigQuery ``update_table_schema`` action.

        The abstract op carries the new column set in ``params.schema``;
        we route it to the BQ action handler which emits the ALTER TABLE
        SET COLUMN sequence to converge live schema with the contract.
        """
        params = action.get("params", {})
        binding = params.get("binding", {})
        location_info = binding.get("location", {})
        dataset = location_info.get("dataset")
        table = location_info.get("table")
        if not (dataset and table):
            raise ProviderError(
                "registerSchema requires both 'dataset' and 'table' in binding.location"
            )
        return self._execute_bigquery_action(
            {
                "op": "bq.update_table_schema",
                "dataset": dataset,
                "table": table,
                "project": location_info.get("project") or self.project,
                "schema": params.get("schema", []),
            }
        )

    def _create_view_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``createView`` (v0.7.1 abstract op) into a
        BigQuery ``ensure_view`` action with the SQL query the
        contract author supplied in ``params.query``.
        """
        params = action.get("params", {})
        binding = params.get("binding", {})
        location_info = binding.get("location", {})
        dataset = location_info.get("dataset")
        view = location_info.get("table") or location_info.get("view")
        query = params.get("query") or params.get("sql")
        if not (dataset and view and query):
            raise ProviderError(
                "createView requires 'dataset', 'table'/'view', and 'query' in params"
            )
        return self._execute_bigquery_action(
            {
                "op": "bq.ensure_view",
                "dataset": dataset,
                "view": view,
                "project": location_info.get("project") or self.project,
                "query": query,
            }
        )

    def _grant_access_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``grantAccess`` into ``iam.grant_role``.

        ``params`` carries the principal (``role``, ``member``) and
        the resource scope (``dataset``, ``table``, or ``project``).
        """
        params = action.get("params", {})
        return self._execute_iam_action(
            {
                "op": "iam.grant_role",
                "role": params.get("role"),
                "member": params.get("member") or params.get("principal"),
                "project": params.get("project") or self.project,
                "resource": params.get("resource"),
            }
        )

    def _revoke_access_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``revokeAccess`` into ``iam.revoke_role``."""
        params = action.get("params", {})
        return self._execute_iam_action(
            {
                "op": "iam.revoke_role",
                "role": params.get("role"),
                "member": params.get("member") or params.get("principal"),
                "project": params.get("project") or self.project,
                "resource": params.get("resource"),
            }
        )

    def _update_policy_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``updatePolicy`` into ``iam.set_policy``.

        The abstract op carries the full IAM policy document under
        ``params.policy``; we forward it to the IAM action handler.
        """
        params = action.get("params", {})
        return self._execute_iam_action(
            {
                "op": "iam.set_policy",
                "project": params.get("project") or self.project,
                "resource": params.get("resource"),
                "policy": params.get("policy", {}),
            }
        )

    def _publish_event_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``publishEvent`` (v0.7.1 abstract op) into a
        Pub/Sub topic-publish action.

        ``params`` carries the topic and message payload; routed
        through the existing ``ps.publish`` handler.
        """
        params = action.get("params", {})
        topic = params.get("topic")
        if not topic:
            raise ProviderError("publishEvent requires 'topic' in params")
        return self._execute_pubsub_action(
            {
                "op": "ps.publish",
                "topic": topic,
                "project": params.get("project") or self.project,
                "data": params.get("data") or params.get("payload") or {},
                "attributes": params.get("attributes", {}),
            }
        )

    def _custom_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Translate ``custom`` (v0.7.1 abstract op) into the
        provider-specific operation the author embedded under
        ``params.op``. The author is responsible for naming a
        valid native op (e.g. ``bq.ensure_dataset``); we just
        forward the params dict as the action.
        """
        params = action.get("params", {})
        native_op = params.get("op")
        if not native_op:
            raise ProviderError(
                "custom requires 'op' field naming the native operation to dispatch"
            )
        # Build a flat action dict with the native op; let the dispatcher route.
        forwarded = dict(params)
        forwarded["op"] = native_op
        return self._execute_action(forwarded)

    def _execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Dispatch action to appropriate service handler.

        Routes actions based on operation prefix:
        - bq.*: BigQuery operations
        - gcs.*: Cloud Storage operations
        - ps.*: Pub/Sub operations
        - composer.*: Cloud Composer operations
        - dataflow.*: Dataflow operations
        - run.*: Cloud Run operations
        - scheduler.*: Cloud Scheduler operations
        - iam.*: IAM operations
        """
        op = action.get("op")

        if not op:
            raise ProviderError("Action missing required 'op' field")

        # Route to service-specific handlers
        if op.startswith("bq."):
            return self._execute_bigquery_action(action)
        elif op.startswith("gcs."):
            return self._execute_storage_action(action)
        elif op.startswith("ps."):
            return self._execute_pubsub_action(action)
        elif op.startswith("composer."):
            return self._execute_composer_action(action)
        elif op.startswith("dataflow."):
            return self._execute_dataflow_action(action)
        elif op.startswith("run."):
            return self._execute_run_action(action)
        elif op.startswith("scheduler."):
            return self._execute_scheduler_action(action)
        elif op.startswith("iam."):
            return self._execute_iam_action(action)
        elif op.startswith("dbt."):
            return self._execute_dbt_action(action)
        elif op.startswith("dataform."):
            return self._execute_dataform_action(action)
        # 0.7.1 provider actions (no prefix) — translate each abstract
        # op into one or more native ``<service>.<verb>`` actions and
        # dispatch through the corresponding service handler. The
        # translation lives on this provider (not in the planner) so
        # the abstract op semantics are pinned per-provider.
        abstract_handlers = {
            "provisionDataset": self._provision_dataset_071,
            "scheduleTask": self._schedule_task_071,
            "registerSchema": self._register_schema_071,
            "createView": self._create_view_071,
            "grantAccess": self._grant_access_071,
            "revokeAccess": self._revoke_access_071,
            "updatePolicy": self._update_policy_071,
            "publishEvent": self._publish_event_071,
            "custom": self._custom_071,
        }
        if op in abstract_handlers:
            return abstract_handlers[op](action)

        self.warn_kv(event="unknown_action_op", op=op, action_id=action.get("id"))
        return {
            "status": "skipped",
            "op": op,
            "reason": f"Unknown operation: {op}",
            "changed": False,
        }

    def _execute_bigquery_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute BigQuery operations."""
        from .actions import bigquery

        op = action.get("op")

        if op == "bq.ensure_dataset":
            return with_retry(lambda: bigquery.ensure_dataset(action), self.logger)
        elif op == "bq.ensure_table":
            return with_retry(lambda: bigquery.ensure_table(action), self.logger)
        elif op == "bq.ensure_view":
            return with_retry(lambda: bigquery.ensure_view(action), self.logger)
        elif op == "bq.ensure_routine":
            return with_retry(lambda: bigquery.ensure_routine(action), self.logger)
        elif op == "bq.update_table_schema":
            return with_retry(lambda: bigquery.update_table_schema(action), self.logger)
        elif op == "bq.execute_sql":
            return bigquery.execute_sql(action)
        else:
            raise ProviderError(f"Unknown BigQuery operation: {op}")

    def _execute_storage_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Cloud Storage operations."""
        from .actions import storage

        op = action.get("op")

        if op == "gcs.ensure_bucket":
            return with_retry(lambda: storage.ensure_bucket(action), self.logger)
        elif op == "gcs.ensure_prefix":
            return with_retry(lambda: storage.ensure_prefix(action), self.logger)
        elif op == "gcs.ensure_lifecycle":
            return with_retry(lambda: storage.ensure_lifecycle(action), self.logger)
        else:
            raise ProviderError(f"Unknown Storage operation: {op}")

    def _execute_pubsub_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Pub/Sub operations."""
        from .actions import pubsub

        op = action.get("op")

        if op == "ps.ensure_topic":
            return with_retry(lambda: pubsub.ensure_topic(action), self.logger)
        elif op == "ps.ensure_subscription":
            return with_retry(lambda: pubsub.ensure_subscription(action), self.logger)
        elif op == "ps.publish" or op == "ps.publish_message":
            # ``publishEvent`` translation emits ``ps.publish``; the
            # native handler is named ``publish_message``. Accept both
            # so the abstract path doesn't silently no-op.
            return pubsub.publish_message(action)
        else:
            raise ProviderError(f"Unknown Pub/Sub operation: {op}")

    def _execute_composer_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Cloud Composer operations."""
        from .actions import composer

        op = action.get("op")

        if op == "composer.deploy_dag":
            return composer.deploy_dag(action)
        elif op == "composer.trigger_dag":
            return composer.trigger_dag(action)
        elif op == "composer.ensure_variables":
            return composer.ensure_variables(action)
        else:
            raise ProviderError(f"Unknown Composer operation: {op}")

    def _execute_dataflow_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Dataflow operations."""
        from .actions import dataflow

        op = action.get("op")

        if op == "dataflow.ensure_template":
            return dataflow.ensure_template(action)
        elif op == "dataflow.launch_job":
            return dataflow.launch_job(action)
        else:
            raise ProviderError(f"Unknown Dataflow operation: {op}")

    def _execute_run_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Cloud Run operations."""
        from .actions import run

        op = action.get("op")

        if op == "run.ensure_service":
            return run.ensure_service(action)
        else:
            raise ProviderError(f"Unknown Run operation: {op}")

    def _execute_scheduler_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Cloud Scheduler operations."""
        from .actions import scheduler

        op = action.get("op")

        if op == "scheduler.ensure_job":
            return scheduler.ensure_job(action)
        else:
            raise ProviderError(f"Unknown Scheduler operation: {op}")

    def _execute_iam_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute IAM operations."""
        from .actions import iam

        op = action.get("op")

        if op == "iam.bind_bq_dataset":
            return iam.bind_bq_dataset(action)
        elif op == "iam.bind_bq_table":
            return iam.bind_bq_table(action)
        elif op == "iam.bind_gcs_bucket":
            return iam.bind_gcs_bucket(action)
        elif op == "iam.bind_pubsub_topic":
            return iam.bind_pubsub_topic(action)
        elif op == "iam.grant_role":
            return iam.grant_role(action)
        elif op == "iam.revoke_role":
            return iam.revoke_role(action)
        elif op == "iam.set_policy":
            return iam.set_policy(action)
        else:
            raise ProviderError(f"Unknown IAM operation: {op}")

    def _execute_dbt_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute dbt operations."""
        from .runtime import dbt_runner

        op = action.get("op")

        if op == "dbt.prepare_profile":
            return dbt_runner.prepare_profile(action)
        elif op == "dbt.run":
            return dbt_runner.run_dbt(action)
        elif op == "dbt.test":
            return dbt_runner.test_dbt(action)
        else:
            raise ProviderError(f"Unknown dbt operation: {op}")

    def _execute_dataform_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Dataform operations."""
        from .runtime import dataform_runner

        op = action.get("op")

        if op == "dataform.compile":
            return dataform_runner.compile_dataform(action)
        elif op == "dataform.run":
            return dataform_runner.run_dataform(action)
        else:
            raise ProviderError(f"Unknown Dataform operation: {op}")

    # NOTE: _provision_dataset_071 is defined above (line ~249).
    # Do not duplicate — Python silently uses the last definition.

    def _schedule_task_071(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle scheduleTask action from 0.7.1 contracts.
        Tasks are delegated to orchestration engines (Airflow/Composer).
        """
        params = action.get("params", {})
        build_id = params.get("buildId", "unknown")

        self.info_kv(
            event="schedule_task_skipped",
            build_id=build_id,
            reason="Task scheduling delegated to orchestration engine",
        )

        return {
            "status": "ok",
            "op": "scheduleTask",
            "changed": False,
            "skipped": True,
            "message": "Task scheduling handled by orchestration engine (Airflow/Composer)",
        }

    def apply_policy(self, policy_data: Dict[str, Any], mode: str = "check") -> Dict[str, Any]:
        """
        Apply IAM policy bindings from compiled policy.

        Args:
            policy_data: Compiled policy with bindings list
            mode: "check" (dry-run) or "enforce" (apply)

        Returns:
            Status dict with results
        """
        from google.api_core import exceptions
        from google.cloud import bigquery

        # Data Catalog is optional - only needed for policy tag permissions
        try:
            from google.cloud import datacatalog_v1

            has_datacatalog = True
        except ImportError:
            has_datacatalog = False
            if mode == "enforce":
                self.logger.warning(
                    "google-cloud-datacatalog not installed - policy tag permissions will be skipped"
                )

        bindings = policy_data.get("bindings", [])
        if not bindings:
            return {"status": "ok", "applied": 0, "message": "No bindings to apply"}

        results = []
        applied = 0
        failed = 0
        seen_errors = set()  # Deduplicate repeated error messages

        for binding in bindings:
            resource_type = binding.get("resource_type")
            principal = binding.get("principal")
            roles = binding.get("roles", [])

            try:
                if resource_type == "bigquery.dataset":
                    # For BigQuery policy tags, we need to grant fine-grained reader role
                    # This is done via Data Catalog policy tags, not BigQuery IAM
                    project = binding.get("project", self.project)
                    dataset = binding.get("dataset")

                    if mode == "enforce":
                        # Grant Data Catalog Fine-Grained Reader role
                        # This allows reading policy-tagged columns
                        if has_datacatalog:
                            self._grant_policy_tag_reader(project, dataset, principal)
                        else:
                            self.logger.warning(
                                f"Skipping policy tag permissions for {principal} - datacatalog not available"
                            )

                        # Also grant BigQuery dataset-level permissions
                        bq_client = bigquery.Client(project=project)
                        dataset_ref = bq_client.dataset(dataset, project=project)
                        policy = bq_client.get_iam_policy(dataset_ref)

                        for role in roles:
                            # Check if binding already exists
                            existing_binding = None
                            for b in policy.bindings:
                                if b.get("role") == role:
                                    existing_binding = b
                                    break

                            if existing_binding:
                                # Add member to existing binding
                                members = set(existing_binding.get("members", []))
                                members.add(principal)
                                existing_binding["members"] = list(members)
                            else:
                                # Create new binding
                                policy.bindings.append({"role": role, "members": [principal]})

                        bq_client.set_iam_policy(dataset_ref, policy)
                        results.append(f"✅ Granted {roles} to {principal} on {project}.{dataset}")
                        applied += 1
                    else:
                        results.append(
                            f"🔍 Would grant {roles} to {principal} on {project}.{dataset}"
                        )

                elif resource_type == "gcs.bucket":
                    bucket = binding.get("bucket")
                    if mode == "enforce":
                        from google.cloud import storage

                        client = storage.Client(project=self.project)
                        bucket_obj = client.bucket(bucket)
                        policy = bucket_obj.get_iam_policy()

                        for role in roles:
                            # Check if binding already exists
                            existing_binding = None
                            for b in policy.bindings:
                                if b.get("role") == role:
                                    existing_binding = b
                                    break

                            if existing_binding:
                                # Add member to existing binding
                                members = set(existing_binding.get("members", []))
                                members.add(principal)
                                existing_binding["members"] = list(members)
                            else:
                                # Create new binding
                                policy.bindings.append({"role": role, "members": [principal]})

                        bucket_obj.set_iam_policy(policy)
                        results.append(f"✅ Granted {roles} to {principal} on gs://{bucket}")
                        applied += 1
                    else:
                        results.append(f"🔍 Would grant {roles} to {principal} on gs://{bucket}")

            except exceptions.GoogleAPIError as e:
                import traceback

                error_msg = str(e)
                error_key = error_msg[:120]  # Deduplicate by truncated message

                # Only log full details for first occurrence of each error
                if error_key not in seen_errors:
                    seen_errors.add(error_key)
                    self.logger.warning(f"GCP API error for {principal}: {error_msg}")
                    self.logger.debug(traceback.format_exc())
                    if "requires allowlisting" in error_msg:
                        self.logger.warning(
                            "BigQuery dataset IAM not enabled for project. Contact GCP support to enable."
                        )

                # Always record the result per principal
                if "requires allowlisting" in error_msg:
                    results.append(
                        f"⚠️ Skipped {principal}: BigQuery dataset IAM requires allowlisting in GCP project"
                    )
                else:
                    results.append(f"❌ Failed {principal}: {error_msg}")
                failed += 1
            except Exception as e:
                import traceback

                error_msg = str(e)
                error_key = error_msg[:120]
                if error_key not in seen_errors:
                    seen_errors.add(error_key)
                    self.logger.error(f"Unexpected error for {principal}: {error_msg}")
                    self.logger.debug(traceback.format_exc())
                results.append(f"❌ Unexpected error for {principal}: {error_msg}")
                failed += 1

        return {
            "status": "ok" if failed == 0 else "partial",
            "mode": mode,
            "applied": applied,
            "failed": failed,
            "results": results,
        }

    def _grant_policy_tag_reader(self, project: str, dataset: str, principal: str) -> None:
        """Grant fine-grained reader permission for policy tags on a dataset."""
        try:
            from google.cloud import bigquery, datacatalog_v1
        except ImportError:
            self.logger.error(
                "google-cloud-datacatalog not installed - cannot grant policy tag permissions"
            )
            return

        # Get the dataset to find its policy tags
        bq_client = bigquery.Client(project=project)
        dataset_ref = bq_client.dataset(dataset, project=project)
        bq_client.get_dataset(dataset_ref)

        # Get all tables in the dataset
        tables = list(bq_client.list_tables(dataset_ref))

        # For each table, find policy tags and grant reader permission
        dc_client = datacatalog_v1.PolicyTagManagerClient()

        for table_ref in tables:
            table = bq_client.get_table(table_ref)

            # Check each field for policy tags
            for field in table.schema:
                if field.policy_tags and field.policy_tags.names:
                    for tag_name in field.policy_tags.names:
                        # Grant fine-grained reader role on this policy tag
                        try:
                            policy = dc_client.get_iam_policy(resource=tag_name)

                            # Check if binding already exists
                            role = "roles/datacatalog.categoryFineGrainedReader"
                            existing_binding = None
                            for b in policy.bindings:
                                if b.role == role:
                                    existing_binding = b
                                    break

                            if existing_binding:
                                # Add member to existing binding
                                if principal not in existing_binding.members:
                                    existing_binding.members.append(principal)
                            else:
                                # Add fine-grained reader binding
                                policy.bindings.append(
                                    datacatalog_v1.Binding(role=role, members=[principal])
                                )

                            dc_client.set_iam_policy(resource=tag_name, policy=policy)
                            self.info_kv(
                                event="policy_tag_permission_granted",
                                principal=principal,
                                tag=tag_name,
                                table=f"{project}.{dataset}.{table.table_id}",
                            )
                        except Exception as e:
                            self.warn_kv(
                                event="policy_tag_grant_failed",
                                principal=principal,
                                tag=tag_name,
                                error=str(e),
                            )

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
