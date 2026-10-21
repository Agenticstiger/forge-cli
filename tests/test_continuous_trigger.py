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

"""``trigger.type: continuous`` (schema 0.7.6): a job its platform keeps running.

A long-running Flink job, or a Kafka Connect or Debezium connector, has no
trigger type: schedule, event and the rest all start runs. ``continuous`` says
no scheduler starts this build, so every path that turns builds into scheduled
runs leaves it out, and the schema refuses another trigger next to it, as
Databricks Jobs refuses a continuous trigger next to any other.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import jsonschema
import pytest
import yaml

from fluid_build.schema_manager import format_schema_error
from fluid_build.util.contract import is_continuous_build, is_unscheduled_build

SCHEMAS = Path(__file__).resolve().parent.parent / "fluid_build" / "schemas"


def _connector(trigger: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "ingest",
        "engine": "kafka-connect",
        "pattern": "acquisition",
        "execution": {"trigger": trigger},
    }


def _contract(fluid_version: str = "0.7.6", **trigger: Any) -> Dict[str, Any]:
    """The requester's Flink job, with a continuous trigger."""
    return {
        "fluidVersion": fluid_version,
        "kind": "DataProduct",
        "id": "network.service-instances-full",
        "name": "Service instances (reconciled)",
        "metadata": {"owner": {"team": "data-engineering"}},
        "builds": [
            {
                "id": "reconcile",
                "engine": "flink" if fluid_version == "0.7.6" else "sql",
                "pattern": "embedded-logic",
                "properties": {
                    "language": "flink_sql",
                    "sql": "INSERT INTO service_instances_full SELECT * FROM service_instances",
                    "parameters": {"parallelism": 2},
                },
                "execution": {
                    "trigger": {"type": "continuous", **trigger},
                    "runtime": {"platform": "kubernetes"},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "service_instances_full",
                "kind": "topic",
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "network.service-instances-full"},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ],
    }


def _errors(version: str, contract: Dict[str, Any]) -> list:
    schema = json.loads((SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    return [format_schema_error(e) for e in validator.iter_errors(contract)]


class TestSchema:
    def test_0_7_6_accepts_a_continuous_trigger(self) -> None:
        assert _errors("0.7.6", _contract()) == []

    def test_0_7_5_is_unchanged(self) -> None:
        # 0.7.6 is the preview; the stable version does not move.
        assert any("'continuous'" in msg for msg in _errors("0.7.5", _contract("0.7.5")))

    @pytest.mark.parametrize(
        "field, value, remedy",
        [
            ("cron", "0 2 * * *", "set type: schedule"),
            ("schedule", "@daily", "set type: schedule"),
            ("datasets", ["dataset://kafka/network.service-instances"], "set type: dataset"),
            ("event", "inventory-updated", "set type: event"),
            ("timetable", {"kind": "business-hours"}, "set type: timetable"),
        ],
    )
    def test_another_trigger_beside_it_is_refused(
        self, field: str, value: Any, remedy: str
    ) -> None:
        # Left in, a scheduler that reads the field alone would start a
        # second copy of a job that is already running.
        assert _errors("0.7.6", _contract(**{field: value})) == [
            f"{field} is not allowed with type continuous: a continuous job runs all the "
            f"time, so its trigger takes no other trigger. Remove {field}, or {remedy}."
        ]

    @pytest.mark.parametrize("trigger", [None, "@daily", ["0 2 * * *"]])
    def test_a_trigger_that_is_not_an_object_gets_one_error(self, trigger: Any) -> None:
        # A blank `trigger:` in YAML is null; it used to draw five refusals
        # of a continuous trigger nobody declared.
        contract = _contract()
        contract["builds"][0]["execution"]["trigger"] = trigger
        assert [m for m in _errors("0.7.6", contract) if "continuous" in m] == []
        assert len(_errors("0.7.6", contract)) == 1

    @pytest.mark.parametrize(
        "trigger",
        [
            pytest.param({"type": "schedule", "cron": "0 2 * * *"}, id="schedule-cron"),
            pytest.param({"cron": "0 2 * * *"}, id="no-type-cron"),
            pytest.param({"type": "dataset", "datasets": ["dataset://x"]}, id="dataset"),
        ],
    )
    def test_other_trigger_types_keep_their_fields(self, trigger: Dict[str, Any]) -> None:
        contract = _contract()
        contract["builds"][0]["execution"]["trigger"] = trigger
        assert _errors("0.7.6", contract) == []


@pytest.mark.parametrize(
    "build, continuous, unscheduled",
    [
        pytest.param(_connector({"type": "continuous"}), True, True, id="continuous"),
        pytest.param(_connector({"type": " Continuous "}), True, True, id="case-and-space"),
        pytest.param(_connector({"type": "schedule"}), False, False, id="schedule"),
        pytest.param({"id": "x", "engine": "flink"}, False, True, id="flink-no-trigger"),
        pytest.param({"id": "x", "execution": None}, False, False, id="null-execution"),
        pytest.param({"id": "x", "execution": {"trigger": "continuous"}}, False, False, id="str"),
    ],
)
def test_classification(build: Dict[str, Any], continuous: bool, unscheduled: bool) -> None:
    assert is_continuous_build(build) is continuous
    assert is_unscheduled_build(build) is unscheduled


def test_a_scheduler_gets_no_task_for_a_continuous_connector() -> None:
    # A Kafka Connect connector used to get a nightly 02:00 task.
    from fluid_build.schedulers.synthesis import synthesize_orchestration_from_builds

    contract = {"id": "p", "builds": [_connector({"type": "continuous"})]}
    assert synthesize_orchestration_from_builds(copy.deepcopy(contract), "airflow") == {}
    contract["builds"].append({"id": "daily", "engine": "sql", "properties": {"sql": "SELECT 1"}})
    tasks = synthesize_orchestration_from_builds(contract, "airflow")["tasks"]
    assert [t["taskId"] for t in tasks] == ["daily"]


@pytest.mark.parametrize("kind, scheduled", [("continuous", False), ("schedule", True)])
def test_a_continuous_build_gets_no_fluid_apply_dag(kind: str, scheduled: bool) -> None:
    # With a cron beside it, as a contract that skipped validation can have.
    from fluid_build.schedulers.airflow.fluid_apply import has_scheduled_builds

    contract = {"builds": [_connector({"type": kind, "cron": "0 2 * * *"})]}
    assert has_scheduled_builds(contract) is scheduled


def test_schedule_sync_writes_nothing_for_a_continuous_connector(tmp_path: Path) -> None:
    # This emitter reads ``schedule`` alone; the schema refuses the pair, but
    # a contract that skipped validation must still not start a second copy.
    from fluid_build.cli._acquisition_stage_ext import schedule_sync_acquisition

    contract = {"id": "p", "builds": [_connector({"type": "continuous", "schedule": "0 2 * * *"})]}
    assert schedule_sync_acquisition(contract, tmp_path) == []
    contract["builds"][0]["execution"]["trigger"]["type"] = "schedule"
    assert schedule_sync_acquisition(contract, tmp_path)


def _run_cli(*argv: str, cwd: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *argv],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")},
    )


class TestCli:
    def test_validate_accepts_a_continuous_trigger(self, tmp_path: Path) -> None:
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(_contract()), "utf-8")
        result = _run_cli("validate", "contract.fluid.yaml", "--offline", "--strict", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_validate_names_the_field_and_the_fix(self, tmp_path: Path) -> None:
        (tmp_path / "contract.fluid.yaml").write_text(
            yaml.safe_dump(_contract(cron="0 2 * * *")), "utf-8"
        )
        result = _run_cli("validate", "contract.fluid.yaml", "--offline", cwd=tmp_path)
        assert result.returncode == 1
        out = " ".join(result.stdout.split())
        assert "builds[0].execution.trigger: cron is not allowed with type continuous" in out
        assert "Remove cron, or set type: schedule." in out

    def test_generate_schedule_has_nothing_to_schedule(self, tmp_path: Path) -> None:
        contract = _contract()
        contract["builds"] = [_connector({"type": "continuous"})]
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")
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
        assert "continuously (trigger.type: continuous)" in " ".join(result.stdout.split())
        assert not (tmp_path / "sched").exists()


# ── a continuous trigger on a build forge-cli runs as a batch run ──────────

_SQL = {"sql": "SELECT 1"}
_REMEDY = "Use trigger.type schedule, or run the job on a platform that keeps it running."


def _build(engine: Any = None, trigger: str = "continuous", **extra: Any) -> Dict[str, Any]:
    build: Dict[str, Any] = {"id": "b", "execution": {"trigger": {"type": trigger}}, **extra}
    if engine is not None:
        build["engine"] = engine
    return build


def _warnings(contract: Dict[str, Any]) -> list:
    from fluid_build.build_runners.base import continuous_trigger_warnings

    return continuous_trigger_warnings(contract)


class TestBatchBuildWarning:
    """A continuous trigger says the job's platform keeps it running, and no
    scheduler starts the build; forge-cli runs dbt, python, inline-SQL and batch
    acquisition builds to completion, so ``fluid validate`` warns."""

    def test_a_dbt_build_is_never_run(self) -> None:
        assert _warnings({"builds": [_build("dbt")]}) == [
            "builds[0] (id 'b'): trigger.type continuous on engine 'dbt': forge-cli runs dbt "
            "builds as batch runs, which end, and its dbt runner refuses a continuous trigger: "
            f"forge-cli never runs this build. {_REMEDY}"
        ]

    def test_a_duckdb_acquisition_runs_once_per_apply(self) -> None:
        assert _warnings({"builds": [_build("duckdb", pattern="acquisition")]}) == [
            "builds[0] (id 'b'): trigger.type continuous on engine 'duckdb': forge-cli runs "
            "duckdb builds as batch runs, which end: this build runs once per fluid apply, and "
            f"no scheduler starts it. {_REMEDY}"
        ]

    @pytest.mark.parametrize(
        "build, says",
        [
            pytest.param(_build("dbt-snowflake"), "its dbt runner refuses", id="dbt-adapter"),
            pytest.param(_build(" Python "), "its python runner refuses", id="python"),
            pytest.param(_build("sql", properties=_SQL), "inline-SQL builds", id="sql"),
            pytest.param(
                _build(pattern="embedded-logic", properties=_SQL),
                "on a build with no engine: forge-cli runs inline-SQL builds",
                id="inline-sql-no-engine",
            ),
            # The runner takes inline SQL ahead of dbt, so the build runs on DuckDB.
            pytest.param(
                _build("dbt", pattern="embedded-logic", properties=_SQL),
                "on engine 'dbt': forge-cli runs inline-SQL builds",
                id="dbt-with-inline-sql",
            ),
            *[
                pytest.param(_build(e, pattern="acquisition"), f"runs {e} builds", id=e)
                for e in ("airbyte", "meltano", "dlt")
            ],
        ],
    )
    def test_every_batch_runner_is_warned(self, build: Dict[str, Any], says: str) -> None:
        (warning,) = _warnings({"builds": [build]})
        assert says in warning
        assert warning.endswith(_REMEDY)

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(_build("kafka-connect", pattern="acquisition"), id="kafka-connect"),
            pytest.param(_build("debezium", pattern="acquisition"), id="debezium"),
            pytest.param(_build("flink", properties=_SQL), id="flink"),
            pytest.param(
                _build("sql", properties={"language": "flink_sql", **_SQL}), id="flink-sql"
            ),
            # No runner of its own: a Spark or Glue streaming job can run on
            # its own platform.
            pytest.param(_build("spark"), id="spark"),
            pytest.param(_build("glue"), id="glue"),
            pytest.param(_build("custom"), id="custom"),
            pytest.param(_build(), id="no-engine-no-sql"),
            pytest.param(_build("dbt", trigger="schedule"), id="schedule"),
            pytest.param({"id": "b", "engine": "dbt"}, id="no-trigger"),
        ],
    )
    def test_no_warning(self, build: Dict[str, Any]) -> None:
        assert _warnings({"builds": [build]}) == []

    @pytest.mark.parametrize(
        "contract",
        [
            pytest.param({"builds": "dbt"}, id="builds-str"),
            pytest.param({"builds": None}, id="builds-null"),
            pytest.param({"builds": ["dbt", None, 3]}, id="build-not-object"),
            pytest.param({"builds": [_build(5)]}, id="engine-int"),
            pytest.param({"builds": [_build("sql", pattern=["x"])]}, id="pattern-list"),
            pytest.param(
                {"builds": [_build(pattern="embedded-logic", properties=["SELECT 1"])]},
                id="properties-list",
            ),
            pytest.param({"builds": [{"id": "b", "engine": "dbt", "execution": "x"}]}, id="exec"),
        ],
    )
    def test_any_shape_is_read_without_raising(self, contract: Dict[str, Any]) -> None:
        # fluid validate runs its contract rules on a contract the schema refused too.
        assert _warnings(contract) == []

    def test_a_build_without_an_id_is_named_by_index(self) -> None:
        build = _build("python")
        del build["id"]
        (warning,) = _warnings({"builds": [_build("spark"), build]})
        assert warning.startswith("builds[1]: trigger.type continuous on engine 'python':")


def test_streaming_engines_are_the_runners_that_declare_streaming() -> None:
    # The kafka-connect and debezium runners leave a connector running; every
    # other acquisition runner runs to completion. Read from the runners, so a
    # new streaming runner cannot be warned about by mistake.
    from fluid_build.api.runner import RunnerCapability
    from fluid_build.build_runners.base import (
        ACQUISITION_ENGINES,
        STREAMING_ACQUISITION_ENGINES,
        _declared_capabilities_for_engine,
    )

    declared = {engine: _declared_capabilities_for_engine(engine) for engine in ACQUISITION_ENGINES}
    assert all(caps is not None for caps in declared.values()), declared
    assert STREAMING_ACQUISITION_ENGINES == {
        engine
        for engine, caps in declared.items()
        if caps is not None and RunnerCapability.STREAMING.value in caps
    }


class TestValidateCli:
    @staticmethod
    def _write(tmp_path: Path) -> None:
        contract = _contract()
        contract["builds"] = [
            {
                "id": "transform",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "repository": "./dbt",
                "execution": {"trigger": {"type": "continuous"}},
            }
        ]
        (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")

    def test_plain_validate_warns_and_passes(self, tmp_path: Path) -> None:
        self._write(tmp_path)
        result = _run_cli("validate", "contract.fluid.yaml", "--offline", cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        out = " ".join(result.stdout.split())
        assert "builds[0] (id 'transform'): trigger.type continuous on engine 'dbt'" in out

    def test_strict_fails(self, tmp_path: Path) -> None:
        self._write(tmp_path)
        result = _run_cli("validate", "contract.fluid.yaml", "--offline", "--strict", cwd=tmp_path)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "warnings treated as errors (--strict)" in result.stdout

    def test_json_lists_the_warning(self, tmp_path: Path) -> None:
        self._write(tmp_path)
        result = _run_cli(
            "validate", "contract.fluid.yaml", "--offline", "--format", "json", cwd=tmp_path
        )
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report["valid"] is True
        assert [w for w in report["warnings"] if "trigger.type continuous" in w]


class TestRunnerRefusal:
    def test_the_dbt_runner_names_the_trigger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from fluid_build.build_runners.dbt import runner

        def _not_reached(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a refused build must not prepare a dbt profile")

        monkeypatch.setattr(runner, "resolve_dbt_profiles_dir", _not_reached)
        assert runner.execute_dbt_build(_build("dbt"), tmp_path, tmp_path) == 1
        out = " ".join(capsys.readouterr().out.split())
        assert "trigger.type continuous: forge-cli runs dbt builds as batch runs" in out
        assert "Unknown trigger type" not in out

    def test_the_python_runner_names_the_trigger(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from fluid_build.build_runners.python import runner

        def _not_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a refused build must not run its script")

        script = tmp_path / "ingest.py"
        script.write_text("print('ran')\n", "utf-8")
        monkeypatch.setattr(runner.subprocess, "run", _not_run)
        assert runner.execute_build(_build("python"), script, tmp_path) == 1
        out = " ".join(capsys.readouterr().out.split())
        assert "trigger.type continuous: forge-cli runs a build's Python script" in out
        assert "Unknown trigger type" not in out

    def test_another_unknown_trigger_keeps_its_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        from fluid_build.build_runners.python import runner

        script = tmp_path / "ingest.py"
        script.write_text("print('ran')\n", "utf-8")
        assert runner.execute_build(_build("python", trigger="event"), script, tmp_path) == 1
        assert "Unknown trigger type: event" in capsys.readouterr().out
