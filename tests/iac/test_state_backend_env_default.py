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

"""``fluid apply --state-backend`` defaults from ``FLUID_STATE_BACKEND``.

A CI job cannot keep OpenTofu state in the workspace: the workspace is
wiped after every run, so the next run re-plans every resource as new. The
flag had no environment default, so a pipeline had to thread it through
every apply invocation or lose its state. These tests drive the real
``apply_via_opentofu`` up to ``tofu init`` (stubbed, no binary and no
cloud call) and read the backend block it wrote into the module.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError

pytestmark = [pytest.mark.unit]

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.state
name: State Demo
description: State backend default.
domain: Demo
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: aws
      format: parquet
      location:
        bucket: demo-lake
        database: demo_bronze
        table: rows
        path: bronze/rows/
        region: eu-north-1
    contract:
      schema:
        - {name: id, type: VARCHAR, required: true}
"""


def _apply_and_read_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    flag: Optional[str],
    contract_text: str = _CONTRACT,
) -> tuple:
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(contract_text, encoding="utf-8")
    monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(engine.runner, "require_tofu_version", lambda *a, **k: None)
    printed: list = []
    monkeypatch.setattr(engine, "cprint", lambda *a, **k: printed.append(" ".join(map(str, a))))

    def _init_stops_here(*_a: Any, **_k: Any) -> SimpleNamespace:
        return SimpleNamespace(ok=False, stderr="stub: tofu init not run", stdout="")

    monkeypatch.setattr(engine.runner, "tofu_init", _init_stops_here)
    args = argparse.Namespace(
        contract=str(contract),
        env=None,
        provider=None,
        workspace_dir=tmp_path,
        state_backend=flag,
        dry_run=True,
        allow_data_loss=False,
        no_verify_plan_binding=False,
    )
    with pytest.raises(CLIError) as exc:
        engine.apply_via_opentofu(args, logging.getLogger("test.state_backend"))
    assert exc.value.event == "opentofu_init_failed"
    (module,) = list((tmp_path / ".fluid" / "iac").rglob("main.tf.json"))
    doc: Dict[str, Any] = json.loads(module.read_text(encoding="utf-8"))
    state_lines = [line for line in printed if "state:" in line]
    return doc.get("terraform", {}).get("backend"), state_lines


def test_env_var_supplies_the_backend_when_the_flag_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://ci-state/fluid/demo.tfstate")
    backend, state_lines = _apply_and_read_backend(tmp_path, monkeypatch, flag=None)
    assert backend == {"s3": {"bucket": "ci-state", "key": "fluid/demo.tfstate"}}
    assert state_lines and "FLUID_STATE_BACKEND" in state_lines[0]


def test_flag_wins_over_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://from-env/key")
    backend, _ = _apply_and_read_backend(tmp_path, monkeypatch, flag="gcs://from-flag/prefix")
    assert backend == {"gcs": {"bucket": "from-flag", "prefix": "prefix"}}


def test_empty_flag_forces_local_state_over_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://from-env/key")
    backend, _ = _apply_and_read_backend(tmp_path, monkeypatch, flag="")
    assert backend is None


def test_neither_flag_nor_env_is_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    backend, state_lines = _apply_and_read_backend(tmp_path, monkeypatch, flag=None)
    assert backend is None
    assert state_lines and state_lines[0].strip().endswith("local")


def test_an_unusable_env_value_is_a_typed_error_naming_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(_CONTRACT, encoding="utf-8")
    monkeypatch.setenv("FLUID_STATE_BACKEND", "ftp://nowhere/state")
    monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(engine.runner, "require_tofu_version", lambda *a, **k: None)
    monkeypatch.setattr(
        engine.runner,
        "tofu_init",
        lambda *a, **k: SimpleNamespace(ok=False, stderr="stub: tofu init not run", stdout=""),
    )
    args = argparse.Namespace(
        contract=str(contract),
        env=None,
        provider=None,
        workspace_dir=tmp_path,
        state_backend=None,
        dry_run=True,
        allow_data_loss=False,
        no_verify_plan_binding=False,
    )
    with pytest.raises(CLIError) as exc:
        engine.apply_via_opentofu(args, logging.getLogger("test.state_backend"))
    assert exc.value.event == "apply_state_backend_invalid"
    assert exc.value.context["source"] == "FLUID_STATE_BACKEND"


# ── A bucket-only FLUID_STATE_BACKEND keys state per contract ────────────
#
# One CI job sets the variable once and applies every product with it. The
# demo's contracts carry no ``packaging`` block, so the flag's default (the
# shared legacy key) would give them all one OpenTofu state, and each apply
# would plan to destroy the resources the others had created.

#: A second product, applied by the same job; no ``packaging`` block either.
_OTHER_CONTRACT = _CONTRACT.replace("id: demo.state", "id: demo.other_state").replace(
    "table: rows", "table: other_rows"
)


def test_a_bucket_only_env_value_gives_each_contract_its_own_s3_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://ci-state")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first, _ = _apply_and_read_backend(tmp_path / "a", monkeypatch, flag=None)
    second, _ = _apply_and_read_backend(
        tmp_path / "b", monkeypatch, flag=None, contract_text=_OTHER_CONTRACT
    )
    assert first == {"s3": {"bucket": "ci-state", "key": "fluid/demo_state/terraform.tfstate"}}
    assert second == {
        "s3": {"bucket": "ci-state", "key": "fluid/demo_other_state/terraform.tfstate"}
    }


@pytest.mark.parametrize("spec", ["gcs://ci-state", "gcs://ci-state/"])
def test_a_bucket_only_env_value_gives_each_contract_its_own_gcs_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: str
) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", spec)
    backend, _ = _apply_and_read_backend(tmp_path, monkeypatch, flag=None)
    assert backend == {"gcs": {"bucket": "ci-state", "prefix": "fluid/demo_state"}}


def test_the_flag_keeps_the_shared_legacy_key_for_a_contract_without_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing ``--state-backend s3://bucket`` users have state at the legacy
    key; moving it would re-plan every resource as new."""
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    backend, _ = _apply_and_read_backend(tmp_path, monkeypatch, flag="s3://ci-state")
    assert backend == {"s3": {"bucket": "ci-state", "key": "fluid/terraform.tfstate"}}
