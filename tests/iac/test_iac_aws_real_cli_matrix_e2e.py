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

"""Stage 3 — full ``fluid`` CLI matrix on real AWS.

The other Stage 3 files for AWS prove the *emitter + tofu apply* path
end-to-end (calling ``runner.tofu_apply`` from the harness, bypassing
``cli/apply.py``). This file proves the **CLI dispatch chain itself**:

  fluid validate → fluid plan → fluid apply (every applicable --mode)

for the OpenTofu apply path, against real AWS. Each test invokes the
``fluid`` CLI as a subprocess so we exercise:

  cli/validate.py → schema + alias normalization
  cli/plan.py → bundleDigest + planDigest emission
  cli/apply.py → _verify_plan_binding + resolve_apply_engine
    → _apply_opentofu_engine.apply_via_opentofu → runner.tofu_*

Six tests:

  * test_real_cli_validate — `fluid validate` returns 0 on a valid
    contract.
  * test_real_cli_plan_emits_signed_plan_json — `fluid plan` writes a
    plan.json with both bundleDigest and planDigest populated.
  * test_real_cli_apply_dry_run_no_resources — `fluid apply --mode
    dry-run` plans but does not apply (no S3 bucket lands in AWS).
  * test_real_cli_apply_amend_creates_resources — `fluid apply --mode
    amend` runs the IaC apply against real AWS; resources show up.
  * test_real_cli_apply_plan_binding_tamper_rejected — corrupting
    plan.json after fluid-plan makes fluid-apply raise PlanBindingError.
  * test_real_cli_apply_no_verify_plan_binding_bypass — same corrupt
    plan + ``--no-verify-plan-binding`` succeeds with a WARNING.

Triple-gated like the rest of Stage 3 (``tofu`` + FLUID_IAC_LIVE_AWS=1 +
the four IAM role-ARN env vars + a non-root principal). Per-test
resource isolation via the existing ``aws_real_project`` fixture.
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
    AWS_LIVE_ENABLED,
    AWS_LIVE_SKIP_REASON,
    aws_iceberg_contract,
    aws_real_boto,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.aws,
    pytest.mark.slow,
    pytest.mark.skipif(not AWS_LIVE_ENABLED, reason=AWS_LIVE_SKIP_REASON),
]


def _fluid(
    *args: str,
    cwd: Path,
    env_overrides: Optional[Mapping[str, str]] = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess:
    """Invoke the fluid CLI as subprocess (same shape as the AWS dbt-mesh suite)."""
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
    """Stage-3 gate env (FLUID_IAC_LIVE_AWS + IAM role ARNs)."""
    overrides: Dict[str, str] = {}
    for var in (
        "FLUID_IAC_LIVE_AWS",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "FLUID_AWS_LAMBDA_ROLE_ARN",
        "FLUID_AWS_SFN_ROLE_ARN",
        "FLUID_AWS_GLUE_ROLE_ARN",
        "FLUID_AWS_SPECTRUM_ROLE_ARN",
    ):
        v = os.environ.get(var)
        if v:
            overrides[var] = v
    return overrides


def _write_contract(workdir: Path, contract: Dict[str, Any]) -> Path:
    """Serialise a contract dict to ``contract.fluid.yaml`` under workdir."""
    contract = {"fluidVersion": "0.7.3", "kind": "DataProduct", **contract}
    contract.setdefault("metadata", {"layer": "Silver", "owner": {"team": "data-eng"}})
    target = workdir / "contract.fluid.yaml"
    target.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return target


def _build_s3_only_contract(bucket: str, cid: str = "iac.aws.cli.s3") -> Dict[str, Any]:
    """A minimal Stage-3 contract — single S3 bucket. Cheap to apply,
    fast to destroy, no IAM-role attachments. Ideal for CLI-matrix
    tests where the focus is the dispatch chain, not the resource."""
    return {
        "id": cid,
        "name": "CLI matrix S3",
        "exposes": [
            {
                "exposeId": "raw",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "bucket": bucket,
                        "path": "raw/",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


# ---------------------------------------------------------------------------
# fluid validate
# ---------------------------------------------------------------------------


def test_real_cli_validate(aws_real_project, aws_account):
    """`fluid validate` returns 0 on a valid AWS contract — proves schema
    validation + alias normalization runs through cleanly."""
    bucket = aws_real_project.name("clival")
    _write_contract(aws_real_project.workdir, _build_s3_only_contract(bucket))
    rc = _fluid(
        "validate",
        "contract.fluid.yaml",
        cwd=aws_real_project.workdir,
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
    """Run ``fluid bundle`` then ``fluid plan <bundle.tgz>`` — the
    canonical 11-stage flow that produces both ``bundleDigest`` and
    ``planDigest``. Returns ``(bundle_path, plan_path)``.

    Plan-binding is computed against the .tgz: planning from a raw
    .yaml leaves ``bundleDigest`` empty, which makes plan-binding
    verification a no-op at apply time. The bundle flow is what real
    pipelines use; the test matrix follows it.
    """
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
    assert rc.returncode == 0, f"fluid bundle failed:\n{rc.stderr or rc.stdout}"
    bundle_path = workdir / bundle_name
    assert bundle_path.exists(), f"bundle.tgz not written; ls: {list(workdir.iterdir())}"

    rc = _fluid(
        "plan",
        bundle_name,
        "--out",
        plan_name,
        cwd=workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert rc.returncode == 0, f"fluid plan failed:\n{rc.stderr or rc.stdout}"
    plan_path = workdir / plan_name
    assert plan_path.exists(), f"plan.json not written; ls: {list(workdir.iterdir())}"
    return bundle_path, plan_path


def test_real_cli_plan_emits_signed_plan_json(aws_real_project, aws_account):
    """`fluid bundle` + `fluid plan` writes a plan.json with
    bundleDigest + planDigest populated. Proves cli/bundle.py + cli/plan.py
    wire plan-binding correctly through the canonical 11-stage flow."""
    bucket = aws_real_project.name("cliplan")
    _write_contract(aws_real_project.workdir, _build_s3_only_contract(bucket))
    _, plan_path = _bundle_then_plan(aws_real_project.workdir)
    plan_doc = json.loads(plan_path.read_text())
    assert "bundleDigest" in plan_doc and "planDigest" in plan_doc, list(plan_doc.keys())
    assert plan_doc["bundleDigest"].startswith("sha256:"), plan_doc["bundleDigest"]
    assert plan_doc["planDigest"].startswith("sha256:"), plan_doc["planDigest"]


# ---------------------------------------------------------------------------
# fluid apply --mode dry-run — plans but doesn't apply
# ---------------------------------------------------------------------------


def test_real_cli_apply_dry_run_no_resources(aws_real_project, aws_account):
    """`fluid apply --mode dry-run` runs the OpenTofu plan step but does
    NOT apply. Verified by confirming the S3 bucket the contract would
    have created does NOT exist in AWS afterwards."""
    bucket = aws_real_project.name("clidry")
    _write_contract(aws_real_project.workdir, _build_s3_only_contract(bucket))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "dry-run",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    assert rc.returncode == 0, rc.stderr or rc.stdout

    s3 = aws_real_boto("s3")
    bucket_names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert bucket not in bucket_names, (
        f"--mode dry-run created a real bucket: {bucket!r}. "
        f"Existing fluid-iactest buckets: "
        f"{[n for n in bucket_names if n.startswith('fluid-iactest')]}"
    )


# ---------------------------------------------------------------------------
# fluid apply --mode amend — default, IaC-only, real apply
# ---------------------------------------------------------------------------


def test_real_cli_apply_amend_creates_resources(aws_real_project, aws_account):
    """`fluid apply --mode amend` (the IaC-only mode, the default for
    non-build pipelines) runs through the full CLI dispatch + applies
    to real AWS. Verified via S3 ListBuckets."""
    bucket = aws_real_project.name("cliamend")
    _write_contract(aws_real_project.workdir, _build_s3_only_contract(bucket))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    aws_real_project.applied = True  # so the fixture's destroy fires at teardown
    assert rc.returncode == 0, rc.stderr or rc.stdout

    s3 = aws_real_boto("s3")
    bucket_names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert bucket in bucket_names, f"bucket {bucket!r} not in S3 after amend apply"


# ---------------------------------------------------------------------------
# Plan-binding tamper detection
# ---------------------------------------------------------------------------


def _tamper_plan_json(plan_path: Path) -> None:
    """Corrupt a plan.json by mutating an action — changes the actions
    array's hash but leaves the planDigest pointing at the OLD content,
    so fluid-apply's verify step must catch the mismatch."""
    doc = json.loads(plan_path.read_text())
    # Inject a fake extra action so the actions array no longer matches
    # what planDigest was computed over.
    actions = doc.setdefault("actions", [])
    actions.append({"op": "_fluid_test_injection", "tampered": True})
    plan_path.write_text(json.dumps(doc))


def test_real_cli_apply_plan_binding_tamper_rejected(aws_real_project, aws_account):
    """A plan.json tampered with after `fluid bundle + fluid plan` must
    be REJECTED by `fluid apply` — PlanBindingError surfaces as a
    non-zero exit. No resources should be created. Uses the canonical
    bundle flow so both bundleDigest and planDigest are populated."""
    bucket = aws_real_project.name("clitamper")
    _write_contract(aws_real_project.workdir, _build_s3_only_contract(bucket))
    _, plan_path = _bundle_then_plan(aws_real_project.workdir)
    _tamper_plan_json(plan_path)

    apply_rc = _fluid(
        "apply",
        "plan.json",
        "--mode",
        "amend",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=180,
    )
    assert apply_rc.returncode != 0, (
        "fluid-apply ACCEPTED a tampered plan.json — plan-binding broken!\n"
        f"stdout:\n{apply_rc.stdout[-2000:]}"
    )
    combined = (apply_rc.stdout + apply_rc.stderr).lower()
    assert (
        "planbinding" in combined
        or "plan_tamper" in combined
        or "plan binding" in combined
        or "digest" in combined
    ), f"expected plan-binding error keyword. last 2000 chars:\n{combined[-2000:]}"

    s3 = aws_real_boto("s3")
    bucket_names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert (
        bucket not in bucket_names
    ), "tampered apply created the bucket — verify gate not blocking"


def test_real_cli_apply_no_verify_plan_binding_bypass(aws_real_project, aws_account):
    """``--no-verify-plan-binding`` is the documented DR escape hatch:
    it lets fluid-apply proceed against a corrupt-digest plan, logging
    at WARNING level. Verified by applying a tampered plan with the
    bypass flag — the resource lands."""
    bucket = aws_real_project.name("clibypass")
    _write_contract(aws_real_project.workdir, _build_s3_only_contract(bucket))
    _, plan_path = _bundle_then_plan(aws_real_project.workdir)
    _tamper_plan_json(plan_path)

    rc = _fluid(
        "apply",
        "plan.json",
        "--mode",
        "amend",
        "--no-verify-plan-binding",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=600,
    )
    aws_real_project.applied = True
    assert rc.returncode == 0, (
        f"--no-verify-plan-binding should let a tampered plan apply.\n"
        f"stdout:\n{rc.stdout[-2000:]}\nstderr:\n{rc.stderr[-1000:]}"
    )

    s3 = aws_real_boto("s3")
    bucket_names = {b["Name"] for b in s3.list_buckets()["Buckets"]}
    assert bucket in bucket_names, "bypass apply should have created the bucket"
