# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

The filters are parsed by :mod:`fluid_build.providers._row_filters`, the parse the MCP
output-port gateway applies them with too, so a platform's policy and the gateway
enforce the same filter. The predicate is one SQL boolean expression over the
expose's columns, parsed (sqlglot) rather than trusted: a statement separator, a
comment, a subquery, a clause besides the WHERE, a bind placeholder or a column the
schema does not declare is refused, so the text written into the platform's policy is
the predicate and nothing else.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

from ..providers._row_filters import RowFilter, RowFilterError, parse_row_filters
from .base import UnsupportedBindingError
from .column_access import policy_where


def _where(exposure: Mapping[str, Any], index: int) -> str:
    return policy_where(exposure, index, "rowFilters")


def row_filters_for(
    exposure: Mapping[str, Any], index: int = 0, *, dialect: Optional[str] = None
) -> Tuple[RowFilter, ...]:
    """The expose's row filters, or ``()``; a malformed one is refused, and so is one the
    platform's engine (``dialect``, a sqlglot dialect name) would read with other
    columns than declared, since the predicate is written into its policy as it stands."""
    policy = exposure.get("policy") if isinstance(exposure, Mapping) else None
    authz = policy.get("authz") if isinstance(policy, Mapping) else None
    raw = authz.get("rowFilters") if isinstance(authz, Mapping) else None
    if raw is None:
        return ()
    schema = (exposure.get("contract") or {}).get("schema") or []
    names = (col.get("name") for col in schema if isinstance(col, Mapping))
    declared = {name for name in names if isinstance(name, str)}
    try:
        return parse_row_filters(
            raw, declared=declared, where_at=_where(exposure, index), dialect=dialect
        )
    except RowFilterError as exc:
        raise UnsupportedBindingError("row-filter", str(exc), exc.hints) from exc


__all__ = ["RowFilter", "row_filters_for"]
