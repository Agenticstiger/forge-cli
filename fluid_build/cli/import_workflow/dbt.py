# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""dbt ``target/manifest.json`` → FLUID contract importer.

Faithful brownfield conversion of a real dbt project. ``dbt parse`` (no
warehouse access needed) produces ``target/manifest.json``; this importer
reads it with **plain stdlib JSON — no dbt-core dependency** — and emits
ONE DataProduct per dbt project (per-folder/per-group splitting is a noted
follow-up):

* nodes + ``depends_on``     → one expose per model/seed/snapshot, with the
  ref-derived intra-project DAG recorded per-expose (``dbt-depends-on``
  label) and per-step ``builds[].transformations``
* sources                    → ``consumes[]`` (source freshness →
  ``qosExpectations.freshnessMax``)
* generic tests              → ``dq.rules[]`` via the SHARED reverse table
  in :mod:`fluid_build.engines.dbt._test_mapping` (NOT a 4th divergent
  mapper); ``relationships`` / range tests → column ``validationRules``
* ``config.materialized``    → expose kind + build materialization hints
* folder/layer               → ``metadata.layer`` + ``metadata.productType``
  via :func:`fluid_build.forge.product_types.normalize_metadata_in_place`
  (staging→Bronze/SDP, marts→Gold/CDP, mirroring
  ``engines/dbt/models.py::_infer_layer``)
* ``catalog.json`` / schema.yml ``data_type`` → column types

Design borrowed from ``datacontract-cli``'s ``dbt_importer.py`` (MIT):
zero-dbt-dependency manifest parse, the ``/v(\\d+).json`` minimum-schema-
version gate (>= v9, dbt 1.5+), the PK-inference precedence that reimplements
dbt's ``ModelNode.infer_primary_key``, FK recovery from ``relationships``
tests, and ``materialized`` → physical-type mapping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .registry import Importer, ImportReport

# Manifest schema v9 (dbt 1.5, May 2023) is where column constraints
# stabilised — same floor datacontract-cli enforces. v12 (dbt 1.8+) is the
# primary target and has been stable since mid-2024.
MIN_MANIFEST_SCHEMA_VERSION = 9
_SCHEMA_VERSION_RE = re.compile(r"/v(\d+)\.json")

# ``ref('model')`` / ``ref("pkg", "model")`` — last quoted arg is the model.
_REF_RE = re.compile(r"ref\(\s*(?:['\"][^'\"]+['\"]\s*,\s*)?['\"]([^'\"]+)['\"]\s*\)")

# FLUID identifier pattern (schema `$defs.identifier`).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*[A-Za-z0-9_]$|^[A-Za-z0-9_]$")

# dbt folder layer → medallion layer (mirrors engines/dbt/models.py).
_LAYER_TO_MEDALLION = {"staging": "Bronze", "intermediate": "Silver", "marts": "Gold"}
_LAYER_ORDER = ("staging", "intermediate", "marts")

# adapter_type → (binding.platform, binding.format) for the v0.7.3 enums.
_ADAPTER_BINDINGS: Dict[str, Tuple[str, str]] = {
    "bigquery": ("gcp", "bigquery_table"),
    "snowflake": ("snowflake", "snowflake_table"),
    "redshift": ("aws", "redshift_table"),
    "databricks": ("databricks", "delta_table"),
    "duckdb": ("local", "other"),
}

# Canonical bare column types accepted by the v0.7.3 `$defs.column.type` enum
# (parameterised forms like decimal(18,4) are validated by base-token here and
# by the schema's case-insensitive pattern downstream).
_CANONICAL_TYPES = frozenset(
    {
        "array", "bigint", "bignumeric", "bigserial", "binary", "bit", "blob", "bool",
        "boolean", "bytea", "bytes", "char", "character", "clob", "date", "datetime",
        "datetime2", "dec", "decimal", "double", "enum", "float", "float32", "float4",
        "float64", "float8", "geography", "geom", "geometry", "guid", "hll", "int",
        "int16", "int2", "int32", "int4", "int64", "int8", "integer", "interval",
        "json", "jsonb", "long", "longint", "map", "mediumint", "money", "nchar",
        "number", "numeric", "nvarchar", "object", "point", "raw", "real", "record",
        "row", "serial", "smalldatetime", "smallint", "string", "struct", "super",
        "text", "time", "timestamp", "timestamp_ltz", "timestamp_ntz", "timestamp_tz",
        "timestampntz", "timestamptz", "tinyint", "uniqueidentifier", "uuid",
        "varbinary", "varchar", "varchar2", "variant", "year",
    }
)  # fmt: skip

# Multi-word / warehouse-native spellings → canonical FLUID types
# (case-insensitive prefix matching, same approach as datacontract-cli's
# ``map_dbt_type_to_odcs``).
_TYPE_ALIASES = {
    "character varying": "varchar",
    "double precision": "double",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "time",
}

_DEFAULT_OWNER = {"team": "imported", "email": "import@forge.local"}


@dataclass
class DbtManifestImporter(Importer):
    """``fluid import dbt <project-dir | manifest.json>``."""

    name: str = "dbt"

    def can_import(self, source: str) -> bool:
        return (
            locate_manifest(Path(source)) is not None or (Path(source) / "dbt_project.yml").exists()
        )

    def import_to_contract(
        self, source: str, *, options: Optional[Dict[str, Any]] = None
    ) -> tuple[Dict[str, Any], ImportReport]:
        options = options or {}
        manifest_path = locate_manifest(Path(source))
        if manifest_path is None:
            raise FileNotFoundError(
                f"No target/manifest.json found under {source}. "
                "Run `dbt parse` in the project (no warehouse access needed) "
                "to produce it, then re-run the import."
            )

        with manifest_path.open(encoding="utf-8") as f:
            manifest = json.load(f)

        report = ImportReport()
        version = _manifest_schema_version(manifest)
        if version is not None and version < MIN_MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"manifest schema v{version} is older than the supported minimum "
                f"v{MIN_MANIFEST_SCHEMA_VERSION} (dbt 1.5+). Re-run `dbt parse` "
                "with a current dbt to regenerate target/manifest.json."
            )
        if version is None:
            report.notes.append(
                "manifest metadata.dbt_schema_version missing/unparseable — proceeding best-effort"
            )

        catalog = _load_catalog(manifest_path, options, report)
        contract = _build_contract(manifest, manifest_path, version, catalog, report)
        return contract, report


# ---------------------------------------------------------------------------
# Location / version helpers (also used by the legacy-scan router)
# ---------------------------------------------------------------------------


def locate_manifest(source: Path) -> Optional[Path]:
    """Resolve *source* (project dir or direct path) to a manifest.json."""
    if source.is_file() and source.suffix == ".json":
        return source
    if source.is_dir():
        for candidate in (source / "target" / "manifest.json", source / "manifest.json"):
            if candidate.is_file():
                return candidate
    return None


def _manifest_schema_version(manifest: Dict[str, Any]) -> Optional[int]:
    raw = str((manifest.get("metadata") or {}).get("dbt_schema_version") or "")
    match = _SCHEMA_VERSION_RE.search(raw)
    return int(match.group(1)) if match else None


def _load_catalog(
    manifest_path: Path, options: Dict[str, Any], report: ImportReport
) -> Dict[str, Any]:
    """Load catalog.json (warehouse column types) if present next to the manifest."""
    catalog_path = (
        Path(options["catalog"])
        if options.get("catalog")
        else (manifest_path.parent / "catalog.json")
    )
    if not catalog_path.is_file():
        report.required_defaults.append(
            "catalog.json not found — column types come from schema.yml data_type only "
            "(run `dbt docs generate` for warehouse-accurate types)"
        )
        return {}
    try:
        with catalog_path.open(encoding="utf-8") as f:
            return json.load(f) or {}
    except (json.JSONDecodeError, OSError) as exc:
        report.notes.append(f"catalog.json unreadable ({exc}) — ignored")
        return {}


# ---------------------------------------------------------------------------
# Contract assembly
# ---------------------------------------------------------------------------


def _build_contract(
    manifest: Dict[str, Any],
    manifest_path: Path,
    version: Optional[int],
    catalog: Dict[str, Any],
    report: ImportReport,
) -> Dict[str, Any]:
    meta = manifest.get("metadata") or {}
    project_name = str(meta.get("project_name") or "dbt-project")
    adapter = str(meta.get("adapter_type") or "").lower()
    nodes: Dict[str, Any] = manifest.get("nodes") or {}
    sources: Dict[str, Any] = manifest.get("sources") or {}

    models = _select_project_nodes(nodes, project_name, report)
    if not models:
        report.unsupported.append(f"no enabled models found for project {project_name!r}")
        return {}

    tests_by_model = _collect_generic_tests(nodes, project_name, set(models), report)

    exposes: List[Dict[str, Any]] = []
    transformations: List[Dict[str, Any]] = []
    materializations: Dict[str, str] = {}
    layers_present: set[str] = set()
    referenced_sources: List[str] = []
    expose_by_model_uid: Dict[str, Dict[str, Any]] = {}

    for uid, node in models.items():
        layer = _infer_model_layer(node)
        layers_present.add(layer)
        materialized = str((node.get("config") or {}).get("materialized") or "view").lower()

        for dep in (node.get("depends_on") or {}).get("nodes") or []:
            if isinstance(dep, str) and dep.startswith("source.") and dep in sources:
                referenced_sources.append(dep)

        if materialized == "ephemeral":
            report.notes.append(
                f"model {node.get('name')} is ephemeral (no physical relation) — "
                "kept in lineage labels, no expose emitted"
            )
            continue

        expose = _build_expose(
            uid, node, layer, materialized, adapter, catalog, tests_by_model.get(uid, []), report
        )
        exposes.append(expose)
        expose_by_model_uid[uid] = expose
        report.mapped_one_to_one.append(f"model.{node.get('name')}")
        transformations.append(
            {
                "name": str(node.get("name")),
                "model": str(node.get("original_file_path") or node.get("path") or ""),
                "outputs": [expose["exposeId"]],
            }
        )
        if materialized in ("table", "view", "incremental"):
            materializations[expose["exposeId"]] = materialized

    consumes = _build_consumes(referenced_sources, sources, report)
    _attach_semantic_models(manifest, expose_by_model_uid, report)

    product_layer = _product_layer(layers_present)
    metadata: Dict[str, Any] = {"layer": product_layer, "owner": dict(_DEFAULT_OWNER)}
    from fluid_build.forge.product_types import normalize_metadata_in_place

    normalize_metadata_in_place(metadata)  # fills the productType twin (Bronze↔SDP … Gold↔CDP)
    report.required_defaults.append("metadata.owner defaulted — set the real owning team")

    project_slug = _safe_identifier(project_name)
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": f"{product_layer.lower()}.{project_slug}",
        "name": f"Imported from dbt: {project_name}",
        "domain": "imported",
        "description": (
            f"Auto-converted from dbt manifest {manifest_path.name} "
            f"(schema v{version if version is not None else '?'}, "
            f"dbt {meta.get('dbt_version', '?')}, adapter {adapter or '?'})"
        ),
        "metadata": metadata,
        "builds": [
            {
                "id": "dbt_run",
                "pattern": "hybrid-reference",
                "engine": "dbt",
                "properties": {"model": "models/", "materializations": materializations},
                "outputs": [e["exposeId"] for e in exposes],
                "transformations": transformations,
            }
        ],
        "exposes": exposes,
    }
    if consumes:
        contract["consumes"] = consumes
    report.notes.append(
        "product boundary: ONE DataProduct per dbt project "
        "(per-folder/per-group splitting is a planned follow-up flag)"
    )
    return contract


def _select_project_nodes(
    nodes: Dict[str, Any], project_name: str, report: ImportReport
) -> Dict[str, Any]:
    """Enabled models/seeds/snapshots owned by the project package. No model cap."""
    selected: Dict[str, Any] = {}
    foreign_packages: set[str] = set()
    for uid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in ("model", "seed", "snapshot"):
            continue
        package = str(node.get("package_name") or "")
        if package and package != project_name:
            foreign_packages.add(package)
            continue
        if (node.get("config") or {}).get("enabled") is False:
            report.unsupported.append(f"model {node.get('name')} is disabled — skipped")
            continue
        selected[uid] = node
    for package in sorted(foreign_packages):
        report.notes.append(
            f"skipped models from installed package {package!r} (not project-owned)"
        )
    return selected


# ---------------------------------------------------------------------------
# Layer inference (mirrors engines/dbt/models.py::_infer_layer)
# ---------------------------------------------------------------------------


def _infer_model_layer(node: Dict[str, Any]) -> str:
    """staging | intermediate | marts, from folder path first, then name."""
    path = str(node.get("original_file_path") or node.get("path") or "").lower()
    name = str(node.get("name") or "").lower()
    haystack = f"{path}/{name}"
    if any(token in haystack for token in ("stg_", "staging", "extract", "raw")):
        return "staging"
    if any(token in haystack for token in ("int_", "intermediate", "prep")):
        return "intermediate"
    return "marts"


def _product_layer(layers_present: set[str]) -> str:
    """Most-downstream layer present wins (what the product exposes)."""
    for layer in reversed(_LAYER_ORDER):
        if layer in layers_present:
            return _LAYER_TO_MEDALLION[layer]
    return "Bronze"


# ---------------------------------------------------------------------------
# Exposes (models → output ports)
# ---------------------------------------------------------------------------


def _build_expose(
    uid: str,
    node: Dict[str, Any],
    layer: str,
    materialized: str,
    adapter: str,
    catalog: Dict[str, Any],
    tests: List[Dict[str, Any]],
    report: ImportReport,
) -> Dict[str, Any]:
    name = str(node.get("name") or uid.rsplit(".", 1)[-1])
    expose_id = _safe_identifier(name)
    kind = "view" if materialized == "view" else "table"

    columns = _build_columns(uid, node, catalog, tests, report)
    dq_rules = _build_dq_rules(name, tests, report)

    contract_block: Dict[str, Any] = {}
    if columns:
        contract_block["schema"] = columns
    else:
        contract_block["schemaPolicy"] = "discover_and_freeze"
        report.required_defaults.append(
            f"model {name}: no columns in manifest/catalog — schemaPolicy=discover_and_freeze"
        )
    if dq_rules:
        contract_block["dq"] = {"rules": dq_rules}

    labels: Dict[str, str] = {
        "dbt-unique-id": uid,
        "dbt-layer": layer,
        "dbt-materialized": materialized,
    }
    upstream_models = _upstream_model_names(node)
    if upstream_models:
        labels["dbt-depends-on"] = ",".join(upstream_models)

    expose: Dict[str, Any] = {
        "exposeId": expose_id,
        "kind": kind,
        "labels": labels,
        "binding": _build_binding(node, materialized, adapter),
        "contract": contract_block,
    }
    description = node.get("description")
    if description:
        expose["description"] = str(description)
    tags = _safe_tags(node.get("tags"))
    if tags:
        expose["tags"] = tags
    return expose


def _upstream_model_names(node: Dict[str, Any]) -> List[str]:
    """Intra-project ref() lineage: upstream model names from depends_on."""
    upstream: List[str] = []
    for dep in (node.get("depends_on") or {}).get("nodes") or []:
        if isinstance(dep, str) and dep.split(".", 1)[0] in ("model", "seed", "snapshot"):
            upstream.append(dep.rsplit(".", 1)[-1])
    return list(dict.fromkeys(upstream))


def _build_binding(node: Dict[str, Any], materialized: str, adapter: str) -> Dict[str, Any]:
    platform, fmt = _ADAPTER_BINDINGS.get(adapter, ("other", "other"))
    if adapter == "snowflake" and materialized == "view":
        fmt = "snowflake_view"
    location: Dict[str, str] = {}
    database = node.get("database")
    schema = node.get("schema")
    table = node.get("alias") or node.get("name")
    if platform == "gcp":
        if database:
            location["project"] = str(database)
        if schema:
            location["dataset"] = str(schema)
    else:
        if database:
            location["database"] = str(database)
        if schema:
            location["schema"] = str(schema)
    if table:
        location["table"] = str(table)
    return {"platform": platform, "format": fmt, "location": location}


# ---------------------------------------------------------------------------
# Columns (manifest data_type + catalog.json overlay) and PK/FK inference
# ---------------------------------------------------------------------------


def _build_columns(
    uid: str,
    node: Dict[str, Any],
    catalog: Dict[str, Any],
    tests: List[Dict[str, Any]],
    report: ImportReport,
) -> List[Dict[str, Any]]:
    model_name = str(node.get("name"))
    manifest_cols: Dict[str, Any] = node.get("columns") or {}
    catalog_types = _catalog_types(catalog, uid)

    # Documented columns first (schema.yml order), then catalog-only columns.
    ordered: List[str] = list(manifest_cols)
    for cat_name in catalog_types.get("__order__", []):
        if cat_name.lower() not in {c.lower() for c in ordered}:
            ordered.append(cat_name)

    not_null_cols = {t["column"] for t in tests if t["name"] == "not_null" and t.get("column")}
    unique_cols = {t["column"] for t in tests if t["name"] == "unique" and t.get("column")}
    pk_columns = _infer_primary_key(node, manifest_cols, not_null_cols, unique_cols)

    columns: List[Dict[str, Any]] = []
    for col_name in ordered:
        manifest_col = manifest_cols.get(col_name) or {}
        raw_type = catalog_types.get(col_name.lower()) or manifest_col.get("data_type")
        col_type, defaulted = _normalize_column_type(raw_type)
        if defaulted:
            report.required_defaults.append(
                f"column {model_name}.{col_name}: type {raw_type!r} not mappable — "
                "defaulted to string"
            )
        entry: Dict[str, Any] = {"name": col_name, "type": col_type}
        if manifest_col.get("description"):
            entry["description"] = _scrub_display_text(
                manifest_col["description"],
                field_desc=f"column {model_name}.{col_name} description",
                report=report,
            )
        if col_name in not_null_cols or _column_has_not_null_constraint(manifest_col):
            entry["required"] = True
        if col_name in pk_columns:
            entry["semanticType"] = "identifier"
        validation_rules = _column_validation_rules(col_name, tests, report, model_name)
        if validation_rules:
            entry["validationRules"] = validation_rules
        columns.append(entry)
    return columns


def _catalog_types(catalog: Dict[str, Any], uid: str) -> Dict[str, Any]:
    """{lowercased column name → type} + insertion order, from catalog.json."""
    entry = (catalog.get("nodes") or {}).get(uid) or (catalog.get("sources") or {}).get(uid) or {}
    out: Dict[str, Any] = {"__order__": []}
    for col_name, col in (entry.get("columns") or {}).items():
        if isinstance(col, dict) and col.get("type"):
            out[col_name.lower()] = str(col["type"])
            out["__order__"].append(col_name)
    return out


def _normalize_column_type(raw: Any) -> Tuple[str, bool]:
    """Map a dbt/warehouse type spelling onto the FLUID column-type surface.

    Returns ``(type, defaulted)`` — *defaulted* True when the input could not
    be mapped and ``string`` was substituted (reported by the caller).
    """
    if raw is None or not str(raw).strip():
        return "string", True
    lowered = str(raw).strip().lower()
    for alias, canonical in _TYPE_ALIASES.items():
        if lowered.startswith(alias):
            return canonical, False
    base = lowered.split("(", 1)[0].strip()
    if base in _CANONICAL_TYPES:
        return lowered, False
    return "string", True


def _column_has_not_null_constraint(manifest_col: Dict[str, Any]) -> bool:
    return any(
        isinstance(c, dict) and str(c.get("type", "")).lower() == "not_null"
        for c in manifest_col.get("constraints") or []
    )


def _infer_primary_key(
    node: Dict[str, Any],
    manifest_cols: Dict[str, Any],
    not_null_cols: set,
    unique_cols: set,
) -> set:
    """PK inference precedence (reimplements dbt's ModelNode.infer_primary_key,
    per the datacontract-cli borrow):

    1. model-level ``constraints`` with type primary_key
    2. column-level ``constraints`` with type primary_key
    3. columns with both ``unique`` AND ``not_null`` tests
    4. columns with ``unique`` tests
    """
    model_pk = {
        str(col)
        for constraint in node.get("constraints") or []
        if isinstance(constraint, dict) and str(constraint.get("type", "")).lower() == "primary_key"
        for col in constraint.get("columns") or []
    }
    if model_pk:
        return model_pk
    column_pk = {
        name
        for name, col in manifest_cols.items()
        if any(
            isinstance(c, dict) and str(c.get("type", "")).lower() == "primary_key"
            for c in (col or {}).get("constraints") or []
        )
    }
    if column_pk:
        return column_pk
    both = unique_cols & not_null_cols
    if both:
        return both
    return set(unique_cols)


def _column_validation_rules(
    col_name: str, tests: List[Dict[str, Any]], report: ImportReport, model_name: str
) -> List[Dict[str, str]]:
    """Constraint-shaped tests that have NO faithful dqRule.type (relationships,
    numeric range — intentionally outside the _test_mapping bijection) are
    recovered as column validationRules instead of being dropped."""
    rules: List[Dict[str, str]] = []
    for test in tests:
        if test.get("column") != col_name:
            continue
        if test["name"] == "relationships":
            to_model, field = test.get("to") or "?", test.get("field") or "?"
            rules.append(
                {
                    "type": "custom",
                    "constraint": f"references {to_model}.{field}",
                    "message": f"{col_name} must exist in {to_model}.{field}",
                }
            )
            report.notes.append(
                f"FK recovered from relationships test: {model_name}.{col_name} → "
                f"{to_model}.{field}"
            )
        elif test["name"] == "dbt_expectations.expect_column_values_to_be_between":
            bounds = []
            if test.get("min_value") is not None:
                bounds.append(f">= {test['min_value']}")
            if test.get("max_value") is not None:
                bounds.append(f"<= {test['max_value']}")
            if bounds:
                rules.append({"type": "range", "constraint": " and ".join(bounds)})
    return rules


# ---------------------------------------------------------------------------
# Generic tests → dq.rules[] via the SHARED reverse table
# ---------------------------------------------------------------------------


def _collect_generic_tests(
    nodes: Dict[str, Any],
    project_name: str,
    model_uids: set,
    report: ImportReport,
) -> Dict[str, List[Dict[str, Any]]]:
    """Normalize every generic test node into {model_uid: [test-descriptor]}."""
    by_model: Dict[str, List[Dict[str, Any]]] = {}
    for uid, node in nodes.items():
        if not isinstance(node, dict) or node.get("resource_type") != "test":
            continue
        package = str(node.get("package_name") or "")
        if package and package != project_name:
            continue
        test_meta = node.get("test_metadata")
        if not isinstance(test_meta, dict):
            report.unsupported.append(
                f"singular test {node.get('name')} (bespoke SQL) — re-author as a dq rule"
            )
            continue
        model_uid = _test_target_model(node, model_uids)
        if model_uid is None:
            continue
        kwargs = test_meta.get("kwargs") or {}
        namespace = test_meta.get("namespace")
        short_name = str(test_meta.get("name") or "")
        descriptor: Dict[str, Any] = {
            "name": f"{namespace}.{short_name}" if namespace else short_name,
            "column": node.get("column_name") or kwargs.get("column_name"),
            "severity": _test_severity(node),
            "values": kwargs.get("values"),
            "expression": kwargs.get("expression"),
            "field": kwargs.get("field"),
            "datepart": kwargs.get("datepart"),
            "interval": kwargs.get("interval"),
            "fluid_window": kwargs.get("_fluid_window"),
            "min_value": kwargs.get("min_value"),
            "max_value": kwargs.get("max_value"),
            "to": _parse_ref(kwargs.get("to")),
        }
        by_model.setdefault(model_uid, []).append(descriptor)
    return by_model


def _test_target_model(node: Dict[str, Any], model_uids: set) -> Optional[str]:
    attached = node.get("attached_node")
    if isinstance(attached, str) and attached in model_uids:
        return attached
    for dep in (node.get("depends_on") or {}).get("nodes") or []:
        if isinstance(dep, str) and dep in model_uids:
            return dep
    return None


def _test_severity(node: Dict[str, Any]) -> str:
    severity = str((node.get("config") or {}).get("severity") or "error").lower()
    return "warn" if severity == "warn" else "error"


def _parse_ref(raw: Any) -> Optional[str]:
    """``"ref('stg_customers')"`` (optionally jinja-wrapped) → ``stg_customers``."""
    if not isinstance(raw, str):
        return None
    match = _REF_RE.search(raw)
    return match.group(1) if match else raw.strip() or None


def _build_dq_rules(
    model_name: str, tests: List[Dict[str, Any]], report: ImportReport
) -> List[Dict[str, Any]]:
    """dbt generic tests → dq.rules[] through REVERSE_TEST_TO_RULE — the shared
    reverse table in engines/dbt/_test_mapping (NOT a 4th divergent mapper)."""
    import fluid_build.engines.dbt._test_mapping as _tm

    rules: List[Dict[str, Any]] = []
    for test in tests:
        rule_type = _tm.test_to_rule_type(test["name"])
        if rule_type is None:
            # relationships / range are recovered as column validationRules;
            # everything else is honestly unsupported.
            if test["name"] not in (
                "relationships",
                "dbt_expectations.expect_column_values_to_be_between",
            ):
                report.unsupported.append(
                    f"dbt test {test['name']} on {model_name}"
                    + (f".{test['column']}" if test.get("column") else "")
                    + " — no FLUID dq-rule mapping"
                )
            continue

        column = test.get("column")
        rule: Dict[str, Any] = {
            "id": _dq_rule_id(model_name, rule_type, column),
            "type": rule_type,
            "severity": test["severity"],
        }
        if column:
            rule["selector"] = str(column)

        if rule_type == "valid_values":
            values = test.get("values") or []
            if values:
                # Round-trips through _test_mapping.valid_values(): it parses
                # "<col> valid values: a, b, c." back out of the description.
                joined = ", ".join(str(v) for v in values)
                rule["description"] = f"{column or model_name} valid values: {joined}."
        elif rule_type == "accuracy":
            if test.get("expression"):
                rule["description"] = f"expression_is_true: {test['expression']}"
        elif rule_type == "freshness":
            selector = test.get("field") or column
            if selector:
                rule["selector"] = str(selector)
            window = _recency_window(test)
            if window:
                rule["window"] = window

        rules.append(rule)
        report.mapped_one_to_one.append(
            f"test.{test['name']} → dq.{rule_type} ({model_name}"
            + (f".{column}" if column else "")
            + ")"
        )
    return rules


def _dq_rule_id(model_name: str, rule_type: str, column: Optional[str]) -> str:
    return f"{model_name}_{rule_type}" + (f"_{column}" if column else "")


_ISO_DURATION_RE = re.compile(r"^P(?!$)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$")

_DATEPART_TO_ISO = {
    "minute": "PT{n}M",
    "hour": "PT{n}H",
    "day": "P{n}D",
    "week": "P{n}W",
    "month": "P{n}M",
    "year": "P{n}Y",
}


def _recency_window(test: Dict[str, Any]) -> Optional[str]:
    """dbt_utils.recency datepart/interval → ISO-8601 duration for dqRule.window."""
    fluid_window = test.get("fluid_window")
    if isinstance(fluid_window, str) and _ISO_DURATION_RE.match(fluid_window):
        return fluid_window  # forward mapping preserved the original verbatim
    datepart = str(test.get("datepart") or "").lower()
    interval = test.get("interval")
    template = _DATEPART_TO_ISO.get(datepart)
    if template and isinstance(interval, int) and interval > 0:
        return template.format(n=interval)
    return None


# ---------------------------------------------------------------------------
# Sources → consumes[]
# ---------------------------------------------------------------------------


def _build_consumes(
    referenced_sources: List[str],
    sources: Dict[str, Any],
    report: ImportReport,
) -> List[Dict[str, Any]]:
    consumes: List[Dict[str, Any]] = []
    seen: set = set()
    for uid in referenced_sources:
        src = sources.get(uid) or {}
        source_name = str(src.get("source_name") or "unknown")
        table_name = str(src.get("name") or uid.rsplit(".", 1)[-1])
        product_id = _safe_identifier(f"source.{source_name}")
        expose_id = _safe_identifier(table_name)
        if (product_id, expose_id) in seen:
            continue
        seen.add((product_id, expose_id))
        entry: Dict[str, Any] = {
            "productId": product_id,
            "exposeId": expose_id,
            "purpose": f"dbt source {source_name}.{table_name}",
        }
        freshness_max = _source_freshness(src)
        if freshness_max:
            entry["qosExpectations"] = {"freshnessMax": freshness_max}
        consumes.append(entry)
        report.mapped_one_to_one.append(f"source.{source_name}.{table_name} → consumes[]")
    return consumes


def _source_freshness(src: Dict[str, Any]) -> Optional[str]:
    freshness = src.get("freshness") or {}
    for key in ("error_after", "warn_after"):
        spec = freshness.get(key) or {}
        count = spec.get("count")
        period = str(spec.get("period") or "").lower()
        template = _DATEPART_TO_ISO.get(period)
        if template and isinstance(count, int) and count > 0:
            return template.format(n=count)
    return None


# ---------------------------------------------------------------------------
# Semantic layer: manifest semantic_models / metrics → exposes[].semantics
# ---------------------------------------------------------------------------

_VALID_SEMANTIC_AGGS = {
    "sum",
    "avg",
    "count",
    "count_distinct",
    "min",
    "max",
    "median",
    "percentile",
}
_VALID_ENTITY_TYPES = {"primary", "foreign", "unique", "natural"}

# Jinja delimiters. dbt renders Jinja in YAML property values (descriptions,
# meta, expr, filter, …) when the operator runs ``dbt parse`` on a generated
# project. A hostile third-party manifest carrying e.g.
# ``{{ env_var('AWS_SECRET_ACCESS_KEY') }}`` in an imported free-text field
# would therefore have that value rendered into the operator's own manifest
# artifact after the import → generate → parse round trip — an indirect
# info-disclosure chain. Every recovered free-text field routes through the
# two guards below.
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.DOTALL)
# Bare two-char Jinja delimiters (for stray-fragment cleanup after the
# span-removal loop). Single ``{`` / ``}`` are legitimate display text.
_JINJA_DELIMITER_RE = re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}")
# MetricFlow's legitimate templated object references inside metric filters.
_METRICFLOW_TEMPLATE_RE = re.compile(
    r"^\{\{\s*(?:Dimension|TimeDimension|Entity|Metric)\s*\(", re.IGNORECASE
)


def _scrub_display_text(value: Any, *, field_desc: str, report: ImportReport) -> str:
    """Strip Jinja markup from an imported display / governance string.

    Descriptions, tags, labels, and owners never legitimately carry a
    template, so the braced spans are removed outright (dbt would otherwise
    render them on parse) and the redaction is surfaced. Returns the cleaned
    text.

    Removal loops to a fixpoint: a single ``re.sub`` pass can *reform* a
    delimiter at a gap junction (e.g. ``{{%%}%set x=env_var('S')%}`` →
    ``{%set x=env_var('S')%}``), which would then execute on ``dbt parse``.
    Iterating until the string stops changing guarantees no delimiter
    survives via reformation. Bounded by ``len(text)`` — each pass removes
    at least one delimiter pair, so it always terminates.
    """
    text = str(value)
    if not _JINJA_RE.search(text):
        return text
    cleaned = text
    for _ in range(len(text) + 1):
        stripped = _JINJA_RE.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    # Remove any stray delimiter fragments left after the loop (e.g.
    # ``{{{{ x }}}}`` → dangling ``}}``). A bare closer never renders, but
    # this keeps the invariant "no Jinja delimiter survives" literally true.
    # Only the two-char delimiters are removed — single ``{`` / ``}`` are
    # legitimate display text (e.g. ``{value}``) and left intact.
    cleaned = _JINJA_DELIMITER_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    report.unsupported.append(
        f"{field_desc}: Jinja template markup stripped from imported text — "
        "display/governance fields must not carry templates (dbt renders them "
        "on parse; e.g. env-var/secret exfiltration)"
    )
    return cleaned


def _flag_sql_jinja(value: Any, *, field_desc: str, report: ImportReport) -> str:
    """Preserve an imported SQL-bearing string (expr / filter) but flag Jinja.

    expr/filter are deliberately SQL-bearing and MetricFlow filters
    legitimately template object references (``{{ Dimension('...') }}``), so
    the value is kept verbatim. MetricFlow-shaped templating is noted as
    expected; any OTHER Jinja (``env_var``, arbitrary calls) is surfaced as a
    review-before-generate risk so the operator scrutinises the regenerated
    project before running dbt. Returns the value unchanged.
    """
    text = str(value)
    spans = _JINJA_RE.findall(text)
    if not spans:
        return text
    non_metricflow = [s for s in spans if not _METRICFLOW_TEMPLATE_RE.match(s)]
    if non_metricflow:
        report.unsupported.append(
            f"{field_desc}: imported expression contains non-MetricFlow Jinja "
            f"({non_metricflow[0][:60]!r}) — REVIEW before generating; dbt "
            "renders it on parse (env-var/secret exfiltration risk)"
        )
    else:
        report.notes.append(
            f"{field_desc}: imported expression uses MetricFlow object "
            "templating (Dimension/TimeDimension/Entity/Metric) — preserved"
        )
    return text


def _attach_semantic_models(
    manifest: Dict[str, Any],
    expose_by_model_uid: Dict[str, Dict[str, Any]],
    report: ImportReport,
) -> None:
    """Map manifest ``semantic_models`` + ``metrics`` (dbt-semantic-interfaces,
    manifest v10+) into ``exposes[].semantics``.

    Round-trip closure: ``fluid generate transformation`` exports the
    semantics block to MetricFlow YAML; without this importer leg a
    brownfield dbt project lost its semantic layer on the way IN. The
    posture matches the rest of the importer — map faithfully what the
    contract schema can hold, degrade loudly (report notes) for what it
    can't (agg_params pre-0.7.6, window_groupings, cumulative/conversion
    metric types).
    """
    semantic_models = manifest.get("semantic_models") or {}
    if not isinstance(semantic_models, dict) or not semantic_models:
        return

    blocks_by_sm_name: Dict[str, Dict[str, Any]] = {}
    measure_home: Dict[str, Dict[str, Any]] = {}

    for uid, sm in semantic_models.items():
        if not isinstance(sm, dict):
            continue
        expose = _semantic_model_expose(sm, expose_by_model_uid)
        if expose is None:
            report.unsupported.append(
                f"semantic model {sm.get('name') or uid}: target model has no expose "
                "(ephemeral / foreign package?) — dropped"
            )
            continue
        block = _semantics_block_from_semantic_model(sm, report)
        expose["semantics"] = block
        blocks_by_sm_name[str(sm.get("name") or "")] = block
        for measure in block.get("measures", []):
            measure_home[measure["name"]] = block
        report.mapped_one_to_one.append(
            f"semantic_model.{sm.get('name')} → exposes[{expose['exposeId']}].semantics"
        )

    for uid, metric in (manifest.get("metrics") or {}).items():
        if not isinstance(metric, dict):
            continue
        _attach_metric(metric, uid, measure_home, blocks_by_sm_name, report)


def _semantic_model_expose(
    sm: Dict[str, Any], expose_by_model_uid: Dict[str, Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    for dep in (sm.get("depends_on") or {}).get("nodes") or []:
        if isinstance(dep, str) and dep in expose_by_model_uid:
            return expose_by_model_uid[dep]
    # Fallback: parse the ref('model') string and match by node name.
    model_name = _parse_ref(sm.get("model"))
    if model_name:
        for uid, expose in expose_by_model_uid.items():
            if uid.rsplit(".", 1)[-1] == model_name:
                return expose
    return None


def _semantics_block_from_semantic_model(
    sm: Dict[str, Any], report: ImportReport
) -> Dict[str, Any]:
    sm_name = str(sm.get("name") or "semantic_model")
    block: Dict[str, Any] = {"name": sm_name}
    if sm.get("description"):
        block["description"] = _scrub_display_text(
            sm["description"], field_desc=f"semantic model {sm_name} description", report=report
        )

    default_time = ((sm.get("defaults") or {}).get("agg_time_dimension") or "").strip()
    if default_time:
        block["defaultAggTimeDimension"] = default_time

    # Round-trip recovery of the governance surface the MetricFlow bridge
    # exports as namespaced config.meta keys (contract → dbt → contract).
    sm_meta = (sm.get("config") or {}).get("meta") or {}
    fluid_tags = sm_meta.get("fluid_tags")
    if isinstance(fluid_tags, list) and fluid_tags:
        # Drop tags that scrub to empty (a fully-templated tag) — an empty
        # tag fails the schema tag pattern.
        clean_tags = [
            scrubbed
            for t in fluid_tags
            if (
                scrubbed := _scrub_display_text(
                    t, field_desc=f"semantic model {sm_name} tag", report=report
                )
            )
        ]
        if clean_tags:
            block["tags"] = clean_tags
    fluid_labels = sm_meta.get("fluid_labels")
    if isinstance(fluid_labels, dict) and fluid_labels:
        clean_labels = {}
        for k, v in fluid_labels.items():
            key = _scrub_display_text(
                k, field_desc=f"semantic model {sm_name} label key", report=report
            )
            val = _scrub_display_text(
                v, field_desc=f"semantic model {sm_name} label value", report=report
            )
            if key and val:  # both must survive the scrub to stay a valid label
                clean_labels[key] = val
        if clean_labels:
            block["labels"] = clean_labels

    entities: List[Dict[str, Any]] = []
    for raw in sm.get("entities") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        entity_type = str(raw.get("type") or "").lower()
        if entity_type not in _VALID_ENTITY_TYPES:
            report.unsupported.append(
                f"semantic model {sm_name}: entity {raw.get('name')!r} has "
                f"unsupported type {raw.get('type')!r} — dropped"
            )
            continue
        entity: Dict[str, Any] = {"name": str(raw["name"]), "type": entity_type}
        if raw.get("expr"):
            entity["expr"] = _flag_sql_jinja(
                raw["expr"], field_desc=f"entity {raw['name']} expr", report=report
            )
        if raw.get("description"):
            entity["description"] = _scrub_display_text(
                raw["description"], field_desc=f"entity {raw['name']} description", report=report
            )
        entities.append(entity)
    if entities:
        block["entities"] = entities

    dimensions: List[Dict[str, Any]] = []
    for raw in sm.get("dimensions") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        dim_type = str(raw.get("type") or "categorical").lower()
        dimension: Dict[str, Any] = {"name": str(raw["name"])}
        if raw.get("expr"):
            dimension["expr"] = _flag_sql_jinja(
                raw["expr"], field_desc=f"dimension {raw['name']} expr", report=report
            )
        if raw.get("description"):
            dimension["description"] = _scrub_display_text(
                raw["description"], field_desc=f"dimension {raw['name']} description", report=report
            )
        if dim_type == "time":
            dimension["type"] = "time"
            granularity = str(
                ((raw.get("type_params") or {}).get("time_granularity")) or ""
            ).strip()
            if granularity:
                from fluid_build.forge_datamodel import time_grains as _time_grains

                canonical = _time_grains.normalize_time_grain(granularity)
                if canonical is not None:
                    dimension["typeParams"] = {"timeGranularity": canonical}
                else:
                    report.unsupported.append(
                        f"semantic model {sm_name}: dimension {raw['name']!r} "
                        f"granularity {granularity!r} has no contract equivalent — "
                        "granularity omitted"
                    )
        else:
            dimension["type"] = "categorical"
        dimensions.append(dimension)
    if dimensions:
        block["dimensions"] = dimensions

    measures: List[Dict[str, Any]] = []
    for raw in sm.get("measures") or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        agg = str(raw.get("agg") or "").lower()
        if agg not in _VALID_SEMANTIC_AGGS:
            report.unsupported.append(
                f"semantic model {sm_name}: measure {raw.get('name')!r} has "
                f"unsupported agg {raw.get('agg')!r} — dropped"
            )
            continue
        measure: Dict[str, Any] = {"name": str(raw["name"]), "agg": agg}
        if raw.get("expr"):
            measure["expr"] = _flag_sql_jinja(
                raw["expr"], field_desc=f"measure {raw['name']} expr", report=report
            )
        if raw.get("description"):
            measure["description"] = _scrub_display_text(
                raw["description"], field_desc=f"measure {raw['name']} description", report=report
            )
        if raw.get("agg_time_dimension"):
            measure["aggTimeDimension"] = str(raw["agg_time_dimension"])
        if raw.get("create_metric"):
            measure["createMetric"] = True
        nad = raw.get("non_additive_dimension")
        if isinstance(nad, dict) and nad.get("name"):
            entry: Dict[str, Any] = {"name": str(nad["name"])}
            window_choice = str(nad.get("window_choice") or "").lower()
            if window_choice in ("min", "max"):
                entry["windowChoice"] = window_choice
            measure["nonAdditiveDimension"] = entry
            if nad.get("window_groupings"):
                report.unsupported.append(
                    f"semantic model {sm_name}: measure {raw['name']!r} "
                    "non_additive_dimension.window_groupings has no contract "
                    "slot yet — dropped"
                )
        if raw.get("agg_params"):
            # measures[].aggParams lands in the 0.7.6 preview schema; the
            # importer emits the GA fluidVersion, so record the loss
            # instead of emitting a key the schema rejects.
            report.unsupported.append(
                f"semantic model {sm_name}: measure {raw['name']!r} agg_params "
                "requires fluidVersion >= 0.7.6 — dropped (re-import after "
                "upgrading the contract version to keep percentile parameters)"
            )
        measures.append(measure)
    if measures:
        block["measures"] = measures
    return block


def _attach_metric(
    metric: Dict[str, Any],
    uid: str,
    measure_home: Dict[str, Dict[str, Any]],
    blocks_by_sm_name: Dict[str, Dict[str, Any]],
    report: ImportReport,
) -> None:
    name = str(metric.get("name") or uid.rsplit(".", 1)[-1])
    metric_type = str(metric.get("type") or "simple").lower()
    type_params = metric.get("type_params") or {}

    if metric_type not in ("simple", "ratio", "derived"):
        report.unsupported.append(
            f"metric {name}: type {metric_type!r} has no contract equivalent "
            "(cumulative/conversion land with the Tier-1 schema work) — dropped"
        )
        return

    entry: Dict[str, Any] = {"name": name, "type": metric_type}
    if metric.get("description"):
        entry["description"] = _scrub_display_text(
            metric["description"], field_desc=f"metric {name} description", report=report
        )
    metric_owner = ((metric.get("config") or {}).get("meta") or {}).get("owner")
    if metric_owner:
        entry["owner"] = _scrub_display_text(
            metric_owner, field_desc=f"metric {name} owner", report=report
        )
    filter_sql = _metric_filter_sql(metric)
    if filter_sql:
        entry["filter"] = _flag_sql_jinja(
            filter_sql, field_desc=f"metric {name} filter", report=report
        )

    home: Optional[Dict[str, Any]] = None
    if metric_type == "simple":
        measure_name = str(((type_params.get("measure") or {}).get("name")) or "")
        if not measure_name:
            report.unsupported.append(f"metric {name}: no measure reference — dropped")
            return
        entry["measure"] = measure_name
        home = measure_home.get(measure_name)
    elif metric_type == "ratio":
        numerator = str(((type_params.get("numerator") or {}).get("name")) or "")
        denominator = str(((type_params.get("denominator") or {}).get("name")) or "")
        if not numerator or not denominator:
            report.unsupported.append(f"metric {name}: incomplete ratio inputs — dropped")
            return
        entry["numerator"] = numerator
        entry["denominator"] = denominator
        home = measure_home.get(numerator) or measure_home.get(denominator)
    else:  # derived
        expr = str(type_params.get("expr") or "")
        input_metrics = [
            str(m.get("name"))
            for m in type_params.get("metrics") or []
            if isinstance(m, dict) and m.get("name")
        ]
        if not expr or not input_metrics:
            report.unsupported.append(f"metric {name}: incomplete derived inputs — dropped")
            return
        entry["expr"] = _flag_sql_jinja(expr, field_desc=f"metric {name} expr", report=report)
        entry["inputMetrics"] = input_metrics
        for existing in blocks_by_sm_name.values():
            if any(m.get("name") in input_metrics for m in existing.get("metrics", [])):
                home = existing
                break

    if home is None:
        report.unsupported.append(
            f"metric {name}: could not resolve a home semantic model — dropped"
        )
        return
    home.setdefault("metrics", []).append(entry)
    report.mapped_one_to_one.append(f"metric.{name} → semantics.metrics")


def _metric_filter_sql(metric: Dict[str, Any]) -> Optional[str]:
    """dbt metric filter → contract filter string. Multiple where-filters
    AND together; Jinja-templated filters pass through verbatim (the
    governed MCP query path fails closed on them; the MetricFlow export
    round-trips them untouched)."""
    filter_obj = metric.get("filter")
    if isinstance(filter_obj, str):
        return filter_obj.strip() or None
    if isinstance(filter_obj, dict):
        clauses = [
            str(f.get("where_sql_template") or "").strip()
            for f in filter_obj.get("where_filters") or []
            if isinstance(f, dict)
        ]
        clauses = [c for c in clauses if c]
        if clauses:
            return " AND ".join(f"({c})" for c in clauses) if len(clauses) > 1 else clauses[0]
    return None


# ---------------------------------------------------------------------------
# Identifier / tag sanitisation
# ---------------------------------------------------------------------------


def _safe_identifier(raw: str) -> str:
    """Keep already-valid FLUID identifiers verbatim; slugify the rest."""
    if _IDENTIFIER_RE.match(raw or ""):
        return raw
    from fluid_build.util.contract import slugify_identifier

    return slugify_identifier(raw or "", fallback="imported")


_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


def _safe_tags(raw: Any) -> List[str]:
    """dbt tags → FLUID tag grammar (lowercase alnum + hyphens), dropping empties."""
    tags: List[str] = []
    for tag in raw or []:
        cleaned = re.sub(r"[^a-z0-9]+", "-", str(tag).lower()).strip("-")
        if cleaned and _TAG_RE.match(cleaned) and cleaned not in tags:
            tags.append(cleaned)
    return tags
