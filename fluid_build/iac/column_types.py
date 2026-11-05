# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a column type becomes on a cloud table, and what that costs.

The AWS (Glue) and GCP (BigQuery) emitters each translate a contract's column
types into their own. A type with no faithful target used to be written anyway:
Glue turned 47 of the 79 types the schema admits into ``string``, and BigQuery
emitted ``ARRAY`` (no such column type: an array is a REPEATED column of an
element type) or a ``STRUCT`` with no fields, which it rejects at apply. Neither
said so, and ``fluid validate --strict`` passed.

Each emitter now classifies a type as one of:

* ``exact``: the target type holds every value the declared type does.
* ``lossy``: the target has no such type, a fallback is written (``json`` becomes
  ``string`` on Glue) and ``fluid validate`` warns, so ``--strict`` fails.
* ``refused``: there is no form worth writing; validate, ``generate iac`` and
  ``apply`` all refuse with an :class:`UnsupportedBindingError` of kind
  ``column-type``.

The classification lives with each emitter's type table (``_hive_outcome`` in
``providers/aws.py``, ``_bq_outcome`` in ``providers/gcp.py``). This module holds
what both share, and :func:`column_type_findings` is the one function the emitter
and the validate-time gate both call, so they cannot disagree.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, NamedTuple, Optional, Tuple

from .base import UnsupportedBindingError

EXACT = "exact"
LOSSY = "lossy"
REFUSED = "refused"


class TypeOutcome(NamedTuple):
    """One declared type's translation: ``target`` is ``None`` when refused."""

    target: Optional[str]
    status: str
    why: str = ""
    fix: str = ""


#: Types that carry element or field types. No cloud table takes them without that
#: detail, which a column declares as ``fields``, ``element``, or ``key`` and ``value``
#: from contract 0.7.7 (``$defs.column`` was closed before, and ``array(string)`` is
#: only a type pattern).
STRUCTURED = frozenset({"array", "struct", "map", "object", "record", "row"})

#: The fix for a refused structured column: BigQuery for every one, and Glue for a
#: declared one it cannot hold.
NESTED_FIX = "Declare json or string instead."

#: The fix for a bare structured column on Glue, which holds it once it is declared.
NESTED_FIX_DECLARE = (
    "Declare the type's fields, element, or key and value (contract 0.7.7 or later), "
    "or declare json or string."
)


def expand(groups: Mapping[str, str]) -> Dict[str, str]:
    """``{"INT64": "int, bigint"}`` as ``{"int": "INT64", "bigint": "INT64"}``: a type
    table written one target at a time, read one declared type at a time."""
    return {name.strip(): target for target, names in groups.items() for name in names.split(",")}


def exact(target: str) -> TypeOutcome:
    return TypeOutcome(target, EXACT)


def lossy(target: str, why: str, fix: str) -> TypeOutcome:
    return TypeOutcome(target, LOSSY, why, fix)


def refused(why: str, fix: str) -> TypeOutcome:
    return TypeOutcome(None, REFUSED, why, fix)


def split_type(raw: Any) -> Tuple[str, str]:
    """``("decimal", "(10,2)")`` for ``"Decimal (10,2)"``; ``("array", "(string)")``
    for ``"ARRAY(string)"``. The base is lower-cased with its words single-spaced
    (``timestamp with time zone``); the rest is whatever follows it, spaces dropped.
    A missing type is ``string``, as the emitters have always read it."""
    text = " ".join(str(raw if raw is not None else "string").strip().lower().split())
    if not text:
        text = "string"
    cuts = [i for i in (text.find("("), text.find("<")) if i >= 0]
    cut = min(cuts) if cuts else len(text)
    return text[:cut].strip(), text[cut:].replace(" ", "")


def exposes_of(contract: Any) -> List[Any]:
    """The contract's ``exposes`` when it is a list, else none: a schema-invalid contract
    is the schema check's to report, and the type gate skips what it cannot read."""
    exposes = contract.get("exposes") if isinstance(contract, Mapping) else None
    return exposes if isinstance(exposes, list) else []


def schema_of(exposure: Mapping[str, Any]) -> List[Any]:
    """The expose's ``contract.schema`` columns when they are a list, else none."""
    section = exposure.get("contract")
    schema = section.get("schema") if isinstance(section, Mapping) else None
    return schema if isinstance(schema, list) else []


def column_type_findings(
    expose: str,
    schema: Iterable[Any],
    outcome_for: Callable[[Any], TypeOutcome],
    target: str,
    column_outcome: Optional[Callable[[Mapping[str, Any]], Optional[TypeOutcome]]] = None,
) -> Tuple[Optional[UnsupportedBindingError], List[str]]:
    """``(refusal, warnings)`` for one expose's columns on ``target`` (a table kind).

    One refusal covers every column the target cannot hold, so the author fixes
    them in one pass; each lossy column is its own warning. ``column_outcome``
    judges a whole column first (one that declares its ``fields``, ``element``,
    ``key`` or ``value``); ``None`` leaves it to ``outcome_for``, which sees the
    type name alone.
    """
    parts: List[str] = []
    fixes: Dict[str, List[str]] = {}
    warnings: List[str] = []
    for column in schema or []:
        if not isinstance(column, Mapping):
            continue
        name = column.get("name")
        declared = str(column.get("type") if column.get("type") is not None else "string")
        outcome = (column_outcome(column) if column_outcome else None) or outcome_for(
            column.get("type")
        )
        if outcome.status == REFUSED:
            parts.append(f"{name!r} is declared {declared!r}, {outcome.why}")
            fixes.setdefault(outcome.fix, []).append(repr(name))
        elif outcome.status == LOSSY:
            warnings.append(
                f"expose '{expose}': column {name!r} is declared {declared!r}, {outcome.why}; "
                f"it is written as {outcome.target!r}. {outcome.fix}"
            )
    if not parts:
        return None, warnings
    refusal = UnsupportedBindingError(
        "column-type",
        f"expose '{expose}': {target} cannot hold its columns as declared: "
        + "; ".join(parts)
        + ".",
        tuple(
            f"{'Column' if len(names) == 1 else 'Columns'} {', '.join(names)}: {fix}"
            for fix, names in fixes.items()
        ),
    )
    return refusal, warnings
