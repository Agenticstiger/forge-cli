# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The weekly catalog refresh keeps its write token away from fetched code.

The refresh installs unpinned packages and runs them against a network
response, and it also pushes a branch. Those two things used to share a job, so
the checkout's token sat in ``.git/config`` while ``pip install`` ran. The job
is split: one runs the fetched code with no write permission and no stored
credential, the other pushes and runs nothing it fetched.

These tests read the workflow as data; actionlint cannot say whether a token
and an install share a runner.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "update-model-catalog.yml"
)


@pytest.fixture(scope="module")
def doc() -> Dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(job["steps"])


def _uses(step: Dict[str, Any]) -> str:
    return str(step.get("uses", "")).split("@", 1)[0]


def _write_jobs(doc: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    return [
        (name, job)
        for name, job in doc["jobs"].items()
        if (job.get("permissions") or {}).get("contents") == "write"
    ]


def test_the_workflow_is_read_only_unless_one_job_asks_otherwise(doc):
    assert doc["permissions"] == {"contents": "read"}
    assert [name for name, _ in _write_jobs(doc)] == ["push-proposal"]


def test_no_checkout_leaves_the_token_on_disk(doc):
    seen = 0
    for name, job in doc["jobs"].items():
        for step in _steps(job):
            if _uses(step) == "actions/checkout":
                seen += 1
                assert step.get("with", {}).get("persist-credentials") is False, name
    assert seen == 2


def test_the_job_that_runs_fetched_code_cannot_write(doc):
    job = doc["jobs"]["update-catalog"]
    assert "permissions" not in job, "it must inherit the read-only default"
    text = "\n".join(str(s.get("run", "")) for s in _steps(job))
    assert "pip install" in text and "update_model_catalog.py" in text
    assert "git push" not in text
    assert "github.token" not in yaml.safe_dump(job)


def test_the_write_job_installs_and_runs_nothing_it_fetched(doc):
    ((_, job),) = _write_jobs(doc)
    assert job["needs"] == "update-catalog"
    for step in _steps(job):
        assert _uses(step) in {"", "actions/checkout", "actions/download-artifact"}
        run = str(step.get("run", ""))
        for forbidden in ("pip ", "python scripts/", "npm ", "curl ", "uv "):
            assert forbidden not in run, step.get("name")


def test_the_proposed_file_is_checked_before_it_replaces_anything(doc):
    ((_, job),) = _write_jobs(doc)
    names = [s.get("name") for s in _steps(job)]
    check = names.index("Check and place the proposed catalog")
    push = names.index("Push the proposed catalog to a branch")
    assert check < push
    run = _steps(job)[check]["run"]
    assert "json.load" in run and "isinstance" in run


def test_the_token_reaches_only_the_pushing_step_and_not_argv(doc):
    ((_, job),) = _write_jobs(doc)
    holders = [s for s in _steps(job) if "github.token" in yaml.safe_dump(s)]
    assert [s["name"] for s in holders] == ["Push the proposed catalog to a branch"]
    run = holders[0]["run"]
    assert holders[0]["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "GIT_CONFIG_VALUE_0" in run
    assert "-c http" not in run and "https://x-access-token" not in run


def _place(tmp_path: Path, proposed: object) -> subprocess.CompletedProcess:
    """Run the push job's check-and-place step under bash in a scratch tree."""
    step = next(
        s
        for s in _steps(yaml.safe_load(WORKFLOW.read_text())["jobs"]["push-proposal"])
        if s.get("name") == "Check and place the proposed catalog"
    )
    (tmp_path / "proposed").mkdir()
    (tmp_path / "fluid_build" / "cli").mkdir(parents=True)
    (tmp_path / "fluid_build" / "cli" / "llm_models.json").write_text("{}", encoding="utf-8")
    (tmp_path / "proposed" / "llm_models.json").write_text(json.dumps(proposed), encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", step["run"]], cwd=tmp_path, env=os.environ, capture_output=True, text=True
    )


_REAL = Path(__file__).resolve().parents[1] / "fluid_build" / "cli" / "llm_models.json"


def test_the_shipped_catalog_passes_the_check_and_is_placed(tmp_path) -> None:
    catalog = json.loads(_REAL.read_text(encoding="utf-8"))
    got = _place(tmp_path, catalog)
    assert got.returncode == 0, got.stdout + got.stderr
    placed = json.loads((tmp_path / "fluid_build/cli/llm_models.json").read_text("utf-8"))
    assert placed == catalog


@pytest.mark.parametrize("shape", ["list", "missing-key", "extra-key"])
def test_a_proposal_that_is_not_the_catalog_s_shape_replaces_nothing(tmp_path, shape) -> None:
    catalog = json.loads(_REAL.read_text(encoding="utf-8"))
    proposed: object = {
        "list": [catalog],
        "missing-key": {k: v for k, v in catalog.items() if k != "tiers"},
        "extra-key": {**catalog, "run": "x"},
    }[shape]
    got = _place(tmp_path, proposed)
    assert got.returncode != 0, got.stdout
    assert (tmp_path / "fluid_build/cli/llm_models.json").read_text("utf-8") == "{}"
