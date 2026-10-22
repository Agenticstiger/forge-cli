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

"""The ``duckdb-floor`` CI job runs what differs between DuckDB versions.

pyproject.toml takes ``duckdb>=1.5.0`` with no ceiling, and that job is the
only one that installs the floor itself. Its test list is written out, and a
written-out list drifts: the MCP SDK job's had come to name 3 of the 12 MCP
test files when it was replaced by ``-k mcp``. So the list must hold every
file the DuckDB end-to-end job runs, and every test file that branches on the
installed DuckDB version.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_TEST_FILE = re.compile(r"\btests/[\w/]+\.py\b")
#: How a test reads the installed DuckDB version (the compat and sandbox tests
#: derive their ``_DUCKDB_2`` flag from one of these).
_READS_THE_VERSION = re.compile(r"duckdb\.__version__|\bduckdb_version\(|\bMIN_DUCKDB_VERSION\b")


def _files_run_by(job: str) -> set[str]:
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    scripts = [step.get("run", "") for step in steps]
    return {path for script in scripts if "pytest" in script for path in _TEST_FILE.findall(script)}


def test_the_floor_runs_every_suite_the_duckdb_end_to_end_job_runs():
    end_to_end = _files_run_by("duckdb-integration")
    assert end_to_end, "found no test file in the DuckDB end-to-end job's pytest step"
    missing = sorted(end_to_end - _files_run_by("duckdb-floor"))
    assert not missing, f"add to the duckdb-floor job in ci.yml: {missing}"


def test_the_floor_runs_every_test_that_branches_on_the_duckdb_version():
    this_file = Path(__file__).resolve()
    branching = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("test_*.py")
        if path.resolve() != this_file
        and _READS_THE_VERSION.search(path.read_text(encoding="utf-8"))
    }
    assert branching, "found no test that reads the DuckDB version; the scan is broken"
    missing = sorted(branching - _files_run_by("duckdb-floor"))
    assert not missing, f"add to the duckdb-floor job in ci.yml: {missing}"


def test_every_file_the_floor_names_exists():
    absent = sorted(p for p in _files_run_by("duckdb-floor") if not (REPO_ROOT / p).is_file())
    assert not absent, f"the duckdb-floor job names files that do not exist: {absent}"
