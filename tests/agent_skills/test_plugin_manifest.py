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


def test_a_missing_fluid_is_reported_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "validate_command", lambda path: None)
    err = io.StringIO()
    assert hook.main(_payload("/w/contract.fluid.yaml"), err) == 1
    assert "pip install data-product-forge" in err.getvalue()


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


def test_a_relative_path_cannot_become_an_option() -> None:
    path = hook.contract_path({"tool_input": {"file_path": "--report=x.fluid.yaml"}})
    assert path is not None and Path(path).is_absolute()
    command = hook.validate_command(path)
    if command is not None:
        assert command[2] == path


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
