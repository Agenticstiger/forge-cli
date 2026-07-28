#!/usr/bin/env python3
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

"""CI-friendly Snowflake e2e for the Fluid MCP output port.

Identical to the on-laptop validation that lives under /tmp, but
in-tree so GitHub Actions can invoke it via ``python scripts/ci/...``
without writing the script to disk first. Reads Snowflake creds
strictly from env (never from a .env file) and binds the gateway
to ``${SNOWFLAKE_DATABASE}.INFORMATION_SCHEMA.TABLES`` — guaranteed
to exist for any role with USAGE on the database, so the test
doesn't depend on a seeded TELCO_* table.

Three scenarios:
  L1 allowed model + allowed use-case → real Snowflake rows.
  L2 denied model → typed AgentPolicyDenied envelope.
  L3 allowed model + denied use-case → typed deny by use-case.

Total LLM cost per full run: ~$0.0001 on gpt-4o-mini, ~$0.08 on
Anthropic Haiku 4.5.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

AUDIT_ROOT = Path(tempfile.mkdtemp(prefix="forge-mcp-snowflake-ci-"))
os.environ["FLUID_AUDIT_ROOT"] = str(AUDIT_ROOT)

import litellm  # noqa: E402
from mcp.types import Implementation  # noqa: E402

# In-memory client<->server harness via the SDK version-compat seam
# (the v1 helper was removed in mcp 2.x).
from fluid_build._mcp_compat import open_inmemory_session, self_attesting_client_kwargs
from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402

LLM_MODEL = (
    "anthropic/claude-haiku-4-5-20251001"
    if os.environ.get("ANTHROPIC_API_KEY")
    else "openai/gpt-4o-mini"
)
LLM_BARE = "claude-haiku-4-5-20251001" if os.environ.get("ANTHROPIC_API_KEY") else "gpt-4o-mini"


def _build_contract() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    required = [
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_ROLE",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"::error::missing Snowflake env vars: {missing}")
        sys.exit(2)

    expose: Dict[str, Any] = {
        "exposeId": "snowflake_information_schema_tables",
        "kind": "table",
        "version": "1.0.0",
        "title": "Snowflake INFORMATION_SCHEMA.TABLES (CI e2e)",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {
                "account": os.environ["SNOWFLAKE_ACCOUNT"],
                "database": os.environ["SNOWFLAKE_DATABASE"],
                "schema": "INFORMATION_SCHEMA",
                "table": "TABLES",
            },
        },
        "contract": {
            "schema": [
                {"name": "TABLE_CATALOG", "type": "STRING", "required": True},
                {"name": "TABLE_SCHEMA", "type": "STRING", "required": True},
                {"name": "TABLE_NAME", "type": "STRING", "required": True},
                {"name": "TABLE_TYPE", "type": "STRING", "required": True},
            ]
        },
        "agentPort": {"kind": "mcp", "tools": ["sample"], "maxRowsPerRead": 5},
        "policy": {
            "agentPolicy": {
                "allowedModels": [LLM_BARE],
                "allowedUseCases": ["analysis", "qa"],
                "deniedUseCases": ["training"],
            }
        },
    }
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "ci.snowflake.smoke",
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


async def _drive(*, label, bound_model_id, bound_use_case, expected) -> Dict[str, Any]:
    contract, expose = _build_contract()
    policy = OutputPortPolicy.from_contract_and_flags(expose=expose)
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    server.state.model_id = bound_model_id
    server.state.use_case = bound_use_case

    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs("forge-mcp-snowflake-ci", "1.0.0"),
    ) as client:
        listing = await client.list_tools()
        tools = _tool_definitions(listing)
        response = await asyncio.to_thread(
            litellm.completion,
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Use the `sample` tool with limit=3 then summarise."},
                {"role": "user", "content": "Show me 3 sample rows."},
            ],
            tools=tools,
            tool_choice="auto",
            max_tokens=512,
        )
        cost = float(
            (getattr(response, "_hidden_params", None) or {}).get("response_cost", 0.0) or 0.0
        )
        sample_calls = [
            c
            for choice in response.choices
            for c in (choice.message.tool_calls or [])
            if c.function.name == "sample"
        ]
        if not sample_calls:
            return {"label": label, "status": "INCONCLUSIVE", "cost_usd": cost}
        result = await client.call_tool(
            "sample", json.loads(sample_calls[0].function.arguments or "{}")
        )
        payload = json.loads(result.content[0].text)
        actual = (
            "deny"
            if payload.get("error") == "AgentPolicyDenied"
            else "allow" if payload.get("error") is None else "tool-error"
        )
        return {
            "label": label,
            "expected": expected,
            "actual": actual,
            "status": "PASS" if actual == expected else "FAIL",
            "deny_reason": payload.get("reason"),
            "rows_served": payload.get("rowCount", 0),
            "cost_usd": round(cost, 5),
        }


async def main() -> int:
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        print("::error::set OPENAI_API_KEY or ANTHROPIC_API_KEY")
        return 2
    print(f"LLM: {LLM_MODEL}")
    results = [
        await _drive(
            label="L1 allow", bound_model_id=LLM_BARE, bound_use_case="analysis", expected="allow"
        ),
        await _drive(
            label="L2 deny by model",
            bound_model_id="gpt-4o",
            bound_use_case="analysis",
            expected="deny",
        ),
        await _drive(
            label="L3 deny by use-case",
            bound_model_id=LLM_BARE,
            bound_use_case="training",
            expected="deny",
        ),
    ]
    total_cost = 0.0
    print("\n=== Fluid MCP Snowflake e2e (CI) ===")
    for r in results:
        marker = "PASS" if r["status"] == "PASS" else "FAIL"
        print(
            f"[{marker}] {r['label']}  expected={r['expected']}  "
            f"actual={r['actual']}  reason={r.get('deny_reason') or '-'}  "
            f"rows={r.get('rows_served', 0)}  cost=${r['cost_usd']}"
        )
        total_cost += r["cost_usd"]
    print(f"\ntotal LLM cost: ${round(total_cost, 5)}")
    return 0 if all(r["status"] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
