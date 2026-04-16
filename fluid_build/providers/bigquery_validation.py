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
Google Cloud Platform BigQuery validation provider.

Implements the ValidationProvider interface for validating FLUID contracts
against actual BigQuery resources.
"""

from typing import Any, Dict, List, Optional

from google.api_core import exceptions as google_exceptions
from google.cloud import bigquery

from fluid_build.providers.quality_engine import (
    execute_quality_checks,
    quality_results_to_issues,
)
from fluid_build.providers.validation_provider import (
    FieldSchema,
    ResourceSchema,
    ResourceType,
    ValidationIssue,
    ValidationProvider,
    ValidationResult,
)


class BigQueryValidationProvider(ValidationProvider):
    """Validation provider for Google Cloud BigQuery"""

    # Type mapping from BigQuery types to standard types
    TYPE_MAPPINGS = {
        "STRING": ["VARCHAR", "CHAR", "TEXT"],
        "INTEGER": ["INT", "INT64", "BIGINT"],
        "FLOAT": ["FLOAT64", "DOUBLE", "DECIMAL", "NUMERIC"],
        "BOOLEAN": ["BOOL"],
        "TIMESTAMP": ["DATETIME"],
        "DATE": ["DATE"],
        "TIME": ["TIME"],
        "BYTES": ["BINARY"],
        "RECORD": ["STRUCT"],
        "ARRAY": ["REPEATED"],
    }

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize BigQuery validation provider.

        Args:
            config: Configuration containing:
                - project_id: GCP project ID
                - location: Optional BigQuery location/region
        """
        super().__init__(config)
        self.project_id = config.get("project_id")
        self.location = config.get("location", "US")
        self._client = None

    @property
    def provider_name(self) -> str:
        return "gcp"

    @property
    def client(self) -> bigquery.Client:
        """Lazy-load BigQuery client"""
        if self._client is None:
            self._client = bigquery.Client(project=self.project_id)
        return self._client

    def validate_connection(self) -> bool:
        """Test BigQuery connection"""
        try:
            # Try to list datasets (minimal operation)
            list(self.client.list_datasets(max_results=1))
            return True
        except Exception:
            return False

    def get_resource_schema(self, resource_spec: Dict[str, Any]) -> Optional[ResourceSchema]:
        """
        Retrieve BigQuery table schema.

        Args:
            resource_spec: Resource specification with binding.resource containing:
                - dataset: Dataset name
                - table: Table name

        Returns:
            ResourceSchema if table exists, None otherwise
        """
        try:
            # Extract table reference
            table_fqn = self._extract_table_fqn(resource_spec)
            if not table_fqn:
                return None

            # Get table reference
            table_ref = self.client.get_table(table_fqn)

            # Convert BigQuery schema to FieldSchema list
            fields = []
            for field in table_ref.schema:
                fields.append(
                    FieldSchema(
                        name=field.name,
                        type=field.field_type,
                        mode=field.mode,
                        description=field.description,
                    )
                )

            # Create ResourceSchema
            return ResourceSchema(
                resource_type=ResourceType.TABLE,
                fully_qualified_name=table_fqn,
                fields=fields,
                row_count=table_ref.num_rows,
                size_bytes=table_ref.num_bytes,
                last_modified=table_ref.modified.isoformat() if table_ref.modified else None,
                metadata={
                    "table_type": table_ref.table_type,
                    "location": table_ref.location,
                    "created": table_ref.created.isoformat() if table_ref.created else None,
                    "description": table_ref.description,
                },
            )

        except google_exceptions.NotFound:
            return None
        except Exception as e:
            # Re-raise with more context
            raise Exception(f"Error retrieving BigQuery schema: {str(e)}") from e

    def validate_resource(
        self, contract_spec: Dict[str, Any], actual_schema: Optional[ResourceSchema]
    ) -> ValidationResult:
        """
        Validate BigQuery resource against contract.

        Args:
            contract_spec: Expected resource specification from contract
            actual_schema: Actual schema from BigQuery (or None if not found)

        Returns:
            ValidationResult with all validation issues
        """
        issues = []
        resource_name = self._get_resource_name(contract_spec)

        # Check if resource exists
        if actual_schema is None:
            table_fqn = self._extract_table_fqn(contract_spec)
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="missing_resource",
                    message=f"Table '{table_fqn}' does not exist in BigQuery",
                    path="exposes[].binding.resource",
                    expected=table_fqn,
                    actual=None,
                    suggestion=f"Create the table using: bq mk --table {table_fqn}",
                    documentation_url="https://cloud.google.com/bigquery/docs/tables",
                )
            )

            return ValidationResult(
                resource_name=resource_name, success=False, issues=issues, schema=None
            )

        # Extract expected schema from contract
        expected_fields = self._extract_expected_fields(contract_spec)

        if expected_fields:
            # Compare schemas
            schema_issues = self.compare_schemas(expected_fields, actual_schema.fields)
            issues.extend(schema_issues)

        # Validate row count
        if actual_schema.row_count == 0:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="empty_table",
                    message=f"Table '{actual_schema.fully_qualified_name}' has no rows",
                    path="exposes[].binding.resource",
                    expected="> 0 rows",
                    actual="0 rows",
                    suggestion="Verify that data has been loaded into the table",
                )
            )

        # Check for quality SLA if specified
        sla = contract_spec.get("sla", {})
        if "row_count_min" in sla:
            min_rows = sla["row_count_min"]
            if actual_schema.row_count < min_rows:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="row_count_below_threshold",
                        message=f"Table has {actual_schema.row_count} rows, below minimum of {min_rows}",
                        path="exposes[].sla.row_count_min",
                        expected=f">= {min_rows} rows",
                        actual=f"{actual_schema.row_count} rows",
                        suggestion="Check data pipeline for issues",
                    )
                )

        return ValidationResult(
            resource_name=resource_name,
            success=len([i for i in issues if i.severity == "error"]) == 0,
            issues=issues,
            schema=actual_schema,
        )

    def normalize_type(self, provider_type: str) -> str:
        """
        Normalize BigQuery type to standard type.

        Args:
            provider_type: BigQuery type name

        Returns:
            Normalized type name
        """
        provider_type_upper = provider_type.upper()

        # Check if it's already a standard type
        for standard_type, variants in self.TYPE_MAPPINGS.items():
            if provider_type_upper == standard_type or provider_type_upper in variants:
                return standard_type

        return provider_type_upper

    def _extract_table_fqn(self, resource_spec: Dict[str, Any]) -> Optional[str]:
        """Extract fully qualified table name from resource spec"""
        binding = resource_spec.get("binding", {})
        location = binding.get("location", {})

        # Handle location.properties pattern (standard FLUID contract)
        props = location.get("properties", {})
        if props:
            project = props.get("project", self.project_id)
            dataset = props.get("dataset")
            table = props.get("table")

            if dataset and table:
                return f"{project}.{dataset}.{table}"

        # Fallback: Try binding.resource pattern
        resource = binding.get("resource", {})

        if isinstance(resource, str):
            # If resource is a string, assume it's already FQN or table name
            if "." in resource:
                return resource
            return f"{self.project_id}.{resource}"

        # Extract components from resource dict
        dataset = resource.get("dataset")
        table = resource.get("table")

        if dataset and table:
            return f"{self.project_id}.{dataset}.{table}"

        return None

    def _get_resource_name(self, resource_spec: Dict[str, Any]) -> str:
        """Get display name for resource"""
        return resource_spec.get("id") or resource_spec.get("name") or "unknown"

    def _extract_expected_fields(self, resource_spec: Dict[str, Any]) -> List[FieldSchema]:
        """Extract expected field schemas from contract"""
        fields = []

        # Schema is directly in the expose/resource spec
        schema_fields = resource_spec.get("schema", [])

        # Also check dataset.table.fields pattern for backward compatibility
        if not schema_fields:
            dataset = resource_spec.get("dataset", {})
            table_spec = dataset.get("table", {})
            schema_fields = table_spec.get("fields", [])

        for field in schema_fields:
            if isinstance(field, dict):
                # Handle both 'nullable' boolean and 'mode' string
                mode = field.get("mode", "NULLABLE")
                if "nullable" in field:
                    mode = "NULLABLE" if field["nullable"] else "REQUIRED"

                fields.append(
                    FieldSchema(
                        name=field.get("name", ""),
                        type=field.get("type", "STRING"),
                        mode=mode,
                        description=field.get("description"),
                    )
                )

        return fields

    def run_quality_checks(
        self,
        resource_spec: Dict[str, Any],
        rules: List[Dict[str, Any]],
    ) -> List[ValidationIssue]:
        """Execute DQ rules against BigQuery via SQL."""
        fqn = self._extract_table_fqn(resource_spec)
        if not fqn:
            return [
                ValidationIssue(
                    severity="warning",
                    category="quality",
                    message="Cannot run quality checks: unable to resolve table reference",
                    path="contract.dq.rules",
                )
            ]
        # BigQuery FQN uses backtick quoting: `project.dataset.table`
        table_ref = f"`{fqn}`"

        def _exec(sql):
            rows = self.client.query(sql).result()
            return [tuple(row.values()) for row in rows]

        results = execute_quality_checks(
            rules=rules,
            table_ref=table_ref,
            execute_fn=_exec,
            dialect="bigquery",
        )
        return quality_results_to_issues(results)
