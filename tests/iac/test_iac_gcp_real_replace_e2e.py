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

"""Stage 3 — destructive apply modes + data-loss gate on real GCP.

GCP analogue of ``test_iac_aws_real_replace_e2e.py``. The OpenTofu
engine's ``_data_loss_blocked`` fires whenever ``tofu plan`` proposes
any destroys AND ``--allow-data-loss`` is unset — works identically
for both clouds since the gate is provider-agnostic.

Two tests:

  * ``test_real_gcp_destructive_apply_blocked_without_allow_flag`` —
    destructive change without the flag must be blocked.
  * ``test_real_gcp_destructive_apply_succeeds_with_allow_flag`` —
    same change with the flag completes cleanly.
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
    return {
        "FLUID_IAC_LIVE_GCP": "1",
        "FLUID_GCP_PROJECT": GCP_LIVE_PROJECT,
        "FLUID_GCP_TEST_SA": GCP_LIVE_TEST_SA,
        "FLUID_GCP_REGION": GCP_LIVE_REGION,
        "GOOGLE_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_CLOUD_PROJECT": GCP_LIVE_PROJECT,
        "GOOGLE_REGION": GCP_LIVE_REGION,
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT": GCP_LIVE_TEST_SA,
    }


def _two_dataset_contract(ds_a: str, ds_b: str, cid: str) -> Dict[str, Any]:
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
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": ds_a, "table": "events", "region": GCP_LIVE_REGION},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            },
            {
                "exposeId": "b",
                "kind": "table",
                "binding": {
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": ds_b, "table": "events", "region": GCP_LIVE_REGION},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            },
        ],
    }


def _one_dataset_contract(ds_a: str, cid: str) -> Dict[str, Any]:
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
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {"dataset": ds_a, "table": "events", "region": GCP_LIVE_REGION},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _write_contract(workdir: Path, contract: Dict[str, Any]) -> None:
    (workdir / "contract.fluid.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=False), encoding="utf-8"
    )


def test_real_gcp_destructive_apply_blocked_without_allow_flag(gcp_real_project, gcp_account):
    """Apply 2 datasets, then re-apply with B removed WITHOUT
    ``--allow-data-loss``. Gate blocks (non-zero exit); B still exists."""
    cid = "iac.gcp.real.replace.block"
    ds_a = gcp_real_project.name("repa").replace("-", "_")
    ds_b = gcp_real_project.name("repb").replace("-", "_")

    _write_contract(gcp_real_project.workdir, _two_dataset_contract(ds_a, ds_b, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    gcp_real_project.applied = True
    assert rc.returncode == 0, rc.stderr or rc.stdout

    # Phase 2 — drop ds_b, no allow flag.
    _write_contract(gcp_real_project.workdir, _one_dataset_contract(ds_a, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "replace",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=120,
    )
    assert rc.returncode != 0, (
        f"data-loss gate did not fire for GCP destructive apply.\n" f"stdout:\n{rc.stdout[-2000:]}"
    )
    combined = (rc.stdout + rc.stderr).lower()
    assert (
        "data_loss" in combined or "data loss" in combined or "destroy" in combined
    ), f"expected data-loss keyword. last 2000:\n{combined[-2000:]}"

    # Dataset B must still exist.
    bq = gcp_real_client("bigquery")
    ds = bq.get_dataset(f"{GCP_LIVE_PROJECT}.{ds_b}")
    assert ds.dataset_id == ds_b, "dataset B was destroyed despite gate"


def test_real_gcp_destructive_apply_succeeds_with_allow_flag(gcp_real_project, gcp_account):
    """Same destructive change with ``--allow-data-loss``: ds_b is gone."""
    cid = "iac.gcp.real.replace.allow"
    ds_a = gcp_real_project.name("ralwa").replace("-", "_")
    ds_b = gcp_real_project.name("ralwb").replace("-", "_")

    _write_contract(gcp_real_project.workdir, _two_dataset_contract(ds_a, ds_b, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "amend",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    gcp_real_project.applied = True
    assert rc.returncode == 0, rc.stderr or rc.stdout

    _write_contract(gcp_real_project.workdir, _one_dataset_contract(ds_a, cid))
    rc = _fluid(
        "apply",
        "contract.fluid.yaml",
        "--mode",
        "replace",
        "--allow-data-loss",
        "--yes",
        cwd=gcp_real_project.workdir,
        env_overrides=_live_env(),
        timeout=300,
    )
    assert rc.returncode == 0, (
        f"--allow-data-loss should let destructive apply through.\n"
        f"stdout:\n{rc.stdout[-2000:]}\nstderr:\n{rc.stderr[-1000:]}"
    )

    bq = gcp_real_client("bigquery")
    bq.get_dataset(f"{GCP_LIVE_PROJECT}.{ds_a}")  # A still exists
    from google.cloud.exceptions import NotFound

    try:
        bq.get_dataset(f"{GCP_LIVE_PROJECT}.{ds_b}")
        pytest.fail(f"dataset B {ds_b} should have been destroyed with --allow-data-loss")
    except NotFound:
        pass
