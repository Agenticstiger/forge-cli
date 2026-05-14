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

"""Per-field rules that classify a structural change as breaking / non_breaking / info.

The rule functions in this module are stateless — each takes a slice of the
baseline and the new contract and returns a list of ``Change`` objects. The
top-level orchestration lives in ``changelog.py``; rule additions land here
so the table is in one place.

Conservative bias: when a change's classification is ambiguous (e.g. a type
swap that isn't clearly widening), we mark it BREAKING. Downstream consumers
can't make assumptions about the new shape; the safer default is to flag.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .changelog_types import Change

# Type widening table — keys are the "from" type, values are the set of
# types that strictly widen them. Anything not in this table is treated as
# a breaking type change. Names normalized to upper-case AND stripped of
# precision/scale annotations (those are compared separately below).
_WIDENING: Mapping[str, frozenset[str]] = {
    "INT": frozenset({"BIGINT", "INTEGER", "LONG", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE"}),
    "INT32": frozenset({"INT64", "BIGINT", "LONG", "DECIMAL", "FLOAT", "DOUBLE"}),
    "INTEGER": frozenset({"BIGINT", "LONG", "DECIMAL", "FLOAT", "DOUBLE"}),
    "SMALLINT": frozenset({"INT", "INT32", "INT64", "BIGINT", "INTEGER", "LONG"}),
    "FLOAT": frozenset({"DOUBLE", "DECIMAL", "NUMERIC"}),
    "VARCHAR": frozenset({"TEXT", "STRING"}),
    "STRING": frozenset({"TEXT"}),
    "DATE": frozenset({"DATETIME", "TIMESTAMP"}),
    "TIME": frozenset({"DATETIME", "TIMESTAMP"}),
}

# Type-name + parameter regex. Captures e.g. ``DECIMAL(10,2)`` as ("DECIMAL",
# "10,2") and ``VARCHAR(255)`` as ("VARCHAR", "255"). Suffix-attribute-less
# types ("BIGINT") fall through with empty params.
_TYPE_PARAM_RE = re.compile(r"^([A-Z0-9_]+)\s*(?:\(([^)]+)\))?\s*$")


def _norm_type(t: Any) -> str:
    if not isinstance(t, str):
        return ""
    return t.strip().upper()


def _parse_type(t: str) -> Tuple[str, Optional[Tuple[int, ...]]]:
    """Split a type string into (base_name, params_tuple_or_None).

    Examples:
        ``"DECIMAL(10,2)"`` → ``("DECIMAL", (10, 2))``
        ``"VARCHAR(255)"`` → ``("VARCHAR", (255,))``
        ``"BIGINT"`` → ``("BIGINT", None)``
        ``"DECIMAL"`` → ``("DECIMAL", None)``  (no precision specified)

    Returns ``("", None)`` when the input doesn't parse cleanly — caller
    decides whether to treat that as a generic mismatch.
    """
    if not t:
        return ("", None)
    m = _TYPE_PARAM_RE.match(t.strip())
    if not m:
        return (t.strip(), None)
    base, params = m.group(1), m.group(2)
    if not params:
        return (base, None)
    try:
        nums = tuple(int(p.strip()) for p in params.split(","))
    except ValueError:
        return (base, None)
    return (base, nums)


def _classify_param_change(
    old_params: Optional[Tuple[int, ...]],
    new_params: Optional[Tuple[int, ...]],
    base: str,
) -> Optional[str]:
    """Classify a same-base type parameter change as 'widening' / 'narrowing'.

    Returns ``"widening"`` if every parameter grew (or stayed equal),
    ``"narrowing"`` if any parameter shrank, ``"changed"`` for ambiguous /
    not-comparable cases, or ``None`` when both sides have no params (so
    there's no diff to classify).
    """
    if old_params is None and new_params is None:
        return None
    if old_params is None or new_params is None:
        # One side has an explicit precision/length; the other doesn't.
        # Compilers treat the unparameterized form as "implementation
        # default", which can be either way — flag as a generic change.
        return "changed"
    if old_params == new_params:
        return None
    if len(old_params) != len(new_params):
        # DECIMAL(10) vs DECIMAL(10,2) — semantically different shapes.
        return "changed"
    pairs = list(zip(old_params, new_params, strict=True))
    if all(n >= o for o, n in pairs):
        return "widening"
    if all(n <= o for o, n in pairs):
        return "narrowing"
    # Mixed: e.g. precision grew but scale shrank — neither pure widening
    # nor pure narrowing. Treat conservatively as a change requiring review.
    return "changed"


def _is_widening(old_type: str, new_type: str) -> bool:
    """Strict widening check — base-name lookup only, no params considered.

    Used by callers that already extracted base names. For mixed
    base+param diffs use :func:`_classify_type_diff` which is precision-aware.
    """
    if old_type == new_type:
        return True
    widens_to = _WIDENING.get(old_type, frozenset())
    return new_type in widens_to


def _classify_type_diff(old_full: str, new_full: str) -> Tuple[str, str]:
    """Classify a (possibly parameterized) type change.

    Returns ``(severity, kind)``:
      * ``("non_breaking", "column_type_widened")`` — base widened or
        parameters strictly grew
      * ``("breaking", "column_type_changed")`` — base narrowed, parameters
        shrank, or unrelated type swap
      * ``("info", "column_type_param_changed")`` — same base, parameters
        changed in a way that's neither pure widening nor pure narrowing
    """
    old_base, old_params = _parse_type(old_full)
    new_base, new_params = _parse_type(new_full)

    if old_base == new_base:
        # Same base type — classify by parameter movement.
        change = _classify_param_change(old_params, new_params, old_base)
        if change is None:
            # Same base, no param difference but full strings differ —
            # whitespace or capitalization. Treat as info-level.
            return ("info", "column_type_normalized")
        if change == "widening":
            return ("non_breaking", "column_type_widened")
        if change == "narrowing":
            return ("breaking", "column_type_changed")
        return ("info", "column_type_param_changed")

    # Different base type — fall back to the base-name widening table.
    if _is_widening(old_base, new_base):
        return ("non_breaking", "column_type_widened")
    return ("breaking", "column_type_changed")


def _columns_by_name(columns: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for col in columns or []:
        if isinstance(col, Mapping):
            name = col.get("name") or col.get("id")
            if isinstance(name, str):
                out[name] = col
    return out


def _primary_key_set(expose: Mapping[str, Any]) -> frozenset[str]:
    """Extract the set of primary-key column names from an expose dict.

    Supports both ``primaryKey: [col1, col2]`` at the expose root and the
    per-column ``primaryKey: true`` flag — both occur in real contracts.
    """
    pk: set[str] = set()
    expose_pk = expose.get("primaryKey")
    if isinstance(expose_pk, (list, tuple)):
        for n in expose_pk:
            if isinstance(n, str):
                pk.add(n)
    for col in expose.get("schema", []) or []:
        if isinstance(col, Mapping) and col.get("primaryKey") is True:
            name = col.get("name")
            if isinstance(name, str):
                pk.add(name)
    return frozenset(pk)


def diff_columns(
    baseline_expose: Mapping[str, Any],
    new_expose: Mapping[str, Any],
    expose_id: str,
    expose_idx: int,
) -> List[Change]:
    """Classify per-column changes between two versions of the same expose."""
    base_cols = baseline_expose.get("schema", []) or []
    new_cols = new_expose.get("schema", []) or []
    base_by_name = _columns_by_name(base_cols)
    new_by_name = _columns_by_name(new_cols)
    pk = _primary_key_set(new_expose) | _primary_key_set(baseline_expose)
    path_prefix = f"exposes[{expose_idx}].schema"

    changes: List[Change] = []

    # Removed columns — always breaking.
    for col_name in base_by_name.keys() - new_by_name.keys():
        changes.append(
            Change(
                path=f"{path_prefix}.{col_name}",
                kind="column_removed",
                severity="breaking",
                description=f"column '{col_name}' removed from expose '{expose_id}'",
                before=dict(base_by_name[col_name]),
                after=None,
            )
        )

    # Added columns — non-breaking when nullable; breaking when non-nullable
    # (downstream pipelines may insert rows that fail the NOT NULL check).
    for col_name in new_by_name.keys() - base_by_name.keys():
        col = new_by_name[col_name]
        is_nullable = col.get("nullable", True) is not False
        severity = "non_breaking" if is_nullable else "breaking"
        description = (
            f"column '{col_name}' added to expose '{expose_id}'"
            if severity == "non_breaking"
            else f"NOT NULL column '{col_name}' added to expose '{expose_id}' "
            f"(breaks inserts that don't supply a value)"
        )
        changes.append(
            Change(
                path=f"{path_prefix}.{col_name}",
                kind="column_added",
                severity=severity,
                description=description,
                before=None,
                after=dict(col),
            )
        )

    # Modified columns — type / nullability / description / PII.
    for col_name in base_by_name.keys() & new_by_name.keys():
        old = base_by_name[col_name]
        new = new_by_name[col_name]
        old_type = _norm_type(old.get("type"))
        new_type = _norm_type(new.get("type"))

        if old_type and new_type and old_type != new_type:
            # Precision/length-aware classifier handles DECIMAL(p,s),
            # VARCHAR(n) and same-base parameter movement separately from
            # base-name widening. See ``_classify_type_diff`` for the rules.
            severity, kind = _classify_type_diff(old_type, new_type)
            if kind == "column_type_widened":
                desc = f"column '{col_name}' type widened {old_type} -> {new_type}"
            elif kind == "column_type_param_changed":
                desc = (
                    f"column '{col_name}' type parameters changed "
                    f"{old_type} -> {new_type} (review: not pure widening or narrowing)"
                )
            elif kind == "column_type_normalized":
                desc = (
                    f"column '{col_name}' type normalized "
                    f"{old_type} -> {new_type} (whitespace/casing only)"
                )
            else:
                desc = (
                    f"column '{col_name}' type changed "
                    f"{old_type} -> {new_type} (not a widening cast)"
                )
            changes.append(
                Change(
                    path=f"{path_prefix}.{col_name}.type",
                    kind=kind,
                    severity=severity,
                    description=desc,
                    before=old_type,
                    after=new_type,
                )
            )

        # Nested / struct field traversal. When a column carries a
        # ``fields`` block (struct-shaped, common in BigQuery RECORD and
        # JSON-schema-derived contracts), recurse into the fields and
        # apply the column-level rules to them. The recursive call uses
        # a path-prefix-anchored sub-call so paths stay readable
        # (``...schema.address.fields.zip``).
        if isinstance(old.get("fields"), Sequence) or isinstance(new.get("fields"), Sequence):
            changes.extend(
                _diff_nested_fields(
                    old.get("fields") or [],
                    new.get("fields") or [],
                    expose_id=expose_id,
                    path_prefix=f"{path_prefix}.{col_name}.fields",
                )
            )

        # PII annotation drift. A column gaining ``pii: true`` is a
        # governance signal — downstream consumers may have access
        # controls that key off this flag. Surface as INFO-level so it
        # shows up in PR-review feedback without breaking the build.
        old_pii = bool(old.get("pii"))
        new_pii = bool(new.get("pii"))
        if old_pii != new_pii:
            changes.append(
                Change(
                    path=f"{path_prefix}.{col_name}.pii",
                    kind="column_pii_added" if new_pii else "column_pii_removed",
                    severity="info",
                    description=(
                        f"column '{col_name}' is now PII"
                        if new_pii
                        else f"column '{col_name}' PII annotation removed"
                    ),
                    before=old_pii,
                    after=new_pii,
                )
            )

        # Nullability changes.
        old_null = old.get("nullable", True) is not False
        new_null = new.get("nullable", True) is not False
        if old_null != new_null:
            if new_null and col_name in pk:
                # PK column became nullable — breaking.
                changes.append(
                    Change(
                        path=f"{path_prefix}.{col_name}.nullable",
                        kind="primary_key_nullable",
                        severity="breaking",
                        description=(
                            f"primary-key column '{col_name}' is now nullable "
                            f"(downstream joins / uniqueness assumptions break)"
                        ),
                        before=old_null,
                        after=new_null,
                    )
                )
            elif not new_null:
                # NOT NULL added to a previously-nullable column — breaking.
                changes.append(
                    Change(
                        path=f"{path_prefix}.{col_name}.nullable",
                        kind="column_nullable_tightened",
                        severity="breaking",
                        description=(
                            f"column '{col_name}' is no longer nullable "
                            f"(rows with NULL in this column will now fail)"
                        ),
                        before=old_null,
                        after=new_null,
                    )
                )
            else:
                # Loosening to nullable on a non-PK — non-breaking.
                changes.append(
                    Change(
                        path=f"{path_prefix}.{col_name}.nullable",
                        kind="column_nullable_loosened",
                        severity="non_breaking",
                        description=f"column '{col_name}' is now nullable",
                        before=old_null,
                        after=new_null,
                    )
                )

        # Description / docstring — info only.
        old_desc = old.get("description")
        new_desc = new.get("description")
        if (old_desc or new_desc) and old_desc != new_desc:
            changes.append(
                Change(
                    path=f"{path_prefix}.{col_name}.description",
                    kind="column_description_changed",
                    severity="info",
                    description=f"column '{col_name}' description updated",
                    before=old_desc,
                    after=new_desc,
                )
            )

    return changes


def diff_consumes(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> List[Change]:
    """Track upstream-product references; removed upstreams break downstreams."""
    base_refs = _consume_refs(baseline)
    new_refs = _consume_refs(new)

    changes: List[Change] = []
    for ref in base_refs - new_refs:
        changes.append(
            Change(
                path="consumes",
                kind="consume_removed",
                severity="breaking",
                description=(
                    f"upstream product '{ref}' removed from consumes[] "
                    f"(downstreams may now lack data)"
                ),
                before=ref,
                after=None,
            )
        )
    for ref in new_refs - base_refs:
        changes.append(
            Change(
                path="consumes",
                kind="consume_added",
                severity="non_breaking",
                description=f"upstream product '{ref}' added to consumes[]",
                before=None,
                after=ref,
            )
        )
    return changes


def _consume_refs(contract: Mapping[str, Any]) -> frozenset[str]:
    refs: set[str] = set()
    for c in contract.get("consumes") or []:
        if not isinstance(c, Mapping):
            continue
        ref = c.get("ref") or c.get("productId") or c.get("provider") or c.get("id")
        if isinstance(ref, str):
            refs.add(ref)
    return frozenset(refs)


def diff_agent_policy(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> List[Change]:
    """Detect narrowing of allowed models / use-cases on the agent policy block."""
    old_policy = baseline.get("agentPolicy") or {}
    new_policy = new.get("agentPolicy") or {}
    if not isinstance(old_policy, Mapping) or not isinstance(new_policy, Mapping):
        return []

    changes: List[Change] = []
    for field in ("allowedModels", "allowedUseCases"):
        old_set = _as_str_set(old_policy.get(field))
        new_set = _as_str_set(new_policy.get(field))
        # Empty set means "no constraint" in many policy interpretations,
        # so a non-empty old set narrowing to a smaller non-empty new set
        # is the breaking shape. Removing items entirely is what flags.
        if old_set and new_set != old_set:
            removed = old_set - new_set
            if removed:
                changes.append(
                    Change(
                        path=f"agentPolicy.{field}",
                        kind="agent_policy_narrowed",
                        severity="breaking",
                        description=(
                            f"agentPolicy.{field} narrowed " f"(removed: {sorted(removed)})"
                        ),
                        before=sorted(old_set),
                        after=sorted(new_set),
                    )
                )

    for field in ("deniedModels", "deniedUseCases"):
        old_set = _as_str_set(old_policy.get(field))
        new_set = _as_str_set(new_policy.get(field))
        added = new_set - old_set
        if added:
            changes.append(
                Change(
                    path=f"agentPolicy.{field}",
                    kind="agent_policy_denied_expanded",
                    severity="breaking",
                    description=(f"agentPolicy.{field} expanded " f"(added: {sorted(added)})"),
                    before=sorted(old_set),
                    after=sorted(new_set),
                )
            )
    return changes


def diff_sovereignty(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> List[Change]:
    """Detect narrowing of allowed regions / expansion of prohibited transfers."""
    old_sov = baseline.get("sovereignty") or {}
    new_sov = new.get("sovereignty") or {}
    if not isinstance(old_sov, Mapping) or not isinstance(new_sov, Mapping):
        return []

    changes: List[Change] = []

    old_allowed = _as_str_set(old_sov.get("allowedRegions"))
    new_allowed = _as_str_set(new_sov.get("allowedRegions"))
    if old_allowed and new_allowed != old_allowed:
        removed = old_allowed - new_allowed
        if removed:
            changes.append(
                Change(
                    path="sovereignty.allowedRegions",
                    kind="sovereignty_regions_narrowed",
                    severity="breaking",
                    description=(
                        f"sovereignty.allowedRegions narrowed " f"(removed: {sorted(removed)})"
                    ),
                    before=sorted(old_allowed),
                    after=sorted(new_allowed),
                )
            )

    old_prohibit = _as_str_set(old_sov.get("prohibitTransferTo"))
    new_prohibit = _as_str_set(new_sov.get("prohibitTransferTo"))
    added_prohibit = new_prohibit - old_prohibit
    if added_prohibit:
        changes.append(
            Change(
                path="sovereignty.prohibitTransferTo",
                kind="sovereignty_prohibit_expanded",
                severity="breaking",
                description=(
                    f"sovereignty.prohibitTransferTo expanded " f"(added: {sorted(added_prohibit)})"
                ),
                before=sorted(old_prohibit),
                after=sorted(new_prohibit),
            )
        )
    return changes


def diff_quality_severity(
    baseline_expose: Mapping[str, Any],
    new_expose: Mapping[str, Any],
    expose_id: str,
    expose_idx: int,
) -> List[Change]:
    """A quality rule whose severity escalates warn -> error breaks downstream gating."""
    old_tests = _quality_tests_by_name(baseline_expose)
    new_tests = _quality_tests_by_name(new_expose)

    changes: List[Change] = []
    path_prefix = f"exposes[{expose_idx}].quality.tests"
    for name, new_test in new_tests.items():
        old_test = old_tests.get(name)
        if old_test is None:
            continue
        old_sev = str(old_test.get("severity", "warn")).lower()
        new_sev = str(new_test.get("severity", "warn")).lower()
        if old_sev != "error" and new_sev == "error":
            changes.append(
                Change(
                    path=f"{path_prefix}.{name}.severity",
                    kind="quality_severity_escalated",
                    severity="breaking",
                    description=(
                        f"quality test '{name}' on expose '{expose_id}' "
                        f"escalated to severity=error (previously {old_sev}); "
                        f"downstream gating will now hard-fail"
                    ),
                    before=old_sev,
                    after=new_sev,
                )
            )
    return changes


def _quality_tests_by_name(expose: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    quality = expose.get("quality") or {}
    if not isinstance(quality, Mapping):
        return out
    tests = quality.get("tests") or []
    for t in tests:
        if isinstance(t, Mapping):
            name = t.get("name") or t.get("id")
            if isinstance(name, str):
                out[name] = t
    return out


def diff_metadata(baseline: Mapping[str, Any], new: Mapping[str, Any]) -> List[Change]:
    """Description / owner / tag drift — info-level signal."""
    changes: List[Change] = []

    # ``description`` lives at the top level of the fluid contract in
    # v0.7.x, not under ``metadata`` — but track both for forward-compat
    # with future revisions that may move it.
    for field in ("description", "name"):
        old_v = baseline.get(field)
        new_v = new.get(field)
        if (old_v or new_v) and old_v != new_v:
            changes.append(
                Change(
                    path=field,
                    kind=f"metadata_{field}_changed",
                    severity="info",
                    description=f"{field} updated",
                    before=old_v,
                    after=new_v,
                )
            )

    old_meta = baseline.get("metadata") or {}
    new_meta = new.get("metadata") or {}
    if not isinstance(old_meta, Mapping) or not isinstance(new_meta, Mapping):
        return changes

    for field in ("description", "owner", "domain"):
        old_v = old_meta.get(field)
        new_v = new_meta.get(field)
        if (old_v or new_v) and old_v != new_v:
            changes.append(
                Change(
                    path=f"metadata.{field}",
                    kind=f"metadata_{field}_changed",
                    severity="info",
                    description=f"metadata.{field} updated",
                    before=old_v,
                    after=new_v,
                )
            )

    old_tags = _as_str_set(old_meta.get("tags"))
    new_tags = _as_str_set(new_meta.get("tags"))
    if old_tags != new_tags:
        changes.append(
            Change(
                path="metadata.tags",
                kind="metadata_tags_changed",
                severity="info",
                description=(
                    f"metadata.tags updated "
                    f"(added: {sorted(new_tags - old_tags)}, "
                    f"removed: {sorted(old_tags - new_tags)})"
                ),
                before=sorted(old_tags),
                after=sorted(new_tags),
            )
        )
    return changes


def _as_str_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(v for v in value if isinstance(v, str))
    if isinstance(value, str):
        return frozenset({value})
    return frozenset()


def _diff_nested_fields(
    base_fields: Sequence[Mapping[str, Any]],
    new_fields: Sequence[Mapping[str, Any]],
    *,
    expose_id: str,
    path_prefix: str,
) -> List[Change]:
    """Recurse into ``column.fields[]`` (struct / record types).

    Same classification rules as top-level columns:
      - field removed → BREAKING
      - field added (nullable) → NON_BREAKING
      - field added (NOT NULL) → BREAKING
      - field type changed → classified by widening table + precision rules
      - nested ``fields`` recurses further (recursion depth unbounded in
        principle, but real-world contracts stay shallow)
    """
    base_by_name = _columns_by_name(base_fields)
    new_by_name = _columns_by_name(new_fields)
    changes: List[Change] = []

    for fname in base_by_name.keys() - new_by_name.keys():
        changes.append(
            Change(
                path=f"{path_prefix}.{fname}",
                kind="nested_field_removed",
                severity="breaking",
                description=(
                    f"nested field '{fname}' removed from expose '{expose_id}' " f"({path_prefix})"
                ),
                before=dict(base_by_name[fname]),
                after=None,
            )
        )

    for fname in new_by_name.keys() - base_by_name.keys():
        field = new_by_name[fname]
        is_nullable = field.get("nullable", True) is not False
        severity = "non_breaking" if is_nullable else "breaking"
        changes.append(
            Change(
                path=f"{path_prefix}.{fname}",
                kind="nested_field_added",
                severity=severity,
                description=(
                    f"nested field '{fname}' added to expose '{expose_id}' " f"({path_prefix})"
                ),
                before=None,
                after=dict(field),
            )
        )

    for fname in base_by_name.keys() & new_by_name.keys():
        old = base_by_name[fname]
        new = new_by_name[fname]
        old_type = _norm_type(old.get("type"))
        new_type = _norm_type(new.get("type"))
        if old_type and new_type and old_type != new_type:
            severity, kind = _classify_type_diff(old_type, new_type)
            changes.append(
                Change(
                    path=f"{path_prefix}.{fname}.type",
                    kind=f"nested_{kind}",
                    severity=severity,
                    description=(
                        f"nested field '{fname}' type changed " f"{old_type} -> {new_type}"
                    ),
                    before=old_type,
                    after=new_type,
                )
            )
        # Deeper nesting (struct of struct).
        if isinstance(old.get("fields"), Sequence) or isinstance(new.get("fields"), Sequence):
            changes.extend(
                _diff_nested_fields(
                    old.get("fields") or [],
                    new.get("fields") or [],
                    expose_id=expose_id,
                    path_prefix=f"{path_prefix}.{fname}.fields",
                )
            )

    return changes


def iter_expose_pairs(
    baseline: Mapping[str, Any], new: Mapping[str, Any]
) -> Iterable[tuple[str, int, Optional[Mapping[str, Any]], Optional[Mapping[str, Any]]]]:
    """Yield (expose_id, idx_in_new, baseline_expose_or_None, new_expose_or_None).

    Matched by ``id`` field — falls back to positional match when ids absent.
    """
    base_exposes = list(baseline.get("exposes") or [])
    new_exposes = list(new.get("exposes") or [])
    base_by_id = {
        (e.get("id") or e.get("exposeId") or f"_pos_{i}"): (i, e)
        for i, e in enumerate(base_exposes)
        if isinstance(e, Mapping)
    }
    new_by_id = {
        (e.get("id") or e.get("exposeId") or f"_pos_{i}"): (i, e)
        for i, e in enumerate(new_exposes)
        if isinstance(e, Mapping)
    }
    all_ids = set(base_by_id) | set(new_by_id)
    for eid in all_ids:
        b = base_by_id.get(eid)
        n = new_by_id.get(eid)
        idx_in_new = n[0] if n else (b[0] if b else 0)
        yield eid, idx_in_new, (b[1] if b else None), (n[1] if n else None)
