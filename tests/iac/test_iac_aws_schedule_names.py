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
import ast
import hashlib
import json
import logging
import re
import sys
import types
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli import generate_iac
from fluid_build.cli._apply_opentofu_engine import emit_module
from fluid_build.cli._common import CLIError
from fluid_build.providers._plan_validation import PlanValidationError
from fluid_build.providers.aws.plan.schedule import SchedulePlanner
from fluid_build.providers.aws.util.config import AwsAccountUnresolvedError
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
    @staticmethod
    def _digest(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]

    def test_a_name_the_rule_accepts_is_returned_as_written(self):
        # Nothing already deployed under such a name is renamed.
        assert bounded_aws_name("fluid-workflow-sales_orders", LAMBDA_FUNCTION_NAME) == (
            "fluid-workflow-sales_orders"
        )

    def test_a_name_that_was_sanitised_ends_in_the_hash_of_the_raw_name(self):
        raw = "fluid-workflow-sales.orders"
        assert bounded_aws_name(raw, LAMBDA_FUNCTION_NAME) == (
            f"fluid-workflow-sales-orders-{self._digest(raw)}"
        )

    def test_two_ids_that_sanitise_alike_do_not_collide(self):
        dotted = bounded_aws_name("fluid-workflow-a.b", LAMBDA_FUNCTION_NAME)
        dashed = bounded_aws_name("fluid-workflow-a-b", LAMBDA_FUNCTION_NAME)
        assert dashed == "fluid-workflow-a-b"
        assert dotted != dashed

    def test_each_service_keeps_the_characters_it_allows(self):
        raw = "fluid-x.y_z"
        assert bounded_aws_name(raw, SCHEDULER_SCHEDULE_NAME) == raw  # `.` is legal here
        assert bounded_aws_name(raw, EVENTBRIDGE_RULE_NAME) == raw
        assert bounded_aws_name(raw, LAMBDA_FUNCTION_NAME) == f"fluid-x-y_z-{self._digest(raw)}"
        assert bounded_aws_name("a:b/c d", STATE_MACHINE_NAME) == (
            f"a-b-c-d-{self._digest('a:b/c d')}"
        )
        assert bounded_aws_name("mwaa-a.b", MWAA_ENVIRONMENT_NAME) == (
            f"mwaa-a-b-{self._digest('mwaa-a.b')}"
        )

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


class TestContractValuesNeverBecomeGeneratedLambdaCode:
    """A contract value reaches the generated Lambda source only as a literal.

    Spliced inside quotes (``'{contract_id}'``, ``json.loads('''{tasks}''')``), a
    task param such as ``'''+__import__('os').system('id')+'''`` closed the
    literal and ran on every schedule tick under ``fluid-workflow-execution``,
    and an id with a trailing newline left a Lambda that was a SyntaxError.
    """

    EVIL = "'''+__import__('os').system('id')+'''"
    HOSTILE_IDS = [
        EVIL,
        "a'b\"c'''d\"\"\"e",
        "back\\slash\\'",
        "line\nbreak",
        "trailing.newline\n",
        "${aws_lambda_function.x.arn}{braces}{{x}}",
    ]
    #: Calls the generated sources are expected to make, by final name.
    EXPECTED_CALLS = {
        "loads",
        "dumps",
        "getLogger",
        "setLevel",
        "get",
        "info",
        "set",
        "list",
        "all",
        "add",
        "remove",
        "client",
        "invoke",
        "RuntimeError",
        "len",
    }

    @staticmethod
    def _sources(contract_id: str, param: str) -> Dict[str, str]:
        planner = SchedulePlanner(ACCOUNT, REGION)
        task = {
            "taskId": param,
            "type": "provider_action",
            "action": "aws.glue.run_job",
            "params": {"k": param},
            "dependsOn": [],
        }
        contract = {"id": contract_id, "name": "n"}
        orchestration = {"tasks": [task]}
        return {
            "workflow": planner._generate_workflow_lambda_code(contract, orchestration)["ZipFile"],
            "s3": planner._generate_s3_trigger_lambda_code(contract, {})["ZipFile"],
            "dynamodb": planner._generate_dynamodb_trigger_lambda_code(contract, {})["ZipFile"],
            "event": planner._generate_event_trigger_lambda_code(contract, {})["ZipFile"],
            "sqs": planner._generate_sqs_trigger_lambda_code(contract, {})["ZipFile"],
        }

    @staticmethod
    def _callee(call: ast.Call) -> str:
        func = call.func
        return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "?")

    @pytest.mark.parametrize("contract_id", HOSTILE_IDS, ids=range(len(HOSTILE_IDS)))
    @pytest.mark.parametrize("param", [EVIL, "p'\"\\\n${x}"], ids=["inject", "punctuation"])
    def test_the_source_parses_calls_only_what_it_should_and_round_trips(self, contract_id, param):
        for kind, source in self._sources(contract_id, param).items():
            tree = ast.parse(source)  # a SyntaxError here is the trailing-newline defect
            callees = {self._callee(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
            assert callees <= self.EXPECTED_CALLS, (kind, callees - self.EXPECTED_CALLS)

            strings = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values, strict=True):
                        if (
                            isinstance(key, ast.Constant)
                            and key.value == "contract_id"
                            and isinstance(value, ast.Constant)
                        ):
                            strings.add(value.value)
            if kind != "workflow":
                assert strings == {contract_id}, kind
                functions = [
                    ast.literal_eval(kw.value)
                    for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    for kw in n.keywords
                    if kw.arg == "FunctionName"
                ]
                assert functions == [SchedulePlanner.workflow_function_name(contract_id)]

    @pytest.mark.parametrize("contract_id", HOSTILE_IDS, ids=range(len(HOSTILE_IDS)))
    def test_the_workflow_lambda_round_trips_its_tasks_and_default_id(self, contract_id):
        param = self.EVIL
        source = self._sources(contract_id, param)["workflow"]
        boto3 = types.ModuleType("boto3")
        namespace: Dict[str, Any] = {}
        sys.modules["boto3"], saved = boto3, sys.modules.get("boto3")
        try:
            exec(compile(source, "<lambda>", "exec"), namespace)  # noqa: S102
        finally:
            if saved is None:
                del sys.modules["boto3"]
            else:
                sys.modules["boto3"] = saved
        assert namespace["TASKS"] == [
            {"taskId": param, "action": "aws.glue.run_job", "params": {"k": param}, "dependsOn": []}
        ]
        # The handler's fallback id is the exact id, and running it runs no payload.
        handler_defaults = [
            ast.literal_eval(n.args[1])
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call)
            and self._callee(n) == "get"
            and len(n.args) == 2
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "contract_id"
        ]
        assert handler_defaults == [contract_id]
        assert namespace["handler"]({}, None)["statusCode"] == 200


class TestEveryPlanningFailureIsARefusalNotADrop:
    def test_an_orchestration_task_the_planner_rejects_stops_generate_iac(self, tmp_path):
        contract = _contract(
            engine="airflow",
            tasks=[
                {"taskId": "a", "type": "provider_action", "action": "gcp.bq.run", "params": {}}
            ],
        )
        with pytest.raises(CLIError) as excinfo:
            generate_iac.run(_args(contract, tmp_path), logging.getLogger("t"))
        assert excinfo.value.event == "generate_iac_failed"
        assert "Orchestration planning failed" in excinfo.value.context["error"]

    def test_a_schedule_planner_failure_is_a_plan_validation_error(self, monkeypatch):
        from fluid_build.providers.aws.provider import AwsProvider

        def boom(*args, **kwargs):
            raise ValueError("bad schedule")

        monkeypatch.setattr("fluid_build.providers.aws.plan.schedule.plan_schedule_actions", boom)
        provider = AwsProvider(account_id=ACCOUNT, region=REGION)
        with pytest.raises(PlanValidationError, match="Schedule planning failed"):
            provider._plan_schedule(_contract())


class TestS3BucketNames:
    @pytest.fixture
    def validate(self):
        from fluid_build.providers.aws.util.validation import ResourceValidator

        return ResourceValidator(ACCOUNT, REGION).validate_s3_bucket_name

    @pytest.mark.parametrize(
        "bucket", ["acme.orders.lake", "my-bucket.v2", "a1b", "data.2026-lake", "x" * 63]
    )
    def test_a_name_s3_allows_is_accepted(self, validate, bucket):
        assert validate(bucket) == (True, None)

    @pytest.mark.parametrize(
        "bucket",
        [
            "a..b",
            "a.-b",
            "a-.b",
            ".abc",
            "abc.",
            "-abc",
            "192.168.0.1",
            "Upper.case",
            "has_underscore",
            "ab",
            "x" * 64,
            "xn--bucket",
        ],
    )
    def test_a_name_s3_refuses_is_refused(self, validate, bucket):
        assert validate(bucket)[0] is False

    def test_generate_iac_accepts_a_dotted_bucket(self, tmp_path):
        contract = _contract()
        contract["exposes"][0]["binding"]["location"]["bucket"] = "acme.orders.lake"
        assert generate_iac.run(_args(contract, tmp_path), logging.getLogger("t")) == 0


class TestTheUnresolvedAccountMessage:
    @staticmethod
    def _provider_without_account(monkeypatch):
        from fluid_build.providers.aws.provider import AwsProvider

        monkeypatch.setattr(
            "fluid_build.providers.aws.util.config.resolve_account_and_region",
            lambda account_id=None, region=None: (None, REGION),
        )
        return AwsProvider(region=REGION)

    def test_missing_boto3_is_named_with_the_install_command(self, monkeypatch):
        provider = self._provider_without_account(monkeypatch)
        monkeypatch.setattr("fluid_build.providers.aws.util.config._boto3_missing", lambda: True)
        with pytest.raises(AwsAccountUnresolvedError) as excinfo:
            provider.plan(_contract())
        message = str(excinfo.value)
        assert "boto3 is not installed" in message
        assert "pip install 'data-product-forge[aws]'" in message
        assert "AWS_ACCOUNT_ID" in message

    def test_with_boto3_present_it_points_at_credentials(self, monkeypatch):
        provider = self._provider_without_account(monkeypatch)
        monkeypatch.setattr("fluid_build.providers.aws.util.config._boto3_missing", lambda: False)
        with pytest.raises(AwsAccountUnresolvedError) as excinfo:
            provider.plan(_contract())
        message = str(excinfo.value)
        assert "sts:GetCallerIdentity" in message
        assert "boto3" not in message
