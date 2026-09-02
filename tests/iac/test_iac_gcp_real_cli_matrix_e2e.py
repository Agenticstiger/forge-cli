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

"""Stage 3 — full ``fluid`` CLI matrix on real GCP.

GCP analogue of ``test_iac_aws_real_cli_matrix_e2e.py``. The other GCP
Stage 3 tests prove the *emitter + tofu apply* path end-to-end (via
``runner.tofu_apply`` directly). This file proves the **CLI dispatch
chain** for GCP:

  fluid validate → fluid plan → fluid apply (every applicable --mode)

Six tests:

  * test_real_cli_validate_gcp — `fluid validate` returns 0 on a valid
    GCP contract.
  * test_real_cli_plan_emits_signed_plan_json_gcp — `fluid plan`
    writes plan.json with bundleDigest + planDigest populated.
  * test_real_cli_apply_dry_run_no_resources_gcp — `fluid apply --mode
    dry-run` plans but does not apply (no BQ dataset created).
  * test_real_cli_apply_amend_creates_resources_gcp — `fluid apply
    --mode amend` runs through to a real BigQuery dataset.
  * test_real_cli_apply_plan_binding_tamper_rejected_gcp — a corrupted
    plan.json makes fluid-apply raise PlanBindingError.
  * test_real_cli_apply_no_verify_plan_binding_bypass_gcp — the same
    corrupt plan + ``--no-verify-plan-binding`` succeeds with WARNING.

Triple-gated like the rest of GCP Stage 3.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest
import yaml

from .conftest import (
    GCP_LIVE_ENABLED,
    GCP_LIVE_PROJECT,
    GCP_LIVE_REGION,
    GCP_LIVE_SKIP_REASON,
    GCP_LIVE_TEST_SA,
    gcp_real_client,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.gcp,
    pytest.mark.slow,
    pytest.mark.skipif(not GCP_LIVE_ENABLED, reason=GCP_LIVE_SKIP_REASON),
]


def _fluid(
    *args: str,
    cwd: Path,
    env_overrides: Optional[Mapping[str, str]] = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _live_env() -> Dict[str, str]:
    """Stage-3 gate + GCP provider env (project, region, impersonation)."""
    return {
        "FLUID_IAC_LIVE_GCP": "1",
        "FLUID_GCP_PROJECT": GCP_LIVE_PROJECT,
        "FLUID_GCP_TEST_SA": GCP_LIVE_TEST_SA,
        "FLUID_GCP_REGION": GCP_LIVE_REGION,
        # The hashicorp/google provider needs the project + region via
        # env when no static provider config is emitted by the plugin.
        "GOOGLE_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_CLOUD_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_REGION": GCP_LIVE_REGION,
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": GCP_LIVE_TEST_SA,
    }


def _write_contract(workdir: Path, contract: Dict[str, Any]) -> Path:
    contract = {"fluidVersion": "0.7.3", "kind": "DataProduct", **contract}
    contract.setdefault("metadata", {"layer": "Silver", "owner": {"team": "data-eng"}})
    target = workdir / "contract.fluid.yaml"
    target.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return target


def _build_bq_contract(dataset_id: str, cid: str = "iac.gcp.cli.bq") -> Dict[str, Any]:
    """A minimal Stage-3 contract — single BigQuery dataset+table.
    Cheap (BQ free tier covers metadata), fast to apply + destroy."""
    return {
        "id": cid,
        "name": "CLI matrix BQ",
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "dataset": dataset_id,
                        "table": "events",
                        "region": GCP_LIVE_REGION,
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


# ---------------------------------------------------------------------------
# fluid validate
# ---------------------------------------------------------------------------


def test_real_cli_validate_gcp(gcp_real_project, gcp_account):
    """`fluid validate` returns 0 on a valid GCP contract."""
    dataset_id = gcp_real_project.name("clival").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))
    rc = _fluid(
        "validate",
        "contract.fluid.yaml",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=60,
    )
    assert rc.returncode == 0, (
        f"validate exited {rc.returncode}\n"
        f"stdout:\n{rc.stdout[-2000:]}\nstderr:\n{rc.stderr[-1000:]}"
    )


# ---------------------------------------------------------------------------
# fluid plan
# ---------------------------------------------------------------------------


def _bundle_then_plan(
    workdir: Path, *, bundle_name: str = "bundle.tgz", plan_name: str = "plan.json"
) -> tuple[Path, Path]:
    """``fluid bundle`` then ``fluid plan <bundle.tgz>`` — the canonical
    11-stage flow that produces both ``bundleDigest`` and ``planDigest``."""
    rc = _fluid(
        "bundle",
        "contract.fluid.yaml",
        "--out",
        bundle_name,
        "--format",
        "tgz",
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert rc.returncode == 0, f"bundle failed:\n{rc.stderr or rc.stdout}"
    bundle_path = workdir / bundle_name
    assert bundle_path.exists()

    rc = _fluid(
        "plan",
        bundle_name,
        "--out",
        plan_name,
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert rc.returncode == 0, f"plan failed:\n{rc.stderr or rc.stdout}"
    plan_path = workdir / plan_name
    assert plan_path.exists()
    return bundle_path, plan_path


def test_real_cli_plan_emits_signed_plan_json_gcp(gcp_real_project, gcp_account):
    """`fluid bundle + fluid plan` writes plan.json with both digests."""
    dataset_id = gcp_real_project.name("cliplan").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))
    _, plan_path = _bundle_then_plan(gcp_real_project.workdir)
    doc = json.loads(plan_path.read_text())
    assert "bundleDigest" in doc and "planDigest" in doc, list(doc.keys())
    assert doc["bundleDigest"].startswith("sha256:"), doc["bundleDigest"]
    assert doc["planDigest"].startswith("sha256:"), doc["planDigest"]


# ---------------------------------------------------------------------------
# fluid apply --mode dry-run
# ---------------------------------------------------------------------------


def test_real_cli_apply_dry_run_no_resources_gcp(gcp_real_project, gcp_account):
    """`fluid apply --mode dry-run` plans but does not apply; the BQ
    dataset must NOT exist afterwards."""
    dataset_id = gcp_real_project.name("clidry").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "dry-run",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    assert rc.returncode == 0, rc.stderr or rc.stdout

    bq = gcp_real_client("bigquery")
    from google.cloud.exceptions import NotFound

    try:
        bq.get_dataset(f"{GCP_LIVE_PROJECT}.{dataset_id}")
        pytest.fail(f"--mode dry-run created a real BQ dataset: {dataset_id}")
    except NotFound:
        pass  # expected


# ---------------------------------------------------------------------------
# fluid apply --mode amend
# ---------------------------------------------------------------------------


def test_real_cli_apply_amend_creates_resources_gcp(gcp_real_project, gcp_account):
    """`fluid apply --mode amend` provisions a real BQ dataset."""
    dataset_id = gcp_real_project.name("cliamend").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    gcp_real_project.applied = True
    assert rc.returncode == 0, rc.stderr or rc.stdout

    bq = gcp_real_client("bigquery")
    ds = bq.get_dataset(f"{GCP_LIVE_PROJECT}.{dataset_id}")
    assert ds.dataset_id == dataset_id


# ---------------------------------------------------------------------------
# Plan-binding tamper detection
# ---------------------------------------------------------------------------


def _tamper_plan_json(plan_path: Path) -> None:
    doc = json.loads(plan_path.read_text())
    actions = doc.setdefault("actions", [])
    actions.append({"op": "_fluid_test_injection", "tampered": True})
    plan_path.write_text(json.dumps(doc))


def test_real_cli_apply_plan_binding_tamper_rejected_gcp(gcp_real_project, gcp_account):
    """A tampered plan.json (from the bundle flow, so digests are
    populated) must be rejected — PlanBindingError surfaces as
    non-zero exit. BQ dataset must NOT be created."""
    dataset_id = gcp_real_project.name("clitamper").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))
    _, plan_path = _bundle_then_plan(gcp_real_project.workdir)
    _tamper_plan_json(plan_path)

    apply_rc = _fluid(
        "apply",
        "plan.json",
        "--mode",
        "amend",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert apply_rc.returncode != 0, (
        f"fluid-apply ACCEPTED a tampered plan.json\n" f"stdout:\n{apply_rc.stdout[-2000:]}"
    )
    combined = (apply_rc.stdout + apply_rc.stderr).lower()
    assert (
        "planbinding" in combined
        or "plan_tamper" in combined
        or "plan binding" in combined
        or "digest" in combined
    ), f"expected plan-binding keyword. last 2000:\n{combined[-2000:]}"

    bq = gcp_real_client("bigquery")
    from google.cloud.exceptions import NotFound

    try:
        bq.get_dataset(f"{GCP_LIVE_PROJECT}.{dataset_id}")
        pytest.fail(f"tampered apply created the dataset {dataset_id} — verify gate broken")
    except NotFound:
        pass


def test_real_cli_apply_no_verify_plan_binding_bypass_gcp(gcp_real_project, gcp_account):
    """``--no-verify-plan-binding`` bypasses the plan-binding gate — same
    tampered plan as the previous test applies cleanly with a WARNING."""
    dataset_id = gcp_real_project.name("clibypass").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))
    _, plan_path = _bundle_then_plan(gcp_real_project.workdir)
    _tamper_plan_json(plan_path)

    rc = _fluid(
        "apply",
        "plan.json",
        "--mode",
        "amend",
        "--no-verify-plan-binding",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    gcp_real_project.applied = True
    assert rc.returncode == 0, (
        f"--no-verify-plan-binding should let tampered plan apply.\n"
        f"stdout:\n{rc.stdout[-2000:]}"
    )

    bq = gcp_real_client("bigquery")
    ds = bq.get_dataset(f"{GCP_LIVE_PROJECT}.{dataset_id}")
    assert ds.dataset_id == dataset_id


# ---------------------------------------------------------------------------
# fluid verify — reading the deployed table back
# ---------------------------------------------------------------------------
# verify is the only stage that inspects the cloud *after* apply, so it is the
# one that proves a deployment did what its plan promised. Both directions are
# asserted on purpose: a verify that only ever exits 0 would pass against a
# table that was never created, which is the same silent-success class as an
# emitter returning a module with no resources.


def test_real_cli_verify_matches_deployed_schema_gcp(gcp_real_project, gcp_account):
    """`fluid verify --strict` returns 0 when the live BigQuery table
    matches the contract it was applied from."""
    dataset_id = gcp_real_project.name("cliverify").replace("-", "_")
    _write_contract(gcp_real_project.workdir, _build_bq_contract(dataset_id))

    apply_rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    gcp_real_project.applied = True
    assert apply_rc.returncode == 0, apply_rc.stderr or apply_rc.stdout

    rc = _fluid(
        "verify",
        "contract.fluid.yaml",
        "--strict",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    assert rc.returncode == 0, rc.stderr or rc.stdout


def test_real_cli_verify_detects_schema_drift_gcp(gcp_real_project, gcp_account):
    """`fluid verify --strict` must FAIL when the contract declares a column
    the deployed table does not have.

    Without this direction the passing case above proves nothing: it would
    also pass if verify inspected nothing at all.
    """
    dataset_id = gcp_real_project.name("clidrift").replace("-", "_")
    contract = _build_bq_contract(dataset_id)
    _write_contract(gcp_real_project.workdir, contract)

    apply_rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    gcp_real_project.applied = True
    assert apply_rc.returncode == 0, apply_rc.stderr or apply_rc.stdout

    # Declare a column that was never deployed, then re-verify.
    contract["exposes"][0]["contract"]["schema"].append(
        {"name": "column_that_was_never_deployed", "type": "string"}
    )
    _write_contract(gcp_real_project.workdir, contract)

    rc = _fluid(
        "verify",
        "contract.fluid.yaml",
        "--strict",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    assert rc.returncode != 0, (
        "verify --strict passed against a table missing a contracted column; "
        "stdout=%s stderr=%s" % (rc.stdout, rc.stderr)
    )
