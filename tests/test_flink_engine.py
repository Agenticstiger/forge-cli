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

"""``engine: flink`` (schema 0.7.6): validated and planned, never run here.

A Flink job runs on its own platform (the Flink Kubernetes Operator, AWS
Managed Service for Apache Flink). Its SQL is Flink's dialect, so it must
reach neither the local DuckDB engine, a warehouse planner nor a scheduler.
The build runner counts it on its own summary line, apart from skips. The
same holds for ``properties.language: flink_sql``, the way a 0.7.5 contract
declares the job.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest
import yaml

from fluid_build.build_runners.base import is_embedded_sql_build, run_builds_from_args
from fluid_build.util.contract import is_externally_run_build

SCHEMAS = Path(__file__).resolve().parent.parent / "fluid_build" / "schemas"

FLINK_SQL = (
    "INSERT INTO service_instances_full\n"
    "SELECT i.id, i.status, v.location\n"
    "FROM service_instances i\n"
    "JOIN inventory v ON i.id = v.instance_id\n"
)


def _flink_build(platform: str = "kubernetes", *, engine: str = "flink") -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": "reconcile",
        "engine": engine,
        "pattern": "embedded-logic",
        "properties": {
            "language": "flink_sql",
            "sql": FLINK_SQL,
            "parameters": {"parallelism": 2},
        },
    }
    if platform:
        build["execution"] = {"runtime": {"platform": platform}}
    return build


#: ``engine: flink`` (0.7.6), and the 0.7.5 way to declare the same job.
SHAPES = [
    pytest.param(("0.7.6", "flink"), id="engine-flink"),
    pytest.param(("0.7.5", "sql"), id="language-flink-sql"),
]


def _contract(fluid_version: str = "0.7.6", engine: str = "flink") -> Dict[str, Any]:
    contract = {
        "fluidVersion": fluid_version,
        "kind": "DataProduct",
        "id": "network.service-instances-full",
        "name": "Service instances (reconciled)",
        "metadata": {"owner": {"team": "data-engineering"}},
        "consumes": [
            {
                "productId": "network.service-instances",
                "exposeId": "service_instances",
                "versionConstraint": "^1.0",
            }
        ],
        "builds": [_flink_build(engine=engine)],
        "exposes": [
            {
                "exposeId": "service_instances_full",
                "kind": "topic",
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "network.service-instances-full"},
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string"},
                        {"name": "status", "type": "string"},
                        {"name": "location", "type": "string"},
                    ]
                },
            }
        ],
    }
    return contract


def _errors(version: str, contract: Dict[str, Any]) -> list:
    schema = json.loads((SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    return [e.message for e in validator.iter_errors(contract)]


class TestSchema:
    def test_0_7_6_accepts_engine_flink(self) -> None:
        assert _errors("0.7.6", _contract()) == []

    def test_0_7_5_is_unchanged(self) -> None:
        # The engine arrived in 0.7.6; the released 0.7.5 does not move.
        assert any("'flink'" in msg for msg in _errors("0.7.5", _contract("0.7.5")))

    def test_0_7_5_declares_the_job_with_language_flink_sql(self) -> None:
        assert _errors("0.7.5", _contract("0.7.5", "sql")) == []

    def test_deployment_settings_belong_under_parameters(self) -> None:
        # embeddedLogicPattern is closed, so a setting beside ``sql`` fails.
        contract = _contract()
        props = contract["builds"][0]["properties"]
        props["parallelism"] = props.pop("parameters")["parallelism"]
        assert any("parallelism" in msg for msg in _errors("0.7.6", contract))


@pytest.mark.parametrize(
    "build, external",
    [
        pytest.param(_flink_build(), True, id="engine-flink"),
        pytest.param({"engine": " FLINK "}, True, id="engine-case-and-space"),
        pytest.param({"engine": "sql", "properties": {"language": "Flink_SQL"}}, True, id="lang"),
        pytest.param({"engine": "sql", "properties": {"sql": "SELECT 1"}}, False, id="sql"),
        pytest.param({"properties": {"language": "sql"}}, False, id="language-sql"),
        # Out of scope here: other languages keep the behaviour they had.
        pytest.param({"properties": {"language": "python", "sql": "--"}}, False, id="python"),
        pytest.param({"engine": 7, "properties": ["flink_sql"]}, False, id="malformed"),
    ],
)
def test_classification(build: Dict[str, Any], external: bool) -> None:
    assert is_externally_run_build(build) is external


@pytest.mark.parametrize("engine", ["flink", "sql"])
def test_a_flink_build_is_not_inline_sql(engine: str) -> None:
    # ``pattern: embedded-logic`` + ``properties.sql`` made it inline SQL,
    # which ran Flink SQL on DuckDB, or failed on an unknown platform.
    assert is_embedded_sql_build(_flink_build(engine=engine)) is False
    plain = {"pattern": "embedded-logic", "properties": {"sql": "SELECT 1"}}
    assert is_embedded_sql_build(plain) is True
    python = {
        "pattern": "embedded-logic",
        "properties": {"sql": "--", "language": "python"},
    }
    assert is_embedded_sql_build(python) is True  # unchanged


def _args(contract_path: Path, *, dry_run: bool) -> argparse.Namespace:
    return argparse.Namespace(
        contract=str(contract_path),
        build_id=None,
        dry_run=dry_run,
        fail_fast=False,
        delay=0,
        no_output=True,
        mode="amend-and-build",
        env=None,
        sample_rows=None,
    )


class TestBuildRunner:
    @pytest.mark.parametrize("shape", SHAPES)
    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "run"])
    @pytest.mark.parametrize("platform", ["kubernetes", ""], ids=["kubernetes", "no-platform"])
    def test_a_flink_build_is_reported_not_run(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        dry_run: bool,
        platform: str,
        shape: tuple,
    ) -> None:
        def _must_not_run(*_a: Any, **_k: Any) -> int:
            raise AssertionError("a flink build reached the inline-SQL executor")

        monkeypatch.setattr(
            "fluid_build.build_runners.base._execute_embedded_sql_build", _must_not_run
        )
        version, engine = shape
        contract = _contract(version, engine)
        contract["builds"] = [_flink_build(platform, engine=engine)]
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")

        # A contract whose only build runs elsewhere is not "every build was
        # skipped": nothing is missing, so the apply succeeds without
        # --allow-skipped-builds.
        assert run_builds_from_args(_args(path, dry_run=dry_run), logging.getLogger("t")) == 0
        out = capsys.readouterr().out
        said = "engine 'flink'" if engine == "flink" else "language 'flink_sql'"
        assert f"{said} runs on" in out and "outside forge-cli" in out
        assert "Run outside forge-cli: 1" in out
        assert "Skipped: 0" in out
        assert "INSERT INTO" not in out

    def test_the_run_report_says_why_it_did_not_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import fluid_build.observability.apply_run as apply_run

        recorded: list = []

        class _Report:
            def record_build(self, **kwargs: Any) -> None:
                recorded.append(kwargs)

            def build_failed(self, event: str) -> None:
                raise AssertionError(event)

        monkeypatch.setattr(apply_run, "current_apply_run", lambda: _Report())
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(_contract()), encoding="utf-8")
        assert run_builds_from_args(_args(path, dry_run=True), logging.getLogger("t")) == 0
        assert recorded == [
            {
                "build_id": "reconcile",
                "status": "skipped",
                "reason": "runs_outside_forge",
            }
        ]


@pytest.mark.parametrize("shape", SHAPES)
class TestPlanners:
    """No planner turns Flink SQL into a DuckDB, Athena, BigQuery or Snowflake action."""

    def test_local_planner(self, shape: tuple) -> None:
        from fluid_build.providers.local.planner import plan_actions

        actions = plan_actions(copy.deepcopy(_contract(*shape)), logger=logging.getLogger("t"))
        assert not [a for a in actions if a.get("op") == "execute_sql"]
        assert "INSERT INTO service_instances_full" not in json.dumps(actions)

    def test_local_planner_lands_the_sql_build_before_a_flink_build(self, shape: tuple) -> None:
        # With the Flink build last, the expose took the first loaded input,
        # raw, because the last build had produced no table. The SQL build
        # names the expose; the Flink build names none.
        from fluid_build.providers.local.planner import plan_actions

        contract = copy.deepcopy(_contract(*shape))
        clean = {
            "id": "clean",
            "engine": "sql",
            "pattern": "embedded-logic",
            "outputs": ["service_instances_full"],
            "properties": {
                "sql": "SELECT id FROM customers",
                "parameters": {"inputs": [{"name": "customers", "path": "data/customers.csv"}]},
            },
        }
        contract["builds"].insert(0, clean)
        contract["exposes"][0]["binding"] = {
            "platform": "local",
            "format": "csv",
            "location": {"path": "out/clean.csv"},
        }
        actions = plan_actions(contract, logger=logging.getLogger("t"))
        (materialize,) = [a for a in actions if a.get("op") == "materialize"]
        assert materialize["payload"]["source_table"] == "result_clean"

    def test_local_planner_lands_nothing_for_an_expose_flink_writes(self, shape: tuple) -> None:
        # With every build running elsewhere, the expose took the first loaded
        # input and landed it raw under the expose's name.
        from fluid_build.providers.local.planner import plan_actions

        contract = copy.deepcopy(_contract(*shape))
        contract["builds"][0]["properties"]["parameters"]["inputs"] = [
            {"name": "raw", "path": "data/raw.csv"}
        ]
        contract["exposes"][0]["binding"] = {
            "platform": "local",
            "format": "csv",
            "location": {"path": "out/full.csv"},
        }
        actions = plan_actions(contract, logger=logging.getLogger("t"))
        assert not [a for a in actions if a.get("op") == "materialize"]

    @pytest.mark.parametrize("sql_outputs", [None, ["customers_full"]], ids=["none", "declared"])
    def test_an_expose_flink_may_write_never_lands_another_builds_rows(
        self, shape: tuple, sql_outputs, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A SQL build's table landed under the Flink expose's name, columns the
        # expose does not declare included.
        from fluid_build.providers.local.planner import plan_actions

        contract = copy.deepcopy(_contract(*shape))
        full = {
            "id": "full",
            "engine": "sql",
            "pattern": "embedded-logic",
            "properties": {
                "sql": "SELECT id, email, status FROM customers",
                "parameters": {"inputs": [{"name": "customers", "path": "data/c.csv"}]},
            },
        }
        if sql_outputs:
            full["outputs"] = sql_outputs
        contract["builds"].insert(0, full)
        contract["exposes"] = [
            {
                "exposeId": name,
                "binding": {
                    "platform": "local",
                    "format": "csv",
                    "location": {"path": f"out/{name}.csv"},
                },
            }
            for name in ("customers_full", "customers_public")
        ]
        caplog.set_level(logging.INFO, logger="t")
        actions = plan_actions(contract, logger=logging.getLogger("t"))
        landed = {
            a["resource_id"]: a["payload"]["source_table"]
            for a in actions
            if a.get("op") == "materialize"
        }
        if sql_outputs:
            assert landed == {"customers_full": "result_full"}
        else:
            # Either build may write either expose: neither is guessed.
            assert landed == {}
            assert "Declare builds[].outputs" in caplog.text

    def test_a_null_build_does_not_crash_the_local_planner(self, shape: tuple) -> None:
        from fluid_build.providers.local.planner import plan_actions

        contract = copy.deepcopy(_contract(*shape))
        contract["builds"].insert(0, None)
        plan_actions(contract, logger=logging.getLogger("t"))

    def test_local_provider_derives_no_sql(self, tmp_path: Path, shape: tuple) -> None:
        from fluid_build.providers.local.local import LocalProvider

        provider = LocalProvider(project=str(tmp_path))
        actions = provider._derive_actions_from_contract(copy.deepcopy(_contract(*shape)))
        assert actions == [{"op": "noop", "skipped": True, "reason": "build_runs_outside_forge"}]

    def test_aws_planner(self, shape: tuple) -> None:
        from fluid_build.providers.aws.plan.planner import plan_actions

        actions = plan_actions(copy.deepcopy(_contract(*shape)), "123456789012", "eu-west-1")
        assert not [a for a in actions if a.get("op") == "athena.execute_query"]

    def test_gcp_planner(self, shape: tuple) -> None:
        from fluid_build.providers.gcp.plan.planner import plan_actions

        contract = copy.deepcopy(_contract(*shape))
        contract["builds"][0]["outputs"] = ["service_instances_full"]
        actions = plan_actions(contract, "proj", "europe-west1", logging.getLogger("t"))
        assert not [a for a in actions if a.get("op") == "bq.sql.execute"]

    def test_snowflake_planner(self, shape: tuple) -> None:
        from fluid_build.providers.snowflake.plan.planner import plan_actions

        actions = plan_actions(
            copy.deepcopy(_contract(*shape)),
            account="acme",
            warehouse="WH",
            database="DB",
            schema="PUBLIC",
        )
        assert not [a for a in actions if a.get("op") == "sf.sql.execute"]

    def test_snowflake_replace_still_ensures_the_flink_target(self, shape: tuple) -> None:
        # Replace mode skips ensure_table for an expose a SQL build recreates;
        # a Flink build does not recreate it, so its table is still ensured.
        from fluid_build.providers.snowflake.plan.planner import plan_actions

        contract = copy.deepcopy(_contract(*shape))
        contract["builds"][0]["outputs"] = ["full"]
        contract["exposes"] = [
            {
                "exposeId": "full",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "DB", "schema": "PUBLIC", "table": "FULL"},
                },
                "contract": {"schema": [{"name": "ID", "type": "STRING"}]},
            }
        ]
        actions = plan_actions(
            contract,
            account="acme",
            warehouse="WH",
            database="DB",
            schema="PUBLIC",
            mode="replace",
        )
        assert [a for a in actions if a.get("op") == "sf.table.ensure"]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_scheduler_gets_no_task_for_a_flink_build(shape: tuple) -> None:
    # A nightly task "running" a continuous job would only log its SQL and pass.
    from fluid_build.schedulers.synthesis import synthesize_orchestration_from_builds

    contract = _contract(*shape)
    assert synthesize_orchestration_from_builds(copy.deepcopy(contract), "airflow") == {}
    contract["builds"].append({"id": "daily", "engine": "sql", "properties": {"sql": "SELECT 1"}})
    tasks = synthesize_orchestration_from_builds(contract, "airflow")["tasks"]
    assert [t["taskId"] for t in tasks] == ["daily"]


def _run_cli(*argv: str, cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")},
    )


class TestGenerators:
    @pytest.mark.parametrize("shape", SHAPES)
    def test_generate_transformation_has_nothing_to_generate(
        self, tmp_path: Path, shape: tuple
    ) -> None:
        # It failed with engine_not_found and told the user to write the code.
        (tmp_path / "contract.fluid.yaml").write_text(
            yaml.safe_dump(_contract(*shape)), encoding="utf-8"
        )
        result = _run_cli(
            "generate",
            "transformation",
            "contract.fluid.yaml",
            "-o",
            "out",
            "-q",
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "there is no transformation code to generate" in " ".join(result.stdout.split())
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("shape", SHAPES)
    def test_generate_schedule_has_nothing_to_schedule(self, tmp_path: Path, shape: tuple) -> None:
        # It wrote a nightly DAG whose task only logged the Flink SQL.
        (tmp_path / "contract.fluid.yaml").write_text(
            yaml.safe_dump(_contract(*shape)), encoding="utf-8"
        )
        result = _run_cli(
            "generate",
            "schedule",
            "contract.fluid.yaml",
            "-o",
            "sched",
            "--scheduler",
            "airflow",
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "nothing to schedule" in " ".join(result.stdout.split())
        assert not (tmp_path / "sched").exists()

    @pytest.mark.parametrize(
        "variant, extra_args",
        [
            pytest.param("cron-trigger", [], id="cron-trigger-no-scheduler"),
            pytest.param("engine-no-tasks", [], id="orchestration-engine-no-tasks"),
        ],
    )
    def test_nothing_to_schedule_whatever_names_the_scheduler(
        self, tmp_path: Path, variant: str, extra_args: list
    ) -> None:
        # A cron trigger defaulted the scheduler and then failed on the empty
        # list; an orchestration.engine with no tasks wrote an empty DAG.
        contract = _contract()
        if variant == "cron-trigger":
            contract["builds"][0]["execution"]["trigger"] = {
                "type": "schedule",
                "schedule": "@daily",
            }
        else:
            contract["orchestration"] = {"engine": "airflow"}
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
        result = _run_cli(
            "generate",
            "schedule",
            "contract.fluid.yaml",
            "-o",
            "sched",
            *extra_args,
            cwd=tmp_path,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "nothing to schedule" in " ".join(result.stdout.split())
        assert not (tmp_path / "sched").exists()

    def test_an_unknown_scheduler_is_still_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract()), encoding="utf-8")
        result = _run_cli(
            "generate",
            "schedule",
            "contract.fluid.yaml",
            "--scheduler",
            "bogus",
            cwd=tmp_path,
        )
        assert result.returncode == 1
        assert "No scheduler available for 'bogus'" in result.stdout

    def test_a_cron_trigger_on_a_flink_build_gets_no_fluid_apply_dag(self) -> None:
        # The DAG would run `fluid apply --build-id reconcile`, which runs
        # nothing for a Flink job, and report green every time.
        from fluid_build.schedulers.airflow.fluid_apply import (
            has_scheduled_builds,
            scheduled_builds,
        )

        contract = _contract()
        contract["builds"][0]["execution"]["trigger"] = {
            "type": "schedule",
            "schedule": "@daily",
        }
        assert has_scheduled_builds(contract) is False
        assert scheduled_builds(contract) == []


def test_every_build_skipped_counts_only_builds_forge_cli_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract = _contract()
    contract["builds"].append({"id": "ingest", "engine": "python", "repository": "./"})
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    assert run_builds_from_args(_args(path, dry_run=True), logging.getLogger("t")) == 1
    assert "Every build was skipped (1/1)" in capsys.readouterr().err
