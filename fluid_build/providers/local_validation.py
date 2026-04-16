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
Local validation provider.

Implements the ValidationProvider interface for validating FLUID contracts
against local resources using DuckDB for file introspection (CSV, Parquet,
JSON, and DuckDB tables).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
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

LOG = logging.getLogger("fluid.providers.local_validation")

# Regex for safe SQL identifiers
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str) -> str:
    """Validate a SQL identifier to prevent injection."""
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


class LocalValidationProvider(ValidationProvider):
    """Validation provider for local files using DuckDB."""

    TYPE_MAPPINGS: Dict[str, List[str]] = {
        "VARCHAR": ["VARCHAR", "STRING", "TEXT", "CHAR", "JSON", "JSONB"],
        "BIGINT": [
            "BIGINT",
            "INT",
            "INTEGER",
            "SMALLINT",
            "TINYINT",
            "INT64",
            "HUGEINT",
            "UBIGINT",
            "UINTEGER",
        ],
        "DOUBLE": ["DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC", "FLOAT64"],
        "BOOLEAN": ["BOOLEAN", "BOOL"],
        "DATE": ["DATE"],
        "TIMESTAMP": [
            "TIMESTAMP",
            "DATETIME",
            "TIMESTAMP_NTZ",
            "TIMESTAMPTZ",
            "TIMESTAMP WITH TIME ZONE",
        ],
        "TIME": ["TIME"],
        "BLOB": ["BLOB", "BINARY", "BYTES", "VARBINARY"],
        "JSON": ["JSON", "JSONB", "VARIANT"],
        "LIST": ["LIST", "ARRAY"],
        "STRUCT": ["STRUCT", "OBJECT", "RECORD"],
    }

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.base_dir = config.get("base_dir", ".")
        self._duckdb = None

    def _get_duckdb(self):
        """Lazy-load duckdb module."""
        if self._duckdb is None:
            try:
                import duckdb

                self._duckdb = duckdb
            except ImportError:
                raise RuntimeError(
                    "duckdb is required for local validation. " "Install with: pip install duckdb"
                )
        return self._duckdb

    @property
    def provider_name(self) -> str:
        return "local"

    def validate_connection(self) -> bool:
        """Local provider is always reachable."""
        try:
            duckdb = self._get_duckdb()
            conn = duckdb.connect(":memory:")
            conn.execute("SELECT 1")
            conn.close()
            return True
        except Exception:
            return False

    def get_resource_schema(self, resource_spec: Dict[str, Any]) -> Optional[ResourceSchema]:
        """Introspect a local file using DuckDB's auto-detection."""
        try:
            file_path = self._resolve_path(resource_spec)
            if not file_path:
                return None

            path = Path(file_path)
            if not path.exists():
                return None

            duckdb = self._get_duckdb()
            conn = duckdb.connect(":memory:")

            try:
                ext = path.suffix.lower()
                abs_path = str(path.resolve())

                # DuckDB native database file — connect and query the table directly
                if ext in (".duckdb", ".db"):
                    binding = resource_spec.get("binding", {})
                    loc = binding.get("location", {})
                    schema_name = loc.get("schema", "main")
                    table_name = loc.get("table", "")
                    if not table_name:
                        LOG.warning("DuckDB binding: no table specified in location")
                        return None
                    conn.close()
                    conn = duckdb.connect(abs_path, read_only=True)
                    table_ref = f'"{schema_name}"."{table_name}"'
                    describe_sql = f"DESCRIBE SELECT * FROM {table_ref}"
                    rows = conn.execute(describe_sql).fetchall()
                    fields: List[FieldSchema] = []
                    for row in rows:
                        fields.append(FieldSchema(name=row[0], type=row[1], mode="NULLABLE"))
                    count_row = conn.execute(f"SELECT COUNT(*) FROM {table_ref}").fetchone()
                    row_count = count_row[0] if count_row else None
                    size_bytes = path.stat().st_size
                    conn.close()
                    return ResourceSchema(
                        resource_type=ResourceType.TABLE,
                        fields=fields,
                        row_count=row_count,
                        size_bytes=size_bytes,
                    )

                if ext in (".parquet", ".pq"):
                    read_fn = "read_parquet"
                elif ext in (".csv", ".tsv"):
                    read_fn = "read_csv_auto"
                elif ext in (".json", ".jsonl", ".ndjson"):
                    read_fn = "read_json_auto"
                else:
                    LOG.warning("Unsupported file extension: %s", ext)
                    return None

                # Use parameterised path – DuckDB read functions accept a string literal
                describe_sql = "DESCRIBE SELECT * FROM {fn}('{p}')".format(
                    fn=read_fn, p=abs_path.replace("'", "''")
                )
                rows = conn.execute(describe_sql).fetchall()

                fields: List[FieldSchema] = []
                for row in rows:
                    col_name = row[0]
                    col_type = row[1]
                    fields.append(
                        FieldSchema(
                            name=col_name,
                            type=col_type,
                            mode="NULLABLE",
                        )
                    )

                # Row count
                count_sql = "SELECT COUNT(*) FROM {fn}('{p}')".format(
                    fn=read_fn, p=abs_path.replace("'", "''")
                )
                count_row = conn.execute(count_sql).fetchone()
                row_count = count_row[0] if count_row else None

                # File size
                size_bytes = path.stat().st_size

                return ResourceSchema(
                    resource_type=ResourceType.TABLE,
                    fully_qualified_name=str(path),
                    fields=fields,
                    row_count=row_count,
                    size_bytes=size_bytes,
                    last_modified=str(path.stat().st_mtime),
                    metadata={
                        "format": ext.lstrip("."),
                        "read_function": read_fn,
                    },
                )
            finally:
                conn.close()

        except Exception as e:
            raise Exception(f"Error reading local file: {str(e)}") from e

    def validate_resource(
        self,
        contract_spec: Dict[str, Any],
        actual_schema: Optional[ResourceSchema],
    ) -> ValidationResult:
        """Validate a local file against the contract."""
        issues: List[ValidationIssue] = []
        resource_name = contract_spec.get("id") or contract_spec.get("exposeId") or "unknown"

        if actual_schema is None:
            file_path = self._resolve_path(contract_spec) or "unknown"
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="missing_resource",
                    message=f"File '{file_path}' does not exist",
                    path="exposes[].binding.location",
                    expected=file_path,
                    actual=None,
                    suggestion="Verify the file path and ensure data has been generated",
                )
            )
            return ValidationResult(
                resource_name=resource_name,
                success=False,
                issues=issues,
                schema=None,
            )

        expected_fields = self._extract_expected_fields(contract_spec)
        if expected_fields:
            schema_issues = self.compare_schemas(expected_fields, actual_schema.fields)
            issues.extend(schema_issues)

        if actual_schema.row_count == 0:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="empty_table",
                    message=f"File '{actual_schema.fully_qualified_name}' has no rows",
                    path="exposes[].binding.location",
                    expected="> 0 rows",
                    actual="0 rows",
                    suggestion="Verify data has been generated",
                )
            )

        sla = contract_spec.get("sla", {})
        if "row_count_min" in sla and actual_schema.row_count is not None:
            min_rows = sla["row_count_min"]
            if actual_schema.row_count < min_rows:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="row_count_below_threshold",
                        message=f"File has {actual_schema.row_count} rows, below minimum of {min_rows}",
                        path="exposes[].sla.row_count_min",
                        expected=f">= {min_rows} rows",
                        actual=f"{actual_schema.row_count} rows",
                        suggestion="Check data generation pipeline",
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

    def _resolve_path(self, resource_spec: Dict[str, Any]) -> Optional[str]:
        """Resolve the local file path from a resource spec."""
        binding = resource_spec.get("binding", {})
        location = binding.get("location", {})

        # location.path is standard for local bindings
        path = location.get("path")
        if not path:
            props = location.get("properties", {})
            path = props.get("path")

        if not path:
            return None

        # Resolve relative to base_dir
        resolved = Path(self.base_dir) / path
        return str(resolved)

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
        """Execute DQ rules against a local file via DuckDB."""
        file_path = self._resolve_path(resource_spec)
        if not file_path:
            return [
                ValidationIssue(
                    severity="warning",
                    category="quality",
                    message="Cannot run quality checks: unable to resolve file path",
                    path="contract.dq.rules",
                )
            ]
        path = Path(file_path)
        if not path.exists():
            return [
                ValidationIssue(
                    severity="warning",
                    category="quality",
                    message=f"Cannot run quality checks: file '{file_path}' not found",
                    path="contract.dq.rules",
                )
            ]
        ext = path.suffix.lower()
        abs_path = str(path.resolve())
        duckdb = self._get_duckdb()

        # DuckDB native database: connect directly and query the bound table
        if ext in (".duckdb", ".db"):
            binding = resource_spec.get("binding", {})
            loc = binding.get("location", {})
            schema_name = loc.get("schema", "main")
            table_name = loc.get("table", "")
            if not table_name:
                return [
                    ValidationIssue(
                        severity="warning",
                        category="quality",
                        message="DuckDB binding missing location.table — cannot run quality checks",
                        path="contract.dq.rules",
                    )
                ]
            conn = duckdb.connect(abs_path, read_only=True)
            table_ref = f'"{schema_name}"."{table_name}"'
            try:

                def _exec(sql):
                    return conn.execute(sql).fetchall()

                results = execute_quality_checks(
                    rules=rules,
                    table_ref=table_ref,
                    execute_fn=_exec,
                    dialect="ansi",
                )
                return quality_results_to_issues(results)
            finally:
                conn.close()

        if ext in (".parquet", ".pq"):
            read_fn = "read_parquet"
        elif ext in (".csv", ".tsv"):
            read_fn = "read_csv_auto"
        elif ext in (".json", ".jsonl", ".ndjson"):
            read_fn = "read_json_auto"
        else:
            return [
                ValidationIssue(
                    severity="warning",
                    category="quality",
                    message=f"Unsupported file format for quality checks: {ext}",
                    path="contract.dq.rules",
                )
            ]
        table_ref = "{fn}('{p}')".format(fn=read_fn, p=abs_path.replace("'", "''"))
        conn = duckdb.connect(":memory:")
        try:

            def _exec(sql):
                return conn.execute(sql).fetchall()

            results = execute_quality_checks(
                rules=rules,
                table_ref=table_ref,
                execute_fn=_exec,
                dialect="ansi",
            )
            return quality_results_to_issues(results)
        finally:
            conn.close()
