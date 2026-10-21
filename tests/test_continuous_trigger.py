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


def test_a_continuous_build_gets_no_fluid_apply_dag() -> None:
    from fluid_build.schedulers.airflow.fluid_apply import has_scheduled_builds

    assert has_scheduled_builds({"builds": [_connector({"type": "continuous"})]}) is False


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
