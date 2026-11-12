# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid test --results-table``: append one run's results to a Unity Catalog table.

One row per test case of the run, the same cases ``--output junit`` reports
(``_test_cases.py``): each data-quality rule that ran, then each structural
check category (``rule_type = 'structure'``). The table is shared by every
product, so it lives in a schema the operator names, never in a product's own
schema (``fluid apply`` owns those).

The shape follows the one-row-per-check-per-run results tables of Databricks
Labs DQX (its summary-metrics table: run id, run time, metric, value) and of
Elementary's ``elementary_test_results``; the pattern is adapted, no code is
taken (DQX is under the Databricks License).

What is created: the table, with ``CREATE TABLE IF NOT EXISTS``, owned by the
identity that first runs this. The catalog and the schema are the operator's
and are never created. An existing table must have every column below, or
nothing is written: a name that points at some other table writes nothing
into it.

Every value is bound as a ``:name`` parameter; the three name parts are
validated identifiers, backtick-quoted.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from fluid_build.providers._sql_safety import validate_ident

from ._common import CLIError
from ._test_cases import rule_checks, structural_categories

#: ``(column, type, comment)`` in table order.
RESULT_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
    ("run_id", "STRING NOT NULL", "One fluid test invocation"),
    ("run_time", "TIMESTAMP NOT NULL", "When the checks ran (UTC)"),
    ("product_id", "STRING NOT NULL", "The contract id"),
    ("product_version", "STRING", "The contract version"),
    ("layer", "STRING", "metadata.layer: Bronze, Silver or Gold"),
    ("expose_id", "STRING", "The expose the check ran against"),
    ("table_name", "STRING", "catalog.schema.table of that expose"),
    ("rule_id", "STRING NOT NULL", "dq.rules[].id, or the name of the structural check"),
    ("rule_type", "STRING", "dq.rules[].type, or structure for the checks of fluid test itself"),
    ("severity", "STRING", "The declared severity of the rule"),
    ("passed", "BOOLEAN NOT NULL", "Whether the check passed"),
    ("observed_value", "STRING", "What was measured"),
    ("threshold", "STRING", "What it had to be"),
    ("message", "STRING", "What happened"),
    ("fluid_version", "STRING", "The data-product-forge version that ran the checks"),
)

#: ``rule_type`` of the rows for ``fluid test``'s own structural checks.
STRUCTURE_RULE_TYPE = "structure"

_CASTS = {"run_time": "TIMESTAMP", "passed": "BOOLEAN"}
_TABLE_COMMENT = "fluid test results: one row per check per run (data-product-forge)"
_MAX_TEXT = 2000
TABLES_PATH = "/api/2.1/unity-catalog/tables"


def _status(message: str) -> None:
    """One status line on stderr, so ``--output json`` keeps stdout clean."""
    sys.stderr.write(message + "\n")


def parse_results_table(value: Any) -> Tuple[str, str, str]:
    """``catalog.schema.table`` of ``--results-table``, each part validated."""
    parts = str(value or "").split(".")
    try:
        if len(parts) != 3:
            raise ValueError(value)
        return validate_ident(parts[0]), validate_ident(parts[1]), validate_ident(parts[2])
    except ValueError:
        raise CLIError(
            2,
            "test_results_table_invalid",
            {
                "results_table": str(value),
                "expected": "catalog.schema.table, each part letters, digits and underscores",
            },
        ) from None


def _expose_tables(contract: Mapping[str, Any]) -> Dict[str, Tuple[str, str, str]]:
    """``expose id -> (catalog, schema, table)`` for each expose bound to a table."""
    out: Dict[str, Tuple[str, str, str]] = {}
    exposes = contract.get("exposes")
    # A malformed ``exposes`` (not a list) is the validator's to report.
    for idx, expose in enumerate(exposes if isinstance(exposes, list) else []):
        if not isinstance(expose, dict):
            continue
        binding = expose.get("binding") if isinstance(expose.get("binding"), dict) else {}
        location = binding.get("location") if isinstance(binding.get("location"), dict) else {}
        catalog = location.get("database") or location.get("catalog")
        schema, table = location.get("schema"), location.get("table")
        if catalog and schema and table:
            key = str(expose.get("id") or expose.get("exposeId") or f"expose_{idx}")
            out[key] = (str(catalog), str(schema), str(table))
    return out


def _expose_versions(contract: Mapping[str, Any]) -> Dict[str, str]:
    """``expose id -> exposes[].version`` (the version ``fluid apply`` tags the table with)."""
    out: Dict[str, str] = {}
    for idx, expose in enumerate(contract.get("exposes") or []):
        if isinstance(expose, dict) and expose.get("version") is not None:
            key = str(expose.get("id") or expose.get("exposeId") or f"expose_{idx}")
            out[key] = str(expose["version"])
    return out


def refuse_product_schema(table: Tuple[str, str, str], contract: Mapping[str, Any]) -> None:
    """A results table in one of the tested product's own schemas is refused."""
    for catalog, schema, _ in _expose_tables(contract).values():
        if (catalog.lower(), schema.lower()) == (table[0].lower(), table[1].lower()):
            raise CLIError(
                2,
                "test_results_table_in_product_schema",
                {
                    "results_table": ".".join(table),
                    "product_schema": f"{catalog}.{schema}",
                    "why": "the results table is shared by every product, and fluid apply "
                    "owns this product's schema",
                    "fix": f"name a schema of its own, e.g. {table[0]}.dq.{table[2]}",
                },
            )


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 3] + "..."


def _severity(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    return text or None


def results_rows(
    report: Any,
    contract: Mapping[str, Any],
    *,
    run_id: str,
    fluid_version: Optional[str],
) -> List[Dict[str, Any]]:
    """The rows of one run: each rule that ran, then each structural check."""
    tables = _expose_tables(contract)
    versions = _expose_versions(contract)
    # The contract's own version when it has one; the report's "0.0.0" is a
    # placeholder for "none declared", never a version.
    product_version = contract.get("version")
    product_version = None if product_version is None else str(product_version)
    metadata = contract.get("metadata") if isinstance(contract.get("metadata"), dict) else {}
    run_time = report.validation_time.astimezone(timezone.utc)
    only_table = next(iter(tables.items())) if len(tables) == 1 else None
    common = {
        "run_id": run_id,
        "run_time": run_time,
        "product_id": report.contract_id,
        "layer": _text(metadata.get("layer")),
        "fluid_version": fluid_version,
    }
    rows: List[Dict[str, Any]] = []
    for check in rule_checks(report):
        expose_id = check.expose_id or (check.name.rsplit(".", 1)[0] if "." in check.name else None)
        table = tables.get(expose_id or "")
        rows.append(
            {
                **common,
                "product_version": _text(versions.get(expose_id or "", product_version)),
                "expose_id": expose_id,
                "table_name": ".".join(table) if table else None,
                "rule_id": check.rule_id or check.name.rsplit(".", 1)[-1],
                "rule_type": check.rule_type,
                "severity": _severity(check.severity),
                "passed": bool(check.passed),
                "observed_value": _text(check.actual),
                "threshold": _text(check.expected),
                "message": _text(check.message),
            }
        )
    builtin, extra, by_category = structural_categories(report)
    from fluid_build.severity import is_error, is_warning

    for category in builtin + extra:
        issues = by_category.get(category, [])
        errors = [i for i in issues if is_error(i.severity)]
        warnings = [i for i in issues if is_warning(i.severity)]
        shown = errors + warnings
        single = shown[0] if len(shown) == 1 else None
        if single is not None:
            observed, threshold = single.actual, single.expected
        elif shown:
            observed, threshold = f"{len(errors)} error(s), {len(warnings)} warning(s)", None
        else:
            observed, threshold = None, None
        only = only_table[0] if only_table else None
        rows.append(
            {
                **common,
                "product_version": _text(versions.get(only or "", product_version)),
                "expose_id": only,
                "table_name": ".".join(only_table[1]) if only_table else None,
                "rule_id": category,
                "rule_type": STRUCTURE_RULE_TYPE,
                "severity": "error" if errors else ("warning" if warnings else None),
                "passed": not errors,
                "observed_value": _text(observed),
                "threshold": _text(threshold),
                "message": _text("; ".join(i.message for i in shown) or "OK"),
            }
        )
    return rows


def _create_table_sql(ref: str) -> str:
    from fluid_build.providers._sql_safety import quote_string_literal_for

    columns = ",\n  ".join(
        f"{name} {kind} COMMENT {quote_string_literal_for('databricks', note)}"
        for name, kind, note in RESULT_COLUMNS
    )
    comment = quote_string_literal_for("databricks", _TABLE_COMMENT)
    return f"CREATE TABLE IF NOT EXISTS {ref} (\n  {columns}\n) USING DELTA\nCOMMENT {comment}"


def _value(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "passed":
        return "true" if value else "false"
    if name == "run_time" and isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return value


def insert_statement(ref: str, rows: Sequence[Mapping[str, Any]]) -> Tuple[str, List]:
    """One ``INSERT`` for every row of the run, and its one parameter.

    The rows travel as a single JSON array bound to ``:rows`` and are unpacked in
    SQL (``from_json`` + ``explode``), so the run is appended in one statement:
    all of it or none of it, whatever its size. A run split over several
    statements could be left half-written, and the dashboard would take that
    half for the latest run.
    """
    from fluid_build.providers.databricks.statements import param

    names = [name for name, _, _ in RESULT_COLUMNS]
    struct = ", ".join(f"{name}: STRING" for name in names)
    select = ", ".join(
        f"CAST(r.{name} AS {_CASTS[name]})" if name in _CASTS else f"r.{name}" for name in names
    )
    statement = (
        f"INSERT INTO {ref} ({', '.join(names)})\n"
        f"SELECT {select}\n"
        f"FROM (SELECT explode(from_json(:rows, 'ARRAY<STRUCT<{struct}>>')) AS r)"
    )
    payload = [{name: _value(name, row.get(name)) for name in names} for row in rows]
    return statement, [param("rows", json.dumps(payload))]


def _fix_for(error: str, table: Tuple[str, str, str]) -> Optional[str]:
    """The one action that fixes a failed write, when the error says which."""
    catalog, schema, _ = table
    upper = error.upper()
    if "NO MODULE NAMED 'DATABRICKS" in upper:
        from fluid_build.providers.databricks.statements import DATABRICKS_EXTRA_INSTALL

        return f"Install the Databricks extra: {DATABRICKS_EXTRA_INSTALL}"
    if "SCHEMA_NOT_FOUND" in upper or "NO_SUCH_SCHEMA" in upper:
        return f"Create the schema first (forge never creates it): CREATE SCHEMA `{catalog}`.`{schema}`"
    if "CATALOG_NOT_FOUND" in upper or "NO_SUCH_CATALOG" in upper:
        return f"Catalog `{catalog}` does not exist; name an existing catalog"
    if "PERMISSION" in upper or "INSUFFICIENT_PRIVILEGES" in upper or "NOT AUTHORIZED" in upper:
        return (
            f"Grant this identity USE CATALOG on `{catalog}`, USE SCHEMA and CREATE TABLE on "
            f"`{catalog}`.`{schema}`, and MODIFY on the table once it exists"
        )
    return None


def _check_columns(client: Any, full_name: str) -> None:
    """Refuse a table that lacks one of :data:`RESULT_COLUMNS`."""
    table = client.api_client.do("GET", f"{TABLES_PATH}/{quote(full_name, safe='')}")
    have = {
        str(c.get("name", "")).lower()
        for c in (table.get("columns") or [] if isinstance(table, dict) else [])
        if isinstance(c, dict)
    }
    missing = [name for name, _, _ in RESULT_COLUMNS if name not in have]
    if missing:
        raise CLIError(
            1,
            "test_results_table_mismatch",
            {
                "results_table": full_name,
                "missing_columns": ", ".join(missing),
                "why": "the table exists and is not a fluid test results table, so nothing "
                "was written to it",
            },
        )


def write_results(
    report: Any,
    contract: Mapping[str, Any],
    results_table: str,
    *,
    client_factory: Optional[Callable[[], Any]] = None,
    warehouse_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Tuple[str, int]:
    """Append the run's rows; return ``(table, rows written)``.

    Raises :class:`CLIError` with what to do when nothing could be written.
    """
    from fluid_build import __version__
    from fluid_build.providers.databricks.statements import (
        StatementRunner,
        default_client,
        scrub,
        warehouse_id_from_env,
    )

    table = parse_results_table(results_table)
    refuse_product_schema(table, contract)
    full_name = ".".join(table)
    ref = ".".join(f"`{p}`" for p in table)
    rows = results_rows(
        report, contract, run_id=run_id or uuid.uuid4().hex, fluid_version=__version__
    )
    try:
        client = (client_factory or default_client)()
        runner = StatementRunner(client, warehouse_id or warehouse_id_from_env())
        runner.run(_create_table_sql(ref))
        _check_columns(client, full_name)
        statement, params = insert_statement(ref, rows)
        runner.run(statement, params)
    except CLIError:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure becomes one safe message
        message = f"{type(exc).__name__}: {scrub(exc)}"
        context: Dict[str, Any] = {"results_table": full_name, "error": message}
        fix = _fix_for(message, table)
        if fix:
            context["fix"] = fix
        raise CLIError(1, "test_results_write_failed", context) from None
    _status(f"Results: {len(rows)} row(s) appended to {full_name}")
    return full_name, len(rows)


__all__ = [
    "RESULT_COLUMNS",
    "STRUCTURE_RULE_TYPE",
    "insert_statement",
    "parse_results_table",
    "refuse_product_schema",
    "results_rows",
    "write_results",
]
