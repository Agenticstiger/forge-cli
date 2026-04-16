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

# fluid_build/providers/gcp/gcp.py
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from fluid_build.providers.base import BaseProvider, ProviderError


class GcpProvider(BaseProvider):
    """
    GCP provider with BigQuery integration.
    - Uses Application Default Credentials (ADC) for authentication
    - Creates datasets and tables idempotently
    - Lazy-loads BigQuery client to avoid auth issues at import time
    """

    def __init__(
        self, project: Optional[str] = None, region: Optional[str] = "europe-west3", logger=None
    ) -> None:
        super().__init__(project=project, region=region, logger=logger)
        self._bq_client = None  # Lazy-load to avoid auth at import
        self._log_info(
            "provider_init", {"provider": "gcp", "project": self.project, "region": self.region}
        )

    def _get_bq_client(self):
        """Lazy-load BigQuery client with proper error handling."""
        if self._bq_client is None:
            try:
                from google.cloud import bigquery

                self._bq_client = bigquery.Client(project=self.project)
                self._log_info("bigquery_client_initialized", {"project": self.project})
            except Exception as e:
                raise ProviderError(
                    f"Failed to initialize BigQuery client: {e}\n"
                    "Ensure you're authenticated with: gcloud auth application-default login"
                )
        return self._bq_client

    # ---------------- CLI-facing methods ----------------

    def _extract_governance_labels(
        self, contract: Dict[str, Any], expose: Dict[str, Any] = None
    ) -> Dict[str, str]:
        """
        Extract governance labels from FLUID contract for GCP resources.

        Combines:
        - Contract-level labels and metadata
        - Expose-level labels and policy metadata
        - Policy classification converted to GCP labels

        Args:
            contract: Full FLUID contract
            expose: Specific expose definition (optional)

        Returns:
            Dict of string labels ready for BigQuery/GCP
        """
        labels = {}

        # Extract contract-level labels
        contract_labels = contract.get("labels", {})
        for key, value in contract_labels.items():
            # GCP labels must be lowercase, alphanumeric + underscores/hyphens
            safe_key = key.lower().replace(" ", "_").replace(".", "_")
            safe_value = (
                str(value).lower().replace(" ", "_").replace(".", "_")[:63]
            )  # 63 char limit
            labels[safe_key] = safe_value

        # Extract contract-level tags (convert to labels)
        for tag in contract.get("tags", []):
            safe_tag = tag.lower().replace(" ", "_").replace("-", "_")
            labels[f"tag_{safe_tag}"] = "true"

        # Extract metadata fields
        metadata = contract.get("metadata", {})
        if "layer" in metadata:
            labels["fluid_layer"] = metadata["layer"].lower()

        owner = metadata.get("owner", {})
        if "team" in owner:
            labels["fluid_team"] = owner["team"].replace("@", "_at_").replace(".", "_")[:63]
        if "email" in owner:
            labels["fluid_owner"] = owner["email"].replace("@", "_at_").replace(".", "_")[:63]

        # Add contract ID for traceability
        labels["fluid_contract_id"] = contract.get("id", "unknown").replace(".", "_")[:63]

        # Extract expose-level governance if provided
        if expose:
            # Expose-level labels
            expose_labels = expose.get("labels", {})
            for key, value in expose_labels.items():
                safe_key = key.lower().replace(" ", "_").replace(".", "_")
                safe_value = str(value).lower().replace(" ", "_").replace(".", "_")[:63]
                labels[safe_key] = safe_value

            # Expose-level tags
            for tag in expose.get("tags", []):
                safe_tag = tag.lower().replace(" ", "_").replace("-", "_")
                labels[f"tag_{safe_tag}"] = "true"

            # Policy classification
            policy = expose.get("policy", {})
            if "classification" in policy:
                labels["data_classification"] = policy["classification"].lower()

            # Policy authentication method
            if "authn" in policy:
                labels["authn_method"] = policy["authn"].lower()

            # Policy labels and tags
            policy_labels = policy.get("labels", {})
            for key, value in policy_labels.items():
                safe_key = f"policy_{key}".lower().replace(" ", "_").replace(".", "_")
                safe_value = str(value).lower().replace(" ", "_").replace(".", "_")[:63]
                labels[safe_key] = safe_value

            for tag in policy.get("tags", []):
                safe_tag = tag.lower().replace(" ", "_").replace("-", "_")
                labels[f"policy_{safe_tag}"] = "true"

        return labels

    def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Turn a FLUID contract into GCP actions (idempotent)."""
        self._log_info("plan_start", {"contract_id": contract.get("id")})
        actions: List[Dict[str, Any]] = []

        # Process builds to find source data loading instructions
        for build in contract.get("builds", []):
            props = build.get("properties", {})
            sources = props.get("sources", [])

            for source in sources:
                table_path = source.get("table", "")  # Format: "dataset.table"
                csv_path = source.get("csvPath")

                if table_path and csv_path:
                    parts = table_path.split(".")
                    if len(parts) == 2:
                        dataset_id, table_id = parts
                        # Ensure dataset exists
                        actions.append(
                            {
                                "op": "ensure_dataset",
                                "project": self.project,
                                "dataset": dataset_id,
                            }
                        )
                        # Load CSV (will create table with autodetect)
                        actions.append(
                            {
                                "op": "load_csv",
                                "project": self.project,
                                "dataset": dataset_id,
                                "table": table_id,
                                "csv_path": csv_path,
                            }
                        )

        # Process exposes (output tables)
        for expo in contract.get("exposes", []):
            # Support both 0.5.7 (binding) and legacy (location) formats
            binding = expo.get("binding", {})
            loc = binding.get("location", {})
            fmt = binding.get("format")

            if fmt == "bigquery_table":
                schema = expo.get("contract", {}).get("schema", [])
                region = loc.get("region", "US")  # Use region from contract, default to US

                # Extract governance labels from contract and expose
                governance_labels = self._extract_governance_labels(contract, expo)
                self._log_info(
                    "governance_labels_extracted",
                    {
                        "expose_id": expo.get("exposeId"),
                        "label_count": len(governance_labels),
                    },
                )

                # Dataset action with governance labels
                dataset_description = expo.get("description") or contract.get("description", "")
                actions.append(
                    {
                        "op": "ensure_dataset",
                        "project": loc.get("project") or self.project,
                        "dataset": loc.get("dataset"),
                        "location": region,
                        "description": dataset_description,
                        "labels": governance_labels,
                    }
                )

                # Table action with governance labels
                table_description = expo.get("title") or expo.get("description", "")
                actions.append(
                    {
                        "op": "ensure_table",
                        "project": loc.get("project") or self.project,
                        "dataset": loc.get("dataset"),
                        "table": loc.get("table"),
                        "schema": schema,
                        "description": table_description,
                        "labels": governance_labels,
                    }
                )

        self._log_info("plan_end", {"actions": len(actions)})
        return actions

    def apply(self, actions: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        """Execute actions; unimplemented ops return ok/skip with logging."""
        t0 = time.time()
        results: List[Dict[str, Any]] = []
        applied = failed = 0

        self._log_info("apply_start", {"actions": len(actions)})

        for i, a in enumerate(actions):
            op = a.get("op")
            try:
                # 0.7.1 provider actions
                if op == "provisionDataset":
                    res = self._provision_dataset(a)
                elif op == "scheduleTask":
                    res = self._schedule_task(a)
                # Legacy 0.5.7 operations
                elif op == "ensure_dataset":
                    res = self._ensure_dataset(a)
                elif op == "ensure_table":
                    res = self._ensure_table(a)
                elif op == "load_gcs_to_bq":
                    res = self._load_gcs_to_bq(a)
                elif op == "set_iam":
                    res = self._set_iam(a)
                elif op == "schedule_composer_dag":
                    res = self._schedule_composer(a)
                elif op == "run_dbt":
                    res = self._run_dbt(a)
                elif op == "load_csv":
                    res = self._load_csv(a)
                else:
                    self._log_warning("unknown_action", {"op": op})
                    res = {"status": "ok", "op": "noop", "skipped": True, "original_op": op}

                results.append({"i": i, **res})
                if res.get("status") == "ok" and not res.get("skipped", False):
                    applied += 1

            except Exception as e:  # noqa: BLE001
                failed += 1
                error_msg = str(e)
                # Extract more details from Google API exceptions
                if hasattr(e, "message"):
                    error_msg = e.message
                elif hasattr(e, "_errors"):
                    error_msg = str(e._errors)

                self._log_error("apply_action_failed", {"op": op, "error": error_msg})
                results.append({"i": i, "status": "error", "op": op, "error": error_msg})

        out = {
            "provider": "gcp",
            "applied": applied,
            "failed": failed,
            "duration_sec": round(time.time() - t0, 3),
            "timestamp": self._ts(),
            "results": results,
        }
        self._log_info("apply_end", {k: v for k, v in out.items() if k != "results"})
        return out

    def render(self, actions: List[Dict[str, Any]]) -> str:
        return json.dumps({"provider": "gcp", "actions": actions}, indent=2)

    # ---------------- Operation handlers ----------------

    def _ensure_dataset(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """Create BigQuery dataset if it doesn't exist (idempotent)."""
        project = a.get("project") or self.project
        dataset_id = a.get("dataset")

        if not (project and dataset_id):
            raise ProviderError("ensure_dataset requires project and dataset")

        try:
            from google.api_core import exceptions as gcp_exceptions
            from google.cloud import bigquery

            client = self._get_bq_client()
            dataset_ref = f"{project}.{dataset_id}"

            try:
                # Try to get existing dataset
                dataset = client.get_dataset(dataset_ref)
                self._log_info(
                    "dataset_exists",
                    {"project": project, "dataset": dataset_id, "location": dataset.location},
                )
                return {
                    "status": "ok",
                    "op": "ensure_dataset",
                    "action": "exists",
                    "dataset": dataset_ref,
                    "location": dataset.location,
                }

            except gcp_exceptions.NotFound:
                # Dataset doesn't exist, create it
                dataset = bigquery.Dataset(dataset_ref)
                dataset.location = self.region or "US"
                dataset.description = f"FLUID Data Product dataset - {dataset_id}"

                try:
                    created_dataset = client.create_dataset(dataset, timeout=30)
                    self._log_info(
                        "dataset_created",
                        {
                            "project": project,
                            "dataset": dataset_id,
                            "location": created_dataset.location,
                        },
                    )
                    return {
                        "status": "ok",
                        "op": "ensure_dataset",
                        "action": "created",
                        "dataset": dataset_ref,
                        "location": created_dataset.location,
                    }
                except gcp_exceptions.Conflict:
                    # Race condition: created between get and create
                    dataset = client.get_dataset(dataset_ref)
                    self._log_info(
                        "dataset_exists_after_conflict",
                        {"project": project, "dataset": dataset_id, "location": dataset.location},
                    )
                    return {
                        "status": "ok",
                        "op": "ensure_dataset",
                        "action": "exists",
                        "dataset": dataset_ref,
                        "location": dataset.location,
                    }

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to ensure dataset {project}.{dataset_id}: {e}")

    def _ensure_table(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """Create BigQuery table if it doesn't exist (idempotent)."""
        project = a.get("project") or self.project
        dataset_id = a.get("dataset")
        table_id = a.get("table")
        schema_spec = a.get("schema", [])

        if not (project and dataset_id and table_id):
            raise ProviderError("ensure_table requires project, dataset, table")

        try:
            from google.api_core import exceptions as gcp_exceptions
            from google.cloud import bigquery

            client = self._get_bq_client()
            table_ref = f"{project}.{dataset_id}.{table_id}"

            try:
                # Try to get existing table
                table = client.get_table(table_ref)
                self._log_info(
                    "table_exists",
                    {
                        "project": project,
                        "dataset": dataset_id,
                        "table": table_id,
                        "num_rows": table.num_rows,
                        "schema_fields": len(table.schema),
                    },
                )
                return {
                    "status": "ok",
                    "op": "ensure_table",
                    "action": "exists",
                    "table": table_ref,
                    "num_rows": table.num_rows,
                    "schema_fields": len(table.schema),
                }

            except gcp_exceptions.NotFound:
                # Table doesn't exist - only create if schema is provided
                # (tables without schema will be created by load_csv with autodetect)
                if not schema_spec:
                    self._log_info(
                        "table_skipped_no_schema",
                        {
                            "project": project,
                            "dataset": dataset_id,
                            "table": table_id,
                            "note": "Will be created by data load",
                        },
                    )
                    return {
                        "status": "ok",
                        "op": "ensure_table",
                        "action": "skipped",
                        "table": table_ref,
                        "note": "No schema - will be created by load operation",
                    }

                # Create table with explicit schema
                table = bigquery.Table(table_ref)

                # Convert FLUID schema to BigQuery schema
                bq_schema = []
                for field in schema_spec:
                    field_name = field.get("name")
                    field_type = field.get("type", "STRING").upper()

                    # Map common type aliases
                    type_mapping = {
                        "VARCHAR": "STRING",
                        "INT": "INTEGER",
                        "INT64": "INTEGER",
                        "FLOAT64": "FLOAT",
                        "BOOL": "BOOLEAN",
                        "DATETIME": "TIMESTAMP",
                    }
                    field_type = type_mapping.get(field_type, field_type)

                    # Determine mode (NULLABLE or REQUIRED)
                    mode = "REQUIRED" if field.get("required", False) else "NULLABLE"

                    bq_schema.append(
                        bigquery.SchemaField(
                            name=field_name,
                            field_type=field_type,
                            mode=mode,
                            description=field.get("description", ""),
                        )
                    )

                table.schema = bq_schema

                table.description = f"FLUID Data Product table - {table_id}"

                try:
                    created_table = client.create_table(table, timeout=30)
                    self._log_info(
                        "table_created",
                        {
                            "project": project,
                            "dataset": dataset_id,
                            "table": table_id,
                            "schema_fields": len(created_table.schema),
                        },
                    )
                    return {
                        "status": "ok",
                        "op": "ensure_table",
                        "action": "created",
                        "table": table_ref,
                        "schema_fields": len(created_table.schema),
                    }
                except gcp_exceptions.Conflict:
                    # Race condition: created between get and create
                    table = client.get_table(table_ref)
                    self._log_info(
                        "table_exists_after_conflict",
                        {
                            "project": project,
                            "dataset": dataset_id,
                            "table": table_id,
                            "num_rows": table.num_rows,
                            "schema_fields": len(table.schema),
                        },
                    )
                    return {
                        "status": "ok",
                        "op": "ensure_table",
                        "action": "exists",
                        "table": table_ref,
                        "num_rows": table.num_rows,
                        "schema_fields": len(table.schema),
                    }

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to ensure table {project}.{dataset_id}.{table_id}: {e}")

    def _load_gcs_to_bq(self, a: Dict[str, Any]) -> Dict[str, Any]:
        self._log_info("load_gcs_to_bq", {"note": "stub"})
        return {"status": "ok", "op": "load_gcs_to_bq", "skipped": True}

    def _set_iam(self, a: Dict[str, Any]) -> Dict[str, Any]:
        self._log_info("set_iam", {"note": "stub"})
        return {"status": "ok", "op": "set_iam", "skipped": True}

    def _schedule_composer(self, a: Dict[str, Any]) -> Dict[str, Any]:
        self._log_info("schedule_composer_dag", {"note": "stub"})
        return {"status": "ok", "op": "schedule_composer_dag", "skipped": True}

    def _run_dbt(self, a: Dict[str, Any]) -> Dict[str, Any]:
        self._log_info("run_dbt", {"note": "stub"})
        return {"status": "ok", "op": "run_dbt", "skipped": True}

    # ---------------- 0.7.1 Provider Actions ----------------

    def _provision_dataset(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """
        Provision BigQuery dataset from 0.7.1 provisionDataset action.
        Maps from params structure to ensure_dataset call.
        """
        params = a.get("params", {})
        binding = params.get("binding", {})
        location = binding.get("location", {})

        # Extract dataset info from binding
        project = location.get("project") or self.project
        dataset_id = location.get("dataset")

        if not dataset_id:
            raise ProviderError("provisionDataset requires dataset in binding.location")

        # Delegate to ensure_dataset
        return self._ensure_dataset({"project": project, "dataset": dataset_id})

    def _schedule_task(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """
        Schedule task from 0.7.1 scheduleTask action.
        For now, this is a stub - tasks will be executed manually or via Airflow.
        """
        params = a.get("params", {})
        build_id = params.get("buildId", "unknown")

        self._log_info(
            "schedule_task",
            {
                "build_id": build_id,
                "note": "Task scheduling delegated to orchestration engine (Airflow/Composer)",
            },
        )

        return {
            "status": "ok",
            "op": "scheduleTask",
            "skipped": True,
            "message": "Task scheduling handled by orchestration engine",
        }

    def _load_csv(self, a: Dict[str, Any]) -> Dict[str, Any]:
        """Load CSV file into BigQuery table."""
        project = a.get("project") or self.project
        dataset_id = a.get("dataset")
        table_id = a.get("table")
        csv_path = a.get("csv_path")

        if not (project and dataset_id and table_id and csv_path):
            raise ProviderError("load_csv requires project, dataset, table, and csv_path")

        try:
            import os

            from google.cloud import bigquery

            client = self._get_bq_client()
            table_ref = f"{project}.{dataset_id}.{table_id}"

            if not os.path.exists(csv_path):
                raise ProviderError(f"CSV file not found: {csv_path}")

            job_config = bigquery.LoadJobConfig(
                source_format=bigquery.SourceFormat.CSV,
                skip_leading_rows=1,
                autodetect=True,  # Auto-detect schema from CSV
                write_disposition="WRITE_TRUNCATE",  # Replace existing data
            )

            with open(csv_path, "rb") as source_file:
                load_job = client.load_table_from_file(
                    source_file, table_ref, job_config=job_config
                )

            # Wait for completion
            load_job.result()

            # Get final table stats
            table = client.get_table(table_ref)
            self._log_info(
                "csv_loaded",
                {
                    "project": project,
                    "dataset": dataset_id,
                    "table": table_id,
                    "csv_path": csv_path,
                    "rows_loaded": table.num_rows,
                },
            )

            return {
                "status": "ok",
                "op": "load_csv",
                "table": table_ref,
                "rows_loaded": table.num_rows,
                "csv_path": csv_path,
            }

        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(
                f"Failed to load CSV {csv_path} into {project}.{dataset_id}.{table_id}: {e}"
            )

    # ---------------- Logging helpers ----------------

    def _log_info(self, msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if self.logger:
            self.logger.info(msg, extra=extra or {})

    def _log_warning(self, msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if self.logger:
            self.logger.warning(msg, extra=extra or {})

    def _log_error(self, msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
        if self.logger:
            self.logger.error(msg, extra=extra or {})

    @staticmethod
    def _ts() -> str:
        import datetime

        return datetime.datetime.utcnow().isoformat() + "Z"
