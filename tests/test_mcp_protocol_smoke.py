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
from pathlib import Path


def test_mcp_stdio_initialize_tools_list_and_call(tmp_path: Path):
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
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build",
            "mcp",
            "serve",
            "--read-only",
        ],
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    responses = [
        json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")
    ]
    assert [response["id"] for response in responses] == [1, 2, 3]
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
