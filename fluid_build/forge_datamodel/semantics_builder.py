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

"""Shared primitives for building ``exposes[].semantics`` blocks.

The contract semantics block has two independent producers — the
data-model pipeline (``forge_datamodel/emit/fluid_contract.py``) and the
copilot interview path (``cli/forge_copilot_contract_helpers.py``) —
which historically shared no code and drifted (different agg inference,
different grain handling, neither populating
``defaultAggTimeDimension``). The assembly idioms that must agree across
producers live here; per-path naming conventions (pinned by each path's
tests) stay with the paths.

The headline fix carried by this module: OSI metric expressions are
whole aggregate calls (``SUM(amount)``), and the previous translation
copied them verbatim into ``measures[].expr`` next to an inferred
``agg`` — so both consumers double-aggregated (the MCP query compiler
rendered ``SUM(SUM(amount))``, invalid SQL on every engine, and the dbt
MetricFlow bridge exported the same double wrap).
:func:`measure_from_aggregate_expression` strips the outer aggregate
when the expression is a single aggregate call, and
:func:`infer_measure_agg` classifies ``COUNT(DISTINCT …)`` as
``count_distinct`` (previously misfiled as plain ``count``).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fluid_build.forge_datamodel import time_grains as _time_grains

# Single top-level aggregate call, e.g. ``SUM(amount)`` / ``count( * )``.
# The inner expression is validated for balanced parens separately so
# ``SUM(a) / COUNT(b)`` (which this regex also matches end-to-end with
# inner ``a) / COUNT(b``) is rejected as "not a single aggregate".
_AGGREGATE_CALL_RE = re.compile(
    r"(?is)^\s*(sum|avg|min|max|median|count)\s*\((.*)\)\s*$",
)
_DISTINCT_PREFIX_RE = re.compile(r"(?is)^\s*distinct\s+(.+)$")

# ANY aggregate function call anywhere in an expression — broader than
# :data:`_AGGREGATE_CALL_RE`, which only matches an expression that is
# EXACTLY one aggregate call. Used by the contract validator to reject
# the double-aggregation shape, including the compound forms the
# single-call parser deliberately skips (``SUM(a) / COUNT(b)``). The
# trailing ``\s*\(`` is what keeps a column named ``count_of_orders``
# from matching.
_ANY_AGGREGATE_CALL_RE = re.compile(
    r"(?i)\b("
    r"sum|avg|mean|min|max|median|count|"
    r"stddev|stddev_pop|stddev_samp|variance|var_pop|var_samp|"
    r"percentile_cont|percentile_disc|approx_percentile|approx_count_distinct|"
    r"array_agg|string_agg|listagg|group_concat|bool_and|bool_or|any_value"
    r")\s*\(",
)


def first_aggregate_call(expr: str) -> Optional[str]:
    """Return the name of the first aggregate function ``expr`` calls, or
    ``None`` when it calls none.

    A ``measures[].expr`` is the PRE-aggregation input — the declared
    ``agg`` is applied to it — so an aggregate call inside it means the
    contract double-aggregates (``{agg: sum, expr: SUM(amount)}`` →
    ``SUM(SUM(amount))``). A ``dimensions[].expr`` lands in the GROUP BY,
    where an aggregate is equally invalid. Both are rejected by
    ``cli/contract_validation.ContractValidator._validate_semantics``.
    """
    match = _ANY_AGGREGATE_CALL_RE.search(expr or "")
    return match.group(1).lower() if match is not None else None


def parse_aggregate_expression(expr: str) -> Optional[Tuple[str, str]]:
    """Split a single-aggregate SQL expression into (agg, inner expr).

    Returns ``None`` when ``expr`` is not exactly one aggregate call
    (complex expressions like ``SUM(a) / COUNT(b)`` belong in derived /
    ratio metrics, not measures). ``COUNT(DISTINCT x)`` maps to
    ``("count_distinct", "x")``; ``COUNT(*)`` maps to ``("count", "1")``
    — the dbt convention for a row count, and valid SQL either way.
    """
    match = _AGGREGATE_CALL_RE.match(expr or "")
    if match is None:
        return None
    func = match.group(1).lower()
    inner = match.group(2).strip()
    if not _parens_balanced(inner):
        return None
    if func == "count":
        distinct = _DISTINCT_PREFIX_RE.match(inner)
        if distinct is not None:
            return "count_distinct", distinct.group(1).strip()
        if inner == "*":
            return "count", "1"
        return "count", inner
    return func, inner


def infer_measure_agg(expr: str) -> str:
    """Best-effort agg classification for an aggregate expression.

    Falls back to ``sum`` for anything unrecognized — the historical
    default, kept so complex expressions degrade the same way they
    always have."""
    parsed = parse_aggregate_expression(expr)
    return parsed[0] if parsed is not None else "sum"


def measure_from_aggregate_expression(
    name: str,
    expr: str,
    *,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a ``measures[]`` entry from an aggregate expression.

    Single aggregate calls are split into ``agg`` + inner ``expr`` so
    consumers apply the aggregation exactly once. Expressions that are
    not a single aggregate keep the legacy verbatim shape (``agg: sum``
    + full expr) — still wrong for consumers, but that class needs
    derived/ratio metric emission, not a measure, and silently mangling
    it here would hide the modeling gap.
    """
    parsed = parse_aggregate_expression(expr)
    if parsed is not None:
        agg, inner = parsed
        measure: Dict[str, Any] = {"name": name, "agg": agg, "expr": inner}
    else:
        measure = {"name": name, "agg": "sum", "expr": expr}
    if description:
        measure["description"] = description
    return measure


def validate_semantics_block(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """Validate-time gate for ``exposes[].semantics`` (anti-no-op, mirrors
    ``output_ports.vector.validate_vector_binding``). Returns
    ``(errors, warnings)``.

    Catches the DOUBLE-AGGREGATION shape. A ``measures[]`` entry declares
    its aggregation in ``agg`` and its PRE-aggregation input in ``expr``,
    so an ``expr`` that is itself an aggregate call double-wraps:
    ``{agg: sum, expr: SUM(TOTAL_PRICE)}`` compiles to
    ``SUM(SUM(TOTAL_PRICE))`` on the governed MCP ``query`` path and
    exports the same double wrap through the dbt MetricFlow bridge —
    invalid SQL on every engine. ``dimensions[].expr`` has the same
    problem: it lands in the GROUP BY.

    The forge emitters stopped producing that shape (see
    :func:`measure_from_aggregate_expression`), but nothing caught it in
    a HAND-AUTHORED or third-party contract: ``fluid validate`` reported
    "✅ Valid" with zero warnings and ``fluid policy-check --strict``
    scored 100/100, and the only feedback was an opaque engine error on
    the first query. Hard errors, because the shape cannot produce a
    working query on any engine.

    Also WARNS on a NAME COLLISION between a dimension and a metric or
    measure in the same expose. Every mainstream semantic layer treats
    those as one namespace and rejects the duplicate at model-build time
    (dbt MetricFlow registers entities / dimensions / measures / metrics
    in a single global namespace; Cube requires ``name`` to be unique
    among all dimensions, measures and segments —
    https://cube.dev/docs/dimensions/). Snowflake Semantic Views tolerate
    it and then emit two output columns with the same name, which the
    docs tell you to fix by renaming through a table alias
    (https://docs.snowflake.com/en/user-guide/views-semantic/querying).
    Our governed ``query`` path does that rename automatically where it
    can, and rejects the request where it can't — either way the contract
    is ambiguous and the author should know. A WARNING, not an error:
    contracts carrying the collision still answer queries correctly, so
    failing them outright would break working models.
    """
    errors: List[str] = []
    warnings: List[str] = []
    exposes = contract.get("exposes")
    if not isinstance(exposes, list):
        return errors, warnings
    for expose in exposes:
        if not isinstance(expose, Mapping):
            continue
        semantics = expose.get("semantics")
        if not isinstance(semantics, Mapping):
            continue
        expose_id = str(expose.get("exposeId") or expose.get("id") or "?")
        for entry in semantics.get("measures") or []:
            if not isinstance(entry, Mapping):
                continue
            agg = entry.get("agg")
            expr = entry.get("expr")
            if not agg or not isinstance(expr, str):
                continue
            nested = first_aggregate_call(expr)
            if nested is None:
                continue
            errors.append(
                f"expose '{expose_id}': measure "
                f"'{entry.get('name') or '?'}' declares agg '{agg}' over an expr "
                f"that is itself an aggregate ({expr!r}). Consumers apply 'agg' "
                f"to 'expr', so this compiles to "
                f"{str(agg).upper()}({nested.upper()}(...)) — invalid SQL on "
                f"every engine. Split them: 'expr: SUM(amount)' + 'agg: sum' "
                f"becomes 'expr: amount' + 'agg: sum'; "
                f"'expr: COUNT(DISTINCT id)' becomes 'expr: id' + "
                f"'agg: count_distinct'."
            )
        for entry in semantics.get("dimensions") or []:
            if not isinstance(entry, Mapping):
                continue
            expr = entry.get("expr")
            if not isinstance(expr, str):
                continue
            nested = first_aggregate_call(expr)
            if nested is None:
                continue
            errors.append(
                f"expose '{expose_id}': dimension "
                f"'{entry.get('name') or '?'}' has an aggregate expr ({expr!r}). "
                f"Dimensions land in the GROUP BY, where {nested.upper()}(...) "
                f"is invalid on every engine. Model it as a measure, or group "
                f"by the underlying column."
            )
        warnings.extend(_name_collision_warnings(semantics, expose_id))
    return errors, warnings


def _semantic_names(semantics: Mapping[str, Any], kind: str) -> Dict[str, str]:
    """Case-folded name → as-authored name for one semantics collection.

    Case-folded because the collision that matters is the one an ENGINE
    sees: unquoted identifiers fold, so ``Order_Status`` and
    ``order_status`` are the same output column name.
    """
    found: Dict[str, str] = {}
    for entry in semantics.get(kind) or []:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if isinstance(name, str) and name:
            found.setdefault(name.casefold(), name)
    return found


def _name_collision_warnings(semantics: Mapping[str, Any], expose_id: str) -> List[str]:
    """Warn when a metric or measure shares a dimension's name.

    A ``query`` that selects the metric/measure AND groups by the
    identically-named dimension would project two columns with one name;
    the drivers key rows with ``dict(zip(columns, values))``, so one of
    the two values would be silently dropped. ``compile_semantic_query``
    now renames or rejects instead — this warning names the underlying
    modelling problem at validate time rather than leaving it to be
    discovered on the first query.
    """
    messages: List[str] = []
    dimensions = _semantic_names(semantics, "dimensions")
    if not dimensions:
        return messages
    measures = _semantic_names(semantics, "measures")
    metric_measure: Dict[str, Any] = {}
    for entry in semantics.get("metrics") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str):
            metric_measure.setdefault(str(entry["name"]).casefold(), entry.get("measure"))
    for folded, metric_name in _semantic_names(semantics, "metrics").items():
        if folded not in dimensions:
            continue
        measure_ref = metric_measure.get(folded)
        fallback_taken = isinstance(measure_ref, str) and measure_ref.casefold() in dimensions
        messages.append(
            f"expose '{expose_id}': metric '{metric_name}' has the same name as "
            f"dimension '{dimensions[folded]}'. Metric, measure and dimension "
            f"names share one namespace (dbt MetricFlow, Cube), so a query for "
            f"this metric grouped by that dimension would project two columns "
            f"with one name. The governed query path "
            + (
                f"REJECTS that request — measure '{measure_ref}' collides too, so "
                f"there is no distinct name left to fall back to."
                if fallback_taken
                else f"falls back to the measure name ('{measure_ref}') for the "
                f"aggregate column."
            )
            + " Rename one of them."
        )
    for folded, measure_name in measures.items():
        if folded not in dimensions:
            continue
        messages.append(
            f"expose '{expose_id}': measure '{measure_name}' has the same name as "
            f"dimension '{dimensions[folded]}'. Metric, measure and dimension "
            f"names share one namespace (dbt MetricFlow, Cube); a query for this "
            f"measure grouped by that dimension is rejected because both columns "
            f"would be projected under one name and one value would be silently "
            f"dropped from every row. Rename one of them."
        )
    return messages


def simple_metric(name: str, measure: str, *, description: Optional[str] = None) -> Dict[str, Any]:
    metric: Dict[str, Any] = {"name": name, "type": "simple", "measure": measure}
    if description:
        metric["description"] = description
    return metric


def normalized_time_type_params(grain: Optional[str]) -> Optional[Dict[str, str]]:
    """``typeParams`` for a time dimension, or ``None`` when the grain
    doesn't normalize — emitters must omit rather than write an
    enum-invalid contract."""
    canonical = _time_grains.normalize_time_grain(grain)
    if canonical is None:
        return None
    return {"timeGranularity": canonical}


def default_agg_time_dimension(dimensions: List[Mapping[str, Any]]) -> Optional[str]:
    """The model-level default aggregation time dimension: the first
    declared time dimension. Populating it (instead of leaving the
    schema field write-never) lets consumers skip their fallback
    heuristics and makes the choice visible in the contract."""
    for dimension in dimensions:
        if dimension.get("type") == "time" and dimension.get("name"):
            return str(dimension["name"])
    return None


def _parens_balanced(expr: str) -> bool:
    depth = 0
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0
