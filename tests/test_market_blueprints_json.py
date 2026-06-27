# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""`fluid market --blueprints --format json` must emit PURE machine-parseable JSON.

Regression test for the gap where the blueprint-listing path ignored `--format
json` and printed the rich human table (plus a deprecation banner and registry
status lines) to stdout, contaminating any script that piped the output. Only
the catalog path honoured `--format json`. These tests drive the REAL CLI and
assert the whole stdout stream parses as JSON with the expected keys — the bug
was specifically stdout contamination, so an in-process call wouldn't catch it.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit]

_EXPECTED_KEYS = {"id", "name", "category", "maturity", "source", "version", "description"}


def _run_blueprints_json(*extra: str) -> subprocess.CompletedProcess:
    # No cwd override: inherits the test process's cwd so it resolves the same
    # fluid_build under test (installed in CI; source in a dev checkout). Offline
    # — bundled blueprints ship in the package, so no registry/network is needed.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build.cli",
            "market",
            "--blueprints",
            "--format",
            "json",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_blueprints_json_stdout_is_pure_parseable_json() -> None:
    r = _run_blueprints_json()
    assert r.returncode == 0, r.stderr
    # The WHOLE stdout must parse — no banner / table / status-line contamination.
    data = json.loads(r.stdout)
    assert isinstance(data, list)
    assert data, "expected at least the bundled blueprints"
    for item in data:
        assert _EXPECTED_KEYS <= set(item), f"missing keys: {_EXPECTED_KEYS - set(item)}"


def test_blueprints_json_has_no_human_contamination() -> None:
    r = _run_blueprints_json()
    assert r.returncode == 0, r.stderr
    for noise in ("Searching marketplace", "deprecated", "Blueprint Marketplace (", "No registry"):
        assert noise not in r.stdout, f"human noise leaked into JSON stdout: {noise!r}"


def test_blueprints_json_respects_search_filter() -> None:
    r = _run_blueprints_json("--search", "analytics")
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert isinstance(data, list)  # may be empty, but must still be a valid JSON array
