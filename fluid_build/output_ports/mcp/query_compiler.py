# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

A query reads one expose. ``semantics.relationships`` (schema 0.7.7) is
information here: ``describe`` returns it with the rest of the semantics
block, and no query joins on it (cross-expose joins are phase 2 of
RFC-semantics-tier1). A dimension's ``typeParams.validityParams`` is
ignored: a validity window's bounds group like any time dimension, and
no query is filtered to the version current at a point in time.

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
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from fluid_build.forge_datamodel import semantics_builder as _semantics_builder
from fluid_build.output_ports.mcp.auth import PRINCIPAL_ATTR
from fluid_build.providers._row_filters import (
    RowFilter,
    RowFilterError,
    parse_predicate,
    parse_row_filters,
)
from fluid_build.providers._sql_safety import (
    names_refused_duckdb_function,
    names_refused_postgres_function,
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
    """The expose filters rows by principal (``policy.authz.rowFilters``) and the
    request carries no verified principal that a row filter or the expose's
    ``policy.authz.readers`` names.

    The gateway then cannot tell which rows the caller may read, so the request is
    refused (fail closed): an unknown caller is served neither a filtered
    principal's rows nor a reader's every row. A ``RuntimeError``, not a
    :class:`QueryValidationError`, so the wire carries only its name and the detail
    stays in the operator log."""


class RowFilterUnavailable(RuntimeError):
    """The expose's row filters cannot be applied as declared, so the request is
    refused rather than served unfiltered.

    Raised for a malformed ``policy.authz.rowFilters`` entry (no principal, a
    predicate that is not one boolean expression, a column the schema does not
    declare, a principal filtered twice), malformed ``policy.authz.readers``, a
    predicate this engine would read differently from the contract, and the old
    ``policy.rowFilters`` key. The cause is the contract, which the caller cannot
    change, and the detail names predicates and principals, so like
    :class:`RowFilterIdentityMissing` it is not a :class:`QueryValidationError`:
    the wire carries only its name."""


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


# Quoted spans an expression can carry, in the order the engine reads them. One
# left-to-right pass so an apostrophe inside a quoted identifier (or a double quote
# inside a string) cannot be mistaken for the opening of a span. Comments are already
# blocked by ``_sql_safety``'s ``--`` / ``/*`` / ``*/`` rejection, and the expression
# allowlist refuses a backslash and ``$``, so no escape form other than a doubled quote
# can reach this scan.
_QUOTED_SPAN_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`")

# Dialects where ``"..."`` is a STRING literal. Every other dialect the compiler
# targets (DuckDB, Postgres, Snowflake, Athena/Trino, ANSI) reads ``"..."`` as a
# quoted IDENTIFIER, which is a column reference and must stay visible to the scan.
# An unknown dialect (``None``) takes the conservative reading: identifier.
_DOUBLE_QUOTE_IS_STRING_DIALECTS = frozenset({"bigquery", "databricks", "mysql", "spark"})

# Driver dialect token -> sqlglot dialect. An unknown token parses and prints as
# generic ANSI SQL.
_SQLGLOT_DIALECTS = {
    "athena": "trino",
    "bigquery": "bigquery",
    "databricks": "spark",
    "duckdb": "duckdb",
    "postgres": "postgres",
    "postgresql": "postgres",
    "snowflake": "snowflake",
    "spark": "spark",
    "trino": "trino",
}


def _strip_string_literals(sql: str, dialect: Optional[str] = None) -> str:
    """Replace every string literal in ``sql`` with a single space so subsequent
    identifier scans don't false-positive on quoted occurrences of restricted column
    names (e.g. ``WHERE name = 'email'``).

    ``'...'`` is a string literal in every dialect. ``"..."`` is one only where
    ``dialect`` says so (BigQuery, Spark / Databricks, MySQL); everywhere else it is a
    quoted identifier and is left in place, so ``"region"`` is still seen as a
    reference to ``region``. Backtick spans are identifiers and are always kept.
    """
    double_is_string = (dialect or "").strip().lower() in _DOUBLE_QUOTE_IS_STRING_DIALECTS

    def _replace(match: "re.Match[str]") -> str:
        span = match.group(0)
        if span[0] == "'" or (span[0] == '"' and double_is_string):
            return " "
        return span

    return _QUOTED_SPAN_RE.sub(_replace, sql)


def _clean_column_names(columns: Optional[Iterable[str]]) -> Tuple[str, ...]:
    """Normalise a caller-supplied column-name iterable to a tuple of
    non-empty strings (drivers hand us ``set`` objects that may contain
    junk from a malformed contract)."""
    return tuple(c for c in (columns or ()) if isinstance(c, str) and c)


def _first_referenced_column(
    expression: str, columns: Iterable[str], dialect: Optional[str] = None
) -> Optional[str]:
    """Return the first name in ``columns`` that ``expression``
    references as an identifier, or ``None``.

    String literals are stripped first so ``WHERE label = 'email'``
    doesn't false-positive when ``email`` is one of ``columns``. This
    is the same identifier scan :func:`compile_free_form_sql` runs
    over a caller-supplied statement — hoisted here so the semantic
    ``query`` path enforces the identical rule against contract-declared
    measure / dimension / filter expressions.
    """
    scanned = _strip_string_literals(expression, dialect)
    for column in columns:
        if _restricted_name_pattern(column).search(scanned):
            return column
    return None


def _reject_restricted_reference(
    expression: str,
    restricted_columns: Iterable[str],
    *,
    subject: str,
    dialect: Optional[str] = None,
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
    hit = _first_referenced_column(expression, restricted_columns, dialect)
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


# Alias of the projection every expression is evaluated over when the expose
# restricts columns (see :func:`_allowed_columns`).
_PROJECTION_ALIAS = "_fluid_allowed"

# The opening of a subquery: a parenthesis followed by a keyword that starts a query.
# ``SELECT`` is already refused by the expression allowlist, but DuckDB also reads a
# query that begins with ``FROM`` / ``TABLE`` / ``SUMMARIZE`` (``EXISTS(FROM t WHERE ..)``),
# and that names the base table directly, bypassing the projection below. The standard
# function forms that contain ``FROM`` (``EXTRACT(year FROM d)``, ``SUBSTRING(s FROM 2)``,
# ``IS DISTINCT FROM``) never open a parenthesis with it.
_SUBQUERY_OPEN_RE = re.compile(
    r"\(\s*(?:from|table|with|values|summarize|describe|pivot|unpivot|explain)\b",
    re.IGNORECASE,
)


def _reject_subquery(expression: str, *, subject: str, dialect: Optional[str] = None) -> None:
    """Raise when ``expression`` embeds a subquery.

    On an expose that restricts columns or filters rows, a subquery would name the
    base table directly and read it without the projection and row filter the
    outer statement applies. Expressions there may only use the exposed table's own
    columns.
    """
    if _SUBQUERY_OPEN_RE.search(_strip_string_literals(expression, dialect)):
        raise QueryValidationError(
            f"{subject} contains a subquery. On an expose with column restrictions or "
            f"row filters, expressions may only use the exposed table's own columns."
        )


def _require_balanced_parentheses(expression: str, *, subject: str) -> None:
    """Raise when ``expression`` closes a parenthesis it did not open.

    An expression is wrapped in parentheses where it joins a WHERE; unbalanced text
    could close that wrapper early. A parenthesis inside a quoted span (a string
    literal or a quoted identifier) does not count.
    """
    if not _parens_balanced(expression):
        raise QueryValidationError(
            f"{subject} has an expression with unbalanced parentheses; the governed "
            "query path cannot apply it."
        )


# Forms that select a column without naming it, or hand over a whole row. On an expose
# that redacts columns they would return a redacted column's values under a name the
# redaction never matches (the compiler redacts by the columns an expression NAMES), so
# they are refused there: a column selector, an ``IDENTIFIER(...)`` that builds a name
# from text, a star, and the table (or the compiler's projection of it) used as a value.
_COLUMN_SELECTOR_RE = re.compile(r"(?i)(?<![\w$])[\"`]?(?:columns|identifier)[\"`]?\s*\(")
_COUNT_STAR_RE = re.compile(r"(?i)(?<![\w$])count\s*\(\s*\*\s*\)")
# Words after which a ``*`` is a star, not a multiplication.
_STAR_KEYWORDS = frozenset({"distinct", "all", "exclude", "replace", "rename", "ilike", "like"})


def _mask_string_literals(expression: str, dialect: Optional[str]) -> str:
    """``expression`` with every string literal replaced by the number ``0``, so what
    remains is the SQL the engine reads as names and operators and a literal still
    counts as an operand."""
    double_is_string = (dialect or "").strip().lower() in _DOUBLE_QUOTE_IS_STRING_DIALECTS

    def _replace(match: "re.Match[str]") -> str:
        span = match.group(0)
        return "0" if span[0] == "'" or (span[0] == '"' and double_is_string) else span

    return _QUOTED_SPAN_RE.sub(_replace, expression)


def _has_star_value(masked: str) -> bool:
    """True when ``masked`` holds a ``*`` that stands for columns rather than the
    multiplication operator: after ``(``, ``,``, ``.``, ``[``, an operator, a keyword
    such as ``DISTINCT``, or at the start. ``a * b``, ``f(x) * 2`` and ``a ** b`` are
    arithmetic and stay allowed."""
    text = masked.replace("**", "^")
    for match in re.finditer(r"\*", text):
        before = text[: match.start()].rstrip()
        if not before:
            return True
        previous = before[-1]
        if previous in "(,.[":
            return True
        if previous.isalnum() or previous in '_)]"`':
            word = re.search(r"([A-Za-z_]+)$", before)
            if word is not None and word.group(1).lower() in _STAR_KEYWORDS:
                return True
            continue
        return True
    return False


_QUOTE_CHARS_RE = re.compile(r"[\"'`]")


def _table_last_identifier(table_reference: str, dialect: Optional[str] = None) -> Optional[str]:
    """The name a table reference ends in, or ``None`` when it ends in anything else
    (a function call, for one): ``customers`` for ``db.public."customers"``, and for
    BigQuery's `` `proj.ds.customers` `` too, where one pair of backticks wraps every
    part and the engine's implicit alias is still the last one."""
    reference = table_reference.strip()
    try:
        from sqlglot import exp

        table = exp.to_table(reference, dialect=_SQLGLOT_DIALECTS.get((dialect or "").lower()))
        # A dialect that does not know the quote (backticks under the default one)
        # leaves it in the name: that is not an answer, the text scan below is.
        if (
            isinstance(table.this, exp.Identifier)
            and table.name
            and not _QUOTE_CHARS_RE.search(table.name)
        ):
            return table.name
    except Exception:  # noqa: BLE001 - any parser failure falls back to the text scan
        pass
    match = re.search(r'(?:"([^"]+)"|`([^`]+)`|([A-Za-z_][\w$]*))\s*$', reference)
    if match is None:
        return None
    last = next(group for group in match.groups() if group)
    return last.rsplit(".", 1)[-1] or None


def _reject_unnamed_column_access(
    expression: str,
    *,
    subject: str,
    table_reference: str,
    dialect: Optional[str] = None,
    allow_count_star: bool = False,
) -> None:
    """On an expose that redacts columns, refuse an expression that could read one
    without naming it.

    Refused: ``COLUMNS(...)``, ``IDENTIFIER(...)``, a ``*`` that stands for columns
    (``count(*)`` is fine, and ``allow_count_star`` lets a ``count`` measure's own
    ``*`` through; multiplication is fine), and the table itself, or the compiler's
    projection of it, used as a value (``to_json(customers)``). A qualified column
    (``customers.amount``) names a column and is checked as one elsewhere.
    """
    masked = _mask_string_literals(expression, dialect)
    scanned = _COUNT_STAR_RE.sub("0", masked)
    if allow_count_star and scanned.strip() == "*":
        scanned = "0"
    names = [_PROJECTION_ALIAS]
    last = _table_last_identifier(table_reference, dialect)
    if last:
        names.append(last)
    whole_row = re.compile(
        r"(?i)(?<![\w$])(?:"
        + "|".join(rf'"{re.escape(n)}"|`{re.escape(n)}`|{re.escape(n)}' for n in names)
        + r")(?![\w$\"`])(?!\s*\.)"
    )
    if _COLUMN_SELECTOR_RE.search(scanned) or _has_star_value(scanned) or whole_row.search(scanned):
        raise QueryValidationError(
            f"{subject} selects columns without naming them (a column selector, "
            f"IDENTIFIER(...), a star, or the whole row). This expose redacts columns "
            f"(contract.schema[].sensitivity), and a redaction applies to the columns an "
            f"expression names, so name the columns it reads."
        )


def _allowed_columns(index: "_SemanticIndex", restricted: Iterable[str]) -> List[str]:
    """The ``contract.schema`` columns that are NOT restricted, in schema order.

    This is an allowlist on purpose: a column the contract does not declare is not
    projected either, so a column added to the table later is never readable by
    default. Fails closed when nothing readable is declared.
    """
    return _allowed_names(list(index.columns), restricted)


def _allowed_names(declared: Iterable[str], restricted: Iterable[str]) -> List[str]:
    denied = {name.casefold() for name in restricted}
    allowed: List[str] = []
    seen: Set[str] = set()
    for name in declared:
        folded = name.casefold()
        if folded in denied or folded in seen:
            continue
        seen.add(folded)
        allowed.append(name)
    if not allowed:
        raise QueryValidationError(
            "This expose restricts columns or filters rows, but contract.schema declares "
            "no readable column to project, so the governed query path cannot evaluate "
            "an expression over the policy's projection. Declare the readable columns in "
            "contract.schema."
        )
    return allowed


# The projection's opening, as the compiler renders it: ``(SELECT a, b FROM``. The
# driver's statement guard refuses a body ``SELECT`` (a union / exfiltration marker);
# this is the one body ``SELECT`` the compiler itself writes. It is recognised at any
# position, so a caller's own layout around the table reference (a parenthesis, a
# DuckDB prefix alias ``c: customers``, a keyword before it) does not turn the
# compiler's output into a refusal. That is safe because a caller's SQL, a contract
# expression and the table reference all pass ``validate_sql_expression_allowlist``,
# which refuses the word ``select`` anywhere in them, so this shape can only be the
# compiler's. Every column is a bare or quoted identifier, never an expression or a
# nested query.
_PROJECTION_COLUMN = (
    r"""(?:[A-Za-z_][A-Za-z0-9_]*|"[A-Za-z_][A-Za-z0-9_]*"|`[A-Za-z_][A-Za-z0-9_]*`)"""
)
_PROJECTION_OPEN_RE = re.compile(
    r"\(SELECT " + _PROJECTION_COLUMN + r"(?:, " + _PROJECTION_COLUMN + r")* FROM "
)


def strip_projection_open(sql_body: str) -> str:
    """Remove the compiler's own projection ``(SELECT <columns> FROM`` opening from a
    rendered statement body, so a driver's body-keyword guard sees only the rest.

    The remainder (table reference, row-filter predicate, alias, outer clauses) is
    still scanned in full: only the opening goes, so a ``SELECT`` anywhere else in the
    statement (a union branch, a scalar subquery over another column list) is still
    refused. The shape removed is the one :func:`compile_semantic_query` and
    :func:`compile_free_form_sql` render for an expose that restricts columns or rows.
    """
    return _PROJECTION_OPEN_RE.sub("(", sql_body)


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


#: The alias the exposed table carries inside a row-filtered read, and the qualifier
#: every column of a row-filter predicate is given. Qualified, a predicate column can
#: only be the exposed table's own: a column the table lacks is an engine error, never
#: a same-named column of the caller's statement (a ``LATERAL`` neighbour, an outer
#: query) that the filter would compare instead.
ROW_FILTER_ALIAS = "_fluid_rows"

#: What the statement shown to a caller carries in place of its row-filter predicate.
ROW_FILTER_SHOWN = "(<row filter>)"

# How :func:`parse_predicate` frames a predicate; its spans count from here.
_PREDICATE_FRAME = "SELECT 1 FROM t WHERE "

# The compiler's own bind markers. ``render_sql_for_dialect`` and the drivers rewrite
# them wherever they appear, a string literal included, so a predicate that holds the
# text would reach the engine changed (``code <> 'x:p_1'`` would compare with another
# value and serve the row it was written to withhold).
_BIND_MARKER_TEXT_RE = re.compile(r"(?i):p_")

# Words the driver's statement guard refuses anywhere in a statement, a string literal
# included, and the expression allowlist does not: a predicate holding one would be
# refused on every read, so it is refused here, where the reason can be named.
_GUARDED_WORD_RE = re.compile(r"(?i)\b(?:union|intersect|except)\b")


def _policy_at(expose: Mapping[str, Any]) -> str:
    return f"exposes[{expose.get('exposeId') or '?'}].policy"


def _authz_block(expose: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The expose's ``policy.authz``, or ``None`` when it declares none.

    Refuses the old ``policy.rowFilters`` key when it holds anything: no schema allows
    it, and a contract that still fills it meant its rows to be filtered, so reading
    it as "no filter" would serve them all. A ``policy`` or ``authz`` that is not a
    mapping is refused for the same reason.
    """
    policy = expose.get("policy")
    if policy is None or (isinstance(policy, Mapping) and not policy):
        return None
    at = _policy_at(expose)
    if not isinstance(policy, Mapping):
        raise RowFilterUnavailable(f"{at} is not a mapping, so its row filters cannot be read.")
    if policy.get("rowFilters"):
        raise RowFilterUnavailable(
            f"{at}.rowFilters is not a contract field. Row filters are declared under "
            f"{at}.authz.rowFilters, each naming a principal and a where predicate; an "
            "expose that still carries the old key is refused rather than served unfiltered."
        )
    authz = policy.get("authz")
    if authz is None or (isinstance(authz, Mapping) and not authz):
        return None
    if not isinstance(authz, Mapping):
        raise RowFilterUnavailable(
            f"{at}.authz is not a mapping, so its row filters cannot be read."
        )
    return authz


def declares_row_filters(expose: Mapping[str, Any]) -> bool:
    """Whether ``expose`` declares ``policy.authz.rowFilters`` entries.

    Raises :class:`RowFilterUnavailable` for the old ``policy.rowFilters`` key and for
    a ``policy`` or ``authz`` that is not a mapping, as every read path does."""
    authz = _authz_block(expose)
    return bool(authz.get("rowFilters")) if authz is not None else False


def _schema_column_names(expose: Mapping[str, Any]) -> Set[str]:
    """``contract.schema[].name`` as written, for checking a predicate's columns. A
    name that is not a string declares no column."""
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    if not isinstance(schema, list):
        return set()
    names = (column.get("name") for column in schema if isinstance(column, Mapping))
    return {name for name in names if isinstance(name, str)}


def _principal_names(values: Iterable[Any]) -> FrozenSet[str]:
    """The non-blank strings among ``values``, trimmed."""
    return frozenset(item.strip() for item in values if isinstance(item, str) and item.strip())


def _verified_principals(caller_attributes: Optional[Mapping[str, Any]]) -> FrozenSet[str]:
    """The principals in the caller's :data:`~fluid_build.output_ports.mcp.auth.PRINCIPAL_ATTR`:
    one string or a list of them. Any other value names none."""
    attrs = caller_attributes if isinstance(caller_attributes, Mapping) else {}
    value = attrs.get(PRINCIPAL_ATTR)
    if isinstance(value, str):
        return _principal_names([value])
    return _principal_names(value) if isinstance(value, (list, tuple)) else frozenset()


def _declared_readers(authz: Mapping[str, Any], at: str) -> FrozenSet[str]:
    """``policy.authz.readers``; anything but a list of principals is refused."""
    raw = authz.get("readers")
    if raw is not None and not isinstance(raw, list):
        raise RowFilterUnavailable(f"{at}.authz.readers must be a list of principals.")
    return _principal_names(raw or ())


def _qualified_predicate(row_filter: RowFilter, at: str, read: Optional[str]) -> str:
    """``row_filter.where`` as this engine runs it: every column qualified with
    :data:`ROW_FILTER_ALIAS`, after checking the engine reads it as declared.

    :func:`parse_row_filters` checks the predicate as standard SQL, the reading the
    IaC emitters write platform policies from. ``read`` is the engine's sqlglot
    dialect; a predicate it reads with other columns is refused (on BigQuery and Spark
    ``"region" <> 'x'`` compares two strings and selects every row), as is one it
    cannot parse, one that reaches a column through a qualifier, and SQL the gateway
    refuses in every contract expression (a statement keyword, a refused function).
    The qualifiers are spliced into the contract's own text, at each column's source
    span, so the predicate reaches the engine as written; the result is parsed again
    and must name the same columns, each through :data:`ROW_FILTER_ALIAS`.
    """
    from sqlglot import exp

    try:
        validate_sql_expression_allowlist(row_filter.where)
    except ValueError:
        raise RowFilterUnavailable(
            f"{at}.where uses SQL the gateway does not run in a contract expression: a "
            "statement keyword (inside a string literal too), a refused function or a "
            "character outside its allowlist."
        ) from None
    if _GUARDED_WORD_RE.search(row_filter.where):
        raise RowFilterUnavailable(
            f"{at}.where holds UNION, INTERSECT or EXCEPT, which the gateway's statement "
            "guard refuses anywhere in a statement, inside a string literal too."
        )
    if _BIND_MARKER_TEXT_RE.search(row_filter.where):
        raise RowFilterUnavailable(
            f"{at}.where holds the text ':p_', which the gateway rewrites as a bind "
            "marker wherever it appears, inside a string literal too."
        )
    # As for every contract expression on a policy-bearing expose: a subquery opening
    # (``(FROM``, ``(TABLE``, ``(VALUES``, ...) would read a relation by itself.
    if _SUBQUERY_OPEN_RE.search(_strip_string_literals(row_filter.where, read)):
        raise RowFilterUnavailable(
            f"{at}.where contains a subquery; a row filter is one boolean expression over "
            "the expose's own columns."
        )
    try:
        tree = parse_predicate(row_filter.where, at, dialect=read)
    except RowFilterError as exc:
        raise RowFilterUnavailable(f"{exc} (as this engine reads it)") from exc
    columns = list(tree.args["where"].find_all(exp.Column))
    # Every occurrence counts, not only the names: ``region IS NOT NULL AND "region"
    # <> 'US'`` names ``region`` either way, but BigQuery reads its second ``"region"``
    # as a string and the predicate as ``region IS NOT NULL``.
    standard = parse_predicate(row_filter.where, at)
    if Counter(c.name for c in columns) != Counter(
        c.name for c in standard.args["where"].find_all(exp.Column)
    ):
        raise RowFilterUnavailable(
            f"{at}.where reads other columns on this engine than as declared, so it cannot "
            "be applied as written here (a double-quoted name, for one, is a string on "
            "BigQuery and Spark)."
        )
    offsets: List[int] = []
    for column in columns:
        if any(column.args.get(part) for part in ("table", "db", "catalog")):
            raise RowFilterUnavailable(
                f"{at}.where names a column through a qualifier; a row filter reads the "
                "expose's own columns, so write them unqualified."
            )
        name = column.this
        start = name.meta.get("start") if isinstance(name, exp.Identifier) else None
        if not isinstance(start, int) or start < len(_PREDICATE_FRAME):
            raise RowFilterUnavailable(f"{at}.where could not be qualified for this engine.")
        offsets.append(start - len(_PREDICATE_FRAME))
    qualified = row_filter.where
    for offset in sorted(offsets, reverse=True):
        qualified = f"{qualified[:offset]}{ROW_FILTER_ALIAS}.{qualified[offset:]}"
    try:
        check = parse_predicate(qualified, at, dialect=read)
    except RowFilterError:
        check = None
    names = [] if check is None else list(check.args["where"].find_all(exp.Column))
    if (
        check is None
        or sorted(c.name for c in names) != sorted(c.name for c in columns)
        or any(
            c.table != ROW_FILTER_ALIAS or c.args.get("db") or c.args.get("catalog") for c in names
        )
    ):
        raise RowFilterUnavailable(f"{at}.where could not be qualified for this engine.")
    return qualified


def row_filter_predicate(
    expose: Mapping[str, Any],
    caller_attributes: Optional[Mapping[str, Any]],
    *,
    dialect: Optional[str] = None,
) -> Optional[str]:
    """The predicate that limits a caller to its rows, or ``None`` for every row.

    ``exposes[].policy.authz.rowFilters[]`` (fluid-schema 0.7.6) lets each filter's
    principal read only the rows its ``where`` selects, and a reader no filter names
    read every row. Resolved for the caller's verified principals
    (:data:`~fluid_build.output_ports.mcp.auth.PRINCIPAL_ATTR`, one or a list):

    * no row filter declared: ``None``;
    * a principal of the caller is in ``policy.authz.readers`` and no filter names
      it: ``None``, every row;
    * otherwise the union of the predicates of the filters that name the caller's
      principals, as BigQuery combines the row access policies naming one principal:
      ``((p1) OR (p2))``, each column qualified with :data:`ROW_FILTER_ALIAS`;
    * no filter and no reader names a principal of the caller, or it has none:
      :class:`RowFilterIdentityMissing`. The gateway cannot tell an unfiltered
      reader from a filtered principal it does not know, so it serves neither.

    Every filter is parsed by :mod:`fluid_build.providers._row_filters`, the parse the
    IaC emitters write platform policies from, and checked against ``dialect`` (the
    driver's dialect token). A filter that cannot be applied raises
    :class:`RowFilterUnavailable`, which every caller sees whichever filter it is, as
    does the old ``policy.rowFilters`` key. The predicate binds no parameter: every
    value in it is the contract's own literal.
    """
    authz = _authz_block(expose)
    raw = None if authz is None else authz.get("rowFilters")
    if authz is None or raw is None:
        return None
    at = _policy_at(expose)
    try:
        filters = parse_row_filters(
            raw, declared=_schema_column_names(expose), where_at=f"{at}.authz.rowFilters"
        )
    except RowFilterError as exc:
        raise RowFilterUnavailable(str(exc)) from exc
    if not filters:
        return None
    read = _SQLGLOT_DIALECTS.get((dialect or "").strip().lower())
    predicates = {
        row_filter.principal: _qualified_predicate(
            row_filter, f"{at}.authz.rowFilters[{position}]", read
        )
        for position, row_filter in enumerate(filters)
    }
    unfiltered_readers = _declared_readers(authz, at) - set(predicates)
    principals = _verified_principals(caller_attributes)
    if principals & unfiltered_readers:
        return None
    mine = [
        predicates[row_filter.principal]
        for row_filter in filters
        if row_filter.principal in principals
    ]
    if not mine:
        raise RowFilterIdentityMissing(
            f"{at}.authz.rowFilters limits rows by principal, and this request carries no "
            "verified principal that a row filter or policy.authz.readers names (it "
            f"carries {sorted(principals) or 'none'}). A principal binds only from a "
            "verified credential: map the JWT claim that carries it with "
            "FLUID_MCP_JWT_CLAIM_MAPPING=<claim>=principal."
        )
    return "(" + " OR ".join(f"({predicate})" for predicate in mine) + ")"


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

    row_filter: Optional[str] = None
    """The row-filter predicate :func:`row_filter_predicate` resolved for the caller and
    the statement applies, or ``None``. It is the contract's policy text, so the
    statement the caller is shown carries ``(<row filter>)`` in its place (see
    :meth:`shown_sql`)."""

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

    def shown_sql(self) -> str:
        """:attr:`sql` as the caller may see it: the row-filter predicate, which is
        policy the ``describe`` tool does not return either, replaced by
        ``(<row filter>)``."""
        if not self.row_filter:
            return self.sql
        return self.sql.replace(self.row_filter, ROW_FILTER_SHOWN)

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
    """True when every ``(`` closes in order and none closes early.

    A parenthesis inside a quoted span (a string literal, a quoted identifier) is part
    of a name or a value, so the spans are taken out first: an ``"(("`` in one place
    and an ``"))"`` in another must not cancel a parenthesis the statement really
    closes. A quote that never closes leaves its text in place and is counted."""
    depth = 0
    for ch in _QUOTED_SPAN_RE.sub(" ", expr):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


# BigQuery has no single ``DATE_TRUNC`` — the function is typed
# (``DATE_TRUNC`` / ``DATETIME_TRUNC`` / ``TIMESTAMP_TRUNC``) and picking the
# wrong one is a hard error. Every other dialect this compiler targets
# (snowflake / postgres / duckdb / redshift / athena-trino) accepts the ANSI
# ``DATE_TRUNC('<grain>', <expr>)`` form.
_BIGQUERY_TRUNC_BY_TYPE = {
    "date": "DATE_TRUNC",
    "datetime": "DATETIME_TRUNC",
    "timestamp": "TIMESTAMP_TRUNC",
    "timestamp_ntz": "DATETIME_TRUNC",
    "timestamp_tz": "TIMESTAMP_TRUNC",
    "timestamp_ltz": "TIMESTAMP_TRUNC",
}


def _truncate_to_grain(
    dimension_name: str,
    expr: str,
    grain: str,
    index: _SemanticIndex,
    dialect: Optional[str],
) -> str:
    """Wrap a time dimension's expression in the declared grain's truncation.

    ``dimensions[].typeParams.timeGranularity`` was read by the dbt export
    (``engines/dbt/semantic_models.py`` emits ``type_params.time_granularity``)
    but ignored here, so the governed query path grouped by the raw
    second-resolution timestamp: "revenue by day" over 20,000 orders returned
    20,000 groups of one order each, while the exported MetricFlow project
    computed 2,405 daily rows from the same contract. Two consumers, two
    answers.
    """
    if dialect == "bigquery":
        # Resolve the typed variant from the contract column the expression
        # names. Anything more complex than a bare column reference is not
        # safely typeable here, so fail closed rather than emit SQL BigQuery
        # rejects (the same posture ``_render_measure_expression`` takes for
        # percentile on BigQuery).
        column = index.columns.get(expr.strip())
        declared = str((column or {}).get("type") or "").strip().lower().split("(", 1)[0]
        function = _BIGQUERY_TRUNC_BY_TYPE.get(declared)
        if function is None:
            raise QueryValidationError(
                f"Dimension {dimension_name!r} declares timeGranularity {grain!r}, but "
                f"BigQuery's truncation function depends on the column type and "
                f"{expr!r} does not resolve to a declared date/datetime/timestamp "
                "column. Declare the column in contract.schema with a temporal type, "
                "or drop typeParams.timeGranularity."
            )
        return f"{function}({expr}, {grain.upper()})"
    return f"DATE_TRUNC('{grain}', {expr})"


def _resolve_dimension(
    dimension_name: str,
    index: _SemanticIndex,
    *,
    dialect: Optional[str] = None,
) -> Tuple[str, str]:
    """Return (alias, sql_expression) for a dimension reference.

    A dimension can come from the semantic block (preferred) or fall
    back to a contract column. Either way the expression is built
    from validated identifiers.

    A ``time`` dimension carrying ``typeParams.timeGranularity`` is truncated
    to that grain, so the governed query path answers the same number as the
    MetricFlow project exported from the same contract.
    """
    dimension = index.dimensions.get(dimension_name)
    if dimension is not None:
        expr = dimension.get("expr") or dimension_name
        if not isinstance(expr, str):
            raise QueryValidationError(f"Dimension {dimension_name!r} has non-string expr")
        validate_sql_expression_allowlist(expr)
        grain = _declared_grain(dimension)
        if grain is not None:
            expr = _truncate_to_grain(dimension_name, expr, grain, index, dialect)
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


def _declared_grain(dimension: Mapping[str, Any]) -> Optional[str]:
    """The canonical time grain a dimension declares, or ``None``.

    Only ``type: time`` dimensions carry a grain; the value is normalised
    through the repo's single grain vocabulary
    (:mod:`fluid_build.forge_datamodel.time_grains`) — the same one the dbt
    export uses — so an alias such as ``daily`` resolves identically on both
    surfaces and an unrecognised value is ignored rather than interpolated.
    """
    if str(dimension.get("type") or "").strip().lower() != "time":
        return None
    type_params = dimension.get("typeParams")
    if not isinstance(type_params, Mapping):
        return None
    from fluid_build.forge_datamodel.time_grains import normalize_time_grain

    return normalize_time_grain(type_params.get("timeGranularity"))


# ``measures[].agg`` → SQL aggregate, and the default percentile, shared with the
# Databricks metric view (``forge_datamodel/semantics_builder.py``) so every path that
# renders a measure as SQL aggregates it the same way. The default MUST stay identical
# to the dbt MetricFlow bridge's (``engines/dbt/semantic_models.py``, pinned by tests) so
# the governed query path and the exported dbt project answer the same number for the
# same contract. 0.5 == median.
_AGG_FUNCTIONS = _semantics_builder.AGGREGATE_FUNCTIONS
DEFAULT_PERCENTILE = _semantics_builder.DEFAULT_PERCENTILE


def _resolve_percentile_params(measure_name: str, measure: Mapping[str, Any]) -> Tuple[float, bool]:
    """Return (percentile, use_discrete) from ``measure.aggParams``.

    ``aggParams`` is the 0.7.6 contract slot mirroring
    dbt-semantic-interfaces' ``agg_params``. The percentile value is
    validated to a real number in [0, 1] — it is interpolated into SQL
    as a literal (aggregate arguments must be constants), so validation
    here is the safety boundary
    (:func:`~fluid_build.forge_datamodel.semantics_builder.percentile_params`).
    """
    try:
        return _semantics_builder.percentile_params(measure_name, measure)
    except ValueError as exc:
        raise QueryValidationError(str(exc)) from None


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
    projection_alias = validate_ident(alias or measure_name)
    if agg_raw == "percentile":
        if dialect in ("bigquery", "athena"):
            raise QueryValidationError(
                f"Measure {measure_name!r} uses agg 'percentile', which the "
                f"{dialect} engine does not support as a grouped aggregate. "
                "Use 'median'-free approximations engine-side or query a "
                "different measure."
            )
        # Validated here so the error is a QueryValidationError, as it always was.
        _resolve_percentile_params(measure_name, measure)
    return f"{_semantics_builder.aggregate_call(measure_name, measure, expr_raw)} AS {projection_alias}"


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

    ``dialect`` is the driver's dialect token (``descriptor().dialect``). It
    decides how a quoted name in an expression is read, and which engine a
    row-filter predicate is checked against (:func:`row_filter_predicate`).

    ``caller_attributes`` is the request's verified identity. When the expose
    declares ``policy.authz.rowFilters``, :func:`row_filter_predicate` resolves the
    rows the caller's principals may read, and the statement reads
    ``(SELECT <allowed columns> FROM <table> AS _fluid_rows WHERE <predicate>)``
    instead of the table: nothing in the outer statement (a measure, a dimension,
    a metric filter, a caller filter) can see a row the predicate removed. A
    caller no filter or reader names, a malformed filter, or the old
    ``policy.rowFilters`` key is refused before anything is compiled
    (:class:`RowFilterIdentityMissing`, :class:`RowFilterUnavailable`).

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

    Redaction follows the columns an expression NAMES, so on an expose that
    redacts columns an expression that would read one without naming it is
    refused (see :func:`_reject_unnamed_column_access`): a column selector,
    ``IDENTIFIER(...)``, a ``*`` that stands for columns, and the table itself or
    the compiler's projection of it used as a value. ``count(*)``, multiplication
    and aggregates over a sensitive column (``count(DISTINCT email)``,
    ``sum(salary)``) are unaffected.

    Every term of the WHERE is parenthesised, so a measure-, dimension- or
    filter-expression with an ``OR`` in it cannot widen a predicate ANDed to it.
    """
    if (metric is None) == (measure is None):
        raise QueryValidationError("Exactly one of 'metric' or 'measure' must be provided")
    if not isinstance(table_reference, str) or not table_reference.strip():
        raise QueryValidationError("table_reference must be a non-empty string")
    validate_sql_expression_allowlist(table_reference)

    # Row-level security first: a caller the policy does not name, or a filter that
    # cannot be applied, is refused before the payload is looked at.
    row_predicate = row_filter_predicate(expose, caller_attributes, dialect=dialect)

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

    # Expressions are free SQL, so naming a restricted column is only the FRIENDLY
    # refusal. The boundary is structural: with restrictions or a row filter, every
    # expression below is evaluated over a projection that does not contain the
    # restricted columns or the filtered rows (``source`` below), so a spelling this
    # scan does not recognise still fails at the engine. A subquery would name the
    # base table directly and skip that projection, so it is refused outright on a
    # policy-bearing expose, whoever the caller is.
    guard_subqueries = (
        bool(restricted)
        or bool(redacted)
        or row_predicate is not None
        or declares_row_filters(expose)
    )

    def _check_expression(expression: str, subject: str, *, allow_count_star: bool = False) -> None:
        _reject_restricted_reference(expression, restricted, subject=subject, dialect=dialect)
        if guard_subqueries:
            _reject_subquery(expression, subject=subject, dialect=dialect)
        if redacted:
            _reject_unnamed_column_access(
                expression,
                subject=subject,
                table_reference=table_reference,
                dialect=dialect,
                allow_count_star=allow_count_star,
            )

    measure_expr = measure_definition.get("expr") or measure_name
    if isinstance(measure_expr, str):
        _check_expression(
            measure_expr,
            f"Measure {measure_name!r}",
            allow_count_star=str(measure_definition.get("agg") or "").lower() == "count",
        )
    if metric_filter is not None:
        _check_expression(metric_filter, f"Metric {metric!r} filter")

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
        alias, expr = _resolve_dimension(dimension_name, index, dialect=dialect)
        _check_expression(expr, f"Dimension {dimension_name!r}")
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
        if _first_referenced_column(expr, redacted, dialect) is not None:
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
        and _first_referenced_column(measure_expr, redacted, dialect) is not None
    ):
        redacted_aliases.append(measure_alias)

    where_clauses: List[str] = []
    params: List[Any] = []
    if metric_filter is not None:
        # Contract-declared metric predicate (already allowlist-validated in
        # ``_resolve_metric``). Parenthesized so it ANDs safely with the
        # caller's dimension filters below.
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
            # Same expression as the SELECT/GROUP BY, grain truncation
            # included — filtering `order_date = '1992-01-02'` must match the
            # values the projection returns.
            _, dimension_expr = _resolve_dimension(filter_key, index, dialect=dialect)
        else:
            dimension_expr = validate_ident(filter_key)
        # An equality filter on a denied column is a working inference
        # oracle even though the column never appears in the projection:
        # ``filters: {ACCOUNT_BALANCE: 9999.99}`` returns the revenue of
        # exactly the rows carrying that balance. Reject the reference.
        _check_expression(dimension_expr, f"Filter key {filter_key!r}")
        # The dimension's expression is free SQL, so it is parenthesised: an ``OR`` in
        # it must not become the top-level operator of the WHERE it is ANDed into.
        _require_balanced_parentheses(dimension_expr, subject=f"Filter key {filter_key!r}")
        placeholder_index = len(params)
        where_clauses.append(f"(({dimension_expr}) = :p_{placeholder_index})")
        params.append(filter_value)

    if not isinstance(limit, int) or limit < 1 or limit > 1_000_000:
        raise QueryValidationError("limit must be an integer in [1, 1_000_000]")

    # With restrictions or a row filter the expressions read
    # ``(SELECT <allowed columns> FROM <table> AS _fluid_rows WHERE <predicate>)``
    # rather than the table: a restricted column is not in that relation, so no
    # spelling of it (quoted, selected by pattern, or as part of the whole row) can
    # reach it, and no row the predicate removed is in it either. The predicate is
    # policy rather than caller input, so it may name a column the agent is denied,
    # and nothing in the outer statement (whose WHERE terms are each parenthesised)
    # can see or widen what it removed.
    source = table_reference
    if restricted or row_predicate is not None:
        allowed = _allowed_columns(index, restricted)
        inner = f"SELECT {', '.join(allowed)} FROM {table_reference}"
        if row_predicate is not None:
            inner += f" AS {ROW_FILTER_ALIAS} WHERE {row_predicate}"
        source = f"({inner}) AS {_PROJECTION_ALIAS}"

    sql_lines: List[str] = [
        "SELECT " + ", ".join(select_parts),
        f"FROM {source}",
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
        row_filter=row_predicate,
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


# A table name that is really a file, a glob or a URL (DuckDB reads ``FROM 'x.csv'`` as
# a file scan). None of these is a catalog table, and one that names the bound file
# would read it without the projection.
_FILE_LIKE_TABLE_RE = re.compile(r"[./\\*:]")

# The clauses the compiler's projection sets: its columns, its FROM (``from_`` in
# current sqlglot, ``from`` before) and, with a row filter, its WHERE. Anything else on
# a projection the re-parse finds (a join, a GROUP BY, a QUALIFY, a sample) was not
# written by the compiler.
_PROJECTION_CLAUSES = frozenset({"expressions", "from", "from_", "where"})


def _declared_column_names(expose: Mapping[str, Any]) -> List[str]:
    names: List[str] = []
    for column in (expose.get("contract") or {}).get("schema") or []:
        if isinstance(column, Mapping) and isinstance(column.get("name"), str):
            names.append(validate_ident(column["name"]))
    return names


def _refuse_table_keyword(sql: str, read: Optional[str]) -> None:
    """Refuse a caller statement that uses the ``TABLE`` keyword.

    ``TABLE <name>`` reads a whole table in the engines that accept it (DuckDB,
    Postgres, Trino), and ``TABLE(...)`` is a table function in Snowflake. Parsers
    disagree on both, so the statement is checked at the token level, before it is
    parsed: no reading of the tree can skip it. A quoted ``"TABLE"`` is an identifier
    token, not the keyword, and stays allowed.
    """
    from sqlglot import TokenType
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.errors import SqlglotError

    try:
        tokens = Dialect.get_or_raise(read).tokenize(sql)
    except SqlglotError:
        raise _unreadable_sql() from None
    if any(token.token_type == TokenType.TABLE for token in tokens):
        raise QueryValidationError(
            "sql may not use the TABLE keyword on an expose that restricts columns or rows; "
            "select from the exposed table by name."
        )


def _unreadable_sql() -> QueryValidationError:
    return QueryValidationError(
        "sql could not be rewritten to read only the columns this expose allows; write "
        "one plain SELECT over the exposed table."
    )


def _is_plain_source(source: Any, *, allow_lateral: bool) -> bool:
    from sqlglot import exp

    if isinstance(source, exp.Table):
        return isinstance(source.this, exp.Identifier)
    if isinstance(source, (exp.Subquery, exp.Unnest, exp.Values)):
        return True
    return allow_lateral and isinstance(source, exp.Lateral)


def _require_plain_sources(tree: Any) -> None:
    """Refuse a statement with a source the rewrite does not know how to project.

    Everything in a ``FROM``, a ``JOIN`` or a ``LATERAL`` must be a table named by a
    plain identifier, a subquery, an ``UNNEST`` or a ``VALUES`` list. Anything else
    (a table function, ``TABLE(...)``, ``ROWS FROM``, or a node a later parser adds)
    could read the exposed table without being a table reference the rewrite sees, so
    it is refused by what it is, not by a list of the forms known today.
    """
    from sqlglot import exp

    for node in tree.find_all(exp.From, exp.Join, exp.Lateral):
        if not _is_plain_source(node.this, allow_lateral=isinstance(node, (exp.From, exp.Join))):
            raise QueryValidationError(
                "sql may only read from tables named in the usual way, subqueries, UNNEST "
                "and VALUES on an expose that restricts columns or rows; query the exposed "
                "table directly."
            )


def _without_locations(value: Any) -> Any:
    """A ``json_serialize_sql`` tree without its source positions, so two parses of one
    expression at different offsets compare equal."""
    if isinstance(value, dict):
        return {
            key: _without_locations(child)
            for key, child in value.items()
            if key not in ("query_location", "query_location_length")
        }
    if isinstance(value, list):
        return [_without_locations(child) for child in value]
    return value


def _duckdb_document(connection: Any, sql: str) -> Dict[str, Any]:
    """DuckDB's own parse of ``sql`` (``json_serialize_sql``; nothing is executed)."""
    import json

    from fluid_build.providers._duckdb_compat import first_row

    raw = first_row(connection.execute("SELECT json_serialize_sql(?)", [sql]))
    document = json.loads(raw[0]) if raw else {}
    return document if isinstance(document, dict) else {}


def _duckdb_predicate_where(connection: Any, predicate: str) -> Any:
    """The WHERE DuckDB parses ``predicate`` into on its own, without positions, or
    ``None`` when it does not parse."""
    alone = _duckdb_document(connection, f"SELECT 1 FROM t AS {ROW_FILTER_ALIAS} WHERE {predicate}")
    statements = alone.get("statements") or [{}]
    where = (statements[0].get("node") or {}).get("where_clause")
    return None if alone.get("error") or not where else _without_locations(where)


def _json_objects(document: Any) -> List[Dict[str, Any]]:
    """Every object in a ``json_serialize_sql`` document."""
    objects: List[Dict[str, Any]] = []
    pending = [document]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            objects.append(value)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return objects


def _is_bare_duckdb_projection(
    node: Dict[str, Any], permitted: Set[str], expected_where: Any
) -> bool:
    """Whether a SELECT_NODE is the compiler's projection: bare permitted columns from
    one table, under ``expected_where`` (the table read as :data:`ROW_FILTER_ALIAS`) or
    under no WHERE when there is no row filter, and nothing else."""
    source = node["from_table"]
    where = node.get("where_clause")
    if expected_where is None:
        filtered = not where
    else:
        filtered = (
            bool(where)
            and _without_locations(where) == expected_where
            and str(source.get("alias") or "").casefold() == ROW_FILTER_ALIAS
        )
    return (
        filtered
        and not node.get("modifiers")
        and not node.get("group_expressions")
        and not node.get("group_sets")
        and not node.get("having")
        and not node.get("qualify")
        and not node.get("sample")
        and not source.get("sample")
        and not source.get("at_clause")
        and not (node.get("cte_map") or {}).get("map")
        and bool(node.get("select_list"))
        and all(
            item.get("class") == "COLUMN_REF"
            and len(item.get("column_names") or []) == 1
            and str(item["column_names"][0]).casefold() in permitted
            for item in node["select_list"]
        )
    )


def _crosscheck_duckdb_projection(
    sql: str, exposed_name: str, allowed: Iterable[str], predicate: Optional[str] = None
) -> None:
    """Have DuckDB's own parser confirm the rewritten statement.

    The rewrite is checked with sqlglot, and sqlglot is not the engine: the two can
    read one statement differently. DuckDB serialises the statement it will run
    (``json_serialize_sql``, a parse with no execution), and every read of the exposed
    table in that tree must be the only source of a projection of bare allowed
    columns. With a row filter (``predicate``) that projection reads the table as
    :data:`ROW_FILTER_ALIAS` and its WHERE is the predicate, as DuckDB parses it on
    its own, and nothing else. A statement DuckDB cannot serialise is refused.
    """
    try:
        import duckdb
    except ImportError:  # pragma: no cover - a duckdb dialect implies duckdb
        return
    from fluid_build.providers._duckdb_sandbox import DuckDBAllowlist, secure_duckdb_connect

    refusal = QueryValidationError(
        "sql could not be checked against the columns this expose allows; write one "
        "plain SELECT over the exposed table."
    )
    permitted = {name.casefold() for name in allowed}
    # A parse with no execution, on a sandboxed in-memory connection with no file
    # or network access.
    connection = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    try:
        document = _duckdb_document(connection, sql)
        expected_where = (
            None if predicate is None else _duckdb_predicate_where(connection, predicate)
        )
    except (duckdb.Error, ValueError):
        raise refusal from None
    finally:
        connection.close()
    if document.get("error") or not document.get("statements"):
        raise refusal
    if predicate is not None and expected_where is None:
        raise refusal
    nodes = _json_objects(document)

    def _reads_exposed(node: Any) -> bool:
        return (
            isinstance(node, dict)
            and node.get("type") == "BASE_TABLE"
            and str(node.get("table_name") or "").casefold() == exposed_name
        )

    sanctioned = {
        id(node["from_table"])
        for node in nodes
        if node.get("type") == "SELECT_NODE"
        and _reads_exposed(node.get("from_table"))
        and _is_bare_duckdb_projection(node, permitted, expected_where)
    }
    if any(_reads_exposed(node) and id(node) not in sanctioned for node in nodes):
        raise refusal


def _refuse_unprojectable_reads(tree: Any, exposed_name: str) -> List[Any]:
    """The statement's tables, after refusing what the projection cannot cover: a CTE
    named like the exposed table, and a table function or a file path as a table."""
    from sqlglot import exp

    for cte in tree.find_all(exp.CTE):
        if cte.alias_or_name.casefold() == exposed_name:
            raise QueryValidationError(
                f"sql defines a CTE named {cte.alias_or_name!r}, the name of the exposed "
                "table; use another name."
            )
    tables = list(tree.find_all(exp.Table))
    for table in tables:
        if not isinstance(table.this, exp.Identifier) or _FILE_LIKE_TABLE_RE.search(table.name):
            raise QueryValidationError(
                "sql may not read from a table function or a file path on an expose that "
                "restricts columns or rows; query the exposed table directly."
            )
    return tables


def _splice_projection(sql: str, tables: List[Any], exposed: Any, projection: str) -> str:
    """``sql`` with ``projection`` written over the source span of every read of the
    exposed table, so everything else in the statement reaches the engine exactly as
    written (sqlglot cannot print every engine function it can parse)."""
    exposed_name = exposed.name.casefold()
    splices: List[Tuple[int, int, str]] = []
    for table in tables:
        if table.name.casefold() != exposed_name:
            continue
        spans = [part.meta for part in table.parts]
        if any("start" not in span or "end" not in span for span in spans):
            raise _unreadable_sql()
        replacement = projection if table.args.get("alias") else f"{projection} AS {exposed.name}"
        splices.append((min(m["start"] for m in spans), max(m["end"] for m in spans), replacement))
    rewritten = sql
    for begin, finish, replacement in sorted(splices, reverse=True):
        rewritten = rewritten[:begin] + replacement + rewritten[finish + 1 :]
    return rewritten


def _expected_projection_where(predicate: Optional[str], read: Optional[str]) -> Optional[str]:
    """The WHERE a filtered projection must carry, as sqlglot prints it, or ``None``."""
    if predicate is None:
        return None
    import sqlglot
    from sqlglot.errors import SqlglotError

    try:
        tree = sqlglot.parse_one(
            f"SELECT 1 FROM t AS {ROW_FILTER_ALIAS} WHERE {predicate}", read=read
        )
        return str(tree.args["where"].this.sql(dialect=read))
    except (SqlglotError, KeyError, AttributeError):
        raise _unreadable_sql() from None


def _is_sanctioned_projection(
    table: Any, allowed_names: Set[str], expected_where: Optional[str], read: Optional[str]
) -> bool:
    """Whether a read of the exposed table is the compiler's projection and nothing else:
    the sole source of a subquery that selects bare allowed columns and, with a row
    filter, reads the table as :data:`ROW_FILTER_ALIAS` under exactly that WHERE."""
    from sqlglot import exp

    select = table.parent.parent if isinstance(table.parent, exp.From) else None
    if not isinstance(select, exp.Select) or not isinstance(select.parent, exp.Subquery):
        return False
    if any(value for key, value in select.args.items() if key not in _PROJECTION_CLAUSES):
        return False
    if not all(
        isinstance(item, exp.Column) and not item.table and item.name.casefold() in allowed_names
        for item in select.expressions
    ):
        return False
    where = select.args.get("where")
    if expected_where is None:
        return where is None
    return (
        where is not None
        and where.this.sql(dialect=read) == expected_where
        and table.alias.casefold() == ROW_FILTER_ALIAS
    )


def _project_exposed_table(
    sql: str,
    *,
    table_reference: str,
    allowed: List[str],
    dialect: Optional[str],
    predicate: Optional[str] = None,
) -> str:
    """Rewrite caller SQL so every read of the exposed table goes through a projection
    of the ``allowed`` columns, limited to the rows ``predicate`` selects.

    Each reference to the table, wherever it sits (the outer ``FROM``, a join, a
    self-join, a subquery, a CTE body, a set operation's branch), becomes
    ``(SELECT <allowed columns> FROM <table_reference>) AS <the caller's alias>``, or
    with a row filter (:func:`row_filter_predicate`)
    ``(SELECT <allowed columns> FROM <table_reference> AS _fluid_rows WHERE
    <predicate>) AS <the caller's alias>``. A restricted column is then not part of
    any relation the statement can see, whether it is written as a name, a quoted
    name, ``COLUMNS(...)``, ``*``, ``alias.*`` or the whole row, and neither is a row
    the predicate removed, however many times or in whatever form the statement
    reads the table. A table function or a file path as a table is refused, because
    it would read the data without the projection. The rewritten statement is parsed
    again and checked, so a printing quirk cannot leave a bare reference behind or
    change the predicate.

    This is the shape of Apache Superset's sqlglot-based row-level security
    (``RLSAsSubqueryTransformer``, Apache-2.0), with two differences: the projection
    is spliced into the caller's own text instead of printed by sqlglot, which cannot
    print every engine function it parses, and it selects only the allowed columns
    instead of ``*``.

    ``sqlglot`` is imported here, not at module scope, to keep it off the cold path.
    """
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    read = _SQLGLOT_DIALECTS.get((dialect or "").strip().lower())
    unreadable = _unreadable_sql()
    # Before anything is parsed: no reading of the statement can skip this.
    _refuse_table_keyword(sql, read)
    try:
        statements = sqlglot.parse(sql, read=read)
        exposed = exp.to_table(table_reference, dialect=read)
    except SqlglotError:
        raise unreadable from None
    if len(statements) != 1 or statements[0] is None or not exposed.name:
        raise unreadable
    tree = statements[0]
    _require_plain_sources(tree)
    exposed_name = exposed.name.casefold()
    tables = _refuse_unprojectable_reads(tree, exposed_name)
    row_filter = "" if predicate is None else f" AS {ROW_FILTER_ALIAS} WHERE {predicate}"
    projection = f"(SELECT {', '.join(allowed)} FROM {table_reference}{row_filter})"
    rewritten = _splice_projection(sql, tables, exposed, projection)
    try:
        checked = sqlglot.parse_one(rewritten, read=read)
    except SqlglotError:
        raise unreadable from None
    _require_plain_sources(checked)
    expected_where = _expected_projection_where(predicate, read)
    allowed_names = {name.casefold() for name in allowed}
    for table in checked.find_all(exp.Table):
        # The only place the exposed table may appear is as the sole source of a
        # projection that selects bare allowed columns, filtered by the predicate and
        # nothing else.
        if table.name.casefold() == exposed_name and not _is_sanctioned_projection(
            table, allowed_names, expected_where, read
        ):
            raise QueryValidationError(
                "sql reads the exposed table in a form that cannot be limited to the columns "
                "this expose allows; write one plain SELECT over the exposed table."
            )
    if any(marker in rewritten for marker in (";", "--", "/*", "*/")):
        raise ValueError("Rewritten statement contains a forbidden marker")
    if read == "duckdb":
        _crosscheck_duckdb_projection(rewritten, exposed_name, allowed, predicate)
    return rewritten


# A caller statement that already ends in ``LIMIT <n>`` (case-insensitive,
# trailing whitespace tolerated). Used so the server-side ``LIMIT`` is not
# appended a second time — two trailing LIMITs is a syntax error on most
# engines. Only a bare integer literal counts as "already limited"; a
# ``LIMIT`` with an expression / placeholder is left for the caller to own.
_TRAILING_LIMIT_RE = re.compile(r"(?is)\blimit\s+(\d+)\s*$")

# A reference to a DuckDB logging function or view, in any case, quoted or qualified.


def compile_free_form_sql(
    *,
    sql: str,
    table_reference: str,
    limit: int,
    restricted_columns: Iterable[str] = (),
    expose: Optional[Mapping[str, Any]] = None,
    caller_attributes: Optional[Mapping[str, Any]] = None,
    dialect: Optional[str] = None,
) -> CompiledQuery:
    """Compile a caller-supplied SQL ``query_sql`` payload.

    ``dialect`` is the driver's dialect token. It decides whether ``"..."`` in the
    caller's text is a string literal (BigQuery, Spark) or a quoted identifier (every
    other engine), for the restricted-column scan below, and which engine a row-filter
    predicate is checked against. ``None`` reads ``"..."`` as an identifier, the
    stricter choice.

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
      This is column-level deny only; row-level security is the next bullets.
    * **Row-level security.** When ``expose`` declares
      ``policy.authz.rowFilters``, :func:`row_filter_predicate` resolves the rows
      ``caller_attributes`` (the request's verified identity) may read, and every
      read of the exposed table is rewritten to
      ``(SELECT <allowed columns> FROM <table> AS _fluid_rows WHERE <predicate>)``.
      The filter is applied where the table is read, not around the caller's
      statement: wrapping it as ``SELECT * FROM (<caller_sql>) WHERE <filter>``
      lets the caller's projection spoof the filter column (``SELECT 't1' AS
      tenant_id, secret FROM other``), while a subquery, a CTE, a set operation's
      branch or a second reference to the table each read the filtered projection
      here. It fails closed: a caller no filter or reader names raises
      :class:`RowFilterIdentityMissing`, and a filter that cannot be applied (or
      the old ``policy.rowFilters`` key) :class:`RowFilterUnavailable`.
    * **Column restrictions.** When ``restricted_columns`` is non-empty, ``expose``
      is required (it declares the columns to project; without it the call is
      refused). Every read of the exposed table is then rewritten to a projection
      of the allowed columns, filtered as above when a row filter applies. The
      statement may not use the ``TABLE`` keyword, and every source in it must be a
      plain table, a subquery, ``UNNEST`` or ``VALUES``; on DuckDB the engine's own
      parser checks the rewritten statement too, the row filter included.
    * ``FROM`` references are NOT rewritten otherwise — the caller must
      reference the bound table by its fully-qualified name from the contract.
      Phase-2 may add an AST-level rewrite so consumers can reference the expose
      by exposeId rather than the underlying binding.
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
    if names_refused_duckdb_function(candidate):
        raise QueryValidationError(
            "sql may not use DuckDB's logging functions or views (enable_logging, "
            "disable_logging, truncate_duckdb_logs, write_log, duckdb_logs, "
            "duckdb_logs_parsed, duckdb_log_contexts) or run a statement given as text "
            "(query, query_table, json_execute_serialized_sql). Remove the reference and "
            "query the exposed table directly."
        )
    if names_refused_postgres_function(candidate):
        raise QueryValidationError(
            "sql may not call a function that returns a whole table, schema, database or "
            "query result as XML (table_to_xml, query_to_xml, cursor_to_xml, "
            "schema_to_xml, database_to_xml and their variants). Select the columns of the "
            "exposed table directly."
        )
    body = parts[1]
    validate_sql_expression_allowlist(body)
    if not isinstance(limit, int) or limit < 1 or limit > 1_000_000:
        raise QueryValidationError("limit must be an integer in [1, 1_000_000]")
    if not isinstance(table_reference, str) or not table_reference.strip():
        raise QueryValidationError("table_reference must be a non-empty string")
    validate_sql_expression_allowlist(table_reference)
    # Row-level security first: a caller the policy does not name, or a filter that
    # cannot be applied, is refused before the statement is looked at further.
    row_predicate = row_filter_predicate(expose or {}, caller_attributes, dialect=dialect)
    # Column-mask enforcement — see the ``restricted_columns`` bullet
    # in the docstring above. Strip quoted string literals first so
    # ``WHERE label = 'email'`` doesn't false-positive when ``email``
    # is itself a restricted column.
    if restricted_columns:
        scanned = _strip_string_literals(candidate, dialect)
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
    # Column restrictions and row filters are enforced by structure, not only by the
    # name scan above: every read of the exposed table is rewritten to a projection of
    # the allowed columns, limited to the caller's rows, so a spelling the scan cannot
    # see (``COLUMNS('..')``, ``*``, ``t.*``, the whole row, a quoted name) has no
    # restricted column to reach, and no form of the statement (a subquery, a CTE, a
    # set operation, a second reference) a filtered row. Callers that pass no
    # ``expose`` have no declared columns to project, so they are refused when
    # columns are restricted (the MCP ``query_sql`` handler always passes one).
    restricted = _clean_column_names(restricted_columns)
    if restricted and expose is None:
        raise QueryValidationError(
            "sql cannot be compiled on an expose that restricts columns without the expose "
            "itself: the columns it declares are what the statement is limited to."
        )
    if expose is not None and (restricted or row_predicate is not None):
        candidate = _project_exposed_table(
            candidate,
            table_reference=table_reference,
            allowed=_allowed_names(_declared_column_names(expose), restricted),
            dialect=dialect,
            predicate=row_predicate,
        )
    # Append the server-side cap, but only when the caller didn't already supply a
    # trailing ``LIMIT <n>`` (a second one is a syntax error on most engines).
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
    return CompiledQuery(
        sql=final_sql, params=[], columns=[], row_cap=row_cap, row_filter=row_predicate
    )
