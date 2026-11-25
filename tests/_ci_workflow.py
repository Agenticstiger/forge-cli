# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Reading ``.github/workflows/ci.yml`` for the tests that pin what a CI job runs."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Pattern, Set

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_TEST_FILE = re.compile(r"\btests/[\w/]+\.py\b")


def job(name: str) -> Dict[str, Any]:
    """The job ``name`` as ci.yml declares it."""
    jobs: Dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    found: Dict[str, Any] = jobs[name]
    return found


def scripts_of(name: str) -> str:
    """Every ``run`` script of the job ``name``, joined."""
    return "\n".join(step.get("run", "") for step in job(name)["steps"])


def files_run_by(name: str) -> Set[str]:
    """The test files the job ``name`` hands to pytest."""
    scripts = [step.get("run", "") for step in job(name)["steps"]]
    return {path for script in scripts if "pytest" in script for path in _TEST_FILE.findall(script)}


def files_matching(pattern: Pattern[str], *, besides: Path) -> Set[str]:
    """Every ``tests/**/test_*.py`` whose text ``pattern`` matches, ``besides`` (the
    test that asks) left out, as repository-relative paths."""
    asking = besides.resolve()
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("test_*.py")
        if path.resolve() != asking and pattern.search(path.read_text(encoding="utf-8"))
    }
