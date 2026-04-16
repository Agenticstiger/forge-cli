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
AWS validation provider.

Implements the ValidationProvider interface for validating FLUID contracts
against actual AWS resources via the Glue Data Catalog (Athena tables)
and optionally Redshift.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fluid_build.providers.validation_provider import (
    FieldSchema,
    ResourceSchema,
    ResourceType,
    ValidationIssue,
    ValidationProvider,
    ValidationResult,
)

try:
    import boto3
    from botocore.exceptions import ClientError

    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False
    boto3 = None  # type: ignore[assignment]

LOG = logging.getLogger("fluid.providers.aws_validation")


class AWSValidationProvider(ValidationProvider):
    """Validation provider for AWS (Glue Data Catalog / Athena)."""

    # Glue/Athena type → standard type mapping
    TYPE_MAPPINGS: Dict[str, List[str]] = {
        "STRING": ["STRING", "VARCHAR", "CHAR", "TEXT"],
        "INTEGER": ["INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"],
        "FLOAT": ["FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "REAL"],
        "BOOLEAN": ["BOOLEAN", "BOOL"],
        "TIMESTAMP": ["TIMESTAMP", "DATETIME"],
        "DATE": ["DATE"],
        "BINARY": ["BINARY", "BYTES"],
        "ARRAY": ["ARRAY"],
        "MAP": ["MAP"],
        "STRUCT": ["STRUCT"],
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.region = config.get("region", "us-east-1")
        self.profile = config.get("profile")
        self._glue_client = None

    @property
    def provider_name(self) -> str:
        return "aws"

    @property
    def glue_client(self):
        """Lazy-load Glue client."""
        if self._glue_client is None:
            session_kwargs: Dict[str, Any] = {"region_name": self.region}
            if self.profile:
                session_kwargs["profile_name"] = self.profile
            session = boto3.Session(**session_kwargs)
            self._glue_client = session.client("glue")
        return self._glue_client

    def validate_connection(self) -> bool:
        """Test AWS Glue connectivity."""
        try:
            self.glue_client.get_databases(MaxResults=1)
            return True
        except Exception:
            return False

    def get_resource_schema(self, resource_spec: Dict[str, Any]) -> Optional[ResourceSchema]:
        """Retrieve table schema from the Glue Data Catalog."""
        try:
            fqn = self._extract_fqn(resource_spec)
            if not fqn:
                return None

            database, table = fqn

            response = self.glue_client.get_table(
                DatabaseName=database,
                Name=table,
            )

            tbl = response.get("Table", {})
            sd = tbl.get("StorageDescriptor", {})
            columns = sd.get("Columns", [])
            partition_keys = tbl.get("PartitionKeys", [])

            fields: List[FieldSchema] = []
            for col in columns + partition_keys:
                fields.append(
                    FieldSchema(
                        name=col.get("Name", ""),
                        type=col.get("Type", "string").upper(),
                        mode="NULLABLE",
                        description=col.get("Comment"),
                    )
                )

            # Table type
            table_type_raw = tbl.get("TableType", "EXTERNAL_TABLE")
            if "VIEW" in table_type_raw.upper():
                resource_type = ResourceType.VIEW
            else:
                resource_type = ResourceType.TABLE

            fqn_str = f"{database}.{table}"
            params = tbl.get("Parameters", {})

            return ResourceSchema(
                resource_type=resource_type,
                fully_qualified_name=fqn_str,
                fields=fields,
                row_count=int(params["recordCount"]) if "recordCount" in params else None,
                size_bytes=int(params["sizeKey"]) if "sizeKey" in params else None,
                last_modified=str(tbl.get("UpdateTime")) if tbl.get("UpdateTime") else None,
                metadata={
                    "table_type": table_type_raw,
                    "location": sd.get("Location"),
                    "input_format": sd.get("InputFormat"),
                    "output_format": sd.get("OutputFormat"),
                    "serde": sd.get("SerdeInfo", {}).get("SerializationLibrary"),
                    "description": tbl.get("Description"),
                },
            )

        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "EntityNotFoundException":
                return None
            raise Exception(f"Error retrieving AWS Glue table: {str(e)}") from e
        except Exception as e:
            raise Exception(f"Error retrieving AWS Glue table: {str(e)}") from e

    def validate_resource(
        self,
        contract_spec: Dict[str, Any],
        actual_schema: Optional[ResourceSchema],
    ) -> ValidationResult:
        """Validate an AWS resource against the contract."""
        issues: List[ValidationIssue] = []
        resource_name = contract_spec.get("id") or contract_spec.get("exposeId") or "unknown"

        if actual_schema is None:
            fqn = self._extract_fqn(contract_spec)
            fqn_str = "{}.{}".format(*fqn) if fqn else "unknown"
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="missing_resource",
                    message=f"Table '{fqn_str}' does not exist in AWS Glue catalog",
                    path="exposes[].binding.location",
                    expected=fqn_str,
                    actual=None,
                    suggestion="Create the table in the Glue catalog or verify database/table names",
                )
            )
            return ValidationResult(
                resource_name=resource_name,
                success=False,
                issues=issues,
                schema=None,
            )

        # Compare schemas
        expected_fields = self._extract_expected_fields(contract_spec)
        if expected_fields:
            schema_issues = self.compare_schemas(expected_fields, actual_schema.fields)
            issues.extend(schema_issues)

        # Row count
        if actual_schema.row_count is not None and actual_schema.row_count == 0:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="empty_table",
                    message=f"Table '{actual_schema.fully_qualified_name}' reports 0 rows",
                    path="exposes[].binding.location",
                    expected="> 0 rows",
                    actual="0 rows",
                    suggestion="Verify data has been loaded or Glue crawler has run",
                )
            )

        # SLA thresholds
        sla = contract_spec.get("sla", {})
        if "row_count_min" in sla and actual_schema.row_count is not None:
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
                        suggestion="Check data pipeline or run Glue crawler",
                    )
                )

        return ValidationResult(
            resource_name=resource_name,
            success=len([i for i in issues if i.severity == "error"]) == 0,
            issues=issues,
            schema=actual_schema,
        )

    def normalize_type(self, provider_type: str) -> str:
        upper = provider_type.upper()
        for standard, variants in self.TYPE_MAPPINGS.items():
            if upper == standard or upper in variants:
                return standard
        return upper

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_fqn(
        self,
        resource_spec: Dict[str, Any],
    ) -> Optional[tuple]:
        """Return (database, table) from a resource spec."""
        binding = resource_spec.get("binding", {})
        location = binding.get("location", {})

        database = location.get("database")
        table = location.get("table")

        # Fallback: location.properties
        if not table:
            props = location.get("properties", {})
            database = props.get("database") or database
            table = props.get("table")

        if not all([database, table]):
            return None
        return (database, table)

    @staticmethod
    def _extract_expected_fields(resource_spec: Dict[str, Any]) -> List[FieldSchema]:
        """Extract expected FieldSchema list from contract spec."""
        fields: List[FieldSchema] = []
        schema_fields = resource_spec.get("contract", {}).get("schema", [])
        if not schema_fields:
            schema_fields = resource_spec.get("schema", [])

        for f in schema_fields:
            if isinstance(f, dict):
                mode = f.get("mode", "NULLABLE")
                if "required" in f:
                    mode = "REQUIRED" if f["required"] else "NULLABLE"
                elif "nullable" in f:
                    mode = "NULLABLE" if f["nullable"] else "REQUIRED"
                fields.append(
                    FieldSchema(
                        name=f.get("name", ""),
                        type=f.get("type", "STRING").upper(),
                        mode=mode,
                        description=f.get("description"),
                    )
                )
        return fields

    def run_quality_checks(
        self,
        resource_spec: Dict[str, Any],
        rules: List[Dict[str, Any]],
    ) -> List[ValidationIssue]:
        """Execute DQ rules against Athena via SQL."""
        fqn = self._extract_fqn(resource_spec)
        if not fqn:
            return [
                ValidationIssue(
                    severity="warning",
                    category="quality",
                    message="Cannot run quality checks: unable to resolve table reference",
                    path="contract.dq.rules",
                )
            ]
        # Athena requires unquoted identifiers (Presto SQL)
        "{}.{}".format(*fqn)
        try:
            import boto3 as _boto3

            session_kwargs = {"region_name": self.region}
            if self.profile:
                session_kwargs["profile_name"] = self.profile
            session = _boto3.Session(**session_kwargs)
            session.client("athena")
            # Use Athena to execute quality checks
            # For now, return info that Athena DQ requires async execution
            return [
                ValidationIssue(
                    severity="info",
                    category="quality",
                    message="Athena quality checks require async query execution (not yet implemented)",
                    path="contract.dq.rules",
                )
            ]
        except Exception as e:
            return [
                ValidationIssue(
                    severity="warning",
                    category="quality",
                    message=f"Failed to run quality checks: {e}",
                    path="contract.dq.rules",
                )
            ]
