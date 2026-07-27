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

"""SDK-bound consumer-side MCP output-port server.

Built on Anthropic's official Model Context Protocol Python SDK
(``mcp.server.lowlevel.Server``). The SDK handles JSON-RPC dispatch,
the protocol handshake, and the stdio wire — so this module focuses
on the forge-cli specifics:

1. **Tool surface** — ``describe`` / ``sample`` / ``query`` /
   gated ``query_sql``, derived from the bound expose's shape via
   :func:`derive_advertised_tools`.
2. **agentPolicy enforcement** — every ``tools/call`` evaluates
   :meth:`OutputPortPolicy.check_tool_call` against the caller's
   declared ``model_id`` / ``use_case`` (read from the MCP
   ``initialize`` handshake's ``clientInfo``). A deny returns an MCP
   error envelope instead of dispatching the tool.
3. **Audit trail** — every decision (allow + deny) writes a
   ``data_access`` event via
   :func:`fluid_build.copilot.store.audit_trail.write_audit_event`.

The lifespan API binds session state (model_id, use_case, contract,
expose, policy, driver) once at server start; tool handlers read it
through the SDK ``Context`` parameter. This replaces the prior
custom JSON-RPC dispatcher (~330 LOC of plumbing) with the SDK's
maintained protocol implementation.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional

import yaml

# Anthropic SDK — imported at module top so import failures surface
# clearly and so the server can't half-load. The package gate in
# fluid_build.output_ports.mcp.__init__ catches this case for callers
# that don't need the dispatcher (utility-only imports).
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.types import (  # noqa: E402
    CallToolResult,
    EmbeddedResource,
    Resource,
    TextContent,
    Tool,
)

from fluid_build.copilot.store.audit_trail import (
    rotate_audit_directory,
    write_audit_event,
)

# Tool-call bodies extracted to ``_handlers.py`` (the class kept thin
# delegating ``_tool_*`` methods). Imported as a module — not
# ``from _handlers import ...`` — so a test patching
# ``_handlers.tool_describe`` flows through to the delegation.
from . import (
    _handlers,  # noqa: E402
    _transport,  # noqa: E402
)
from ._expose_utils import (
    _annotate_engine_error,
    _jsonable,
    _summarise_arguments,
)
from .drivers import EngineDriver, build_driver
from .policy import OutputPortPolicy
from .query_compiler import QueryValidationError
from .tools import check_tool_permission, derive_advertised_tools

# NOTE: ``fluid_build.observability`` is imported LAZILY (inside the
# functions that use it), never at module top. Importing an
# ``observability`` submodule forces ``observability/__init__.py`` to
# run, which chains reporter → build_runners → cli/__init__ → back
# into ``observability`` — a circular import that only breaks when
# this module is imported before ``fluid_build.cli``. Deferring the
# import to call time sidesteps the cycle entirely; by the time any
# gateway method runs, both packages are fully initialised.


def _get_run_id() -> str:
    """Lazily resolve the cross-stage run-id. Returns "" on any
    failure so a missing observability extra never blocks the
    gateway."""
    try:
        from fluid_build.observability.run_id import get_or_create_run_id

        return get_or_create_run_id()
    except Exception:  # noqa: BLE001
        return ""


def _get_tracer():
    """Lazily resolve the OTel tracer. Returns None when tracing is
    disabled or the observability extra is absent — callers guard
    with a None check via :class:`_NoOpSpanCtx`."""
    try:
        from fluid_build.observability.tracing import _get_tracer as _resolve

        return _resolve()
    except Exception:  # noqa: BLE001
        return None


SERVER_NAME = "forge-cli-output-port-mcp"
SERVER_VERSION = "0.3.0"
DEFAULT_QUERY_TIMEOUT_SECONDS = 60.0

# Sliding-window rate limit defaults. Operators tune via
# FLUID_MCP_RATE_LIMIT (calls) and FLUID_MCP_RATE_WINDOW_SECONDS.
# Defaults: 60 reads / 60 seconds — friendly for interactive
# Claude/Cursor use, defends against runaway tool-call loops.
# Set FLUID_MCP_RATE_LIMIT=0 to disable.
DEFAULT_RATE_LIMIT_CALLS = int(os.environ.get("FLUID_MCP_RATE_LIMIT", "60"))
DEFAULT_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("FLUID_MCP_RATE_WINDOW_SECONDS", "60"))

# Backpressure: max concurrent in-flight tool calls per gateway
# process. Prevents a runaway agent from saturating the engine
# connection pool. Set 0 to disable.
DEFAULT_MAX_CONCURRENCY = int(os.environ.get("FLUID_MCP_MAX_CONCURRENCY", "8"))

# Circuit breaker (sliding-window, optional Redis fleet backend) is
# physically extracted to ``_circuit.py`` — it crossed the >1500-LOC
# "extract to natural seams" threshold. Re-imported here so
# ``from fluid_build.output_ports.mcp.server import _CircuitBreaker``
# (tests/output_ports/test_pii_audit_circuit_token.py) keeps resolving.
# ``_CircuitBreaker`` is instantiated below; the DEFAULT_CIRCUIT_*
# constants are re-exported (not used here) for API stability so
# ``from ...server import DEFAULT_CIRCUIT_THRESHOLD`` keeps resolving.
# F401 is intentional on the re-exported constants.
from ._circuit import (  # noqa: E402,F401
    DEFAULT_CIRCUIT_COOLDOWN_SECONDS,
    DEFAULT_CIRCUIT_THRESHOLD,
    DEFAULT_CIRCUIT_WINDOW_SECONDS,
    _CircuitBreaker,
)


def _error_result(payload: Mapping[str, Any]) -> CallToolResult:
    """Wrap an error envelope in a ``CallToolResult`` with ``isError``.

    Per the MCP spec a failed tool execution is reported INSIDE the
    result with ``isError: true`` (JSON-RPC errors are reserved for
    protocol-level failures), so the calling agent loop can branch on it
    and the model can see what went wrong. Every gateway refusal —
    agentPolicy denial, rate limit, circuit-open, token budget,
    tool-not-allowed, unknown tool, input validation, engine failure —
    used to come back as a plain content list, which the SDK turns into
    ``isError: false``: an agent framework that branches on ``isError``
    read a policy denial as a successful result and fed the error JSON to
    the model as data.

    The body stays the same JSON envelope callers already parse, so this
    only ADDS the error signal.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(dict(payload), indent=2, default=str))],
        isError=True,
    )


# ---------------------------------------------------------------------
# Session state — bound once at lifespan start, read on every tool call
# ---------------------------------------------------------------------


@dataclass
class SessionState:
    """Per-server state shared across tool handlers via the MCP
    ``Context`` lifespan slot.

    A new server is started per ``stdio_server()`` invocation, so
    one ``SessionState`` corresponds to one operator-supplied
    contract + expose + policy. The ``model_id`` and ``use_case``
    fields are mutated on the first ``initialize`` call (we read
    them from ``clientInfo``) and stay bound for the rest of the
    session — the MCP protocol does not promise per-request
    identity, so per-request rebinding would race.
    """

    contract: Mapping[str, Any]
    expose: Mapping[str, Any]
    policy: OutputPortPolicy
    logger: logging.Logger
    driver: Optional[EngineDriver] = None
    model_id: Optional[str] = None
    use_case: Optional[str] = None
    # All extra clientInfo fields (tenant_id, regions, principal, …)
    # bound at MCP initialize. Used by ``policy.rowFilters`` to
    # resolve ``${caller.<attr>}`` placeholders into per-tenant
    # WHERE clauses at sample/query time.
    caller_attributes: Dict[str, Any] = field(default_factory=dict)
    query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS
    # Cross-stage correlation id (auto-stamped onto OTel spans + audit
    # events; stable across the gateway lifetime). Mirrors the same
    # contract every other forge-cli CLI stage already honours.
    run_id: str = field(default="")
    # Sliding-window rate limit. When ``rate_limit_calls`` is 0 the gate
    # is disabled.
    rate_limit_calls: int = DEFAULT_RATE_LIMIT_CALLS
    rate_limit_window_seconds: float = DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    # Monotonic-clock timestamps of recent calls (the sliding window).
    # See :meth:`check_rate_limit`. Typed ``Any`` to keep the dataclass
    # default trivial; the real runtime type is ``deque[float]``.
    _rate_window: Optional[Any] = None
    # In-flight tool-call count INCLUDING calls queued behind the
    # concurrency semaphore; used by graceful-shutdown to drain so
    # nothing is dropped on SIGTERM.
    _in_flight: int = 0
    # Actively-dispatching count — number of calls currently inside
    # the semaphore (i.e. running, not queued). Operators reading
    # OTel spans / metrics want this to size connection pools and
    # capacity-plan against the configured max_concurrency.
    _actively_dispatching: int = 0
    _shutdown_event: Optional[asyncio.Event] = None
    # Backpressure: an asyncio.Semaphore created lazily on first use
    # (must be inside the running loop). Limits how many tool calls
    # the gateway dispatches concurrently.
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    _concurrency_semaphore: Optional[asyncio.Semaphore] = None
    # Circuit breaker: per-driver-class. Trips on repeated engine
    # failures so a downstream outage doesn't pin every event-loop
    # slot waiting for a timeout.
    _circuit: _CircuitBreaker = field(default_factory=_CircuitBreaker)
    # Token-cap counters for agentPolicy.maxTokensPerRequest and
    # maxTokensPerDay enforcement. Token = approximate string-length
    # / 4 of the JSON-serialised response payload (cheap heuristic
    # that matches Anthropic / OpenAI billing within 20%; precise
    # tokeniser would add tiktoken dep without changing the gate
    # outcome at typical contract caps).
    _tokens_today: int = 0
    # Window start MUST default to "now", not 0.0 — time.monotonic()
    # returns a large arbitrary value, so a 0.0 default would make
    # the very first check_token_budget() see ``now - 0.0`` >> the
    # 24h window and wipe the counter before it was ever read.
    _tokens_today_window_start: float = field(default_factory=time.monotonic)
    # Daily window resets at 24-hour boundaries (rolling, not
    # calendar-day, so the gate doesn't reset at midnight in a
    # different timezone than the operator expected).
    _DAILY_WINDOW_SECONDS: int = 86400

    def get_driver(self) -> EngineDriver:
        """Lazy driver build — defers cloud SDK loads until the first
        actual data tool fires, so ``describe`` / ``get_policy``
        keep working when credentials are missing."""
        if self.driver is None:
            self.driver = build_driver(
                expose=self.expose,
                contract=self.contract,
                logger=self.logger,
                # The ``--readable-paths`` allowlist lives on the policy and
                # MUST reach the driver: the file-backed drivers gate
                # ``binding.location.path`` / ``attach`` / ``dbFile`` against
                # it. Omitting it here left the sandbox unarmed — a served
                # contract could point ``path`` at any host file and the
                # ``sample`` / ``query`` tools returned its contents.
                readable_paths=self.policy.readable_paths,
            )
        return self.driver

    def close_driver(self) -> None:
        """Call the driver's close() method (now a first-class part
        of the EngineDriver contract). Idempotent — calling twice
        is a no-op."""
        if self.driver is None:
            return
        try:
            self.driver.close()
            self.logger.debug("output_port_driver_closed")
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("output_port_driver_close_failed: %s", exc)
        self.driver = None

    def check_rate_limit(self) -> tuple[bool, Optional[str]]:
        """Sliding-window rate-limit check. Returns
        ``(allowed, deny_reason)``. ``rate_limit_calls=0`` disables
        the gate.

        In-process single-replica sliding window over a monotonic-clock
        deque: O(1) amortised, no background thread, no dependency.

        (Previously this used PyrateLimiter's ``Limiter(InMemoryBucket)``.
        That spins up a background "leaker" thread per ``Limiter``
        instance; with a fresh ``SessionState`` per session those threads
        leaked without bound, and across a long-lived process the thread
        stacks exhaust virtual address space. A plain deque is the right
        tool for an in-process window — it removes the thread *and* the
        dependency. A Redis fleet backend was already dropped as
        speculative; if a multi-replica deployment ever needs shared
        limits, reintroduce a backend behind this same method.)
        """
        if self.rate_limit_calls <= 0:
            return True, None
        if self._rate_window is None:
            from collections import deque

            self._rate_window = deque()
        now = time.monotonic()
        cutoff = now - self.rate_limit_window_seconds
        window = self._rate_window
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self.rate_limit_calls:
            return False, (
                f"rate-limit-exceeded ({self.rate_limit_calls} calls per "
                f"{self.rate_limit_window_seconds}s)"
            )
        window.append(now)
        return True, None

    def check_token_budget(self, estimated_tokens: int) -> tuple[bool, Optional[str]]:
        """Per-day token-budget check against
        ``agentPolicy.maxTokensPerDay``. ``maxTokensPerRequest`` is
        checked separately at the per-call site because it depends
        on the response payload size, which we only know after
        execute. Returns ``(allowed, deny_reason)``.

        The agentPolicy block on the expose is the source of truth.
        When the field is absent the budget is unbounded.
        """
        agent_policy = (self.expose.get("policy") or {}).get("agentPolicy") or {}
        max_per_day = agent_policy.get("maxTokensPerDay")
        if not isinstance(max_per_day, int) or max_per_day <= 0:
            return True, None
        # Roll the window if we've crossed the 24h boundary.
        now = time.monotonic()
        if now - self._tokens_today_window_start >= self._DAILY_WINDOW_SECONDS:
            self._tokens_today = 0
            self._tokens_today_window_start = now
        if self._tokens_today + estimated_tokens > max_per_day:
            return False, (
                f"token-budget-exceeded (would consume "
                f"{self._tokens_today + estimated_tokens} > {max_per_day} "
                "maxTokensPerDay)"
            )
        return True, None

    def record_tokens(self, n: int) -> None:
        """Add ``n`` tokens to the daily counter after a successful
        tool dispatch."""
        if n > 0:
            self._tokens_today += n

    def get_concurrency_semaphore(self) -> Optional[asyncio.Semaphore]:
        """Lazily build the concurrency semaphore inside the running
        loop. ``max_concurrency=0`` disables the gate."""
        if self.max_concurrency <= 0:
            return None
        if self._concurrency_semaphore is None:
            self._concurrency_semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._concurrency_semaphore


# ---------------------------------------------------------------------
# Public class — the SDK-bound replacement for OutputPortMcpServer
# ---------------------------------------------------------------------


class OutputPortMcpServer:
    """SDK-bound MCP server bound to one expose.

    Public surface intentionally narrow — instantiate, then call
    :meth:`run` on an asyncio event loop. The class wraps
    :class:`mcp.server.lowlevel.Server` so callers who want to reuse
    the dispatch loop (e.g. for SSE in a future PR) can subclass
    cleanly.
    """

    def __init__(
        self,
        *,
        contract: Mapping[str, Any],
        expose: Mapping[str, Any],
        policy: OutputPortPolicy,
        logger: Optional[logging.Logger] = None,
        query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
        rate_limit_calls: int = DEFAULT_RATE_LIMIT_CALLS,
        rate_limit_window_seconds: float = DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        # Auto-resolve the run_id so audit events + OTel spans
        # share the same correlation token as every other forge-cli
        # CLI stage that the operator runs against the same workspace.
        # ``_get_run_id`` is itself lazy + exception-safe (deferred
        # observability import to dodge the cli↔observability cycle).
        run_id = _get_run_id()
        self.state = SessionState(
            contract=contract,
            expose=expose,
            policy=policy,
            logger=logger or logging.getLogger("fluid.output_port.mcp.server"),
            query_timeout_seconds=query_timeout_seconds,
            run_id=run_id,
            rate_limit_calls=rate_limit_calls,
            rate_limit_window_seconds=rate_limit_window_seconds,
        )
        # Build the SDK Server with our session as lifespan context.
        # The ``lifespan`` callback yields the SessionState; tool
        # handlers retrieve it via ctx.request_context.lifespan_context.
        # Driver close fires on lifespan exit so cloud connection
        # pools (snowflake-connector, bigquery client) don't leak
        # across server restarts.
        state = self.state

        @asynccontextmanager
        async def _lifespan(_srv: "Server") -> AsyncIterator[SessionState]:
            state._shutdown_event = asyncio.Event()
            try:
                yield state
            finally:
                # Drain any in-flight tool calls before tearing down
                # the driver so we don't yank the connection out
                # from under an active query.
                deadline = time.monotonic() + 5.0
                while state._in_flight > 0 and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                state.close_driver()

        self.server: Server = Server(SERVER_NAME, version=SERVER_VERSION, lifespan=_lifespan)
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Bind SDK decorator-based handlers to the server."""
        server = self.server

        @server.list_tools()
        async def _list_tools() -> List[Tool]:
            return _render_tools(self.state.expose, self.state.policy)

        @server.list_resources()
        async def _list_resources() -> List[Resource]:
            return _render_resources(
                contract=self.state.contract,
                expose=self.state.expose,
                contract_path=self.state.policy.contract_path,
            )

        @server.read_resource()
        async def _read_resource(uri: Any) -> str:
            return _read_resource_payload(
                uri=str(uri),
                contract=self.state.contract,
                expose=self.state.expose,
            )

        @server.call_tool()
        async def _call_tool(
            name: str, arguments: Dict[str, Any]
        ) -> List[TextContent | EmbeddedResource] | CallToolResult:
            # Identity binding — resolved FRESH per request from the
            # SDK's request_ctx (NOT cached on the shared SessionState).
            # On HTTP/SSE one process serves many concurrent clients
            # over one SessionState; caching the first client's identity
            # bled it onto every later client (wrong agentPolicy
            # principal + wrong tenant rowFilter). The MCP protocol
            # guarantees initialize precedes any tools/call, so the SDK
            # has already received this client's clientInfo + auth attrs
            # by the time we get here.
            model_id, use_case, caller_attributes = self._resolve_request_identity(server)

            # Open an OTel span around the full tool-call path so
            # operators can correlate gateway traffic with the rest
            # of the forge-cli stage spans (apply / verify / publish).
            # Span name mirrors the existing ``fluid.<stage>`` pattern.
            tracer = _get_tracer()
            span_cm = (
                tracer.start_as_current_span("fluid.mcp.call_tool")
                if tracer is not None
                else _NoOpSpanCtx()
            )
            with span_cm as span:
                _set_span_attrs(
                    span,
                    {
                        "fluid.run_id": self.state.run_id,
                        "fluid.tool": name,
                        "fluid.expose_id": self.state.expose.get("exposeId"),
                        "fluid.model_id": model_id or "<unbound>",
                        "fluid.use_case": use_case or "<unset>",
                        "fluid.policy_source": self.state.policy.policy_source,
                    },
                )

                # Sliding-window rate-limit gate. Fired BEFORE the
                # policy check so a buggy agent can't burn through
                # audit storage by hammering denied tools.
                rl_ok, rl_reason = self.state.check_rate_limit()
                if not rl_ok:
                    decision_payload = {
                        "tool": name,
                        "exposeId": self.state.expose.get("exposeId"),
                        "modelId": model_id,
                        "useCase": use_case,
                        "decision": "deny",
                        "reason": rl_reason,
                        "policySource": "rate-limit",
                        "argumentSummary": _summarise_arguments(arguments),
                        "runId": self.state.run_id,
                    }
                    self._write_audit(decision_payload)
                    _set_span_attrs(span, {"fluid.decision": "deny", "fluid.reason": rl_reason})
                    return _error_result(
                        {
                            "error": "RateLimitExceeded",
                            "tool": name,
                            "reason": rl_reason,
                            "message": (
                                "gateway rate limit hit; back off and retry. "
                                "Tune via FLUID_MCP_RATE_LIMIT / "
                                "FLUID_MCP_RATE_WINDOW_SECONDS."
                            ),
                        }
                    )

                decision_payload, allowed, reason = self._evaluate_policy(
                    tool_name=name,
                    arguments=arguments,
                    model_id=model_id,
                    use_case=use_case,
                )
                self._write_audit(decision_payload)
                _set_span_attrs(
                    span,
                    {
                        "fluid.decision": "allow" if allowed else "deny",
                        "fluid.reason": reason or "",
                    },
                )
                if not allowed:
                    return _error_result(
                        {
                            "error": "AgentPolicyDenied",
                            "tool": name,
                            "reason": reason,
                            "message": (
                                f"denied by agentPolicy ({reason}); "
                                "see audit trail for the full decision."
                            ),
                        }
                    )
                # Circuit-breaker fast-fail: if recent driver
                # failures tripped the breaker, refuse the call now
                # rather than queueing behind another doomed
                # connection attempt.
                if self.state._circuit.is_open():
                    self._write_audit(
                        {
                            "tool": name,
                            "exposeId": self.state.expose.get("exposeId"),
                            "modelId": model_id,
                            "useCase": use_case,
                            "decision": "deny",
                            "reason": "circuit-open",
                            "policySource": "circuit-breaker",
                            "argumentSummary": _summarise_arguments(arguments),
                            "runId": self.state.run_id,
                        }
                    )
                    _set_span_attrs(
                        span, {"fluid.decision": "deny", "fluid.reason": "circuit-open"}
                    )
                    return _error_result(
                        {
                            "error": "CircuitOpen",
                            "tool": name,
                            "message": (
                                "engine circuit-breaker tripped after "
                                "repeated failures; cooling down. Tune "
                                "via FLUID_MCP_CIRCUIT_*."
                            ),
                        }
                    )

                # Per-day token-budget pre-check using a small
                # estimate (we don't know the exact response size
                # before execute; we top up after).
                ok_tokens, tok_reason = self.state.check_token_budget(estimated_tokens=64)
                if not ok_tokens:
                    self._write_audit(
                        {
                            "tool": name,
                            "exposeId": self.state.expose.get("exposeId"),
                            "modelId": model_id,
                            "useCase": use_case,
                            "decision": "deny",
                            "reason": tok_reason,
                            "policySource": "token-budget",
                            "argumentSummary": _summarise_arguments(arguments),
                            "runId": self.state.run_id,
                        }
                    )
                    _set_span_attrs(
                        span, {"fluid.decision": "deny", "fluid.reason": tok_reason or ""}
                    )
                    return _error_result(
                        {
                            "error": "TokenBudgetExceeded",
                            "tool": name,
                            "reason": tok_reason,
                            "message": (
                                "agentPolicy.maxTokensPerDay exceeded; "
                                "back off until the daily window rolls."
                            ),
                        }
                    )

                # Backpressure: bound concurrent dispatches so a
                # runaway agent can't saturate the engine pool.
                # ``_in_flight`` includes queued calls (drain
                # semantics); ``_actively_dispatching`` is the
                # narrower count of calls currently executing
                # (operators size connection pools against this).
                semaphore = self.state.get_concurrency_semaphore()
                self.state._in_flight += 1
                try:
                    if semaphore is not None:
                        async with semaphore:
                            self.state._actively_dispatching += 1
                            try:
                                response = await self._dispatch_allowed_tool(
                                    name, arguments, caller_attributes=caller_attributes
                                )
                            finally:
                                self.state._actively_dispatching = max(
                                    0, self.state._actively_dispatching - 1
                                )
                    else:
                        self.state._actively_dispatching += 1
                        try:
                            response = await self._dispatch_allowed_tool(
                                name, arguments, caller_attributes=caller_attributes
                            )
                        finally:
                            self.state._actively_dispatching = max(
                                0, self.state._actively_dispatching - 1
                            )
                    # Top up token-budget counter from the actual
                    # response size and check the per-request cap.
                    # ``response`` is either a bare content list (success)
                    # or a ``CallToolResult`` carrying ``isError`` (any
                    # refusal / failure), so read the blocks off both.
                    content_blocks = (
                        response.content if isinstance(response, CallToolResult) else response
                    )
                    response_size = sum(len(getattr(c, "text", "") or "") for c in content_blocks)
                    estimated_tokens = max(1, response_size // 4)
                    self.state.record_tokens(estimated_tokens)
                    agent_policy = (self.state.expose.get("policy") or {}).get("agentPolicy") or {}
                    max_per_request = agent_policy.get("maxTokensPerRequest")
                    if (
                        isinstance(max_per_request, int)
                        and max_per_request > 0
                        and estimated_tokens > max_per_request
                    ):
                        self._write_audit(
                            {
                                "tool": name,
                                "exposeId": self.state.expose.get("exposeId"),
                                "modelId": model_id,
                                "useCase": use_case,
                                "decision": "deny",
                                "reason": (
                                    f"per-request-token-cap-exceeded "
                                    f"({estimated_tokens} > {max_per_request})"
                                ),
                                "policySource": "token-budget",
                                "argumentSummary": _summarise_arguments(arguments),
                                "runId": self.state.run_id,
                            }
                        )
                        return _error_result(
                            {
                                "error": "TokenBudgetExceeded",
                                "tool": name,
                                "reason": "per-request-cap",
                                "message": (
                                    "response would exceed "
                                    "agentPolicy.maxTokensPerRequest; "
                                    "narrow your query (smaller LIMIT, "
                                    "fewer columns) and retry."
                                ),
                            }
                        )
                    self.state._circuit.record_success()
                    return response
                except Exception:
                    tripped = self.state._circuit.record_failure()
                    if tripped:
                        self.state.logger.warning(
                            "output_port_circuit_tripped: cooling down for %.1fs",
                            self.state._circuit.cooldown_seconds,
                        )
                    raise
                finally:
                    self.state._in_flight = max(0, self.state._in_flight - 1)

    # ------------------------------------------------------------------
    # Identity binding (E3)
    # ------------------------------------------------------------------

    def _resolve_request_identity(
        self, server: Server
    ) -> tuple[Optional[str], Optional[str], Dict[str, Any]]:
        """Resolve the CALLING client's identity for THIS request — never
        cached on the shared SessionState. On HTTP/SSE one process serves many
        concurrent clients over one SessionState, so caching bled the first
        client's identity onto every later client. The SDK isolates identity
        per request via request_ctx, so read it fresh: self-attested clientInfo
        from request_context.session.client_params, and cryptographic
        fluid_auth_attrs (JWT/mTLS, verified by the transport auth middleware)
        from request_context.request.scope — crypto WINS over self-attestation.
        Returns (model_id, use_case, caller_attributes); any failure -> (None,
        None, {}) so the policy fail-closes on missing identity.
        """
        model_id: Optional[str] = None
        use_case: Optional[str] = None
        attrs: Dict[str, Any] = {}
        try:
            ctx = server.request_context
        except Exception:  # noqa: BLE001 - no active request context
            return None, None, {}
        try:
            session = getattr(ctx, "session", None)
            client_info = getattr(session, "client_params", None)
            client_info = getattr(client_info, "clientInfo", None) if client_info else None
            if client_info is not None:
                extra = getattr(client_info, "model_extra", None) or {}
                if "model" in extra:
                    model_id = str(extra["model"])
                elif hasattr(client_info, "model"):
                    model_id = str(client_info.model)
                if "useCase" in extra:
                    use_case = str(extra["useCase"])
                elif hasattr(client_info, "useCase"):
                    use_case = str(client_info.useCase)
                for key, value in extra.items():
                    if key in {"name", "version", "title", "websiteUrl", "icons"}:
                        continue
                    attrs[key] = value
        except Exception as exc:  # noqa: BLE001
            self.state.logger.debug("output_port_identity_clientinfo_failed: %s", exc)
        try:
            request = getattr(ctx, "request", None)
            scope = getattr(request, "scope", None)
            crypto = (scope or {}).get("fluid_auth_attrs") or {}
            if crypto:
                attrs.update(crypto)
                if "model" in crypto:
                    model_id = str(crypto["model"])
                if "use_case" in crypto:
                    use_case = str(crypto["use_case"])
        except Exception as exc:  # noqa: BLE001
            self.state.logger.debug("output_port_identity_crypto_failed: %s", exc)
        if model_id is not None:
            attrs.setdefault("model", model_id)
        if use_case is not None:
            attrs.setdefault("use_case", use_case)
            attrs.setdefault("useCase", use_case)
        return model_id, use_case, attrs

    # ------------------------------------------------------------------
    # Policy evaluation (E2 wired)
    # ------------------------------------------------------------------

    def _evaluate_policy(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        model_id: Optional[str],
        use_case: Optional[str],
    ) -> tuple[Dict[str, Any], bool, Optional[str]]:
        """Evaluate the policy gate and produce the audit payload.

        ``model_id`` / ``use_case`` are the CALLING client's identity
        resolved per-request by :meth:`_resolve_request_identity` — not
        read from the shared SessionState, which would gate every
        concurrent HTTP/SSE client under the first client's identity.
        """
        allowed, reason = self.state.policy.check_tool_call(
            tool=tool_name,
            model_id=model_id,
            use_case=use_case,
        )
        payload = {
            "tool": tool_name,
            "exposeId": self.state.expose.get("exposeId"),
            "contractPath": (
                str(self.state.policy.contract_path)
                if self.state.policy.contract_path is not None
                else None
            ),
            "modelId": model_id,
            "useCase": use_case,
            "decision": "allow" if allowed else "deny",
            "reason": reason,
            "policySource": self.state.policy.policy_source,
            "argumentSummary": _summarise_arguments(arguments),
            "runId": self.state.run_id,
        }
        return payload, allowed, reason

    def _write_audit(self, payload: Mapping[str, Any]) -> None:
        try:
            # Honour FLUID_AUDIT_ROOT so operators can redirect to a
            # SIEM-forwarded path; the writer falls back to
            # ~/.fluid/store/audit/ when the env var is unset.
            audit_root_env = os.environ.get("FLUID_AUDIT_ROOT")
            audit_root = Path(audit_root_env) if audit_root_env else None
            write_audit_event("data_access", payload=dict(payload), root=audit_root)
        except Exception as exc:  # noqa: BLE001
            # Audit logging must never crash the dispatcher.
            self.state.logger.debug("audit_log_failed: %s", exc)

    # ------------------------------------------------------------------
    # Tool dispatch (after policy allowed)
    # ------------------------------------------------------------------

    async def _dispatch_allowed_tool(
        self, name: str, arguments: Dict[str, Any], *, caller_attributes: Dict[str, Any]
    ) -> List[TextContent] | CallToolResult:
        """Dispatch a tool that has cleared the policy gate.

        ``caller_attributes`` is the CALLING client's per-request
        identity (resolved by :meth:`_resolve_request_identity`),
        threaded explicitly into the data-tool handlers so each
        concurrent HTTP/SSE client's ``${caller.*}`` rowFilters resolve
        against ITS OWN identity — not whatever happens to be cached on
        the shared SessionState.

        Each handler is sync today — driver SDKs (snowflake-connector,
        google-cloud-bigquery, duckdb) are blocking. We run them in
        the default executor so the event loop stays responsive for
        concurrent ``ping``/``initialize`` from the same client.
        """
        try:
            check_tool_permission(
                name,
                allowed_tools=self.state.policy.allowed_tools,
                denied_tools=self.state.policy.denied_tools,
                allow_free_form_sql=self.state.policy.allow_free_form_sql,
            )
        except Exception as exc:  # noqa: BLE001
            return _error_result(
                {
                    "error": "ToolNotAllowed",
                    "tool": name,
                    "message": str(exc),
                }
            )

        loop = asyncio.get_running_loop()
        try:
            if name == "describe":
                payload = await loop.run_in_executor(None, self._tool_describe)
            elif name == "sample":
                # functools.partial threads the per-request
                # caller_attributes kwarg through run_in_executor (which
                # only forwards positional args), so the row filter
                # resolves against THIS client's identity.
                payload = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._tool_sample, arguments, caller_attributes=caller_attributes
                    ),
                )
            elif name == "query":
                payload = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._tool_query, arguments, caller_attributes=caller_attributes
                    ),
                )
            elif name == "query_sql":
                payload = await loop.run_in_executor(
                    None,
                    functools.partial(
                        self._tool_query_sql, arguments, caller_attributes=caller_attributes
                    ),
                )
            else:
                return _error_result(
                    {
                        "error": "UnknownTool",
                        "tool": name,
                        "message": f"Unknown tool: {name!r}.",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            # ENGINE / BINDING failures carry a sanitised, redacted wire
            # message — binding details (database / schema / table) leak
            # threat-model information to the calling LLM and could be
            # used for reconnaissance. The full annotated error (hints +
            # binding context) lands on the audit trail + operator log.
            #
            # A QueryValidationError is the exception: it's pure INPUT
            # validation (unknown measure, bad filter key, out-of-range
            # limit, non-SELECT free-form SQL, restricted-column
            # reference). Its message references only contract-declared
            # names the caller can already enumerate via `describe`, so
            # it leaks nothing — and surfacing it VERBATIM lets the agent
            # self-correct its next call instead of looping blindly
            # against an opaque "see audit trail".
            is_validation_error = isinstance(exc, QueryValidationError)
            full_message = _annotate_engine_error(exc, expose=self.state.expose)
            self.state.logger.warning(
                "output_port_tool_error",
                extra={
                    "tool": name,
                    "exception_type": type(exc).__name__,
                    "annotated_message": full_message,
                    "expose_id": self.state.expose.get("exposeId"),
                },
            )
            try:
                self._write_audit(
                    {
                        "tool": name,
                        "exposeId": self.state.expose.get("exposeId"),
                        "modelId": caller_attributes.get("model"),
                        "useCase": caller_attributes.get("use_case"),
                        "decision": "tool_error",
                        "reason": type(exc).__name__,
                        "policySource": self.state.policy.policy_source,
                        "annotatedMessage": full_message,
                        "argumentSummary": _summarise_arguments(arguments),
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            # isError:true — a tool that raised did NOT succeed, and the
            # MCP spec puts execution failures in the result so the agent
            # loop can react. That holds for a QueryValidationError too:
            # the verbatim message is exactly what the model needs to
            # self-correct, and it can only act on it if it can tell the
            # call failed.
            return _error_result(
                {
                    "error": type(exc).__name__,
                    "tool": name,
                    "message": (
                        str(exc)
                        if is_validation_error
                        else (
                            f"Tool {name!r} failed; see server audit trail for the "
                            "full annotated error."
                        )
                    ),
                }
            )
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    # ---- Per-tool implementations -----------------------------------

    # Tool bodies live in ``_handlers.py`` (extracted from this
    # god-class). These thin delegations keep the method surface +
    # the ``run_in_executor(None, self._tool_*)`` dispatch unchanged;
    # ``_handlers.tool_*`` take the bound SessionState explicitly so
    # they're unit-testable without a full server. Going through the
    # ``_handlers`` module (not ``from _handlers import``) so a test
    # patching ``_handlers.tool_describe`` flows through.
    def _tool_describe(self) -> Dict[str, Any]:
        return _handlers.tool_describe(self.state)

    def _tool_sample(
        self, arguments: Mapping[str, Any], *, caller_attributes: Mapping[str, Any]
    ) -> Dict[str, Any]:
        return _handlers.tool_sample(self.state, arguments, caller_attributes=caller_attributes)

    def _tool_query(
        self, arguments: Mapping[str, Any], *, caller_attributes: Mapping[str, Any]
    ) -> Dict[str, Any]:
        return _handlers.tool_query(self.state, arguments, caller_attributes=caller_attributes)

    def _tool_query_sql(
        self, arguments: Mapping[str, Any], *, caller_attributes: Mapping[str, Any]
    ) -> Dict[str, Any]:
        return _handlers.tool_query_sql(self.state, arguments, caller_attributes=caller_attributes)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # Transport / lifecycle bodies live in ``_transport.py`` (extracted
    # from this god-class — stdio + HTTP/SSE serving is a distinct
    # concern from protocol-handler registration + policy). These thin
    # delegations keep the public method surface + the ``run_stdio``
    # CLI entry point unchanged. Routed through the ``_transport``
    # module so a test patching ``_transport.run_async`` flows through.
    async def run_http_async(self, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Serve the gateway over MCP-SSE (Starlette + uvicorn).
        See :func:`_transport.run_http_async`."""
        return await _transport.run_http_async(self, host=host, port=port)

    def stop_http(self, *, force: bool = False) -> None:
        """Signal a running HTTP/SSE transport to shut down.
        Pass ``force=True`` for an immediate stop that does not wait for
        lingering connections to drain. See :func:`_transport.stop_http`."""
        _transport.stop_http(self, force=force)

    async def run_async(self) -> None:
        """Run on the SDK stdio transport until the client disconnects
        (installs SIGTERM/SIGINT drain handlers).
        See :func:`_transport.run_async`."""
        return await _transport.run_async(self)

    def run(self, *, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> int:
        """Synchronous CLI entry point — dispatch stdio / http.
        See :func:`_transport.run`."""
        return _transport.run(self, transport=transport, host=host, port=port)


# ---------------------------------------------------------------------
# Module-level conveniences (preserving the prior public API shape)
# ---------------------------------------------------------------------


def run_stdio(
    *,
    contract: Mapping[str, Any],
    expose: Mapping[str, Any],
    policy: OutputPortPolicy,
    logger: Optional[logging.Logger] = None,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> int:
    """Build a server and run it on the chosen MCP transport.

    Despite the legacy name, the function accepts a ``transport``
    kwarg (``stdio`` or ``http``) so the CLI surface can pick at
    runtime without a forked code path. Wired by
    :mod:`fluid_build.cli.mcp_output_port`.
    """
    server = OutputPortMcpServer(
        contract=contract,
        expose=expose,
        policy=policy,
        logger=logger,
    )
    return server.run(transport=transport, host=host, port=port)


# ---------------------------------------------------------------------
# Tool/resource rendering helpers (sync, no SDK state)
# ---------------------------------------------------------------------


def _render_tools(expose: Mapping[str, Any], policy: OutputPortPolicy) -> List[Tool]:
    """Translate the cherry-picked tool capability list into MCP
    Tool objects."""
    raw = derive_advertised_tools(
        expose=expose,
        allow_free_form_sql=policy.allow_free_form_sql,
        extra_denied=policy.denied_tools,
    )
    tools: List[Tool] = []
    for entry in raw:
        # Honour an explicit allowlist if set — hide tools the
        # policy will reject so the LLM doesn't waste a call.
        name = entry["name"]
        if policy.allowed_tools is not None and name not in policy.allowed_tools:
            continue
        tools.append(
            Tool(
                name=name,
                description=entry.get("description", ""),
                inputSchema=entry.get("inputSchema") or {"type": "object"},
            )
        )
    return tools


def _render_resources(
    *,
    contract: Mapping[str, Any],
    expose: Mapping[str, Any],
    contract_path: Optional[Path],
) -> List[Resource]:
    """Advertise the contract YAML and expose JSON as MCP resources."""
    expose_id = str(expose.get("exposeId") or "expose")
    base = f"forge://output-port/{expose_id}"
    resources = [
        Resource(
            uri=f"{base}/contract.yaml",
            name="contract.fluid.yaml",
            description="The full FLUID contract YAML this server was started against.",
            mimeType="application/yaml",
        ),
        Resource(
            uri=f"{base}/expose.json",
            name=f"{expose_id}.expose.json",
            description="The bound expose block as JSON, for browsing without spending a tools/call.",
            mimeType="application/json",
        ),
    ]
    if contract_path is not None:
        resources.append(
            Resource(
                uri=f"{base}/contract-path",
                name="contract-path",
                description="Absolute filesystem path of the contract on the server host.",
                mimeType="text/plain",
            )
        )
    return resources


def _read_resource_payload(
    *, uri: str, contract: Mapping[str, Any], expose: Mapping[str, Any]
) -> str:
    """Resolve a resource URI to its body. Unknown URIs raise so the
    SDK turns them into a JSON-RPC error."""
    if uri.endswith("/contract.yaml"):
        return yaml.safe_dump(_jsonable(dict(contract)), sort_keys=False)
    if uri.endswith("/expose.json"):
        return json.dumps(_jsonable(dict(expose)), indent=2, default=str)
    if uri.endswith("/contract-path"):
        # Best-effort — the server may not have a contract_path bound.
        return uri
    raise ValueError(f"Unknown resource URI: {uri!r}")


# ---------------------------------------------------------------------
# OTel span helpers — no-op when tracing is disabled
# ---------------------------------------------------------------------


class _NoOpSpanCtx:
    """Context manager that mimics an OTel span when tracing is off.

    Returning ``None`` from ``__enter__`` lets call sites use
    ``with _NoOpSpanCtx() as span:`` without conditional branching;
    :func:`_set_span_attrs` ignores ``None`` spans.
    """

    def __enter__(self):  # noqa: D401
        return None

    def __exit__(self, *_):  # noqa: D401
        return False


def _set_span_attrs(span: Any, attrs: Mapping[str, Any]) -> None:
    """Stamp attributes on an OTel span, no-op when ``span`` is None
    or the OTel API is missing the ``set_attribute`` method."""
    if span is None:
        return
    setter = getattr(span, "set_attribute", None)
    if setter is None:
        return
    for key, value in attrs.items():
        try:
            setter(key, value if value is not None else "")
        except Exception:  # noqa: BLE001
            pass
