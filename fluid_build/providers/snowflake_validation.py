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
Snowflake validation provider.

Implements the ValidationProvider interface for validating FLUID contracts
against actual Snowflake resources via INFORMATION_SCHEMA queries.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

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

try:
    from fluid_build.providers.snowflake.connection import SnowflakeConnection

    SNOWFLAKE_AVAILABLE = True
except ImportError:
    SNOWFLAKE_AVAILABLE = False
    SnowflakeConnection = None  # type: ignore[assignment,misc]

LOG = logging.getLogger("fluid.providers.snowflake_validation")


class SnowflakeValidationProvider(ValidationProvider):
    """Validation provider for Snowflake Data Cloud."""

    # Snowflake type → standard type mapping
    TYPE_MAPPINGS: Dict[str, List[str]] = {
        "STRING": ["VARCHAR", "CHAR", "CHARACTER", "TEXT", "STRING"],
        "NUMBER": [
            "NUMBER",
            "DECIMAL",
            "NUMERIC",
            "INT",
            "INTEGER",
            "BIGINT",
            "SMALLINT",
            "TINYINT",
            "BYTEINT",
            "FLOAT",
            "FLOAT4",
            "FLOAT8",
            "DOUBLE",
            "DOUBLE PRECISION",
            "REAL",
        ],
        "BOOLEAN": ["BOOLEAN", "BOOL"],
        "DATE": ["DATE"],
        "TIMESTAMP_NTZ": ["TIMESTAMP_NTZ", "DATETIME", "TIMESTAMP WITHOUT TIME ZONE"],
        "TIMESTAMP_LTZ": ["TIMESTAMP_LTZ", "TIMESTAMP WITH LOCAL TIME ZONE"],
        "TIMESTAMP_TZ": ["TIMESTAMP_TZ", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP"],
        "TIME": ["TIME"],
        "BINARY": ["BINARY", "VARBINARY", "BYTES"],
        "VARIANT": ["VARIANT", "JSON", "JSONB"],
        "OBJECT": ["OBJECT", "STRUCT"],
        "ARRAY": ["ARRAY"],
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.account = config.get("account", "")
        self.user = config.get("user", "")
        self.password = config.get("password")
        self.private_key_path = config.get("private_key_path")
        self.private_key_passphrase = config.get("private_key_passphrase")
        self.role = config.get("role")
        self.warehouse = config.get("warehouse")
        self.database = config.get("database")
        self.schema = config.get("schema")
        self.oauth_token = config.get("oauth_token")
        self.authenticator = config.get("authenticator")

    @property
    def provider_name(self) -> str:
        return "snowflake"

    def _connect(self) -> SnowflakeConnection:
        """Create a SnowflakeConnection context manager."""
        return SnowflakeConnection(
            account=self.account,
            user=self.user,
            password=self.password,
            private_key_path=self.private_key_path,
            private_key_passphrase=self.private_key_passphrase,
            role=self.role,
            warehouse=self.warehouse,
            database=self.database,
            schema=self.schema,
            oauth_token=self.oauth_token,
            authenticator=self.authenticator,
        )

    def validate_connection(self) -> bool:
        """Test Snowflake connectivity with a simple query."""
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT 1")
                return rows is not None
        except Exception:
            return False

    def get_resource_schema(self, resource_spec: Dict[str, Any]) -> Optional[ResourceSchema]:
        """Retrieve Snowflake table/view schema via INFORMATION_SCHEMA."""
        try:
            fqn = self._extract_fqn(resource_spec)
            if not fqn:
                return None

            db, sch, tbl = fqn

            with self._connect() as conn:
                # Query column metadata
                rows = conn.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COMMENT "
                    f"FROM {db}.INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    [sch, tbl],
                )

                if not rows:
                    return None

                fields: List[FieldSchema] = []
                for row in rows:
                    col_name, data_type, is_nullable, comment = (
                        row[0],
                        row[1],
                        row[2],
                        row[3] if len(row) > 3 else None,
                    )
                    mode = "NULLABLE" if is_nullable == "YES" else "REQUIRED"
                    fields.append(
                        FieldSchema(
                            name=col_name,
                            type=data_type,
                            mode=mode,
                            description=comment,
                        )
                    )

                # Fetch row count
                count_rows = conn.execute(f'SELECT COUNT(*) FROM "{db}"."{sch}"."{tbl}"')
                row_count = count_rows[0][0] if count_rows else None

                # Fetch table metadata
                meta_rows = conn.execute(
                    "SELECT TABLE_TYPE, BYTES, LAST_ALTERED, COMMENT "
                    f"FROM {db}.INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                    [sch, tbl],
                )
                metadata: Dict[str, Any] = {}
                size_bytes: Optional[int] = None
                last_modified: Optional[str] = None
                resource_type = ResourceType.TABLE
                if meta_rows:
                    mr = meta_rows[0]
                    table_type = mr[0] if mr[0] else "BASE TABLE"
                    if "VIEW" in table_type.upper():
                        resource_type = ResourceType.VIEW
                    size_bytes = mr[1]
                    last_modified = str(mr[2]) if mr[2] else None
                    metadata["table_type"] = table_type
                    metadata["comment"] = mr[3] if len(mr) > 3 else None

                fqn_str = f'"{db}"."{sch}"."{tbl}"'
                return ResourceSchema(
                    resource_type=resource_type,
                    fully_qualified_name=fqn_str,
                    fields=fields,
                    row_count=row_count,
                    size_bytes=size_bytes,
                    last_modified=last_modified,
                    metadata=metadata,
                )

        except Exception as e:
            raise Exception(f"Error retrieving Snowflake schema: {str(e)}") from e

    def validate_resource(
        self,
        contract_spec: Dict[str, Any],
        actual_schema: Optional[ResourceSchema],
    ) -> ValidationResult:
        """Validate a Snowflake resource against the contract."""
        issues: List[ValidationIssue] = []
        resource_name = contract_spec.get("id") or contract_spec.get("exposeId") or "unknown"

        if actual_schema is None:
            fqn = self._extract_fqn(contract_spec)
            fqn_str = '"{0}"."{1}"."{2}"'.format(*fqn) if fqn else "unknown"
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="missing_resource",
                    message=f"Table '{fqn_str}' does not exist in Snowflake",
                    path="exposes[].binding.location",
                    expected=fqn_str,
                    actual=None,
                    suggestion="Create the table or verify the database/schema/table names",
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

        # Warn on empty table
        if actual_schema.row_count == 0:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="empty_table",
                    message=f"Table '{actual_schema.fully_qualified_name}' has no rows",
                    path="exposes[].binding.location",
                    expected="> 0 rows",
                    actual="0 rows",
                    suggestion="Verify data has been loaded",
                )
            )

        # Check SLA thresholds
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
        """Normalize Snowflake type to a standard form for comparison."""
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
        """Return (database, schema, table) from a resource spec, or None."""
        binding = resource_spec.get("binding", {})
        location = binding.get("location", {})

        database = location.get("database") or self.database
        schema = location.get("schema") or self.schema
        table = location.get("table")

        # Fallback: location.properties (standard FLUID pattern)
        if not table:
            props = location.get("properties", {})
            database = props.get("database") or database
            schema = props.get("schema") or schema
            table = props.get("table")

        if not all([database, schema, table]):
            return None
        return (database, schema, table)

    @staticmethod
    def _extract_expected_fields(resource_spec: Dict[str, Any]) -> List[FieldSchema]:
        """Extract expected FieldSchema list from contract spec."""
        fields: List[FieldSchema] = []
        # Try contract.schema first, then schema directly
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
                        type=f.get("type", "STRING"),
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
        """Execute DQ rules against Snowflake via SQL."""
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
        table_ref = '"{}"."{}"."{}"'.format(*fqn)
        with self._connect() as conn:
            results = execute_quality_checks(
                rules=rules,
                table_ref=table_ref,
                execute_fn=conn.execute,
                dialect="snowflake",
            )
        return quality_results_to_issues(results)
