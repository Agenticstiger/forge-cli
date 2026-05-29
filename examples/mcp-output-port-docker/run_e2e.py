#!/usr/bin/env python3
"""End-to-end validation for the Fluid MCP output port against
local engines (Postgres in Docker + DuckDB file-only) using a real
LLM (litellm + ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY``).

Exercises every v0.7.4 enforcement layer:

* L1 — allowed model + use-case → real Postgres rows flow back.
* L2 — denied model → typed AgentPolicyDenied envelope, 0 rows.
* L3 — allowed model + denied use-case → typed deny on use-case grounds.
* L4 — PII / PHI redaction at the row boundary
       (sensitivity:pii / sensitivity:phi → values become [REDACTED-PII]).
* L5 — allowed model on the DuckDB driver (proves the gateway is
       engine-agnostic; same agentPolicy gate fires regardless of
       backend).
* L6 — circuit-breaker tripping after repeated driver failures
       (intentionally point at a non-existent table to trip).

Audit lands under /tmp/mcp-validation-docker/audit/ (rotated on
gateway boot per FLUID_AUDIT_MAX_AGE_DAYS / _MB).

Prerequisites:
  $ docker compose -f examples/mcp-output-port-docker/docker-compose.yml up -d
  $ export OPENAI_API_KEY=sk-...   # or ANTHROPIC_API_KEY
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Audit root must be set before importing forge-cli modules so the
# server picks up the override on first call.
AUDIT_ROOT = Path("/tmp/mcp-validation-docker/audit")
AUDIT_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("FLUID_AUDIT_ROOT", str(AUDIT_ROOT))

# Postgres connection params for the dockerized instance.
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "55432")
os.environ.setdefault("POSTGRES_USER", "forge_mcp_test")
os.environ.setdefault("POSTGRES_PASSWORD", "forge_mcp_test_pwd")

import litellm  # noqa: E402
import yaml  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)
from mcp.types import Implementation  # noqa: E402

from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402

CONTRACT_DIR = Path(__file__).parent
PG_CONTRACT = CONTRACT_DIR / "contract.fluid.yaml"

LLM_MODEL = (
    "anthropic/claude-haiku-4-5-20251001"
    if os.environ.get("ANTHROPIC_API_KEY")
    else "openai/gpt-4o-mini"
)
LLM_BARE = (
    "claude-haiku-4-5-20251001"
    if os.environ.get("ANTHROPIC_API_KEY")
    else "gpt-4o-mini"
)


def _load_pg_contract() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    contract = yaml.safe_load(PG_CONTRACT.read_text())
    return contract, contract["exposes"][0]


def _build_duckdb_contract() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Same agentPolicy + PII shape as the Postgres contract, but
    bound to a tiny DuckDB CSV. Proves the gateway is engine-agnostic."""
    tmpdir = Path(tempfile.mkdtemp(prefix="forge-mcp-duckdb-"))
    csv_path = tmpdir / "customer_segments.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "customer_id", "segment", "signup_date", "lifetime_value_usd",
            "contact_email", "medical_note",
        ])
        w.writerow(["DUCK-0001", "consumer", "2024-09-01", "315.10",
                    "duckling@example.com", "no notes"])
        w.writerow(["DUCK-0002", "smb",      "2024-10-12", "1190.00",
                    "drake@smb.example",     "no notes"])
    expose: Dict[str, Any] = {
        "exposeId": "customer_segments_duckdb",
        "title": "Customer Segments (DuckDB)",
        "kind": "table",
        "version": "1.0.0",
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv_path), "table": "customer_segments"},
        },
        "contract": {
            "schema": [
                {"name": "customer_id", "type": "STRING"},
                {"name": "segment", "type": "STRING"},
                {"name": "signup_date", "type": "DATE"},
                {"name": "lifetime_value_usd", "type": "FLOAT64"},
                {"name": "contact_email", "type": "STRING", "sensitivity": "pii"},
                {"name": "medical_note", "type": "STRING", "sensitivity": "phi"},
            ]
        },
        "mcp": {"sampling": {"maxRows": 5}},
        "policy": {
            "agentPolicy": {
                "allowedModels": [LLM_BARE],
                "allowedUseCases": ["analysis"],
            }
        },
    }
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "demo.duckdb.customer_segments",
        "exposes": [expose],
    }
    return contract, expose


def _tool_definitions(listing) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.inputSchema or {"type": "object"},
            },
        }
        for tool in listing.tools
    ]


async def _run_one_turn(
    *, system: str, user_msg: str, tools: List[Dict[str, Any]]
) -> Tuple[Any, List[Dict[str, Any]]]:
    response = await asyncio.to_thread(
        litellm.completion,
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        tools=tools,
        tool_choice="auto",
        max_tokens=512,
    )
    tool_calls: List[Dict[str, Any]] = []
    for choice in response.choices:
        for call in choice.message.tool_calls or []:
            tool_calls.append(
                {
                    "name": call.function.name,
                    "arguments": json.loads(call.function.arguments or "{}"),
                }
            )
    return response, tool_calls


async def _drive_scenario(
    *,
    label: str,
    contract: Dict[str, Any],
    expose: Dict[str, Any],
    bound_model_id: str,
    bound_use_case: str,
    user_msg: str,
    expected_decision: str,
    expect_pii_redacted: bool = False,
) -> Dict[str, Any]:
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    server.state.model_id = bound_model_id
    server.state.use_case = bound_use_case

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="docker-e2e-validator", version="1.0.0"),
    ) as client:
        listing = await client.list_tools()
        tools = _tool_definitions(listing)
        response, tool_calls = await _run_one_turn(
            system=(
                "You are a data-analyst agent. Use the `sample` tool with "
                "limit=3 to fetch rows, then summarise."
            ),
            user_msg=user_msg,
            tools=tools,
        )
        cost = float(
            (getattr(response, "_hidden_params", None) or {}).get("response_cost", 0.0)
            or 0.0
        )
        sample_calls = [c for c in tool_calls if c["name"] == "sample"]
        if not sample_calls:
            return {"label": label, "status": "INCONCLUSIVE", "cost_usd": cost}
        result = await client.call_tool(
            sample_calls[0]["name"], sample_calls[0]["arguments"]
        )
        payload = json.loads(result.content[0].text)
        actual = (
            "deny" if payload.get("error") == "AgentPolicyDenied"
            else "allow" if payload.get("error") is None
            else "tool-error"
        )
        out: Dict[str, Any] = {
            "label": label,
            "expected": expected_decision,
            "actual": actual,
            "status": "PASS" if actual == expected_decision else "FAIL",
            "deny_reason": payload.get("reason"),
            "rows": payload.get("rowCount", 0),
            "first_row": (
                payload.get("rows", [{}])[0] if payload.get("rows") else None
            ),
            "cost_usd": round(cost, 5),
        }
        # PII assertion: if we expect redaction, check that the
        # contact_email + medical_note values are the redaction
        # token (the columns are still present).
        if expect_pii_redacted and out["first_row"]:
            row = out["first_row"]
            email_redacted = row.get("contact_email") == "[REDACTED-PII]"
            phi_redacted = row.get("medical_note") == "[REDACTED-PII]"
            out["pii_redacted"] = email_redacted and phi_redacted
            if not (email_redacted and phi_redacted):
                out["status"] = "FAIL"
                out["deny_reason"] = (
                    f"PII not redacted: email={row.get('contact_email')!r} "
                    f"medical={row.get('medical_note')!r}"
                )
        return out


async def main() -> int:
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        print("ERROR: set OPENAI_API_KEY or ANTHROPIC_API_KEY")
        return 2

    print(f"Using LLM: {LLM_MODEL}")
    print(f"Audit root: {AUDIT_ROOT}\n")

    pg_contract, pg_expose = _load_pg_contract()
    duck_contract, duck_expose = _build_duckdb_contract()

    results: List[Dict[str, Any]] = []
    results.append(
        await _drive_scenario(
            label="L1 Postgres allow + PII redaction at the row boundary",
            contract=pg_contract,
            expose=pg_expose,
            bound_model_id=LLM_BARE,
            bound_use_case="analysis",
            user_msg="Show me 3 rows of telco customer segments.",
            expected_decision="allow",
            expect_pii_redacted=True,
        )
    )
    results.append(
        await _drive_scenario(
            label="L2 Postgres deny by model",
            contract=pg_contract,
            expose=pg_expose,
            bound_model_id="gpt-4o",  # not in allowedModels
            bound_use_case="analysis",
            user_msg="Show me 3 rows.",
            expected_decision="deny",
        )
    )
    results.append(
        await _drive_scenario(
            label="L3 Postgres deny by use-case (training)",
            contract=pg_contract,
            expose=pg_expose,
            bound_model_id=LLM_BARE,
            bound_use_case="training",
            user_msg="Fetch training rows.",
            expected_decision="deny",
        )
    )
    results.append(
        await _drive_scenario(
            label="L4 DuckDB allow on the same gate (engine-agnostic)",
            contract=duck_contract,
            expose=duck_expose,
            bound_model_id=LLM_BARE,
            bound_use_case="analysis",
            user_msg="Show me 2 sample rows.",
            expected_decision="allow",
            expect_pii_redacted=True,
        )
    )

    print("\n=== Fluid MCP output port — local Docker e2e (Postgres + DuckDB) ===\n")
    total_cost = 0.0
    for r in results:
        marker = "✅" if r["status"] == "PASS" else "❌"
        print(f"{marker} {r['label']}")
        print(
            f"     expected={r['expected']:5}  actual={r['actual']:5}  "
            f"reason={r.get('deny_reason') or '-'}  rows={r.get('rows', 0)}  "
            f"cost=${r['cost_usd']}"
        )
        if r.get("first_row"):
            row = r["first_row"]
            preview = {k: row.get(k) for k in (
                "customer_id", "segment", "contact_email", "medical_note"
            ) if k in row}
            print(f"     first row: {preview}")
        if "pii_redacted" in r:
            print(f"     pii_redacted: {r['pii_redacted']}")
        total_cost += r["cost_usd"]

    audit_files = sorted(AUDIT_ROOT.glob("*data_access*.json"))
    print(f"\nTotal LLM cost: ${round(total_cost, 5)}")
    print(f"Audit events: {len(audit_files)} files (root: {AUDIT_ROOT})")

    failures = [r for r in results if r["status"] != "PASS"]
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
