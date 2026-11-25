# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Row filters: one parse of ``exposes[].policy.authz.rowFilters[]`` for every consumer.

``exposes[].policy.authz.rowFilters[]`` (``{principal, where, name?}``, fluid-schema
0.7.6) lets its principal read only the rows its ``where`` predicate selects. A reader
no filter names reads every row, and a principal has at most one filter per expose.

Two consumers read the filters through :func:`parse_row_filters`:

* the IaC emitters (``fluid_build.iac.row_access``), which write each filter into the
  platform's own policy (a BigQuery row access policy, a Lake Formation data cells
  filter, a Unity Catalog row filter);
* the MCP output-port gateway (``fluid_build.output_ports.mcp.query_compiler``), which
  applies the filters itself on every read it serves.

So the filter a platform enforces and the filter the gateway enforces are the same
filter. The predicate is parsed with sqlglot rather than trusted: a statement
separator, a comment, a subquery, a set operation, any clause besides the WHERE
(``GROUP BY``, ``HAVING``, ``QUALIFY``, ``WINDOW``, ``ORDER BY``, ``LIMIT``), a bind
placeholder or a column the schema does not declare is refused. What a policy holds
is then one boolean expression over the expose's own columns, evaluated on each row
by itself.

Only the standard library is imported at module scope, because this module sits on
the gateway's import path next to ``_sql_safety``; sqlglot is imported when a
predicate is parsed. Without sqlglot a predicate is refused, never passed unchecked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Collection, List, Mapping, Optional, Tuple

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}\Z")

#: The clauses of ``SELECT 1 FROM t WHERE <predicate>`` a predicate leaves set. sqlglot
#: names the FROM ``from_`` in current releases and ``from`` in older ones.
_PREDICATE_CLAUSES = frozenset({"expressions", "from", "from_", "where"})

_ONE_EXPRESSION_HINT = ("Write the predicate alone, e.g. consent_data_analytics = true.",)


@dataclass(frozen=True)
class RowFilter:
    """One ``rowFilters[]`` entry, validated."""

    principal: str
    #: The predicate, exactly as written (trimmed).
    where: str
    #: The filter's name on the platform: ``name``, or derived from the principal.
    name: str
    #: The columns the predicate reads, in the order it names them.
    columns: Tuple[str, ...] = ()


class RowFilterError(ValueError):
    """A ``rowFilters`` value that cannot be applied as written.

    ``hints`` are the concrete next steps. Each consumer raises its own error from it:
    the IaC emitters an ``UnsupportedBindingError``, the MCP gateway a refusal of the
    request.
    """

    def __init__(self, message: str, hints: Tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.hints = hints


def derived_name(principal: str) -> str:
    """``analysts_rows`` for ``group:analysts@northwind.example``."""
    local = principal.split(":", 1)[-1].split("@", 1)[0]
    stem = re.sub(r"[^a-z0-9]+", "_", local.lower()).strip("_") or "principal"
    if not stem[0].isalpha():
        stem = f"p_{stem}"
    return f"{stem[:56]}_rows"


def parse_predicate(where: str, at: str, *, dialect: Optional[str] = None) -> Any:
    """``where`` parsed as the WHERE of ``SELECT 1 FROM t``, as ``dialect`` reads it.

    ``dialect`` is a sqlglot dialect name; ``None`` is sqlglot's default reading, the
    one the IaC emitters check a predicate with. Returns the parsed statement (its
    ``args["where"]`` holds the predicate). A predicate that is not one boolean
    expression over plain columns is refused with :class:`RowFilterError`.
    """
    if ";" in where or "--" in where or "/*" in where or "*/" in where:
        raise RowFilterError(
            f"{at}.where holds a statement separator or a comment; a row filter is one "
            "boolean expression, written into the platform's policy as it stands.",
            _ONE_EXPRESSION_HINT,
        )
    try:
        from sqlglot import expressions as exp
        from sqlglot import parse_one
        from sqlglot.errors import SqlglotError
    except ImportError:  # pragma: no cover - sqlglot is a base dependency
        raise RowFilterError(
            f"{at}.where cannot be checked: sqlglot, a base dependency of forge-cli, is "
            "not installed, and an unchecked predicate is never applied.",
            ("Reinstall data-product-forge with its base dependencies.",),
        ) from None
    try:
        tree = parse_one(f"SELECT 1 FROM t WHERE {where}", read=dialect)
    except RecursionError:
        raise RowFilterError(
            f"{at}.where nests too deeply to be checked.",
            ("Write the predicate with fewer nested parentheses.",),
        ) from None
    except SqlglotError as exc:
        # A tokenizer error (an unterminated quote) is not a ParseError, so the base
        # class is caught: every unreadable predicate is refused the same way.
        raise RowFilterError(
            f"{at}.where does not parse as a SQL boolean expression: {str(exc).splitlines()[0]}",
            ("Write one boolean expression over the expose's columns.",),
        ) from exc
    set_operation = getattr(exp, "SetOperation", exp.Union)
    if (
        not isinstance(tree, exp.Select)
        or tree.args.get("where") is None
        or any(value for key, value in tree.args.items() if key not in _PREDICATE_CLAUSES)
        or sum(1 for _ in tree.find_all(exp.Select)) != 1
        or tree.find(exp.Subquery, set_operation) is not None
    ):
        raise RowFilterError(
            f"{at}.where must be one boolean expression over the expose's columns, with no "
            "subquery, so the platform evaluates it on each row by itself.",
            _ONE_EXPRESSION_HINT,
        )
    placeholders = tuple(
        node
        for node in (
            exp.Placeholder,
            exp.Parameter,
            getattr(exp, "SessionParameter", None),
        )
        if node is not None
    )
    if tree.find(*placeholders) is not None:
        raise RowFilterError(
            f"{at}.where holds a bind placeholder; a row filter is applied as written, so "
            "every value in it is a literal.",
            ("Write the value itself, e.g. region = 'EU'.",),
        )
    return tree


def predicate_columns(tree: Any) -> Tuple[str, ...]:
    """The columns a :func:`parse_predicate` result reads, in the order it names them."""
    from sqlglot import expressions as exp

    return tuple(dict.fromkeys(col.name for col in tree.args["where"].find_all(exp.Column)))


def parse_row_filters(
    raw: Any, *, declared: Collection[Any], where_at: str
) -> Tuple[RowFilter, ...]:
    """The filters ``raw`` (an expose's ``policy.authz.rowFilters``) declares, or ``()``.

    ``declared`` holds the expose's ``contract.schema`` column names, and ``where_at``
    names the list in a refusal (``exposes[<id>].policy.authz.rowFilters``). A value
    that is not a list of filters, or a filter that cannot be applied as written, is
    refused with :class:`RowFilterError`: a filter skipped would serve the rows it
    was written to withhold.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RowFilterError(f"{where_at} must be a list of filters.")
    out: List[RowFilter] = []
    seen_principals: set[str] = set()
    seen_names: set[str] = set()
    for i, entry in enumerate(raw):
        at = f"{where_at}[{i}]"
        if not isinstance(entry, Mapping):
            raise RowFilterError(f"{at} must be a mapping.")
        principal = entry.get("principal")
        where = entry.get("where")
        if not isinstance(principal, str) or not principal.strip():
            raise RowFilterError(
                f"{at} names no principal, so there is no one to filter.",
                ("Set principal to the logical principal the filter applies to.",),
            )
        if not isinstance(where, str) or not where.strip():
            raise RowFilterError(
                f"{at} has no where predicate, so it selects nothing.",
                ("Set where to one SQL boolean expression over the expose's columns.",),
            )
        principal, where = principal.strip(), where.strip()
        if principal in seen_principals:
            raise RowFilterError(
                f"{at} filters {principal} a second time; a principal has one row filter per "
                "expose, which the platform applies as it stands.",
                ("Combine the two predicates with AND into one filter.",),
            )
        seen_principals.add(principal)
        name = entry.get("name") or derived_name(principal)
        if not isinstance(name, str) or not _NAME.match(name):
            raise RowFilterError(
                f"{at}.name {name!r} must be lowercase letters, digits and underscores, "
                "starting with a letter, at most 63 characters."
            )
        if name in seen_names:
            raise RowFilterError(f"{at}.name {name!r} names another filter of this expose too.")
        seen_names.add(name)
        columns = predicate_columns(parse_predicate(where, at))
        unknown = [c for c in columns if c not in declared]
        if unknown:
            raise RowFilterError(
                f"{at}.where reads {unknown}, which the expose's schema does not declare.",
                ("Name columns of exposes[].contract.schema in the predicate.",),
            )
        out.append(RowFilter(principal=principal, where=where, name=name, columns=columns))
    return tuple(out)


__all__ = [
    "RowFilter",
    "RowFilterError",
    "derived_name",
    "parse_predicate",
    "parse_row_filters",
    "predicate_columns",
]
