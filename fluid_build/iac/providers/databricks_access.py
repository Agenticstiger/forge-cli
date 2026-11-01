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

"""Column masks and row filters for a Databricks table: the SQL, and nothing that runs it.

``exposes[].policy.authz.columnRestrictions`` and ``rowFilters`` are enforced by Unity
Catalog itself: a SQL function bound on the table, so a query by a restricted principal
is rewritten by the engine, whichever tool sends it. The semantics are the ones GCP and
AWS give the same fields (``iac/column_access.py``, ``iac/row_access.py``):

* ``access: deny`` on a column becomes a mask that returns ``CAST(NULL AS <type>)`` for
  the principal: Unity Catalog has no column-level grant, so the column stays listed.
* ``access: mask`` returns the value masked, as BigQuery's data policy does:
  ``last_four`` is ``XXXXX`` and the last four characters, ``first_four`` the first four
  and ``XXXXX``, a value of four characters or fewer is its SHA-256 (hex) instead and a
  NULL stays NULL (the AWS emitter's rule, ``aws.py::_athena_mask``); ``nullify`` is NULL.
  The two text rules take a string column; ``nullify`` takes any.
* One mask per column (the Unity Catalog limit) combines every restriction on it; a
  principal in a deny and a mask gets the deny. Everyone else reads the value.
* One row filter per table (the same limit). A reader no filter names reads every row;
  a member of several filtered principals reads the union of their predicates.
* ``access: allow`` is refused: it would blind every reader the allow does not name,
  the table's owner and its pipelines included.

The functions are created once under names that carry a hash of their definition
(``fluid_rf_<table>_<hash8>``, ``fluid_mask_<table>_<column>_<hash8>``). Unity Catalog
refuses to replace a function a live filter or mask uses, so a changed definition is a new
function, bound in place of the old one; the same contract always names the same function,
which is how a second run finds nothing to do and ``fluid verify`` knows what to expect.

A predicate is contract text that becomes a function body, so it is parsed (sqlglot,
Databricks dialect) as one boolean expression, checked node by node against an allowlist
and written back from the tree, never spliced. What the allowlist holds: comparisons,
``AND`` / ``OR`` / ``NOT``, ``IS [NOT] NULL``, ``IN``, ``BETWEEN``, ``LIKE`` / ``ILIKE``,
``+ - *``, literals, ``CAST``, columns of the table, and the scalar functions
:data:`ALLOWED_FUNCTIONS`: pure and deterministic over the row's own values. Anything that
reads session or other-table state (``current_user``, ``is_member``, a subquery), or that a
reader could use to learn something the row does not hold, is refused.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...providers._sql_safety import reject_control_chars, validate_ident
from ..base import UnsupportedBindingError
from ..column_access import DENY, MASK, MASK_RULES, restrictions_for
from ..principals import DATABRICKS, logical_key, principal_map, resolve_principal
from ..row_access import row_filters_for

#: Function name prefixes. A live filter or mask whose function carries one was made here,
#: so only those are ever dropped: a filter an admin set by hand is theirs.
ROW_FILTER_PREFIX = "fluid_rf_"
MASK_PREFIX = "fluid_mask_"

#: The scalar functions a row filter may call, each pure and deterministic over its
#: arguments (sqlglot class names).
ALLOWED_FUNCTIONS = ("lower", "upper", "length", "trim", "coalesce", "abs")

_STRING_TYPES = frozenset({"string", "varchar", "char"})
_MAX_NAME = 128
_MASK_TEXT = "XXXXX"

GROUP = "group"
USER = "user"
SERVICE_PRINCIPAL = "serviceAccount"


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-{kind}", message, tuple(remediation))


# ── results ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Function:
    """A Unity Catalog SQL function to create, named by its definition."""

    #: ``catalog.schema.name``, lower-case, unquoted.
    full_name: str
    #: The ``CREATE FUNCTION IF NOT EXISTS`` statement.
    create: str


@dataclass(frozen=True)
class RowFilterSpec:
    function: Function
    #: The columns the function takes, in parameter order: ``ON (...)``.
    columns: Tuple[str, ...]
    bind: str


@dataclass(frozen=True)
class MaskSpec:
    column: str
    function: Function
    bind: str


@dataclass(frozen=True)
class TableEnforcement:
    """What a table's contract says Unity Catalog must enforce on it."""

    full_name: str
    row_filter: Optional[RowFilterSpec]
    masks: Tuple[MaskSpec, ...]
    #: Every identity a restriction names: the ones to revoke ``SELECT`` from if a binding
    #: cannot be made (:func:`revoke_statements`).
    restricted: Tuple[str, ...]


# ── SQL helpers ────────────────────────────────────────────────────────


def quote(name: str) -> str:
    """A plain identifier, backtick-quoted (``validate_ident`` first: nothing else gets in)."""
    return "`" + validate_ident(name) + "`"


def quote_principal(name: str) -> str:
    """A principal name as a quoted identifier (a user's email is no plain identifier)."""
    reject_control_chars(name, "principal")
    return "`" + name.replace("`", "``") + "`"


def quote_table(full_name: str) -> str:
    return ".".join(quote(part) for part in full_name.split("."))


def revoke_statements(full_name: str, principals: Sequence[str]) -> List[str]:
    """``REVOKE SELECT`` on the table from each principal, the fail-safe of a failed binding."""
    return [
        f"REVOKE SELECT ON TABLE {quote_table(full_name)} FROM {quote_principal(p)}"
        for p in principals
    ]


def unbind_row_filter(full_name: str) -> str:
    return f"ALTER TABLE {quote_table(full_name)} DROP ROW FILTER"


def unbind_mask(full_name: str, column: str) -> str:
    return f"ALTER TABLE {quote_table(full_name)} ALTER COLUMN {quote(column)} DROP MASK"


def is_forge_function(function_name: str, prefix: str) -> bool:
    """Whether a live function (full name, any quoting or case) was made by this module."""
    last = str(function_name or "").replace("`", "").rsplit(".", 1)[-1].lower()
    return last.startswith(prefix)


def same_function(live: Any, expected: str) -> bool:
    return str(live or "").replace("`", "").lower() == expected.lower()


def _digest(definition: str) -> str:
    return hashlib.sha256(definition.encode("utf-8")).hexdigest()[:8]


def _function_name(prefix: str, stem: str, definition: str) -> str:
    stem = stem[: _MAX_NAME - len(prefix) - 9]
    return f"{prefix}{stem}_{_digest(definition)}"


def _function(
    catalog: str, schema: str, prefix: str, stem: str, params: str, returns: str, body: str
) -> Function:
    definition = f"({params}) RETURNS {returns} RETURN {body}"
    name = _function_name(prefix, stem, definition)
    full = f"{catalog}.{schema}.{name}"
    return Function(
        full_name=full,
        create=f"CREATE FUNCTION IF NOT EXISTS {quote_table(full)}{definition}",
    )


# ── principals ─────────────────────────────────────────────────────────


def _kind(principal: str, at: str) -> str:
    """How a logical principal is recognised in SQL, from its prefix.

    ``group:`` is an account group (``is_account_group_member``), ``user:`` a user
    (``current_user()``), ``serviceAccount:`` a service principal (``current_user()`` is its
    application ID). A principal with no prefix, or ``domain:``, names no one SQL can match.
    """
    head, sep, rest = logical_key(principal).partition(":")
    if sep and rest.strip():
        if head == "group":
            return GROUP
        if head == "user":
            return USER
        if head == "serviceAccount":
            return SERVICE_PRINCIPAL
    raise _refusal(
        "principal-kind",
        f"{at} names principal {principal!r}, which is not a group:, user: or serviceAccount: "
        "principal, so there is no Unity Catalog SQL test for it.",
        "Write the principal as group:<name>, user:<email> or serviceAccount:<name>, and map "
        "it in binding.principals to the Databricks identity.",
    )


def string_literal(value: str) -> str:
    """``value`` as a Databricks SQL string literal.

    Spark SQL escapes with a backslash and has no ``''``: ``'it''s'`` is the two literals
    ``'it'`` and ``'s'``, which concatenate to ``its``, so the doubling
    ``_sql_safety.quote_string_literal`` does would silently turn ``o'brien@x.com`` into a
    name no one has, and the restriction would never match. Backslash first, then the quote;
    a control character is refused.
    """
    reject_control_chars(value, "string literal")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _member_sql(kind: str, identity: str) -> str:
    reject_control_chars(identity, "principal")
    literal = string_literal(identity)
    if kind == GROUP:
        return f"is_account_group_member({literal})"
    return f"current_user() = {literal}"


class _Resolver:
    """Logical principals to ``[(kind, identity)]``, through ``binding.principals``."""

    def __init__(self, binding: Mapping[str, Any], where: str):
        self._map = principal_map(binding)
        self._where = where

    def __call__(self, principal: str, at: str) -> Tuple[Tuple[str, str], ...]:
        kind = _kind(principal, at)
        identities = resolve_principal(principal, self._map, platform=DATABRICKS, where=self._where)
        return tuple((kind, identity) for identity in identities)


def _any_member(members: Sequence[Tuple[str, str]]) -> str:
    terms = sorted({_member_sql(kind, identity) for kind, identity in members})
    return "(" + " OR ".join(terms) + ")"


# ── predicates ─────────────────────────────────────────────────────────


def render_predicate(
    where_text: str,
    columns: Mapping[str, str],
    at: str,
    types: Optional[Mapping[str, str]] = None,
) -> Tuple[str, Tuple[str, ...]]:
    """``(databricks SQL, columns read)`` for a predicate, or a ``row-filter-invalid`` refusal.

    ``columns`` is ``{lower-case name: table column name}`` and ``types`` ``{column name:
    type}``, which a predicate that is a lone column must have as BOOLEAN. The SQL is written from the
    parsed tree with every column back-quoted, then parsed again: it must give the same
    text, or it is refused.
    """
    import sqlglot
    from sqlglot import expressions as exp

    def invalid(why: str) -> UnsupportedBindingError:
        return _refusal(
            "row-filter-invalid",
            f"{at}.where {why}",
            "Write one boolean expression over the table's columns, e.g. "
            "consent_data_analytics = true AND (is_suppressed IS NULL OR is_suppressed = false).",
        )

    text = where_text.strip()
    for token in (";", "--", "/*", "*/"):
        if token in text:
            raise invalid(
                f"holds {token!r}: a statement separator or a comment. A row filter is one "
                "boolean expression and nothing else."
            )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise invalid("holds a control character.")
    try:
        statements = sqlglot.parse(text, read="databricks")
    except sqlglot.errors.SqlglotError as exc:
        raise invalid(f"does not parse as Databricks SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1 or statements[0] is None:
        raise invalid("is not exactly one expression.")
    tree = statements[0]

    functions = {
        getattr(exp, name.capitalize())
        for name in ALLOWED_FUNCTIONS
        if hasattr(exp, name.capitalize())
    }
    allowed = {
        exp.And, exp.Or, exp.Not, exp.Paren, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.Null, exp.Boolean, exp.Literal, exp.Column,
        exp.Identifier, exp.In, exp.Between, exp.Like, exp.ILike, exp.Neg, exp.Add, exp.Sub,
        exp.Mul, exp.Cast, exp.DataType, exp.DataTypeParam, *functions,
    }  # fmt: skip
    for node in tree.walk():
        if type(node) not in allowed:
            raise invalid(
                f"uses {type(node).__name__} ({node.sql(dialect='databricks')[:40]!r}), which a "
                "row filter may not: a subquery, a statement, a session function such as "
                "current_user() or is_member(), or a function outside "
                f"{', '.join(ALLOWED_FUNCTIONS)}."
            )
    inner = tree
    while isinstance(inner, exp.Paren):
        inner = inner.this
    boolean = (
        exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.NullSafeEQ, exp.NullSafeNEQ, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike,
        exp.Boolean, exp.Column,
    )  # fmt: skip
    if not isinstance(inner, boolean):
        raise invalid("is not a boolean expression.")
    if isinstance(inner, exp.Column) and types is not None:
        canonical = columns.get(inner.name.lower())
        if canonical is not None and str(types.get(canonical, "")).lower() != "boolean":
            raise invalid(f"is the column {inner.name!r}, which is not BOOLEAN.")

    used: Dict[str, None] = {}

    def requote(node: Any) -> Any:
        if not isinstance(node, exp.Column):
            return node
        if node.table or node.db or node.args.get("catalog"):
            raise invalid(f"qualifies column {node.name!r}; name the table's columns alone.")
        canonical = columns.get(node.name.lower())
        if canonical is None:
            raise invalid(f"reads {node.name!r}, which the table's schema does not declare.")
        used.setdefault(canonical, None)
        return exp.column(canonical, quoted=True)

    tree = tree.transform(requote)
    if not used:
        raise invalid("reads no column, so it would keep or hide every row for everyone.")
    rendered = tree.sql(dialect="databricks")
    again = sqlglot.parse(rendered, read="databricks")
    if len(again) != 1 or again[0] is None or again[0].sql(dialect="databricks") != rendered:
        raise invalid("does not render back to the same expression, so it is refused.")
    return rendered, tuple(used)


# ── the table ──────────────────────────────────────────────────────────


def _mask_value(rule: str, column_type: str) -> str:
    """The masked value of ``val``, for a rule (see the module docstring)."""
    if rule == "nullify":
        return f"CAST(NULL AS {column_type.upper()})"
    shown = (
        f"concat('{_MASK_TEXT}', right(`val`, 4))"
        if rule == "last_four"
        else (f"concat(left(`val`, 4), '{_MASK_TEXT}')")
    )
    return (
        "CASE WHEN `val` IS NULL THEN CAST(NULL AS STRING) "
        f"WHEN length(`val`) <= 4 THEN sha2(`val`, 256) ELSE {shown} END"
    )


def compile_enforcement(
    *,
    where: str,
    index: int,
    exposure: Mapping[str, Any],
    binding: Mapping[str, Any],
    catalog: str,
    schema: str,
    table: str,
    columns: Sequence[Mapping[str, Any]],
) -> Optional[TableEnforcement]:
    """The expose's mask and row-filter enforcement, ``None`` if it declares none.

    Raises :class:`UnsupportedBindingError` (``databricks-...``) for what Unity Catalog
    cannot express; it never drops a restriction.
    """
    full_name = f"{catalog}.{schema}.{table}"
    types = {str(c["name"]): str(c["type"]) for c in columns}
    lower = {name.lower(): name for name in types}
    resolve = _Resolver(binding, where)
    try:
        restrictions = restrictions_for(exposure, index)
    except UnsupportedBindingError as exc:
        raise _refusal("column-restriction-invalid", f"{where}: {exc}", *exc.remediation) from exc
    try:
        filters = row_filters_for(exposure, index)
    except UnsupportedBindingError as exc:
        raise _refusal("row-filter-invalid", f"{where}: {exc}", *exc.remediation) from exc
    except Exception as exc:  # noqa: BLE001 — the shared reader's parser can raise its own
        # tokenizer errors on text it cannot read (a backslash-escaped quote, say): refused.
        raise _refusal(
            "row-filter-invalid",
            f"{where}: a row filter predicate could not be read as SQL ({type(exc).__name__}).",
            "Write each predicate as one boolean expression over the table's columns.",
        ) from exc
    if not restrictions and not filters:
        return None

    restricted: Dict[str, None] = {}
    masks = _compile_masks(
        where, catalog, schema, table, full_name, types, lower, restrictions, resolve, restricted
    )
    row_filter = _compile_row_filter(
        where, catalog, schema, table, full_name, types, lower, filters, resolve, restricted
    )
    return TableEnforcement(
        full_name=full_name,
        row_filter=row_filter,
        masks=masks,
        restricted=tuple(sorted(restricted)),
    )


def _compile_masks(
    where: str,
    catalog: str,
    schema: str,
    table: str,
    full_name: str,
    types: Mapping[str, str],
    lower: Mapping[str, str],
    restrictions: Sequence[Any],
    resolve: _Resolver,
    restricted: Dict[str, None],
) -> Tuple[MaskSpec, ...]:
    denied: Dict[str, List[Tuple[str, str]]] = {}
    masked: Dict[str, List[Tuple[str, str]]] = {}
    rules: Dict[str, str] = {}
    for i, restriction in enumerate(restrictions):
        at = f"{where}.policy.authz.columnRestrictions[{i}]"
        if restriction.access == DENY:
            target = denied
        elif restriction.access == MASK and restriction.mask in MASK_RULES:
            target = masked
        else:
            raise _refusal(
                "column-allow-unmapped",
                f"{at} is access: {restriction.access}. An allow list would hide the column "
                "from every reader it does not name, the table's owner and its pipelines "
                "included, and Unity Catalog has no column grant to express it.",
                "Use access: deny for the principals that must not read the columns, or "
                "access: mask to let them read them masked.",
            )
        members = resolve(restriction.principal, at)
        for _kind_, identity in members:
            restricted.setdefault(identity, None)
        for column in restriction.columns:
            canonical = lower[column.lower()]
            target.setdefault(canonical, []).extend(members)
            if restriction.access == MASK:
                rules[canonical] = restriction.mask
    out: List[MaskSpec] = []
    for column in types:  # the table's column order
        deny_members, mask_members = denied.get(column, []), masked.get(column, [])
        if not (deny_members or mask_members):
            continue
        column_type = types[column]
        rule = rules.get(column)
        if mask_members and rule != "nullify" and column_type.lower() not in _STRING_TYPES:
            raise _refusal(
                "mask-type",
                f"{where} masks {column} ({column_type}) by {rule}, which masks text.",
                "Mask a string column, or use mask: nullify.",
            )
        branches: List[str] = []
        if deny_members:
            branches.append(
                f"WHEN {_any_member(deny_members)} THEN CAST(NULL AS {column_type.upper()})"
            )
        if mask_members and rule is not None:
            branches.append(
                f"WHEN {_any_member(mask_members)} THEN {_mask_value(rule, column_type)}"
            )
        body = "CASE " + " ".join(branches) + " ELSE `val` END"
        function = _function(
            catalog, schema, MASK_PREFIX, f"{table}_{column}", f"`val` {column_type.upper()}",
            column_type.upper(), body,
        )  # fmt: skip
        out.append(
            MaskSpec(
                column=column,
                function=function,
                bind=(
                    f"ALTER TABLE {quote_table(full_name)} ALTER COLUMN {quote(column)} "
                    f"SET MASK {quote_table(function.full_name)}"
                ),
            )
        )
    return tuple(out)


def _compile_row_filter(
    where: str,
    catalog: str,
    schema: str,
    table: str,
    full_name: str,
    types: Mapping[str, str],
    lower: Mapping[str, str],
    filters: Sequence[Any],
    resolve: _Resolver,
    restricted: Dict[str, None],
) -> Optional[RowFilterSpec]:
    if not filters:
        return None
    parts: List[Tuple[str, str]] = []  # (membership SQL, predicate SQL)
    used: Dict[str, None] = {}
    all_members: List[Tuple[str, str]] = []
    for i, entry in enumerate(filters):
        at = f"{where}.policy.authz.rowFilters[{i}]"
        members = resolve(entry.principal, at)
        if not members:
            continue  # mapped to no identity on Databricks: nothing to restrict there
        sql, columns = render_predicate(entry.where, lower, at, types)
        for column in columns:
            used.setdefault(column, None)
        for _kind_, identity in members:
            restricted.setdefault(identity, None)
        all_members.extend(members)
        parts.append((_any_member(members), sql))
    if not parts:
        return None
    ordered = tuple(c for c in types if c in used)  # the table's column order
    body = " OR ".join(
        [f"NOT {_any_member(all_members)}"]
        + [f"({member} AND ({predicate}))" for member, predicate in sorted(parts)]
    )
    params = ", ".join(f"{quote(c)} {types[c].upper()}" for c in ordered)
    function = _function(catalog, schema, ROW_FILTER_PREFIX, table, params, "BOOLEAN", body)
    return RowFilterSpec(
        function=function,
        columns=ordered,
        bind=(
            f"ALTER TABLE {quote_table(full_name)} SET ROW FILTER "
            f"{quote_table(function.full_name)} ON ({', '.join(quote(c) for c in ordered)})"
        ),
    )


__all__ = [
    "ALLOWED_FUNCTIONS",
    "MASK_PREFIX",
    "ROW_FILTER_PREFIX",
    "Function",
    "MaskSpec",
    "RowFilterSpec",
    "TableEnforcement",
    "compile_enforcement",
    "is_forge_function",
    "quote_table",
    "render_predicate",
    "revoke_statements",
    "string_literal",
    "same_function",
    "unbind_mask",
    "unbind_row_filter",
]
