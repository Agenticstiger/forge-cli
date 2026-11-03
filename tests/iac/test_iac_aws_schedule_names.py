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

"""An AWS contract's schedule is never dropped silently, and is honestly labelled.

``fluid generate iac`` on a contract whose id carried a dot (the schema's own
example is ``gold.hr.employee_360_v1``) logged "Planned EventBridge schedule"
and wrote a module with no ``aws_lambda_function``, ``aws_lambda_permission`` or
``aws_scheduler_schedule``, exit 0: the planner named the Lambda
``fluid-workflow-<id>`` raw, the plan validator refused the dot, and
``native_actions`` swallowed the refusal at DEBUG. ``fluid apply`` shares that
function, so it omitted the schedule the same way.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli import generate_iac
from fluid_build.cli._apply_opentofu_engine import emit_module
from fluid_build.cli._common import CLIError
from fluid_build.providers._plan_validation import PlanValidationError
from fluid_build.providers.aws.plan.schedule import SchedulePlanner
from fluid_build.providers.aws.util.names import (
    EVENTBRIDGE_RULE_NAME,
    LAMBDA_FUNCTION_NAME,
    LAMBDA_STATEMENT_ID,
    MWAA_ENVIRONMENT_NAME,
    SCHEDULER_SCHEDULE_NAME,
    STATE_MACHINE_NAME,
    bounded_aws_name,
)
from fluid_build.providers.base import ProviderError

pytestmark = [pytest.mark.unit, pytest.mark.provider]

ACCOUNT = "123456789012"
REGION = "us-east-1"
DOTTED = "sales.orders_daily"
LONG = "gold.finance." + "quarterly_revenue_recognition_" * 4 + "v1"

_LAMBDA_NAME = re.compile(r"[a-zA-Z0-9\-_]{1,64}")
_STATEMENT_ID = re.compile(r"[a-zA-Z0-9\-_]{1,100}")
_SCHEDULE_NAME = re.compile(r"[a-zA-Z0-9\-_.]{1,64}")


def _contract(contract_id: str = DOTTED, **orchestration: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": contract_id,
        "name": "Orders",
        "metadata": {"layer": "Silver", "owner": {"team": "data", "email": "d@example.com"}},
        "orchestration": {"engine": "none", "schedule": "cron(0 6 * * ? *)", **orchestration},
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": {
                        "bucket": "offline-lake",
                        "path": "orders/",
                        "database": "sales",
                        "table": "orders",
                        "region": REGION,
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _args(contract: Dict[str, Any], tmp_path: Path) -> argparse.Namespace:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return argparse.Namespace(
        contract=str(path), provider="auto", out=str(tmp_path / "out"), env=None
    )


@pytest.fixture(autouse=True)
def _account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)


class TestBoundedAwsName:
    def test_a_name_that_fits_is_only_sanitised(self):
        assert bounded_aws_name("fluid-workflow-sales.orders", LAMBDA_FUNCTION_NAME) == (
            "fluid-workflow-sales-orders"
        )

    def test_each_service_keeps_the_characters_it_allows(self):
        raw = "fluid-x.y_z"
        assert bounded_aws_name(raw, SCHEDULER_SCHEDULE_NAME) == raw  # `.` is legal here
        assert bounded_aws_name(raw, EVENTBRIDGE_RULE_NAME) == raw
        assert bounded_aws_name(raw, LAMBDA_FUNCTION_NAME) == "fluid-x-y_z"
        assert bounded_aws_name("a:b/c d", STATE_MACHINE_NAME) == "a-b-c-d"
        assert bounded_aws_name("mwaa-a.b", MWAA_ENVIRONMENT_NAME) == "mwaa-a-b"

    @pytest.mark.parametrize(
        "rule",
        [
            LAMBDA_FUNCTION_NAME,
            LAMBDA_STATEMENT_ID,
            SCHEDULER_SCHEDULE_NAME,
            EVENTBRIDGE_RULE_NAME,
            MWAA_ENVIRONMENT_NAME,
            STATE_MACHINE_NAME,
        ],
    )
    def test_a_long_name_is_cut_to_the_limit_deterministically(self, rule):
        limit = rule[1]
        raw = "fluid-workflow-" + "x" * 300
        name = bounded_aws_name(raw, rule)
        assert len(name) == limit
        assert bounded_aws_name(raw, rule) == name

    def test_two_long_names_sharing_a_prefix_do_not_collide(self):
        a = bounded_aws_name("fluid-workflow-" + "x" * 80 + "-a", LAMBDA_FUNCTION_NAME)
        b = bounded_aws_name("fluid-workflow-" + "x" * 80 + "-b", LAMBDA_FUNCTION_NAME)
        assert len(a) == len(b) == 64
        assert a != b


class TestPlannedScheduleNames:
    @pytest.mark.parametrize("contract_id", [DOTTED, LONG], ids=["dotted", "long"])
    def test_every_derived_name_is_valid_and_the_target_matches(self, contract_id):
        actions = SchedulePlanner(ACCOUNT, REGION).plan_schedule_actions(_contract(contract_id))
        by_op = {a["op"]: a for a in actions}
        function = by_op["lambda.ensure_function"]
        schedule = by_op["eventbridge.ensure_schedule"]
        permission = by_op["lambda.add_permission"]

        assert _LAMBDA_NAME.fullmatch(function["function_name"])
        assert _SCHEDULE_NAME.fullmatch(schedule["schedule_name"])
        assert _STATEMENT_ID.fullmatch(permission["statement_id"])
        assert schedule["target"]["arn"].endswith(f":function:{function['function_name']}")
        assert permission["function_name"] == function["function_name"]
        assert permission["source_arn"].endswith(f"/default/{schedule['schedule_name']}")

    def test_the_names_are_deterministic(self):
        planner = SchedulePlanner(ACCOUNT, REGION)
        assert planner.plan_schedule_actions(_contract(LONG)) == planner.plan_schedule_actions(
            _contract(LONG)
        )

    def test_two_long_ids_with_a_shared_prefix_get_distinct_functions(self):
        planner = SchedulePlanner(ACCOUNT, REGION)
        a = planner.plan_schedule_actions(_contract(LONG + "_a"))[0]["function_name"]
        b = planner.plan_schedule_actions(_contract(LONG + "_b"))[0]["function_name"]
        assert a != b

    def test_trigger_lambdas_invoke_the_sanitised_workflow_function(self):
        contract = _contract(
            LONG,
            triggers=[
                {"type": "s3", "id": "land.csv", "bucket": "b"},
                {"type": "sqs", "id": "q", "queue": "q"},
                {"type": "eventbridge", "id": "evt.1", "eventPattern": {"source": ["x"]}},
            ],
        )
        actions = SchedulePlanner(ACCOUNT, REGION).plan_schedule_actions(contract)
        workflow = next(a for a in actions if a["id"].startswith("lambda_workflow_"))
        for action in actions:
            if action["op"] == "lambda.ensure_function":
                assert _LAMBDA_NAME.fullmatch(action["function_name"])
            if action["op"] == "lambda.add_permission":
                assert _STATEMENT_ID.fullmatch(action["statement_id"])
            if action["id"].startswith("lambda_") and "_trigger_" in action["id"]:
                assert f"FunctionName='{workflow['function_name']}'" in action["code"]["ZipFile"]

    @pytest.mark.parametrize("contract_id", [DOTTED, LONG], ids=["dotted", "long"])
    def test_generate_iac_emits_the_schedule(self, contract_id, tmp_path):
        args = _args(_contract(contract_id), tmp_path)
        assert generate_iac.run(args, logging.getLogger("t")) == 0
        module = json.loads((tmp_path / "out" / "main.tf.json").read_text(encoding="utf-8"))
        resources = module["resource"]
        ((fn_key, function),) = resources["aws_lambda_function"].items()
        ((_, schedule),) = resources["aws_scheduler_schedule"].items()
        ((_, permission),) = resources["aws_lambda_permission"].items()

        assert _LAMBDA_NAME.fullmatch(function["function_name"])
        assert _SCHEDULE_NAME.fullmatch(schedule["name"])
        assert _STATEMENT_ID.fullmatch(permission["statement_id"])
        # The schedule and the permission reference the emitted function, not a literal.
        assert schedule["target"]["arn"] == f"${{aws_lambda_function.{fn_key}.arn}}"
        assert permission["function_name"] == f"${{aws_lambda_function.{fn_key}.function_name}}"


class TestEveryNameStartsWithALetter:
    """MWAA requires a leading letter; every name here carries a literal prefix
    (``fluid-``, ``mwaa-``, ``Allow...``) that survives both the replacement and
    the cut, whatever the id starts with."""

    HOSTILE = "9 weird/id:" + "v" * 120

    def test_names_built_from_a_hostile_id(self):
        planner = SchedulePlanner(ACCOUNT, REGION)
        triggers = [
            {"type": "s3", "id": "1.x", "bucket": "b"},
            {"type": "sqs", "id": "2.y", "queue": "q"},
            {"type": "eventbridge", "id": "3.z", "eventPattern": {}},
        ]
        tasks = [{"taskId": "a", "type": "provider_action", "action": "x"}]
        actions = planner.plan_schedule_actions(
            _contract(self.HOSTILE, triggers=triggers, engine="step-functions", tasks=tasks)
        )
        actions += planner.plan_schedule_actions(_contract(self.HOSTILE, engine="mwaa"))
        limits = {
            "function_name": 64,
            "statement_id": 100,
            "schedule_name": 64,
            "rule_name": 64,
            "state_machine_name": 80,
            "environment_name": 80,
        }
        names = []
        for a in actions:
            for key, limit in limits.items():
                if key in a:
                    assert len(a[key]) <= limit, (key, a[key])
                    names.append(a[key])
        assert len(names) >= 10
        assert all(n[0].isalpha() for n in names), names
        # MWAA: a letter first, then letters, digits, ``-`` and ``_``, up to 80.
        env = [a["environment_name"] for a in actions if "environment_name" in a]
        assert env and all(re.fullmatch(r"[a-zA-Z][0-9a-zA-Z\-_]{0,79}", n) for n in env)


class _Refuses:
    """A stand-in native provider whose ``plan`` raises ``exc``."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def plan(self, contract, **_):
        raise self._exc


@pytest.fixture
def planner_raises(monkeypatch: pytest.MonkeyPatch):
    def install(exc: BaseException) -> None:
        monkeypatch.setattr("fluid_build.cli._common.build_provider", lambda *a, **k: _Refuses(exc))

    return install


class TestAPlannerThatRanAndRefusedIsLoud:
    def test_a_plan_validation_failure_stops_native_actions(self, planner_raises):
        planner_raises(PlanValidationError("Plan validation failed:\nAction 1: bad name"))
        with pytest.raises(PlanValidationError):
            generate_iac.native_actions(_contract(), logging.getLogger("t"), offline=True)

    def test_it_is_recognised_through_the_cause_chain(self, planner_raises):
        try:
            raise PlanValidationError("nope")
        except PlanValidationError as inner:
            try:
                raise RuntimeError("wrapped") from inner
            except RuntimeError as outer:
                assert generate_iac._is_plan_validation_refusal(outer) is True

    def test_the_message_is_not_what_is_matched(self):
        assert (
            generate_iac._is_plan_validation_refusal(ProviderError("Plan validation failed: ..."))
            is False
        )

    def test_generate_iac_exits_non_zero(self, planner_raises, tmp_path):
        planner_raises(PlanValidationError("Plan validation failed:\nAction 1: bad name"))
        with pytest.raises(CLIError) as excinfo:
            generate_iac.run(_args(_contract(), tmp_path), logging.getLogger("t"))
        assert excinfo.value.event == "generate_iac_failed"
        assert "Plan validation failed" in excinfo.value.context["error"]
        assert not (tmp_path / "out" / "main.tf.json").exists()

    def test_the_apply_native_path_raises(self, planner_raises):
        planner_raises(PlanValidationError("Plan validation failed:\nAction 1: bad name"))
        with pytest.raises(PlanValidationError):
            emit_module(object(), _contract(), object(), logging.getLogger("t"))

    def test_a_real_planner_refusal_stops_generate_iac(self, tmp_path):
        contract = _contract()
        contract["exposes"][0]["binding"]["location"]["bucket"] = "Not_A_Bucket"
        with pytest.raises(CLIError) as excinfo:
            generate_iac.run(_args(contract, tmp_path), logging.getLogger("t"))
        assert excinfo.value.event == "generate_iac_failed"
        assert "Plan validation failed" in excinfo.value.context["error"]

    def test_a_sovereignty_refusal_still_propagates(self, planner_raises):
        from fluid_build.cli._errors import SovereigntyViolationError

        planner_raises(
            SovereigntyViolationError.for_connector(connector="aws:us-east-1", jurisdiction="EU")
        )
        with pytest.raises(SovereigntyViolationError):
            generate_iac.native_actions(_contract(), logging.getLogger("t"), offline=True)

    @pytest.mark.parametrize("exc", [ImportError("no boto3"), RuntimeError("no credentials")])
    def test_a_planner_that_could_not_run_is_still_best_effort(self, planner_raises, exc):
        planner_raises(exc)
        assert generate_iac.native_actions(_contract(), logging.getLogger("t")) == []


class TestThePlaceholderLambdaIsLabelled:
    @staticmethod
    def _warnings(caplog) -> list:
        events = []
        for record in caplog.records:
            if record.levelno == logging.WARNING and "schedule_placeholder_lambda" in (
                record.getMessage()
            ):
                events.append(json.loads(record.getMessage()))
        return events

    def test_a_schedule_warns_that_the_lambda_is_a_placeholder(self, caplog):
        logger = logging.getLogger("t")
        with caplog.at_level(logging.WARNING, logger="t"):
            generate_iac.native_actions(_contract(), logger, offline=True)
        (event,) = self._warnings(caplog)
        assert event["contract_id"] == DOTTED
        assert "placeholder" in event["message"]
        assert "does not run" in event["message"]
        assert f"role/{'fluid-workflow-execution'}" in event["message"]
        assert "role/EventBridgeSchedulerRole" in event["message"]
        assert "must already exist" in event["message"]

    def test_apply_native_path_warns_too(self, caplog):
        """``fluid apply`` plans through ``native_actions`` without ``offline``."""
        with caplog.at_level(logging.WARNING, logger="t"):
            generate_iac.native_actions(_contract(), logging.getLogger("t"))
        assert len(self._warnings(caplog)) == 1

    def test_generate_iac_shows_it(self, caplog, tmp_path):
        with caplog.at_level(logging.WARNING):
            generate_iac.run(_args(_contract(), tmp_path), logging.getLogger("t"))
        assert len(self._warnings(caplog)) == 1

    def test_no_schedule_no_warning(self, caplog):
        contract = _contract()
        del contract["orchestration"]
        with caplog.at_level(logging.WARNING, logger="t"):
            generate_iac.native_actions(contract, logging.getLogger("t"), offline=True)
        assert self._warnings(caplog) == []

    def test_an_mwaa_schedule_plans_no_placeholder_lambda(self, caplog):
        with caplog.at_level(logging.WARNING, logger="t"):
            generate_iac.native_actions(
                _contract(engine="mwaa"), logging.getLogger("t"), offline=True
            )
        assert self._warnings(caplog) == []
