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
import collections
import json
import logging
import os
import signal
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Deque, Dict, List, Mapping, Optional

import yaml

# Anthropic SDK — imported at module top so import failures surface
# clearly and so the server can't half-load. The package gate in
# fluid_build.output_ports.mcp.__init__ catches this case for callers
# that don't need the dispatcher (utility-only imports).
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import (  # noqa: E402
    EmbeddedResource,
    Resource,
    TextContent,
    Tool,
)

from fluid_build.copilot.store.audit_trail import (
    rotate_audit_directory,
    write_audit_event,
)

from ._expose_utils import (
    _annotate_engine_error,
    _jsonable,
    _summarise_arguments,
)
from .drivers import EngineDriver, build_driver
from .policy import OutputPortPolicy
from .query_compiler import compile_free_form_sql, compile_semantic_query
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

# Circuit breaker: when a driver fails ``threshold`` times within
# ``window_seconds``, subsequent calls fast-fail with a
# ``CircuitOpen`` envelope for ``cooldown_seconds`` instead of
# hitting the engine. Prevents a downstream outage from cascading
# into per-call timeouts that pin event-loop slots.
DEFAULT_CIRCUIT_THRESHOLD = int(os.environ.get("FLUID_MCP_CIRCUIT_THRESHOLD", "5"))
DEFAULT_CIRCUIT_WINDOW_SECONDS = float(os.environ.get("FLUID_MCP_CIRCUIT_WINDOW_SECONDS", "60"))
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = float(os.environ.get("FLUID_MCP_CIRCUIT_COOLDOWN_SECONDS", "30"))


@dataclass
class _CircuitBreaker:
    """Sliding-window failure counter that trips into ``open`` for
    ``cooldown_seconds`` once ``threshold`` failures land within
    ``window_seconds``. Half-open behaviour is implicit: the first
    call after cooldown is allowed; if it fails, the circuit
    re-opens.

    Single-replica deployments use the in-process counter (default).
    Multi-replica deployments set
    ``FLUID_MCP_CIRCUIT_BACKEND=redis`` so a downstream warehouse
    outage trips the breaker for EVERY gateway replica
    simultaneously — preventing the thundering-herd retry storm
    that would otherwise hit the recovering warehouse from N
    independently-healing breakers. Falls back to in-process on
    Redis outage (fail-open is documented loud — same posture as
    the rate-limit Redis backend).
    """

    threshold: int = DEFAULT_CIRCUIT_THRESHOLD
    window_seconds: float = DEFAULT_CIRCUIT_WINDOW_SECONDS
    cooldown_seconds: float = DEFAULT_CIRCUIT_COOLDOWN_SECONDS
    breaker_key: str = "fluid:mcp:circuit:default"
    _failures: Deque[float] = field(default_factory=collections.deque)
    _opened_at: Optional[float] = None
    _redis_client: Any = None
    _redis_unavailable: bool = False

    def is_open(self) -> bool:
        # Try Redis first when configured; fall through to local.
        if self._is_redis_enabled():
            redis_state = self._is_open_redis()
            if redis_state is not None:
                return redis_state
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at < self.cooldown_seconds:
            return True
        # Cooldown elapsed → reset to closed (half-open semantics).
        self._opened_at = None
        self._failures.clear()
        return False

    def record_failure(self) -> bool:
        """Append a failure timestamp; returns True if the circuit
        just tripped from this failure."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._failures.append(now)
        tripped_local = self._opened_at is None and len(self._failures) >= self.threshold
        if tripped_local:
            self._opened_at = now
        # Mirror to Redis for fleet-wide visibility.
        if self._is_redis_enabled():
            self._record_failure_redis(now=now, tripped=tripped_local)
        return tripped_local

    def record_success(self) -> None:
        # Successful call partially heals the breaker — clear the
        # most recent failure so a flaky engine doesn't permanently
        # accumulate towards the threshold.
        if self._failures:
            self._failures.pop()

    # ------------------------------------------------------------------
    # Redis backend (fleet-wide circuit state)
    # ------------------------------------------------------------------

    def _is_redis_enabled(self) -> bool:
        if self._redis_unavailable:
            return False
        return os.environ.get("FLUID_MCP_CIRCUIT_BACKEND", "memory").lower() == "redis"

    def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        try:
            import redis  # type: ignore[import-not-found]

            url = os.environ.get(
                "FLUID_MCP_CIRCUIT_REDIS_URL",
                os.environ.get("FLUID_MCP_RATE_LIMIT_REDIS_URL", "redis://127.0.0.1:6379/0"),
            )
            client = redis.Redis.from_url(url, socket_timeout=2.0)
            client.ping()
            self._redis_client = client
            return client
        except Exception:  # noqa: BLE001
            self._redis_unavailable = True
            return None

    def _is_open_redis(self) -> Optional[bool]:
        """Return True if Redis says the breaker is open, False if
        closed, None if Redis is unreachable (caller falls back to
        in-process state)."""
        client = self._redis()
        if client is None:
            return None
        try:
            opened_at_raw = client.get(f"{self.breaker_key}:opened_at")
        except Exception:  # noqa: BLE001
            self._redis_unavailable = True
            return None
        if opened_at_raw is None:
            return False
        try:
            opened_at = float(opened_at_raw)
        except ValueError:
            return False
        if time.time() - opened_at < self.cooldown_seconds:
            return True
        # Cooldown elapsed — best-effort clean up so the next
        # success doesn't re-read stale state.
        try:
            client.delete(f"{self.breaker_key}:opened_at", f"{self.breaker_key}:failures")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _record_failure_redis(self, *, now: float, tripped: bool) -> None:
        client = self._redis()
        if client is None:
            return
        try:
            now_ms = int(time.time() * 1000)
            cutoff_ms = now_ms - int(self.window_seconds * 1000)
            failures_key = f"{self.breaker_key}:failures"
            opened_key = f"{self.breaker_key}:opened_at"
            pipe = client.pipeline()
            pipe.zremrangebyscore(failures_key, 0, cutoff_ms)
            pipe.zadd(failures_key, {f"{now_ms}-{os.getpid()}": now_ms})
            pipe.zcard(failures_key)
            pipe.expire(failures_key, int(self.window_seconds) + 60)
            _, _, count, _ = pipe.execute()
            if count >= self.threshold:
                # Use NX so a peer replica's open marker isn't
                # overwritten — we want first-tripper-wins semantics.
                client.set(opened_key, str(time.time()), nx=True, ex=int(self.cooldown_seconds) + 5)
        except Exception:  # noqa: BLE001
            self._redis_unavailable = True


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
    # Sliding-window rate limit: timestamps of the last N tool calls.
    # When ``rate_limit_calls`` is 0 the window is unused.
    rate_limit_calls: int = DEFAULT_RATE_LIMIT_CALLS
    rate_limit_window_seconds: float = DEFAULT_RATE_LIMIT_WINDOW_SECONDS
    _call_window: Deque[float] = field(default_factory=collections.deque)
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
        ``(allowed, deny_reason)`` and prunes expired entries.
        ``rate_limit_calls=0`` disables the gate.

        When ``FLUID_MCP_RATE_LIMIT_BACKEND=redis`` and
        ``FLUID_MCP_RATE_LIMIT_REDIS_URL`` are set, the window is
        backed by Redis (sorted-set ZADD/ZCARD pattern) so a fleet
        of gateway replicas shares a single rate-limit budget.
        Falls back to the in-process deque when Redis isn't
        configured. The Redis backend is best-effort: if Redis
        becomes unreachable we **fail open** (allow the call) and
        log loud — the alternative is dropping all traffic on a
        Redis outage, which is worse than transient rate-limit
        bypass.
        """
        if self.rate_limit_calls <= 0:
            return True, None
        # Per-instance backend uses the in-process deque; multi-
        # instance backend uses Redis. The decision is made once
        # per call cheaply; Redis client is cached on the state.
        backend = os.environ.get("FLUID_MCP_RATE_LIMIT_BACKEND", "memory").lower()
        if backend == "redis":
            ok, reason = self._check_rate_limit_redis()
            if reason != "redis-unavailable-fallback-open":
                return ok, reason
            # Fall through to in-process backend on Redis outage.
        now = time.monotonic()
        window_start = now - self.rate_limit_window_seconds
        while self._call_window and self._call_window[0] < window_start:
            self._call_window.popleft()
        if len(self._call_window) >= self.rate_limit_calls:
            return False, (
                f"rate-limit-exceeded ({self.rate_limit_calls} calls per "
                f"{self.rate_limit_window_seconds}s)"
            )
        self._call_window.append(now)
        return True, None

    def _check_rate_limit_redis(self) -> tuple[bool, Optional[str]]:
        """Sliding-window rate-limit backed by a Redis sorted set.

        Key pattern: ``fluid:mcp:rate:{contract_id}:{expose_id}``
        — fleet-wide, shared across every gateway replica that
        connects to the same Redis. ZADD adds the call timestamp,
        ZCARD counts the live entries within the window, ZREMRANGEBYSCORE
        evicts expired entries. The 3-command pipeline is atomic.

        Connection is cached on the state for reuse. Failures
        return ``"redis-unavailable-fallback-open"`` so the caller
        knows to use the in-process backend.
        """
        try:
            import redis  # type: ignore[import-not-found]
        except ImportError:
            self.logger.warning(
                "FLUID_MCP_RATE_LIMIT_BACKEND=redis but redis-py not installed; "
                "falling back to in-process rate limit. Install with: "
                "pip install redis"
            )
            return True, "redis-unavailable-fallback-open"

        url = os.environ.get("FLUID_MCP_RATE_LIMIT_REDIS_URL", "redis://127.0.0.1:6379/0")
        client = getattr(self, "_redis_client", None)
        if client is None:
            try:
                client = redis.Redis.from_url(url, socket_timeout=2.0)
                client.ping()
                self._redis_client = client  # cache on instance
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("redis_rate_limit_connect_failed: %s", exc)
                return True, "redis-unavailable-fallback-open"

        contract_id = self.contract.get("id") or "default"
        expose_id = self.expose.get("exposeId") or "default"
        key = f"fluid:mcp:rate:{contract_id}:{expose_id}"
        now_ms = int(time.time() * 1000)
        window_start_ms = now_ms - int(self.rate_limit_window_seconds * 1000)
        try:
            pipe = client.pipeline()
            pipe.zremrangebyscore(key, 0, window_start_ms)
            pipe.zcard(key)
            pipe.zadd(key, {f"{now_ms}-{os.getpid()}": now_ms})
            pipe.expire(key, int(self.rate_limit_window_seconds) + 60)
            _, count, _, _ = pipe.execute()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("redis_rate_limit_pipeline_failed: %s", exc)
            return True, "redis-unavailable-fallback-open"
        if count >= self.rate_limit_calls:
            # Roll back our own ZADD so the next call gets a fresh
            # slot when traffic recedes.
            try:
                client.zrem(key, f"{now_ms}-{os.getpid()}")
            except Exception:  # noqa: BLE001
                pass
            return False, (
                f"rate-limit-exceeded ({self.rate_limit_calls} calls per "
                f"{self.rate_limit_window_seconds}s, fleet-wide via Redis)"
            )
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
        ) -> List[TextContent | EmbeddedResource]:
            # Identity binding — the SDK lowlevel Server doesn't
            # provide an initialize hook on the public surface in
            # all SDK versions, so we read clientInfo lazily on the
            # first tool call. The MCP protocol guarantees
            # initialize precedes any tools/call, so the SDK has
            # already received clientInfo by the time we get here;
            # we resolve it via the request context.
            self._bind_caller_identity_from_context(server)

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
                        "fluid.model_id": self.state.model_id or "<unbound>",
                        "fluid.use_case": self.state.use_case or "<unset>",
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
                        "modelId": self.state.model_id,
                        "useCase": self.state.use_case,
                        "decision": "deny",
                        "reason": rl_reason,
                        "policySource": "rate-limit",
                        "argumentSummary": _summarise_arguments(arguments),
                        "runId": self.state.run_id,
                    }
                    self._write_audit(decision_payload)
                    _set_span_attrs(span, {"fluid.decision": "deny", "fluid.reason": rl_reason})
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "RateLimitExceeded",
                                    "tool": name,
                                    "reason": rl_reason,
                                    "message": (
                                        "gateway rate limit hit; back off and retry. "
                                        "Tune via FLUID_MCP_RATE_LIMIT / "
                                        "FLUID_MCP_RATE_WINDOW_SECONDS."
                                    ),
                                },
                                indent=2,
                            ),
                        )
                    ]

                decision_payload, allowed, reason = self._evaluate_policy(
                    tool_name=name, arguments=arguments
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
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "AgentPolicyDenied",
                                    "tool": name,
                                    "reason": reason,
                                    "message": (
                                        f"denied by agentPolicy ({reason}); "
                                        "see audit trail for the full decision."
                                    ),
                                },
                                indent=2,
                            ),
                        )
                    ]
                # Circuit-breaker fast-fail: if recent driver
                # failures tripped the breaker, refuse the call now
                # rather than queueing behind another doomed
                # connection attempt.
                if self.state._circuit.is_open():
                    self._write_audit(
                        {
                            "tool": name,
                            "exposeId": self.state.expose.get("exposeId"),
                            "modelId": self.state.model_id,
                            "useCase": self.state.use_case,
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
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "CircuitOpen",
                                    "tool": name,
                                    "message": (
                                        "engine circuit-breaker tripped after "
                                        "repeated failures; cooling down. Tune "
                                        "via FLUID_MCP_CIRCUIT_*."
                                    ),
                                },
                                indent=2,
                            ),
                        )
                    ]

                # Per-day token-budget pre-check using a small
                # estimate (we don't know the exact response size
                # before execute; we top up after).
                ok_tokens, tok_reason = self.state.check_token_budget(estimated_tokens=64)
                if not ok_tokens:
                    self._write_audit(
                        {
                            "tool": name,
                            "exposeId": self.state.expose.get("exposeId"),
                            "modelId": self.state.model_id,
                            "useCase": self.state.use_case,
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
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "TokenBudgetExceeded",
                                    "tool": name,
                                    "reason": tok_reason,
                                    "message": (
                                        "agentPolicy.maxTokensPerDay exceeded; "
                                        "back off until the daily window rolls."
                                    ),
                                },
                                indent=2,
                            ),
                        )
                    ]

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
                                response = await self._dispatch_allowed_tool(name, arguments)
                            finally:
                                self.state._actively_dispatching = max(
                                    0, self.state._actively_dispatching - 1
                                )
                    else:
                        self.state._actively_dispatching += 1
                        try:
                            response = await self._dispatch_allowed_tool(name, arguments)
                        finally:
                            self.state._actively_dispatching = max(
                                0, self.state._actively_dispatching - 1
                            )
                    # Top up token-budget counter from the actual
                    # response size and check the per-request cap.
                    response_size = sum(len(getattr(c, "text", "") or "") for c in response)
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
                                "modelId": self.state.model_id,
                                "useCase": self.state.use_case,
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
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(
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
                                    },
                                    indent=2,
                                ),
                            )
                        ]
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

    def _bind_caller_identity_from_context(self, server: Server) -> None:
        """Read ``clientInfo.model`` / ``clientInfo.useCase`` from the
        SDK's session-info if present, store on SessionState.

        The SDK exposes the initialize-time clientInfo via the
        per-request context object on the active session. We poke
        through the documented surface; if the SDK ever drops it,
        the gate falls back to ``None`` (which the policy treats as
        ``missing-model-identity``, fail-closed).
        """
        if self.state.model_id is not None:
            return  # already bound this session
        try:
            session = server.request_context.session
            client_info = getattr(session, "client_params", None)
            client_info = getattr(client_info, "clientInfo", None) if client_info else None
            if client_info is None:
                return
            # MCP clientInfo carries name + version; we extend the
            # convention to include model + useCase. Anthropic's SDK
            # accepts arbitrary extra fields on clientInfo.
            extra = getattr(client_info, "model_extra", None) or {}
            if "model" in extra:
                self.state.model_id = str(extra["model"])
            elif hasattr(client_info, "model"):
                self.state.model_id = str(client_info.model)
            if "useCase" in extra:
                self.state.use_case = str(extra["useCase"])
            elif hasattr(client_info, "useCase"):
                self.state.use_case = str(client_info.useCase)
            # Capture every extra field on clientInfo so contract
            # ``rowFilters`` can resolve ``${caller.<attr>}``
            # placeholders. We strip the well-known fields the SDK
            # already validates (name/version/model/useCase) so the
            # caller_attributes dict only carries authority context.
            attrs: Dict[str, Any] = {}
            for key, value in extra.items():
                if key in {"name", "version", "title", "websiteUrl", "icons"}:
                    continue
                attrs[key] = value
            # Convenience aliases for the most common row-filter
            # placeholders so contracts can write `${caller.model}`
            # or `${caller.use_case}` without quoting the camelCase.
            if self.state.model_id is not None:
                attrs.setdefault("model", self.state.model_id)
            if self.state.use_case is not None:
                attrs.setdefault("use_case", self.state.use_case)
                attrs.setdefault("useCase", self.state.use_case)
            self.state.caller_attributes = attrs
            self.state.logger.info(
                "output_port_session_bound",
                extra={
                    "model_id": self.state.model_id,
                    "use_case": self.state.use_case,
                    "caller_attribute_keys": sorted(attrs.keys()),
                },
            )
        except Exception as exc:  # noqa: BLE001
            # Identity binding must never crash the dispatcher;
            # missing identity is just a fail-closed gate.
            self.state.logger.debug("output_port_identity_bind_failed: %s", exc)

    # ------------------------------------------------------------------
    # Policy evaluation (E2 wired)
    # ------------------------------------------------------------------

    def _evaluate_policy(
        self, *, tool_name: str, arguments: Mapping[str, Any]
    ) -> tuple[Dict[str, Any], bool, Optional[str]]:
        """Evaluate the policy gate and produce the audit payload."""
        allowed, reason = self.state.policy.check_tool_call(
            tool=tool_name,
            model_id=self.state.model_id,
            use_case=self.state.use_case,
        )
        payload = {
            "tool": tool_name,
            "exposeId": self.state.expose.get("exposeId"),
            "contractPath": (
                str(self.state.policy.contract_path)
                if self.state.policy.contract_path is not None
                else None
            ),
            "modelId": self.state.model_id,
            "useCase": self.state.use_case,
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
        self, name: str, arguments: Dict[str, Any]
    ) -> List[TextContent]:
        """Dispatch a tool that has cleared the policy gate.

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
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": "ToolNotAllowed",
                            "tool": name,
                            "message": str(exc),
                        }
                    ),
                )
            ]

        loop = asyncio.get_running_loop()
        try:
            if name == "describe":
                payload = await loop.run_in_executor(None, self._tool_describe)
            elif name == "sample":
                payload = await loop.run_in_executor(None, self._tool_sample, arguments)
            elif name == "query":
                payload = await loop.run_in_executor(None, self._tool_query, arguments)
            elif name == "query_sql":
                payload = await loop.run_in_executor(None, self._tool_query_sql, arguments)
            else:
                payload = {
                    "error": "UnknownTool",
                    "tool": name,
                    "message": f"Unknown tool: {name!r}.",
                }
        except Exception as exc:  # noqa: BLE001
            # Wire response carries a sanitised, redacted error
            # message — engine binding details (database / schema /
            # table) leak threat-model information to the calling
            # LLM and could be used for reconnaissance. The full
            # annotated error (with hints + binding context) lands
            # on the audit trail and the operator log instead.
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
                        "modelId": self.state.model_id,
                        "useCase": self.state.use_case,
                        "decision": "tool_error",
                        "reason": type(exc).__name__,
                        "policySource": self.state.policy.policy_source,
                        "annotatedMessage": full_message,
                        "argumentSummary": _summarise_arguments(arguments),
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            payload = {
                "error": type(exc).__name__,
                "tool": name,
                "message": (
                    f"Tool {name!r} failed; see server audit trail for the " "full annotated error."
                ),
            }
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

    # ---- Per-tool implementations -----------------------------------

    def _tool_describe(self) -> Dict[str, Any]:
        driver = self.state.get_driver()
        descriptor = driver.descriptor()
        return {
            "exposeId": self.state.expose.get("exposeId"),
            "title": self.state.expose.get("title"),
            "kind": self.state.expose.get("kind"),
            "version": self.state.expose.get("version"),
            "contract": _jsonable(self.state.expose.get("contract") or {}),
            "semantics": _jsonable(self.state.expose.get("semantics") or {}),
            "binding": {
                "platform": descriptor.platform,
                "format": descriptor.format,
                "tableReference": descriptor.table_reference,
                "dialect": descriptor.dialect,
                "capabilities": dict(descriptor.capabilities),
            },
            "agentPolicy": _jsonable(
                ((self.state.expose.get("policy") or {}).get("agentPolicy") or {})
            ),
        }

    def _tool_sample(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        driver = self.state.get_driver()
        requested = arguments.get("limit", 10)
        try:
            limit = int(requested)
        except (TypeError, ValueError):
            limit = 10
        cap = self.state.policy.max_sample_rows
        effective = min(max(limit, 1), cap)
        # Pass caller_attributes so any policy.rowFilters[] in the
        # contract resolve their ${caller.*} placeholders against
        # the bound MCP clientInfo. Drivers that don't override
        # sample() use the base impl, which compiles the filter
        # into a parameterised WHERE clause.
        result = driver.sample(limit=effective, caller_attributes=self.state.caller_attributes)
        return {
            "exposeId": self.state.expose.get("exposeId"),
            "columns": list(result.columns),
            "rows": [_jsonable(row) for row in result.rows],
            "rowCount": len(result.rows),
            "truncated": result.truncated,
            "requestedLimit": limit,
            "effectiveLimit": effective,
        }

    def _tool_query(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        driver = self.state.get_driver()
        compiled = compile_semantic_query(
            expose=self.state.expose,
            arguments=dict(arguments),
            descriptor=driver.descriptor(),
        )
        result = driver.query(compiled=compiled, timeout_seconds=self.state.query_timeout_seconds)
        return _serialize_query_result(self.state.expose, compiled, result)

    def _tool_query_sql(self, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        driver = self.state.get_driver()
        compiled = compile_free_form_sql(
            expose=self.state.expose,
            arguments=dict(arguments),
            descriptor=driver.descriptor(),
        )
        result = driver.query(compiled=compiled, timeout_seconds=self.state.query_timeout_seconds)
        return _serialize_query_result(self.state.expose, compiled, result)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_http_async(self, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Serve the gateway over MCP-SSE on ``host:port``.

        Borrowed transport: ``mcp.server.sse.SseServerTransport`` +
        Starlette + uvicorn. The MCP SDK ships SseServerTransport;
        Starlette / uvicorn come along as transitive deps of the
        ``mcp`` extra. Operators connect via
        ``http://host:port/sse`` from any MCP client that supports
        the SSE transport (Claude Desktop, MCP Inspector,
        custom HTTP-MCP clients).

        Identity binding still flows through ``clientInfo`` —
        SSE-bound sessions are functionally identical to stdio
        ones; the only difference is the wire transport.

        Authentication: when ``FLUID_MCP_AUTH_TOKEN`` is set in the
        environment, the gateway requires every HTTP request to
        carry an ``Authorization: Bearer <token>`` header that
        matches. Wrong / missing token → 401 Unauthorized BEFORE
        the SSE handshake runs, so callers can't even open a
        session. Comparison uses ``hmac.compare_digest`` to defeat
        timing-side-channel guesses. This is a real defensive
        layer — better than nothing — but operators MUST still
        front the gateway with an mTLS / OAuth proxy for production
        because shared-secret tokens are vulnerable to replay /
        leakage. See ``examples/mcp-output-port-docker/proxy/`` for
        a Caddy reverse-proxy template that pairs this gateway with
        mTLS or OAuth2 enforcement.
        """
        import uvicorn
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Mount, Route

        from .auth import AuthValidator, extract_mtls_identity

        # Resolve the auth strategy from FLUID_MCP_AUTH_MODE
        # (shared-token / jwt / spiffe / none). When the validator
        # is unconfigured (mode=none, or shared-token without
        # FLUID_MCP_AUTH_TOKEN), the gateway runs unauthenticated
        # and surfaces a loud warning.
        auth_validator = AuthValidator.from_env()
        if not auth_validator.is_enabled():
            self.state.logger.warning(
                "output_port_http_no_auth_configured: gateway is unauthenticated. "
                "Set FLUID_MCP_AUTH_MODE=jwt|spiffe|shared-token + matching "
                "config OR front with mTLS/OAuth proxy before exposing to an "
                "untrusted network."
            )
        # Snapshot for the per-request middleware closure + the
        # per-call attribute merger inside the SSE handler.
        state = self.state

        class _AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if not auth_validator.is_enabled():
                    return await call_next(request)
                decision = auth_validator.validate(dict(request.headers))
                if not decision.allowed:
                    return JSONResponse(
                        {
                            "error": "unauthorized",
                            "message": (
                                f"auth ({decision.identity_kind}) refused: "
                                f"{decision.deny_reason}"
                            ),
                        },
                        status_code=401,
                    )
                # Stash the resolved caller attributes on the request
                # scope so the SSE handler below can merge them onto
                # the SessionState (cryptographic identity replaces
                # self-attestation for downstream rowFilter resolution).
                request.scope["fluid_auth_attrs"] = dict(decision.caller_attributes)
                request.scope["fluid_auth_kind"] = decision.identity_kind
                # Also pull mTLS metadata forwarded by the proxy so
                # the audit trail records BOTH the JWT identity AND
                # the cert that carried it.
                request.scope["fluid_auth_attrs"].update(
                    extract_mtls_identity(dict(request.headers))
                )
                return await call_next(request)

        sse_path = "/sse"
        messages_path = "/messages/"
        sse = SseServerTransport(messages_path)

        async def handle_sse(request):
            # Cryptographic caller_attributes from the authn layer
            # win over any self-attested clientInfo.* extras the
            # session is about to bind. We snapshot them here so
            # the lifespan-bound SessionState picks them up before
            # the first tools/call arrives.
            crypto_attrs = request.scope.get("fluid_auth_attrs") or {}
            if crypto_attrs:
                state.caller_attributes = {
                    **state.caller_attributes,
                    **crypto_attrs,
                }
                # Promote sub → model_id / use_case if those claims
                # were in the JWT mapping but the SDK clientInfo
                # didn't carry them. Cryptographic identity wins.
                if "model" in crypto_attrs and not state.model_id:
                    state.model_id = str(crypto_attrs["model"])
                if "use_case" in crypto_attrs and not state.use_case:
                    state.use_case = str(crypto_attrs["use_case"])
            async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
                await self.server.run(
                    streams[0],
                    streams[1],
                    self.server.create_initialization_options(),
                )
            return Response()

        app = Starlette(
            routes=[
                Route(sse_path, endpoint=handle_sse),
                Mount(messages_path, app=sse.handle_post_message),
            ],
            middleware=[Middleware(_AuthMiddleware)],
        )
        config = uvicorn.Config(app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        # Stash the uvicorn Server so stop_http() (or a test harness,
        # or a future SIGTERM handler for the HTTP path) can flip
        # ``should_exit`` and unwind serve() cleanly. Without this the
        # only way to stop an HTTP-mode gateway was process kill —
        # and a test that spun one in a daemon thread leaked a
        # spinning event loop for the rest of the run.
        self._uvicorn_server = server
        self.state.logger.info(
            "output_port_http_serve_start",
            extra={
                "host": host,
                "port": port,
                "sse_path": sse_path,
                "auth_mode": auth_validator.mode,
                "auth_enabled": auth_validator.is_enabled(),
            },
        )
        try:
            await server.serve()
        finally:
            self._uvicorn_server = None

    def stop_http(self) -> None:
        """Signal a running HTTP/SSE transport to shut down.

        Sets uvicorn's ``should_exit`` flag so the ``serve()`` loop
        returns at its next iteration. Safe to call from any thread
        (the flag is a plain bool uvicorn polls). No-op when the
        gateway isn't running in HTTP mode."""
        server = getattr(self, "_uvicorn_server", None)
        if server is not None:
            server.should_exit = True

    async def run_async(self) -> None:
        """Run the server on the SDK stdio transport until the
        client disconnects.

        Installs SIGTERM and SIGINT handlers that flip
        ``state._shutdown_event`` so the lifespan teardown can drain
        in-flight tool calls (up to 5s) before tearing down driver
        connections. SIGHUP is intentionally NOT trapped — operators
        use it for log-rotation triggers in some deployments.
        """

        loop = asyncio.get_running_loop()

        def _handle_sig(sig: signal.Signals) -> None:
            self.state.logger.info(
                "output_port_signal_received",
                extra={"signal": sig.name, "in_flight": self.state._in_flight},
            )
            if self.state._shutdown_event is not None:
                self.state._shutdown_event.set()
            # Cancel the main task so stdio_server unblocks.
            for task in asyncio.all_tasks(loop):
                if task is not asyncio.current_task():
                    task.cancel()

        registered: List[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_sig, sig)
                registered.append(sig)
            except (NotImplementedError, RuntimeError):
                # Windows / restricted environments fall back to the
                # KeyboardInterrupt path in ``run()``.
                pass

        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                )
        finally:
            for sig in registered:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass

    def run(self, *, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8765) -> int:
        """Synchronous entry point used by the CLI. ``transport``
        is one of ``"stdio"`` (default — pipe with the MCP client) or
        ``"http"`` (MCP-SSE on ``host:port``). Returns the process
        exit code (0 on clean disconnect, non-zero on startup
        failure)."""
        try:
            if transport == "http":
                asyncio.run(self.run_http_async(host=host, port=port))
            elif transport == "stdio":
                asyncio.run(self.run_async())
            else:
                raise ValueError(f"unknown transport {transport!r}; supported: stdio, http")
            return 0
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Both arrive on SIGTERM/SIGINT after the lifespan drain.
            return 0
        except Exception as exc:  # noqa: BLE001
            self.state.logger.error("output_port_server_crashed: %s", exc)
            return 1


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
