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
