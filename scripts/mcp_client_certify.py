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

"""Certify forge-cli's MCP server against real local MCP clients.

This is intentionally separate from the hermetic pytest smoke test. The smoke
test pins the wire protocol without optional desktop tooling; this script uses
the real MCP Inspector CLI and Claude Code when they are installed so release
validation catches client-facing integration drift.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class CertificationCheck:
    name: str
    status: str
    detail: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _server_command() -> List[str]:
    return [
        sys.executable,
        "-m",
        "fluid_build",
        "mcp",
        "serve",
        "--read-only",
    ]


def _base_env(repo_root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["FLUID_QUIET"] = "1"
    env["FLUID_NONINTERACTIVE"] = "1"
    return env


def _run(
    args: List[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _tail(text: str, limit: int = 2000) -> str:
    return (text or "")[-limit:]


def _check_direct_jsonrpc(repo_root: Path, timeout: int) -> CertificationCheck:
    env = _base_env(repo_root)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "forge-cli-certifier", "version": "1.0.0"},
                "capabilities": {},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_source_adapters", "arguments": {}},
        },
    ]
    proc = subprocess.run(
        _server_command(),
        cwd=str(repo_root),
        env=env,
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return CertificationCheck(
            name="direct_jsonrpc",
            status="fail",
            detail=f"server exited {proc.returncode}",
            stdout_tail=_tail(proc.stdout),
            stderr_tail=_tail(proc.stderr),
        )
    responses = [
        json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")
    ]
    tools = responses[1]["result"]["tools"]
    call_result = responses[2]["result"]
    ok = (
        responses[0]["result"]["protocolVersion"] == "2025-06-18"
        and tools
        and all(tool.get("inputSchema", {}).get("type") == "object" for tool in tools)
        and call_result.get("isError") is False
    )
    return CertificationCheck(
        name="direct_jsonrpc",
        status="pass" if ok else "fail",
        detail=f"{len(tools)} tools advertised with schemas",
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


def _check_mcp_inspector(repo_root: Path, timeout: int) -> CertificationCheck:
    if not shutil.which("npx"):
        return CertificationCheck(
            name="mcp_inspector",
            status="skip",
            detail="npx not installed",
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
    server = ["--", *_server_command()]
    list_proc = _run(
        [*base, "--method", "tools/list", *server],
        cwd=repo_root,
        env=env,
        timeout=timeout,
    )
    call_proc = _run(
        [
            *base,
            "--method",
            "tools/call",
            "--tool-name",
            "list_source_adapters",
            *server,
        ],
        cwd=repo_root,
        env=env,
        timeout=timeout,
    )
    if list_proc.returncode != 0 or call_proc.returncode != 0:
        return CertificationCheck(
            name="mcp_inspector",
            status="fail",
            detail=f"tools/list rc={list_proc.returncode}; tools/call rc={call_proc.returncode}",
            stdout_tail=_tail(list_proc.stdout + call_proc.stdout),
            stderr_tail=_tail(list_proc.stderr + call_proc.stderr),
        )
    tools_payload = json.loads(list_proc.stdout)
    call_payload = json.loads(call_proc.stdout)
    ok = (
        tools_payload.get("tools")
        and call_payload.get("isError") is False
        and all(
            tool.get("inputSchema", {}).get("type") == "object" for tool in tools_payload["tools"]
        )
    )
    return CertificationCheck(
        name="mcp_inspector",
        status="pass" if ok else "fail",
        detail="MCP Inspector tools/list and tools/call succeeded",
        stdout_tail=_tail(call_proc.stdout),
        stderr_tail=_tail(list_proc.stderr + call_proc.stderr),
    )


def _claude_project_config(repo_root: Path) -> Dict[str, Any]:
    return {
        "mcpServers": {
            "fluid-forge": {
                "command": sys.executable,
                "args": [
                    "-m",
                    "fluid_build",
                    "mcp",
                    "serve",
                    "--read-only",
                ],
                "env": {
                    "PYTHONPATH": str(repo_root),
                    "FLUID_QUIET": "1",
                    "FLUID_NONINTERACTIVE": "1",
                },
            }
        }
    }


def _check_claude_code(repo_root: Path, timeout: int) -> CertificationCheck:
    if not shutil.which("claude"):
        return CertificationCheck(
            name="claude_code",
            status="skip",
            detail="claude CLI not installed",
        )
    env = _base_env(repo_root)
    with tempfile.TemporaryDirectory(prefix="fluid_mcp_claude_cert_") as td:
        temp_dir = Path(td)
        (temp_dir / ".mcp.json").write_text(
            json.dumps(_claude_project_config(repo_root), indent=2),
            encoding="utf-8",
        )
        proc = _run(
            ["claude", "mcp", "get", "fluid-forge"],
            cwd=temp_dir,
            env=env,
            timeout=timeout,
        )
    ok = proc.returncode == 0 and "Status: \u2713 Connected" in proc.stdout
    return CertificationCheck(
        name="claude_code",
        status="pass" if ok else "fail",
        detail=(
            "Claude Code project MCP health check connected"
            if ok
            else "Claude Code did not report connected"
        ),
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


def certify(
    *, timeout: int = 60, skip_inspector: bool = False, skip_claude: bool = False
) -> List[CertificationCheck]:
    repo_root = _repo_root()
    checks = [_check_direct_jsonrpc(repo_root, timeout)]
    if not skip_inspector:
        checks.append(_check_mcp_inspector(repo_root, timeout))
    if not skip_claude:
        checks.append(_check_claude_code(repo_root, timeout))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify forge-cli MCP client compatibility")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-inspector", action="store_true")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    checks = certify(
        timeout=args.timeout,
        skip_inspector=args.skip_inspector,
        skip_claude=args.skip_claude,
    )
    payload = [asdict(check) for check in checks]
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        for check in checks:
            print(f"{check.name}: {check.status} - {check.detail}")
    return 1 if any(check.status == "fail" for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
