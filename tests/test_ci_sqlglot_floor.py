# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``sqlglot-floor`` CI job runs what differs between sqlglot versions.

pyproject.toml takes ``sqlglot>=<floor>`` with no ceiling, and that job is the only one
that installs the floor itself. Its test list is written out, and a written-out list
drifts. So the list must hold every test file that compiles a ``query_sql`` statement or
a row filter in process, since those results depend on how sqlglot reads SQL: the
gateway's projection, its row filters and the fence for filtered callers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ._ci_workflow import REPO_ROOT, files_matching, files_run_by, scripts_of

pytestmark = pytest.mark.unit

#: The calls that compile a statement or a row filter with sqlglot.
_COMPILES = re.compile(
    r"\b(?:compile_free_form_sql|_project_exposed_table|parse_row_filters"
    r"|row_filter_predicate|tool_query_sql)\("
)


def test_the_floor_runs_every_test_that_compiles_a_statement_or_a_row_filter():
    compiling = files_matching(_COMPILES, besides=Path(__file__))
    assert compiling, "found no test that compiles a statement; the scan is broken"
    missing = sorted(compiling - files_run_by("sqlglot-floor"))
    assert not missing, f"add to the sqlglot-floor job in ci.yml: {missing}"


def test_every_file_the_floor_names_exists():
    named = files_run_by("sqlglot-floor")
    assert named, "found no test file in the sqlglot-floor job's pytest step"
    absent = sorted(path for path in named if not (REPO_ROOT / path).is_file())
    assert not absent, f"the sqlglot-floor job names files that do not exist: {absent}"


def test_the_floor_installs_exactly_the_version_pyproject_names():
    scripts = scripts_of("sqlglot-floor")
    assert "pyproject.toml" in scripts and "sqlglot==${floor}" in scripts
