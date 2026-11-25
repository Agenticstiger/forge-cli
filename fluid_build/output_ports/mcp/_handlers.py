# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional

from ._expose_utils import _jsonable
from .drivers.base import EngineDriver, UnsupportedBindingError
from .query_compiler import (
    QueryValidationError,
    RelatedTable,
    compile_free_form_sql,
    compile_semantic_query,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .server import SessionState


def _shown_sql(compiled: Any) -> Any:
    """The compiled statement as the caller may see it: a row-filter predicate (policy
    the ``describe`` tool does not return either) reads ``(<row filter>)``."""
    shown = getattr(compiled, "shown_sql", None)
    return shown() if callable(shown) else getattr(compiled, "sql", None)


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
            "sql": _shown_sql(compiled),
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
    # Pass caller_attributes so any policy.authz.rowFilters in the
    # contract resolve against THIS request's verified principal. The
    # kwarg is the per-request identity threaded down from the
    # dispatcher; it falls back to ``state.caller_attributes`` only for
    # callers that don't pass it (tests). Drivers that don't override
    # sample() use the base impl, which limits the read to the rows the
    # principal may read.
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


_LOG = logging.getLogger("fluid.output_port.mcp.query")


def _named_relationships(expose: Mapping[str, Any], args: Mapping[str, Any]) -> List[str]:
    """The as-of relationships of ``expose`` that ``args`` name: a dimension or a filter key
    ``<relationship>.<column>`` (exactly one dot) whose prefix is one of its
    ``semantics.relationships[]`` with a non-empty ``asOf``, in the order named."""
    semantics = expose.get("semantics")
    listed = semantics.get("relationships") if isinstance(semantics, Mapping) else None
    as_of = {
        r["name"]
        for r in (listed if isinstance(listed, list) else [])
        if isinstance(r, Mapping) and isinstance(r.get("name"), str) and r.get("asOf")
    }
    dimensions = args.get("dimensions")
    filters = args.get("filters")
    names = [*(dimensions if isinstance(dimensions, list) else [])]
    names += list(filters) if isinstance(filters, Mapping) else []
    found: List[str] = []
    for name in names:
        if isinstance(name, str) and name.count(".") == 1:
            prefix = name.split(".")[0]
            if prefix in as_of and prefix not in found:
                found.append(prefix)
    return found


def _related_tables(
    state: "SessionState", driver: Any, args: Mapping[str, Any]
) -> Dict[str, RelatedTable]:
    """The tables the as-of relationships a ``query`` names reach, each with its own column
    policy, or nothing when it names none. The target's table is read as THIS driver's
    connection reads it, and only when its ``agentPolicy`` is the served expose's: the gate
    this call passed is the served expose's."""
    expose = state.expose
    named = _named_relationships(expose, args)
    if not named:
        return {}
    contract = getattr(state, "contract", None)
    exposes = contract.get("exposes") if isinstance(contract, Mapping) else None
    served = expose.get("policy")
    served_policy = (served.get("agentPolicy") if isinstance(served, Mapping) else None) or {}
    by_name: Dict[str, Mapping[str, Any]] = {}
    for entry in expose["semantics"]["relationships"]:
        if isinstance(entry, Mapping):
            by_name.setdefault(entry["name"], entry)
    related: Dict[str, RelatedTable] = {}
    for relationship in named:
        to = by_name[relationship].get("to")

        def refuse(why: str, *, _name: str = relationship, _to: Any = to) -> QueryValidationError:
            return QueryValidationError(
                f"Relationship {_name!r} cannot be read: the table it reaches ({_to!r}) {why}."
            )

        target = next(
            (
                e
                for e in (exposes if isinstance(exposes, list) else [])
                if isinstance(e, Mapping) and e.get("exposeId") == to
            ),
            None,
        )
        if target is None:
            raise refuse("is not an expose of this contract")
        policy = target.get("policy")
        target_policy = (policy.get("agentPolicy") if isinstance(policy, Mapping) else None) or {}
        if target_policy != served_policy:
            raise refuse("carries another agentPolicy than the expose being queried")
        reader = getattr(driver, "related_table_reference", None)
        try:
            if not callable(reader):
                raise UnsupportedBindingError("this driver cannot read another expose")
            reference = reader(target)
        except (UnsupportedBindingError, ValueError) as exc:
            logger = getattr(driver, "logger", None) or _LOG
            logger.info("related_table_unreadable: %s -> %s: %s", relationship, to, exc)
            raise refuse("cannot be read in the same statement on this gateway") from None
        related[relationship] = RelatedTable(
            expose=target,
            table_reference=reference,
            restricted_columns=tuple(sorted(EngineDriver._compute_restricted_columns(target))),
            redacted_columns=tuple(sorted(EngineDriver._compute_pii_columns(target))),
        )
    return related


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
        # Enforce policy.authz.rowFilters on the query tool: the compiled
        # statement reads the table through the rows the caller's principal may
        # read. ``attrs`` is this request's verified identity (falls back to
        # state for callers that don't pass the kwarg).
        caller_attributes=attrs,
        table_reference=descriptor.table_reference,
        # Pass the engine dialect so a row-filter predicate is checked as THIS
        # engine reads it (on BigQuery a double-quoted name is a string, so a
        # predicate written with one is refused rather than misread).
        dialect=descriptor.dialect,
        # Column-level policy, enforced at COMPILE time. The driver's
        # ``project()`` drops restricted columns / redacts PII by matching
        # the OUTPUT column name, which the semantic layer aliases away:
        # a measure ``{name: avg_balance, expr: ACCOUNT_BALANCE}`` served
        # statistics over a denied column, and a dimension ``{name:
        # seg_alias, expr: MARKET_SEGMENT}`` served raw PII. The compiler
        # matches on the underlying EXPRESSION instead — rejecting denied
        # columns outright (as ``query_sql`` already did) and tagging the
        # aliases whose values must be redacted.
        restricted_columns=set(getattr(driver, "_restricted_columns", set())),
        redacted_columns=set(getattr(driver, "_pii_columns", set())),
        # An as-of relationship the request names: the version of its target the row's
        # date falls in, read through the target's own governed projection.
        related=_related_tables(state, driver, args),
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
        # Enforce policy.authz.rowFilters on the free-form query_sql tool: every
        # read of the exposed table in the caller's statement is rewritten to
        # the rows the caller's principal may read (never a wrap around the
        # statement, whose projection the caller controls). ``attrs`` is this
        # request's verified identity (falls back to state for callers that
        # don't pass the kwarg).
        expose=state.expose,
        caller_attributes=attrs,
        dialect=descriptor.dialect,
        # A filtered caller's comparisons are typed by contract.schema; the driver reads
        # the table's own types once, so a declared type the table does not hold is refused.
        physical_types=getattr(driver, "physical_column_types", None),
    )
    result = driver.query(compiled=compiled, timeout_seconds=state.query_timeout_seconds)
    return _serialize_query_result(state.expose, compiled, result)
