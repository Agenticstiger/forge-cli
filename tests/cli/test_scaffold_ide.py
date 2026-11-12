# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``fluid scaffold-ide`` — the agentic-IDE config emitter.

Adversarial bias: this command emits configs the data team's IDE will execute.
Every test asks "what happens if the generated config is wrong?" — invalid
JSON breaks MCP boot; missing frontmatter means steering doesn't load; PATH-
dependent commands break in IDE subprocesses; idempotency failures clobber
project work; a hook that never fires validates nothing.

Six shapes verified:

1. Per-target file tree is what the IDE expects (paths + files), skills included.
2. Generated JSON is valid and has the correct MCP server shape with an
   absolute python path (no PATH dependency in the IDE subprocess).
3. Steering files have the right frontmatter so each IDE actually loads them,
   and the index stays short, names every skill and tells the truth about the CLI.
4. Idempotency: re-run without ``--force`` errors; with ``--force`` overwrites,
   and an existing CLAUDE.md / AGENTS.md / .claude settings file keeps its content.
5. The Claude Code hook reads its input the way Claude Code passes it.
6. **Live MCP handshake**: spawn the generated ``mcp.command + args``, perform
   the MCP ``initialize`` + ``tools/list`` round-trip, assert forge tools
   appear. This is the only test that catches "config looks fine but the
   subprocess never speaks MCP."
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fluid_build.agent_skills import skill_dirs, skill_files
from fluid_build.cli import scaffold_ide
from fluid_build.cli._common import CLIError
from fluid_build.cli.mcp.models import TOOL_CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
_COMMANDS_TEST = REPO_ROOT / "tests" / "docs" / "test_template_readme_commands_are_real.py"
_spec = importlib.util.spec_from_file_location("_readme_commands", _COMMANDS_TEST)
assert _spec and _spec.loader
_readme_commands = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_readme_commands)

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


def _skill_paths(skills_dir: str) -> list[str]:
    return [
        f"{skills_dir}/{skill.name}/{rel.as_posix()}"
        for skill in skill_dirs()
        for rel in skill_files(skill)
    ]


# ---------------------------------------------------------------------------
# 1. Per-target file trees
# ---------------------------------------------------------------------------

EXPECTED_FILES = {
    "kiro": [
        ".kiro/settings/mcp.json",
        ".kiro/steering/fluid.md",
        ".kiro/hooks/on-save-contract.md",
        ".kiro/hooks/pre-commit-bundle.md",
        ".kiro/specs/first-data-product.md",
        *_skill_paths(".agents/skills"),
    ],
    "cursor": [
        ".cursor/mcp.json",
        ".cursor/rules/fluid.mdc",
        ".cursor/HOOKS.md",
        *_skill_paths(".agents/skills"),
    ],
    "claude-code": [
        ".mcp.json",
        "CLAUDE.md",
        ".claude/settings.local.json",
        ".gitignore",
        *_skill_paths(".claude/skills"),
    ],
    "cline": [
        ".cline/mcp_settings.json",
        ".clinerules/fluid.md",
        ".clinerules/MCP_SETUP.md",
        *_skill_paths(".agents/skills"),
    ],
    "generic": [
        "mcp.json",
        "AGENTS.md",
        *_skill_paths(".agents/skills"),
    ],
}

#: Where each target's always-loaded index lands.
INDEX_FILES = {
    "kiro": ".kiro/steering/fluid.md",
    "cursor": ".cursor/rules/fluid.mdc",
    "claude-code": "CLAUDE.md",
    "cline": ".clinerules/fluid.md",
    "generic": "AGENTS.md",
}


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_file_tree(tmp_path: Path, target: str):
    """Each target produces exactly the expected file set."""
    assert _run(tmp_path, target) == 0
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert written == sorted(EXPECTED_FILES[target])


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_skills_are_copied_byte_for_byte(tmp_path: Path, target: str):
    """The installed skills are the pack's own files, generated references included."""
    _run(tmp_path, target)
    skills_dir = scaffold_ide.SKILLS_DIRS[target]
    for skill in skill_dirs():
        for rel in skill_files(skill):
            installed = tmp_path / skills_dir / skill.name / rel
            assert installed.read_bytes() == (skill / rel).read_bytes(), installed


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
# 3. Per-target frontmatter and the index
# ---------------------------------------------------------------------------


def test_kiro_steering_has_inclusion_always(tmp_path: Path):
    """Kiro reads steering only when frontmatter says so."""
    _run(tmp_path, "kiro")
    body = (tmp_path / ".kiro/steering/fluid.md").read_text()
    assert body.startswith("---\n"), "missing frontmatter"
    assert "inclusion: always" in body.split("---\n")[1]


def test_cursor_rule_has_alwaysapply_true(tmp_path: Path):
    """Cursor MDC rules need alwaysApply: true to load on every conversation."""
    _run(tmp_path, "cursor")
    body = (tmp_path / ".cursor/rules/fluid.mdc").read_text()
    head = body.split("---\n")[1]
    assert "alwaysApply: true" in head
    assert "description:" in head


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_the_index_is_short_and_names_every_skill(tmp_path: Path, target: str):
    """The index is always in the agent's context, so it pays for every character.
    It replaced 11,196 characters of steering; it names each skill where it is
    installed and counts the MCP tools from the server's own registry."""
    _run(tmp_path, target)
    index = (tmp_path / INDEX_FILES[target]).read_text()
    assert len(index) < 3000, len(index)
    for skill in skill_dirs():
        path = f"{scaffold_ide.SKILLS_DIRS[target]}/{skill.name}/SKILL.md"
        assert path in index and (tmp_path / path).is_file(), path
    assert f"`fluid mcp serve`, {len(TOOL_CAPABILITIES)} tools" in index


@pytest.mark.parametrize("target", ALL_TARGETS)
def test_emitted_text_tells_the_truth_about_the_cli(tmp_path: Path, target: str):
    """Every `fluid` command the pack shows runs, and the drifted ones stay gone."""
    _run(tmp_path, target)
    defects = []
    for doc in sorted(tmp_path.rglob("*")):
        if doc.suffix not in {".md", ".mdc"}:
            continue
        text = doc.read_text()
        for retired in ("fluid generate-artifacts", "13 tools", "--no-verify-digest"):
            if retired in text:
                defects.append(f"{doc.relative_to(tmp_path)}: {retired}")
        commands = list(_readme_commands._commands(text))
        commands += re.findall(r"(?<!`)`(fluid [^`\n]+)`(?!`)", text)
        for command in commands:
            found = _readme_commands._defect(command)
            if found and found[0] != "missing required argument":
                defects.append(f"{doc.relative_to(tmp_path)}: `{command}` -> {found}")
    assert not defects, "\n".join(defects)


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
    target = tmp_path / ".kiro/steering/fluid.md"
    skill = tmp_path / ".agents/skills/authoring-fluid-contracts/SKILL.md"
    target.write_text("# user-edited\n")
    skill.write_text("# user-edited\n")
    _run(tmp_path, "kiro", force=True)
    assert "agent guide" in target.read_text()
    assert skill.read_text().startswith("---\nname: authoring-fluid-contracts\n")


@pytest.mark.parametrize("target, name", [("generic", "AGENTS.md"), ("claude-code", "CLAUDE.md")])
def test_existing_instructions_are_kept(tmp_path: Path, target: str, name: str):
    """The block is appended to an existing file, and --force replaces only the block."""
    (tmp_path / name).write_text("# Project rules\n\nour stuff\n")
    _run(tmp_path, target)
    body = (tmp_path / name).read_text()
    assert body.startswith("# Project rules\n\nour stuff\n")
    assert body.count("BEGIN forge-cli scaffold-ide block") == 1

    (tmp_path / name).write_text(body + "\n## After the block\n\nkept too\n")
    _run(tmp_path, target, force=True)
    refreshed = (tmp_path / name).read_text()
    assert refreshed.startswith("# Project rules\n\nour stuff\n")
    assert refreshed.rstrip().endswith("## After the block\n\nkept too")
    assert refreshed.count("BEGIN forge-cli scaffold-ide block") == 1
    assert refreshed.count("END forge-cli scaffold-ide block") == 1


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
    assert "old block" in body


# ---------------------------------------------------------------------------
# 5. The Claude Code validate hook
# ---------------------------------------------------------------------------


def _post_tool_use(root: Path, name: str = "settings.local.json") -> list:
    return json.loads((root / ".claude" / name).read_text())["hooks"]["PostToolUse"]


HOOK_SCRIPT = str(scaffold_ide._hook_script())


def test_claude_code_hook_reads_the_edited_path_from_stdin(tmp_path: Path):
    """Claude Code passes the tool call as JSON on stdin and substitutes nothing into
    the command; an ``if`` holds one permission rule. The hook it replaced
    (``fluid validate ${tool_input.file_path}`` behind ``Edit(...) | Write(...)``)
    therefore never validated a contract."""
    _run(tmp_path, "claude-code")
    (entry,) = _post_tool_use(tmp_path)
    assert entry["matcher"] == "Edit|Write"
    (handler,) = entry["hooks"]
    assert "if" not in handler
    assert "${" not in handler["command"]
    assert handler["command"] == f"{sys.executable} {HOOK_SCRIPT}"
    assert Path(HOOK_SCRIPT).is_file()


def test_claude_code_hook_never_runs_python_dash_m(tmp_path: Path):
    """``python -m`` puts the project directory, where the agent writes, first on
    ``sys.path``; the hook runs its script by path instead."""
    _run(tmp_path, "claude-code")
    (entry,) = _post_tool_use(tmp_path)
    assert " -m " not in entry["hooks"][0]["command"]


def test_claude_code_hook_is_personal_not_shared(tmp_path: Path):
    """The hook names this machine's interpreter, so it goes in settings.local.json,
    not the settings.json a team commits, and that file is kept out of git."""
    (tmp_path / ".gitignore").write_text("dist/")
    _run(tmp_path, "claude-code")
    _run(tmp_path, "claude-code", force=True)
    assert (tmp_path / ".claude/settings.local.json").is_file()
    assert not (tmp_path / ".claude/settings.json").exists()
    assert (tmp_path / ".gitignore").read_text() == "dist/\n.claude/settings.local.json\n"


def test_a_users_own_validate_hooks_are_left_alone(tmp_path: Path):
    """Only a handler scaffold-ide itself wrote is removed from the shared settings.json:
    a user's own ``validate_contract.sh`` or ``make validate_contracts`` stays."""
    shared = tmp_path / ".claude/settings.json"
    shared.parent.mkdir(parents=True)
    own = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [{"type": "command", "command": "./scripts/validate_contract.sh"}],
                },
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "make validate_contracts"}],
                },
            ]
        }
    }
    shared.write_text(json.dumps(own))
    _run(tmp_path, "claude-code")
    assert json.loads(shared.read_text()) == own


def test_claude_code_hook_quotes_an_interpreter_path_with_spaces(tmp_path: Path):
    python = str(tmp_path / "My Tools" / "python")
    _run(tmp_path / "ws", "claude-code", python=python)
    (entry,) = _post_tool_use(tmp_path / "ws")
    assert entry["hooks"][0]["command"].startswith(f"'{python}' ")


def test_claude_code_settings_are_merged_not_replaced(tmp_path: Path):
    """The team's settings.json keeps everything but the broken hook an earlier
    scaffold-ide wrote there, even other handlers in the same entry; the local file
    keeps its own settings, and a re-run never stacks a second copy."""
    shared = tmp_path / ".claude/settings.json"
    local = tmp_path / ".claude/settings.local.json"
    shared.parent.mkdir(parents=True)
    old_hook = {
        "type": "command",
        "if": "Edit(*contract.fluid.yaml) | Write(*contract.fluid.yaml)",
        "command": "fluid validate ${tool_input.file_path}",
        "timeout": 30,
    }
    beside = {"type": "command", "command": "echo also-on-edit"}
    theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    shared.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Bash(ls *)"]},
                "hooks": {
                    "PostToolUse": [{"matcher": "Edit|Write", "hooks": [old_hook, beside]}, theirs]
                },
            }
        )
    )
    local.write_text(json.dumps({"env": {"FOO": "1"}}))
    _run(tmp_path, "claude-code")
    _run(tmp_path, "claude-code", force=True)

    team = json.loads(shared.read_text())
    assert team["permissions"] == {"allow": ["Bash(ls *)"]}
    assert team["hooks"]["PostToolUse"] == [
        {"matcher": "Edit|Write", "hooks": [beside]},
        theirs,
    ]
    mine = json.loads(local.read_text())
    assert mine["env"] == {"FOO": "1"}
    commands = [h["command"] for e in mine["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert commands == [f"{sys.executable} {HOOK_SCRIPT}"]


@pytest.mark.parametrize(
    "body, event",
    [
        ("{ not json", "claude_settings_not_valid_json"),
        ('{"hooks": null}', "claude_settings_unexpected_shape"),
        ('{"hooks": {"PostToolUse": {"matcher": "x"}}}', "claude_settings_unexpected_shape"),
        ("[]", "claude_settings_unexpected_shape"),
    ],
)
def test_claude_code_settings_of_the_wrong_shape_are_refused(tmp_path: Path, body, event):
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(body)
    with pytest.raises(CLIError, match=event):
        _run(tmp_path, "claude-code")
    assert settings.read_text() == body


# ---------------------------------------------------------------------------
# 6. LIVE MCP handshake — the test that catches "config looks fine but the
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
