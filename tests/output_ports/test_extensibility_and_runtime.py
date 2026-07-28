# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Coverage for the gaps that turned "code exists" into "code
exists + verified" — closes items 1–4 from the prior gap audit:

* register_driver() out-of-tree extension point.
* Audit-rotation boot hook (full CLI flow, not just the helper).
* Backpressure + rate limit under realistic burst load.
* HTTP / SSE transport integration (real uvicorn + ClientSession
  over the SSE wire, not just the in-memory bridge).

These tests close the silent-regression risk on the hardening
layers added in this PR.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Mapping
from unittest.mock import patch

import pytest

duckdb = pytest.importorskip("duckdb")

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402
from mcp.types import Implementation  # noqa: E402

# In-memory client<->server harness via the SDK version-compat seam
# (the v1 helper was removed in mcp 2.x).
from fluid_build._mcp_compat import open_inmemory_session, self_attesting_client_kwargs
from fluid_build.output_ports.mcp.drivers import (  # noqa: E402
    EngineDriver,
    UnsupportedBindingError,
    build_driver,
    register_driver,
    supported_keys,
)
from fluid_build.output_ports.mcp.drivers.base import (  # noqa: E402
    DriverDescriptor,
    QueryResult,
)
from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402

from ._fixtures import make_expose, write_customer_csv  # noqa: E402

# ---------------------------------------------------------------------
# register_driver() — out-of-tree extension point
# ---------------------------------------------------------------------


class _StubDriver(EngineDriver):
    """Minimal in-memory driver used to verify ``register_driver``
    actually plumbs the registry to ``build_driver``."""

    name = "stub"

    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            platform="stub-platform",
            format="stub_table",
            table_reference="<stub>",
            dialect="stub",
            capabilities={"sample": True},
        )

    def execute(self, *, sql, params=(), timeout_seconds=None) -> QueryResult:
        return QueryResult(columns=("col_a",), rows=({"col_a": "hello"},))

    def health_check(self) -> Dict[str, Any]:
        return {"status": "ok", "engine": "stub"}


def test_register_driver_routes_build_driver_to_custom_class(monkeypatch):
    """Out-of-tree wheels register their own (platform, format) →
    DriverClass via the public ``register_driver`` API. Verify that
    the next ``build_driver`` call resolves to the registered class."""
    register_driver(("stub-platform", "stub_table"), _StubDriver)
    try:
        keys = supported_keys()
        assert ("stub-platform", "stub_table") in keys
        expose = {
            "exposeId": "stub_demo",
            "binding": {
                "platform": "stub-platform",
                "format": "stub_table",
                "location": {"table": "anything"},
            },
        }
        driver = build_driver(expose=expose, contract={})
        assert isinstance(driver, _StubDriver)
        result = driver.execute(sql="SELECT 1")
        assert result.rows == ({"col_a": "hello"},)
    finally:
        # Clean up: we don't want the stub leaking into other tests.
        from fluid_build.output_ports.mcp.drivers import _DRIVER_REGISTRY

        _DRIVER_REGISTRY.pop(("stub-platform", "stub_table"), None)


def test_register_driver_replaces_existing_binding_silently():
    """Replacing an existing binding is the documented behaviour
    (so customers can override an upstream default with a private
    wheel) — verify it actually swaps the class."""
    from fluid_build.output_ports.mcp.drivers import (
        _DRIVER_REGISTRY,
        DuckDBDriver,
    )

    original = _DRIVER_REGISTRY.get(("local", "csv"))
    register_driver(("local", "csv"), _StubDriver)
    try:
        assert _DRIVER_REGISTRY[("local", "csv")] is _StubDriver
    finally:
        if original is not None:
            register_driver(("local", "csv"), original)
        else:
            _DRIVER_REGISTRY.pop(("local", "csv"), None)
    assert _DRIVER_REGISTRY[("local", "csv")] is DuckDBDriver


def test_register_driver_rejects_malformed_keys():
    with pytest.raises(TypeError):
        register_driver("not-a-tuple", _StubDriver)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_driver((1, 2), _StubDriver)  # type: ignore[arg-type]


# ---------------------------------------------------------------------
# Audit rotation boot hook — full CLI flow, not just the helper
# ---------------------------------------------------------------------


def test_cli_serve_hook_invokes_audit_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Walks the CLI's ``_run_serve`` boot path with a stubbed
    ``run_stdio`` and asserts that ``rotate_audit_directory`` was
    called with the resolved root. Catches the regression where a
    refactor drops the call and audit dirs grow unboundedly again.
    """
    from fluid_build.cli import mcp_output_port as cli_module

    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    monkeypatch.setenv("FLUID_AUDIT_ROOT", str(audit_root))
    # Pre-seed an old file so rotation has work to do.
    seed = audit_root / "20100101T000000Z_old_data_access.json"
    seed.write_text("{}", encoding="utf-8")
    old_mtime = time.time() - (40 * 86400)
    os.utime(seed, (old_mtime, old_mtime))

    # Build a minimal contract + expose the loader can swallow.
    contract_path = tmp_path / "contract.fluid.yaml"
    csv_path = tmp_path / "rows.csv"
    csv_path.write_text("a\n1\n", encoding="utf-8")
    contract_path.write_text(
        f"""
fluidVersion: "0.7.4"
kind: DataProduct
id: demo.local.audit_rotate_check
name: Audit rotate boot test
domain: demo
metadata:
  layer: Bronze
  owner: {{team: x, email: x@example.com}}
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: local
      format: csv
      location: {{path: "{csv_path}", table: rows}}
    contract:
      schema:
        - name: a
          type: STRING
          required: true
""",
        encoding="utf-8",
    )

    captured = {}

    def _fake_run_stdio(**kwargs):
        captured.update(kwargs)
        return 0

    # ``_run_serve`` imports ``run_stdio`` lazily from the package at call time
    # (Light-CLI: keeps the MCP SDK off the ``fluid --help`` path), so patch the
    # canonical source rather than a CLI-module re-export.
    monkeypatch.setattr("fluid_build.output_ports.mcp.run_stdio", _fake_run_stdio)

    # Drive the same path the CLI does for `serve`.
    args = type(
        "Args",
        (),
        {
            "contract": str(contract_path),
            "env": None,
            "expose_id": None,
            "allow_tools": None,
            "deny_tools": None,
            "readable_paths": None,
            "allow_sql": False,
            "max_sample_rows": 100,
            "query_timeout_seconds": 60.0,
            "transport": "stdio",
            "host": "127.0.0.1",
            "port": 8765,
            "allow_models": None,
            "deny_models": None,
            "allow_use_cases": None,
            "deny_use_cases": None,
        },
    )()
    rc = cli_module._run_serve(args, logging.getLogger("test"))
    assert rc == 0
    # The aged seed must have been removed by the boot hook.
    assert (
        not seed.exists()
    ), "audit-rotation boot hook didn't fire — the aged seed file is still present"


# ---------------------------------------------------------------------
# Backpressure + rate-limit under burst load
# ---------------------------------------------------------------------


def _build_burst_test_contract(tmp_path: Path):
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = make_expose(
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv_path), "table": "customer_profiles"},
        },
    )
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "burst.local.demo",
        "exposes": [expose],
    }
    return contract, expose


@pytest.mark.asyncio
async def test_rate_limit_denies_after_threshold_under_real_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Drives the dispatcher (not the SessionState helper) with
    rate_limit=3 and asserts the 4th call returns the
    ``RateLimitExceeded`` envelope and writes a rate-limit audit
    event."""
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"
    contract, expose = _build_burst_test_contract(tmp_path)
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("burst-driver",),
        # The fixture CSV lives under tmp_path, not cwd. ``--readable-paths``
        # is enforced by the driver now (build_driver forwards it), so the
        # sandbox has to name the directory the data actually lives in — the
        # same thing a real operator does.
        readable_paths=(tmp_path.resolve(),),
    )
    server = OutputPortMcpServer(
        contract=contract,
        expose=expose,
        policy=policy,
        rate_limit_calls=3,
        rate_limit_window_seconds=60.0,
    )

    payloads: list[Dict[str, Any]] = []
    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("burst-test", "1.0.0", model="burst-driver"),
    ) as client:
        for _ in range(4):
            result = await client.call_tool("sample", {"limit": 1})
            payloads.append(json.loads(result.content[0].text))

    assert payloads[0].get("error") is None, payloads[0]
    assert payloads[1].get("error") is None, payloads[1]
    assert payloads[2].get("error") is None, payloads[2]
    assert payloads[3].get("error") == "RateLimitExceeded", payloads[3]

    # Audit must include the rate-limit deny.
    rate_events = [
        json.loads(p.read_text())
        for p in sorted(audit_root.glob("*_data_access.json"))
        if "rate-limit" in p.read_text()
    ]
    assert rate_events, "rate-limit deny must land in audit trail"


@pytest.mark.asyncio
async def test_backpressure_semaphore_serialises_concurrent_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """20 concurrent calls against a 2-slot semaphore — proves the
    semaphore actually queues. We measure max simultaneous in-flight
    via the dispatcher's _in_flight counter."""
    monkeypatch.setenv("HOME", str(tmp_path))
    contract, expose = _build_burst_test_contract(tmp_path)
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("burst-driver",),
        # The fixture CSV lives under tmp_path, not cwd. ``--readable-paths``
        # is enforced by the driver now (build_driver forwards it), so the
        # sandbox has to name the directory the data actually lives in — the
        # same thing a real operator does.
        readable_paths=(tmp_path.resolve(),),
    )
    server = OutputPortMcpServer(
        contract=contract,
        expose=expose,
        policy=policy,
        rate_limit_calls=0,  # disable the rate gate — we want backpressure to bind
    )
    server.state.model_id = "burst-driver"
    server.state.max_concurrency = 2  # bind backpressure low

    max_seen = {"value": 0}
    real_dispatch = server._dispatch_allowed_tool

    async def _spy_dispatch(name, arguments):
        # We're called INSIDE the semaphore (after the _actively_
        # dispatching bump), so this captures the bound concurrency.
        # _in_flight is intentionally NOT used — that includes
        # queued calls and is the wrong metric for backpressure.
        max_seen["value"] = max(max_seen["value"], server.state._actively_dispatching)
        await asyncio.sleep(0.02)
        return await real_dispatch(name, arguments)

    server._dispatch_allowed_tool = _spy_dispatch  # type: ignore[method-assign]

    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("burst-test", "1.0.0"),
    ) as client:
        await asyncio.gather(*(client.call_tool("sample", {"limit": 1}) for _ in range(20)))

    # Active concurrency must respect the semaphore cap.
    assert max_seen["value"] <= 2, (
        f"backpressure semaphore did not bind: max actively-dispatching observed "
        f"= {max_seen['value']}"
    )
    # And drain must complete cleanly: no calls left behind.
    assert server.state._in_flight == 0
    assert server.state._actively_dispatching == 0


# ---------------------------------------------------------------------
# HTTP / SSE transport integration
# ---------------------------------------------------------------------


def _free_port() -> int:
    """Grab a free TCP port to bind the SSE server during the test."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def _running_sse_server(server: OutputPortMcpServer, port: int):
    """Spin up the SSE transport in a background thread; yield once
    it's accepting connections."""
    import threading

    started = threading.Event()
    error: Dict[str, Any] = {}

    def _run():
        try:
            asyncio.run(server.run_http_async(host="127.0.0.1", port=port))
        except Exception as exc:  # noqa: BLE001
            error["exc"] = exc
        finally:
            started.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    # Wait for the port to accept connections (uvicorn binds quickly
    # but isn't synchronous about it).
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            sock.close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError(f"SSE server didn't bind {port} within 5s; error={error.get('exc')}")
    try:
        yield
    finally:
        # HARD-stop uvicorn. ``should_exit`` alone only breaks the accept
        # loop; uvicorn's shutdown() then WAITS for any half-open SSE
        # stream to drain. On Python 3.13/3.14 that wait outran the old
        # join(timeout=5.0): the join silently gave up, the test "passed",
        # and a LIVE uvicorn thread leaked into the rest of the run —
        # spinning an event loop that starved every later async test until
        # the CI job was cancelled (~10 min). That was the 3.13/3.14 hang.
        # ``force=True`` sets uvicorn's force_exit so serve() returns at
        # once instead of waiting on the connection.
        server.stop_http(force=True)
        # Generous join: force_exit returns serve() near-instantly once the
        # client has disconnected (the real flow — every test closes its
        # session/stream before this finally runs), but a heavily-loaded CI
        # runner may take a few seconds to fully unwind. 15s covers that
        # without false-tripping the assert below.
        thread.join(timeout=15.0)
        # Never SILENTLY leak. If the thread is somehow still alive, fail
        # loudly HERE — a local, diagnosable failure on THIS test — rather
        # than poisoning the whole suite with a starving background loop
        # (and the job-level timeout-minutes / per-test pytest-timeout are
        # the last-resort backstops if it ever does).
        assert (
            not thread.is_alive()
        ), "SSE server thread did not stop after force-stop — would leak a uvicorn loop"


@pytest.mark.asyncio
async def test_http_sse_transport_serves_tools_and_enforces_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Spin up the gateway on a real SSE port + connect via the
    SDK's SSE client. Verifies the HTTP transport code path is
    actually exercised end-to-end (not just compiled)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FLUID_MCP_AUTH_TOKEN", raising=False)
    contract, expose = _build_burst_test_contract(tmp_path)
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("http-test",),
        # The fixture CSV lives under tmp_path, not cwd. ``--readable-paths``
        # is enforced by the driver now (build_driver forwards it), so the
        # sandbox has to name the directory the data actually lives in — the
        # same thing a real operator does.
        readable_paths=(tmp_path.resolve(),),
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    server.state.model_id = "http-test"

    port = _free_port()
    with _running_sse_server(server, port):
        url = f"http://127.0.0.1:{port}/sse"
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                **self_attesting_client_kwargs("http-sse-test", "1.0.0", model="http-test"),
            ) as session:
                await session.initialize()
                listing = await session.list_tools()
                tool_names = {t.name for t in listing.tools}
                assert "sample" in tool_names
                result = await session.call_tool("sample", {"limit": 2})
                payload = json.loads(result.content[0].text)
                assert payload.get("error") is None, payload
                assert payload["rowCount"] >= 1


def test_http_sse_bearer_token_required_when_env_var_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When ``FLUID_MCP_AUTH_TOKEN`` is set, every HTTP request
    must carry a matching ``Authorization: Bearer <token>`` header
    OR the gateway returns 401 BEFORE the SSE handshake.

    Drives the auth check via raw HTTP (httpx) rather than the
    SDK SSE client so we can assert the wire-level 401 status.
    """
    import httpx

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FLUID_MCP_AUTH_TOKEN", "s3cr3t-shared-token")
    contract, expose = _build_burst_test_contract(tmp_path)
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("http-auth-test",),
        # The fixture CSV lives under tmp_path, not cwd. ``--readable-paths``
        # is enforced by the driver now (build_driver forwards it), so the
        # sandbox has to name the directory the data actually lives in — the
        # same thing a real operator does.
        readable_paths=(tmp_path.resolve(),),
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    port = _free_port()

    with _running_sse_server(server, port):
        url = f"http://127.0.0.1:{port}/sse"
        # Without a token → 401.
        with httpx.stream("GET", url, timeout=2.0) as resp:
            assert (
                resp.status_code == 401
            ), f"unauthenticated SSE request must be denied; got {resp.status_code}"
        # With wrong token → 401.
        with httpx.stream(
            "GET", url, headers={"Authorization": "Bearer wrong"}, timeout=2.0
        ) as resp:
            assert resp.status_code == 401
        # With correct token → SSE stream opens (streaming response,
        # we just confirm it started — full SDK flow is in the
        # other test).
        with httpx.stream(
            "GET",
            url,
            headers={"Authorization": "Bearer s3cr3t-shared-token"},
            timeout=2.0,
        ) as resp:
            assert (
                resp.status_code == 200
            ), f"authenticated SSE request must be accepted; got {resp.status_code}"


# ---------------------------------------------------------------------
# Validation-error surfacing vs engine-error sanitisation
#
# The dispatcher returns a QueryValidationError's message VERBATIM (it
# references only contract-declared names the agent can see via
# `describe`, so it leaks nothing and lets the agent self-correct),
# while every OTHER exception stays sanitised behind "see audit trail"
# so engine / binding details never reach the model. These drive the
# REAL dispatcher (not the handler helper) over the in-memory wire.
# ---------------------------------------------------------------------


def _build_semantic_test_contract(tmp_path: Path):
    """DuckDB/CSV expose WITH a semantic model so the ``query`` tool can
    compile — and so an unknown-measure query raises a
    ``QueryValidationError`` rather than failing earlier."""
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = make_expose(
        semantics={
            "name": "customer_profiles",
            "measures": [
                {"name": "customer_count", "agg": "count_distinct", "expr": "customer_id"},
            ],
            "dimensions": [{"name": "signup_date", "type": "time"}],
        },
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv_path), "table": "customer_profiles"},
        },
    )
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "semantic.local.demo",
        "exposes": [expose],
    }
    return contract, expose


@pytest.mark.asyncio
async def test_query_validation_error_surfaces_message_to_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unknown-measure ``query`` returns the compiler's helpful
    ``QueryValidationError`` message VERBATIM (with the known measures
    listed) so the agent can self-correct — NOT the opaque envelope."""
    monkeypatch.setenv("HOME", str(tmp_path))
    contract, expose = _build_semantic_test_contract(tmp_path)
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("test-agent",),
        # The fixture CSV lives under tmp_path, not cwd. ``--readable-paths``
        # is enforced by the driver now (build_driver forwards it), so the
        # sandbox has to name the directory the data actually lives in — the
        # same thing a real operator does.
        readable_paths=(tmp_path.resolve(),),
    )
    server = OutputPortMcpServer(
        contract=contract, expose=expose, policy=policy, rate_limit_calls=0
    )
    # Declare the model via real ``clientInfo`` — identity is resolved
    # PER REQUEST from the SDK's request_context, never cached on the
    # shared SessionState (the cross-client bleed this fix removed; see
    # test_identity_isolation.py).
    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("validation-test", "1.0.0", model="test-agent"),
    ) as client:
        result = await client.call_tool("query", {"measure": "no_such_measure", "limit": 5})
    payload = json.loads(result.content[0].text)

    assert payload["error"] == "QueryValidationError", payload
    assert "Unknown measure" in payload["message"], payload
    assert "no_such_measure" in payload["message"], payload
    # The known measure is listed so the agent can pick a valid one.
    assert "customer_count" in payload["message"], payload


@pytest.mark.asyncio
async def test_engine_error_stays_sanitised_at_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-validation (engine/driver) failure STAYS sanitised — the
    binding (database / schema / table) embedded in the engine error
    must never reach the caller; only 'see audit trail' is returned."""
    monkeypatch.setenv("HOME", str(tmp_path))
    contract, expose = _build_semantic_test_contract(tmp_path)
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=("test-agent",),
        # The fixture CSV lives under tmp_path, not cwd. ``--readable-paths``
        # is enforced by the driver now (build_driver forwards it), so the
        # sandbox has to name the directory the data actually lives in — the
        # same thing a real operator does.
        readable_paths=(tmp_path.resolve(),),
    )
    server = OutputPortMcpServer(
        contract=contract, expose=expose, policy=policy, rate_limit_calls=0
    )

    secret = "database=topsecret_db table=topsecret_tbl"

    def _boom(*args, **kwargs):
        raise RuntimeError(f"engine connection failed: {secret}")

    # Patch the lazily-built driver's execute so base.query raises a
    # genuine (non-validation) engine error carrying binding info.
    server.state.get_driver().execute = _boom

    # Declare the model via real ``clientInfo`` — identity is resolved
    # PER REQUEST from the SDK's request_context, never cached on the
    # shared SessionState (see test_identity_isolation.py).
    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("engine-error-test", "1.0.0", model="test-agent"),
    ) as client:
        result = await client.call_tool("query", {"measure": "customer_count", "limit": 5})
    payload = json.loads(result.content[0].text)

    assert payload["message"] == (
        "Tool 'query' failed; see server audit trail for the full annotated error."
    ), payload
    # The binding leak must NOT appear anywhere in the wire payload.
    assert "topsecret" not in json.dumps(payload), payload
