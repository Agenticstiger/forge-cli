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

"""Concurrency regression: per-request identity isolation on the MCP gateway.

Pins the fix for the HIGH-severity cross-client identity bleed. The
HTTP/SSE transport serves MANY concurrent MCP clients over ONE shared
:class:`SessionState`. The pre-fix code CACHED the first authenticated
client's identity onto that shared state:

* ``_transport.handle_sse`` wrote ``state.caller_attributes`` /
  ``state.model_id`` / ``state.use_case`` from the connecting client's
  attrs, and
* ``server._bind_caller_identity_from_context`` short-circuited with
  ``if self.state.model_id is not None: return`` — sticky-to-first.

The consequence: every *later* client was evaluated under the FIRST
client's principal. A denied model slipped through (or an allowed one
was denied) because the agentPolicy gate read the wrong ``model_id``;
and a tenant saw another tenant's rows because ``${caller.tenant_id}``
rowFilters resolved against the wrong ``caller_attributes``.

The fix replaces the cache with
:meth:`OutputPortMcpServer._resolve_request_identity`, which reads the
identity FRESH from the per-request context handed to it by the compat
call-tool adapter (v1: the ``request_ctx`` ContextVar behind
``Server.request_context``; v2: the ctx object the SDK passes into the
handler) — self-attested ``clientInfo`` from ``request_context.session``
+ cryptographic ``fluid_auth_attrs`` from
``request_context.request.scope``, crypto winning — and threads it as
explicit args into the policy gate + the data-tool handlers. Nothing
identity-bearing is written back onto the shared state.

These tests simulate concurrent clients by fabricating distinct
per-request context objects and asserting each request resolves to —
and is gated under — its OWN identity, never sticky-to-first. The
fabricated shape (``.session.client_params`` + ``.request.scope``) is
what BOTH SDK generations expose, so this file runs unmodified under
mcp 1.x and 2.x.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional

import pytest

from fluid_build.output_ports.mcp._handlers import tool_query  # noqa: E402
from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.query_compiler import (  # noqa: E402
    compile_semantic_query,
)
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402

# ---------------------------------------------------------------------
# Fixtures — a server whose agentPolicy ALLOWS "good-model" and DENIES
# "bad-model", plus helpers to fabricate a per-request context.
# ---------------------------------------------------------------------

_EXPOSE_MODEL_GATE: Dict[str, Any] = {
    "exposeId": "demo",
    "kind": "table",
    "policy": {"agentPolicy": {"allowedModels": ["good-model"]}},
}

# An expose whose rowFilter resolves ``${caller.tenant_id}`` — used to
# prove caller_attributes isolate per request all the way into the
# compiled SQL the driver would execute.
_TENANT_EXPOSE: Dict[str, Any] = {
    "exposeId": "demo",
    "semantics": {"measures": [{"name": "row_count", "agg": "count", "expr": "id"}]},
    "policy": {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]},
}


def _make_server(expose: Mapping[str, Any]) -> OutputPortMcpServer:
    """Build a real OutputPortMcpServer bound to ``expose``. The driver
    is never built (these tests stop at the policy / identity seams), so
    no engine credentials are needed."""
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "demo.v1",
        "exposes": [expose],
    }
    return OutputPortMcpServer(
        contract=contract,
        expose=expose,
        policy=policy,
        logger=logging.getLogger("test.identity_isolation"),
    )


def _client_info(**fields: Any) -> SimpleNamespace:
    """Fake an MCP ``clientInfo`` (Implementation) carrying the given
    extra fields under ``model_extra`` — exactly the shape
    ``_resolve_request_identity`` reads (it prefers ``model_extra`` for
    ``model`` / ``useCase`` and copies the remaining extras into
    ``caller_attributes``)."""
    return SimpleNamespace(model_extra=dict(fields))


def _request_context(
    *,
    client_info: Optional[SimpleNamespace] = None,
    scope: Optional[Dict[str, Any]] = None,
) -> SimpleNamespace:
    """Fabricate the object ``server.request_context`` returns for one
    request: ``.session.client_params.clientInfo`` (self-attestation)
    and ``.request.scope`` (the Starlette scope carrying the verified
    ``fluid_auth_attrs``). For stdio ``request`` is None — mirrored by
    passing ``scope=None``."""
    session = SimpleNamespace(client_params=SimpleNamespace(clientInfo=client_info))
    request = SimpleNamespace(scope=scope) if scope is not None else None
    return SimpleNamespace(session=session, request=request)


# ---------------------------------------------------------------------
# 1. _resolve_request_identity returns THIS request's model, per request
# ---------------------------------------------------------------------


def test_resolve_request_identity_returns_per_request_model() -> None:
    """Two requests with different ``clientInfo.model`` resolve to their
    OWN model — not whichever connected first."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    model_id, use_case, attrs = server._resolve_request_identity(
        _request_context(client_info=_client_info(model="good-model"))
    )
    assert model_id == "good-model"
    assert attrs.get("model") == "good-model"

    model_id2, _use_case2, attrs2 = server._resolve_request_identity(
        _request_context(client_info=_client_info(model="bad-model"))
    )
    assert model_id2 == "bad-model"
    assert attrs2.get("model") == "bad-model"


def test_resolve_request_identity_no_context_fails_closed() -> None:
    """With no active request context (``request_ctx`` unset, the SDK
    raises LookupError) the resolver returns the fail-closed triple so
    the policy denies on missing identity."""
    server = _make_server(_EXPOSE_MODEL_GATE)
    # request_ctx is unset here -> Server.request_context raises LookupError.
    assert server._resolve_request_identity(None) == (None, None, {})


# ---------------------------------------------------------------------
# 2. Each model is gated under ITS OWN identity (allow good, deny bad)
# ---------------------------------------------------------------------


def test_evaluate_policy_gates_each_model_under_its_own_identity() -> None:
    """The core security property: resolve + evaluate for two requests;
    the allowed model is ALLOWED and the denied model is DENIED — i.e.
    the gate uses each request's resolved identity, not a cached one."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    # Request A — good-model (in allowedModels).
    model_a, use_case_a, _ = server._resolve_request_identity(
        _request_context(client_info=_client_info(model="good-model"))
    )
    _payload_a, allowed_a, reason_a = server._evaluate_policy(
        tool_name="sample", arguments={}, model_id=model_a, use_case=use_case_a
    )
    assert allowed_a is True, f"good-model must be allowed, got reason={reason_a}"

    # Request B — bad-model (NOT in allowedModels).
    model_b, use_case_b, _ = server._resolve_request_identity(
        _request_context(client_info=_client_info(model="bad-model"))
    )
    _payload_b, allowed_b, reason_b = server._evaluate_policy(
        tool_name="sample", arguments={}, model_id=model_b, use_case=use_case_b
    )
    assert allowed_b is False, "bad-model must be denied"
    assert reason_b == "not-in-allowedModels"


def test_denied_then_allowed_order_independent() -> None:
    """Same property but bad-model FIRST: proves the deny isn't an
    artifact of evaluation order and the FIRST request never pins the
    identity for the SECOND (the pre-fix bleed direction)."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    # Request 1 — bad-model first.
    m1, u1, _ = server._resolve_request_identity(
        _request_context(client_info=_client_info(model="bad-model"))
    )
    _p1, allowed1, _r1 = server._evaluate_policy(
        tool_name="sample", arguments={}, model_id=m1, use_case=u1
    )
    assert allowed1 is False

    # Request 2 — good-model second. Pre-fix this would have been gated
    # under the cached bad-model and WRONGLY denied.
    m2, u2, _ = server._resolve_request_identity(
        _request_context(client_info=_client_info(model="good-model"))
    )
    _p2, allowed2, _r2 = server._evaluate_policy(
        tool_name="sample", arguments={}, model_id=m2, use_case=u2
    )
    assert (
        allowed2 is True
    ), "good-model after bad-model must still be allowed (not sticky-to-first)"


# ---------------------------------------------------------------------
# 3. Identity is NOT cached on the shared SessionState (no bleed)
# ---------------------------------------------------------------------


def test_identity_never_cached_on_shared_session_state() -> None:
    """Resolving identity must leave the shared SessionState untouched.

    The cached fields (``model_id`` / ``use_case`` / ``caller_attributes``)
    are the exact bleed vector this fix removed; they must stay at their
    defaults (None / None / {}) before AND after any number of
    resolutions, regardless of what clients self-attest.
    """
    server = _make_server(_EXPOSE_MODEL_GATE)
    assert server.state.model_id is None
    assert server.state.use_case is None
    assert server.state.caller_attributes == {}

    for model in ("good-model", "bad-model", "another-model"):
        server._resolve_request_identity(
            _request_context(client_info=_client_info(model=model, useCase="analysis"))
        )

    assert server.state.model_id is None, "identity must NOT be cached on shared state"
    assert server.state.use_case is None, "identity must NOT be cached on shared state"
    assert server.state.caller_attributes == {}, "attrs must NOT be cached on shared state"


# ---------------------------------------------------------------------
# 4. caller_attributes (tenant) resolve per-request — different tenants
# ---------------------------------------------------------------------


def test_caller_attributes_resolve_per_request_tenant() -> None:
    """Two requests carrying different ``tenant_id`` resolve to their own
    attrs — the rowFilter input that decides which tenant's rows a query
    returns."""
    server = _make_server(_TENANT_EXPOSE)

    _m, _u, attrs_acme = server._resolve_request_identity(
        _request_context(client_info=_client_info(tenant_id="acme"))
    )
    assert attrs_acme.get("tenant_id") == "acme"

    _m, _u, attrs_globex = server._resolve_request_identity(
        _request_context(client_info=_client_info(tenant_id="globex"))
    )
    assert attrs_globex.get("tenant_id") == "globex"
    # The two tenants are distinct — no carry-over from the first.
    assert attrs_acme["tenant_id"] != attrs_globex["tenant_id"]


# ---------------------------------------------------------------------
# 5. Handler-level: per-request caller_attributes land in the executed
#    SQL — proves the tenant isolation end-to-end at the handler seam.
# ---------------------------------------------------------------------


class _CapturingDriver:
    """Minimal EngineDriver stub that records the compiled statement.

    Mirrors ``tests/output_ports/test_query_rls.py::_CapturingDriver`` so
    the row filter actually compiled into the SQL is observable without a
    real engine.
    """

    def __init__(self) -> None:
        self.captured: Any = None
        self._restricted_columns: set = set()
        self._pii_columns: set = set()

    def descriptor(self) -> SimpleNamespace:
        return SimpleNamespace(
            table_reference="db.t",
            dialect="duckdb",
            platform="local",
            format="csv",
            capabilities={},
        )

    def query(self, *, compiled: Any, timeout_seconds: Any = None) -> SimpleNamespace:
        self.captured = compiled
        return SimpleNamespace(columns=[], rows=[])


def _fake_state(expose: Mapping[str, Any], driver: _CapturingDriver) -> SimpleNamespace:
    """A SessionState-shaped stub whose ``caller_attributes`` is set to a
    WRONG sentinel tenant. If a handler ever fell back to
    ``state.caller_attributes`` instead of the per-request kwarg, the
    sentinel — not the request's tenant — would appear in the SQL,
    failing the assertion. That makes this a direct pin on the threading
    of per-request identity through the handler.
    """
    return SimpleNamespace(
        expose=expose,
        caller_attributes={"tenant_id": "STALE-SHARED-TENANT"},
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )


def test_caller_attributes_threaded_into_handler_per_request() -> None:
    """``tool_query`` with an explicit per-request ``caller_attributes``
    must compile THAT tenant into the SQL — never the (deliberately
    wrong) value cached on the shared state."""
    # Request A — tenant acme.
    driver_a = _CapturingDriver()
    state = _fake_state(_TENANT_EXPOSE, driver_a)
    tool_query(
        state,
        {"measure": "row_count", "limit": 10},
        caller_attributes={"tenant_id": "acme"},
    )
    assert driver_a.captured is not None
    assert '"tenant_id" = :p_0' in driver_a.captured.sql
    assert "acme" in driver_a.captured.params
    assert "STALE-SHARED-TENANT" not in driver_a.captured.params

    # Request B — tenant globex, SAME shared state object (the
    # concurrent-clients-over-one-state scenario).
    driver_b = _CapturingDriver()
    state.get_driver = lambda: driver_b  # type: ignore[assignment]
    tool_query(
        state,
        {"measure": "row_count", "limit": 10},
        caller_attributes={"tenant_id": "globex"},
    )
    assert driver_b.captured is not None
    assert "globex" in driver_b.captured.params
    # The first request's tenant must NOT bleed into the second.
    assert "acme" not in driver_b.captured.params
    assert "STALE-SHARED-TENANT" not in driver_b.captured.params


# ---------------------------------------------------------------------
# 6. Cryptographic auth attrs (JWT/mTLS) win over self-attestation
# ---------------------------------------------------------------------


def test_crypto_attrs_win_over_self_attestation() -> None:
    """When the transport auth middleware verified a cryptographic
    identity (``request.scope['fluid_auth_attrs']``), it overrides the
    client's self-attested ``clientInfo`` — a spoofed clientInfo.model
    can't escape a gate keyed on the verified principal."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    # Self-attests good-model, but the verified JWT says bad-model.
    ctx = _request_context(
        client_info=_client_info(model="good-model", tenant_id="self-said"),
        scope={"fluid_auth_attrs": {"model": "bad-model", "tenant_id": "jwt-said"}},
    )
    model_id, _use_case, attrs = server._resolve_request_identity(ctx)
    assert model_id == "bad-model", "cryptographic identity must win over self-attestation"
    assert attrs.get("tenant_id") == "jwt-said", "crypto attrs override self-attested attrs"

    # And the gate denies it (the verified principal is not allowed).
    _payload, allowed, reason = server._evaluate_policy(
        tool_name="sample", arguments={}, model_id=model_id, use_case=None
    )
    assert allowed is False
    assert reason == "not-in-allowedModels"


# ---------------------------------------------------------------------
# 7. Interleaved concurrency — two requests evaluated on the same event
#    loop with their OWN contexts. ContextVars are per-coroutine, so
#    each coroutine's request_ctx.set() is invisible to the other; this
#    is the closest hermetic analogue to two concurrent SSE clients.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interleaved_requests_each_gated_under_own_identity() -> None:
    """Run good-model and bad-model evaluations as two interleaved
    coroutines and assert each gets its own verdict. Because ContextVar
    state is copied per Task, a concurrent client cannot observe (or be
    pinned by) another's identity."""
    server = _make_server(_EXPOSE_MODEL_GATE)
    barrier = asyncio.Event()

    async def _evaluate(model: str) -> bool:
        model_id, use_case, _ = server._resolve_request_identity(
            _request_context(client_info=_client_info(model=model))
        )
        # Yield AFTER setting our context but BEFORE evaluating, so
        # the two tasks' contexts are live simultaneously — if state
        # leaked across tasks, the late evaluator would read the
        # other's identity.
        barrier.set()
        await barrier.wait()
        await asyncio.sleep(0)
        _payload, allowed, _reason = server._evaluate_policy(
            tool_name="sample", arguments={}, model_id=model_id, use_case=use_case
        )
        return allowed

    good_allowed, bad_allowed = await asyncio.gather(
        _evaluate("good-model"), _evaluate("bad-model")
    )
    assert good_allowed is True, "good-model concurrent request must be allowed"
    assert bad_allowed is False, "bad-model concurrent request must be denied"
    # Shared state stayed clean throughout the interleave.
    assert server.state.model_id is None
    assert server.state.caller_attributes == {}


# ---------------------------------------------------------------------
# 8. Belt-and-suspenders: the compiler itself produces distinct SQL for
#    distinct tenants (guards against a future shared-buffer regression
#    in the compiler that the handler test couldn't catch on its own).
# ---------------------------------------------------------------------


def test_compiler_binds_distinct_tenant_params() -> None:
    acme = compile_semantic_query(
        expose=_TENANT_EXPOSE,
        measure="row_count",
        limit=100,
        caller_attributes={"tenant_id": "acme"},
        table_reference="db.t",
    )
    globex = compile_semantic_query(
        expose=_TENANT_EXPOSE,
        measure="row_count",
        limit=100,
        caller_attributes={"tenant_id": "globex"},
        table_reference="db.t",
    )
    assert acme.params == ["acme"]
    assert globex.params == ["globex"]


# ---------------------------------------------------------------------
# 9. Trust-tier separation: when the transport ENFORCES auth, only
#    verified claims bind. Regression for two authz bypasses found by
#    the mcp-dual-support security review:
#
#    (a) The verified and self-attested attribute dicts were flattened
#        together, so "crypto wins" held only for the keys the JWT claim
#        mapping happened to produce (the default mapping yields four).
#        A ${caller.<attr>} rowFilter outside that set — or ANY custom
#        FLUID_MCP_JWT_CLAIM_MAPPING, which replaces rather than merges
#        the defaults — was satisfied by the caller's own claim, turning
#        a fail-closed denial into an attacker-chosen RLS predicate.
#    (b) model / use_case were promoted from self-attestation whenever
#        the mapping didn't produce them, letting a client walk past the
#        agentPolicy gate under a valid token.
#
#    ``fluid_auth_kind`` (stamped by the transport auth middleware
#    whenever a validator actually ran) is the enforcement signal.
# ---------------------------------------------------------------------


def _enforced_ctx(
    *,
    attested: Dict[str, Any],
    verified: Dict[str, Any],
    auth_kind: str = "jwt",
) -> SimpleNamespace:
    """A request where auth was ENFORCED: the client self-attests
    ``attested`` via the capabilities channel while the middleware
    stamped the verified ``verified`` attrs + the auth kind."""
    capabilities = SimpleNamespace(experimental={"fluid": dict(attested)})
    session = SimpleNamespace(
        client_params=SimpleNamespace(client_info=None, capabilities=capabilities)
    )
    scope = {"fluid_auth_attrs": dict(verified), "fluid_auth_kind": auth_kind}
    return SimpleNamespace(session=session, request=SimpleNamespace(scope=scope))


def test_self_attested_attr_cannot_fill_an_unmapped_rowfilter_placeholder() -> None:
    """A ${caller.region} rowFilter must NOT be satisfiable by the
    caller's own attestation when a JWT is enforced but its claim
    mapping never produced ``region`` — it must fail closed instead."""
    server = _make_server(_TENANT_EXPOSE)

    _m, _u, attrs = server._resolve_request_identity(
        _enforced_ctx(
            attested={"region": "eu-restricted"},
            verified={"sub": "attacker@acme", "tenant_id": "acme"},
        )
    )

    assert "region" not in attrs, "caller-chosen region bound under an enforced JWT"
    assert attrs == {"sub": "attacker@acme", "tenant_id": "acme"}


def test_self_attested_tenant_cannot_override_a_differently_mapped_claim() -> None:
    """A custom claim mapping that lands the verified tenant at
    ``tenant`` must not leave ``tenant_id`` free for the caller."""
    server = _make_server(_TENANT_EXPOSE)

    _m, _u, attrs = server._resolve_request_identity(
        _enforced_ctx(
            attested={"tenant_id": "globex-VICTIM"},
            verified={"sub": "attacker@acme", "tenant": "acme"},
        )
    )

    assert attrs.get("tenant_id") != "globex-VICTIM"
    assert attrs.get("tenant") == "acme"


def test_self_attested_use_case_cannot_override_a_verified_claim() -> None:
    """The agentPolicy gate must see the VERIFIED use case, not the one
    the caller re-attested alongside it."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    model_id, use_case, _attrs = server._resolve_request_identity(
        _enforced_ctx(
            attested={"useCase": "reporting"},
            verified={"model": "good-model", "use_case": "exfiltrate"},
        )
    )

    assert model_id == "good-model"
    assert use_case == "exfiltrate", "caller re-attested past a verified use_case claim"


def test_self_attested_model_is_ignored_when_the_token_never_asserted_one() -> None:
    """With auth enforced and no verified ``model`` claim, the gate must
    fail closed on identity rather than trust the client's word."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    model_id, _use_case, attrs = server._resolve_request_identity(
        _enforced_ctx(
            attested={"model": "good-model"},
            verified={"sub": "attacker", "tenant_id": "acme"},
        )
    )

    assert model_id is None, "self-attested model bound under an enforced JWT"
    assert "model" not in attrs

    _payload, allowed, reason = server._evaluate_policy(
        tool_name="describe",
        arguments={},
        model_id=model_id,
        use_case=None,
    )
    assert allowed is False
    assert reason == "missing-model-identity"


def test_self_attestation_still_binds_when_no_auth_is_configured() -> None:
    """Unchanged behaviour for the no-auth deployment: with no
    ``fluid_auth_kind`` stamped, self-attested identity is all there is."""
    server = _make_server(_EXPOSE_MODEL_GATE)

    capabilities = SimpleNamespace(experimental={"fluid": {"model": "good-model"}})
    session = SimpleNamespace(
        client_params=SimpleNamespace(client_info=None, capabilities=capabilities)
    )
    ctx = SimpleNamespace(session=session, request=None)

    model_id, _use_case, attrs = server._resolve_request_identity(ctx)

    assert model_id == "good-model"
    assert attrs.get("model") == "good-model"
