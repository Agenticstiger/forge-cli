# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Each ``iac-tests.yml`` job holds only the token permissions it uses.

The Stage 1 and Stage 2 jobs run a pull request's test code. A job granted
``id-token: write`` can request an OIDC token, which the cloud roles trust, so that
grant belongs only to a job whose steps exchange one for credentials: the two Stage 3
jobs, through aws-actions/configure-aws-credentials and google-github-actions/auth.
The workflow level grants nothing (zizmor's excessive-permissions audit), so a job
added later starts with no permissions and asks for what it needs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "iac-tests.yml"
#: The steps that exchange the job's OIDC token for cloud credentials.
_OIDC_ACTIONS = ("aws-actions/configure-aws-credentials@", "google-github-actions/auth@")


def _workflow() -> Dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _uses_oidc(job: Dict[str, Any]) -> bool:
    return any(
        str(step.get("uses", "")).startswith(_OIDC_ACTIONS) for step in job.get("steps") or []
    )


def test_the_workflow_level_grants_nothing():
    assert _workflow()["permissions"] == {}


@pytest.mark.parametrize("name", sorted(_workflow()["jobs"]))
def test_a_job_requests_an_oidc_token_only_when_a_step_uses_it(name):
    job = _workflow()["jobs"][name]
    expected = {"contents": "read", **({"id-token": "write"} if _uses_oidc(job) else {})}
    assert job.get("permissions") == expected


def test_the_jobs_that_run_pull_request_code_get_no_oidc_token():
    jobs = _workflow()["jobs"]
    for name in ("stage1-unit", "stage1-lanes", "stage2-emulator"):
        assert "id-token" not in jobs[name]["permissions"], name
    assert {name for name, job in jobs.items() if _uses_oidc(job)} == {"stage3-aws", "stage3-gcp"}
