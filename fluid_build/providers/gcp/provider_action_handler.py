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
Provider action handler for GCP (FLUID 0.7.1)
Extends GCP provider to handle declarative provider actions.
"""

from typing import Any, Dict, List

from fluid_build.forge.core.provider_actions import ActionType, ProviderAction


class GCPProviderActionHandler:
    """
    Handles execution of provider actions for GCP.
    Supports BigQuery, GCS, IAM, and other GCP services.
    """

    def __init__(self, provider, logger=None):
        """
        Initialize handler with GCP provider instance.

        Args:
            provider: GcpProvider instance
            logger: Optional logger
        """
        self.provider = provider
        self.logger = logger

    def execute_actions(self, actions: List[ProviderAction]) -> Dict[str, Any]:
        """
        Execute a list of provider actions in order.

        Args:
            actions: List of ProviderAction objects

        Returns:
            Dict with execution results
        """
        results = {"success": True, "actions_executed": 0, "actions_failed": 0, "details": []}

        for action in actions:
            try:
                result = self._execute_single_action(action)
                results["actions_executed"] += 1
                results["details"].append(
                    {"action_id": action.action_id, "status": "success", "result": result}
                )
            except Exception as e:
                results["success"] = False
                results["actions_failed"] += 1
                results["details"].append(
                    {"action_id": action.action_id, "status": "failed", "error": str(e)}
                )
                if self.logger:
                    self.logger.error(f"Action {action.action_id} failed: {e}")

        return results

    def _execute_single_action(self, action: ProviderAction) -> Dict[str, Any]:
        """Execute a single provider action."""

        # Route to appropriate handler based on action type
        if action.action_type == ActionType.PROVISION_DATASET:
            return self._provision_dataset(action)
        elif action.action_type == ActionType.GRANT_ACCESS:
            return self._grant_access(action)
        elif action.action_type == ActionType.REVOKE_ACCESS:
            return self._revoke_access(action)
        elif action.action_type == ActionType.SCHEDULE_TASK:
            return self._schedule_task(action)
        elif action.action_type == ActionType.REGISTER_SCHEMA:
            return self._register_schema(action)
        elif action.action_type == ActionType.CREATE_VIEW:
            return self._create_view(action)
        elif action.action_type == ActionType.UPDATE_POLICY:
            return self._update_policy(action)
        elif action.action_type == ActionType.PUBLISH_EVENT:
            return self._publish_event(action)
        elif action.action_type == ActionType.CUSTOM:
            return self._execute_custom(action)
        else:
            raise ValueError(f"Unsupported action type: {action.action_type}")

    def _provision_dataset(self, action: ProviderAction) -> Dict[str, Any]:
        """
        Provision BigQuery dataset or GCS bucket.

        Config options:
        - type: "bigquery_dataset", "bigquery_table", "gcs_bucket"
        - datasetId: BigQuery dataset name
        - tableId: BigQuery table name
        - bucket: GCS bucket name
        - location: Region (default from provider)
        - labels: Resource labels
        """
        config = action.config
        resource_type = config.get("type", "bigquery_dataset")

        if resource_type == "bigquery_dataset":
            return self._provision_bigquery_dataset(action)
        elif resource_type == "bigquery_table":
            return self._provision_bigquery_table(action)
        elif resource_type == "gcs_bucket":
            return self._provision_gcs_bucket(action)
        else:
            raise ValueError(f"Unknown GCP resource type: {resource_type}")

    def _provision_bigquery_dataset(self, action: ProviderAction) -> Dict[str, Any]:
        """Provision BigQuery dataset idempotently."""
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound

        config = action.config
        dataset_id = config.get("datasetId") or config.get("dataset")
        location = config.get("location", self.provider.region)
        labels = config.get("labels", {})

        if not dataset_id:
            raise ValueError("datasetId is required for BigQuery dataset provisioning")

        client = self.provider._get_bq_client()
        dataset_ref = f"{self.provider.project}.{dataset_id}"

        try:
            dataset = client.get_dataset(dataset_ref)
            return {
                "status": "already_exists",
                "dataset_id": dataset_id,
                "location": dataset.location,
            }
        except NotFound:
            # Create dataset
            dataset = bigquery.Dataset(dataset_ref)
            dataset.location = location
            dataset.labels = labels

            dataset = client.create_dataset(dataset)
            return {"status": "created", "dataset_id": dataset_id, "location": location}

    def _provision_bigquery_table(self, action: ProviderAction) -> Dict[str, Any]:
        """Provision BigQuery table idempotently."""
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound

        config = action.config
        dataset_id = config.get("datasetId") or config.get("dataset")
        table_id = config.get("tableId") or config.get("table")
        schema_json = config.get("schema")

        if not dataset_id or not table_id:
            raise ValueError("Both datasetId and tableId are required")

        client = self.provider._get_bq_client()
        table_ref = f"{self.provider.project}.{dataset_id}.{table_id}"

        try:
            table = client.get_table(table_ref)
            return {"status": "already_exists", "table_id": table_id, "num_rows": table.num_rows}
        except NotFound:
            # Create table
            table = bigquery.Table(table_ref)

            # Set schema if provided
            if schema_json:
                if isinstance(schema_json, str):
                    import json

                    schema_json = json.loads(schema_json)
                table.schema = [
                    bigquery.SchemaField(field["name"], field["type"]) for field in schema_json
                ]

            table = client.create_table(table)
            return {"status": "created", "table_id": table_id}

    def _provision_gcs_bucket(self, action: ProviderAction) -> Dict[str, Any]:
        """Provision GCS bucket idempotently."""
        from google.cloud import storage

        config = action.config
        bucket_name = config.get("bucket") or config.get("bucketName")
        location = config.get("location", self.provider.region)

        if not bucket_name:
            raise ValueError("bucket is required for GCS bucket provisioning")

        client = storage.Client(project=self.provider.project)

        try:
            bucket = client.get_bucket(bucket_name)
            return {"status": "already_exists", "bucket": bucket_name, "location": bucket.location}
        except Exception:
            # Create bucket
            bucket = client.bucket(bucket_name)
            bucket = client.create_bucket(bucket, location=location)
            return {"status": "created", "bucket": bucket_name, "location": location}

    def _grant_access(self, action: ProviderAction) -> Dict[str, Any]:
        """
        Grant IAM access to resource.

        Config options:
        - principal: User/group email or service account
        - role: IAM role (e.g., roles/bigquery.dataViewer)
        - resource: Resource to grant access to (dataset/table/bucket)
        """
        config = action.config
        principal = config.get("principal")
        role = config.get("role", "roles/bigquery.dataViewer")
        resource_type = config.get("resourceType", "dataset")

        if not principal:
            raise ValueError("principal is required for granting access")

        # For BigQuery datasets
        if resource_type == "dataset":
            from google.cloud import bigquery

            dataset_id = config.get("dataset") or config.get("datasetId")
            client = self.provider._get_bq_client()
            dataset = client.get_dataset(f"{self.provider.project}.{dataset_id}")

            # Add IAM binding
            policy = dataset.access_entries
            entry = bigquery.AccessEntry(
                role=role.split("/")[-1],  # Extract role name
                entity_type="userByEmail",
                entity_id=principal,
            )

            if entry not in policy:
                policy.append(entry)
                dataset.access_entries = policy
                client.update_dataset(dataset, ["access_entries"])

                return {
                    "status": "granted",
                    "principal": principal,
                    "role": role,
                    "resource": dataset_id,
                }
            else:
                return {"status": "already_granted", "principal": principal, "role": role}

        return {"status": "not_implemented", "resource_type": resource_type}

    def _revoke_access(self, action: ProviderAction) -> Dict[str, Any]:
        """Revoke IAM access from resource."""
        config = action.config
        config.get("principal")

        # Similar to grant_access but removes entries
        return {"status": "not_implemented", "message": "Revoke access not yet implemented"}

    def _schedule_task(self, action: ProviderAction) -> Dict[str, Any]:
        """Schedule task (typically handled by Airflow)."""
        return {"status": "delegated_to_airflow", "action_id": action.action_id}

    def _register_schema(self, action: ProviderAction) -> Dict[str, Any]:
        """Register schema in data catalog."""
        return {"status": "not_implemented", "message": "Schema registration not yet implemented"}

    def _create_view(self, action: ProviderAction) -> Dict[str, Any]:
        """Create BigQuery view."""
        from google.cloud import bigquery

        config = action.config
        dataset_id = config.get("dataset") or config.get("datasetId")
        view_id = config.get("view") or config.get("viewId")
        query = config.get("query")

        if not all([dataset_id, view_id, query]):
            raise ValueError("dataset, view, and query are required")

        client = self.provider._get_bq_client()
        view_ref = f"{self.provider.project}.{dataset_id}.{view_id}"

        view = bigquery.Table(view_ref)
        view.view_query = query

        try:
            view = client.create_table(view)
            return {"status": "created", "view_id": view_id}
        except Exception:
            # Update if exists
            view = client.update_table(view, ["view_query"])
            return {"status": "updated", "view_id": view_id}

    def _update_policy(self, action: ProviderAction) -> Dict[str, Any]:
        """Update resource policy."""
        return {"status": "not_implemented", "message": "Policy updates not yet implemented"}

    def _publish_event(self, action: ProviderAction) -> Dict[str, Any]:
        """Publish event to Pub/Sub."""
        return {"status": "not_implemented", "message": "Event publishing not yet implemented"}

    def _execute_custom(self, action: ProviderAction) -> Dict[str, Any]:
        """Execute custom action."""
        config = action.config
        command = config.get("command")

        if command:
            # Could execute via subprocess for custom gcloud commands
            return {"status": "custom_command_delegated", "command": command}

        return {"status": "no_custom_command_specified"}
