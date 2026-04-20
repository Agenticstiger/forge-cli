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
from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.snowflake.util.config import resolve_env_templates

from ._common import CLIError, load_contract_with_overlay

LOG = logging.getLogger("fluid.cli.verify")

COMMAND = "verify"

# Backwards-compatible alias for any legacy callers in this module.
_resolve_env_templates = resolve_env_templates


def _hydrate_dotenv_into_environ(project_root: Path, environment: Optional[str]) -> None:
    """Hydrate ``os.environ`` from ``.env`` files and ``FLUID_SECRETS_FILE``.

    Thin shim over ``_common.hydrate_dotenv`` — kept in this module so tests
    and legacy callers that import it from ``fluid_build.cli.verify`` keep
    working. See the shared helper's docstring for load order and error
    semantics.
    """
    from ._common import hydrate_dotenv

    hydrate_dotenv(project_root, environment=environment)


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


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Main verify command execution"""

    cprint("=" * 80)
    cprint("🔍 FLUID Verify - Multi-Dimensional Contract Validation")
    cprint("=" * 80)

    # Load contract using shared infrastructure (overlays now work!)
    contract_path = args.contract
    cprint(f"Contract: {contract_path}")
    if not Path(contract_path).exists():
        raise CLIError(1, "contract_not_found", {"path": contract_path})
    try:
        contract = load_contract_with_overlay(contract_path, getattr(args, "env", None), logger)
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "contract_load_failed", {"path": contract_path, "error": str(e)})

    contract_id = contract.get("id", "unknown")
    cprint(f"Contract ID: {contract_id}")

    # Hydrate .env files into os.environ so subsequent {{ env.VAR }}
    # resolution (and the Snowflake identifier allowlist) sees values
    # the user already staged in .env. Matches the apply path, which hits
    # the credential resolver chain with project_root defaulted to Path.cwd()
    # (see provider_enhanced -> resolve_snowflake_settings fallback). The
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

            database = _resolve_env_templates(location.get("database")) or snowflake_settings.get(
                "database"
            )
            schema = _resolve_env_templates(location.get("schema")) or snowflake_settings.get(
                "schema"
            )
            table = (
                _resolve_env_templates(location.get("table"))
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
        else:
            results[expose_name] = {
                "status": "error",
                "error": f"Unsupported format: {format_type}",
            }

    # Display results with dimensional analysis
    match_count = 0
    mismatch_count = 0
    error_count = 0

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
                        _resolve_env_templates(location.get("database", "")) or "",
                        _resolve_env_templates(location.get("schema", "")) or "",
                        _resolve_env_templates(location.get("table", "")) or "",
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

        if result["status"] == "error":
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

        # Update counts
        if status == "match":
            match_count += 1
        else:
            mismatch_count += 1

    # Summary
    cprint("\n" + "=" * 80)
    cprint("📊 Verification Summary")
    cprint("=" * 80)
    cprint(f"Total verified: {len(results)}")
    success(f"Match: {match_count}")
    warning(f"Mismatch: {mismatch_count}")
    console_error(f"Error: {error_count}")

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
            },
            "results": results,
        }

        output_path = Path(args.out)
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2)

        cprint(f"\n📄 Report saved: {output_path}")

    # Exit with error code if strict mode and issues found
    if args.strict and (mismatch_count > 0 or error_count > 0):
        return 1

    return 0
