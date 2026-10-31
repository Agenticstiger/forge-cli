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

"""A dbt build that runs in a Databricks workspace as a job is not run here.

``fluid apply`` provisions the job (``iac/providers/databricks_jobs.py``) and
Databricks runs dbt on its trigger, so the build runner reports the build and
neither runs it nor warns about a continuous trigger. A local dbt build is
unchanged. Driven the way ``tests/test_flink_engine.py`` drives an externally
run build; no dbt, no network.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.build_runners.base import (
    _batch_runner_for,
    continuous_trigger_warnings,
    run_builds_from_args,
)
from fluid_build.util.contract import is_databricks_job_build

pytestmark = pytest.mark.unit

JOB_LINE = "dbt runs in the Databricks workspace as job 'fluid sales.orders transform'"


def _build(
    *,
    platform: str = "databricks",
    engine: str = "dbt",
    trigger: str = "",
    build_id: str = "transform",
) -> Dict[str, Any]:
    execution: Dict[str, Any] = {}
    if platform:
        execution["runtime"] = {"platform": platform}
    if trigger:
        execution["trigger"] = {"type": trigger}
    build: Dict[str, Any] = {
        "id": build_id,
        "engine": engine,
        "repository": "https://github.com/o/r?tag=v1",
        "outputs": ["orders"],
        "properties": {"model": "orders"},
    }
    if execution:
        build["execution"] = execution
    return build


def _contract(*builds: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "metadata": {"owner": {"team": "data-platform"}},
        "builds": list(builds),
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "orders"},
                },
                "contract": {"schema": [{"name": "id", "type": "bigint"}]},
            }
        ],
    }


def _args(contract_path: Path, *, dry_run: bool = False) -> argparse.Namespace:
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


@pytest.fixture
def no_local_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Databricks job build must reach neither the dbt nor the python runner."""

    def _must_not_run(*_a: Any, **_k: Any) -> int:
        raise AssertionError("a Databricks job build reached a local runner")

    monkeypatch.setattr("fluid_build.build_runners.dbt.runner.execute_dbt_build", _must_not_run)
    monkeypatch.setattr("fluid_build.build_runners.python.runner.execute_build", _must_not_run)


def _flat(capsys: pytest.CaptureFixture) -> str:
    """Captured output with the console's line wrapping undone."""
    return " ".join(capsys.readouterr().out.split())


# ── classification ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "build, expected",
    [
        pytest.param(_build(), True, id="dbt-databricks-platform"),
        pytest.param(_build(engine="dbt-databricks"), True, id="dbt-databricks-engine"),
        pytest.param(_build(engine=" DBT "), True, id="engine-case-and-space"),
        pytest.param(_build(platform=" Databricks "), True, id="platform-case-and-space"),
        pytest.param(_build(platform=""), False, id="no-platform"),
        pytest.param(_build(platform="snowflake"), False, id="other-platform"),
        pytest.param(_build(engine="python"), False, id="python"),
        pytest.param(_build(engine="spark"), False, id="spark-on-databricks"),
        pytest.param({"engine": "dbt", "execution": {"runtime": "databricks"}}, False, id="shape"),
        pytest.param({"engine": 7, "execution": "x"}, False, id="malformed"),
    ],
)
def test_is_databricks_job_build(build: Dict[str, Any], expected: bool) -> None:
    assert is_databricks_job_build(build) is expected


# ── the batch-runner map ────────────────────────────────────────────────────
class TestBatchRunner:
    @pytest.mark.parametrize("trigger", ["", "schedule", "continuous"])
    def test_no_batch_runner_takes_a_databricks_job_build(self, trigger: str) -> None:
        assert _batch_runner_for(_build(trigger=trigger)) is None

    def test_a_dbt_adapter_build_on_databricks_is_a_job_too(self) -> None:
        assert _batch_runner_for(_build(engine="dbt-snowflake")) is None

    def test_a_local_dbt_build_still_has_the_dbt_runner(self) -> None:
        assert _batch_runner_for(_build(platform="")) == ("dbt builds", "dbt")
        assert _batch_runner_for(_build(platform="duckdb")) == ("dbt builds", "dbt")


class TestContinuousTriggerWarnings:
    def test_a_continuous_databricks_job_build_gets_none(self) -> None:
        contract = _contract(_build(trigger="continuous"))
        assert continuous_trigger_warnings(contract) == []

    def test_a_continuous_local_dbt_build_still_warns(self) -> None:
        (warning,) = continuous_trigger_warnings(
            _contract(_build(platform="", trigger="continuous"))
        )
        assert "builds[0]" in warning and "'transform'" in warning
        assert "trigger.type continuous" in warning
        assert "dbt runner refuses a continuous trigger" in warning

    def test_only_the_local_build_is_named_beside_a_databricks_one(self) -> None:
        contract = _contract(
            _build(trigger="continuous"),
            _build(platform="", trigger="continuous", build_id="local"),
        )
        (warning,) = continuous_trigger_warnings(contract)
        assert "builds[1]" in warning and "'local'" in warning

    def test_a_scheduled_local_dbt_build_does_not_warn(self) -> None:
        assert continuous_trigger_warnings(_contract(_build(platform="", trigger="schedule"))) == []


# ── run_builds_from_args ────────────────────────────────────────────────────
class TestRunBuilds:
    @pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "run"])
    def test_a_databricks_job_build_is_reported_not_run(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        no_local_run: None,
        dry_run: bool,
    ) -> None:
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(_contract(_build())), encoding="utf-8")

        # Nothing is missing, so the apply succeeds without --allow-skipped-builds.
        assert run_builds_from_args(_args(path, dry_run=dry_run), logging.getLogger("t")) == 0
        out = _flat(capsys)
        assert JOB_LINE in out
        assert "databricks jobs run-now" in out
        assert "Run outside forge-cli: 1" in out
        assert "Skipped: 0" in out
        assert "Executed: 0" in out

    def test_a_continuous_databricks_job_build_is_reported_the_same_way(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, no_local_run: None
    ) -> None:
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(_contract(_build(trigger="continuous"))), encoding="utf-8")
        assert run_builds_from_args(_args(path), logging.getLogger("t")) == 0
        out = _flat(capsys)
        assert JOB_LINE in out and "Run outside forge-cli: 1" in out

    def test_the_run_report_records_it_skipped_for_running_outside_forge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_local_run: None
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
        path.write_text(yaml.safe_dump(_contract(_build())), encoding="utf-8")
        assert run_builds_from_args(_args(path, dry_run=True), logging.getLogger("t")) == 0
        assert recorded == [
            {"build_id": "transform", "status": "skipped", "reason": "runs_outside_forge"}
        ]

    def test_a_databricks_build_that_writes_no_databricks_table_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, no_local_run: None
    ) -> None:
        # Bound elsewhere: no plugin provisions a job for it, so skipping it as
        # "provisioned" would be a silent no-op. It fails, naming the fix.
        contract = _contract(_build())
        contract["exposes"][0]["binding"] = {
            "platform": "aws",
            "format": "parquet",
            "location": {"bucket": "b", "path": "orders/"},
        }
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        assert run_builds_from_args(_args(path), logging.getLogger("t")) != 0
        out = _flat(capsys)
        assert "writes no Databricks expose" in out
        assert "databricks-job-no-target" in out
        assert JOB_LINE not in out

    def test_the_job_name_uses_the_product_id_and_the_build_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, no_local_run: None
    ) -> None:
        contract = _contract(_build(build_id="nightly"))
        contract["id"] = "finance.ledger"
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        assert run_builds_from_args(_args(path), logging.getLogger("t")) == 0
        assert "as job 'fluid finance.ledger nightly'" in _flat(capsys)

    def test_two_databricks_job_builds_are_both_counted_outside_forge(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, no_local_run: None
    ) -> None:
        contract = _contract(_build(), _build(build_id="second"))
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        assert run_builds_from_args(_args(path), logging.getLogger("t")) == 0
        out = _flat(capsys)
        assert "Run outside forge-cli: 2" in out
        assert "fluid sales.orders transform'" in out and "fluid sales.orders second'" in out

    def test_a_local_dbt_build_is_still_dispatched_to_the_dbt_runner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        ran: list = []

        def _fake_dbt(build: Dict[str, Any], *_a: Any, **_k: Any) -> int:
            ran.append(build["id"])
            return 0

        project = tmp_path / "dbt"
        project.mkdir()
        (project / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
        monkeypatch.setattr("fluid_build.build_runners.dbt.runner.execute_dbt_build", _fake_dbt)
        local = _build(platform="", build_id="local")
        local["repository"] = "./dbt"
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(_contract(_build(), local)), encoding="utf-8")

        assert run_builds_from_args(_args(path), logging.getLogger("t")) == 0
        out = _flat(capsys)
        assert ran == ["local"]  # the Databricks job build was not handed over
        assert JOB_LINE in out and "Run outside forge-cli: 1" in out
        assert "Executed: 1" in out
