"""A2/A3 — the welcome scan detects AI coding-agent CLIs and surfaces keyless
authoring (no API key needed).

Reliable signal: which agent CLIs are on PATH (``shutil.which``). A live MCP
sampling channel is deliberately *not* probed here — it only exists inside the
non-interactive ``forge_run`` tool, never in the interactive path where the
welcome scan runs, so checking it would be dead code.
"""

from __future__ import annotations

import pytest

from fluid_build.cli import _welcome_scan
from fluid_build.cli._welcome_scan import (
    WelcomeFindings,
    render_welcome,
    run_welcome_scan,
)

pytestmark = pytest.mark.unit


def _which_factory(present):
    present = set(present)

    def _which(binary):
        return f"/usr/local/bin/{binary}" if binary in present else None

    return _which


def test_probe_coding_agents_maps_binaries_to_canonical(monkeypatch):
    monkeypatch.setattr(_welcome_scan.shutil, "which", _which_factory({"claude", "cursor-agent"}))
    result = _welcome_scan._probe_coding_agents()
    # Order follows _CODING_AGENT_BINARIES (claude-code first).
    assert result == {"coding_agents_available": ["claude-code", "cursor"]}


def test_probe_coding_agents_none_installed(monkeypatch):
    monkeypatch.setattr(_welcome_scan.shutil, "which", _which_factory(set()))
    assert _welcome_scan._probe_coding_agents() == {"coding_agents_available": []}


def test_suggested_keyless_prefers_claude_code():
    f = WelcomeFindings(coding_agents_available=["claude-code", "codex"])
    assert f.suggested_keyless_provider() == "claude-code"
    assert (
        WelcomeFindings(coding_agents_available=["codex"]).suggested_keyless_provider() == "codex"
    )
    assert WelcomeFindings().suggested_keyless_provider() == ""


def test_run_welcome_scan_populates_coding_agents(tmp_path, monkeypatch):
    monkeypatch.setattr(_welcome_scan.shutil, "which", _which_factory({"claude"}))
    # Isolate ~/.fluid/usage.json so we aren't flagged as a return user.
    monkeypatch.setattr(_welcome_scan.Path, "home", lambda: tmp_path)
    findings = run_welcome_scan(start=tmp_path)
    assert "claude-code" in findings.coding_agents_available


def test_render_surfaces_keyless_option_when_agent_present():
    from rich.console import Console

    findings = WelcomeFindings(coding_agents_available=["claude-code"], scan_duration_ms=5)
    out = Console(record=True, width=100)
    render_welcome(findings, console=out)
    rendered = out.export_text()
    assert "Keyless" in rendered
    assert "claude-code" in rendered


def test_render_no_keyless_row_without_agents():
    from rich.console import Console

    findings = WelcomeFindings(scan_duration_ms=5)
    out = Console(record=True, width=100)
    render_welcome(findings, console=out)
    rendered = out.export_text()
    assert "Keyless" not in rendered
