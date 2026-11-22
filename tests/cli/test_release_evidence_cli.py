# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The deploy evidence a job Forge does not run leaves, through the ``fluid`` command itself.

``fluid ship --release-dir`` seals a release at each deploy, and ``fluid runs status
--release-dir`` reads the sealed releases back: which version and which code was in force, and
the bundle a commit's ``fluid.bundle-digest`` stamp names. Both run here through
``fluid_build.cli:main``, the ``fluid`` console script, with the real stages of ``fluid ship``
(each one a ``fluid`` process, as in CI).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

import fluid_build
from fluid_build.cli import _ship_stages as st
from fluid_build.cli import main

REPO_ROOT = Path(fluid_build.__file__).resolve().parent.parent
PRODUCT = "example.evidence"
FIRST, SECOND = "4458aaa4" * 5, "85096c85" * 5


def _contract(version: str) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Evidence",
        "domain": "example",
        "metadata": {"layer": "Bronze", "owner": {"team": "data-platform"}},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "version": version,
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": "out/orders.csv"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
        "builds": [{"id": "external", "pattern": "hybrid-reference"}],
    }


@pytest.fixture(scope="module")
def deployed(tmp_path_factory: pytest.TempPathFactory) -> Dict[str, Any]:
    """Two deploys of the job's own pipeline, each sealed by ``fluid ship --release-dir``: the
    contract at 1.4.0 built from ``FIRST``, then at 1.5.0 from ``SECOND`` (made once: each
    ``fluid ship`` runs four ``fluid`` processes)."""
    work = tmp_path_factory.mktemp("evidence") / "project"
    work.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        for name in ("FLUID_ENV", "GENERATE_EMIT", "FLUID_RUN_ID", *st.STAGE_TOGGLES.values()):
            mp.delenv(name, raising=False)
        for name in ("FLUID_SCHEDULER_JOB", "FLUID_SCHEDULER_RUN_ID"):
            mp.delenv(name, raising=False)
        # Each stage is a ``python -m fluid_build.cli`` process: it must import this checkout.
        paths = [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")]
        mp.setenv("PYTHONPATH", os.pathsep.join(p for p in paths if p))
        mp.setenv("COLUMNS", "200")
        mp.chdir(work)
        first = _deploy(work, mp, "1.4.0", FIRST)
        second = _deploy(work, mp, "1.5.0", SECOND)
    return {"releases": str(work.parent / "releases"), "first": first, "second": second}


def _deploy(
    project: Path, monkeypatch: pytest.MonkeyPatch, version: str, sha: str
) -> Dict[str, Any]:
    """One deploy: the contract at ``version``, built by GitHub Actions from ``sha``, sealed by
    ``fluid ship --release-dir`` beside the deploy. Returns its ``release.json``."""
    contract = project / "contract.fluid.yaml"
    contract.write_text(yaml.safe_dump(_contract(version), sort_keys=False), encoding="utf-8")
    for name, value in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ID": f"run-{version}",
        "GITHUB_SHA": sha,
        "GITHUB_REPOSITORY": "org/orders",
        "GITHUB_REF": f"refs/tags/v{version}",
    }.items():
        monkeypatch.setenv(name, value)
    release_dir = project.parent / "releases" / f"v{version}"
    argv = ["ship", str(contract), "--until", "validate-artifacts", "--ci-output", "plain"]
    assert main([*argv, "--release-dir", str(release_dir)]) == 0
    index: Dict[str, Any] = json.loads((release_dir / "release.json").read_text(encoding="utf-8"))
    return index


def test_each_deploy_is_sealed_and_runs_status_says_which_was_in_force(
    deployed: Dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    first, second, releases = deployed["first"], deployed["second"], deployed["releases"]
    assert first["identity"]["expose_versions"] == {"orders": "1.4.0"}
    assert first["provenance"]["source"]["revision"] == FIRST
    capsys.readouterr()

    assert main(["runs", "status", PRODUCT, "--release-dir", releases, "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["releases_read"] == 2 and answer["not_read"] == {}
    (only,) = answer["answers"]
    in_force = only["in_force"]
    assert in_force["expose_versions"] == {"orders": "1.5.0"}
    assert in_force["release_id"] == second["identity_digest"]
    assert in_force["bundle_digest"] == second["identity"]["bundle"]["manifest_digest"]
    assert in_force["source"] == {
        "revision": SECOND,
        "repository": "https://github.com/org/orders",
        "ref": "refs/tags/v1.5.0",
    }

    assert main(["runs", "status", PRODUCT, "--release-dir", releases]) == 0
    text = capsys.readouterr().out
    assert "In force now:" in text and "orders 1.5.0" in text
    assert f"code     {SECOND} https://github.com/org/orders refs/tags/v1.5.0" in text
    assert f"bundle   {second['identity']['bundle']['manifest_digest']}" in text
    assert "orders 1.4.0" not in text  # sealed before, and no longer in force


def test_a_day_before_any_deploy_has_no_release_and_bad_input_is_a_usage_error(
    deployed: Dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    releases = deployed["releases"]
    capsys.readouterr()
    assert main(["runs", "status", PRODUCT, "--release-dir", releases, "--on", "2000-01-01"]) == 1
    assert (
        f"No release of {PRODUCT} was sealed by the end of 2000-01-01." in capsys.readouterr().out
    )
    assert main(["runs", "status", PRODUCT, "--release-dir", releases, "--env", "prod"]) == 1
    assert main(["runs", "status", PRODUCT, "--release-dir", releases, "--on", "2026-13-01"]) == 2
    assert main(["runs", "status", PRODUCT, "--release-dir", str(tmp_path / "nowhere")]) == 2
