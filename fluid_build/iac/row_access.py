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

"""Row filters: which rows a principal may read, one derivation for every cloud.

``exposes[].policy.authz.rowFilters[]`` (``{principal, where, name?}``, fluid-schema
0.7.6) lets its principal read only the rows its ``where`` predicate selects. A reader
no filter names reads every row, and a principal has at most one filter per expose.
Each emitter writes the filters in its platform's own terms:

* GCP BigQuery: a row access policy per filter (``google_bigquery_row_access_policy``,
  ``filter_predicate`` and ``grantees``), and one more that selects every row for
  every other reader and writer of the table, because BigQuery shows a principal that
  no row access policy names no row at all (its row-level security docs).
* AWS Glue: a Lake Formation data cells filter (``aws_lakeformation_data_cells_filter``,
  ``row_filter.filter_expression``, the principal's excluded columns as its column
  wildcard's exclusions), and the principal's ``SELECT`` is granted on the filter
  instead of on the table.

The predicate is one SQL boolean expression over the expose's columns. It is parsed
(sqlglot) rather than trusted: a statement separator, a comment or a column the schema
does not declare is refused, so the text written into the platform's policy is the
predicate and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Tuple

from .base import UnsupportedBindingError
from .column_access import policy_where

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


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


def _where(exposure: Mapping[str, Any], index: int) -> str:
    return policy_where(exposure, index, "rowFilters")


def _derived_name(principal: str) -> str:
    """``analysts_rows`` for ``group:analysts@northwind.example``."""
    local = principal.split(":", 1)[-1].split("@", 1)[0]
    stem = re.sub(r"[^a-z0-9]+", "_", local.lower()).strip("_") or "principal"
    if not stem[0].isalpha():
        stem = f"p_{stem}"
    return f"{stem[:56]}_rows"


def _predicate_columns(where: str, at: str) -> Tuple[str, ...]:
    """The columns ``where`` reads; a predicate that is not one boolean expression is refused."""
    if ";" in where or "--" in where or "/*" in where or "*/" in where:
        raise UnsupportedBindingError(
            "row-filter",
            f"{at}.where holds a statement separator or a comment; a row filter is one "
            "boolean expression, written into the platform's policy as it stands.",
            ("Write the predicate alone, e.g. consent_data_analytics = true.",),
        )
    try:
        import sqlglot
        from sqlglot import expressions as exp
    except ImportError:  # pragma: no cover - sqlglot is a core dependency
        return ()
    try:
        tree = sqlglot.parse_one(f"SELECT 1 FROM t WHERE {where}")
    except sqlglot.errors.ParseError as exc:
        raise UnsupportedBindingError(
            "row-filter",
            f"{at}.where does not parse as a SQL boolean expression: {str(exc).splitlines()[0]}",
            ("Write one boolean expression over the expose's columns.",),
        ) from exc
    clause = tree.args.get("where")
    if (
        not isinstance(tree, exp.Select)
        or clause is None
        or tree.args.get("group")
        or tree.args.get("order")
        or tree.args.get("limit")
        or tree.find(exp.Subquery, exp.Union)
    ):
        raise UnsupportedBindingError(
            "row-filter",
            f"{at}.where must be one boolean expression over the expose's columns, with no "
            "subquery, so the platform evaluates it on each row by itself.",
            ("Write the predicate alone, e.g. consent_data_analytics = true.",),
        )
    return tuple(dict.fromkeys(col.name for col in clause.find_all(exp.Column)))


def row_filters_for(exposure: Mapping[str, Any], index: int = 0) -> Tuple[RowFilter, ...]:
    """The expose's row filters, or ``()``; a malformed one is refused."""
    policy = exposure.get("policy") if isinstance(exposure, Mapping) else None
    authz = policy.get("authz") if isinstance(policy, Mapping) else None
    raw = authz.get("rowFilters") if isinstance(authz, Mapping) else None
    if not raw:
        return ()
    where_at = _where(exposure, index)
    if not isinstance(raw, list):
        raise UnsupportedBindingError("row-filter", f"{where_at} must be a list of filters.", ())
    schema = (exposure.get("contract") or {}).get("schema") or []
    declared = {col.get("name") for col in schema if isinstance(col, Mapping)}
    out: List[RowFilter] = []
    seen_principals: set[str] = set()
    seen_names: set[str] = set()
    for i, entry in enumerate(raw):
        at = f"{where_at}[{i}]"
        if not isinstance(entry, Mapping):
            raise UnsupportedBindingError("row-filter", f"{at} must be a mapping.", ())
        principal = entry.get("principal")
        where = entry.get("where")
        if not isinstance(principal, str) or not principal.strip():
            raise UnsupportedBindingError(
                "row-filter",
                f"{at} names no principal, so there is no one to filter.",
                ("Set principal to the logical principal the filter applies to.",),
            )
        if not isinstance(where, str) or not where.strip():
            raise UnsupportedBindingError(
                "row-filter",
                f"{at} has no where predicate, so it selects nothing.",
                ("Set where to one SQL boolean expression over the expose's columns.",),
            )
        principal, where = principal.strip(), where.strip()
        if principal in seen_principals:
            raise UnsupportedBindingError(
                "row-filter",
                f"{at} filters {principal} a second time; a principal has one row filter per "
                "expose, which the platform applies as it stands.",
                ("Combine the two predicates with AND into one filter.",),
            )
        seen_principals.add(principal)
        name = entry.get("name") or _derived_name(principal)
        if not isinstance(name, str) or not _NAME.match(name):
            raise UnsupportedBindingError(
                "row-filter",
                f"{at}.name {name!r} must be lowercase letters, digits and underscores, "
                "starting with a letter, at most 63 characters.",
                (),
            )
        if name in seen_names:
            raise UnsupportedBindingError(
                "row-filter", f"{at}.name {name!r} names another filter of this expose too.", ()
            )
        seen_names.add(name)
        columns = _predicate_columns(where, at)
        unknown = [c for c in columns if c not in declared]
        if unknown:
            raise UnsupportedBindingError(
                "row-filter",
                f"{at}.where reads {unknown}, which the expose's schema does not declare.",
                ("Name columns of exposes[].contract.schema in the predicate.",),
            )
        out.append(RowFilter(principal=principal, where=where, name=name, columns=columns))
    return tuple(out)


__all__ = ["RowFilter", "row_filters_for"]
