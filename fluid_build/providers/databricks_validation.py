# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid test`` against a Unity Catalog table (``binding.platform: databricks``).

What is read, and what it costs:

- **Columns**: ``GET /api/2.1/unity-catalog/tables/{catalog.schema.table}``, a
  metadata call that starts no compute. A 404 means the table does not exist.
  The contract's types go through the emitter's own FLUID → Databricks type
  table (``engines/dbt/_types.sql_type``) and both sides through the
  provider's normaliser, so ``VARCHAR`` matches the ``string`` that
  ``fluid apply`` created.
- **Row count and data-quality rules**: SQL on ``DATABRICKS_WAREHOUSE_ID``
  through the Statement Execution API (``databricks/statements.py``), one
  ``SELECT`` per rule; a rule over several columns is still one statement.
  Nothing is written.

The table is ``binding.location`` ``database`` (the Unity Catalog catalog, the
convention ``fluid apply`` uses), ``schema`` and ``table``; every part is
validated before it reaches a URL or a statement, and backtick-quoted in SQL.

Sign-in is ``databricks_auth.sdk_config`` (``DATABRICKS_HOST`` plus a token,
an OAuth client or a named profile), the policy ``fluid apply`` and
``fluid verify`` share.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.quality_engine import (
    QualityCheckResult,
    execute_quality_checks,
    expose_columns,
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

TABLES_PATH = "/api/2.1/unity-catalog/tables"

#: The table tag ``fluid apply`` puts a product's id in (``iac/providers/databricks.py``
#: ``_PRODUCT_TAGS``); a reference to another product finds its table by it.
PRODUCT_TAG = "fluid_product_id"
WAREHOUSES_PATH = "/api/2.0/sql/warehouses"


def _is_not_found(exc: BaseException) -> bool:
    for attr in ("status_code", "status", "http_status"):
        if getattr(exc, attr, None) == 404:
            return True
    if type(exc).__name__ in {"NotFound", "ResourceDoesNotExist"}:
        return True
    code = str(getattr(exc, "error_code", "") or "")
    return code in {"TABLE_DOES_NOT_EXIST", "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"}


def table_name(resource_spec: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    """``(catalog, schema, table)`` of an expose, each validated, or ``None``.

    Raises ``ValueError`` for a part that is not a plain identifier.
    """
    binding = resource_spec.get("binding") or {}
    location = binding.get("location") if isinstance(binding, dict) else None
    if not isinstance(location, dict):
        return None
    catalog = location.get("database") or location.get("catalog")
    schema = location.get("schema")
    table = location.get("table")
    if not (catalog and schema and table):
        return None
    return validate_ident(str(catalog)), validate_ident(str(schema)), validate_ident(str(table))


def sql_table_ref(parts: Tuple[str, str, str]) -> str:
    """```catalog`.`schema`.`table``` for validated parts."""
    return ".".join(f"`{validate_ident(p)}`" for p in parts)


def _not_run(rule: Dict[str, Any], full_name: str) -> QualityCheckResult:
    """A rule on a table that does not exist: failed at its severity, never run."""
    rule_id = str(rule.get("id") or "unnamed")
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type=str(rule.get("type") or "").lower(),
        selector=str(rule.get("selector") or ""),
        passed=False,
        severity=rule.get("severity", "warning"),
        message=f"Rule '{rule_id}' was not checked: table {full_name} does not exist",
        expected="the table to exist",
        actual="not executed",
    )


class DatabricksValidationProvider(ValidationProvider):
    """Validate an expose against its live Unity Catalog table."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self._client_factory: Optional[Callable[[], Any]] = config.get("client_factory")
        self._warehouse_id: Optional[str] = config.get("warehouse_id")
        self._client: Any = None
        self._runner: Any = None
        self._references: Dict[Tuple[str, str, str], Any] = {}
        self._tables: Dict[str, Optional[Dict[str, Any]]] = {}

    @property
    def provider_name(self) -> str:
        return "databricks"

    # ── plumbing ────────────────────────────────────────────────────────

    def _get_client(self) -> Any:
        if self._client is None:
            from fluid_build.providers.databricks.statements import default_client

            self._client = (self._client_factory or default_client)()
        return self._client

    def _sql(self):
        if self._runner is None:
            from fluid_build.providers.databricks.statements import (
                StatementRunner,
                warehouse_id_from_env,
            )

            warehouse = self._warehouse_id or warehouse_id_from_env(os.environ)
            self._runner = StatementRunner(self._get_client(), warehouse)
        return self._runner

    def _table(self, parts: Tuple[str, str, str]) -> Optional[Dict[str, Any]]:
        """The table's Unity Catalog metadata, read once per run; ``None`` when it
        does not exist. A metadata read, so no compute starts."""
        full_name = ".".join(parts)
        if full_name not in self._tables:
            try:
                self._tables[full_name] = self._get(f"{TABLES_PATH}/{quote(full_name, safe='')}")
            except Exception as exc:  # noqa: BLE001
                if not _is_not_found(exc):
                    from fluid_build.providers.databricks.statements import StatementError, scrub

                    raise StatementError(f"{type(exc).__name__}: {scrub(exc)}") from None
                self._tables[full_name] = None
        return self._tables[full_name]

    def _get(self, path: str) -> Dict[str, Any]:
        out = self._get_client().api_client.do("GET", path)
        return out if isinstance(out, dict) else {}

    # ── ValidationProvider ──────────────────────────────────────────────

    def validate_connection(self) -> bool:
        """Sign in and find the warehouse; neither starts compute.

        Raises with a message that says what to fix: no host or credential,
        no ``DATABRICKS_WAREHOUSE_ID``, or a warehouse this identity cannot see.
        """
        from fluid_build.providers.databricks.statements import (
            StatementError,
            scrub,
            warehouse_id_from_env,
        )

        warehouse = self._warehouse_id or warehouse_id_from_env(os.environ)
        try:
            self._get(f"{WAREHOUSES_PATH}/{quote(warehouse, safe='')}")
        except Exception as exc:  # noqa: BLE001 - one safe message per failure
            if _is_not_found(exc):
                raise StatementError(
                    f"SQL warehouse {warehouse!r} was not found in the workspace "
                    "DATABRICKS_HOST names, or this identity cannot use it"
                ) from None
            raise StatementError(f"{type(exc).__name__}: {scrub(exc)}") from None
        return True

    def get_resource_schema(self, resource_spec: Dict[str, Any]) -> Optional[ResourceSchema]:
        parts = table_name(resource_spec)
        if parts is None:
            raise ValueError(
                "binding.location needs database (the Unity Catalog catalog), schema and table"
            )
        full_name = ".".join(parts)
        table = self._table(parts)
        if table is None:
            return None
        from fluid_build.iac.providers.databricks import normalize_column_type

        fields: List[FieldSchema] = []
        for col in table.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name"):
                continue
            raw = col.get("type_text") or col.get("type_name") or ""
            fields.append(
                FieldSchema(
                    name=str(col["name"]),
                    type=normalize_column_type(raw),
                    mode="NULLABLE" if col.get("nullable", True) is not False else "REQUIRED",
                    description=col.get("comment"),
                )
            )
        rows = self._sql().run(f"SELECT COUNT(*) FROM {sql_table_ref(parts)}")
        row_count = int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else None
        return ResourceSchema(
            resource_type=ResourceType.TABLE,
            fully_qualified_name=full_name,
            fields=fields,
            row_count=row_count,
            metadata={"table_type": table.get("table_type")},
        )

    def validate_resource(
        self,
        contract_spec: Dict[str, Any],
        actual_schema: Optional[ResourceSchema],
    ) -> ValidationResult:
        issues: List[ValidationIssue] = []
        resource_name = contract_spec.get("id") or contract_spec.get("exposeId") or "unknown"
        if actual_schema is None:
            parts = table_name(contract_spec)
            name = ".".join(parts) if parts else "unknown"
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="missing_resource",
                    message=f"Table '{name}' does not exist in Unity Catalog",
                    path="exposes[].binding.location",
                    expected=name,
                    actual=None,
                    suggestion="Run 'fluid apply' for this contract, or correct "
                    "binding.location (database is the catalog)",
                )
            )
            return ValidationResult(resource_name, False, issues, None)

        expected = self._expected_fields(contract_spec)
        if expected:
            issues.extend(self.compare_schemas(expected, actual_schema.fields))

        if actual_schema.row_count == 0:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="empty_table",
                    message=f"Table '{actual_schema.fully_qualified_name}' has no rows",
                    path="exposes[].binding.location",
                    expected="> 0 rows",
                    actual="0 rows",
                    suggestion="Run the product's build, then test again",
                )
            )
        sla = contract_spec.get("sla") or {}
        min_rows = sla.get("row_count_min") if isinstance(sla, dict) else None
        if min_rows is not None and actual_schema.row_count is not None:
            if actual_schema.row_count < min_rows:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="row_count_below_threshold",
                        message=(
                            f"Table has {actual_schema.row_count} rows, below minimum of "
                            f"{min_rows}"
                        ),
                        path="exposes[].sla.row_count_min",
                        expected=f">= {min_rows} rows",
                        actual=f"{actual_schema.row_count} rows",
                        suggestion="Check data pipeline for issues",
                    )
                )
        return ValidationResult(
            resource_name=resource_name,
            success=not any(i.severity == "error" for i in issues),
            issues=issues,
            schema=actual_schema,
        )

    def normalize_type(self, provider_type: str) -> str:
        from fluid_build.iac.providers.databricks import normalize_column_type

        return normalize_column_type(provider_type)

    @staticmethod
    def _expected_fields(resource_spec: Dict[str, Any]) -> List[FieldSchema]:
        """The contract's columns, typed as ``fluid apply`` creates them."""
        from fluid_build.engines.dbt._types import sql_type

        fields: List[FieldSchema] = []
        for col in expose_columns(resource_spec):
            if "required" in col:
                mode = "REQUIRED" if col["required"] else "NULLABLE"
            elif "nullable" in col:
                mode = "NULLABLE" if col["nullable"] else "REQUIRED"
            else:
                mode = str(col.get("mode") or "NULLABLE").upper()
            fields.append(
                FieldSchema(
                    name=str(col.get("name", "")),
                    type=sql_type(str(col.get("type") or "string"), "databricks"),
                    mode=mode,
                    description=col.get("description"),
                )
            )
        return fields

    # ── data-quality rules ──────────────────────────────────────────────

    def run_quality_results(
        self, resource_spec: Dict[str, Any], rules: List[Dict[str, Any]]
    ) -> List[QualityCheckResult]:
        """Every rule's result, passing ones included (with what was measured).

        A table that does not exist runs no statement: each rule is reported as not
        executed, and the missing table itself is the resource check's error.
        """
        parts = table_name(resource_spec)
        if parts is None:
            raise ValueError(
                "binding.location needs database (the Unity Catalog catalog), schema and table"
            )
        if self._table(parts) is None:
            return [_not_run(rule, ".".join(parts)) for rule in rules if isinstance(rule, dict)]
        return execute_quality_checks(
            rules=rules,
            table_ref=sql_table_ref(parts),
            execute_fn=self._sql().run,
            dialect="databricks",
            columns=expose_columns(resource_spec),
            resolve_reference=lambda product, column: self._reference_table(
                product, column, prefer_catalog=parts[0]
            ),
        )

    def _reference_table(
        self, product_id: str, column: str, prefer_catalog: Optional[str] = None
    ) -> str:
        """The live table of ``product_id`` that has ``column``, as a SQL reference.

        Found by the ``fluid_product_id`` tag ``fluid apply`` puts on every table it
        makes (one ``system.information_schema.table_tags`` query, which lists only
        what this identity can see), then by the column, from each candidate's
        Unity Catalog metadata. Tables in ``prefer_catalog`` (the tested table's
        catalog) win over tables elsewhere, so a look-alike tagged table in another
        catalog cannot stand in for the product's own. Raises ``LookupError`` saying
        why when there is no such table, or more than one.
        """
        key = (product_id, column.lower(), (prefer_catalog or "").lower())
        if key not in self._references:
            self._references[key] = self._find_reference(product_id, column, prefer_catalog)
        found = self._references[key]
        if isinstance(found, LookupError):
            raise found
        return found

    def _find_reference(
        self, product_id: str, column: str, prefer_catalog: Optional[str] = None
    ) -> Any:
        from fluid_build.providers.databricks.statements import param

        rows = self._sql().run(
            "SELECT catalog_name, schema_name, table_name "
            "FROM system.information_schema.table_tags "
            "WHERE tag_name = :tag AND tag_value = :product",
            [param("tag", PRODUCT_TAG), param("product", product_id)],
        )
        candidates = sorted({tuple(str(v) for v in row[:3]) for row in rows if len(row) >= 3})
        having = []
        for parts in candidates:
            try:
                table = self._get(f"{TABLES_PATH}/{quote('.'.join(parts), safe='')}")
            except Exception:  # noqa: BLE001 - a table this identity cannot read is no match
                continue
            names = {str(c.get("name", "")).lower() for c in table.get("columns") or []}
            if column.lower() in names:
                having.append(parts)
        local = [p for p in having if prefer_catalog and p[0].lower() == prefer_catalog.lower()]
        if local:
            having = local
        if not having:
            return LookupError(
                f"no table tagged {PRODUCT_TAG}={product_id} with a column {column!r} is "
                "visible to this identity (has that product been applied?)"
            )
        if len(having) > 1:
            listed = ", ".join(".".join(p) for p in having[:3])
            return LookupError(
                f"{len(having)} tables tagged {PRODUCT_TAG}={product_id} have a column "
                f"{column!r} ({listed}), so which one it references is ambiguous"
            )
        try:
            return sql_table_ref(having[0])
        except ValueError:
            return LookupError(
                f"the table of {product_id} has a name that is not a plain identifier"
            )

    def run_quality_checks(
        self, resource_spec: Dict[str, Any], rules: List[Dict[str, Any]]
    ) -> List[ValidationIssue]:
        return quality_results_to_issues(self.run_quality_results(resource_spec, rules))


__all__ = ["DatabricksValidationProvider", "sql_table_ref", "table_name"]
