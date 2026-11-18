# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""FLUID SQL/1: the expression language of a contract's rules about valid data.

``RFC-data-rules.md`` §2 defines it: a closed SQL expression subset, two-valued,
with exact numbers, in which ``check``, ``expr``, ``when`` and
``aggregate.filter`` are written (schema 0.7.7). ``fluid-sql-vectors.json``, next
to this module, is the conformance suite that forge-cli and flux-expr/2 both run.

- :func:`parse` reads a text into its tree, refusing anything the grammar does
  not derive (:class:`FluidSqlError` carries one of :data:`REASONS`).
- :func:`check` resolves its names against a :class:`Model` (one expose) and
  infers its type and static scale.
- :func:`evaluate` computes its value for a row, exactly.
- :func:`canonical` writes the canonical text; :func:`render_sql` the SQL an engine
  runs (§2.8; one of :data:`DIALECTS`, :func:`render_duckdb` for DuckDB), and
  :func:`render_cel` the CEL a Confluent data-contract rule runs (refusing what has
  no exact form).

Standard library only, and never imported by ``fluid --help``.
"""

from __future__ import annotations

from ._canonical import canonical, quote_name, quote_string
from ._checker import PREDICATE, VALUE, Checked, check, check_derive_fit, check_derive_target
from ._errors import REASONS, FluidSqlError
from ._evaluate import add_months, evaluate, months_between, round_fraction, years_between
from ._lexer import FUNCTIONS, KEYWORDS, MAX_CODE_POINTS, MAX_DEPTH, MAX_ROUND_PLACES
from ._nodes import Node, from_json, to_json, walk
from ._parser import parse
from ._render import (
    DIALECTS,
    MAX_RENDERED_CHARS,
    RenderTooLarge,
    column_reader,
    exact_number_type,
    number_literal,
    render_duckdb,
    render_sql,
    sql_literal,
    sql_name,
)
from ._render_cel import CelUntranslatable, render_cel
from ._types import Column, ColumnType, Link, Model, SqlType, column_type

__all__ = [
    "DIALECTS",
    "MAX_RENDERED_CHARS",
    "RenderTooLarge",
    "column_reader",
    "exact_number_type",
    "number_literal",
    "render_sql",
    "sql_literal",
    "sql_name",
    "FUNCTIONS",
    "KEYWORDS",
    "MAX_CODE_POINTS",
    "MAX_DEPTH",
    "MAX_ROUND_PLACES",
    "PREDICATE",
    "REASONS",
    "VALUE",
    "CelUntranslatable",
    "Checked",
    "Column",
    "ColumnType",
    "FluidSqlError",
    "Link",
    "Model",
    "Node",
    "SqlType",
    "add_months",
    "canonical",
    "check",
    "check_derive_fit",
    "check_derive_target",
    "column_type",
    "evaluate",
    "from_json",
    "months_between",
    "parse",
    "quote_name",
    "quote_string",
    "render_cel",
    "render_duckdb",
    "round_fraction",
    "to_json",
    "walk",
    "years_between",
]
