# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid mcp serve --transport http``: the authoring tools over MCP
Streamable HTTP, driven through the real CLI in a subprocess.

The transport itself (gate, Origin / Host checks, session ownership) is
pinned by ``tests/test_mcp_http.py``; these tests pin what an operator sees:
the refusals, the typed errors, the startup line and a real client round trip.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")

from fluid_build._mcp_compat import open_streamable_http_session  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "0f" * 32  # pragma: allowlist secret
_AUTH_VARS = (
    "FLUID_MCP_AUTH_MODE",
    "FLUID_MCP_AUTH_TOKEN",
    "FLUID_MCP_JWT_ISSUER",
    "FLUID_MCP_JWT_AUDIENCE",
    "FLUID_MCP_JWT_JWKS_URL",
    "FLUID_MCP_TRUSTED_PROXIES",
)


def _env(tmp_path: Path, **overrides: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AUTH_VARS}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HOME": str(tmp_path / "home"),
            "FLUID_QUIET": "1",
            "FLUID_NONINTERACTIVE": "1",
            "COLUMNS": "200",
        }
    )
    env.update(overrides)
    return env


def _serve_cmd(*args: str) -> list:
    return [sys.executable, "-m", "fluid_build", "mcp", "serve", "--transport", "http", *args]


def _run(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        _serve_cmd(*args),
        cwd=str(tmp_path),
        env=_env(tmp_path, **env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_unauthenticated_serve_beyond_loopback_is_refused(tmp_path: Path, host: str) -> None:
    proc = _run(tmp_path, "--host", host, "--port", str(_free_port()))
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "fluid mcp serve: refusing to listen on" in output
    assert "--host 127.0.0.1" in output and "FLUID_MCP_AUTH_TOKEN" in output
    assert "Traceback" not in output


def test_a_short_token_beyond_loopback_is_refused(tmp_path: Path) -> None:
    short = "too-short-to-expose"  # pragma: allowlist secret
    proc = _run(
        tmp_path, "--host", "0.0.0.0", "--port", str(_free_port()), FLUID_MCP_AUTH_TOKEN=short
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "shorter than 32" in output
    assert short not in output


def test_an_unknown_auth_mode_is_a_typed_error(tmp_path: Path) -> None:
    proc = _run(tmp_path, "--port", str(_free_port()), FLUID_MCP_AUTH_MODE="mtls")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "FLUID_MCP_AUTH_MODE" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("port", ["0", "70000", "http"])
def test_the_port_must_be_a_tcp_port(tmp_path: Path, port: str) -> None:
    proc = _run(tmp_path, "--port", port)
    assert proc.returncode == 2
    assert "--port" in proc.stderr


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early ({proc.returncode}): {proc.stderr.read()}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"server did not listen on {port} within {timeout}s")


def test_serve_over_http_lists_and_calls_tools(tmp_path: Path) -> None:
    port = _free_port()
    proc = subprocess.Popen(
        _serve_cmd("--port", str(port), "--read-only"),
        cwd=str(tmp_path),
        env=_env(tmp_path, FLUID_MCP_AUTH_TOKEN=TOKEN),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        _wait_for_port(port, proc)
        refused = httpx.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Accept": "application/json, text/event-stream"},
            timeout=5,
        )
        assert refused.status_code == 401

        async def _round_trip():
            async with open_streamable_http_session(
                url, headers={"Authorization": f"Bearer {TOKEN}"}
            ) as session:
                names = {tool.name for tool in (await session.list_tools()).tools}
                adapters = await session.call_tool("list_source_adapters", {})
                return names, json.loads(adapters.content[0].text)

        names, adapters = asyncio.run(_round_trip())
        assert {"validate_contract", "list_source_adapters", "diff_models"} <= names
        # --read-only prunes the mutating tools over HTTP exactly as over stdio.
        assert not {"update_entity", "add_relationship", "regenerate_physical"} & names
        assert any(entry.get("name") == "snowflake" for entry in adapters["adapters"])
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
    assert proc.returncode == 0, stderr
    assert f"fluid mcp serve: MCP Streamable HTTP at {url} (auth: shared-token" in stderr
    assert TOKEN not in stderr


def _blocking_tool(monkeypatch: pytest.MonkeyPatch):
    """Replace every tool with one that blocks until released, recording the
    thread it ran on."""
    from fluid_build.cli.mcp import dispatch as dispatch_mod
    from fluid_build.cli.mcp import policy as policy_mod

    started, release, threads = threading.Event(), threading.Event(), []

    def _call_tool(name, arguments, **_):
        threads.append(threading.get_ident())
        started.set()
        release.wait(timeout=10)
        return {"tool": name}

    monkeypatch.setattr(dispatch_mod, "_call_tool", _call_tool)
    monkeypatch.setattr(policy_mod, "_current_policy", policy_mod.McpPolicy())
    return started, release, threads


def test_over_http_a_slow_tool_leaves_the_event_loop_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """One event loop serves every HTTP client: a tool waiting on a slow catalog
    must not stop the loop from serving the others (or from handling Ctrl-C)."""
    from fluid_build.cli.mcp import server as server_mod

    started, release, threads = _blocking_tool(monkeypatch)
    monkeypatch.setattr(server_mod, "_TOOLS_OFF_LOOP", True)

    async def _scenario():
        call = asyncio.create_task(server_mod._dispatch_sync_tool("list_source_adapters", {}))
        # Only reachable while the tool is still blocked if the loop is free.
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
        assert not call.done()
        release.set()
        return await asyncio.wait_for(call, timeout=5), threading.get_ident()

    result, loop_thread = asyncio.run(_scenario())
    assert result == {"tool": "list_source_adapters"}
    assert threads and threads[0] != loop_thread


def test_over_stdio_tools_still_run_on_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdio keeps synchronous dispatch: a pipelining client that closes stdin
    must get every response flushed (tests/test_mcp_protocol_smoke.py)."""
    from fluid_build.cli.mcp import server as server_mod

    _, release, threads = _blocking_tool(monkeypatch)
    release.set()
    monkeypatch.setattr(server_mod, "_TOOLS_OFF_LOOP", False)

    async def _scenario():
        await server_mod._dispatch_sync_tool("list_source_adapters", {})
        return threading.get_ident()

    loop_thread = asyncio.run(_scenario())
    assert threads == [loop_thread]


def test_serve_http_moves_tools_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build import _mcp_http
    from fluid_build.cli.mcp import cli as cli_mod
    from fluid_build.cli.mcp import policy as policy_mod
    from fluid_build.cli.mcp import server as server_mod

    async def _no_serve(*args, **kwargs):
        return None

    for name in _AUTH_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(server_mod, "_TOOLS_OFF_LOOP", False)
    monkeypatch.setattr(policy_mod, "_current_policy", None)
    monkeypatch.setattr(_mcp_http, "serve_streamable_http", _no_serve)
    args = argparse.Namespace(transport="http", host="127.0.0.1", port=_free_port())
    rc = cli_mod._serve_http(
        server_mod._get_mcp_app(), policy_mod.McpPolicy(), args, logging.getLogger("t")
    )
    assert rc == 0
    assert server_mod._TOOLS_OFF_LOOP is True
