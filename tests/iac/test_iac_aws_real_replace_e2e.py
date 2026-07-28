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

"""Stage 3 — destructive apply modes + data-loss gate on real AWS.

The OpenTofu apply engine fires its data-loss gate
(``_data_loss_blocked`` in ``cli/_apply_opentofu_engine.py``) whenever
``tofu plan`` proposes any resource destruction AND
``--allow-data-loss`` is not set. The gate runs BEFORE any tofu
apply, so blocked destructive changes never touch infrastructure.

Two tests:

  * ``test_real_aws_destructive_apply_blocked_without_allow_flag`` —
    apply a contract, then re-apply with the resource REMOVED from
    the contract WITHOUT ``--allow-data-loss``. Expect non-zero exit
    + ``opentofu_data_loss_gate`` event; the original resource is
    NOT destroyed.
  * ``test_real_aws_destructive_apply_succeeds_with_allow_flag`` —
    same destructive change WITH ``--allow-data-loss``. Expect clean
    destroy + the resource is gone from AWS.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import pytest
import yaml

from .conftest import (
    AWS_LIVE_ENABLED,
    AWS_LIVE_SKIP_REASON,
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
    return {
        v: os.environ[v]
        for v in (
            "FLUID_IAC_LIVE_AWS",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "FLUID_AWS_LAMBDA_ROLE_ARN",
            "FLUID_AWS_SFN_ROLE_ARN",
            "FLUID_AWS_GLUE_ROLE_ARN",
            "FLUID_AWS_SPECTRUM_ROLE_ARN",
        )
        if v in os.environ
    }


def _two_bucket_contract(bucket_a: str, bucket_b: str, cid: str) -> Dict[str, Any]:
    """Two S3 exposures; removing one triggers a destructive plan."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cid,
        "name": "Replace test",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng"}},
        "exposes": [
            {
                "exposeId": "a",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": bucket_a, "path": "a/"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            },
            {
                "exposeId": "b",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": bucket_b, "path": "b/"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            },
        ],
    }


def _one_bucket_contract(bucket_a: str, cid: str) -> Dict[str, Any]:
    """Only the first exposure — removing 'b' from the prior contract.
    Re-applying this against a workdir whose state has BOTH buckets
    will plan a destroy of bucket B."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": cid,
        "name": "Replace test",
        "metadata": {"layer": "Silver", "owner": {"team": "data-eng"}},
        "exposes": [
            {
                "exposeId": "a",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {"bucket": bucket_a, "path": "a/"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _write_contract(workdir: Path, contract: Dict[str, Any]) -> None:
    (workdir / "contract.fluid.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
    )


def test_real_aws_destructive_apply_blocked_without_allow_flag(aws_real_project, aws_account):
    """Apply 2 buckets, then re-apply with bucket B removed WITHOUT
    ``--allow-data-loss``. Gate must block (non-zero exit) and bucket B
    must STILL exist in AWS."""
    cid = "iac.aws.real.replace.block"
    bucket_a = aws_real_project.name("repla")
    bucket_b = aws_real_project.name("replb")

    # Phase 1 — provision both buckets.
    _write_contract(aws_real_project.workdir, _two_bucket_contract(bucket_a, bucket_b, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    aws_real_project.applied = True
    assert rc.returncode == 0, rc.stderr or rc.stdout
    names = {b["Name"] for b in aws_real_boto("s3").list_buckets()["Buckets"]}
    assert (
        bucket_a in names and bucket_b in names
    ), f"phase-1 apply didn't create both buckets — got {names & {bucket_a, bucket_b}}"

    # Phase 2 — rewrite contract to drop bucket B, re-apply WITHOUT
    # ``--allow-data-loss``. The gate must block.
    _write_contract(aws_real_project.workdir, _one_bucket_contract(bucket_a, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "replace",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=120,
    )
    assert rc.returncode != 0, (
        f"data-loss gate did NOT fire — destructive apply succeeded without "
        f"--allow-data-loss. exit={rc.returncode}\nstdout:\n{rc.stdout[-2000:]}"
    )
    combined = (rc.stdout + rc.stderr).lower()
    assert (
        "data_loss" in combined or "data loss" in combined or "destroy" in combined
    ), f"expected data-loss-related error. last 2000:\n{combined[-2000:]}"

    # Bucket B must still exist — gate blocked BEFORE the destroy.
    names = {b["Name"] for b in aws_real_boto("s3").list_buckets()["Buckets"]}
    assert bucket_b in names, (
        f"bucket B was destroyed despite gate. extant fluid buckets: "
        f"{sorted(n for n in names if 'fluid-iactest' in n)[:10]}"
    )


def test_real_aws_destructive_apply_succeeds_with_allow_flag(aws_real_project, aws_account):
    """Same destructive change WITH ``--allow-data-loss``: bucket B is
    cleanly destroyed and removed from AWS."""
    cid = "iac.aws.real.replace.allow"
    bucket_a = aws_real_project.name("rallowa")
    bucket_b = aws_real_project.name("rallowb")

    _write_contract(aws_real_project.workdir, _two_bucket_contract(bucket_a, bucket_b, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    aws_real_project.applied = True
    assert rc.returncode == 0, rc.stderr or rc.stdout

    _write_contract(aws_real_project.workdir, _one_bucket_contract(bucket_a, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "replace",
        "--allow-data-loss",
        "--yes",
        cwd=aws_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    assert rc.returncode == 0, (
        f"--allow-data-loss should let the destructive apply through.\n"
        f"stdout:\n{rc.stdout[-2000:]}\nstderr:\n{rc.stderr[-1000:]}"
    )

    names = {b["Name"] for b in aws_real_boto("s3").list_buckets()["Buckets"]}
    assert bucket_a in names, "bucket A unexpectedly destroyed too"
    assert bucket_b not in names, (
        f"bucket B was NOT destroyed despite --allow-data-loss. extant: "
        f"{sorted(n for n in names if 'fluid-iactest' in n)[:10]}"
    )
