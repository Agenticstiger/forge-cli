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

from ..severity import is_error
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


def _expose_schema(expose: Mapping[str, Any]) -> Tuple[Sequence[Any], Optional[str]]:
    """Return an expose's columns and where, below the expose, they were found.

    Pre-0.7 contracts keep their columns at ``schema``; 0.7.x contracts keep
    them at ``contract.schema``. The flat ``schema`` is read when it is
    present and ``contract.schema`` only when it is absent, so a contract
    that carries both is read as it was before 0.7.x. Either one may wrap
    its list as ``{"fields": [...]}``, as ``_verify_reconcile`` and
    ``forge_copilot_drift_guard`` accept, and the columns are then found at
    ``schema.fields``. The location is ``None`` when the expose carries no
    schema at all.
    """
    columns, location = expose.get("schema"), "schema"
    if columns is None:
        contract = expose.get("contract")
        columns = contract.get("schema") if isinstance(contract, Mapping) else None
        location = "contract.schema"
    if columns is None:
        return [], None
    if isinstance(columns, Mapping):
        columns, location = columns.get("fields"), f"{location}.fields"
    return (columns if isinstance(columns, (list, tuple)) else []), location


def _is_nullable(column: Mapping[str, Any]) -> bool:
    """Whether *column* admits NULL.

    Pre-0.7 columns spell NOT NULL ``nullable: false``. A 0.7.x column has no
    ``nullable`` key and spells it ``required: true``. A column is NOT NULL
    when either says so: the IaC emitters read ``required`` alone, by
    truthiness, so a column that declares both and contradicts itself must not
    be read as nullable.
    """
    return column.get("nullable") is not False and not column.get("required")


def _nullability_key(old: Mapping[str, Any], new: Mapping[str, Any]) -> str:
    """The key a nullability change is reported at: the one the contract uses.

    A 0.7.x column says ``required``, a pre-0.7 one ``nullable``. The new
    column decides; the old one does when the new says neither. A column that
    carries both is reported at ``nullable``.
    """
    for column in (new, old):
        if "nullable" in column:
            return "nullable"
        if "required" in column:
            return "required"
    return "nullable"


def _primary_key_set(expose: Mapping[str, Any]) -> frozenset[str]:
    """Extract the set of primary-key column names from an expose dict.

    Supports both ``primaryKey: [col1, col2]`` at the expose root and the
    per-column ``primaryKey: true`` flag — both occur in real contracts —
    and a 0.7.x column's primary-key tag (see :func:`_in_primary_key`).
    """
    pk: set[str] = set()
    expose_pk = expose.get("primaryKey")
    if isinstance(expose_pk, (list, tuple)):
        for n in expose_pk:
            if isinstance(n, str):
                pk.add(n)
    for col in _expose_schema(expose)[0]:
        if isinstance(col, Mapping) and _in_primary_key(col):
            name = col.get("name")
            if isinstance(name, str):
                pk.add(name)
    return frozenset(pk)


def _in_primary_key(column: Mapping[str, Any]) -> bool:
    """Whether *column* says it is part of its expose's primary key.

    A 0.7.x column has no ``primaryKey`` key: it is in the key when its
    ``tags`` hold ``primary-key`` or ``primaryKey``, the tag the ODCS exporter
    turns into ``primaryKey: true`` (``providers/odcs/mappers``).
    """
    tags = column.get("tags")
    tagged = isinstance(tags, (list, tuple)) and ("primary-key" in tags or "primaryKey" in tags)
    return column.get("primaryKey") is True or tagged


#: Column sensitivities that mark personal data, as ``cli/docs_build.py`` and
#: the MCP output port's PII redaction read them.
_PERSONAL_DATA = frozenset({"pii", "phi"})


def _is_pii(column: Mapping[str, Any]) -> bool:
    """Whether *column* holds personal data.

    Pre-0.7 columns say ``pii: true``. A 0.7.x column has no ``pii`` key and
    says ``sensitivity: pii`` (or ``phi``).
    """
    sensitivity = column.get("sensitivity")
    return bool(column.get("pii")) or (
        isinstance(sensitivity, str) and sensitivity in _PERSONAL_DATA
    )


def _sensitivity_changes(
    old: Mapping[str, Any], new: Mapping[str, Any], col_name: str, path_prefix: str
) -> List[Change]:
    """PII annotation drift, and any other change to a 0.7.x column's ``sensitivity``.

    A column gaining ``pii: true`` is a governance signal — downstream
    consumers may have access controls that key off this flag. Surface as
    INFO-level so it shows up in PR-review feedback without breaking the
    build. A 0.7.x column says it with ``sensitivity`` (see :func:`_is_pii`),
    and the change is reported at the key that carried it.
    """
    old_pii, new_pii = _is_pii(old), _is_pii(new)
    old_sensitivity, new_sensitivity = old.get("sensitivity"), new.get("sensitivity")
    if old_pii != new_pii:
        key = "sensitivity" if old_sensitivity != new_sensitivity else "pii"
        return [
            Change(
                path=f"{path_prefix}.{col_name}.{key}",
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
        ]
    if old_sensitivity != new_sensitivity:
        return [
            Change(
                path=f"{path_prefix}.{col_name}.sensitivity",
                kind="column_sensitivity_changed",
                severity="info",
                description=(
                    f"column '{col_name}' sensitivity changed "
                    f"{old_sensitivity or 'unset'} -> {new_sensitivity or 'unset'}"
                ),
                before=old_sensitivity,
                after=new_sensitivity,
            )
        ]
    return []


def diff_columns(
    baseline_expose: Mapping[str, Any],
    new_expose: Mapping[str, Any],
    expose_id: str,
    expose_idx: int,
) -> List[Change]:
    """Classify per-column changes between two versions of the same expose.

    Columns are read from ``schema`` or, in a 0.7.x contract, from
    ``contract.schema`` (see :func:`_expose_schema`).
    """
    base_cols, base_at = _expose_schema(baseline_expose)
    new_cols, new_at = _expose_schema(new_expose)
    base_by_name = _columns_by_name(base_cols)
    new_by_name = _columns_by_name(new_cols)
    pk = _primary_key_set(new_expose) | _primary_key_set(baseline_expose)
    # Paths follow the layout the new contract uses (the baseline's, when the
    # new one gave the expose no schema).
    path_prefix = f"exposes[{expose_idx}].{new_at or base_at or 'schema'}"

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
        is_nullable = _is_nullable(col)
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

        changes.extend(_sensitivity_changes(old, new, col_name, path_prefix))

        # Nullability changes.
        old_null = _is_nullable(old)
        new_null = _is_nullable(new)
        if old_null != new_null:
            key = _nullability_key(old, new)
            if new_null and col_name in pk:
                # PK column became nullable — breaking.
                changes.append(
                    Change(
                        path=f"{path_prefix}.{col_name}.{key}",
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
                        path=f"{path_prefix}.{col_name}.{key}",
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
                        path=f"{path_prefix}.{col_name}.{key}",
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


#: Where an expose keeps its quality rules, and the keys that name one there:
#: pre-0.7 ``quality.tests``; in 0.7.x ``contract.dq.rules`` (``$defs.dqRule``)
#: and the inline ``contract.quality``.
_QUALITY_RULES: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...], str], ...] = (
    (("quality", "tests"), ("name", "id"), "quality test"),
    (("contract", "dq", "rules"), ("id",), "quality rule"),
    (("contract", "quality"), ("rule",), "quality rule"),
)


def diff_quality_severity(
    baseline_expose: Mapping[str, Any],
    new_expose: Mapping[str, Any],
    expose_id: str,
    expose_idx: int,
) -> List[Change]:
    """A quality rule whose severity escalates to a failing one breaks downstream gating.

    Reads the rules of every location in ``_QUALITY_RULES`` and, from 0.7.6,
    each column's own ``quality`` rules (named by ``id``, ODCS's shape).
    ``critical`` fails a run as ``error`` does (``fluid_build.severity``), so
    either one is an escalation from ``warn``, and one to the other is not. A
    rule is matched across versions by name; one without a name is skipped.
    """
    changes: List[Change] = []
    for location, keys, noun in _QUALITY_RULES:
        changes.extend(
            _severity_escalations(
                _rules_by_name(_dig(baseline_expose, location), keys),
                _rules_by_name(_dig(new_expose, location), keys),
                path_prefix=f"exposes[{expose_idx}].{'.'.join(location)}",
                owner=f"expose '{expose_id}'",
                noun=noun,
            )
        )

    base_columns = _columns_by_name(_expose_schema(baseline_expose)[0])
    new_cols, new_at = _expose_schema(new_expose)
    for col_name, new_col in _columns_by_name(new_cols).items():
        old_col = base_columns.get(col_name)
        if old_col is None:
            continue
        changes.extend(
            _severity_escalations(
                _rules_by_name(old_col.get("quality"), ("id", "name")),
                _rules_by_name(new_col.get("quality"), ("id", "name")),
                path_prefix=f"exposes[{expose_idx}].{new_at}.{col_name}.quality",
                owner=f"column '{col_name}' of expose '{expose_id}'",
                noun="quality rule",
            )
        )
    return changes


def _severity_escalations(
    old_rules: Mapping[str, Sequence[Mapping[str, Any]]],
    new_rules: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    path_prefix: str,
    owner: str,
    noun: str,
) -> List[Change]:
    """A named rule escalated: failing in the new contract, and not in the old one.

    A name can be on several rules (nothing makes an id unique), and each
    version is judged by its most severe rule of that name, so a failing rule
    cannot hide behind a ``warn`` one of the same id.
    """
    changes: List[Change] = []
    for name, new_group in new_rules.items():
        old_group = old_rules.get(name)
        if not old_group:
            continue
        old_sev = _most_severe(old_group)
        new_sev = _most_severe(new_group)
        if not is_error(old_sev) and is_error(new_sev):
            changes.append(
                Change(
                    path=f"{path_prefix}.{name}.severity",
                    kind="quality_severity_escalated",
                    severity="breaking",
                    description=(
                        f"{noun} '{name}' on {owner} "
                        f"escalated to severity={new_sev} (previously {old_sev}); "
                        f"downstream gating will now hard-fail"
                    ),
                    before=old_sev,
                    after=new_sev,
                )
            )
    return changes


def _dig(node: Any, keys: Sequence[str]) -> Any:
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _most_severe(rules: Sequence[Mapping[str, Any]]) -> str:
    severities = [str(rule.get("severity", "warn")).lower() for rule in rules]
    return next((sev for sev in severities if is_error(sev)), severities[0])


def _rules_by_name(rules: Any, keys: Sequence[str]) -> dict[str, List[Mapping[str, Any]]]:
    out: dict[str, List[Mapping[str, Any]]] = {}
    if not isinstance(rules, (list, tuple)):
        return out
    for rule in rules:
        if isinstance(rule, Mapping):
            name = next((rule.get(k) for k in keys if rule.get(k)), None)
            if isinstance(name, str):
                out.setdefault(name, []).append(rule)
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
        is_nullable = _is_nullable(field)
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
