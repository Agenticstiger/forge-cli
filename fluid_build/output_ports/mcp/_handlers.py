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

"""Tool-call implementations for the MCP output-port server.

Physically extracted from the ``OutputPortMcpServer`` god-class in
``server.py`` (which carried lifespan + protocol handlers + tool
impls + breaker + transport). These are the actual read-path tool
bodies; ``server.py`` keeps thin delegating methods
(``_tool_describe`` / ``_tool_sample`` / ``_tool_query`` /
``_tool_query_sql``) that the dispatcher calls via
``run_in_executor``, so the class's method surface and the dispatch
path are unchanged.

Each function takes the lifespan-bound ``SessionState`` explicitly
(rather than ``self``) so the tool logic is unit-testable without
standing up a full server. ``SessionState`` is imported only under
``TYPE_CHECKING`` — at runtime these duck-type against
``state.get_driver()`` / ``.expose`` / ``.policy`` /
``.caller_attributes`` / ``.query_timeout_seconds``. That keeps
``server.py → _handlers`` the only import edge (no cycle), since
``_handlers`` imports nothing from ``server``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional

from ._expose_utils import _jsonable
from .query_compiler import compile_free_form_sql, compile_semantic_query

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .server import SessionState


def _serialize_query_result(
    expose: Mapping[str, Any], compiled: Any, result: Any
) -> Dict[str, Any]:
    """Shape the query result for the wire."""
    return {
        "exposeId": expose.get("exposeId"),
        "columns": list(getattr(result, "columns", ())),
        "rows": [_jsonable(row) for row in getattr(result, "rows", ())],
        "rowCount": len(getattr(result, "rows", ()) or ()),
        "truncated": getattr(result, "truncated", False),
        "compiled": {
            "sql": getattr(compiled, "sql", None),
            "parameters": getattr(compiled, "parameters", None),
        },
    }


def tool_describe(state: "SessionState") -> Dict[str, Any]:
    driver = state.get_driver()
    descriptor = driver.descriptor()
    return {
        "exposeId": state.expose.get("exposeId"),
        "title": state.expose.get("title"),
        "kind": state.expose.get("kind"),
        "version": state.expose.get("version"),
        "contract": _jsonable(state.expose.get("contract") or {}),
        "semantics": _jsonable(state.expose.get("semantics") or {}),
        "binding": {
            "platform": descriptor.platform,
            "format": descriptor.format,
            "tableReference": descriptor.table_reference,
            "dialect": descriptor.dialect,
            "capabilities": dict(descriptor.capabilities),
        },
        "agentPolicy": _jsonable(((state.expose.get("policy") or {}).get("agentPolicy") or {})),
    }


def tool_sample(
    state: "SessionState",
    arguments: Mapping[str, Any],
    *,
    caller_attributes: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    driver = state.get_driver()
    requested = arguments.get("limit", 10)
    try:
        limit = int(requested)
    except (TypeError, ValueError):
        limit = 10
    cap = state.policy.max_sample_rows
    effective = min(max(limit, 1), cap)
    # Pass caller_attributes so any policy.rowFilters[] in the
    # contract resolve their ${caller.*} placeholders against
    # THIS request's caller identity. The kwarg is the per-request
    # identity threaded down from the dispatcher; it falls back to
    # ``state.caller_attributes`` only for legacy callers that don't
    # pass it (back-compat for existing tests + stdio). Drivers that
    # don't override sample() use the base impl, which compiles the
    # filter into a parameterised WHERE clause.
    attrs = caller_attributes if caller_attributes is not None else state.caller_attributes
    result = driver.sample(limit=effective, caller_attributes=attrs)
    return {
        "exposeId": state.expose.get("exposeId"),
        "columns": list(result.columns),
        "rows": [_jsonable(row) for row in result.rows],
        "rowCount": len(result.rows),
        "truncated": result.truncated,
        "requestedLimit": limit,
        "effectiveLimit": effective,
    }


def _resolve_limit(requested: Any, *, cap: int) -> int:
    """Clamp an optional caller-supplied row limit into ``[1, cap]``.

    The ``query`` / ``query_sql`` tool schemas declare ``limit`` with
    ``minimum: 1`` but NO default, so an LLM that omits it sends no
    value — yet the compilers require a concrete int in
    ``[1, 1_000_000]`` and reject ``None``. Default a missing limit to
    the server cap (the same value ``sample`` uses) and clamp any
    provided value down to the cap, so a curious agent can't widen the
    row budget past ``--max-sample-rows``.
    """
    if requested is None:
        return max(1, cap)
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return max(1, cap)
    return min(max(value, 1), cap)


def tool_query(
    state: "SessionState",
    arguments: Mapping[str, Any],
    *,
    caller_attributes: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    driver = state.get_driver()
    descriptor = driver.descriptor()
    args = dict(arguments)
    attrs = caller_attributes if caller_attributes is not None else state.caller_attributes
    # compile_semantic_query takes the individual semantic fields +
    # a driver-built ``table_reference`` — NOT an ``arguments`` dict or
    # a ``descriptor``. (Calling it with those was a long-standing bug
    # that raised TypeError before any engine round-trip, so the
    # ``query`` tool never worked end-to-end.)
    compiled = compile_semantic_query(
        expose=state.expose,
        metric=args.get("metric"),
        measure=args.get("measure"),
        dimensions=args.get("dimensions"),
        filters=args.get("filters"),
        limit=_resolve_limit(args.get("limit"), cap=state.policy.max_sample_rows),
        # Enforce policy.rowFilters[] on the query tool (was bypassed — only
        # sample applied them); merged into the compiled WHERE. ``attrs`` is
        # this request's caller identity (falls back to state for legacy
        # callers that don't pass the kwarg).
        caller_attributes=attrs,
        table_reference=descriptor.table_reference,
    )
    result = driver.query(compiled=compiled, timeout_seconds=state.query_timeout_seconds)
    return _serialize_query_result(state.expose, compiled, result)


def tool_query_sql(
    state: "SessionState",
    arguments: Mapping[str, Any],
    *,
    caller_attributes: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    driver = state.get_driver()
    descriptor = driver.descriptor()
    args = dict(arguments)
    attrs = caller_attributes if caller_attributes is not None else state.caller_attributes
    # Block free-form SQL from referencing EITHER a column-restricted
    # OR a PII-marked column. Both are masked/redacted by the driver's
    # row-level ``project()``, but that step matches by output column
    # NAME — so ``SELECT email AS x`` would alias past it. Rejecting
    # the column at compile time closes that alias-bypass leak for both
    # masking classes (project() only ever saw restricted columns;
    # PII columns had the same hole on the free-form path).
    restricted = set(getattr(driver, "_restricted_columns", set())) | set(
        getattr(driver, "_pii_columns", set())
    )
    # compile_free_form_sql takes ``sql`` / ``table_reference`` /
    # ``limit`` / ``restricted_columns`` — NOT an ``arguments`` dict or
    # a ``descriptor`` (same long-standing signature bug as ``query``).
    compiled = compile_free_form_sql(
        sql=args.get("sql", ""),
        table_reference=descriptor.table_reference,
        limit=_resolve_limit(args.get("limit"), cap=state.policy.max_sample_rows),
        restricted_columns=restricted,
        # Enforce policy.rowFilters[] on the free-form query_sql tool too (was
        # bypassed). Can't merge a WHERE into arbitrary SQL, so the compiler
        # wraps it: SELECT * FROM (<caller_sql>) WHERE <rowfilter> LIMIT n.
        # ``attrs`` is this request's caller identity (falls back to state
        # for legacy callers that don't pass the kwarg).
        expose=state.expose,
        caller_attributes=attrs,
    )
    result = driver.query(compiled=compiled, timeout_seconds=state.query_timeout_seconds)
    return _serialize_query_result(state.expose, compiled, result)
