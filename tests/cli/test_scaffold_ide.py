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

"""Tests for ``fluid scaffold-ide`` — the agentic-IDE config emitter.

Adversarial bias: this command emits configs the data team's IDE will execute.
Every test asks "what happens if the generated config is wrong?" — invalid
JSON breaks MCP boot; missing frontmatter means steering doesn't load; PATH-
dependent commands break in IDE subprocesses; idempotency failures clobber
project work.

Five shapes verified:

1. Per-target file tree is what the IDE expects (paths + files).
2. Generated JSON is valid and has the correct MCP server shape with an
   absolute python path (no PATH dependency in the IDE subprocess).
3. Steering files have the right frontmatter so each IDE actually loads them.
4. Idempotency: re-run without ``--force`` errors; with ``--force`` overwrites.
5. **Live MCP handshake**: spawn the generated ``mcp.command + args``, perform
   the MCP ``initialize`` + ``tools/list`` round-trip, assert forge tools
   appear. This is the only test that catches "config looks fine but the
   subprocess never speaks MCP."
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fluid_build.cli import scaffold_ide
from fluid_build.cli._common import CLIError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_TARGETS = ("kiro", "cursor", "claude-code", "cline", "generic")


def _make_args(out: Path, target: str, *, force: bool = False, python: str | None = None):
    return argparse.Namespace(
        target=target,
        out=str(out),
        force=force,
        python=python or sys.executable,
    )


def _run(out: Path, target: str, **kw) -> int:
    return scaffold_ide.run(_make_args(out, target, **kw))


# ---------------------------------------------------------------------------
# 1. Per-target file trees
# ---------------------------------------------------------------------------

EXPECTED_FILES = {
    "kiro": [
        ".kiro/settings/mcp.json",
        ".kiro/steering/01-forge-cli.md",
        ".kiro/steering/02-contract-schema.md",
        ".kiro/steering/03-pipeline-decisions.md",
        ".kiro/steering/04-guardrails.md",
        ".kiro/hooks/on-save-contract.md",
        ".kiro/hooks/pre-commit-bundle.md",
        ".kiro/specs/first-data-product.md",
    ],
    "cursor": [
        ".cursor/mcp.json",
        ".cursor/rules/01-forge-cli.mdc",
        ".cursor/rules/02-contract-schema.mdc",
        ".cursor/rules/03-pipeline-decisions.mdc",
        ".cursor/rules/04-guardrails.mdc",
        ".cursor/HOOKS.md",
    ],
    "claude-code": [
        ".mcp.json",
        "CLAUDE.md",
        ".claude/settings.json",
    ],
    "cline": [
        ".cline/mcp_settings.json",
        ".clinerules/01-forge-cli.md",
        ".clinerules/02-contract-schema.md",
        ".clinerules/03-pipeline-decisions.md",
        ".clinerules/04-guardrails.md",
        ".clinerules/MCP_SETUP.md",
    ],
    "generic": [
        "mcp.json",
        "AGENTS.md",
        ".ai/steering/01-forge-cli.md",
        ".ai/steering/02-contract-schema.md",
        ".ai/steering/03-pipeline-decisions.md",
        ".ai/steering/04-guardrails.md",
    ],
}


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_file_tree(tmp_path: Path, target: str):
    """Each target produces exactly the expected file set."""
    assert _run(tmp_path, target) == 0
    for rel in EXPECTED_FILES[target]:
        assert (tmp_path / rel).is_file(), f"{target}: missing {rel}"


# ---------------------------------------------------------------------------
# 2. Generated JSON validity + MCP server shape
# ---------------------------------------------------------------------------

MCP_CONFIG_PATHS = {
    "kiro": ".kiro/settings/mcp.json",
    "cursor": ".cursor/mcp.json",
    "claude-code": ".mcp.json",
    "cline": ".cline/mcp_settings.json",
    "generic": "mcp.json",
}


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_mcp_config_shape(tmp_path: Path, target: str):
    """MCP config must be valid JSON, have ``mcpServers.fluid``, and an
    absolute python path so the IDE subprocess doesn't depend on PATH.
    """
    _run(tmp_path, target)
    cfg_path = tmp_path / MCP_CONFIG_PATHS[target]
    config = json.loads(cfg_path.read_text())

    assert "mcpServers" in config
    assert "fluid" in config["mcpServers"]
    entry = config["mcpServers"]["fluid"]

    assert entry["command"] == sys.executable
    assert os.path.isabs(entry["command"]), "command must be absolute"
    assert entry["args"][:4] == ["-m", "fluid_build.cli", "mcp", "serve"]
    assert entry["disabled"] is False
    # Read-only tools should be in autoApprove; mutating ones should not.
    auto = set(entry["autoApprove"])
    assert "read_logical_model" in auto
    assert "validate_contract" in auto
    assert "list_source_tables" in auto
    # Mutating tools must require confirmation — never auto-approved.
    assert "update_entity" not in auto
    assert "forge_from_source" not in auto


def test_python_must_be_absolute(tmp_path: Path):
    """A relative --python path is rejected — IDE subprocesses inherit no PATH."""
    with pytest.raises(CLIError, match="absolute"):
        _run(tmp_path, "kiro", python="python")


# ---------------------------------------------------------------------------
# 3. Per-target frontmatter
# ---------------------------------------------------------------------------


def test_kiro_steering_has_inclusion_always(tmp_path: Path):
    """Kiro reads steering only when frontmatter says so."""
    _run(tmp_path, "kiro")
    for name in ("01-forge-cli", "02-contract-schema", "03-pipeline-decisions", "04-guardrails"):
        body = (tmp_path / f".kiro/steering/{name}.md").read_text()
        assert body.startswith("---\n"), f"{name}: missing frontmatter"
        assert "inclusion: always" in body.split("---\n")[1]


def test_cursor_rules_have_alwaysapply_true(tmp_path: Path):
    """Cursor MDC rules need alwaysApply: true to load on every conversation."""
    _run(tmp_path, "cursor")
    for name in ("01-forge-cli", "02-contract-schema", "03-pipeline-decisions", "04-guardrails"):
        body = (tmp_path / f".cursor/rules/{name}.mdc").read_text()
        head = body.split("---\n")[1]
        assert "alwaysApply: true" in head
        assert "description:" in head


def test_claude_code_writes_hooks_schema(tmp_path: Path):
    """Claude Code's .claude/settings.json must have a PostToolUse hook on
    Edit|Write of contract.fluid.yaml so saves trigger fluid validate.
    """
    _run(tmp_path, "claude-code")
    hooks = json.loads((tmp_path / ".claude/settings.json").read_text())
    pt = hooks["hooks"]["PostToolUse"]
    assert isinstance(pt, list) and len(pt) >= 1
    entry = pt[0]
    assert entry["matcher"] == "Edit|Write"
    assert any(
        "fluid validate" in h["command"] for h in entry["hooks"]
    ), "expected a fluid validate hook"


# ---------------------------------------------------------------------------
# 4. Idempotency + --force
# ---------------------------------------------------------------------------


def test_rerun_without_force_errors(tmp_path: Path):
    """Re-running without --force must refuse to clobber (caller can lose work)."""
    _run(tmp_path, "kiro")
    with pytest.raises(CLIError, match="refusing_to_overwrite"):
        _run(tmp_path, "kiro")


def test_rerun_with_force_overwrites(tmp_path: Path):
    """--force lets us refresh the canonical pack after a forge-cli upgrade."""
    _run(tmp_path, "kiro")
    # Edit a file
    target = tmp_path / ".kiro/steering/01-forge-cli.md"
    target.write_text("# user-edited\n")
    _run(tmp_path, "kiro", force=True)
    body = target.read_text()
    assert "# user-edited" not in body
    assert "forge-cli — agentic guide" in body


def test_generic_appends_to_existing_agents_md(tmp_path: Path):
    """Generic target preserves the existing AGENTS.md and appends our block."""
    (tmp_path / "AGENTS.md").write_text("# Project AGENTS\n\nour stuff\n")
    _run(tmp_path, "generic")
    body = (tmp_path / "AGENTS.md").read_text()
    assert body.startswith("# Project AGENTS")  # original preserved
    assert "BEGIN forge-cli scaffold-ide block" in body
    assert "END forge-cli scaffold-ide block" in body


def test_generic_skip_when_marker_present(tmp_path: Path):
    """Re-running on an AGENTS.md that already has our block is idempotent —
    doesn't double-append."""
    (tmp_path / "AGENTS.md").write_text(
        "# Project AGENTS\n\n"
        "<!-- BEGIN forge-cli scaffold-ide block -->\n"
        "old block\n"
        "<!-- END forge-cli scaffold-ide block -->\n"
    )
    # Second run is no-op without --force (block-detection check)
    _run(tmp_path, "generic")
    body = (tmp_path / "AGENTS.md").read_text()
    # exactly one BEGIN marker
    assert body.count("BEGIN forge-cli scaffold-ide block") == 1


# ---------------------------------------------------------------------------
# 5. LIVE MCP handshake — the test that catches "config looks fine but the
#    subprocess never speaks MCP." Spawns the actual generated invocation and
#    completes the JSON-RPC initialize + tools/list round-trip.
# ---------------------------------------------------------------------------


def _mcp_request(method: str, *, params: dict | None = None, req_id: int = 1) -> bytes:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return (json.dumps(body) + "\n").encode()


def _read_one_response(proc: subprocess.Popen, timeout: float = 10.0) -> dict | None:
    """Read JSON-RPC responses until we get an object with an ``id`` (skip
    server notifications / log lines)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        try:
            obj = json.loads(line.decode().strip())
        except json.JSONDecodeError:
            continue  # non-JSON log line
        if isinstance(obj, dict) and "id" in obj:
            return obj
    return None


@pytest.mark.slow
@pytest.mark.parametrize("target", ALL_TARGETS)
def test_mcp_handshake_lives(tmp_path: Path, target: str):
    """Spawn the exact command + args from the generated MCP config and complete
    the MCP handshake. This is the integration that catches all the silly
    breakage modes (wrong module path, missing `serve` subcommand, etc.).
    """
    _run(tmp_path, target)
    cfg = json.loads((tmp_path / MCP_CONFIG_PATHS[target]).read_text())
    entry = cfg["mcpServers"]["fluid"]

    proc = subprocess.Popen(
        [entry["command"], *entry["args"]],
        cwd=str(tmp_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **entry.get("env", {})},
    )
    try:
        # initialize
        proc.stdin.write(
            _mcp_request(
                "initialize",
                params={
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "scaffold-ide-test", "version": "1.0"},
                },
                req_id=1,
            )
        )
        proc.stdin.flush()
        resp = _read_one_response(proc, timeout=10.0)
        assert resp is not None, f"{target}: no initialize response"
        assert resp.get("id") == 1
        assert "result" in resp, f"{target}: initialize error {resp.get('error')}"

        # notifications/initialized (MCP spec)
        proc.stdin.write(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    }
                )
                + "\n"
            ).encode()
        )
        proc.stdin.flush()

        # tools/list — the smoke that proves the server is healthy
        proc.stdin.write(_mcp_request("tools/list", req_id=2))
        proc.stdin.flush()
        resp = _read_one_response(proc, timeout=10.0)
        assert resp is not None, f"{target}: no tools/list response"
        assert resp.get("id") == 2
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}

        # Must surface forge's signature tools
        expected_subset = {
            "read_logical_model",
            "validate_contract",
            "list_source_adapters",
            "list_source_tables",
            "inspect_source_table",
        }
        missing = expected_subset - names
        assert not missing, f"{target}: missing tools {missing}; got {names}"
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
