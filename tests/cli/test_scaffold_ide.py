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
import contextlib
import importlib.util
import json
import logging
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
        *_skill_paths(".kiro/skills"),
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
    assert "regenerate_physical" not in auto


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
    skill = tmp_path / ".kiro/skills/authoring-fluid-contracts/SKILL.md"
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


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_kiro_skills_live_in_the_kiro_directory():
    """Kiro's workspace skills are read from .kiro/skills/ (https://kiro.dev/docs/skills/)."""
    assert scaffold_ide.SKILLS_DIRS["kiro"] == ".kiro/skills"


def test_a_bad_settings_file_is_found_before_anything_is_written(tmp_path: Path):
    """An invalid settings.json used to surface after .mcp.json and CLAUDE.md were written,
    and the re-run then refused to overwrite .mcp.json."""
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{ not json")
    with pytest.raises(CLIError, match="claude_settings_not_valid_json"):
        _run(tmp_path, "claude-code")
    assert sorted(_snapshot(tmp_path)) == [".claude/settings.json"]


def test_a_bad_local_settings_file_is_found_before_anything_is_written(tmp_path: Path):
    local = tmp_path / ".claude/settings.local.json"
    local.parent.mkdir(parents=True)
    local.write_text("[]")
    with pytest.raises(CLIError, match="claude_settings_unexpected_shape"):
        _run(tmp_path, "claude-code")
    assert sorted(_snapshot(tmp_path)) == [".claude/settings.local.json"]


def test_a_settings_path_that_is_a_directory_is_refused_cleanly(tmp_path: Path):
    (tmp_path / ".claude" / "settings.json").mkdir(parents=True)
    with pytest.raises(CLIError, match="claude_settings_not_valid_json"):
        _run(tmp_path, "claude-code")
    assert _snapshot(tmp_path) == {}


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_a_settings_file_linking_outside_the_project_is_refused(tmp_path: Path, name: str):
    """A cloned repository could link it to a credentials file outside the project."""
    secret = tmp_path / "home" / "credentials.json"
    secret.parent.mkdir()
    secret.write_text('{"token": "x"}')
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / name).symlink_to(secret)
    with pytest.raises(CLIError, match="claude_settings_links_outside_project"):
        _run(project, "claude-code")
    assert sorted(_snapshot(project)) == [f".claude/{name}"]
    assert (project / ".claude" / name).is_symlink()
    assert secret.read_text() == '{"token": "x"}'


def test_a_settings_file_linking_inside_the_project_is_followed(tmp_path: Path):
    shared = tmp_path / "config" / "claude.json"
    shared.parent.mkdir()
    shared.write_text("{}")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").symlink_to(shared)
    assert _run(tmp_path, "claude-code") == 0


@pytest.mark.parametrize("target, name", [("generic", "AGENTS.md"), ("claude-code", "CLAUDE.md")])
def test_instructions_that_are_not_utf8_are_refused_before_any_write(
    tmp_path: Path, target: str, name: str
):
    (tmp_path / name).write_bytes(b"# rules \xff\xfe\n")
    with pytest.raises(CLIError, match="scaffold_ide_file_not_utf8"):
        _run(tmp_path, target)
    assert list(_snapshot(tmp_path)) == [name]
    assert (tmp_path / name).read_bytes() == b"# rules \xff\xfe\n"


BEGIN = scaffold_ide._BLOCK_BEGIN
END = scaffold_ide._BLOCK_END


@pytest.mark.parametrize(
    "text, counts",
    [
        (f"mine\n{BEGIN}\nmy notes\n", (1, 0)),
        (f"mine\n{END}\nnotes\n{BEGIN}\n", (1, 1)),
        (f"{BEGIN}\na\n{END}\nmid\n{BEGIN}\nb\n{END}\n", (2, 2)),
        (f"notes\n{END}\n", (0, 1)),
    ],
    ids=["orphan-begin", "end-before-begin", "two-blocks", "orphan-end"],
)
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("target, name", [("generic", "AGENTS.md"), ("claude-code", "CLAUDE.md")])
def test_malformed_markers_are_refused_and_the_file_is_untouched(
    tmp_path: Path, target: str, name: str, force: bool, text: str, counts: tuple
):
    (tmp_path / name).write_text(text)
    with pytest.raises(CLIError, match="scaffold_ide_block_markers_malformed") as caught:
        _run(tmp_path, target, force=force)
    assert (caught.value.context["begin"], caught.value.context["end"]) == counts
    assert caught.value.suggestions
    assert (tmp_path / name).read_bytes() == text.encode()
    assert list(_snapshot(tmp_path)) == [name]


@pytest.mark.parametrize(
    "event",
    [
        "claude_settings_not_valid_json",
        "claude_settings_unexpected_shape",
        "claude_settings_links_outside_project",
        "scaffold_ide_file_not_utf8",
        "scaffold_ide_block_markers_malformed",
        "refusing_to_overwrite_existing_file_pass_force_to_override",
    ],
)
def test_every_refusal_carries_catalog_guidance(event: str):
    error = CLIError(2, event, {})
    assert any("fluid scaffold-ide" in line for line in error.suggestions)
    assert error.docs_url


def _gitignore_run(tmp_path: Path) -> None:
    _run(tmp_path, "claude-code")


def test_gitignore_keeps_its_mode(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist/\n")
    gitignore.chmod(0o644)
    _gitignore_run(tmp_path)
    assert gitignore.stat().st_mode & 0o777 == 0o644
    assert gitignore.read_text() == "dist/\n.claude/settings.local.json\n"


def test_gitignore_keeps_crlf_line_endings(tmp_path: Path):
    (tmp_path / ".gitignore").write_bytes(b"dist/\r\nbuild/")
    _gitignore_run(tmp_path)
    assert (tmp_path / ".gitignore").read_bytes() == (
        b"dist/\r\nbuild/\r\n.claude/settings.local.json\r\n"
    )


def test_gitignore_with_non_utf8_bytes_keeps_them(tmp_path: Path):
    (tmp_path / ".gitignore").write_bytes(b"caf\xff\n")
    assert _run(tmp_path, "claude-code") == 0
    assert (tmp_path / ".gitignore").read_bytes() == b"caf\xff\n.claude/settings.local.json\n"


def test_a_gitignore_directory_warns_and_the_run_succeeds(tmp_path: Path, caplog):
    (tmp_path / ".gitignore").mkdir()
    with caplog.at_level(logging.WARNING):
        assert _run(tmp_path, "claude-code") == 0
    assert ".claude/settings.local.json" in caplog.text
    assert (tmp_path / ".claude/settings.local.json").is_file()


def test_a_gitignore_linking_outside_the_project_is_never_written(tmp_path: Path, caplog):
    """An append follows the link: a cloned repository must not make it write elsewhere."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"precious\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").symlink_to(outside)
    with caplog.at_level(logging.WARNING):
        assert _run(project, "claude-code") == 0
    assert outside.read_bytes() == b"precious\n"
    assert "links outside" in caplog.text


def test_a_gitignore_linking_inside_the_project_is_appended_to(tmp_path: Path):
    shared = tmp_path / "config" / "ignore"
    shared.parent.mkdir()
    shared.write_bytes(b"node_modules/\n")
    (tmp_path / ".gitignore").symlink_to(shared)
    assert _run(tmp_path, "claude-code") == 0
    assert shared.read_bytes() == b"node_modules/\n.claude/settings.local.json\n"
    assert (tmp_path / ".gitignore").is_symlink()


@pytest.mark.parametrize("line", [".claude/settings.local.json", "/.claude/settings.local.json"])
def test_gitignore_already_naming_the_entry_is_unchanged(tmp_path: Path, line: str):
    body = f"dist/\r\n{line}\r\n".encode()
    (tmp_path / ".gitignore").write_bytes(body)
    _gitignore_run(tmp_path)
    assert (tmp_path / ".gitignore").read_bytes() == body


def _local_commands(root: Path) -> list[str]:
    return [h["command"] for e in _post_tool_use(root) for h in e["hooks"]]


def test_a_wrapper_around_the_validate_command_is_the_users_own(tmp_path: Path):
    shared = tmp_path / ".claude/settings.json"
    shared.parent.mkdir(parents=True)
    own = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "my-tool fluid validate ${tool_input.file_path} --strict",
                        },
                        {
                            "type": "command",
                            "command": f"my-tool {sys.executable} {HOOK_SCRIPT}",
                        },
                    ],
                }
            ]
        }
    }
    shared.write_text(json.dumps(own))
    _run(tmp_path, "claude-code")
    assert json.loads(shared.read_text()) == own


def test_the_exact_legacy_hook_is_removed(tmp_path: Path):
    shared = tmp_path / ".claude/settings.json"
    shared.parent.mkdir(parents=True)
    legacy = {"type": "command", "command": "  fluid validate ${tool_input.file_path}  "}
    shared.write_text(json.dumps({"hooks": {"PostToolUse": [{"hooks": [legacy]}]}}))
    _run(tmp_path, "claude-code")
    assert json.loads(shared.read_text())["hooks"]["PostToolUse"] == []


def test_reruns_never_stack_hook_entries(tmp_path: Path):
    for force in (False, True, True):
        # The first run writes .mcp.json; a later run without --force may refuse it.
        with contextlib.suppress(CLIError):
            _run(tmp_path, "claude-code", force=force)
    assert _local_commands(tmp_path) == [f"{sys.executable} {HOOK_SCRIPT}"]


def test_a_moved_install_replaces_the_old_hook(tmp_path: Path):
    local = tmp_path / ".claude/settings.local.json"
    local.parent.mkdir(parents=True)
    old = "'/old env/bin/python' /old/site-packages/fluid_build/agent_skills/hooks/validate_contract.py"
    win = "'C:\\py\\python.exe' 'C:\\site\\fluid_build\\agent_skills\\hooks\\validate_contract.py'"
    local.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": old}]},
                        {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": win}]},
                    ]
                }
            }
        )
    )
    _run(tmp_path, "claude-code")
    assert _local_commands(tmp_path) == [f"{sys.executable} {HOOK_SCRIPT}"]


def test_the_hook_timeout_outlasts_the_script_s_own():
    spec = importlib.util.spec_from_file_location("_validate_hook", HOOK_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entry = scaffold_ide._validate_hook_entry(sys.executable)
    assert entry["hooks"][0]["timeout"] > module.TIMEOUT_S


#: (target, directory, suffix) of the steering files earlier scaffold-ide versions wrote.
LEGACY = {
    "kiro": (".kiro/steering", ".md"),
    "cursor": (".cursor/rules", ".mdc"),
    "cline": (".clinerules", ".md"),
    "generic": (".ai/steering", ".md"),
}
LEGACY_FILES = {
    "01-forge-cli": "# forge-cli — agentic guide",
    "02-contract-schema": "# contract.fluid.yaml — schema cheatsheet",
    "03-pipeline-decisions": "# When to call which `fluid` command — decision tree",
    "04-guardrails": "# Guardrails — what's free with `pip install data-product-forge`",
}


def _write_legacy(root: Path, target: str) -> list[Path]:
    directory, suffix = LEGACY[target]
    front = "---\ninclusion: always\n---\n" if target in {"kiro", "cursor"} else ""
    paths = []
    for stem, title in LEGACY_FILES.items():
        path = root / directory / f"{stem}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{front}{title}\n\nfluid generate-artifacts, 13 tools\n")
        paths.append(path)
    return paths


@pytest.mark.parametrize("target", sorted(LEGACY))
def test_force_removes_the_legacy_steering_files(tmp_path: Path, target: str, capsys):
    legacy = _write_legacy(tmp_path, target)
    _run(tmp_path, target, force=True)
    assert not any(p.exists() for p in legacy)
    out = capsys.readouterr().out
    assert "removed 4 legacy steering file(s)" in out
    assert (tmp_path / INDEX_FILES[target]).is_file()


@pytest.mark.parametrize("target", sorted(LEGACY))
def test_without_force_legacy_steering_is_kept_with_a_warning(tmp_path: Path, target: str, caplog):
    legacy = _write_legacy(tmp_path, target)
    with caplog.at_level(logging.WARNING):
        _run(tmp_path, target)
    assert all(p.exists() for p in legacy)
    assert "re-run with --force to remove them" in caplog.text.replace("Re-run", "re-run")
    assert all(p.name in caplog.text for p in legacy)


def test_a_users_own_steering_file_with_another_heading_survives(tmp_path: Path):
    mine = tmp_path / ".kiro/steering/01-forge-cli.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("---\ninclusion: always\n---\n# Our team's forge rules\n\nkeep\n")
    other = tmp_path / ".kiro/steering/05-extra.md"
    other.write_text("# forge-cli — agentic guide\n")
    _run(tmp_path, "kiro", force=True)
    assert "keep" in mine.read_text()
    assert other.exists()


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
