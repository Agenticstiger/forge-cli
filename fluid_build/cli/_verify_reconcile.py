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

"""Contract <-> dbt schema reconciliation for ``fluid verify``.

Static, warehouse-free cross-check that a data product's FLUID contract
(``exposes[].contract.schema``) agrees with the columns its dbt project
declares (``models/**/schema.yml``). It surfaces DRIFT — a column the
contract promises that dbt doesn't model, a dbt column the contract never
exposes, or a declared type that disagrees — before the pipeline publishes
a lineage that lies about its own shape.

Design provenance (see ``AGENTS.md`` / commit message):

- The drift vocabulary mirrors dbt's own model-contract mismatch report
  (``column_name`` / ``contract_type`` vs ``definition_type`` /
  ``mismatch_reason``). See docs.getdbt.com/reference/resource-configs/contract.
- Type comparison is deliberately conservative, following datacontract-cli's
  "skip the type check when the type is ambiguous" rule: types are collapsed
  into coarse families and a mismatch is only reported when both sides
  resolve to *distinct, known* families. That avoids false-flagging
  ``NUMBER(38,0)`` vs ``integer`` across adapters.
- ``check-model-columns`` from dbt-checkpoint inspired the bidirectional
  column reconciliation (contract->dbt and dbt->contract).

The reconcile is a pure read of already-authored files; it never connects to
a warehouse and never runs dbt. Published-lineage reconciliation (the third
leg — contract <-> dbt <-> live catalog lineage) needs a running catalog and
is intentionally out of scope here; see the PR body for the scoped plan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

LOG = logging.getLogger("fluid.cli.verify.reconcile")

# ---------------------------------------------------------------------------
# Type families
# ---------------------------------------------------------------------------
#
# Coarse families let us compare a FLUID/SQL contract type against a dbt
# ``data_type`` without a per-adapter type matrix. INTEGER / FLOAT / DECIMAL
# are deliberately merged into a single NUMERIC family: cross-adapter, an
# ``integer`` contract column is routinely materialized as ``NUMBER(38,0)``
# (Snowflake) or ``numeric`` (Postgres), and flagging that as drift would be
# noise. A genuinely wrong shape (number-vs-text, bool-vs-timestamp) still
# trips. Anything not listed resolves to ``UNKNOWN`` and is skipped.
_TYPE_FAMILIES: Dict[str, str] = {}


def _register_family(family: str, *tokens: str) -> None:
    for tok in tokens:
        _TYPE_FAMILIES[tok] = family


_register_family(
    "TEXT",
    "string",
    "text",
    "varchar",
    "varchar2",
    "nvarchar",
    "char",
    "nchar",
    "character",
    "clob",
    "str",
    "uuid",
)
_register_family(
    "NUMERIC",
    "int",
    "integer",
    "int2",
    "int4",
    "int8",
    "int16",
    "int32",
    "int64",
    "tinyint",
    "smallint",
    "mediumint",
    "bigint",
    "long",
    "longint",
    "serial",
    "bigserial",
    "float",
    "float4",
    "float8",
    "float32",
    "float64",
    "double",
    "double precision",
    "real",
    "decimal",
    "dec",
    "numeric",
    "number",
    "bignumeric",
    "money",
)
_register_family("BOOLEAN", "boolean", "bool", "bit")
_register_family("DATE", "date")
_register_family("TIME", "time")
_register_family(
    "TIMESTAMP",
    "datetime",
    "datetime2",
    "smalldatetime",
    "timestamp",
    "timestamptz",
    "timestamp_tz",
    "timestamp_ntz",
    "timestamp_ltz",
    "timestampntz",
    "timestamp without time zone",
    "timestamp with time zone",
    "timestamp with local time zone",
)
_register_family("BINARY", "binary", "varbinary", "bytes", "blob")
_register_family(
    "STRUCTURED",
    "variant",
    "json",
    "jsonb",
    "object",
    "array",
    "struct",
    "map",
    "geography",
    "geometry",
)


def normalize_type(value: Optional[str]) -> str:
    """Collapse a SQL/FLUID type name into a coarse family, or ``UNKNOWN``.

    Case-insensitive; parameter suffixes (``varchar(255)``, ``decimal(18,4)``)
    are stripped before lookup.
    """
    if not value or not isinstance(value, str):
        return "UNKNOWN"
    base = value.strip().lower().split("(", 1)[0].strip()
    return _TYPE_FAMILIES.get(base, "UNKNOWN")


# ---------------------------------------------------------------------------
# Drift model
# ---------------------------------------------------------------------------

_COLUMN_REASONS = {"missing_in_dbt", "missing_in_contract", "type_mismatch"}
_MODEL_REASONS = {"model_missing_in_dbt", "model_missing_in_contract"}


@dataclass
class ColumnDrift:
    """A single column-level disagreement between the contract and dbt."""

    expose_id: str
    model: str
    column: str
    reason: str
    contract_type: Optional[str] = None
    dbt_type: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expose_id": self.expose_id,
            "model": self.model,
            "column": self.column,
            "reason": self.reason,
            "contract_type": self.contract_type,
            "dbt_type": self.dbt_type,
        }

    def human(self) -> str:
        if self.reason == "missing_in_dbt":
            return (
                f"{self.model}.{self.column}: declared in contract "
                f"(type={self.contract_type or '?'}) but not modelled in dbt"
            )
        if self.reason == "missing_in_contract":
            return (
                f"{self.model}.{self.column}: present in dbt "
                f"(type={self.dbt_type or '?'}) but not declared in the contract"
            )
        return (
            f"{self.model}.{self.column}: type mismatch — contract={self.contract_type} "
            f"vs dbt={self.dbt_type}"
        )


@dataclass
class ModelDrift:
    """A model-level disagreement (a model present on one side only)."""

    model: str
    reason: str
    expose_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"model": self.model, "reason": self.reason, "expose_id": self.expose_id}

    def human(self) -> str:
        if self.reason == "model_missing_in_dbt":
            return f"{self.model}: expose has no matching dbt model (schema.yml)"
        return f"{self.model}: public dbt model has no matching expose in the contract"


@dataclass
class ReconcileReport:
    """Aggregated reconcile findings across every dbt build in a contract."""

    column_drifts: List[ColumnDrift] = field(default_factory=list)
    model_drifts: List[ModelDrift] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    checked_builds: int = 0
    checked_models: int = 0

    @property
    def has_drift(self) -> bool:
        return bool(self.column_drifts or self.model_drifts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "has_drift": self.has_drift,
            "checked_builds": self.checked_builds,
            "checked_models": self.checked_models,
            "column_drifts": [d.to_dict() for d in self.column_drifts],
            "model_drifts": [d.to_dict() for d in self.model_drifts],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# dbt project reading
# ---------------------------------------------------------------------------


def load_dbt_schema_models(
    project_dir: Path, *, logger: logging.Logger = LOG
) -> Dict[str, Dict[str, Any]]:
    """Read ``models/**/*.yml`` and return the declared models + columns.

    Returns ``{model_name: {"columns": {lower_name: {"name", "data_type"}},
    "access": <str|None>, "source": <relative path>}}``.

    Only static YAML is read — no ``dbt parse`` and no warehouse. A model's
    documented columns are its dbt contract surface; that is exactly what we
    reconcile the FLUID contract against. Malformed YAML files are logged and
    skipped rather than aborting the whole reconcile.
    """
    models: Dict[str, Dict[str, Any]] = {}
    models_dir = project_dir / "models"
    if not models_dir.is_dir():
        return models

    project_resolved = project_dir.resolve()
    yml_files = sorted(
        [*models_dir.rglob("*.yml"), *models_dir.rglob("*.yaml")],
        key=lambda p: str(p),
    )
    for yml in yml_files:
        # Symlink-escape guard: never read a file that resolves outside the
        # (already workspace-confined) dbt project directory.
        try:
            resolved = yml.resolve()
            resolved.relative_to(project_resolved)
        except (ValueError, OSError):
            logger.warning("reconcile: skipping schema file outside project: %s", yml)
            continue

        try:
            data = yaml.safe_load(yml.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("reconcile: could not parse %s: %s", yml.name, exc)
            continue
        if not isinstance(data, dict):
            continue

        for model in data.get("models") or []:
            if not isinstance(model, dict):
                continue
            name = model.get("name")
            if not name:
                continue
            columns: Dict[str, Dict[str, Any]] = {}
            for col in model.get("columns") or []:
                if not isinstance(col, dict):
                    continue
                col_name = col.get("name")
                if not col_name:
                    continue
                columns[str(col_name).lower()] = {
                    "name": str(col_name),
                    # dbt contracts use ``data_type``; tolerate a bare ``type``.
                    "data_type": col.get("data_type") or col.get("type"),
                }
            try:
                source = str(yml.relative_to(project_dir))
            except ValueError:  # pragma: no cover - defensive
                source = yml.name
            models[str(name)] = {
                "columns": columns,
                "access": model.get("access"),
                "source": source,
            }
    return models


# ---------------------------------------------------------------------------
# Contract reading helpers
# ---------------------------------------------------------------------------


def _expose_id(expose: Dict[str, Any]) -> Optional[str]:
    return expose.get("exposeId") or expose.get("id")


def _expose_schema_columns(expose: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the contract column dicts for an expose (``[{name, type, ...}]``).

    Accepts both the canonical ``exposes[].contract.schema`` location and the
    legacy top-level ``exposes[].schema``; each may be a bare list or a
    ``{"fields": [...]}`` wrapper. Mirrors the lookup ``verify.run`` already
    uses for its warehouse checks.
    """
    schema = expose.get("schema")
    if schema is None:
        schema = (expose.get("contract") or {}).get("schema")
    if isinstance(schema, dict):
        fields = schema.get("fields", [])
    elif isinstance(schema, list):
        fields = schema
    else:
        fields = []
    return [f for f in fields if isinstance(f, dict) and f.get("name")]


def _is_dbt_build(build: Dict[str, Any]) -> bool:
    engine = str(build.get("engine") or "dbt").strip().lower()
    return engine == "dbt" or engine.startswith("dbt-")


def _lookup_model(models: Dict[str, Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """Exact match first, then case-insensitive fallback (dbt lower-cases files)."""
    if name in models:
        return models[name]
    lowered = name.lower()
    for mname, model in models.items():
        if mname.lower() == lowered:
            return model
    return None


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def reconcile_contract_dbt(
    contract: Dict[str, Any],
    contract_path: Any,
    *,
    logger: logging.Logger = LOG,
) -> ReconcileReport:
    """Cross-check the FLUID contract against each dbt build's schema.yml.

    For every ``builds[]`` entry with a dbt engine we resolve its local dbt
    project (workspace-confined, via ``resolve_dbt_project_path``), read the
    declared models/columns, and reconcile them against the exposes the build
    produces (``build.outputs``, or every expose when ``outputs`` is unset).

    A build whose dbt project is not present locally (e.g. a reference-only
    contract whose materialization is owned by an external repo) is recorded
    as a note and skipped — it is not treated as drift.
    """
    # Imported lazily so this stays off the ``fluid --help`` cold path and the
    # module keeps a light import surface.
    from fluid_build.build_runners.dbt.runner import resolve_dbt_project_path

    contract_path = Path(contract_path)
    report = ReconcileReport()

    exposes_raw = contract.get("exposes") or []
    exposes_by_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(exposes_raw, list):
        for expose in exposes_raw:
            if isinstance(expose, dict):
                eid = _expose_id(expose)
                if eid:
                    exposes_by_id[eid] = expose
    elif isinstance(exposes_raw, dict):
        for eid, expose in exposes_raw.items():
            if isinstance(expose, dict):
                exposes_by_id[eid] = expose

    builds = contract.get("builds") or []
    dbt_builds = [b for b in builds if isinstance(b, dict) and _is_dbt_build(b)]
    if not dbt_builds:
        report.notes.append("no dbt builds in contract; nothing to reconcile")
        return report

    all_expose_ids_lower = {eid.lower() for eid in exposes_by_id}

    for build in dbt_builds:
        build_id = str(build.get("id", "unknown"))
        try:
            project_dir = resolve_dbt_project_path(contract_path, build)
        except Exception as exc:  # noqa: BLE001 - reconcile must not crash verify
            logger.warning("reconcile: build %s project resolution failed: %s", build_id, exc)
            project_dir = None

        if project_dir is None:
            report.notes.append(
                f"build '{build_id}': no local dbt project found "
                f"(repository={build.get('repository', './')!r}); skipped"
            )
            continue

        report.checked_builds += 1
        models = load_dbt_schema_models(project_dir, logger=logger)
        report.checked_models += len(models)

        outputs = build.get("outputs")
        target_ids = (
            [str(o) for o in outputs]
            if isinstance(outputs, list) and outputs
            else list(exposes_by_id.keys())
        )

        for eid in target_ids:
            expose = exposes_by_id.get(eid)
            if expose is None:
                # Build references an expose that isn't declared — a contract
                # authoring issue the schema validator owns, not a dbt drift.
                continue
            _reconcile_one(eid, expose, models, report)

        # Public dbt models with no matching expose anywhere in the contract.
        for mname, model in models.items():
            if model.get("access") == "public" and mname.lower() not in all_expose_ids_lower:
                report.model_drifts.append(
                    ModelDrift(model=mname, reason="model_missing_in_contract")
                )

    return report


def _reconcile_one(
    expose_id: str,
    expose: Dict[str, Any],
    models: Dict[str, Dict[str, Any]],
    report: ReconcileReport,
) -> None:
    """Reconcile a single expose's contract columns against its dbt model."""
    contract_cols = _expose_schema_columns(expose)
    model = _lookup_model(models, expose_id)
    if model is None:
        report.model_drifts.append(
            ModelDrift(model=expose_id, reason="model_missing_in_dbt", expose_id=expose_id)
        )
        return

    dbt_cols: Dict[str, Dict[str, Any]] = model["columns"]
    contract_by_lower = {str(c["name"]).lower(): c for c in contract_cols if c.get("name")}

    # Contract column missing from dbt.
    for lname, col in contract_by_lower.items():
        if lname not in dbt_cols:
            report.column_drifts.append(
                ColumnDrift(
                    expose_id=expose_id,
                    model=expose_id,
                    column=str(col["name"]),
                    reason="missing_in_dbt",
                    contract_type=col.get("type"),
                )
            )

    # dbt column not declared in the contract.
    for lname, dbt_col in dbt_cols.items():
        if lname not in contract_by_lower:
            report.column_drifts.append(
                ColumnDrift(
                    expose_id=expose_id,
                    model=expose_id,
                    column=str(dbt_col["name"]),
                    reason="missing_in_contract",
                    dbt_type=dbt_col.get("data_type"),
                )
            )

    # Type disagreement on shared columns (conservative family comparison).
    for lname in set(contract_by_lower) & set(dbt_cols):
        contract_type = contract_by_lower[lname].get("type")
        dbt_type = dbt_cols[lname].get("data_type")
        if not contract_type or not dbt_type:
            continue
        cf, df = normalize_type(contract_type), normalize_type(dbt_type)
        if cf == "UNKNOWN" or df == "UNKNOWN":
            continue
        if cf != df:
            report.column_drifts.append(
                ColumnDrift(
                    expose_id=expose_id,
                    model=expose_id,
                    column=str(contract_by_lower[lname]["name"]),
                    reason="type_mismatch",
                    contract_type=contract_type,
                    dbt_type=dbt_type,
                )
            )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(report: ReconcileReport, *, show_diffs: bool = False) -> None:
    """Print the reconcile section to the console (best-effort, never raises)."""
    from fluid_build.cli.console import cprint, success, warning

    cprint("\n" + "=" * 80)
    cprint("🔗 Contract ↔ dbt Reconciliation")
    cprint("=" * 80)

    for note in report.notes:
        cprint(f"   ℹ️  {note}")

    if report.checked_builds == 0 and not report.has_drift:
        cprint("   (no local dbt project to reconcile)")
        return

    cprint(
        f"   Reconciled {report.checked_builds} dbt build(s), " f"{report.checked_models} model(s)"
    )

    if not report.has_drift:
        success("   ✅ Contract and dbt agree — no schema drift")
        return

    if report.model_drifts:
        warning(f"   ⚠️  {len(report.model_drifts)} model-level drift(s):")
        for md in report.model_drifts:
            cprint(f"      • {md.human()}")

    if report.column_drifts:
        warning(f"   ⚠️  {len(report.column_drifts)} column-level drift(s):")
        # Always show the drift lines — this is the whole point of the check.
        for cd in report.column_drifts:
            cprint(f"      • {cd.human()}")

    if not show_diffs:
        cprint("   💡 Re-run with --show-diffs for remediation detail")
