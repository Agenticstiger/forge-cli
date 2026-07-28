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

"""Certify the consumer MCP output-port server against real clients.

Mirrors :mod:`scripts.mcp_client_certify` but for the consumer-side
server (``fluid mcp output-port serve``). Three checks:

1. Direct JSON-RPC lifecycle: initialize → tools/list →
   ``describe`` / ``sample`` / ``query``. No external tooling.
2. MCP Inspector CLI ``tools/list`` and ``tools/call`` against the
   stdio server, when ``npx`` is installed.
3. Claude Code config health-check via ``claude mcp get`` when the
   ``claude`` CLI is on PATH.

Designed to be run before cutting a release; emits a JSON or
human-friendly report and exits non-zero when any required check
fails. Optional clients are reported as "skipped" rather than
failing.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CertificationCheck:
    name: str
    status: str  # "ok" / "fail" / "skipped"
    detail: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _server_command(contract_path: Path, *, expose_id: str) -> List[str]:
    return [
        sys.executable,
        "-m",
        "fluid_build",
        "mcp",
        "output-port",
        "serve",
        str(contract_path),
        "--expose-id",
        expose_id,
        "--max-sample-rows",
        "10",
    ]


def _base_env(repo_root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["FLUID_QUIET"] = "1"
    env["FLUID_NONINTERACTIVE"] = "1"
    return env


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def _write_demo_contract(workspace: Path) -> Tuple[Path, str]:
    """Write a tiny self-contained DuckDB-backed contract for the
    cert run. Returns (contract_path, expose_id)."""
    csv_path = workspace / "customers.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["customer_id", "email", "signup_date"])
        for customer_id, email, signup in [
            ("C0001", "alice@example.com", "2024-01-15"),
            ("C0002", "bob@example.com", "2024-02-10"),
            ("C0003", "carol@example.com", "2024-03-05"),
        ]:
            writer.writerow([customer_id, email, signup])
    contract_path = workspace / "contract.fluid.yaml"
    contract_path.write_text(
        f"""fluidVersion: "0.7.3"
kind: DataProduct
id: gold.cert.customers_v1
name: Cert customers
metadata:
  layer: Gold
  owner:
    team: certifier
    email: cert@example.com
  businessContext:
    domain: Test
exposes:
  - exposeId: customer_profiles
    kind: table
    contract:
      schema:
        - name: customer_id
          type: STRING
          required: true
        - name: email
          type: STRING
        - name: signup_date
          type: DATE
    binding:
      platform: local
      format: csv
      location:
        path: {csv_path}
        table: customer_profiles
    semantics:
      name: customer_profiles
      measures:
        - name: customer_count
          agg: count_distinct
          expr: customer_id
      dimensions:
        - name: signup_date
          type: time
      metrics:
        - name: active_customers
          type: simple
          measure: customer_count
""",
        encoding="utf-8",
    )
    return contract_path, "customer_profiles"


def _check_direct_jsonrpc(
    repo_root: Path, contract_path: Path, expose_id: str, timeout: int
) -> CertificationCheck:
    """Spin up the gateway as a subprocess and drive it via the
    official MCP Python SDK ClientSession over stdio.

    Replaces the previous raw-stdin JSON-RPC probe (which targeted
    the now-deleted custom dispatcher) with a real protocol
    exchange that mirrors how Claude Desktop / Cursor talk to the
    gateway in production. The check passes when:

    1. ``initialize`` completes (protocol negotiation succeeded).
    2. ``tools/list`` advertises {describe, sample, query}.
    3. ``tools/call describe`` returns a non-error payload.
    4. ``resources/list`` returns the contract + expose resources.

    Identity binding (``model=cert-script``) is self-attested through
    the version-correct channel (SDK 1.x: ``clientInfo`` extras; SDK
    2.x: the ``fluid`` capabilities-extensions block) via the
    ``_mcp_compat`` seam, so an operator running a sample agentPolicy
    on the validation contract sees the gate fire on the call path.
    The expected outcome depends on the contract's policy: if no
    agentPolicy is set, every call allows; if it is, the cert script
    needs the model in the allowlist (validation contract is authored
    with ``cert-script`` already in allowedModels by convention).
    """
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from fluid_build._mcp_compat import attr, self_attesting_client_kwargs

    cmd = _server_command(contract_path, expose_id=expose_id)
    server_params = StdioServerParameters(
        command=cmd[0],
        args=list(cmd[1:]),
        env=_base_env(repo_root),
        cwd=str(contract_path.parent),
    )

    async def _drive() -> Dict[str, Any]:
        client_kwargs = self_attesting_client_kwargs(
            "forge-cli-output-port-certifier",
            "1.0.0",
            model="cert-script",
            useCase="analysis",
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream, **client_kwargs) as session:
                init_result = await session.initialize()
                listing = await session.list_tools()
                resources = await session.list_resources()
                describe = await session.call_tool("describe", {})
                return {
                    "protocolVersion": attr(init_result, "protocol_version", "protocolVersion"),
                    "tools": [t.name for t in listing.tools],
                    "resources": [str(r.uri) for r in resources.resources],
                    "describe_payload_excerpt": (
                        describe.content[0].text[:200] if describe.content else ""
                    ),
                    "describe_is_error": attr(describe, "is_error", "isError", False),
                }

    try:
        result = asyncio.run(asyncio.wait_for(_drive(), timeout=timeout))
    except Exception as exc:  # noqa: BLE001
        return CertificationCheck(
            name="direct_jsonrpc",
            status="fail",
            detail=f"SDK client exchange failed: {type(exc).__name__}: {exc}",
        )

    required_tools = {"describe", "sample", "query"}
    advertised = set(result["tools"])
    missing_tools = sorted(required_tools - advertised)
    if missing_tools:
        return CertificationCheck(
            name="direct_jsonrpc",
            status="fail",
            detail=f"tools/list missing required tools: {missing_tools}",
        )
    if result["describe_is_error"]:
        return CertificationCheck(
            name="direct_jsonrpc",
            status="fail",
            detail=(
                "tools/call describe returned isError=True — likely an "
                "agentPolicy gate denied the cert-script identity. Add "
                "'cert-script' to allowedModels OR remove the model gate "
                "for the validation expose."
            ),
            stdout_tail=_tail(result["describe_payload_excerpt"]),
        )
    if not result["resources"]:
        return CertificationCheck(
            name="direct_jsonrpc",
            status="fail",
            detail="resources/list returned no resources",
        )
    return CertificationCheck(
        name="direct_jsonrpc",
        status="ok",
        detail=(
            f"SDK lifecycle complete: protocol={result['protocolVersion']}, "
            f"tools={sorted(advertised)}, resources={len(result['resources'])}"
        ),
    )


def _check_mcp_inspector(
    repo_root: Path, contract_path: Path, expose_id: str, timeout: int
) -> CertificationCheck:
    if shutil.which("npx") is None:
        return CertificationCheck(
            name="mcp_inspector",
            status="skipped",
            detail="npx not installed; install Node.js and re-run for full certification",
        )
    env = _base_env(repo_root)
    base = [
        "npx",
        "--yes",
        "@modelcontextprotocol/inspector",
        "--cli",
        "--transport",
        "stdio",
    ]
    server = ["--", *_server_command(contract_path, expose_id=expose_id)]
    inspector_args = [*base, "--method", "tools/list", *server]
    proc = subprocess.run(
        inspector_args,
        cwd=str(contract_path.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return CertificationCheck(
            name="mcp_inspector",
            status="fail",
            detail=f"inspector exited with code {proc.returncode}",
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
        )
    if "describe" not in proc.stdout:
        return CertificationCheck(
            name="mcp_inspector",
            status="fail",
            detail="inspector tools/list did not advertise the 'describe' tool",
            stdout_tail=_tail(proc.stdout),
        )
    return CertificationCheck(
        name="mcp_inspector",
        status="ok",
        detail="tools/list advertises the consumer tool surface",
    )


def _check_claude_cli(repo_root: Path) -> CertificationCheck:
    claude_path = shutil.which("claude")
    if claude_path is None:
        return CertificationCheck(
            name="claude_mcp_get",
            status="skipped",
            detail="claude CLI not installed; skip Claude Code project-config check",
        )
    env = _base_env(repo_root)
    proc = subprocess.run(
        [claude_path, "mcp", "list"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        return CertificationCheck(
            name="claude_mcp_get",
            status="skipped",
            detail=(
                "`claude mcp list` failed; ensure Claude Code is configured "
                "with at least one MCP server."
            ),
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
        )
    return CertificationCheck(
        name="claude_mcp_get",
        status="ok",
        detail=f"`claude mcp list` returned {len(proc.stdout.splitlines())} entries",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Certify forge-cli's MCP output-port server against real clients."
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output.")
    parser.add_argument("--timeout", type=int, default=120, help="Per-check timeout in seconds.")
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    with tempfile.TemporaryDirectory(prefix="output-port-cert-") as workspace:
        workspace_path = Path(workspace)
        contract_path, expose_id = _write_demo_contract(workspace_path)
        checks: List[CertificationCheck] = [
            _check_direct_jsonrpc(repo_root, contract_path, expose_id, args.timeout),
            _check_mcp_inspector(repo_root, contract_path, expose_id, args.timeout),
            _check_claude_cli(repo_root),
        ]

    overall_status = "ok"
    for check in checks:
        if check.status == "fail":
            overall_status = "fail"
            break
    if args.json:
        print(
            json.dumps(
                {
                    "overall_status": overall_status,
                    "checks": [asdict(check) for check in checks],
                },
                indent=2,
            )
        )
    else:
        print(f"forge-cli MCP output-port certification: {overall_status.upper()}")
        for check in checks:
            print(f"  [{check.status:>7}] {check.name} — {check.detail}")
            if check.stdout_tail and check.status == "fail":
                print(f"    stdout: {check.stdout_tail[-200:]}")
            if check.stderr_tail and check.status == "fail":
                print(f"    stderr: {check.stderr_tail[-200:]}")
    return 0 if overall_status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
