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

"""A planned AWS op the OpenTofu emitter does not translate is never dropped without a word.

``orchestration.engine: airflow`` (or ``mwaa``) with a schedule made the AWS
planner promise an ``mwaa.ensure_environment`` in ``plan.json``, and the
emitter's ``_emit_from_actions`` had no branch for it: ``main.tf.json`` held no
``aws_mwaa_environment``, ``fluid apply`` reported success, and no Airflow
environment existed. OpenTofu is the only AWS apply engine, so nothing else
created it either.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli import generate_iac
from fluid_build.cli._common import CLIError
from fluid_build.iac import build_module, get_iac_plugin, shadow_compare
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.shadow import native_logical_resources
from fluid_build.providers.aws.provider import AwsProvider
from tests.iac.test_iac_generate_offline import _args, _write, no_aws  # noqa: F401 (fixture)
from tests.iac.test_iac_generate_offline import _orders as _contract

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCOUNT = "123456789012"
_AWS_LOGGER = "fluid_build.iac.providers.aws"


def _aws():
    return get_iac_plugin("aws")


def _planned(contract: Dict[str, Any]) -> list:
    """The real AWS planner's actions — the same call ``fluid apply`` makes."""
    provider = AwsProvider(account_id=ACCOUNT, region="us-east-1", logger=logging.getLogger("t"))
    return list(provider.plan(contract))


class TestMwaaEnvironmentIsRefused:
    def test_the_op_is_refused_with_its_environment_and_a_way_out(self):
        with pytest.raises(UnsupportedBindingError) as excinfo:
            _aws().emit(
                _contract(),
                [{"op": "mwaa.ensure_environment", "environment_name": "mwaa-orders"}],
            )
        err = excinfo.value
        assert err.kind == "mwaa-environment-not-emitted"
        assert "mwaa-orders" in str(err)
        assert "network_configuration" in str(err)
        assert err.remediation, "the CLI renders these as next steps"
        assert any("schedule-sync" in step for step in err.remediation)

    @pytest.mark.parametrize("engine", ["airflow", "mwaa"])
    def test_the_planned_path_fails_before_a_module_is_built(self, engine):
        contract = _contract(orchestration={"engine": engine, "schedule": "0 * * * *"})
        actions = _planned(contract)
        # The plan promises the environment: provider.py calls _plan_schedule
        # unconditionally, so this is the path `fluid apply` takes.
        assert any(a.get("op") == "mwaa.ensure_environment" for a in actions)
        with pytest.raises(UnsupportedBindingError) as excinfo:
            build_module(_aws(), contract, actions=actions)
        assert excinfo.value.kind == "mwaa-environment-not-emitted"
        assert "mwaa-offline_orders" in str(excinfo.value)

    def test_a_named_existing_environment_is_still_refused(self):
        # orchestration.mwaa.environmentName names the environment, but the
        # planned op still asks for it to exist — OpenTofu cannot ensure that.
        contract = _contract(
            orchestration={
                "engine": "airflow",
                "schedule": "0 * * * *",
                "mwaa": {"environmentName": "shared-airflow"},
            }
        )
        with pytest.raises(UnsupportedBindingError) as excinfo:
            build_module(_aws(), contract, actions=_planned(contract))
        assert "shared-airflow" in str(excinfo.value)

    def test_a_schedule_without_airflow_still_emits(self):
        contract = _contract(orchestration={"schedule": "rate(1 hour)"})
        actions = _planned(contract)
        assert not any(a.get("op") == "mwaa.ensure_environment" for a in actions)
        module = json.loads(build_module(_aws(), contract, actions=actions))
        assert "aws_scheduler_schedule" in module["resource"]

    def test_no_orchestration_is_unaffected(self):
        contract = _contract()
        module = json.loads(build_module(_aws(), contract, actions=_planned(contract)))
        assert set(module["resource"]) == {
            "aws_glue_catalog_database",
            "aws_glue_catalog_table",
            "aws_s3_bucket",
        }


class TestGenerateIac:
    @pytest.fixture(autouse=True)
    def _account(self, no_aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
        # After no_aws, which clears it: the EventBridge path names the account.
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)

    def _run(self, tmp_path: Path, contract: Dict[str, Any]) -> Path:
        out = tmp_path / "out"
        generate_iac.run(_args(_write(tmp_path, contract), out), logging.getLogger("t"))
        return out

    def test_airflow_with_a_schedule_is_a_typed_cli_error(self, tmp_path):
        contract = _contract(orchestration={"engine": "airflow", "schedule": "0 * * * *"})
        with pytest.raises(CLIError) as excinfo:
            self._run(tmp_path, contract)
        error = excinfo.value
        assert (error.event, error.context["kind"]) == (
            "unsupported_binding",
            "mwaa-environment-not-emitted",
        )
        assert any("schedule-sync" in step for step in error.context["remediation"])
        # Refused before the output directory is even created.
        assert not (tmp_path / "out").exists()

    def test_a_schedule_without_airflow_writes_the_module(self, tmp_path):
        out = self._run(tmp_path, _contract(orchestration={"schedule": "rate(1 hour)"}))
        module = json.loads((out / "main.tf.json").read_text(encoding="utf-8"))
        assert "aws_scheduler_schedule" in module["resource"]


class TestEveryOtherOp:
    # A non-string op must be the typed refusal too, not a TypeError from hashing it.
    @pytest.mark.parametrize(
        ("action", "named"),
        [
            ({"op": "glue.ensure_crawler", "name": "c"}, "'glue.ensure_crawler'"),
            ({"id": "x"}, "None (action 'x')"),
            ({"id": "x", "op": ["glue", "ensure_job"]}, "['glue', 'ensure_job']"),
        ],
    )
    def test_an_op_with_no_translation_is_refused_by_name(self, action, named):
        with pytest.raises(UnsupportedBindingError) as excinfo:
            _aws().emit(_contract(), [action])
        err = excinfo.value
        assert (err.kind, named in str(err)) == ("planned-op-not-emitted", True)
        assert err.remediation

    @pytest.mark.parametrize(
        "op",
        [
            # The data plane: emit() derives these from exposes[].
            "s3.ensure_bucket",
            "glue.ensure_database",
            "glue.ensure_table",
            "glue.ensure_iceberg_table",
            # Build steps the build runners execute, never provisioned.
            "dbt.prepare_profile",
            "athena.execute_query",
        ],
    )
    def test_an_op_provisioned_or_run_elsewhere_passes_quietly(self, op, caplog):
        caplog.set_level(logging.WARNING, logger=_AWS_LOGGER)
        _aws().emit(_contract(), [{"op": op, "id": "a"}])
        assert not [r for r in caplog.records if op in r.getMessage()]

    @pytest.mark.parametrize(
        "op",
        [
            "iam.bind_glue_database",
            "iam.bind_s3_bucket",
            "athena.create_view",
            "redshift.ensure_schema",
            "redshift.ensure_table",
            "events.ensure_schedule",
        ],
    )
    def test_a_known_op_with_no_emitter_is_named_in_a_warning(self, op, caplog):
        caplog.set_level(logging.WARNING, logger=_AWS_LOGGER)
        _aws().emit(_contract(), [{"op": op, "id": "a"}])
        (record,) = [r for r in caplog.records if op in r.getMessage()]
        assert record.levelno == logging.WARNING
        assert "aws_planned_op_not_emitted" in record.getMessage()

    # A task's ``s3.ensure_bucket`` names a bucket exposes[] may not declare.
    @pytest.mark.parametrize("op", ["glue.run_job", "s3.ensure_bucket"])
    def test_an_orchestration_task_is_named_in_a_warning(self, op, caplog):
        caplog.set_level(logging.WARNING, logger=_AWS_LOGGER)
        task = {
            "id": "task_1",
            "op": op,
            "orchestration": {"task_id": "task_1", "depends_on": [], "engine": "airflow"},
        }
        _aws().emit(_contract(), [task])
        (record,) = [r for r in caplog.records if op in r.getMessage()]
        assert "aws_orchestration_task_not_emitted" in record.getMessage()


class TestShadow:
    def test_an_environment_op_is_a_logical_resource(self):
        # Were the refusal ever removed, --shadow would report the dropped
        # environment as a native-only gap instead of leaving it out.
        found = native_logical_resources(
            [{"op": "mwaa.ensure_environment", "environment_name": "mwaa-orders"}]
        )
        assert {(r.kind, r.identity) for r in found} == {("environment", "mwaa-orders")}

    def test_shadow_compare_surfaces_the_refusal(self):
        with pytest.raises(UnsupportedBindingError):
            shadow_compare(
                _contract(),
                plugin=_aws(),
                native_actions=[{"op": "mwaa.ensure_environment", "environment_name": "e"}],
            )
