# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The pack installs as a Claude Code plugin, through ``npx skills add``, and from the wheel.

Three installers read the same files:

* Claude Code reads ``.claude-plugin/marketplace.json`` at the repository root, then the
  plugin root it names (``.claude-plugin/plugin.json``, ``hooks/hooks.json``, ``skills/``);
* vercel-labs/skills (``npx skills add``) reads the same marketplace file and searches
  ``<source>/skills/`` (``src/plugin-manifest.ts``, checked 2026-11-06);
* ``fluid scaffold-ide`` copies the skills out of the installed package.

``claude plugin validate`` runs too when the ``claude`` CLI is on PATH.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluid_build.agent_skills import PACK_DIR, skill_dirs
from fluid_build.agent_skills.hooks import validate_contract as hook

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = PACK_DIR / ".claude-plugin" / "plugin.json"
HOOKS = PACK_DIR / "hooks" / "hooks.json"
EXAMPLE = (
    PACK_DIR / "skills" / "authoring-fluid-contracts" / "references" / "example.contract.fluid.yaml"
)
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_marketplace_lists_the_pack_by_a_relative_source() -> None:
    market = _load(MARKETPLACE)
    assert _KEBAB.match(market["name"])
    assert market["owner"]["name"]
    (entry,) = market["plugins"]
    assert entry["name"] == _load(PLUGIN)["name"]
    source = entry["source"]
    assert source.startswith("./") and ".." not in source
    assert (REPO_ROOT / source).resolve() == PACK_DIR


def test_the_plugin_manifest_is_valid_and_unversioned() -> None:
    """No ``version``: a relative-path plugin in a git-hosted marketplace is then
    versioned by commit SHA, so users get each pushed change. A pinned string would keep
    them on the cached copy until someone remembered to bump it."""
    plugin = _load(PLUGIN)
    assert _KEBAB.match(plugin["name"])
    assert not plugin["name"].startswith(("claude-", "anthropic-", "cc-plugin-"))
    assert plugin["description"] and plugin["license"] == "Apache-2.0"
    assert plugin["repository"] == "https://github.com/Agenticstiger/forge-cli"
    assert "version" not in plugin
    assert "version" not in _load(MARKETPLACE)["plugins"][0]


def test_npx_skills_finds_every_skill_through_the_marketplace() -> None:
    """vercel-labs/skills joins ``pluginRoot`` and ``source`` and searches ``skills/``."""
    market = _load(MARKETPLACE)
    root = market.get("metadata", {}).get("pluginRoot", "")
    found = []
    for entry in market["plugins"]:
        base = REPO_ROOT / root / entry["source"] / "skills"
        found += sorted(p.parent.name for p in base.glob("*/SKILL.md"))
    assert found == [d.name for d in skill_dirs()]


def test_the_hook_reads_its_input_from_stdin() -> None:
    """A hook command gets no ``${tool_input.*}`` substitution and an ``if`` rule holds
    one permission rule: the hook this replaced had both and never ran a validation."""
    hooks = _load(HOOKS)
    (entry,) = hooks["hooks"]["PostToolUse"]
    assert entry["matcher"] == "Edit|Write"
    (handler,) = entry["hooks"]
    assert handler["type"] == "command" and "if" not in handler
    command = handler["command"]
    assert "${tool_input" not in command
    assert '"${CLAUDE_PLUGIN_ROOT}/hooks/validate_contract.py"' in command
    assert (PACK_DIR / "hooks" / "validate_contract.py").is_file()


def test_the_wheel_ships_the_pack() -> None:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for glob in (
        '"agent_skills/skills/**/*"',
        '"agent_skills/hooks/*.json"',
        '"agent_skills/.claude-plugin/plugin.json"',
    ):
        assert glob in text, glob


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")
@pytest.mark.parametrize("target", [REPO_ROOT, PACK_DIR], ids=["marketplace", "plugin"])
def test_claude_plugin_validate_passes(target: Path) -> None:
    proc = subprocess.run(
        ["claude", "plugin", "validate", str(target)],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# The validate hook
# ---------------------------------------------------------------------------


def _payload(path: str) -> io.StringIO:
    return io.StringIO(json.dumps({"tool_name": "Edit", "tool_input": {"file_path": path}}))


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/w/contract.fluid.yaml", "/w/contract.fluid.yaml"),
        ("/w/products/a/orders.fluid.yml", "/w/products/a/orders.fluid.yml"),
        ("/w/contract.fluid.json", "/w/contract.fluid.json"),
        ("/w/README.md", None),
        ("/w/overlays/dev.yaml", None),
    ],
)
def test_the_hook_only_looks_at_contracts(path: str, expected) -> None:
    assert hook.contract_path({"tool_input": {"file_path": path}}) == expected


def test_a_payload_it_cannot_read_never_blocks() -> None:
    assert hook.main(io.StringIO("not json"), io.StringIO()) == 0
    assert hook.main(io.StringIO("[]"), io.StringIO()) == 0


def test_a_missing_fluid_is_shown_to_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 1 would reach only the transcript: the agent would think the contract checked."""
    monkeypatch.setattr(hook, "validate_command", lambda path: None)
    err = io.StringIO()
    assert hook.main(_payload("/w/contract.fluid.yaml"), err) == 2
    message = err.getvalue()
    assert "/w/contract.fluid.yaml was not validated" in message
    assert "pip install data-product-forge" in message
    assert "fluid validate /w/contract.fluid.yaml --offline" in message


def test_a_validator_that_runs_too_long_is_shown_to_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fluid", timeout=hook.TIMEOUT_S)

    monkeypatch.setattr(hook, "validate_command", lambda path: ["fluid", "validate", path])
    monkeypatch.setattr(hook.subprocess, "run", too_slow)
    err = io.StringIO()
    assert hook.main(_payload("/w/contract.fluid.yaml"), err) == 2
    message = err.getvalue()
    assert "/w/contract.fluid.yaml was not validated" in message
    assert f"more than {hook.TIMEOUT_S}s" in message


def test_the_hook_outlasts_its_own_validator_timeout() -> None:
    """Claude Code stops a hook at its timeout; the script's own message must come first."""
    (entry,) = _load(HOOKS)["hooks"]["PostToolUse"]
    (handler,) = entry["hooks"]
    assert handler["timeout"] > hook.TIMEOUT_S


def test_a_failed_validation_is_shown_to_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        out = "\x1b[31m 1. root: 'name' is required\r\n\x1b]52;c;cGF5bG9hZA==\x07\x9bdone\n"
        return SimpleNamespace(returncode=1, stdout=out, stderr="")

    monkeypatch.setattr(hook, "validate_command", lambda path: ["fluid", "validate", path])
    monkeypatch.setattr(hook.subprocess, "run", fake_run)
    err = io.StringIO()
    assert hook.main(_payload("/w/contract.fluid.yaml"), err) == 2
    message = err.getvalue()
    assert "'name' is required" in message
    for control in ("\x1b", "\x07", "\r", "\x9b", "52;c"):
        assert control not in message, repr(control)
    assert hook.printable("a\u202eb\u2066c\td\ne") == "abc\td\ne"
    assert "fluid validate /w/contract.fluid.yaml --offline" in message


def _json_run(returncode: int, report: dict):
    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=json.dumps(report), stderr="")

    return fake_run


_EMAIL_ISSUE = {
    "severity": "warning",
    "code": "ERR_CONTRACT_PLACEHOLDER_EMAIL",
    "pointer": "/metadata/owner/email",
    "message": "metadata.owner.email: 'sales-data@example.com' is a placeholder address: "
    "example.com is reserved for documentation (RFC 2606), so no mailbox is at it. "
    "Write the address the request gives; if it gives none, delete the key.",
    "fix": "Write the address the request gives; if it gives none, delete the key.",
    "example": "metadata:\n  owner:\n    team: sales-data\n",
    "docs_url": "https://agenticstiger.github.io/forge_docs/cli/validate.html",
}


def test_the_hook_asks_for_json_so_it_can_show_each_fix() -> None:
    command = hook.validate_command("/w/contract.fluid.yaml")
    if command is not None:
        assert command[1:] == [
            "validate",
            "/w/contract.fluid.yaml",
            "--offline",
            "--format",
            "json",
        ]


def test_a_failure_shows_each_issue_with_its_code_pointer_and_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enum = {
        "severity": "error",
        "code": "ERR_CONTRACT_SCHEMA_ENUM",
        "pointer": "/metadata/productType",
        "message": "metadata.productType: 'XDP' is not one of ['SDP', 'ADP', 'CDP']",
        "fix": "Use one of the values the message lists, spelled exactly.",
    }
    report = {"valid": False, "errors": [enum["message"]], "issues": [enum, _EMAIL_ISSUE]}
    monkeypatch.setattr(hook, "validate_command", lambda path: ["fluid", "validate", path])
    monkeypatch.setattr(hook.subprocess, "run", _json_run(1, report))
    err = io.StringIO()

    assert hook.main(_payload("/w/contract.fluid.yaml"), err) == 2
    message = err.getvalue()
    assert "- ERR_CONTRACT_SCHEMA_ENUM at /metadata/productType: metadata.productType" in message
    assert "  Fix: Use one of the values the message lists, spelled exactly." in message
    # The lint's message already ends with its fix: it is not printed twice.
    assert message.count("if it gives none, delete the key") == 1
    assert "fluid validate /w/contract.fluid.yaml --offline" in message


def test_a_pass_with_warnings_tells_the_agent_without_blocking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {"valid": True, "errors": [], "warnings": [_EMAIL_ISSUE["message"]]}
    report["issues"] = [_EMAIL_ISSUE]
    monkeypatch.setattr(hook, "validate_command", lambda path: ["fluid", "validate", path])
    monkeypatch.setattr(hook.subprocess, "run", _json_run(0, report))
    out, err = io.StringIO(), io.StringIO()

    assert hook.main(_payload("/w/contract.fluid.yaml"), err, out) == 0
    assert err.getvalue() == ""
    context = json.loads(out.getvalue())["hookSpecificOutput"]
    assert context["hookEventName"] == "PostToolUse"
    assert "ERR_CONTRACT_PLACEHOLDER_EMAIL at /metadata/owner/email" in context["additionalContext"]
    assert "never invent a value" in context["additionalContext"]


def test_a_clean_pass_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    report = {"valid": True, "errors": [], "warnings": []}
    monkeypatch.setattr(hook, "validate_command", lambda path: ["fluid", "validate", path])
    monkeypatch.setattr(hook.subprocess, "run", _json_run(0, report))
    out, err = io.StringIO(), io.StringIO()

    assert hook.main(_payload("/w/contract.fluid.yaml"), err, out) == 0
    assert (out.getvalue(), err.getvalue()) == ("", "")


def test_an_older_fluid_without_issues_still_shows_its_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {"valid": True, "errors": [], "warnings": ["exposes[0]: a warning"]}
    monkeypatch.setattr(hook, "validate_command", lambda path: ["fluid", "validate", path])
    monkeypatch.setattr(hook.subprocess, "run", _json_run(0, report))
    out = io.StringIO()

    assert hook.main(_payload("/w/contract.fluid.yaml"), io.StringIO(), out) == 0
    assert (
        "- exposes[0]: a warning"
        in json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
    )


def test_a_relative_path_cannot_become_an_option() -> None:
    path = hook.contract_path({"tool_input": {"file_path": "--report=x.fluid.yaml"}})
    assert path is not None and Path(path).is_absolute()
    command = hook.validate_command(path)
    if command is not None:
        assert command[2] == path


def _script(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / ("fluid.exe" if os.name == "nt" else "fluid")
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_a_fluid_in_the_project_directory_is_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows ``shutil.which`` searches the working directory first, so a ``fluid``
    the agent wrote into the project would win. The hook searches by hand and skips the
    working directory and relative PATH entries, so the real one on PATH still runs."""
    project = tmp_path / "project"
    _script(project)
    real = _script(tmp_path / "tools")
    monkeypatch.chdir(project)
    monkeypatch.setattr(hook.sys, "executable", str(tmp_path / "py" / "python"))
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project), str(tmp_path / "tools")]))
    assert hook.find_fluid() == str(real)

    monkeypatch.setenv("PATH", os.pathsep.join([".", str(project)]))
    assert hook.find_fluid() is None


def test_the_console_script_beside_the_interpreter_comes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beside = _script(tmp_path / "venv" / "bin")
    _script(tmp_path / "elsewhere")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hook.sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    assert hook.validate_command("/w/c.fluid.yaml")[0] == str(beside)


def test_the_hook_runs_the_real_validator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    good = tmp_path / "contract.fluid.yaml"
    shutil.copy(EXAMPLE, good)
    command = hook.validate_command(str(good))
    assert command is not None, "no fluid console script beside the interpreter or on PATH"
    assert Path(command[0]).name.startswith("fluid") and "-m" not in command
    assert hook.main(_payload(str(good)), io.StringIO()) == 0

    bad = tmp_path / "broken.fluid.yaml"
    bad.write_text(good.read_text(encoding="utf-8").replace("format: parquet", "format: duckdb"))
    err = io.StringIO()
    assert hook.main(_payload(str(bad)), err) == 2
    assert "'duckdb' is not one of" in err.getvalue()


@pytest.mark.parametrize("module", ["json", "re", "shutil"])
def test_a_module_planted_in_the_project_never_runs(tmp_path: Path, module: str) -> None:
    """The hook runs from the project directory, where the agent writes. Run as a
    script it never imports from there: ``python -m`` would, so a planted ``json.py``
    would run on the agent's next edit without any permission prompt."""
    marker = tmp_path / "pwned"
    (tmp_path / f"{module}.py").write_text(
        f"open({str(marker)!r}, 'w').write('x')\n", encoding="utf-8"
    )
    contract = tmp_path / "contract.fluid.yaml"
    shutil.copy(EXAMPLE, contract)
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(contract)}})
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, str(PACK_DIR / "hooks" / "validate_contract.py")],
        input=payload,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert not marker.exists(), proc.stderr
    assert proc.returncode in (0, 1), proc.stderr
