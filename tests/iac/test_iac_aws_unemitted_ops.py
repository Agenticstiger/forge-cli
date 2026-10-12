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
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli import generate_iac
from fluid_build.cli._common import CLIError
from fluid_build.iac import build_module, get_iac_plugin, shadow_compare
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.aws import (
    _ACTION_EMITTERS,
    _MWAA_OP,
    _OPS_NOT_YET_EMITTED,
    _OPS_OWNED_ELSEWHERE,
)
from fluid_build.iac.shadow import native_logical_resources
from fluid_build.providers.aws.provider import AwsProvider
from fluid_build.providers.base import ProviderError
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
        (keep_airflow,) = [step for step in err.remediation if "schedule-sync" in step]
        assert "create the MWAA environment 'mwaa-orders' yourself" in keep_airflow
        # MWAA's schedule-sync dispatch reads --destination, never --environment-name.
        assert "--scheduler mwaa --dags-dir dags --destination s3://" in keep_airflow
        assert "--environment-name" not in keep_airflow

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
                "mwaa": {
                    "environmentName": "shared-airflow",
                    "dagS3Path": "s3://shared-airflow-dags/dags/",
                },
            }
        )
        # Both paths: the plan's op, and no plan at all (the planner failed).
        for actions in (_planned(contract), []):
            with pytest.raises(UnsupportedBindingError) as excinfo:
                build_module(_aws(), contract, actions=actions)
            err = excinfo.value
            assert "shared-airflow" in str(err)
            # The user already runs it: the way out pushes the DAG to the
            # folder the contract says it reads, and does not say to create one.
            (keep_airflow,) = [step for step in err.remediation if "schedule-sync" in step]
            assert "names the existing MWAA environment 'shared-airflow'" in keep_airflow
            assert "--destination s3://shared-airflow-dags/dags/" in keep_airflow
            assert "yourself" not in keep_airflow

    def test_a_control_character_in_the_dag_path_is_not_printed(self):
        contract = _contract(
            orchestration={**_AIRFLOW, "mwaa": {"dagS3Path": "s3://b/dags/\x1b[2J"}}
        )
        with pytest.raises(UnsupportedBindingError) as excinfo:
            _aws().emit(contract, [])
        assert not any("\x1b" in step for step in excinfo.value.remediation)

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
    # A non-string op is named too, not a TypeError from hashing it.
    @pytest.mark.parametrize(
        ("action", "named"),
        [
            ({"op": "glue.ensure_crawler", "id": "c"}, "op='glue.ensure_crawler' id='c'"),
            ({"id": "x"}, "op=None id='x'"),
            ({"id": "x", "op": ["glue", "ensure_job"]}, "op=['glue', 'ensure_job'] id='x'"),
        ],
    )
    def test_an_op_with_no_translation_is_named_in_a_warning(self, action, named, caplog):
        caplog.set_level(logging.WARNING, logger=_AWS_LOGGER)
        resources = _aws().emit(_contract(), [action])
        # The data plane is still emitted: the op is named, not fatal.
        assert "aws_glue_catalog_table" in resources
        (record,) = [r for r in caplog.records if "planned_op_not_emitted" in r.getMessage()]
        assert named in record.getMessage()

    def test_a_control_character_in_an_op_does_not_reach_the_terminal(self, caplog):
        caplog.set_level(logging.WARNING, logger=_AWS_LOGGER)
        _aws().emit(_contract(), [{"op": "x\x1b[31m", "id": "i\x1b[0m"}])
        (record,) = [r for r in caplog.records if "planned_op_not_emitted" in r.getMessage()]
        assert "\x1b" not in record.getMessage()
        assert "x\\x1b[31m" in record.getMessage()

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
            # The pre-replace backup the apply's rollback writer records.
            "aws.s3.copy_prefix",
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


_AIRFLOW = {"engine": "airflow", "schedule": "0 * * * *"}


def _upper_case_table() -> Dict[str, Any]:
    """Valid to ``fluid validate``; the AWS planner's Glue name check refuses it."""
    contract = _contract(orchestration=dict(_AIRFLOW))
    contract["exposes"][0]["binding"]["location"]["table"] = "Orders"
    return contract


def _cyclic_tasks() -> Dict[str, Any]:
    """``orchestration.tasks`` that depend on each other: the planner raises."""
    task = {"type": "provider_action", "action": "aws.glue.run_job", "params": {}}
    return _contract(
        orchestration={
            **_AIRFLOW,
            "tasks": [
                {**task, "taskId": "a", "dependsOn": ["b"]},
                {**task, "taskId": "b", "dependsOn": ["a"]},
            ],
        }
    )


class TestThePlannerFailing:
    """The planner failing on another field does not let the environment drop.

    ``native_actions`` returns ``[]`` when the native planner raises (it logs
    that at DEBUG), so the emitter saw no ``mwaa.ensure_environment`` and wrote
    the data plane alone, exit 0.
    """

    @pytest.mark.parametrize("make", [_upper_case_table, _cyclic_tasks], ids=["glue-name", "cycle"])
    def test_the_contract_is_refused_without_a_plan(self, make):
        contract = make()
        with pytest.raises(ProviderError):
            _planned(contract)
        with pytest.raises(UnsupportedBindingError) as excinfo:
            _aws().emit(contract, [])
        assert excinfo.value.kind == "mwaa-environment-not-emitted"
        assert "mwaa-offline_orders" in str(excinfo.value)

    def test_generate_iac_is_refused(self, no_aws, monkeypatch, tmp_path):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        out = tmp_path / "out"
        with pytest.raises(CLIError) as excinfo:
            generate_iac.run(
                _args(_write(tmp_path, _upper_case_table()), out), logging.getLogger("t")
            )
        assert excinfo.value.context["kind"] == "mwaa-environment-not-emitted"
        assert not out.exists()

    @pytest.mark.parametrize(
        "orchestration",
        [
            None,
            {"schedule": "rate(1 hour)"},
            {"engine": "airflow"},
            # The planner does not strip the engine: this one is EventBridge.
            {"engine": "airflow ", "schedule": "0 * * * *"},
            {"engine": None, "schedule": "0 * * * *"},
        ],
    )
    def test_a_contract_that_asks_for_no_mwaa_still_emits_without_a_plan(self, orchestration):
        contract = _contract() if orchestration is None else _contract(orchestration=orchestration)
        assert "aws_glue_catalog_table" in _aws().emit(contract, [])

    @pytest.mark.parametrize("engine", ["AIRFLOW", "mwaa"])
    def test_the_engine_is_matched_as_the_planner_matches_it(self, engine):
        contract = _contract(orchestration={"engine": engine, "schedule": "0 * * * *"})
        assert any(a.get("op") == _MWAA_OP for a in _planned(contract))
        with pytest.raises(UnsupportedBindingError):
            _aws().emit(contract, [])


def _two_clouds(other: str) -> Dict[str, Any]:
    """A contract whose first expose is on ``other`` and whose second is on AWS."""
    contract = _contract()
    first = {
        "gcp": {
            "platform": "gcp",
            "format": "bigquery_table",
            "location": {"project": "p-1", "dataset": "ds", "table": "t", "region": "US"},
        },
        "snowflake": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "DB", "schema": "S", "table": "T", "account": "acct"},
        },
    }[other]
    contract["exposes"].insert(
        0,
        {
            "exposeId": f"{other}_orders",
            "kind": "table",
            "binding": first,
            "contract": {"schema": [{"name": "id", "type": "string"}]},
        },
    )
    return contract


class TestAMultiCloudContract:
    """``--provider aws`` on a contract whose first expose is on another cloud.

    ``native_actions`` plans with the first expose's provider, so that cloud's
    ops reach the AWS emitter. Refusing them as unknown AWS ops failed the AWS
    half of the contract, which the ambiguous-provider error tells the user to
    apply with ``--provider aws``.
    """

    @pytest.mark.parametrize("other", ["gcp", "snowflake"])
    def test_generate_iac_writes_the_aws_module(self, other, no_aws, monkeypatch, tmp_path):
        monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
        out = tmp_path / "out"
        args = _args(_write(tmp_path, _two_clouds(other)), out)
        args.provider = "aws"
        generate_iac.run(args, logging.getLogger("t"))
        module = json.loads((out / "main.tf.json").read_text(encoding="utf-8"))
        assert {"aws_glue_catalog_table", "aws_s3_bucket"} <= set(module["resource"])

    def test_another_clouds_op_is_named_not_refused(self, caplog):
        caplog.set_level(logging.WARNING, logger=_AWS_LOGGER)
        resources = _aws().emit(
            _two_clouds("gcp"), [{"op": "bq.ensure_dataset", "id": "dataset_ds"}]
        )
        assert "aws_glue_catalog_table" in resources
        (record,) = [r for r in caplog.records if "bq.ensure_dataset" in r.getMessage()]
        assert record.levelno == logging.WARNING


_AWS_PLANNER = Path(generate_iac.__file__).resolve().parents[1] / "providers" / "aws"
_OP_LITERAL = re.compile(r"""["']op["']\s*:\s*["']([A-Za-z0-9_.]+)["']""")


def _classified() -> set:
    return set(_ACTION_EMITTERS) | _OPS_OWNED_ELSEWHERE | _OPS_NOT_YET_EMITTED | {_MWAA_OP}


class TestThePlannerVocabulary:
    """Every op the AWS planner can produce is emitted or classified.

    The two allowlists in ``iac/providers/aws.py`` are a second copy of the
    planner's vocabulary; this is what ties them together, the way dbt-core's
    adapter tests assert every materialization an adapter ships is handled.
    An op this fails on is warned about at apply, never provisioned.
    """

    def test_every_op_literal_in_the_planner_is_classified(self):
        found = {}
        for path in sorted(_AWS_PLANNER.rglob("*.py")):
            if "tests" in path.relative_to(_AWS_PLANNER).parts:
                continue
            for op in _OP_LITERAL.findall(path.read_text(encoding="utf-8")):
                found.setdefault(op, path.relative_to(_AWS_PLANNER).as_posix())
        # The scan must see the planner, or it proves nothing.
        assert {"glue.ensure_job", _MWAA_OP, "aws.s3.copy_prefix"} <= set(found)
        unclassified = {op: where for op, where in found.items() if op not in _classified()}
        assert unclassified == {}, (
            "planner ops the AWS OpenTofu emitter neither emits nor classifies: add each to "
            "_ACTION_EMITTERS, _OPS_OWNED_ELSEWHERE or _OPS_NOT_YET_EMITTED"
        )

    def test_a_destructive_plan_is_classified(self):
        # mode="replace" adds the phase-3 snapshot ops the default plan skips.
        contract = _contract(orchestration={"schedule": "rate(1 hour)"})
        contract["exposes"][0]["binding"]["format"] = "glue_table"
        actions = AwsProvider(
            account_id=ACCOUNT, region="us-east-1", logger=logging.getLogger("t")
        ).plan(contract, mode="replace")
        ops = {a.get("op") for a in actions}
        assert "aws.s3.copy_prefix" in ops
        assert ops - _classified() == set()
        # And the emitter takes the whole plan: nothing refused, nothing unknown.
        json.loads(build_module(_aws(), contract, actions=actions))

    def test_the_lists_do_not_overlap(self):
        lists = [set(_ACTION_EMITTERS), set(_OPS_OWNED_ELSEWHERE), set(_OPS_NOT_YET_EMITTED)]
        assert sum(len(x) for x in lists) == len(set().union(*lists))
        assert not any(_MWAA_OP in x for x in lists)


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
