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
"""Live integration tests for the ``forge_run`` MCP tool + sampling backchannel.

These tests spawn the real ``fluid mcp serve`` subprocess and act as a
sampling-capable MCP client. They prove the end-to-end story:

1. IDE advertises ``sampling`` capability at ``initialize``.
2. IDE calls ``tools/call forge_run mode=diag prompt=…``.
3. forge_run sends ``sampling/createMessage`` back via stdout.
4. Test (acting as the IDE) replies with a synthetic LLM response on
   the same connection.
5. forge_run extracts the text and returns it in the ``tools/call``
   response.

If these pass, the full Pattern-2 flow (IDE LLM → MCP server → forge LLM
work, no API key) is wired and the IDE on the user's machine can drive
forge using its own subscription.

Why threading: the MCP server reads stdin line-by-line. ``forge_run``
issues a server→client request and blocks until the response arrives.
The test runs a reader thread that watches stdout for server-originated
requests (id ≥ 1_000_000) and writes synthetic replies; meanwhile the
main test thread sends the original ``tools/call`` and waits for the
final ``tools/call`` response.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue

import pytest


def _spawn_mcp_server(cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "fluid_build.cli", "mcp", "serve"],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ},
    )


class _McpHarness:
    """Fake MCP client that handles both client-request responses AND
    server-originated sampling requests. Watches stdout in a thread, sorts
    incoming messages into two queues by id-range.
    """

    SERVER_ID_FLOOR = 1_000_000

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.client_responses: Queue = Queue()  # responses to OUR requests
        self.server_requests: Queue = Queue()  # server→client requests
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while not self._stop.is_set():
            line = self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode().strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            mid = msg.get("id")
            # Server-originated request? (has method + id, e.g. sampling/createMessage)
            if "method" in msg and "id" in msg:
                self.server_requests.put(msg)
                continue
            # Response to our client request?
            if isinstance(mid, int) and mid < self.SERVER_ID_FLOOR:
                self.client_responses.put(msg)
                continue

    def send(self, payload: dict) -> None:
        self.proc.stdin.write((json.dumps(payload) + "\n").encode())
        self.proc.stdin.flush()

    def get_client_response(self, *, timeout: float = 15.0) -> dict:
        return self.client_responses.get(timeout=timeout)

    def get_server_request(self, *, timeout: float = 15.0) -> dict:
        return self.server_requests.get(timeout=timeout)

    def close(self) -> None:
        self._stop.set()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def harness(tmp_path: Path):
    proc = _spawn_mcp_server(tmp_path)
    h = _McpHarness(proc)
    try:
        # initialize with sampling capability (this is what makes the IDE
        # a "sampling-capable client" per MCP spec).
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"sampling": {}},
                    "clientInfo": {"name": "forge-run-live-test", "version": "1.0"},
                },
            }
        )
        init = h.get_client_response(timeout=10)
        assert init["id"] == 1, init
        assert init["result"]["protocolVersion"] == "2025-06-18"
        h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        yield h
    finally:
        h.close()


# ---------------------------------------------------------------------------
# 1. mode='blank' — forge_run runs without LLM, produces a contract
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_forge_run_blank_produces_contract(tmp_path: Path, harness: _McpHarness):
    """Sanity: forge_run mode='blank' runs the in-process forge --agent --blank
    path and produces a contract.fluid.yaml. No sampling required.
    """
    target = tmp_path / "product-blank"
    harness.send(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "forge_run",
                "arguments": {
                    "mode": "blank",
                    "target_dir": str(target),
                    "data_product_type": "SDP",
                },
            },
        }
    )
    resp = harness.get_client_response(timeout=20)
    assert resp["id"] == 2
    if "error" in resp:
        pytest.fail(f"forge_run blank errored: {resp['error']}")
    content = resp["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert payload["mode"] == "blank"
    assert payload["exit_code"] == 0
    assert payload["contract_exists"] is True
    assert Path(payload["contract_path"]).is_file()


# ---------------------------------------------------------------------------
# 2. mode='diag' — proves the sampling round-trip works end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_forge_run_diag_round_trips_sampling(tmp_path: Path, harness: _McpHarness):
    """The headline test: forge_run mode='diag' sends a sampling/createMessage
    request, we (acting as the IDE) reply with a synthetic LLM response, and
    forge_run's tools/call result carries that text back.

    If this passes, the data-team user in Cursor / Kiro / Claude Code can
    drive forge using their IDE's LLM with NO API key on their machine.
    """
    # Background thread: handle the sampling request the server will send.
    intercepted: dict = {}

    def respond_to_sampling():
        try:
            req = harness.get_server_request(timeout=15)
        except Empty:
            return
        intercepted["request"] = req
        assert req["method"] == "sampling/createMessage"
        # Synthetic LLM reply (what Cursor/Kiro/Claude Code would return).
        harness.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {
                    "role": "assistant",
                    "content": {
                        "type": "text",
                        "text": "Hello — from the IDE's LLM, no API key needed.",
                    },
                    "model": "synthetic-test-model",
                    "usage": {"input_tokens": 42, "output_tokens": 17},
                    "stopReason": "endTurn",
                },
            }
        )

    threading.Thread(target=respond_to_sampling, daemon=True).start()

    # Now: send the tools/call that triggers the sampling request.
    harness.send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "forge_run",
                "arguments": {
                    "mode": "diag",
                    "prompt": "Say hello so I know the channel works.",
                },
            },
        }
    )
    resp = harness.get_client_response(timeout=20)
    assert resp["id"] == 3
    if "error" in resp:
        pytest.fail(f"forge_run diag errored: {resp['error']}")

    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["mode"] == "diag"
    assert payload["response_text"] == "Hello — from the IDE's LLM, no API key needed."
    assert payload["model"] == "synthetic-test-model"
    # The SDK doesn't surface a ``usage`` block on its CreateMessageResult
    # (it's optional per spec). What matters is that the response text + model
    # round-tripped correctly.

    # And confirm the request the SDK sent on our behalf was spec-shaped.
    assert "request" in intercepted, "sampling/createMessage was never sent"
    params = intercepted["request"]["params"]
    assert params["systemPrompt"]
    assert params["maxTokens"] >= 1
    # Per spec, ``content`` on each SamplingMessage is a single TextContent
    # object — not a list. (The MCP SDK serialises this way; earlier
    # hand-rolled servers sometimes used a list, which was non-spec.)
    content = params["messages"][0]["content"]
    assert content["type"] == "text"
    assert content["text"].startswith("Say hello")


# ---------------------------------------------------------------------------
# 3. Clients that DON'T advertise sampling get a fast, actionable error
#    when they try forge_run mode='diag'/'ai'
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_forge_run_diag_fails_fast_without_sampling_capability(tmp_path: Path):
    """An IDE that doesn't support MCP sampling should hit a clean error
    immediately on forge_run mode='diag', not hang waiting for a response
    that will never come.
    """
    proc = _spawn_mcp_server(tmp_path)
    h = _McpHarness(proc)
    try:
        # Initialize WITHOUT sampling capability.
        h.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},  # no 'sampling'
                    "clientInfo": {"name": "non-sampling-test", "version": "1.0"},
                },
            }
        )
        init = h.get_client_response(timeout=10)
        assert init["id"] == 1
        h.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        h.send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "forge_run",
                    "arguments": {"mode": "diag", "prompt": "anything"},
                },
            }
        )
        resp = h.get_client_response(timeout=10)
        assert resp["id"] == 2
        # FastMCP reports tool exceptions via ``result.isError = True`` + the
        # human-readable message in ``content[0].text``, NOT via the JSON-RPC
        # ``error`` field. The error field is reserved for protocol-level
        # failures (unknown method, bad JSON). Tool-internal failures land in
        # the result envelope so the IDE can render them in the tool's UI.
        result = resp["result"]
        assert result.get("isError") is True, resp
        msg = result["content"][0]["text"]
        assert "sampling" in msg.lower()
        # Actionable suggestion the operator can act on.
        assert "fluid forge --agent --blank" in msg or "blank" in msg.lower()
    finally:
        h.close()
