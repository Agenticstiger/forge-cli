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

"""Compile an MCP ``query`` call into a parameterised SQL statement.

The consumer-side MCP server's ``query`` tool accepts predeclared
semantic-layer arguments — a metric or measure name, optional
dimension list, optional equality filters keyed by dimension. We
never give the LLM a raw-SQL surface by default; the compiler is the
only path from a tool-call payload to executed SQL.

The compiler enforces three guarantees:

1. **Identifiers are validated.** Every measure / dimension /
   column / table name flows through
   :func:`fluid_build.providers._sql_safety.validate_ident` or
   :func:`validate_sql_expression_allowlist` before being interpolated
   into the rendered SQL.
2. **Filter values are parameters.** Equality filter values are
   never interpolated as SQL literals. Drivers receive a ``params``
   dict and pass it to the engine driver's parameter-binding
   mechanism (``bigquery.QueryJobConfig.query_parameters``,
   ``snowflake.cursor.execute(sql, params)``,
   ``duckdb.execute(sql, params)``).
3. **Rendered SQL is allowlist-safe.** Even though every name is
   validated, the final string is run through
   :func:`validate_sql_expression_allowlist` as a defence-in-depth
   net so a bug in the renderer can't smuggle a banned token (DROP /
   DELETE / etc.) through.

The free-form ``query_sql`` path (gated by ``--allow-sql``) goes
through :func:`compile_free_form_sql` instead, which adds a stricter
SELECT-only check on top of the allowlist.

Borrow-before-build — intentional divergence (per /borrow-before-build):
    Surveyed **MetricFlow** (dbt-labs, Apache-2.0; the OSI / Open
    Semantic Interchange reference compiler) and **Cube**. MetricFlow
    is the canonical metrics→SQL compiler, but it hard-requires a
    *working dbt project + adapter*; Cube is a standalone server, not
    an embeddable library. Neither fits forge's model — the semantic
    spec lives inline in the contract's ``expose.semantics`` (no dbt
    project, no separate server), and the compiler must run in-process
    inside the gateway. So this is a deliberately *minimal*,
    contract-native metric/measure/dimension→SQL compiler (~500 LOC),
    NOT a general semantic layer.
    Interop note: ``expose.semantics`` should track the **OSI**
    (Open Semantic Interchange) spec shape where practical, so a
    contract's metrics stay portable to MetricFlow-compatible tools as
    that standard matures. Revisit adopting MetricFlow directly if
    forge ever assumes a dbt project is present.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from fluid_build.providers._sql_safety import (
    validate_ident,
    validate_sql_expression_allowlist,
)


class QueryValidationError(ValueError):
    """Raised when a ``query`` / ``query_sql`` payload fails INPUT
    validation — an unknown measure / metric / dimension, a bad filter
    key, a missing-or-duplicate metric-vs-measure choice, an
    out-of-range limit, a non-SELECT free-form statement, or a
    reference to a restricted column.

    Subclasses :class:`ValueError` so existing ``except ValueError`` /
    ``pytest.raises(ValueError)`` call sites keep working unchanged.

    The distinction matters at the MCP wire boundary. The server's tool
    dispatcher surfaces a ``QueryValidationError``'s message VERBATIM to
    the calling agent: every such message references only
    contract-declared names the agent can already see via the
    ``describe`` tool (measure / metric / dimension / column names,
    supported aggregations, the limit bound), so it leaks nothing and
    lets the agent self-correct its next call instead of looping
    blindly. Every OTHER exception — engine / driver failures, the
    ``table_reference`` allowlist guard, and the rendered-statement
    defence-in-depth sweep (whose message embeds the rendered SQL, and
    thus the binding's database / schema / table) — stays sanitised
    behind a generic "see server audit trail" message so binding and
    engine details never reach the model.
    """


class RowFilterIdentityMissing(RuntimeError):
    """Raised when a ``rowFilters[]`` entry references a caller attribute
    that is not present (fail-closed deny so the gateway never serves rows
    under an undefined identity)."""


_CALLER_PLACEHOLDER = re.compile(r"\$\{caller\.([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_caller_placeholder(value: Any, caller_attributes: Mapping[str, Any]) -> Any:
    """Replace ``${caller.<attr>}`` tokens in ``value`` with values from
    ``caller_attributes``. Lists / tuples recurse; non-string scalars pass
    through. Raises :class:`RowFilterIdentityMissing` when an attribute is
    absent so a misconfigured contract can never accidentally widen access."""
    if isinstance(value, str):
        match = _CALLER_PLACEHOLDER.fullmatch(value)
        if match is None:
            return value
        attr = match.group(1)
        if attr not in caller_attributes:
            raise RowFilterIdentityMissing(
                f"rowFilter references caller.{attr} but caller_attributes "
                f"only carry: {sorted(caller_attributes)}"
            )
        return caller_attributes[attr]
    if isinstance(value, (list, tuple)):
        return [_resolve_caller_placeholder(v, caller_attributes) for v in value]
    return value


def compile_row_filter_clauses(
    expose: Mapping[str, Any],
    caller_attributes: Mapping[str, Any],
    *,
    offset: int = 0,
) -> Tuple[List[str], List[Any]]:
    """Compile ``expose.policy.rowFilters[]`` into AND-able WHERE clauses +
    bound params, with placeholders ``:p_{offset}``, ``:p_{offset+1}``, …

    Centralises row-level security so EVERY read path enforces it: ``sample``
    (via :meth:`...drivers.base.EngineDriver.compile_row_filter_predicate`,
    ``offset=0``), the semantic ``query`` (merged into its WHERE at
    ``offset=len(existing_params)``), and the free-form ``query_sql`` wrapper.
    The ``offset`` is what lets these merge into a statement that already has
    ``:p_<n>`` placeholders without colliding.

    Fail-closed: a ``${caller.<attr>}`` referencing a missing attribute, or an
    ``in:`` filter resolving to an empty/non-list value, raises
    :class:`RowFilterIdentityMissing` — the gateway prefers no rows to wrong
    rows. Column identifiers route through ``validate_ident``; filter values are
    always bound parameters, never interpolated.
    """
    policy = expose.get("policy") or {}
    filters = policy.get("rowFilters") or []
    clauses: List[str] = []
    params: List[Any] = []
    for entry in filters:
        if not isinstance(entry, Mapping):
            continue
        column_raw = entry.get("column")
        if not isinstance(column_raw, str) or not column_raw:
            continue
        column = validate_ident(column_raw)
        if "equals" in entry:
            value = _resolve_caller_placeholder(entry["equals"], caller_attributes)
            clauses.append(f'"{column}" = :p_{offset + len(params)}')
            params.append(value)
        elif "in" in entry:
            raw_list = _resolve_caller_placeholder(entry["in"], caller_attributes)
            if not isinstance(raw_list, (list, tuple)) or not raw_list:
                raise RowFilterIdentityMissing(
                    f"row filter on column={column!r} expects a non-empty "
                    "list (got empty / non-list value); fail-closed deny."
                )
            placeholders = ", ".join(f":p_{offset + len(params) + i}" for i in range(len(raw_list)))
            clauses.append(f'"{column}" IN ({placeholders})')
            params.extend(raw_list)
        else:
            continue  # unknown operator — silently skip
    return clauses, params


@dataclass(frozen=True)
class CompiledQuery:
    """The result of compiling a semantic ``query`` payload.

    ``sql`` carries named-parameter placeholders in the form
    ``:p_<index>`` (DuckDB), ``@p_<index>`` (BigQuery), or
    ``%(p_<index>)s`` (Snowflake / DB-API). The driver picks the
    flavour at execution time via :meth:`render_sql_for_dialect` so
    the compiler stays driver-agnostic.

    ``params`` is an ordered list of parameter values. The list is
    aligned with the ``:p_<index>`` placeholders by index, so even a
    driver that prefers positional placeholders can use it directly.
    """

    sql: str
    """SQL with named placeholders ``:p_<index>``."""

    params: List[Any] = field(default_factory=list)
    """Parameter values, indexed by placeholder."""

    columns: List[str] = field(default_factory=list)
    """Column names in the SELECT projection (validated identifiers)."""

    def render_sql_for_dialect(self, dialect: str) -> str:
        """Re-render the SQL with the given dialect's placeholder
        syntax.

        The compiler emits portable ``:p_<index>`` placeholders;
        each driver dialect rewrites them to its native form:

        * ``duckdb`` — ``$p_<index>`` (DuckDB named parameters).
        * ``bigquery`` — ``@p_<index>`` (BigQuery named parameters).
        * ``snowflake`` — ``%(p_<index>)s`` (DB-API ``pyformat``).

        Unknown dialects fall back to the unrewritten ``:p_<index>``
        form so a future driver that prefers PEP-249 named style still
        receives something usable.
        """
        if dialect == "duckdb":
            out = self.sql
            for index in range(len(self.params)):
                out = out.replace(f":p_{index}", f"$p_{index}")
            return out
        if dialect == "bigquery":
            return self.sql.replace(":p_", "@p_")
        if dialect == "snowflake":
            out = self.sql
            for index in range(len(self.params)):
                out = out.replace(f":p_{index}", f"%(p_{index})s")
            return out
        return self.sql


@dataclass(frozen=True)
class _SemanticIndex:
    """Pre-indexed semantics + columns for one expose.

    Built once per :func:`compile_semantic_query` call. Splitting this
    out makes the validation steps easier to read and the unit tests
    easier to author against a known-good index.
    """

    measures: Dict[str, Dict[str, Any]]
    metrics: Dict[str, Dict[str, Any]]
    dimensions: Dict[str, Dict[str, Any]]
    columns: Dict[str, Dict[str, Any]]


def _index_expose(expose: Mapping[str, Any]) -> _SemanticIndex:
    """Build a name → definition lookup from an expose's semantics +
    contract schema.

    Both indexes are validated via :func:`validate_ident` so a
    malformed contract surfaces as a clean error before any SQL is
    rendered.
    """
    semantics = expose.get("semantics") or {}
    measures: Dict[str, Dict[str, Any]] = {}
    for definition in semantics.get("measures") or []:
        if not isinstance(definition, Mapping):
            continue
        name = definition.get("name")
        if not isinstance(name, str):
            continue
        validate_ident(name)
        measures[name] = dict(definition)
    metrics: Dict[str, Dict[str, Any]] = {}
    for definition in semantics.get("metrics") or []:
        if not isinstance(definition, Mapping):
            continue
        name = definition.get("name")
        if not isinstance(name, str):
            continue
        validate_ident(name)
        metrics[name] = dict(definition)
    dimensions: Dict[str, Dict[str, Any]] = {}
    for definition in semantics.get("dimensions") or []:
        if not isinstance(definition, Mapping):
            continue
        name = definition.get("name")
        if not isinstance(name, str):
            continue
        validate_ident(name)
        dimensions[name] = dict(definition)
    columns: Dict[str, Dict[str, Any]] = {}
    for column in (expose.get("contract") or {}).get("schema") or []:
        if not isinstance(column, Mapping):
            continue
        name = column.get("name")
        if not isinstance(name, str):
            continue
        validate_ident(name)
        columns[name] = dict(column)
    return _SemanticIndex(
        measures=measures, metrics=metrics, dimensions=dimensions, columns=columns
    )


def _resolve_metric(metric_name: str, index: _SemanticIndex) -> Tuple[str, Dict[str, Any]]:
    """Resolve a metric to (measure_name, measure_definition).

    Phase-1 supports ``simple`` metrics — the canonical case in
    dbt MetricFlow / Snowflake Semantic Views — which point at one
    measure. Derived and ratio metrics are intentionally rejected so
    we don't ship arithmetic over agent-supplied measures until
    Phase-2 hardens that path.
    """
    metric = index.metrics.get(metric_name)
    if metric is None:
        raise QueryValidationError(
            f"Unknown metric {metric_name!r}; "
            f"known: {sorted(index.metrics) or 'none in expose.semantics.metrics'}"
        )
    metric_type = metric.get("type") or "simple"
    if metric_type != "simple":
        raise QueryValidationError(
            f"Metric {metric_name!r} has type {metric_type!r}; "
            "Phase-1 query supports only 'simple' metrics. Use a measure "
            "directly or wait for Phase-2 derived/ratio support."
        )
    measure_name = metric.get("measure")
    if not isinstance(measure_name, str):
        raise QueryValidationError(f"Metric {metric_name!r} is missing a 'measure' reference")
    measure = index.measures.get(measure_name)
    if measure is None:
        raise QueryValidationError(
            f"Metric {metric_name!r} references unknown measure {measure_name!r}"
        )
    return measure_name, measure


def _resolve_dimension(dimension_name: str, index: _SemanticIndex) -> Tuple[str, str]:
    """Return (alias, sql_expression) for a dimension reference.

    A dimension can come from the semantic block (preferred) or fall
    back to a contract column. Either way the expression is built
    from validated identifiers.
    """
    dimension = index.dimensions.get(dimension_name)
    if dimension is not None:
        expr = dimension.get("expr") or dimension_name
        if not isinstance(expr, str):
            raise QueryValidationError(f"Dimension {dimension_name!r} has non-string expr")
        validate_sql_expression_allowlist(expr)
        return validate_ident(dimension_name), expr
    column = index.columns.get(dimension_name)
    if column is not None:
        return (
            validate_ident(dimension_name),
            validate_ident(dimension_name),
        )
    raise QueryValidationError(
        f"Unknown dimension {dimension_name!r}; "
        f"must be defined in expose.semantics.dimensions or contract.schema"
    )


_AGG_FUNCTIONS = {
    "sum": "SUM",
    "avg": "AVG",
    "count": "COUNT",
    "count_distinct": "COUNT",  # rendered with DISTINCT below
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "percentile": "PERCENTILE_CONT",
}


def _render_measure_expression(measure_name: str, measure: Mapping[str, Any]) -> str:
    """Render a measure into ``AGG(expr) AS measure_name``.

    The measure's ``expr`` is allowlist-validated. The
    ``count_distinct`` aggregation is rendered as ``COUNT(DISTINCT
    expr)``; ``percentile`` is left as ``PERCENTILE_CONT(expr)`` for
    Phase-1 (engine-specific syntax for percentile parameters lands
    in Phase-2).
    """
    agg_raw = measure.get("agg")
    if not isinstance(agg_raw, str) or agg_raw not in _AGG_FUNCTIONS:
        raise QueryValidationError(
            f"Measure {measure_name!r} has unsupported agg "
            f"{agg_raw!r}; supported: {sorted(_AGG_FUNCTIONS)}"
        )
    expr_raw = measure.get("expr") or measure_name
    if not isinstance(expr_raw, str):
        raise QueryValidationError(f"Measure {measure_name!r} has non-string expr")
    validate_sql_expression_allowlist(expr_raw)
    sql_func = _AGG_FUNCTIONS[agg_raw]
    if agg_raw == "count_distinct":
        return f"COUNT(DISTINCT {expr_raw}) AS {validate_ident(measure_name)}"
    return f"{sql_func}({expr_raw}) AS {validate_ident(measure_name)}"


def compile_semantic_query(
    *,
    expose: Mapping[str, Any],
    metric: Optional[str] = None,
    measure: Optional[str] = None,
    dimensions: Optional[List[str]] = None,
    filters: Optional[Mapping[str, Any]] = None,
    limit: Optional[int] = None,
    caller_attributes: Optional[Mapping[str, Any]] = None,
    table_reference: str,
) -> CompiledQuery:
    """Compile one ``query`` payload into a :class:`CompiledQuery`.

    ``table_reference`` is a fully-qualified, driver-validated table
    expression — the caller (driver) builds it from
    ``expose.binding.location`` and is responsible for any quoting.
    The compiler treats it as opaque text and only allowlist-checks
    it as a final defence-in-depth measure.

    ``filters`` values are bound as parameters; the keys are
    dimension names that must already exist in ``expose.semantics.dimensions``
    or ``expose.contract.schema``. Unknown keys are rejected.

    ``limit`` is treated as a literal integer, validated to be in
    ``[1, 1_000_000]``. We deliberately don't accept ``None`` for
    "no limit" — every consumer-side query gets a hard cap so a curious
    agent can't run a full-table scan by accident.
    """
    if (metric is None) == (measure is None):
        raise QueryValidationError("Exactly one of 'metric' or 'measure' must be provided")
    if not isinstance(table_reference, str) or not table_reference.strip():
        raise QueryValidationError("table_reference must be a non-empty string")
    validate_sql_expression_allowlist(table_reference)

    index = _index_expose(expose)
    if measure is not None:
        if not isinstance(measure, str):
            raise QueryValidationError("measure must be a string")
        validate_ident(measure)
        measure_definition = index.measures.get(measure)
        if measure_definition is None:
            raise QueryValidationError(
                f"Unknown measure {measure!r}; "
                f"known: {sorted(index.measures) or 'none in expose.semantics.measures'}"
            )
        measure_name = measure
    else:
        if not isinstance(metric, str):
            raise QueryValidationError("metric must be a string")
        validate_ident(metric)
        measure_name, measure_definition = _resolve_metric(metric, index)

    select_parts: List[str] = []
    group_columns: List[str] = []
    projection_aliases: List[str] = []
    for dimension_name in dimensions or []:
        if not isinstance(dimension_name, str):
            raise QueryValidationError("Dimension names must be strings")
        alias, expr = _resolve_dimension(dimension_name, index)
        select_parts.append(f"{expr} AS {alias}")
        group_columns.append(expr)
        projection_aliases.append(alias)

    measure_sql = _render_measure_expression(measure_name, measure_definition)
    select_parts.append(measure_sql)
    projection_aliases.append(measure_name)

    where_clauses: List[str] = []
    params: List[Any] = []
    for filter_key, filter_value in (filters or {}).items():
        if not isinstance(filter_key, str):
            raise QueryValidationError("Filter keys must be strings")
        validate_ident(filter_key)
        if filter_key not in index.dimensions and filter_key not in index.columns:
            raise QueryValidationError(
                f"Filter key {filter_key!r} is not a known dimension or "
                f"contract column. Filters must reference predeclared "
                f"semantics or schema entries."
            )
        if not isinstance(filter_value, (str, int, float, bool)) or isinstance(filter_value, bool):
            # Treat booleans like scalars — most engines accept them; reject
            # lists / dicts / None outright in MVP.
            if filter_value is None or isinstance(filter_value, (list, dict, tuple)):
                raise QueryValidationError(
                    f"Filter value for {filter_key!r} must be a scalar; "
                    f"got {type(filter_value).__name__}"
                )
        if filter_key in index.dimensions:
            _, dimension_expr = _resolve_dimension(filter_key, index)
        else:
            dimension_expr = validate_ident(filter_key)
        placeholder_index = len(params)
        where_clauses.append(f"{dimension_expr} = :p_{placeholder_index}")
        params.append(filter_value)

    # Row-level security: merge policy.rowFilters[] into the WHERE so the
    # semantic ``query`` tool enforces the SAME tenant isolation as ``sample``.
    # Previously query() executed the compiled statement with no row filter, so
    # a multi-tenant rowFilter (e.g. tenant_id = ${caller.tenant_id}) was
    # silently bypassed on the query/query_sql tools. Placeholders continue from
    # the filter params (offset) so they never collide.
    rf_clauses, rf_params = compile_row_filter_clauses(
        expose, caller_attributes or {}, offset=len(params)
    )
    where_clauses.extend(rf_clauses)
    params.extend(rf_params)

    if not isinstance(limit, int) or limit < 1 or limit > 1_000_000:
        raise QueryValidationError("limit must be an integer in [1, 1_000_000]")

    sql_lines: List[str] = [
        "SELECT " + ", ".join(select_parts),
        f"FROM {table_reference}",
    ]
    if where_clauses:
        sql_lines.append("WHERE " + " AND ".join(where_clauses))
    if group_columns:
        sql_lines.append("GROUP BY " + ", ".join(group_columns))
    sql_lines.append(f"LIMIT {limit}")
    sql = "\n".join(sql_lines)

    # Defence-in-depth: even though every interpolated piece is
    # validated above, sweep the rendered statement for the obvious
    # injection markers (semicolons, line / block comments). The
    # expression allowlist itself can't be applied to the full
    # statement (it blocks the SELECT keyword by design), so we run
    # a narrower statement-level check instead.
    _validate_rendered_statement(sql)
    return CompiledQuery(sql=sql, params=params, columns=projection_aliases)


def _validate_rendered_statement(sql: str) -> None:
    """Statement-level defence-in-depth check.

    Designed to catch a regression in the compiler that would slip a
    semicolon, a comment marker, or a stray quote past the per-piece
    allowlist sweeps above. Not the primary safety net — the named
    interpolations are.
    """
    if any(marker in sql for marker in (";", "--", "/*", "*/")):
        # Plain ValueError (NOT QueryValidationError) on purpose: the
        # message embeds the rendered SQL, which carries the binding's
        # database / schema / table. Keeping it a non-validation error
        # means the dispatcher sanitises it behind "see audit trail"
        # rather than surfacing the binding to the calling agent. This
        # is also a should-never-fire defence-in-depth net, not a
        # caller-actionable input error.
        raise ValueError(f"Rendered statement contains forbidden marker: {sql!r}")


_RESTRICTED_NAME_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _restricted_name_pattern(name: str) -> "re.Pattern[str]":
    """Compile (and cache) a case-insensitive word-boundary regex for
    a restricted column name.

    Used by :func:`compile_free_form_sql` to scan the body of a
    free-form SQL statement for any reference to a column whose
    contract policy forbids exposure. Matches identifier occurrences
    only — string literals are stripped before this regex runs.
    """
    cached = _RESTRICTED_NAME_CACHE.get(name)
    if cached is not None:
        return cached
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    _RESTRICTED_NAME_CACHE[name] = pattern
    return pattern


# String literals stripped before scanning for restricted column
# references. ``'…'`` (single-quoted) and ``"…"`` (double-quoted)
# both removed; embedded escaped quotes are tolerated. Comments are
# already blocked by ``_sql_safety``'s ``--`` / ``/*`` / ``*/``
# rejection.
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"")


def _strip_string_literals(sql: str) -> str:
    """Replace every quoted string literal in ``sql`` with a single
    space so subsequent identifier scans don't false-positive on
    quoted occurrences of restricted column names (e.g. ``WHERE name
    = 'email'``)."""
    return _STRING_LITERAL_RE.sub(" ", sql)


def compile_free_form_sql(
    *,
    sql: str,
    table_reference: str,
    limit: int,
    restricted_columns: Iterable[str] = (),
    expose: Optional[Mapping[str, Any]] = None,
    caller_attributes: Optional[Mapping[str, Any]] = None,
) -> CompiledQuery:
    """Compile a caller-supplied SQL ``query_sql`` payload.

    Phase-1 rules — deliberately strict:

    * ``sql`` MUST start with the case-insensitive token ``SELECT`` so
      we never run anything that mutates state.
    * The single-statement allowlist (``;``, ``--`` blocked) applies.
    * The reserved-word allowlist
      (:func:`fluid_build.providers._sql_safety.validate_sql_expression_allowlist`)
      blocks every DDL/DML token in the SQL body.
    * Server-side ``LIMIT`` is appended unconditionally — even if the
      user already supplied one. Two limits is harmless; missing one
      is dangerous.
    * Any identifier reference (case-insensitive, word-bounded) to a
      column listed in ``restricted_columns`` is rejected. This
      defends against the alias bypass — ``SELECT email AS not_email``
      would otherwise sneak masked values past the result-set masking
      step in :class:`fluid_build.output_ports.mcp.drivers.base.EngineDriver`.
      This is column-level deny only; row-level security is the next bullet.
    * **Row-level security.** When ``expose.policy.rowFilters[]`` is declared
      (and ``expose`` / ``caller_attributes`` are passed), the caller SQL is
      wrapped as ``SELECT * FROM (<caller_sql>) WHERE <rowfilter> LIMIT n`` so
      the SAME tenant isolation that ``sample`` / ``query`` apply also holds
      here — there is no longer a free-form bypass. The rowFilter columns must
      be visible in the caller's projection; if absent the engine errors
      (fail-closed). A missing ``${caller.*}`` attribute raises
      :class:`RowFilterIdentityMissing` before any SQL runs.
    * ``FROM`` references are NOT rewritten — the caller must reference the
      bound table by its fully-qualified name from the contract (the RLS
      wrapper only adds an outer ``SELECT * FROM (...)``; it does not rewrite
      the caller's ``FROM``). Phase-2 may add an AST-level rewrite so consumers
      can reference the expose by exposeId rather than the underlying binding.
    """
    if not isinstance(sql, str):
        raise QueryValidationError("sql must be a string")
    candidate = sql.strip()
    if not candidate:
        raise QueryValidationError("sql must be a non-empty string")
    if not candidate.lower().lstrip("(").startswith("select"):
        raise QueryValidationError("Only SELECT statements are allowed in --allow-sql mode")
    # ``_sql_safety`` blocks the SELECT keyword in expression mode, so
    # we hand it only the body after the leading SELECT. Splitting on
    # *any* whitespace (``str.split(None, 1)``) defends against the
    # tab/newline bypass that would skip validation when only ASCII
    # spaces are checked. A SELECT with no body (a single-token
    # ``SELECT``) is malformed and rejected outright.
    parts = candidate.split(None, 1)
    if len(parts) < 2:
        raise QueryValidationError("sql must contain a SELECT body")
    body = parts[1]
    validate_sql_expression_allowlist(body)
    if not isinstance(limit, int) or limit < 1 or limit > 1_000_000:
        raise QueryValidationError("limit must be an integer in [1, 1_000_000]")
    if not isinstance(table_reference, str) or not table_reference.strip():
        raise QueryValidationError("table_reference must be a non-empty string")
    validate_sql_expression_allowlist(table_reference)
    # Column-mask enforcement — see the ``restricted_columns`` bullet
    # in the docstring above. Strip quoted string literals first so
    # ``WHERE label = 'email'`` doesn't false-positive when ``email``
    # is itself a restricted column.
    if restricted_columns:
        scanned = _strip_string_literals(candidate)
        for column in restricted_columns:
            if not isinstance(column, str) or not column:
                continue
            if _restricted_name_pattern(column).search(scanned):
                raise QueryValidationError(
                    f"sql references column {column!r} which is restricted by "
                    f"expose.policy.authz.columnRestrictions / "
                    f"expose.policy.privacy.masking. The free-form "
                    f"--allow-sql path enforces the same column-level deny "
                    f"rules as the sample / query tools — aliasing the "
                    f"column does not bypass them."
                )
    # Row-level security: a WHERE cannot be merged into arbitrary caller SQL,
    # so when the contract declares policy.rowFilters[] we wrap the statement as
    # a subquery and apply the filter on the outside. This enforces the SAME
    # tenant isolation as sample/query — previously query_sql executed the
    # caller SQL with NO row filter, leaking every tenant's rows. The rowFilter
    # columns must be visible in the caller's projection (use SELECT * or include
    # them); if absent the engine errors — fail-closed, never an unfiltered
    # result. The inner LIMIT (if any) is harmless inside the subquery; the outer
    # LIMIT is the enforced cap.
    rf_clauses, rf_params = compile_row_filter_clauses(
        expose or {}, caller_attributes or {}, offset=0
    )
    if rf_clauses:
        rf_where = " AND ".join(rf_clauses)
        final_sql = (
            f"SELECT * FROM (\n{candidate}\n) AS _fluid_rls\n" f"WHERE {rf_where}\nLIMIT {limit}"
        )
        return CompiledQuery(sql=final_sql, params=rf_params, columns=[])
    final_sql = f"{candidate}\nLIMIT {limit}"
    return CompiledQuery(sql=final_sql, params=[], columns=[])
