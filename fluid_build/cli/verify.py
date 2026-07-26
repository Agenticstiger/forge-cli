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

# fluid_build/cli/verify.py
"""
FLUID Verify Command - Multi-Dimensional Contract Validation

Verifies that deployed infrastructure matches the contract specification.
Performs dimensional analysis across:
  1. Schema Structure (column names, counts)
  2. Data Types (field types)
  3. Constraints (nullable/required modes)
  4. Location (region/location)

Provides severity-based drift assessment with clear remediation guidance.
"""

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.console import error as console_error
from fluid_build.observability.tracing import traced_stage as _traced_stage
from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.snowflake.util.config import resolve_env_templates

from ._common import CLIError, load_contract_with_overlay

LOG = logging.getLogger("fluid.cli.verify")

COMMAND = "verify"


def _hydrate_dotenv_into_environ(project_root: Path, environment: Optional[str]) -> None:
    """Hydrate ``os.environ`` from ``.env`` files and ``FLUID_SECRETS_FILE``.

    Thin shim over ``_common.hydrate_dotenv`` — kept in this module so tests
    and legacy callers that import it from ``fluid_build.cli.verify`` keep
    working. See the shared helper's docstring for load order and error
    semantics.
    """
    from ._common import hydrate_dotenv

    hydrate_dotenv(project_root, environment=environment)


# Bug 6: reference-only contracts (builds[].pattern in
# {hybrid-reference, reference, external-reference}) declare a
# schema but delegate materialization to an externally-owned dbt /
# Airflow project. On the first pipeline run the external DAG has
# not yet materialized the table, so Snowflake ``INFORMATION_SCHEMA``
# returns zero columns and ``verify_snowflake_table`` hard-fails
# with "Table not found". Under ``--strict`` that propagates as
# exit 1 at stage 9 — which is wrong: we can't verify a shape that
# the external pipeline owns the creation of. The set below is the
# same marker ``forge/core/artifact_fanout._REFERENCE_PATTERNS``
# uses so the three detection sites (generate_ci, artifact_fanout,
# verify) stay in sync. Mirror any extension in all three.
_REFERENCE_PATTERNS: set = {"hybrid-reference", "reference", "external-reference"}


def _contract_is_reference_only(contract: Dict[str, Any]) -> bool:
    """Return ``True`` if any ``builds[].pattern`` is a reference variant.

    Takes an already-loaded contract dict (``load_contract_with_overlay``
    output) rather than a path, so we avoid a second parse in the verify
    hot path. The canonical path-based helper lives in
    ``forge/core/artifact_fanout._contract_is_reference_only``; this one
    keeps the same predicate shape for dict-holding callers.
    """
    builds = contract.get("builds")
    if not isinstance(builds, list):
        return False
    for build in builds:
        if isinstance(build, dict) and build.get("pattern") in _REFERENCE_PATTERNS:
            return True
    return False


_SNOWFLAKE_TYPE_FAMILIES = {
    "STRING": {"VARCHAR", "CHAR", "CHARACTER", "TEXT", "STRING"},
    "NUMBER": {
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
    },
    "BOOLEAN": {"BOOLEAN", "BOOL"},
    "DATE": {"DATE"},
    "TIME": {"TIME"},
    "TIMESTAMP": {
        "TIMESTAMP",
        "TIMESTAMP_NTZ",
        "TIMESTAMP_LTZ",
        "TIMESTAMP_TZ",
        "DATETIME",
        "TIMESTAMP WITHOUT TIME ZONE",
        "TIMESTAMP WITH LOCAL TIME ZONE",
        "TIMESTAMP WITH TIME ZONE",
    },
    "BINARY": {"BINARY", "VARBINARY", "BYTES"},
    "VARIANT": {"VARIANT", "JSON", "JSONB", "OBJECT", "ARRAY"},
}


def register(sp: argparse._SubParsersAction) -> None:
    """Register the verify command with the CLI"""
    p = sp.add_parser(
        "verify",
        help="Verify deployed resources match contract schema",
        description="""
Verify that deployed infrastructure matches the FLUID contract specification.

Multi-Dimensional Analysis:
  • Schema Structure: Column names and counts
  • Data Types: Field type validation
  • Constraints: nullable/required enforcement
  • Location: Region/location compliance

Severity Levels:
  🔴 CRITICAL - Data loss or system break potential (manual intervention required)
  🟡 WARNING - Non-breaking but should be addressed (manual recommended)
  🔵 INFO - Informational only (auto-fixable or acceptable)
  🟢 SUCCESS - Perfect match (no action needed)

Examples:
  # Verify all exposed data products with dimensional analysis
  fluid verify contract.fluid.yaml

  # Show detailed field-by-field differences
  fluid verify contract.fluid.yaml --show-diffs

  # Exit with error code if mismatches found (CI/CD)
  fluid verify contract.fluid.yaml --strict

  # Output machine-readable report
  fluid verify contract.fluid.yaml --out verification-report.json

Use Cases:
  - CI/CD pipelines: Ensure deployment succeeded correctly
  - Production monitoring: Detect configuration drift  
  - Contract compliance: Validate schema enforcement
  - Pre-deployment checks: Verify before apply
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument("contract", help="Path to FLUID contract YAML file")

    p.add_argument(
        "--expose",
        "--expose-id",
        dest="expose_id",
        help="Verify specific expose by ID (default: all exposes)",
    )

    p.add_argument(
        "--strict", action="store_true", help="Exit with error code if any mismatches found"
    )

    p.add_argument("--out", help="Output verification report to JSON file")

    p.add_argument(
        "--show-diffs", action="store_true", help="Show detailed field-by-field differences"
    )

    p.add_argument(
        "--reconcile-dbt",
        action="store_true",
        help=(
            "Cross-check the contract schema against the build's dbt project "
            "(models/**/schema.yml) and flag drift. Static, warehouse-free. "
            "Drift exits non-zero unless --warn-only is set."
        ),
    )

    p.add_argument(
        "--reconcile-lineage",
        action="store_true",
        help=(
            "Cross-check the contract's declared lineage (consumes[]/exposes[]) "
            "against local run evidence (.fluid run records + cursors) and the "
            "catalog publish payload. Local-only, no network. Critical drift "
            "(an undeclared read or a publish-payload mismatch) fails under "
            "--strict unless --warn-only is set; soft drift never fails."
        ),
    )

    p.add_argument(
        "--warn-only",
        action="store_true",
        help=(
            "Downgrade --reconcile-dbt / --reconcile-lineage drift to a warning "
            "(exit 0). Reports the drift but does not fail the build."
        ),
    )

    p.add_argument("--env", help="Environment overlay file")

    p.set_defaults(func=run)


def assess_drift_severity(
    missing_fields: List,
    extra_fields: List,
    type_mismatches: List,
    mode_mismatches: List,
    region_match: bool,
) -> Dict[str, Any]:
    """
    Assess the severity of detected drift and recommend action.

    Returns severity level, impact assessment, and remediation guidance.
    """
    # Critical: Data loss or system break potential
    if missing_fields or type_mismatches or not region_match:
        actions = []
        if missing_fields:
            actions.append("Review missing fields - queries may fail")
        if type_mismatches:
            actions.append("Type mismatches require table recreation")
        if not region_match:
            actions.append("Region mismatches require resource migration")

        return {
            "level": "CRITICAL",
            "impact": "HIGH",
            "symbol": "🔴",
            "remediation": "MANUAL_INTERVENTION_REQUIRED",
            "reason": "Missing fields, type mismatches, or region mismatch detected",
            "actions": actions,
        }

    # Warning: Non-breaking but should be addressed
    if mode_mismatches:
        return {
            "level": "WARNING",
            "impact": "MEDIUM",
            "symbol": "🟡",
            "remediation": "MANUAL_RECOMMENDED",
            "reason": "Constraint mismatches detected (nullable vs required)",
            "actions": [
                "Mode changes are breaking - requires table recreation or ALTER TABLE",
                "Consider updating contract to match reality if acceptable",
                "Validate no NULL values exist before enforcing REQUIRED",
            ],
        }

    # Info: Informational only
    if extra_fields:
        return {
            "level": "INFO",
            "impact": "LOW",
            "symbol": "🔵",
            "remediation": "AUTO_FIXABLE",
            "reason": "Extra fields found in table (not in contract)",
            "actions": [
                "Extra fields are informational only",
                "Update contract to include if intentional",
                "No immediate action required",
            ],
        }

    # Success: Perfect match
    return {
        "level": "SUCCESS",
        "impact": "NONE",
        "symbol": "🟢",
        "remediation": "NONE",
        "reason": "All checks passed",
        "actions": [],
    }


def verify_bigquery_table(
    project: str,
    dataset: str,
    table: str,
    expected_schema: List[Dict[str, Any]],
    expected_region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Verify BigQuery table with multi-dimensional analysis.

    Dimensions:
      1. Schema Structure (column names, counts)
      2. Data Types (field types)
      3. Constraints (nullable/required modes)
      4. Location (region/location)
    """
    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=project)
        table_id = f"{project}.{dataset}.{table}"

        # Check if table exists
        try:
            bq_table = client.get_table(table_id)
        except Exception:
            return {"status": "error", "error": f"Table not found: {table_id}", "exists": False}

        # Check dataset region
        bq_dataset = client.get_dataset(f"{project}.{dataset}")
        region_match = True
        region_message = None

        if expected_region:
            if bq_dataset.location.lower() != expected_region.lower():
                region_match = False
                region_message = f"Expected {expected_region}, found {bq_dataset.location}"

        # Convert BigQuery schema to comparable format
        actual_fields = {}
        for field in bq_table.schema:
            actual_fields[field.name] = {
                "type": field.field_type.lower(),
                "mode": field.mode.lower() if field.mode else "nullable",
            }

        # Convert contract schema to comparable format
        expected_fields = {}
        for field in expected_schema:
            field_name = field.get("name")
            field_type = field.get("type", "string").lower()

            # Map FLUID types to BigQuery types
            type_mapping = {
                "string": "string",
                "integer": "integer",
                "int": "integer",
                "float": "float",
                "numeric": "numeric",
                "boolean": "bool",
                "bool": "bool",
                "timestamp": "timestamp",
                "date": "date",
                "time": "time",
                "datetime": "datetime",
            }

            bq_type = type_mapping.get(field_type, field_type)
            required = field.get("required", False)

            expected_fields[field_name] = {
                "type": bq_type,
                "mode": "required" if required else "nullable",
            }

        # Dimension 1: Schema Structure (column names and counts)
        matching_fields = []
        missing_fields = []
        extra_fields = []

        for field_name in expected_fields:
            if field_name in actual_fields:
                matching_fields.append(field_name)
            else:
                missing_fields.append(
                    {"field": field_name, "expected": expected_fields[field_name]}
                )

        for field_name in actual_fields:
            if field_name not in expected_fields:
                extra_fields.append({"field": field_name, "actual": actual_fields[field_name]})

        # Dimension 2: Data Types
        type_mismatches = []
        for field_name in matching_fields:
            expected_props = expected_fields[field_name]
            actual_props = actual_fields[field_name]

            if actual_props["type"] != expected_props["type"]:
                type_mismatches.append(
                    {
                        "field": field_name,
                        "expected": expected_props["type"],
                        "actual": actual_props["type"],
                    }
                )

        # Dimension 3: Constraints (nullable/required)
        mode_mismatches = []
        for field_name in matching_fields:
            expected_props = expected_fields[field_name]
            actual_props = actual_fields[field_name]

            if actual_props["mode"] != expected_props["mode"]:
                mode_mismatches.append(
                    {
                        "field": field_name,
                        "expected": expected_props["mode"],
                        "actual": actual_props["mode"],
                    }
                )

        # Assess severity
        severity = assess_drift_severity(
            missing_fields=missing_fields,
            extra_fields=extra_fields,
            type_mismatches=type_mismatches,
            mode_mismatches=mode_mismatches,
            region_match=region_match,
        )

        # Determine overall status
        has_issues = bool(
            missing_fields or extra_fields or type_mismatches or mode_mismatches or not region_match
        )

        return {
            "status": "mismatch" if has_issues else "match",
            "exists": True,
            "table_id": table_id,
            "severity": severity,
            "dimensions": {
                "structure": {
                    "status": "pass" if not (missing_fields or extra_fields) else "fail",
                    "matching_fields": matching_fields,
                    "missing_fields": missing_fields,
                    "extra_fields": extra_fields,
                    "total_expected": len(expected_fields),
                    "total_actual": len(actual_fields),
                },
                "types": {
                    "status": "pass" if not type_mismatches else "fail",
                    "mismatches": type_mismatches,
                },
                "constraints": {
                    "status": "pass" if not mode_mismatches else "fail",
                    "mismatches": mode_mismatches,
                },
                "location": {
                    "status": "pass" if region_match else "fail",
                    "expected": expected_region,
                    "actual": bq_dataset.location,
                    "message": region_message,
                },
            },
            "metadata": {
                "num_rows": bq_table.num_rows,
                "created": bq_table.created.isoformat() if bq_table.created else None,
                "modified": bq_table.modified.isoformat() if bq_table.modified else None,
            },
        }

    except Exception as e:
        LOG.error(f"Error verifying table {table}: {e}")
        return {"status": "error", "error": str(e), "exists": False}


def _normalize_snowflake_type(value: str) -> str:
    base = (value or "STRING").upper().split("(", 1)[0].strip()
    for family, aliases in _SNOWFLAKE_TYPE_FAMILIES.items():
        if base in aliases:
            return family
    return base


def _normalize_snowflake_field_name(value: str) -> str:
    """Snowflake folds unquoted identifiers to uppercase."""
    return (value or "").upper()


def _quote_qualified_snowflake_name(database: str, schema: str, table: str) -> str:
    """
    Build a fully-qualified, quoted Snowflake object name from validated parts.

    Identifier positions in SQL cannot be bound with parameters, so we must
    validate each component before interpolation. `validate_ident` restricts
    inputs to ``[A-Za-z_][A-Za-z0-9_]*``, which eliminates every character
    that could break out of a double-quoted identifier.
    """
    return f'"{validate_ident(database)}"."{validate_ident(schema)}"."{validate_ident(table)}"'


def _fetch_snowflake_columns(
    conn, database: str, schema: str, table: str
) -> Dict[str, Dict[str, Any]]:
    """Return ``{col_name: {type, mode}}`` for the live table, using bind params for values."""
    # `database` is validated by the caller, so interpolating it into the FROM
    # clause is safe. `schema` and `table` flow through as bind parameters.
    rows = conn.execute(
        f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE "
        f'FROM "{validate_ident(database)}".INFORMATION_SCHEMA.COLUMNS '
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION",
        [schema, table],
    )
    if not rows:
        return {}
    return {
        _normalize_snowflake_field_name(str(row[0])): {
            "name": str(row[0]),
            "type": _normalize_snowflake_type(row[1]),
            "mode": "required" if str(row[2]).upper() == "NO" else "nullable",
        }
        for row in rows
    }


def _expected_snowflake_fields(expected_schema: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    expected_fields: Dict[str, Dict[str, Any]] = {}
    for field in expected_schema:
        field_name = field.get("name")
        if not field_name:
            continue
        expected_fields[_normalize_snowflake_field_name(str(field_name))] = {
            "name": str(field_name),
            "type": _normalize_snowflake_type(field.get("type", "STRING")),
            "mode": (
                "required"
                if field.get("required", False) or field.get("nullable") is False
                else "nullable"
            ),
        }
    return expected_fields


def _compare_snowflake_shapes(
    actual: Dict[str, Dict[str, Any]],
    expected: Dict[str, Dict[str, Any]],
) -> Dict[str, List[Any]]:
    matching, missing, extra, type_mismatches, mode_mismatches = [], [], [], [], []
    for name, props in expected.items():
        if name in actual:
            matching.append(props["name"])
        else:
            missing.append(
                {
                    "field": props["name"],
                    "expected": {
                        "type": props["type"],
                        "mode": props["mode"],
                    },
                }
            )
    for name, props in actual.items():
        if name not in expected:
            extra.append(
                {
                    "field": props["name"],
                    "actual": {
                        "type": props["type"],
                        "mode": props["mode"],
                    },
                }
            )
    for name in matching:
        normalized = _normalize_snowflake_field_name(name)
        exp, act = expected[normalized], actual[normalized]
        if act["type"] != exp["type"]:
            type_mismatches.append(
                {"field": exp["name"], "expected": exp["type"], "actual": act["type"]}
            )
        if act["mode"] != exp["mode"]:
            mode_mismatches.append(
                {"field": exp["name"], "expected": exp["mode"], "actual": act["mode"]}
            )
    return {
        "matching": matching,
        "missing": missing,
        "extra": extra,
        "type_mismatches": type_mismatches,
        "mode_mismatches": mode_mismatches,
    }


def verify_snowflake_table(
    *,
    account: str,
    warehouse: str,
    database: str,
    schema: str,
    table: str,
    expected_schema: List[Dict[str, Any]],
    user: Optional[str] = None,
    role: Optional[str] = None,
    authenticator: Optional[str] = None,
    password: Optional[str] = None,
    private_key_path: Optional[str] = None,
    private_key_passphrase: Optional[str] = None,
    oauth_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify a Snowflake table's shape and constraints.

    All SQL identifiers are validated via ``validate_ident`` before any
    statement is issued, so a malicious database/schema/table value (for
    example from an expanded ``{{ env.VAR }}`` template) is rejected before
    it reaches Snowflake.
    """
    # Validate identifiers up front so an injection attempt fails fast,
    # before we open a connection or spend a warehouse credit.
    try:
        qualified = _quote_qualified_snowflake_name(database, schema, table)
    except ValueError as exc:
        LOG.error("Rejected invalid Snowflake identifier for verify: %s", exc)
        return {"status": "error", "error": str(exc), "exists": False}

    try:
        from fluid_build.providers.snowflake.connection import SnowflakeConnection
        from fluid_build.providers.snowflake.util.config import get_connection_params

        params = get_connection_params(
            account=account,
            warehouse=warehouse,
            database=database,
            schema=schema,
            user=user,
            role=role,
            authenticator=authenticator,
            password=password,
            private_key_path=private_key_path,
            private_key_passphrase=private_key_passphrase,
            oauth_token=oauth_token,
        )

        with SnowflakeConnection(**params) as conn:
            actual_fields = _fetch_snowflake_columns(conn, database, schema, table)
            if not actual_fields:
                return {
                    "status": "error",
                    "error": f"Table not found: {qualified}",
                    "exists": False,
                }

            expected_fields = _expected_snowflake_fields(expected_schema)
            diff = _compare_snowflake_shapes(actual_fields, expected_fields)

            count_rows = conn.execute(f"SELECT COUNT(*) FROM {qualified}")
            num_rows = count_rows[0][0] if count_rows else 0

        missing_fields = diff["missing"]
        extra_fields = diff["extra"]
        type_mismatches = diff["type_mismatches"]
        mode_mismatches = diff["mode_mismatches"]
        severity = assess_drift_severity(
            missing_fields=missing_fields,
            extra_fields=extra_fields,
            type_mismatches=type_mismatches,
            mode_mismatches=mode_mismatches,
            region_match=True,
        )
        has_issues = bool(missing_fields or extra_fields or type_mismatches or mode_mismatches)

        return {
            "status": "mismatch" if has_issues else "match",
            "exists": True,
            "table_id": qualified,
            "severity": severity,
            "dimensions": {
                "structure": {
                    "status": "pass" if not (missing_fields or extra_fields) else "fail",
                    "matching_fields": diff["matching"],
                    "missing_fields": missing_fields,
                    "extra_fields": extra_fields,
                    "total_expected": len(expected_fields),
                    "total_actual": len(actual_fields),
                },
                "types": {
                    "status": "pass" if not type_mismatches else "fail",
                    "mismatches": type_mismatches,
                },
                "constraints": {
                    "status": "pass" if not mode_mismatches else "fail",
                    "mismatches": mode_mismatches,
                },
                "location": {
                    "status": "pass",
                    "expected": f"{database}.{schema}",
                    "actual": f"{database}.{schema}",
                    "message": None,
                },
            },
            "metadata": {
                "num_rows": num_rows,
                "created": None,
                "modified": None,
            },
        }
    except Exception as exc:
        LOG.error("Error verifying Snowflake table %s.%s.%s: %s", database, schema, table, exc)
        return {"status": "error", "error": str(exc), "exists": False}


def _verify_local_file(
    expose_name: str,
    expose_config: Dict[str, Any],
    format_type: str,
) -> Dict[str, Any]:
    """Verify a local CSV or Parquet output file using DuckDB.

    Bug A4-1 fix: the format dispatch ``else`` branch previously returned an
    error for every format that is not BigQuery or Snowflake.  This function
    mirrors the BigQuery/Snowflake approach: confirm the file exists and is
    readable, report row count and column names, and surface basic checks.

    Returns a result dict compatible with the existing ``results`` loop below
    (same shape as ``verify_bigquery_table`` and ``verify_snowflake_table``).
    """
    # Resolve the file path from binding.location.path or location.path.
    binding = expose_config.get("binding") or {}
    loc = binding.get("location") or expose_config.get("location") or {}
    file_path_str = loc.get("path") or expose_config.get("path") or ""

    if not file_path_str:
        return {
            "status": "error",
            "error": (
                f"local/csv/parquet expose '{expose_name}' has no location.path declared; "
                "cannot verify"
            ),
            "exists": False,
        }

    file_path = Path(file_path_str)
    if not file_path.exists():
        return {
            "status": "error",
            "error": f"Output file not found: {file_path}",
            "exists": False,
        }

    if not file_path.is_file():
        return {
            "status": "error",
            "error": f"Path exists but is not a regular file: {file_path}",
            "exists": True,
        }

    # Determine actual format from the file extension when the declared
    # format_type is ambiguous ("local" or "").
    suffix = file_path.suffix.lower()
    if suffix in {".parquet", ".pq"} or format_type in {"parquet", "pq"}:
        actual_fmt = "parquet"
    else:
        actual_fmt = "csv"

    # Introspect with DuckDB.
    try:
        import duckdb  # type: ignore[import]

        con = duckdb.connect(":memory:")
        if actual_fmt == "parquet":
            rel = con.sql(f"SELECT * FROM read_parquet({file_path.as_posix()!r})")
        else:
            rel = con.sql(f"SELECT * FROM read_csv_auto({file_path.as_posix()!r})")

        row_count = rel.aggregate("count(*)").fetchone()[0]
        actual_columns = [col for col in rel.columns]
        con.close()
    except Exception as exc:
        return {
            "status": "error",
            "error": f"DuckDB could not read {file_path}: {exc}",
            "exists": True,
        }

    # Compare against the declared schema (best-effort: we report drift but
    # do not hard-fail the verify run — schema drift detection mirrors what
    # the BigQuery verifier does for column name / count checks).
    contract_block = expose_config.get("contract") or {}
    raw_schema = contract_block.get("schema") or expose_config.get("schema") or []
    if isinstance(raw_schema, dict):
        expected_fields = raw_schema.get("fields", [])
    else:
        expected_fields = raw_schema

    dimensions: Dict[str, Any] = {}

    # --- Schema Structure dimension ---
    expected_names = [f["name"] for f in expected_fields if isinstance(f, dict) and "name" in f]
    if expected_names:
        actual_set = set(n.lower() for n in actual_columns)
        missing = [n for n in expected_names if n.lower() not in actual_set]
        extra = [n for n in actual_columns if n.lower() not in {e.lower() for e in expected_names}]
        col_count_match = len(actual_columns) == len(expected_names)
        schema_status = "match" if not missing and not extra else "mismatch"
        dimensions["schema_structure"] = {
            "status": schema_status,
            "expected_count": len(expected_names),
            "actual_count": len(actual_columns),
            "missing_fields": missing,
            "extra_fields": extra,
            "column_count_match": col_count_match,
        }
    else:
        dimensions["schema_structure"] = {
            "status": "match",
            "note": "no expected schema declared; structure not checked",
            "actual_count": len(actual_columns),
        }

    overall_status = (
        "match" if all(d.get("status") == "match" for d in dimensions.values()) else "mismatch"
    )

    return {
        "status": overall_status,
        "exists": True,
        "format": actual_fmt,
        "path": str(file_path),
        "row_count": row_count,
        "actual_columns": actual_columns,
        "dimensions": dimensions,
    }


@_traced_stage("verify")
def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Main verify command execution"""

    cprint("=" * 80)
    cprint("🔍 FLUID Verify - Multi-Dimensional Contract Validation")
    cprint("=" * 80)

    # Load contract using shared infrastructure (overlays now work!)
    # F1 / F6: validate the operator-supplied contract path (traversal,
    # forbidden system paths, symlink) before it reaches the loader.
    from fluid_build.cli.core import FluidCLIError as _FluidCLIError
    from fluid_build.cli.security import validate_cli_path

    try:
        contract_path = str(validate_cli_path(args.contract, mode="read", file_type="contract"))
    except _FluidCLIError as exc:
        if exc.event == "file_not_found":
            raise CLIError(1, "contract_not_found", {"path": args.contract})
        raise
    args.contract = contract_path

    # F1: validate the ``--out`` report write target when set.
    if getattr(args, "out", None):
        args.out = str(
            validate_cli_path(args.out, mode="write", must_exist=False, file_type="report")
        )

    cprint(f"Contract: {contract_path}")
    try:
        contract = load_contract_with_overlay(contract_path, getattr(args, "env", None), logger)
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "contract_load_failed", {"path": contract_path, "error": str(e)})

    contract_id = contract.get("id", "unknown")
    cprint(f"Contract ID: {contract_id}")

    # Bug 6: detect reference-only mode once, used below to downgrade
    # "table not found" hard-errors to informational notices. The
    # underlying dbt / Airflow project owns table creation; on the
    # first pipeline run the table provably doesn't exist yet, so
    # failing ``verify --strict`` adds no signal — it just blocks
    # stage 10 publish.
    reference_only = _contract_is_reference_only(contract)
    if reference_only:
        cprint(
            "   (contract is reference-only — missing tables will be reported as INFO, "
            "not treated as verification failures)"
        )

    # Hydrate .env files into os.environ so subsequent {{ env.VAR }}
    # resolution (and the Snowflake identifier allowlist) sees values
    # the user already staged in .env. Matches the apply path, which hits
    # the credential resolver chain with project_root defaulted to Path.cwd()
    # (see provider -> resolve_snowflake_settings fallback). The
    # contract file itself lives deeper in the tree (e.g. fluid/contracts/*)
    # so its parent is NOT the project root.
    _hydrate_dotenv_into_environ(Path.cwd(), getattr(args, "env", None))

    # Get exposes to verify
    exposes = contract.get("exposes", [])

    # Convert list to dict for easier processing
    exposes_dict = {}
    if isinstance(exposes, list):
        for expose in exposes:
            expose_id = expose.get("exposeId") or expose.get("id")
            if expose_id:
                exposes_dict[expose_id] = expose
    elif isinstance(exposes, dict):
        exposes_dict = exposes

    if args.expose_id:
        if args.expose_id not in exposes_dict:
            raise CLIError(
                1,
                "expose_not_found",
                {"expose_id": args.expose_id, "available": list(exposes_dict.keys())},
            )
        exposes_to_verify = {args.expose_id: exposes_dict[args.expose_id]}
    else:
        exposes_to_verify = exposes_dict

    cprint(f"Exposes to verify: {len(exposes_to_verify)}")
    cprint("=" * 80)

    from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

    snowflake_settings = resolve_snowflake_settings(
        contract=contract,
        project_root=Path(contract_path).parent,
        environment=getattr(args, "env", None),
    )

    # Verify each expose
    results = {}
    for expose_name, expose_config in exposes_to_verify.items():
        # Get format from either 'format' or 'binding.format'
        format_type = expose_config.get("format", "")
        if not format_type:
            binding = expose_config.get("binding", {})
            format_type = binding.get("format", "")

        if format_type == "bigquery_table":
            # Get properties from either 'properties' or 'binding.location'
            properties = expose_config.get("properties", {})
            if not properties:
                binding = expose_config.get("binding", {})
                location = binding.get("location", {})
                # Build target from binding
                project = location.get("project", "")
                dataset = location.get("dataset", "")
                table = location.get("table", "")
                target = f"{project}.{dataset}.{table}"
                properties = {
                    "target": target,
                    "region": location.get("region") or binding.get("region"),
                    "schema": expose_config.get("schema", {}),
                }
            else:
                target = properties.get("target", "")

            # Parse target: project.dataset.table
            parts = target.split(".")
            if len(parts) != 3:
                results[expose_name] = {
                    "status": "error",
                    "error": f"Invalid target format: {target}",
                }
                continue

            project, dataset, table = parts

            # Get schema - can be in multiple places
            schema = properties.get("schema", expose_config.get("schema", {}))
            if not schema:
                contract_section = expose_config.get("contract", {})
                schema = contract_section.get("schema", {})

            # Fields can be directly in schema (list) or under schema.fields
            if isinstance(schema, list):
                fields = schema
            else:
                fields = schema.get("fields", schema) if isinstance(schema, dict) else []

            region = properties.get("region") or properties.get("location")
            if not region:
                binding = expose_config.get("binding", {})
                location = binding.get("location", {})
                region = location.get("region") or binding.get("region")

            result = verify_bigquery_table(
                project=project,
                dataset=dataset,
                table=table,
                expected_schema=fields,
                expected_region=region,
            )

            results[expose_name] = result
        elif format_type == "snowflake_table":
            binding = expose_config.get("binding", {})
            location = binding.get("location", expose_config.get("location", {}))
            properties = binding.get("properties", {})

            database = resolve_env_templates(location.get("database")) or snowflake_settings.get(
                "database"
            )
            schema = resolve_env_templates(location.get("schema")) or snowflake_settings.get(
                "schema"
            )
            table = (
                resolve_env_templates(location.get("table"))
                or properties.get("table")
                or expose_name
            )

            schema_config = expose_config.get(
                "schema", expose_config.get("contract", {}).get("schema", [])
            )
            fields = (
                schema_config
                if isinstance(schema_config, list)
                else schema_config.get("fields", [])
            )

            if not database or not schema or not table:
                results[expose_name] = {
                    "status": "error",
                    "error": "Snowflake verify requires database, schema, and table to be resolved",
                }
                continue

            result = verify_snowflake_table(
                account=snowflake_settings.get("account"),
                warehouse=snowflake_settings.get("warehouse"),
                database=database,
                schema=schema,
                table=table,
                expected_schema=fields,
                user=snowflake_settings.get("user"),
                role=snowflake_settings.get("role"),
                authenticator=snowflake_settings.get("authenticator"),
                password=snowflake_settings.get("password"),
                private_key_path=snowflake_settings.get("private_key_path"),
                private_key_passphrase=snowflake_settings.get("private_key_passphrase"),
                oauth_token=snowflake_settings.get("oauth_token"),
            )
            results[expose_name] = result
        elif format_type in {"csv", "parquet", "pq", "local", ""}:
            # Bug A4-1: verify local csv/parquet output files using duckdb.
            # Mirrors what BigQuery/Snowflake branches do: report row count,
            # column presence, and basic readability without requiring cloud
            # credentials.
            results[expose_name] = _verify_local_file(
                expose_name=expose_name,
                expose_config=expose_config,
                format_type=format_type,
            )
        else:
            # NOT an ``error``: nothing failed, forge simply ships no verifier
            # for this binding format. Conflating the two mattered once
            # ``error`` became fatal — a contract with, say, a kafka_topic
            # expose would have started failing `fluid verify` for a check that
            # was never attempted.
            results[expose_name] = {
                "status": "unsupported",
                "error": f"Unsupported format: {format_type}",
            }

    # Display results with dimensional analysis
    match_count = 0
    mismatch_count = 0
    error_count = 0
    # Exposes forge has no verifier for. Reported, never counted as a failure —
    # "we did not check" is not "the check failed".
    unsupported_count = 0
    # Track critical-severity mismatches separately so ``--strict``
    # can differentiate breaking drift (missing fields, type mismatches,
    # region mismatches) from non-breaking constraint drift
    # (nullable vs required). Without this split, ``verify --strict``
    # red-flagged dbt-built tables for every demo (dbt creates nullable
    # cols by default; contracts often declare ``required: true``) —
    # which conflated a known modelling tension with real schema breaks.
    critical_mismatch_count = 0

    for expose_name, result in results.items():
        expose_config = exposes_to_verify.get(expose_name, {})

        # Get format from either 'format' or 'binding.format'
        format_type = expose_config.get("format", "unknown")
        if not format_type or format_type == "unknown":
            binding = expose_config.get("binding", {})
            format_type = binding.get("format", "unknown")

        # Get properties
        properties = expose_config.get("properties", {})
        if not properties:
            binding = expose_config.get("binding", {})
            location = binding.get("location", {})
            if format_type == "snowflake_table":
                target = ".".join(
                    part
                    for part in [
                        resolve_env_templates(location.get("database", "")) or "",
                        resolve_env_templates(location.get("schema", "")) or "",
                        resolve_env_templates(location.get("table", "")) or "",
                    ]
                    if part
                )
            else:
                project = location.get("project", "")
                dataset = location.get("dataset", "")
                table = location.get("table", "")
                target = f"{project}.{dataset}.{table}"
        else:
            target = properties.get("target", "N/A")

        cprint(f"\n📋 Verifying: {expose_name}")
        cprint(f"   Format: {format_type}")
        cprint(f"   Target: {target}")

        if result["status"] == "unsupported":
            cprint(f"   ⏭️  Skipped: {result.get('error', 'no verifier for this format')}")
            unsupported_count += 1
            continue

        if result["status"] == "error":
            # Bug 6: downgrade "table missing" to INFO for
            # reference-only contracts. The external dbt project hasn't
            # run yet, so the absence of the target table is expected
            # state — not a verification failure. Other errors
            # (auth, bad config, SQL syntax) still fail hard because
            # they indicate real problems regardless of materialization
            # source.
            if reference_only and result.get("exists") is False:
                cprint(
                    f"   🔵 INFO: {result.get('error', 'Table not materialized yet')} "
                    "(reference-only — external pipeline owns creation)"
                )
                # Don't count as error or mismatch: the shape cannot
                # be compared, so we report nothing either way. This
                # mirrors how ``fluid diff`` treats a missing baseline.
                continue
            cprint(f"   ❌ Error: {result.get('error', 'Unknown error')}")
            error_count += 1
            continue

        # Get dimensional results
        status = result["status"]
        severity = result.get("severity", {})
        dimensions = result.get("dimensions", {})
        metadata = result.get("metadata", {})

        structure = dimensions.get("structure", {})
        types = dimensions.get("types", {})
        constraints = dimensions.get("constraints", {})
        location = dimensions.get("location", {})

        # Display severity assessment
        severity_symbol = severity.get("symbol", "⚪")
        severity_level = severity.get("level", "UNKNOWN")
        severity_impact = severity.get("impact", "UNKNOWN")

        cprint(f"\n   {severity_symbol} Severity: {severity_level} (Impact: {severity_impact})")
        cprint(f"   📊 Table Rows: {metadata.get('num_rows', 0):,}")

        # Dimension 1: Schema Structure
        cprint("\n   🔍 Dimension 1: Schema Structure")
        if structure.get("status") == "pass":
            matching = structure.get("matching_fields", [])
            cprint(f"      ✅ PASS - All {len(matching)} column names match specification")
            if args.show_diffs:
                cprint(f"         Columns: {', '.join(matching)}")
        else:
            missing = [f["field"] for f in structure.get("missing_fields", [])]
            extra = [f["field"] for f in structure.get("extra_fields", [])]
            matching = structure.get("matching_fields", [])
            cprint("      ❌ FAIL - Schema structure mismatch")
            cprint(f"         ✅ Matching: {len(matching)}/{structure.get('total_expected', 0)}")
            if missing:
                cprint(f"         ❌ Missing in table: {', '.join(missing)}")
            if extra:
                cprint(f"         ⚠️  Extra in table: {', '.join(extra)}")

        # Dimension 2: Data Types
        cprint("\n   🔍 Dimension 2: Data Types")
        type_mismatches = types.get("mismatches", [])
        if types.get("status") == "pass":
            cprint("      ✅ PASS - All field types match specification")
        else:
            cprint(f"      ❌ FAIL - Type mismatches detected ({len(type_mismatches)})")
            if args.show_diffs:
                for mismatch in type_mismatches:
                    cprint(
                        f"         ≠ {mismatch['field']}: expected {mismatch['expected']}, found {mismatch['actual']}"
                    )

        # Dimension 3: Constraints
        cprint("\n   🔍 Dimension 3: Constraints (nullable/required)")
        mode_mismatches = constraints.get("mismatches", [])
        if constraints.get("status") == "pass":
            cprint("      ✅ PASS - All field constraints match specification")
        else:
            cprint(f"      ⚠️  FAIL - Constraint mismatches detected ({len(mode_mismatches)})")
            if args.show_diffs:
                for mismatch in mode_mismatches:
                    cprint(
                        f"         ≠ {mismatch['field']}: expected {mismatch['expected']}, found {mismatch['actual']}"
                    )

        # Dimension 4: Location
        cprint("\n   🔍 Dimension 4: Location")
        if location.get("status") == "pass":
            cprint(f"      ✅ PASS - Location: {location.get('actual', 'N/A')}")
        else:
            cprint(f"      ❌ FAIL - {location.get('message', 'Location mismatch')}")

        # Remediation guidance
        cprint(f"\n   💡 Remediation: {severity.get('remediation', 'UNKNOWN')}")
        cprint(f"      {severity.get('reason', '')}")
        if args.show_diffs and severity.get("actions"):
            for action in severity["actions"]:
                cprint(f"      • {action}")

        # Update counts. ``critical_mismatch_count`` only ticks when
        # the per-expose severity is CRITICAL (missing fields, type
        # mismatches, region drift). WARNING-only constraint drift
        # still counts toward ``mismatch_count`` so the summary remains
        # accurate, but ``--strict`` consults the critical counter
        # below.
        if status == "match":
            match_count += 1
        else:
            mismatch_count += 1
            if severity.get("level") == "CRITICAL":
                critical_mismatch_count += 1

    # ── Acquisition pattern: post-apply probes ─────────────────────────
    # When the contract has any ``pattern: acquisition`` builds, run the
    # acquisition stage extension against the current workdir. The
    # extension checks: a run record exists, records landed, run state
    # is SUCCEEDED or PARTIAL, no DLQ overflow, and cost is within
    # budget. Failures count toward the verify exit code under
    # ``--strict``.
    try:
        from fluid_build.cli._acquisition_stage_ext import (
            is_acquisition_contract,
            verify_acquisition,
        )

        if is_acquisition_contract(contract):
            cprint("\n" + "=" * 80)
            cprint("🔄 Acquisition Post-Apply Probes")
            cprint("=" * 80)
            # Bug A4-3: use the contract file's parent directory, not cwd(),
            # so run-record lookup works when ``fluid verify`` is called with
            # an absolute contract path from a different working directory.
            acq_results = verify_acquisition(contract, Path(contract_path).resolve().parent)
            for r in acq_results:
                cprint(f"\n   Build: {r.product_id}/{r.build_id}")
                for c in r.checks:
                    icon = "✅" if c.passed else "❌"
                    cprint(f"      {icon} {c.name}: {c.detail}")
                    if not c.passed:
                        mismatch_count += 1
    except Exception as exc:  # noqa: BLE001 — verify must not crash the CLI
        warning(f"Acquisition probes skipped: {exc}")

    # ── Transformation pattern: dbt run_results probes ─────────────────────
    # When the contract has any dbt build, read the run record the dbt runner
    # writes from ``target/run_results.json`` and gate on failing contract
    # tests. A failed ``dbt_tests_passed`` / ``no_error_severity_failures``
    # check is CRITICAL (bumps ``critical_mismatch_count`` so ``--strict``
    # fails the exit code); "no run record yet" only counts toward the
    # summary ``mismatch_count`` (verify may run before amend-and-build).
    try:
        from fluid_build.cli._transformation_stage_ext import (
            CRITICAL_TRANSFORMATION_CHECK_NAMES,
            is_transformation_contract,
            verify_transformation,
        )

        if is_transformation_contract(contract):
            cprint("\n" + "=" * 80)
            cprint("🔧 Transformation Post-Apply Probes (dbt)")
            cprint("=" * 80)
            txf_results = verify_transformation(contract, Path(contract_path).resolve().parent)
            for r in txf_results:
                cprint(f"\n   Build: {r.product_id}/{r.build_id}")
                for c in r.checks:
                    icon = "✅" if c.passed else "❌"
                    cprint(f"      {icon} {c.name}: {c.detail}")
                    if not c.passed:
                        mismatch_count += 1
                        if c.name in CRITICAL_TRANSFORMATION_CHECK_NAMES:
                            critical_mismatch_count += 1
    except Exception as exc:  # noqa: BLE001 — verify must not crash the CLI
        warning(f"Transformation probes skipped: {exc}")

    # ── Contract ↔ dbt reconciliation (opt-in via --reconcile-dbt) ─────────
    # Static, warehouse-free cross-check that the contract schema agrees with
    # the columns the build's dbt project declares. Surfaces DRIFT (contract
    # column absent from dbt, dbt column absent from the contract, type
    # disagreement, missing model). ``getattr`` defaults keep every existing
    # caller (and test Namespace lacking the new attrs) behaving exactly as
    # before — the check only runs when the operator opts in.
    reconcile_report = None
    if getattr(args, "reconcile_dbt", False):
        try:
            from fluid_build.cli._verify_reconcile import (
                reconcile_contract_dbt,
                render_report,
            )

            reconcile_report = reconcile_contract_dbt(contract, contract_path)
            render_report(reconcile_report, show_diffs=getattr(args, "show_diffs", False))
        except Exception as exc:  # noqa: BLE001 — reconcile must not crash verify
            warning(f"Contract↔dbt reconciliation skipped: {exc}")
            reconcile_report = None

    # ── Contract ↔ published-lineage reconciliation (--reconcile-lineage) ──
    # Sibling to --reconcile-dbt: compares the contract's declared lineage
    # (consumes[]/exposes[]) against the run evidence the build runners
    # persisted locally (.fluid run records + cursor state) and the lineage
    # payload the catalog registrar WOULD publish (rebuilt locally — no
    # network). Same posture: opt-in, getattr defaults, never crashes verify.
    lineage_report = None
    if getattr(args, "reconcile_lineage", False):
        try:
            from fluid_build.cli._verify_reconcile_lineage import (
                reconcile_contract_lineage,
                render_lineage_report,
            )

            lineage_report = reconcile_contract_lineage(contract, contract_path)
            render_lineage_report(lineage_report, show_diffs=getattr(args, "show_diffs", False))
        except Exception as exc:  # noqa: BLE001 — reconcile must not crash verify
            warning(f"Contract↔published-lineage reconciliation skipped: {exc}")
            lineage_report = None

    # Summary
    cprint("\n" + "=" * 80)
    cprint("📊 Verification Summary")
    cprint("=" * 80)
    cprint(f"Total verified: {len(results)}")
    success(f"Match: {match_count}")
    warning(f"Mismatch: {mismatch_count}")
    console_error(f"Error: {error_count}")
    if unsupported_count:
        cprint(f"⏭️  Skipped (no verifier for the format): {unsupported_count}")

    if mismatch_count > 0 or error_count > 0:
        cprint("\n💡 Next Steps:")
        cprint("   • For additive changes (new fields): Run 'fluid apply contract.fluid.yaml'")
        cprint("   • For breaking changes (type/mode): Recreate table or use 'ALTER TABLE'")
        cprint("   • For region mismatches: Recreate resources in correct region")

    cprint("=" * 80)

    # Output JSON report if requested
    if args.out:
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "contract": contract_path,
            "contract_id": contract_id,
            "summary": {
                "total": len(results),
                "match": match_count,
                "mismatch": mismatch_count,
                "error": error_count,
                "unsupported": unsupported_count,
            },
            "results": results,
        }
        if reconcile_report is not None:
            report["reconcile"] = reconcile_report.to_dict()
        if lineage_report is not None:
            report["reconcile_lineage"] = lineage_report.to_dict()

        output_path = Path(args.out)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        cprint(f"\n📄 Report saved: {output_path}")

    # Contract↔dbt drift gate (opt-in). When --reconcile-dbt found drift and
    # the operator did NOT pass --warn-only, fail the run so CI catches a
    # contract that no longer matches its dbt project. This is independent of
    # --strict: the operator explicitly asked for the reconcile check, so its
    # drift gates on its own. --warn-only downgrades to a non-fatal warning.
    if reconcile_report is not None and reconcile_report.has_drift:
        if getattr(args, "warn_only", False):
            warning(
                "--warn-only: contract↔dbt drift detected "
                f"({len(reconcile_report.column_drifts)} column, "
                f"{len(reconcile_report.model_drifts)} model) — not failing the build."
            )
        else:
            console_error(
                "Contract↔dbt drift detected "
                f"({len(reconcile_report.column_drifts)} column, "
                f"{len(reconcile_report.model_drifts)} model). "
                "Fix the contract or dbt schema, or pass --warn-only."
            )
            return 1

    # Contract↔published-lineage drift gate (opt-in). Only CRITICAL lineage
    # drift (read-but-undeclared / publish-payload-mismatch) can fail the
    # run, and only under --strict — soft drift (declared-but-never-read)
    # is informational: the consuming build may simply not have run yet.
    # --warn-only downgrades criticals to a warning, mirroring the dbt gate.
    if lineage_report is not None and lineage_report.has_critical_drift:
        n_critical = len(lineage_report.critical_drifts)
        if getattr(args, "warn_only", False):
            warning(
                f"--warn-only: {n_critical} critical lineage drift(s) detected — "
                "not failing the build."
            )
        elif args.strict:
            console_error(
                f"Contract↔published-lineage critical drift ({n_critical}). "
                "Declare the missing consumes[] entries or fix the publish "
                "payload, or pass --warn-only."
            )
            return 1
        else:
            warning(
                f"{n_critical} critical lineage drift(s) detected — "
                "add --strict to gate CI on this."
            )

    # ``error_count`` ALWAYS fails, --strict or not. An error is not "we
    # checked and found drift" — it is "we could not check at all" (auth
    # failure, unreachable container, a table the contract claims exists).
    # This used to be gated on --strict, contradicting the comment right
    # here, so a run printing "❌ Error: 1" exited 0 and CI keying on the
    # exit code treated an unreachable pool as a successful verification.
    if error_count > 0:
        console_error(
            f"{error_count} target(s) could not be verified. This is not drift — "
            "the check itself failed (unreachable container, missing object, or "
            "insufficient privileges). If the target lives in a shared pool, the "
            "platform team may still owe this product USAGE on it."
        )
        return 1

    # Exit with error code if strict mode and CRITICAL issues found.
    # Non-critical mismatches (e.g. nullable-vs-required constraint
    # drift) emit warnings to stderr but do NOT fail the build —
    # operators can tighten the contract incrementally.
    if args.strict and critical_mismatch_count > 0:
        return 1
    if args.strict and mismatch_count > 0:
        warning(
            f"--strict: {mismatch_count} non-critical mismatch(es) downgraded "
            "to warning (constraint-only drift). Tighten the contract or fix "
            "the warehouse to clear them."
        )

    return 0
