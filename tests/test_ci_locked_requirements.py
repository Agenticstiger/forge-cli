# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The hash-locked requirement files behave as the CI jobs and images assume.

A lock is only as good as its install line: ``--require-hashes`` is what makes
pip refuse a file the lock does not name. And an override in a ``.overrides``
file means nothing if the compiled lock still holds the older release. These
tests read the files; they do not resolve anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO = Path(__file__).resolve().parents[1]
REQ = REPO / "requirements"


def _pins(lock: Path) -> Dict[str, Version]:
    pins: Dict[str, Version] = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if m:
            pins[canonicalize_name(m.group(1))] = Version(m.group(2))
    return pins


def _compile_targets() -> list[str]:
    text = (REPO / "scripts" / "ci" / "compile_requirements.sh").read_text(encoding="utf-8")
    m = re.search(r"targets=\(([a-z][^)]*)\)", text)
    assert m, "no default target list in compile_requirements.sh"
    return m.group(1).split()


def test_every_lock_has_a_source_and_a_compile_rule() -> None:
    script = (REPO / "scripts" / "ci" / "compile_requirements.sh").read_text(encoding="utf-8")
    for name in _compile_targets():
        assert (REQ / f"{name}.in").is_file(), name
        assert (REQ / f"{name}.txt").is_file(), name
        assert f"{name} |" in script or f"| {name}" in script or f" {name})" in script, name


def test_the_singer_tap_is_installed_from_its_lock_only() -> None:
    text = (REPO / "Dockerfile.verify").read_text(encoding="utf-8")
    installs = [
        part.strip()
        for part in re.split(r"&&|;|\n", text.replace("\\\n", " "))
        if "pip install" in part and not part.lstrip().startswith("#")
    ]
    assert installs, "Dockerfile.verify installs nothing with pip"
    for cmd in installs:
        assert "--require-hashes -r " in cmd, cmd
    assert any("singer-tap.txt" in cmd for cmd in installs)
    assert "COPY requirements/singer-tap.txt" in text


def test_the_security_tools_lock_is_resolved_for_the_python_that_installs_it() -> None:
    """Security Scan installs it on 3.12; the fixed filelock and wcmatch need 3.11+."""
    script = (REPO / "scripts" / "ci" / "compile_requirements.sh").read_text(encoding="utf-8")
    m = re.search(r"([\w| -]*ci-security[\w| -]*)\) compile_one \"\$\{target\}\" (\S+)", script)
    assert m and m.group(2) == "3.12", script


def test_the_security_tools_lock_holds_the_overrides_and_the_filelock_floor() -> None:
    pins = _pins(REQ / "ci-security.txt")
    overrides = [
        Requirement(line.strip())
        for line in (REQ / "ci-security.overrides").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert {canonicalize_name(r.name) for r in overrides} >= {
        "mcp",
        "wcmatch",
        "opentelemetry-api",
        "opentelemetry-sdk",
    }
    for req in overrides:
        name = canonicalize_name(req.name)
        assert pins[name] in req.specifier, (name, pins[name], str(req.specifier))
    assert pins["filelock"] >= Version("4.2.1")


@pytest.mark.parametrize("name", ["ci-security", "singer-tap"])
def test_every_pin_carries_hashes(name: str) -> None:
    text = (REQ / f"{name}.txt").read_text(encoding="utf-8")
    entries = re.split(r"\n(?=[A-Za-z0-9][A-Za-z0-9._-]*==)", text)
    pinned = [e for e in entries if re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*==", e)]
    assert pinned
    for entry in pinned:
        assert "--hash=sha256:" in entry, entry.splitlines()[0]
