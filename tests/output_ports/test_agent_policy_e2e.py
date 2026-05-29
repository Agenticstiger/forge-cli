# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""In-process MCP integration tests for the agentPolicy gate.

Spins up :class:`OutputPortMcpServer` against a small DuckDB-loaded
CSV, then connects an :class:`mcp.ClientSession` over the SDK's
in-memory transport and exercises the four scenarios that pin the
runtime contract:

* I1 — allowed model successfully calls ``sample`` and gets data.
* I2 — denied model receives the AgentPolicyDenied envelope and
  no data leaves the server.
* I3 — multiple tool calls in one session — each evaluated against
  the bound model_id, each audited.
* I4 — missing identity at initialize — ``tools/list`` works (so
  the client can browse the surface) but ``tools/call`` fails-closed.

These tests do NOT hit any external LLM; they prove the
agentPolicy gate works on the real MCP wire shape. Live LLM tests
live in ``tests/integration/test_mcp_output_port_live_llm.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Mapping

import pytest

duckdb = pytest.importorskip("duckdb")

from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)
from mcp.types import Implementation  # noqa: E402

from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402

from ._fixtures import make_expose, write_customer_csv  # noqa: E402

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _build_contract_and_expose(tmp_path: Path) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Write a small CSV and return a (contract, expose) pair bound
    to a local DuckDB driver. Reuses the cherry-picked test helpers."""
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
        "id": "demo.test.customers_v1",
        "exposes": [expose],
    }
    return contract, expose


def _build_policy(
    *,
    expose: Mapping[str, Any],
    cli_allowed_models=None,
    cli_denied_models=None,
    cli_allowed_use_cases=None,
    cli_denied_use_cases=None,
    audit_root: Path,
) -> OutputPortPolicy:
    """Build a policy that routes audit writes to the test's tmp dir."""
    # FLUID audit writer reads ~/.fluid/store/audit by default; we
    # redirect by monkeypatching HOME for the duration of the test
    # in the caller, which is simpler than threading a custom root
    # through the server.
    return OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=cli_allowed_models,
        cli_denied_models=cli_denied_models,
        cli_allowed_use_cases=cli_allowed_use_cases,
        cli_denied_use_cases=cli_denied_use_cases,
    )


def _audit_files(audit_root: Path) -> list[Dict[str, Any]]:
    """Read every data_access JSON the gateway has emitted."""
    out: list[Dict[str, Any]] = []
    for path in sorted(audit_root.glob("*_data_access.json")):
        out.append(json.loads(path.read_text()))
    return out


# ---------------------------------------------------------------------
# I1 — allowed model successfully calls sample
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i1_allowed_model_can_sample_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"

    contract, expose = _build_contract_and_expose(tmp_path)
    policy = _build_policy(
        expose=expose,
        cli_allowed_models=("claude-haiku-4-5-20251001",),
        audit_root=audit_root,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)

    # Inject the model_id directly into the bound session so this
    # in-memory test doesn't have to thread custom clientInfo through
    # the SDK's Implementation type. The production path uses
    # _bind_caller_identity_from_context() to pull this from the
    # client's initialize handshake.
    server.state.model_id = "claude-haiku-4-5-20251001"

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="test-client", version="0.1.0"),
    ) as client:
        result = await client.call_tool("sample", {"limit": 3})

    payload = json.loads(result.content[0].text)
    assert payload.get("error") is None, payload
    assert payload["exposeId"] == expose["exposeId"]
    assert payload["rowCount"] >= 1

    audit = _audit_files(audit_root)
    assert audit, "data_access audit event should be written"
    assert audit[-1]["payload"]["decision"] == "allow"
    assert audit[-1]["payload"]["modelId"] == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------
# I2 — denied model gets AgentPolicyDenied envelope, no data leaves
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i2_denied_model_gets_typed_deny_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"

    contract, expose = _build_contract_and_expose(tmp_path)
    policy = _build_policy(
        expose=expose,
        cli_allowed_models=("claude-haiku-4-5-20251001",),
        audit_root=audit_root,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    server.state.model_id = "claude-3-opus"  # NOT in allowlist

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="test-client", version="0.1.0"),
    ) as client:
        result = await client.call_tool("sample", {"limit": 3})

    payload = json.loads(result.content[0].text)
    assert payload["error"] == "AgentPolicyDenied"
    assert payload["reason"] == "not-in-allowedModels"
    assert "rows" not in payload, "no data must leave on a deny"

    audit = _audit_files(audit_root)
    deny_events = [e for e in audit if e["payload"]["decision"] == "deny"]
    assert deny_events, "a deny audit event must land"
    assert deny_events[-1]["payload"]["reason"] == "not-in-allowedModels"


# ---------------------------------------------------------------------
# I3 — multiple tool calls in one session, each independently audited
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i3_multiple_calls_each_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"

    contract, expose = _build_contract_and_expose(tmp_path)
    policy = _build_policy(
        expose=expose,
        cli_allowed_models=("claude-haiku-4-5-20251001",),
        audit_root=audit_root,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    server.state.model_id = "claude-haiku-4-5-20251001"

    payloads: list[Dict[str, Any]] = []
    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="test-client", version="0.1.0"),
    ) as client:
        for tool, args in [
            ("describe", {}),
            ("sample", {"limit": 2}),
            ("sample", {"limit": 5}),
        ]:
            result = await client.call_tool(tool, args)
            payloads.append(json.loads(result.content[0].text))

    # Every tool call must independently evaluate the policy AND
    # land its own audit event. The audit writer now uses
    # microsecond + counter + pid disambiguation so concurrent
    # writes never overwrite each other (closes the prior
    # second-precision collision gap).
    assert len(payloads) == 3
    for p in payloads:
        assert p.get("error") is None, p
    audit = _audit_files(audit_root)
    assert len(audit) == 3, f"expected 3 audit events (one per call), got {len(audit)}"
    tools = [event["payload"]["tool"] for event in audit]
    assert tools.count("describe") == 1
    assert tools.count("sample") == 2
    assert all(event["payload"]["decision"] == "allow" for event in audit)
    assert all(event["payload"]["modelId"] == "claude-haiku-4-5-20251001" for event in audit)


# ---------------------------------------------------------------------
# I4 — missing identity: tools/list works, tools/call fails-closed
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i4_missing_identity_browses_but_cannot_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"

    contract, expose = _build_contract_and_expose(tmp_path)
    policy = _build_policy(
        expose=expose,
        cli_allowed_models=("claude-haiku-4-5-20251001",),
        audit_root=audit_root,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    # Deliberately leave server.state.model_id == None to simulate a
    # client that didn't declare its model at initialize.

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="test-client", version="0.1.0"),
    ) as client:
        listing = await client.list_tools()
        assert any(
            t.name == "sample" for t in listing.tools
        ), "tools/list must work even without identity"

        result = await client.call_tool("sample", {"limit": 1})
        payload = json.loads(result.content[0].text)
        assert payload["error"] == "AgentPolicyDenied"
        assert payload["reason"] == "missing-model-identity"

    audit = _audit_files(audit_root)
    deny = [e for e in audit if e["payload"]["decision"] == "deny"]
    assert deny, "fail-closed deny must be audited"
    assert deny[-1]["payload"]["reason"] == "missing-model-identity"


# ---------------------------------------------------------------------
# I5 — production identity binding: clientInfo.{model,useCase} land on
#       SessionState via the SDK's request_context — no manual injection
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i5_identity_binding_via_real_clientinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the production identity-extraction path.

    All previous integration tests inject ``server.state.model_id``
    directly. This one drives the SDK with a real
    ``Implementation(model=..., useCase=...)`` ``clientInfo`` and
    verifies ``_bind_caller_identity_from_context`` extracts both
    fields correctly. Catches any future SDK shape change that
    would silently break the binding (failing closed = production
    outage).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"

    contract, expose = _build_contract_and_expose(tmp_path)
    policy = _build_policy(
        expose=expose,
        cli_allowed_models=("claude-haiku-4-5-20251001",),
        cli_allowed_use_cases=("analysis",),
        audit_root=audit_root,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    # Deliberately leave server.state.model_id and use_case unset so
    # the binding has to come from clientInfo at first tools/call.
    assert server.state.model_id is None
    assert server.state.use_case is None

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(
            name="identity-binding-test",
            version="1.0.0",
            model="claude-haiku-4-5-20251001",
            useCase="analysis",
        ),
    ) as client:
        result = await client.call_tool("sample", {"limit": 1})

    payload = json.loads(result.content[0].text)
    assert payload.get("error") is None, f"expected allow after identity binding, got: {payload}"
    # Production binding succeeded — both fields landed.
    assert server.state.model_id == "claude-haiku-4-5-20251001"
    assert server.state.use_case == "analysis"

    audit = _audit_files(audit_root)
    assert audit, "allow must be audited"
    assert audit[-1]["payload"]["decision"] == "allow"
    assert audit[-1]["payload"]["modelId"] == "claude-haiku-4-5-20251001"
    assert audit[-1]["payload"]["useCase"] == "analysis"


@pytest.mark.asyncio
async def test_i6_identity_binding_denies_when_clientinfo_lacks_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Companion to I5: real clientInfo without ``model`` field still
    fails-closed. Catches an SDK regression where extra fields might
    silently be dropped from clientInfo on the wire."""
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / ".fluid" / "store" / "audit"

    contract, expose = _build_contract_and_expose(tmp_path)
    policy = _build_policy(
        expose=expose,
        cli_allowed_models=("claude-haiku-4-5-20251001",),
        audit_root=audit_root,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="no-model", version="1.0.0"),
    ) as client:
        result = await client.call_tool("sample", {"limit": 1})

    payload = json.loads(result.content[0].text)
    assert payload["error"] == "AgentPolicyDenied"
    assert payload["reason"] == "missing-model-identity"
    audit = _audit_files(audit_root)
    deny = [e for e in audit if e["payload"]["decision"] == "deny"]
    assert deny and deny[-1]["payload"]["reason"] == "missing-model-identity"
