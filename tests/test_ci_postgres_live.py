# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``postgres-role-check`` CI job runs every live Postgres test, and fails when one
cannot run.

The job starts a Postgres server and names it in ``FLUID_TEST_POSTGRES_DSN``. A live
module skips without that variable, so a job that lost it, its ``-m integration`` run or
a file from its pytest step would pass with nothing run. The job also sets
``FLUID_TEST_POSTGRES_REQUIRED=1``, which turns that skip into a failure
(``tests/output_ports/_live_postgres.py``). These tests pin all three, and the helper.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ._ci_workflow import REPO_ROOT, files_matching, files_run_by, job
from .output_ports._live_postgres import DSN_VARIABLE, REQUIRED_VARIABLE, live_postgres

pytestmark = pytest.mark.unit

_JOB = "postgres-role-check"
_LIVE = re.compile(r"\blive_postgres\(\)")


def _pytest_steps() -> List[Dict[str, Any]]:
    return [step for step in job(_JOB)["steps"] if "pytest" in step.get("run", "")]


def test_the_job_runs_every_live_postgres_test():
    live = files_matching(_LIVE, besides=Path(__file__))
    assert live, "found no live Postgres test; the scan is broken"
    missing = sorted(live - files_run_by(_JOB))
    assert not missing, f"add to the {_JOB} job in ci.yml: {missing}"


def test_every_file_the_job_names_exists():
    named = files_run_by(_JOB)
    assert named, f"found no test file in the {_JOB} job's pytest step"
    absent = sorted(path for path in named if not (REPO_ROOT / path).is_file())
    assert not absent, f"the {_JOB} job names files that do not exist: {absent}"


def test_the_job_names_its_server_runs_the_integration_tests_and_requires_them():
    steps = _pytest_steps()
    assert steps, f"found no pytest step in the {_JOB} job"
    for step in steps:
        env = step.get("env") or {}
        assert str(env.get(DSN_VARIABLE, "")).startswith("postgresql://"), step
        assert str(env.get(REQUIRED_VARIABLE)) == "1", step
        assert re.search(r"(?:^|\s)-m\s+integration(?:\s|$)", step["run"]), step


def test_a_live_module_fails_where_the_job_requires_it_and_skips_elsewhere(monkeypatch):
    """A skip raised here would mark this test skipped, not failed, so it is caught
    and turned into a failure: the job must fail, not skip, without a server."""
    monkeypatch.delenv(DSN_VARIABLE, raising=False)
    monkeypatch.setenv(REQUIRED_VARIABLE, "1")
    try:
        live_postgres()
    except pytest.fail.Exception as failed:
        assert DSN_VARIABLE in str(failed)
    except pytest.skip.Exception:
        pytest.fail("live_postgres() skipped where the job requires the live tests")
    else:
        pytest.fail("live_postgres() returned without a server")
    monkeypatch.delenv(REQUIRED_VARIABLE)
    with pytest.raises(pytest.skip.Exception, match=DSN_VARIABLE):
        live_postgres()
