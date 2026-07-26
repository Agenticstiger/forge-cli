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

_RESTRICTED_NAME_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _restricted_name_pattern(name: str) -> "re.Pattern[str]":
    """Compile (and cache) a case-insensitive word-boundary regex for
    a restricted column name.

    Used by :func:`compile_semantic_query` and
    :func:`compile_free_form_sql` to scan a SQL fragment for any
    reference to a column whose contract policy forbids exposure.
    Matches identifier occurrences only — string literals are stripped
    before this regex runs.
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


def _clean_column_names(columns: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Normalise a caller-supplied column-name iterable to a tuple of
    non-empty strings (drivers hand us ``set`` objects that may contain
    junk from a malformed contract)."""
    return tuple(c for c in (columns or ()) if isinstance(c, str) and c)


def _first_referenced_column(expression: str, columns: Iterable[str]) -> Optional[str]:
    """Return the first name in ``columns`` that ``expression``
    references as an identifier, or ``None``.

    String literals are stripped first so ``WHERE label = 'email'``
    doesn't false-positive when ``email`` is one of ``columns``. This
    is the same identifier scan :func:`compile_free_form_sql` runs
    over a caller-supplied statement — hoisted here so the semantic
    ``query`` path enforces the identical rule against contract-declared
    measure / dimension / filter expressions.
    """
    scanned = _strip_string_literals(expression)
    for column in columns:
        if _restricted_name_pattern(column).search(scanned):
            return column
    return None


def _reject_restricted_reference(
    expression: str, restricted_columns: Iterable[str], *, subject: str
) -> None:
    """Raise when ``expression`` references a denied column.

    ``subject`` names the contract element that carried the expression
    (``"Measure 'avg_balance'"``, ``"Dimension 'seg_alias'"``, …) so the
    agent can self-correct. Both the subject and the column name are
    contract-declared and already visible through ``describe``, so the
    message leaks nothing the caller couldn't enumerate — which is why
    it is a :class:`QueryValidationError` (surfaced verbatim) rather
    than a sanitised engine error.
    """
    hit = _first_referenced_column(expression, restricted_columns)
    if hit is None:
        return
    raise QueryValidationError(
        f"{subject} references column {hit!r} which is restricted by "
        f"expose.policy.authz.columnRestrictions / "
        f"expose.policy.privacy.masking. The governed query path enforces "
        f"the same column-level deny rules as the sample / query_sql tools — "
        f"naming a measure, dimension or filter differently from its column "
        f"does not bypass them."
    )


def _resolve_measure_alias(
    *,
    metric: Optional[str],
    measure_name: str,
    taken: Mapping[str, str],
) -> str:
    """Pick the projection alias for the aggregate column.

    Preference order — ``metric`` name (so two metrics over one measure
    are distinguishable), then the ``measure`` name (the pre-0.13.1
    behaviour), then a hard error.

    ``taken`` maps an already-projected dimension alias (case-folded, the
    way an engine folds an unquoted identifier) to its SQL expression.
    A projection alias MUST be unique: the drivers key each row with
    ``dict(zip(columns, values))``, so two columns sharing a name collapse
    into one dict entry and silently discard a value, and the compiler's
    ``ORDER BY <alias>`` becomes an ambiguous reference the engine
    rejects outright.

    Prior art — every mainstream semantic layer treats metric / measure /
    dimension names as ONE namespace and rejects the collision at model
    build time: dbt MetricFlow registers all of them in a single global
    namespace, and Cube requires ``name`` to be "unique among all
    dimensions, measures, and segments" in a cube
    (https://cube.dev/docs/dimensions/). Snowflake Semantic Views tolerate
    the duplicate but then emit two identically-named output columns and
    document the remedy as renaming them through a table alias
    (https://docs.snowflake.com/en/user-guide/views-semantic/querying).
    We can't retroactively make the collision a query-time error — a
    contract whose metric happens to share a dimension's name answered
    correctly before the metric-name aliasing landed — so we apply
    Snowflake's remedy automatically (deterministic rename) and surface
    the modelling problem as a ``fluid validate`` warning instead.
    """
    preferred = metric if metric is not None else measure_name
    if preferred.casefold() not in taken:
        return preferred
    if metric is not None and measure_name.casefold() not in taken:
        # Collision with a requested dimension: fall back to the measure
        # name, which is what this projection was called before metric
        # aliasing existed. The response's ``columns`` still describes the
        # result honestly, so the caller can always tell what it got.
        return measure_name
    raise QueryValidationError(
        f"Projection alias {preferred!r} collides with dimension "
        f"{taken[preferred.casefold()]!r} already in this SELECT, and no "
        f"distinct fallback name is available"
        + (f" (measure {measure_name!r} collides too)" if metric is not None else "")
        + ". Metric, measure and dimension names share one namespace on the "
        "governed query path — rename one of them, or drop the colliding "
        "dimension from this request."
    )


# Aggregations whose result is an actual member value of the input
# column rather than a summary statistic derived from many rows.
# A ``MIN``/``MAX``/``MEDIAN``/``PERCENTILE`` over a PII column hands
# the caller a real cell value, so those projections are redacted the
# same way a raw dimension projection is. ``count`` / ``count_distinct``
# / ``sum`` / ``avg`` stay visible — the ``_compute_pii_columns``
# contract explicitly keeps aggregate analysis over a PII column (e.g.
# ``COUNT(DISTINCT customer_email)``) legitimate.
_VALUE_REVEALING_AGGS = frozenset({"min", "max", "median", "percentile"})

# Matches a single ``:p_<index>`` named placeholder, capturing the index.
# Word-boundary anchored so ``:p_1`` never matches inside ``:p_10`` — the
# substring-collision bug a per-index ``str.replace`` loop has at ≥11 params.
# Shared by :meth:`CompiledQuery.render_sql_for_dialect`; the postgres /
# athena drivers compile the identical pattern for their own rewrites.
_PARAM_PLACEHOLDER_RE = re.compile(r":p_(\d+)\b")


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


def _quote_filter_identifier(column: str, dialect: Optional[str]) -> str:
    """Quote a (already ``validate_ident``-checked) row-filter column for the
    target ``dialect``.

    BigQuery's standard SQL reads ANSI double-quotes as a STRING LITERAL, so
    ``WHERE "tenant_id" = @p_0`` compiles to ``WHERE 'tenant_id' = <val>`` —
    always false → an RLS-protected BigQuery expose returns ZERO rows. BigQuery
    quotes identifiers with backticks instead. Every other dialect we target
    (duckdb / snowflake / postgres / athena) treats double-quotes as an
    identifier quote, so the default (``dialect is None`` or anything non-BQ)
    keeps the ANSI form for back-compat.
    """
    if dialect == "bigquery":
        return f"`{column}`"
    return f'"{column}"'


def compile_row_filter_clauses(
    expose: Mapping[str, Any],
    caller_attributes: Mapping[str, Any],
    *,
    offset: int = 0,
    dialect: Optional[str] = None,
) -> Tuple[List[str], List[Any]]:
    """Compile ``expose.policy.rowFilters[]`` into AND-able WHERE clauses +
    bound params, with placeholders ``:p_{offset}``, ``:p_{offset+1}``, …

    Centralises row-level security so EVERY read path enforces it: ``sample``
    (via :meth:`...drivers.base.EngineDriver.compile_row_filter_predicate`,
    ``offset=0``), the semantic ``query`` (merged into its WHERE at
    ``offset=len(existing_params)``), and the free-form ``query_sql`` path
    (which now rejects rather than wraps — see :func:`compile_free_form_sql`).
    The ``offset`` is what lets these merge into a statement that already has
    ``:p_<n>`` placeholders without colliding.

    ``dialect`` selects the identifier-quoting style for the filter column:
    backticks for ``"bigquery"`` (BQ reads ANSI double-quotes as a string
    literal, which silently turns the predicate always-false → zero rows),
    ANSI double-quotes for every other dialect. ``None`` (default) keeps the
    ANSI form for back-compat with callers that don't know their dialect.

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
        column = _quote_filter_identifier(validate_ident(column_raw), dialect)
        if "equals" in entry:
            value = _resolve_caller_placeholder(entry["equals"], caller_attributes)
            clauses.append(f"{column} = :p_{offset + len(params)}")
            params.append(value)
        elif "in" in entry:
            raw_list = _resolve_caller_placeholder(entry["in"], caller_attributes)
            if not isinstance(raw_list, (list, tuple)) or not raw_list:
                raise RowFilterIdentityMissing(
                    f"row filter on column={column!r} expects a non-empty "
                    "list (got empty / non-list value); fail-closed deny."
                )
            placeholders = ", ".join(f":p_{offset + len(params) + i}" for i in range(len(raw_list)))
            clauses.append(f"{column} IN ({placeholders})")
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

    row_cap: Optional[int] = None
    """The ``LIMIT`` the rendered statement carries, when that LIMIT can
    actually clip the result set.

    ``None`` means "this statement cannot be truncated" — an ungrouped
    aggregate returns exactly one row no matter what the LIMIT says, so
    reporting it as truncated would be its own lie. The driver uses this
    to set :attr:`...drivers.base.QueryResult.truncated` honestly; before
    it existed the wire response hardcoded ``truncated: false`` for every
    ``query``, so a ``revenue by day`` call clipped from 2,405 groups to
    50 was labelled a complete answer.
    """

    redacted_columns: Tuple[str, ...] = ()
    """Projection aliases whose VALUES must be replaced with
    :attr:`...drivers.base.EngineDriver.PII_TOKEN` before the rows leave
    the gateway.

    Redaction has to be keyed off the underlying column EXPRESSION, not
    the output alias: a dimension declared as ``{name: seg_alias, expr:
    MARKET_SEGMENT}`` projects PII under a name the driver's
    name-matching redaction step never recognises. The compiler is the
    only layer that knows the alias→expression mapping, so it resolves
    the alias set here and the driver applies it.
    """

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

        The rewrite uses a word-boundary regex (``:p_(\\d+)\\b``) rather
        than a per-index ``str.replace`` loop. A naive loop corrupts the
        SQL once there are ≥11 params: ``:p_1`` is a prefix of ``:p_10``,
        so replacing ``:p_1`` first turns ``:p_10`` into e.g.
        ``%(p_1)s0``. The single regex pass matches each placeholder
        exactly once and is the same approach the postgres / athena
        drivers already use.
        """
        if dialect == "duckdb":
            return _PARAM_PLACEHOLDER_RE.sub(r"$p_\1", self.sql)
        if dialect == "bigquery":
            # The bare-prefix replace is collision-safe here: ``@p_`` is a
            # 1:1 substitution of the ``:p_`` prefix, so ``:p_10`` → ``@p_10``
            # correctly (unlike the index-suffix forms the other dialects use).
            return self.sql.replace(":p_", "@p_")
        if dialect == "snowflake":
            return _PARAM_PLACEHOLDER_RE.sub(r"%(p_\1)s", self.sql)
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


def _resolve_metric(
    metric_name: str, index: _SemanticIndex
) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """Resolve a metric to (measure_name, measure_definition, filter_sql).

    Phase-1 supports ``simple`` metrics — the canonical case in
    dbt MetricFlow / Snowflake Semantic Views — which point at one
    measure. Derived and ratio metrics are intentionally rejected so
    we don't ship arithmetic over agent-supplied measures until
    Phase-2 hardens that path.

    ``filter_sql`` is the contract's ``metrics[].filter`` predicate
    (allowlist-validated here, applied to the WHERE by the caller).
    Previously it was silently ignored, so a filtered metric like
    ``completed_revenue = sum(amount) WHERE status = 'completed'``
    returned UNFILTERED numbers via the governed ``query`` tool while
    the dbt MetricFlow export honoured the same filter — two consumers,
    two different answers for one contract. A filter that fails the
    safe-expression allowlist (e.g. MetricFlow Jinja templates) raises
    instead of degrading to wrong results — fail closed, never wrong.
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
    metric_filter = metric.get("filter")
    if metric_filter is not None:
        if not isinstance(metric_filter, str) or not metric_filter.strip():
            raise QueryValidationError(f"Metric {metric_name!r} has a non-string/empty 'filter'")
        try:
            metric_filter = validate_sql_expression_allowlist(metric_filter)
        except ValueError:
            # The filter text is contract-declared (visible via ``describe``),
            # so naming the metric is safe; the raw expression is echoed only
            # through the allowlist's own ValueError, which we deliberately
            # do NOT propagate — templated filters (e.g. MetricFlow's
            # ``{{ Dimension('x') }}`` syntax) land here too.
            raise QueryValidationError(
                f"Metric {metric_name!r} carries a filter that fails the "
                "safe-expression allowlist; the governed query path cannot "
                "apply it. Rewrite the contract filter as a plain SQL "
                "predicate over contract columns."
            ) from None
        # Defence-in-depth: the filter is the first contract expression to
        # land in the WHERE next to policy.rowFilters. A deliberately
        # unbalanced filter like ``1=1) OR (1=1`` passes the char allowlist
        # but would escape its wrapping parens and — by AND/OR precedence —
        # neutralize an ANDed RLS clause. Balanced parens close the class.
        if not _parens_balanced(metric_filter):
            raise QueryValidationError(
                f"Metric {metric_name!r} carries a filter with unbalanced "
                "parentheses; the governed query path cannot apply it."
            )
    return measure_name, measure, metric_filter


def _parens_balanced(expr: str) -> bool:
    """True when every ``(`` closes in order and none closes early."""
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


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


# The contract-schema default when a percentile measure carries no
# ``aggParams.percentile``. MUST stay identical to the dbt MetricFlow
# bridge's default (``engines/dbt/semantic_models.py``) so the governed
# query path and the exported dbt project answer the same number for the
# same contract. 0.5 == median.
DEFAULT_PERCENTILE = 0.5


def _resolve_percentile_params(measure_name: str, measure: Mapping[str, Any]) -> Tuple[float, bool]:
    """Return (percentile, use_discrete) from ``measure.aggParams``.

    ``aggParams`` is the 0.7.6 contract slot mirroring
    dbt-semantic-interfaces' ``agg_params``. The percentile value is
    validated to a real number in [0, 1] — it is interpolated into SQL
    as a literal (aggregate arguments must be constants), so validation
    here is the safety boundary.
    """
    params = measure.get("aggParams") or {}
    if not isinstance(params, Mapping):
        raise QueryValidationError(f"Measure {measure_name!r} has non-object aggParams")
    raw = params.get("percentile", DEFAULT_PERCENTILE)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= float(raw) <= 1:
        raise QueryValidationError(
            f"Measure {measure_name!r} has invalid aggParams.percentile "
            f"{raw!r}; expected a number in [0, 1]"
        )
    return float(raw), bool(params.get("useDiscretePercentile"))


def _render_measure_expression(
    measure_name: str,
    measure: Mapping[str, Any],
    *,
    dialect: Optional[str] = None,
    alias: Optional[str] = None,
) -> str:
    """Render a measure into ``AGG(expr) AS <alias>``.

    ``alias`` defaults to the measure name but the caller passes the
    METRIC name when the request named a metric, so two metrics over one
    measure (``total_revenue`` and ``completed_revenue``, both over
    ``revenue``) come back as distinguishable, self-describing columns
    instead of two identical ``revenue`` columns.

    The measure's ``expr`` is allowlist-validated. The
    ``count_distinct`` aggregation is rendered as ``COUNT(DISTINCT
    expr)``; ``percentile`` renders the ANSI ordered-set form
    ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY expr)`` (``p`` from
    ``aggParams.percentile``, default ``DEFAULT_PERCENTILE``;
    ``useDiscretePercentile`` selects ``PERCENTILE_DISC``). BigQuery
    and Athena have no ordered-set percentile aggregate (BQ's
    ``PERCENTILE_CONT`` is analytic-only; Athena offers
    ``approx_percentile`` with different semantics), so those dialects
    fail closed with a clear error instead of shipping SQL the engine
    rejects — or worse, an approximation presented as exact.
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
    projection_alias = validate_ident(alias or measure_name)
    if agg_raw == "count_distinct":
        return f"COUNT(DISTINCT {expr_raw}) AS {projection_alias}"
    if agg_raw == "percentile":
        if dialect in ("bigquery", "athena"):
            raise QueryValidationError(
                f"Measure {measure_name!r} uses agg 'percentile', which the "
                f"{dialect} engine does not support as a grouped aggregate. "
                "Use 'median'-free approximations engine-side or query a "
                "different measure."
            )
        percentile, use_discrete = _resolve_percentile_params(measure_name, measure)
        fn = "PERCENTILE_DISC" if use_discrete else "PERCENTILE_CONT"
        return f"{fn}({percentile:g}) WITHIN GROUP (ORDER BY {expr_raw}) " f"AS {projection_alias}"
    return f"{sql_func}({expr_raw}) AS {projection_alias}"


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
    dialect: Optional[str] = None,
    restricted_columns: Iterable[str] = (),
    redacted_columns: Iterable[str] = (),
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

    The aggregate column is aliased to the METRIC name when the request
    named a metric (so ``total_revenue`` and ``completed_revenue`` — two
    metrics over the one ``revenue`` measure — are distinguishable in the
    result), and to the MEASURE name when the request named a measure
    directly. Every projection alias in the returned
    :attr:`CompiledQuery.columns` is unique after case folding; see
    :func:`_resolve_measure_alias` for how a metric name that collides
    with a requested dimension is resolved.

    ``limit`` is treated as a literal integer, validated to be in
    ``[1, 1_000_000]``. We deliberately don't accept ``None`` for
    "no limit" — every consumer-side query gets a hard cap so a curious
    agent can't run a full-table scan by accident.

    ``dialect`` is the driver's dialect token (``descriptor().dialect``);
    it only selects the identifier-quoting style for any merged
    ``policy.rowFilters[]`` column (backticks on BigQuery, ANSI
    double-quotes elsewhere — see :func:`compile_row_filter_clauses`).
    ``None`` keeps the ANSI form.

    ``restricted_columns`` are the columns denied by
    ``expose.policy.authz.columnRestrictions`` /
    ``expose.policy.privacy.masking``. ANY reference to one — from a
    measure's ``expr``, a dimension's ``expr``, a metric's ``filter``, or
    a caller filter key — is rejected. Dropping them from the projection
    (what the driver's ``project()`` does) is NOT enough on this path:
    the projection is ALIASED, so a measure ``{name: avg_balance, agg:
    avg, expr: ACCOUNT_BALANCE}`` used to sail past the name-matching
    drop and hand a denied column's statistics to the agent, and an
    equality filter on a denied column was a working inference oracle
    over its values. This is the same rule
    :func:`compile_free_form_sql` has always enforced on ``query_sql``.

    ``redacted_columns`` are the PII / PHI columns from
    ``expose.contract.schema[].sensitivity``. Those are NOT rejected —
    the contract deliberately keeps a PII column visible-but-redacted so
    an agent can aggregate over it — but every projection alias whose
    EXPRESSION reads one is returned in
    :attr:`CompiledQuery.redacted_columns` so the driver redacts by
    expression rather than by output name. Without that, a dimension
    ``{name: seg_alias, expr: MARKET_SEGMENT}`` returned raw PII while
    the identically-sourced ``{name: market_segment}`` was redacted.
    """
    if (metric is None) == (measure is None):
        raise QueryValidationError("Exactly one of 'metric' or 'measure' must be provided")
    if not isinstance(table_reference, str) or not table_reference.strip():
        raise QueryValidationError("table_reference must be a non-empty string")
    validate_sql_expression_allowlist(table_reference)

    restricted = _clean_column_names(restricted_columns)
    redacted = _clean_column_names(redacted_columns)

    index = _index_expose(expose)
    metric_filter: Optional[str] = None
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
        measure_name, measure_definition, metric_filter = _resolve_metric(metric, index)

    measure_expr = measure_definition.get("expr") or measure_name
    if isinstance(measure_expr, str):
        _reject_restricted_reference(measure_expr, restricted, subject=f"Measure {measure_name!r}")
    if metric_filter is not None:
        _reject_restricted_reference(metric_filter, restricted, subject=f"Metric {metric!r} filter")

    select_parts: List[str] = []
    group_columns: List[str] = []
    projection_aliases: List[str] = []
    redacted_aliases: List[str] = []
    # Case-folded dimension alias → its SQL expression. Engines fold an
    # unquoted identifier, so ``Status`` and ``status`` are ONE output
    # column name as far as the row dicts and the ORDER BY are concerned.
    dimension_aliases: Dict[str, str] = {}
    for dimension_name in dimensions or []:
        if not isinstance(dimension_name, str):
            raise QueryValidationError("Dimension names must be strings")
        alias, expr = _resolve_dimension(dimension_name, index)
        _reject_restricted_reference(expr, restricted, subject=f"Dimension {dimension_name!r}")
        already = dimension_aliases.get(alias.casefold())
        if already is not None:
            if already == expr:
                # The same dimension asked for twice — idempotent, so drop
                # the repeat rather than emit a duplicate output column.
                # (Projecting it twice used to collapse in the driver's
                # ``dict(zip(columns, values))`` anyway.)
                continue
            raise QueryValidationError(
                f"Dimension {dimension_name!r} projects the same output column "
                f"name as an earlier dimension in this request but a different "
                f"expression ({expr!r} vs {already!r}); one of the two values "
                f"would be silently dropped from every row. Rename one of the "
                f"dimensions in expose.semantics.dimensions."
            )
        dimension_aliases[alias.casefold()] = expr
        select_parts.append(f"{expr} AS {alias}")
        group_columns.append(expr)
        projection_aliases.append(alias)
        # A dimension projects raw column values, so a PII source column
        # must be redacted under WHATEVER alias it is projected as.
        if _first_referenced_column(expr, redacted) is not None:
            redacted_aliases.append(alias)

    # The projection is aliased to the METRIC name when the caller asked
    # for a metric, so ``total_revenue`` and ``completed_revenue`` — two
    # metrics over the one ``revenue`` measure — no longer come back as
    # two indistinguishable ``revenue`` columns. When that name is already
    # taken by a dimension in this same SELECT it falls back to the
    # measure name; see :func:`_resolve_measure_alias`.
    measure_alias = _resolve_measure_alias(
        metric=metric, measure_name=measure_name, taken=dimension_aliases
    )

    measure_sql = _render_measure_expression(
        measure_name, measure_definition, dialect=dialect, alias=measure_alias
    )
    select_parts.append(measure_sql)
    projection_aliases.append(measure_alias)
    # MIN / MAX / MEDIAN / PERCENTILE return an actual cell value, so an
    # otherwise-legitimate aggregate over a PII column still leaks one.
    if (
        isinstance(measure_expr, str)
        and str(measure_definition.get("agg") or "").lower() in _VALUE_REVEALING_AGGS
        and _first_referenced_column(measure_expr, redacted) is not None
    ):
        redacted_aliases.append(measure_alias)

    where_clauses: List[str] = []
    params: List[Any] = []
    if metric_filter is not None:
        # Contract-declared metric predicate (already allowlist-validated in
        # ``_resolve_metric``). Parenthesized so it ANDs safely with the
        # caller's dimension filters and any policy rowFilters below.
        where_clauses.append(f"({metric_filter})")
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
        # An equality filter on a denied column is a working inference
        # oracle even though the column never appears in the projection:
        # ``filters: {ACCOUNT_BALANCE: 9999.99}`` returns the revenue of
        # exactly the rows carrying that balance. Reject the reference.
        _reject_restricted_reference(
            dimension_expr, restricted, subject=f"Filter key {filter_key!r}"
        )
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
        expose, caller_attributes or {}, offset=len(params), dialect=dialect
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
        # A grouped result gets a deterministic "top N by the measure"
        # order. Without it the LIMIT clipped an ARBITRARY slice of the
        # groups — "revenue by day" returned 50 of 2,405 days in whatever
        # order the engine happened to produce, and two identical calls
        # could disagree. Ordering by the measure descending and then by
        # each grouping key gives a total order (the group keys are
        # unique per row of a GROUP BY result), so the answer is both
        # reproducible and the slice an analyst actually wants.
        order_terms = [f"{measure_alias} DESC"] + [
            f"{alias} ASC" for alias in projection_aliases[:-1]
        ]
        sql_lines.append("ORDER BY " + ", ".join(order_terms))
    sql_lines.append(f"LIMIT {limit}")
    sql = "\n".join(sql_lines)

    # Defence-in-depth: even though every interpolated piece is
    # validated above, sweep the rendered statement for the obvious
    # injection markers (semicolons, line / block comments). The
    # expression allowlist itself can't be applied to the full
    # statement (it blocks the SELECT keyword by design), so we run
    # a narrower statement-level check instead.
    _validate_rendered_statement(sql)
    return CompiledQuery(
        sql=sql,
        params=params,
        columns=projection_aliases,
        # Only a GROUPED result can be clipped by the LIMIT; an ungrouped
        # aggregate is exactly one row whatever the cap says, so reporting
        # it as truncatable would be a different lie.
        row_cap=limit if group_columns else None,
        redacted_columns=tuple(redacted_aliases),
    )


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


# A caller statement that already ends in ``LIMIT <n>`` (case-insensitive,
# trailing whitespace tolerated). Used so the server-side ``LIMIT`` is not
# appended a second time — two trailing LIMITs is a syntax error on most
# engines. Only a bare integer literal counts as "already limited"; a
# ``LIMIT`` with an expression / placeholder is left for the caller to own.
_TRAILING_LIMIT_RE = re.compile(r"(?is)\blimit\s+(\d+)\s*$")


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
    * Server-side ``LIMIT`` is appended — but only when the caller's
      statement doesn't already end in one. Appending a second
      ``LIMIT`` is a syntax error on most engines, so we detect a
      trailing ``LIMIT <n>`` (case-insensitive) and leave it; a
      statement with no limit always gets the server cap.
    * Any identifier reference (case-insensitive, word-bounded) to a
      column listed in ``restricted_columns`` is rejected. This
      defends against the alias bypass — ``SELECT email AS not_email``
      would otherwise sneak masked values past the result-set masking
      step in :class:`fluid_build.output_ports.mcp.drivers.base.EngineDriver`.
      This is column-level deny only; row-level security is the next bullet.
    * **Row-level security — FAIL CLOSED.** When
      ``expose.policy.rowFilters[]`` is declared (i.e.
      :func:`compile_row_filter_clauses` returns a non-empty clause
      list), free-form ``query_sql`` is REJECTED with a
      :class:`QueryValidationError`. RLS cannot be safely enforced on
      arbitrary caller SQL: wrapping it as
      ``SELECT * FROM (<caller_sql>) WHERE <rowfilter>`` is bypassable —
      the caller controls the subquery's projection, so
      ``SELECT 't1' AS tenant_id, secret FROM other`` spoofs the filter
      column and reads across tenants. (It also trips every driver's
      ``guard_against_injection_markers`` body-SELECT check, so the
      wrap erred anyway.) Callers that need row-filtered access on an
      RLS-protected expose must use the semantic ``query`` tool, where
      the WHERE is merged into a compiler-controlled statement. A
      missing ``${caller.*}`` attribute still raises
      :class:`RowFilterIdentityMissing` (fail-closed) before the
      rejection check completes.
    * ``FROM`` references are NOT rewritten — the caller must reference the
      bound table by its fully-qualified name from the contract. Phase-2 may
      add an AST-level rewrite so consumers can reference the expose by
      exposeId rather than the underlying binding.
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
    # Row-level security: FAIL CLOSED. RLS cannot be safely enforced on
    # arbitrary caller SQL. The old wrapper —
    #   SELECT * FROM (<caller_sql>) AS _fluid_rls WHERE <rowfilter> LIMIT n
    # was bypassable two ways: (a) the caller controls the subquery's
    # projection, so `SELECT 't1' AS tenant_id, secret FROM other` spoofs the
    # filter column → cross-tenant read; (b) the inner SELECT trips every
    # driver's guard_against_injection_markers body-keyword check, so it erred
    # anyway. When the expose declares policy.rowFilters[], reject the
    # free-form path outright and steer the caller to the semantic `query`
    # tool, where the WHERE is merged into a compiler-controlled statement.
    # (compile_row_filter_clauses still resolves ${caller.*} first, so a
    # missing attribute fails closed via RowFilterIdentityMissing before we
    # reach the rejection.)
    rf_clauses, _rf_params = compile_row_filter_clauses(
        expose or {}, caller_attributes or {}, offset=0
    )
    if rf_clauses:
        raise QueryValidationError(
            "Free-form query_sql is not permitted on an expose that declares "
            "row filters (policy.rowFilters): row-level security cannot be "
            "enforced on arbitrary SQL — use the semantic 'query' tool instead."
        )
    # No row filters: append the server-side cap, but only when the caller
    # didn't already supply a trailing ``LIMIT <n>`` (a second one is a syntax
    # error on most engines).
    trailing = _TRAILING_LIMIT_RE.search(candidate)
    if trailing is not None:
        final_sql = candidate
        row_cap = int(trailing.group(1))
    else:
        final_sql = f"{candidate}\nLIMIT {limit}"
        row_cap = limit
    # ``row_cap`` lets the driver report ``truncated`` honestly on the
    # free-form path too: an arbitrary caller SELECT that comes back
    # holding exactly ``row_cap`` rows has almost certainly been clipped.
    return CompiledQuery(sql=final_sql, params=[], columns=[], row_cap=row_cap)
