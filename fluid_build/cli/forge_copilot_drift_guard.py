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

"""Semantic-drift guard for the copilot authoring / refinement loop.

When the LLM authors (or ``--refine``s) a data contract it can silently drift
away from its own ground truth — renaming a column, dropping one, or changing a
type — so the published product no longer matches the SOURCE it claims to read
or the PRIOR contract version the user asked to evolve. This module detects that
drift *before* the write and feeds corrective feedback back into the existing
self-healing repair loop (``forge_copilot_runtime.generate_copilot_artifacts``)
so the model corrects itself, exactly like the schema-validation and join-key
self-healing paths.

Baselines (either / both):

* **PRIOR contract** — the ``--refine`` seed (``context['seed_contract_override']``).
  This is the strongest baseline: the user asked to *evolve* a contract, so a
  dropped or renamed column is a breaking change they did not request. All of
  dropped / renamed / type-changed are blocking here (mirrors the
  ``fluid forge --refine`` diff, where a removed column reads as breaking).
* **SOURCE schema** — the columns discovered from local sample data
  (``discovery_report.sample_files[*].columns``). A data product legitimately
  *projects* its source (not every source column must survive), so a plain drop
  is NOT drift — only a **rename** of a carried-through column or a silent
  **type change** on a shared column is.

Design provenance — the drift vocabulary intentionally mirrors
``cli/_verify_reconcile.py`` (``ReconcileReport`` + ``ColumnDrift.reason``:
``missing_in_*`` / ``type_mismatch``) so drift reads the same across
``fluid verify`` and forge authoring. The coarse type-family collapse below is
adapted (not imported) from that module to keep the two layers decoupled while
staying symmetric — ``integer`` vs ``NUMBER(38,0)`` across adapters must never
false-flag. datacontract-cli's "skip the type check when the type is ambiguous"
rule is applied identically: a type change is reported only when *both* sides
resolve to distinct, known families.

Scope — this is the **structural** slice (name / type set fidelity). Detecting a
*meaning shift* on a column that kept its name and type (e.g. ``status`` silently
repurposed from order-state to account-state) needs an LLM judge and is a
scoped follow-up; see the PR body.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BASELINE_SOURCE",
    "BASELINE_PRIOR",
    "SchemaDrift",
    "SemanticDriftReport",
    "detect_schema_drift",
    "detect_authoring_drift",
    "drift_guard_enabled",
    "extract_contract_columns",
    "extract_source_columns",
]

#: Human-readable baseline labels — also embedded verbatim in the corrective
#: feedback so the LLM knows what it is being reconciled against.
BASELINE_SOURCE = "source schema"
BASELINE_PRIOR = "prior contract"

#: Similarity floor for treating a dropped baseline column + an added authored
#: column as a *rename* rather than an unrelated drop + add. Deliberately
#: conservative so a genuinely new output column isn't mistaken for a rename.
_RENAME_SIMILARITY_FLOOR = 0.72

#: Suffixes stripped when comparing column "stems" so ``customer_id`` and
#: ``customer_key`` are recognised as the same logical entity renamed.
_STEM_SUFFIXES = ("_id", "_key", "_pk", "_fk", "_code", "_num", "_number", "_ts")


# ---------------------------------------------------------------------------
# Coarse type families (adapted from cli/_verify_reconcile.py — see module docstring)
# ---------------------------------------------------------------------------

_TYPE_FAMILIES: Dict[str, str] = {}


def _register_family(family: str, *tokens: str) -> None:
    for tok in tokens:
        _TYPE_FAMILIES[tok] = family


_register_family(
    "TEXT",
    "string",
    "str",
    "text",
    "varchar",
    "varchar2",
    "nvarchar",
    "char",
    "nchar",
    "character",
    "clob",
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
    "record",
    "geography",
    "geometry",
)


def _type_family(value: Optional[str]) -> str:
    """Collapse a SQL/FLUID/inferred type name into a coarse family or ``UNKNOWN``.

    Case-insensitive; parameter suffixes (``varchar(255)``, ``decimal(18,4)``)
    are stripped before lookup.
    """
    if not value or not isinstance(value, str):
        return "UNKNOWN"
    base = value.strip().lower().split("(", 1)[0].strip()
    return _TYPE_FAMILIES.get(base, "UNKNOWN")


# ---------------------------------------------------------------------------
# Drift model (mirrors _verify_reconcile.ColumnDrift / ReconcileReport shape)
# ---------------------------------------------------------------------------


@dataclass
class SchemaDrift:
    """A single blocking column-level drift between authored contract + baseline."""

    column: str
    reason: str  # dropped_column | renamed_column | type_changed
    baseline_kind: str
    baseline_type: Optional[str] = None
    authored_type: Optional[str] = None
    renamed_to: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "column": self.column,
            "reason": self.reason,
            "baseline_kind": self.baseline_kind,
            "baseline_type": self.baseline_type,
            "authored_type": self.authored_type,
            "renamed_to": self.renamed_to,
        }

    def human(self) -> str:
        if self.reason == "dropped_column":
            return (
                f"column '{self.column}' (type={self.baseline_type or '?'}) is in the "
                f"{self.baseline_kind} but was dropped from the authored contract"
            )
        if self.reason == "renamed_column":
            return (
                f"column '{self.column}' from the {self.baseline_kind} was renamed to "
                f"'{self.renamed_to}' in the authored contract"
            )
        return (
            f"column '{self.column}' changed type: {self.baseline_kind}="
            f"{self.baseline_type} vs authored={self.authored_type}"
        )


@dataclass
class SemanticDriftReport:
    """Aggregated drift findings for one authored contract vs one baseline."""

    baseline_kind: str
    drifts: List[SchemaDrift] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def has_drift(self) -> bool:
        """True when at least one *blocking* drift was found."""
        return bool(self.drifts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "baseline_kind": self.baseline_kind,
            "has_drift": self.has_drift,
            "drifts": [d.to_dict() for d in self.drifts],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Column extraction
# ---------------------------------------------------------------------------


def _norm_columns(raw: Mapping[str, Any]) -> Dict[str, Dict[str, Optional[str]]]:
    """Return ``{lower_name: {"name", "type"}}`` from a ``{name: type}`` mapping."""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for name, type_ in raw.items():
        if not name:
            continue
        out[str(name).lower()] = {"name": str(name), "type": str(type_) if type_ else None}
    return out


def _expose_schema_fields(expose: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Column dicts for an expose — canonical ``contract.schema`` or legacy ``schema``.

    Each location may be a bare list or a ``{"fields": [...]}`` wrapper (mirrors
    ``_verify_reconcile._expose_schema_columns``).
    """
    schema = expose.get("schema")
    if schema is None:
        schema = (expose.get("contract") or {}).get("schema")
    if isinstance(schema, Mapping):
        fields = schema.get("fields", [])
    elif isinstance(schema, list):
        fields = schema
    else:
        fields = []
    return [f for f in fields if isinstance(f, Mapping) and f.get("name")]


def extract_contract_columns(contract: Mapping[str, Any]) -> Dict[str, Dict[str, Optional[str]]]:
    """Flatten every expose's schema into ``{lower_name: {"name", "type"}}``.

    A flat union across exposes is deliberate for this first slice: it flags a
    column that vanished (or was retyped) *anywhere* in the product without
    false-flagging a column that merely moved between exposes. Per-expose
    precision is a scoped follow-up.
    """
    if not isinstance(contract, Mapping):
        return {}
    exposes = contract.get("exposes")
    expose_iter: List[Mapping[str, Any]] = []
    if isinstance(exposes, list):
        expose_iter = [e for e in exposes if isinstance(e, Mapping)]
    elif isinstance(exposes, Mapping):
        expose_iter = [e for e in exposes.values() if isinstance(e, Mapping)]

    out: Dict[str, Dict[str, Optional[str]]] = {}
    for expose in expose_iter:
        for field_def in _expose_schema_fields(expose):
            name = field_def.get("name")
            if not name:
                continue
            lname = str(name).lower()
            # First writer wins so the earliest declaration's type is the anchor.
            out.setdefault(
                lname,
                {"name": str(name), "type": _field_type(field_def)},
            )
    return out


def _field_type(field_def: Mapping[str, Any]) -> Optional[str]:
    value = field_def.get("type") or field_def.get("logicalType") or field_def.get("physicalType")
    return str(value) if value else None


def extract_source_columns(discovery_report: Any) -> Dict[str, Dict[str, Optional[str]]]:
    """Flatten discovered sample-file columns into ``{lower_name: {"name","type"}}``.

    ``discovery_report.sample_files[*]['columns']`` is a ``{name: inferred_type}``
    mapping (see ``forge_copilot_schema_inference.summarize_sample_file``). The
    union across sample files is the source surface the product reads from.
    """
    sample_files = getattr(discovery_report, "sample_files", None) or []
    merged: Dict[str, Any] = {}
    for sample in sample_files:
        if not isinstance(sample, Mapping):
            continue
        for name, type_ in (sample.get("columns") or {}).items():
            if name and str(name).lower() not in merged:
                merged[str(name).lower()] = (name, type_)
    return _norm_columns({orig: t for orig, t in merged.values()})


# ---------------------------------------------------------------------------
# Rename detection
# ---------------------------------------------------------------------------


def _stem(name: str) -> str:
    low = name.lower().replace("-", "_")
    for suffix in _STEM_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            return low[: -len(suffix)]
    return low


def _best_rename_candidate(name: str, candidates: Sequence[str]) -> Optional[str]:
    """Return the most likely rename target for *name* among *candidates*.

    A shared stem (``customer_id`` ↔ ``customer_key``) is a strong signal; failing
    that, a high character-overlap ratio (``order_amount`` ↔ ``order_amt``) above
    the conservative floor. Returns ``None`` when nothing is similar enough.
    """
    if not candidates:
        return None
    src_stem = _stem(name)
    best: Optional[str] = None
    best_score = 0.0
    for cand in candidates:
        if cand.lower() == name.lower():
            continue
        stem_match = src_stem and _stem(cand) == src_stem
        ratio = difflib.SequenceMatcher(None, name.lower(), cand.lower()).ratio()
        score = 1.0 if stem_match else ratio
        if score > best_score:
            best_score = score
            best = cand
    if best is not None and best_score >= _RENAME_SIMILARITY_FLOOR:
        return best
    return None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_schema_drift(
    baseline_cols: Mapping[str, Mapping[str, Optional[str]]],
    authored_cols: Mapping[str, Mapping[str, Optional[str]]],
    *,
    baseline_kind: str,
    flag_dropped: bool,
) -> SemanticDriftReport:
    """Classify column drift between an authored contract and a baseline.

    Args:
        baseline_cols / authored_cols: ``{lower_name: {"name", "type"}}`` maps.
        baseline_kind: one of :data:`BASELINE_SOURCE` / :data:`BASELINE_PRIOR`
            (embedded in the corrective feedback verbatim).
        flag_dropped: when ``True`` a baseline column with no authored match (and
            no rename candidate) is a *blocking* ``dropped_column`` drift; when
            ``False`` (source baseline) it is an informational note only, because
            a product may legitimately project a subset of its source.

    Renames and type changes are always blocking. Added columns (authored-only,
    not a rename target) are always informational notes.
    """
    report = SemanticDriftReport(baseline_kind=baseline_kind)

    authored_only = [ln for ln in authored_cols if ln not in baseline_cols]
    claimed: set[str] = set()

    for lname, bcol in baseline_cols.items():
        acol = authored_cols.get(lname)
        if acol is not None:
            bf = _type_family(bcol.get("type"))
            af = _type_family(acol.get("type"))
            if bf != "UNKNOWN" and af != "UNKNOWN" and bf != af:
                report.drifts.append(
                    SchemaDrift(
                        column=str(bcol["name"]),
                        reason="type_changed",
                        baseline_kind=baseline_kind,
                        baseline_type=bcol.get("type"),
                        authored_type=acol.get("type"),
                    )
                )
            continue

        # Baseline column absent from the authored contract — rename or drop?
        remaining = [authored_cols[ln]["name"] for ln in authored_only if ln not in claimed]
        candidate = _best_rename_candidate(str(bcol["name"]), [str(c) for c in remaining])
        if candidate is not None:
            claimed.add(candidate.lower())
            report.drifts.append(
                SchemaDrift(
                    column=str(bcol["name"]),
                    reason="renamed_column",
                    baseline_kind=baseline_kind,
                    baseline_type=bcol.get("type"),
                    authored_type=authored_cols.get(candidate.lower(), {}).get("type"),
                    renamed_to=candidate,
                )
            )
        elif flag_dropped:
            report.drifts.append(
                SchemaDrift(
                    column=str(bcol["name"]),
                    reason="dropped_column",
                    baseline_kind=baseline_kind,
                    baseline_type=bcol.get("type"),
                )
            )
        else:
            report.notes.append(
                f"source column '{bcol['name']}' is not carried into the product "
                "(a projection, not drift)"
            )

    for lname in authored_only:
        if lname not in claimed:
            report.notes.append(
                f"authored column '{authored_cols[lname]['name']}' is not in the "
                f"{baseline_kind}"
            )

    return report


def resolve_drift_baseline(
    context: Mapping[str, Any],
    discovery_report: Any,
) -> Optional[Tuple[str, Dict[str, Dict[str, Optional[str]]]]]:
    """Pick the drift baseline: prior contract (``--refine``) first, else source.

    Returns ``(baseline_kind, columns)`` or ``None`` when neither baseline
    carries any columns to reconcile against.
    """
    override = context.get("seed_contract_override") if isinstance(context, Mapping) else None
    if isinstance(override, Mapping) and override.get("kind") == "DataProduct":
        prior_cols = extract_contract_columns(override)
        if prior_cols:
            return BASELINE_PRIOR, prior_cols

    source_cols = extract_source_columns(discovery_report)
    if source_cols:
        return BASELINE_SOURCE, source_cols
    return None


def detect_authoring_drift(
    context: Mapping[str, Any],
    discovery_report: Any,
    authored_contract: Mapping[str, Any],
) -> Optional[SemanticDriftReport]:
    """Resolve a baseline and classify drift for an authored contract.

    Pure — the ``FLUID_FORGE_DRIFT_GUARD`` opt-in gate is checked by the caller
    (:func:`drift_guard_enabled`) so this stays directly unit-testable. Returns
    ``None`` when there is no baseline or the authored contract has no columns.
    """
    baseline = resolve_drift_baseline(context, discovery_report)
    if baseline is None:
        return None
    baseline_kind, baseline_cols = baseline
    authored_cols = extract_contract_columns(authored_contract)
    if not authored_cols:
        return None
    return detect_schema_drift(
        baseline_cols,
        authored_cols,
        baseline_kind=baseline_kind,
        flag_dropped=(baseline_kind == BASELINE_PRIOR),
    )


def drift_guard_enabled() -> bool:
    """Opt-in gate — the guard is OFF unless ``FLUID_FORGE_DRIFT_GUARD`` is truthy.

    Non-breaking by default: existing forge runs are unaffected until a user (or
    CI) explicitly turns the guard on. Truthy values: ``1 / true / yes / on``.
    """
    value = os.environ.get("FLUID_FORGE_DRIFT_GUARD", "").strip().lower()
    return value in {"1", "true", "yes", "on"}
