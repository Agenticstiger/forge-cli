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

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def test_mcp_stdio_initialize_tools_list_and_call(tmp_path: Path):
    """Smoke-test the JSON-RPC wire end-to-end.

    NB on the I/O model: this used to use ``subprocess.run(input=...)``
    which writes all messages then immediately closes stdin. That
    worked against the legacy hand-rolled ``_serve_stdio`` (sequential
    sync loop — EOF only after each response was flushed) but is
    incompatible with the SDK's async architecture. The low-level
    server dispatches each request as a separate task via
    ``tg.start_soon`` and explicitly cancels in-flight handlers on
    stdin EOF (see the SDK's
    ``mcp/server/lowlevel/server.py::Server.run`` and its
    inline comment: *"Transport closed: cancel in-flight handlers …
    when they eventually try to respond they hit a closed write
    stream"*).

    Switching to ``Popen`` + read-each-response-before-sending-next
    keeps stdin open until every response has been received, matching
    the actual MCP transport contract.
    """
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    env["FLUID_QUIET"] = "1"
    env["FLUID_NONINTERACTIVE"] = "1"
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "pytest-mcp-smoke", "version": "1.0.0"},
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

    proc = subprocess.Popen(
        [sys.executable, "-m", "fluid_build", "mcp", "serve", "--read-only"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=env,
        text=True,
    )

    def _send(msg: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def _read_response_with_id(req_id: int, *, timeout: float = 10.0) -> dict:
        """Drain stdout until we see a JSON line whose ``id`` matches."""
        assert proc.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            stripped = line.strip()
            if not (stripped.startswith("{") and stripped.endswith("}")):
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id") == req_id:
                return obj
        raise AssertionError(f"timed out waiting for response with id={req_id}")

    responses: list[dict] = []
    try:
        for msg in messages:
            _send(msg)
            responses.append(_read_response_with_id(msg["id"]))
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[0]["result"]["capabilities"]["tools"]["listChanged"] is False
    assert responses[0]["result"]["serverInfo"]["name"] == "forge-cli-mcp"

    tools = responses[1]["result"]["tools"]
    assert tools
    assert all(tool.get("inputSchema", {}).get("type") == "object" for tool in tools)
    assert "list_source_adapters" in {tool["name"] for tool in tools}

    call_result = responses[2]["result"]
    assert call_result["isError"] is False
    assert call_result["content"][0]["type"] == "text"
    payload = json.loads(call_result["content"][0]["text"])
    assert {item["name"] for item in payload["adapters"]} >= {"snowflake", "bigquery"}
