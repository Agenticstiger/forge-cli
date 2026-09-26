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
    assert first == {"s3": {"bucket": "ci-state", "key": "fluid/demo.state/terraform.tfstate"}}
    assert second == {
        "s3": {"bucket": "ci-state", "key": "fluid/demo.other_state/terraform.tfstate"}
    }


@pytest.mark.parametrize("spec", ["gcs://ci-state", "gcs://ci-state/"])
def test_a_bucket_only_env_value_gives_each_contract_its_own_gcs_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: str
) -> None:
    monkeypatch.setenv("FLUID_STATE_BACKEND", spec)
    backend, _ = _apply_and_read_backend(tmp_path, monkeypatch, flag=None)
    assert backend == {"gcs": {"bucket": "ci-state", "prefix": "fluid/demo.state"}}


def test_the_flag_keeps_the_shared_legacy_key_for_a_contract_without_packaging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing ``--state-backend s3://bucket`` users have state at the legacy
    key; moving it would re-plan every resource as new."""
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    backend, _ = _apply_and_read_backend(tmp_path, monkeypatch, flag="s3://ci-state")
    assert backend == {"s3": {"bucket": "ci-state", "key": "fluid/terraform.tfstate"}}


def test_ids_the_old_key_folded_together_get_their_own_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``demo.state`` and ``demo_state`` are both schema-valid; ``safe_ident``
    gave both ``fluid/demo_state/``, so one job applying both shared a state."""
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://ci-state")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    dotted, _ = _apply_and_read_backend(tmp_path / "a", monkeypatch, flag=None)
    underscored, _ = _apply_and_read_backend(
        tmp_path / "b",
        monkeypatch,
        flag=None,
        contract_text=_CONTRACT.replace("id: demo.state", "id: demo_state"),
    )
    assert dotted["s3"]["key"] == "fluid/demo.state/terraform.tfstate"
    assert underscored["s3"]["key"] == "fluid/demo_state/terraform.tfstate"


def test_an_id_that_cannot_key_a_state_is_a_typed_error_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(_CONTRACT.replace("id: demo.state", "id: 'demo state'"), encoding="utf-8")
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://ci-state")
    monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(engine.runner, "require_tofu_version", lambda *a, **k: None)
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
    assert "cannot name its own state" in exc.value.context["error"]


# ── The apply output names the state object it used ─────────────────────
#
# The same bucket-only value keys state one way as --state-backend and
# another as FLUID_STATE_BACKEND. Measured before: both printed only
# ``remote: s3 (from ...)``, so a pipeline that moved from one form to the
# other landed on an empty state, re-planned every resource as new, and
# nothing in its output said why.


def test_the_state_line_names_the_object_each_form_resolved_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.delenv("FLUID_STATE_BACKEND", raising=False)
    _, flag_lines = _apply_and_read_backend(tmp_path / "a", monkeypatch, flag="s3://ci-state")
    monkeypatch.setenv("FLUID_STATE_BACKEND", "s3://ci-state")
    _, env_lines = _apply_and_read_backend(tmp_path / "b", monkeypatch, flag=None)
    assert flag_lines == [
        "  state:       remote: s3://ci-state/fluid/terraform.tfstate (from --state-backend)"
    ]
    assert env_lines == [
        "  state:       remote: s3://ci-state/fluid/demo.state/terraform.tfstate"
        " (from FLUID_STATE_BACKEND)"
    ]


@pytest.mark.parametrize("value", ["s3://ci-state", "gcs://ci-state", "gcs://ci-state/team/x"])
def test_the_printed_location_given_as_the_flag_selects_the_same_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    monkeypatch.setenv("FLUID_STATE_BACKEND", value)
    via_env, env_lines = _apply_and_read_backend(tmp_path / "a", monkeypatch, flag=None)
    printed = env_lines[0].split("remote: ", 1)[1].split(" (from ", 1)[0]
    monkeypatch.delenv("FLUID_STATE_BACKEND")
    via_flag, _ = _apply_and_read_backend(tmp_path / "b", monkeypatch, flag=printed)
    assert via_flag == via_env


def test_a_bucket_that_could_hide_a_credential_is_refused_unechoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state line now prints the bucket, so a ``key:secret@`` in the spec
    must never get that far, nor into the error."""
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(_CONTRACT, encoding="utf-8")
    spec = "s3://AKIAEXAMPLE:NotARealSecret@ci-state"  # pragma: allowlist secret
    monkeypatch.setenv("FLUID_STATE_BACKEND", spec)
    monkeypatch.setattr(engine.runner, "tofu_path", lambda: "/usr/bin/tofu")
    monkeypatch.setattr(engine.runner, "require_tofu_version", lambda *a, **k: None)
    printed: list = []
    monkeypatch.setattr(engine, "cprint", lambda *a, **k: printed.append(" ".join(map(str, a))))
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
    assert "NotARealSecret" not in json.dumps(exc.value.context)
    assert not any("NotARealSecret" in line for line in printed)


def test_the_flag_help_says_how_the_two_forms_key_a_bucket() -> None:
    from fluid_build.cli import apply as apply_cli

    subparsers = argparse.ArgumentParser().add_subparsers()
    apply_cli.register(subparsers)
    sub = subparsers.choices["apply"]
    action = next(a for a in sub._actions if "--state-backend" in a.option_strings)
    help_text = " ".join((action.help or "").split())
    assert "$FLUID_STATE_BACKEND" in help_text
    assert "$FLUID_STATE_BACKEND (bucket only: keys per contract, fluid/<id>/" in help_text
    assert "unlike the flag" in help_text


def test_apply_help_still_fits_its_cap_with_debug_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tests/cli/conftest.py`` sets FLUID_LOG_LEVEL=DEBUG, which prints four
    registration lines before the help; a worker that inherits it measures
    ``fluid apply --help`` four lines longer. The longer --state-backend help
    must not push it over the cap there."""
    from tests.test_cli_help_style import _HELP_LINE_CAP, _help_output

    monkeypatch.setenv("FLUID_LOG_LEVEL", "DEBUG")
    assert len(_help_output("apply").splitlines()) <= _HELP_LINE_CAP


# ── The generated pipelines leave the state backend to the variable ──────
#
# A pipeline that passed ``--state-backend "$FLUID_STATE_BACKEND"`` would
# silently put every product back on the flag's one shared key.


def _generated_pipeline_files() -> Dict[str, str]:
    from fluid_build.forge.core.pipeline_templates import (
        PipelineComplexity,
        PipelineConfig,
        PipelineProvider,
        PipelineTemplateGenerator,
    )

    generator = PipelineTemplateGenerator()
    files: Dict[str, str] = {}
    for provider in PipelineProvider:
        for complexity in PipelineComplexity:
            for install_mode in ("pypi", "dev-source"):
                for oidc in (None, "aws", "gcp", "azure"):
                    config = PipelineConfig(
                        provider=provider,
                        complexity=complexity,
                        install_mode=install_mode,
                        oidc_provider=oidc,
                        enable_marketplace_publishing=True,
                    )
                    for name, text in generator.generate_pipeline(config).items():
                        label = f"{provider.value}/{complexity.value}/{install_mode}/{oidc}/{name}"
                        files[label] = text
    return files


def test_no_generated_pipeline_passes_the_state_backend_flag() -> None:
    files = _generated_pipeline_files()
    assert len(files) >= 7 * 4 * 2 * 4
    assert any("fluid apply" in text for text in files.values())
    offenders = sorted(label for label, text in files.items() if "state-backend" in text)
    assert offenders == []


def test_no_scheduled_dag_passes_the_state_backend_flag() -> None:
    import yaml

    from fluid_build.schedulers.airflow import fluid_apply
    from tests.cli._schedule_dag_fixtures import DEMO_CONTRACT, DEMO_CONTRACT_PATH

    dags = fluid_apply.render_fluid_apply_dags(
        yaml.safe_load(DEMO_CONTRACT), env="aws", contract_path=DEMO_CONTRACT_PATH
    )
    assert dags
    assert not any("state-backend" in source for source in dags.values())
    assert not any("state-backend" in line for line in fluid_apply.BASH_SCRIPT_LINES)
