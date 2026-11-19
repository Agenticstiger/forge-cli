# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply`` runs a build whatever its ``execution.trigger.type``.

``fluid generate schedule`` renders a DAG whose task is ``fluid apply
--build-id <id>`` for every type a scheduler or an event starts (schema 0.7.7),
so the dbt and python runners run such a build once when apply asks
(``force_run``), and leave it to its scheduler otherwise. ``manual`` keeps its
iterations; ``continuous`` stays refused; a type the schema does not name is an
error naming the ones it does.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from fluid_build.build_runners.dbt import runner as dbt_runner
from fluid_build.build_runners.python import runner as python_runner
from fluid_build.util.contract import SCHEDULER_TRIGGER_TYPES, trigger_type_of

pytestmark = [pytest.mark.unit]

SCHEMA = Path(__file__).resolve().parents[2] / "fluid_build" / "schemas" / "fluid-schema-0.7.7.json"


def _out(capsys: pytest.CaptureFixture) -> str:
    """Captured output with Rich's line wrapping undone."""
    return " ".join(capsys.readouterr().out.split())


def _build(trigger: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": "hourly-marts", "engine": "dbt-duckdb", "execution": {"trigger": trigger}}


@pytest.fixture
def dbt_runs(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Every dbt command the runner starts; nothing real runs."""
    runs: List[List[str]] = []

    def _run(cmd: List[str], **_kwargs: Any) -> Any:
        runs.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dbt_runner, "_load_dbt_project_config", lambda *_a: {})
    monkeypatch.setattr(dbt_runner, "resolve_dbt_profiles_dir", lambda *_a: (None, None))
    monkeypatch.setattr(dbt_runner, "build_dbt_command", lambda *_a, **_k: ["dbt", "build"])
    monkeypatch.setattr(dbt_runner, "_persist_dbt_run_record", lambda *_a, **_k: None)
    monkeypatch.setattr(dbt_runner.subprocess, "run", _run)
    return runs


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "ingest.py"
    path.write_text("print('ran')\n", encoding="utf-8")
    return path


def test_the_set_is_every_schema_type_but_manual_and_continuous() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    types = set(schema["$defs"]["trigger"]["properties"]["type"]["enum"])
    assert SCHEDULER_TRIGGER_TYPES == types - {"manual", "continuous"}


@pytest.mark.parametrize(
    "trigger, expected",
    [({}, "manual"), ({"type": " Dataset "}, "dataset"), ({"type": None}, "manual")],
)
def test_the_type_is_read_lower_cased(trigger: Dict[str, Any], expected: str) -> None:
    assert trigger_type_of({"execution": {"trigger": trigger}}) == expected


class TestDbtRunner:
    @pytest.mark.parametrize("kind", sorted(SCHEDULER_TRIGGER_TYPES))
    def test_apply_runs_a_scheduled_build_once(
        self, kind: str, tmp_path: Path, dbt_runs: List[List[str]], capsys
    ) -> None:
        trigger = {"type": kind, "cron": "0 * * * *", "iterations": 5}
        rc = dbt_runner.execute_dbt_build(
            _build(trigger), tmp_path, tmp_path, delay=0, force_run=True
        )
        assert rc == 0
        assert dbt_runs == [["dbt", "build"]]
        out = _out(capsys)
        assert f"Trigger: {kind} (manual apply override)" in out
        assert "Unknown trigger type" not in out

    @pytest.mark.parametrize("kind", ["schedule_and_dataset", "dataset", "event"])
    def test_without_apply_its_scheduler_runs_it(
        self, kind: str, tmp_path: Path, dbt_runs: List[List[str]], capsys
    ) -> None:
        rc = dbt_runner.execute_dbt_build(_build({"type": kind}), tmp_path, tmp_path, delay=0)
        assert rc == 0
        assert dbt_runs == []
        assert f"trigger.type {kind} means a scheduler starts this build" in _out(capsys)

    def test_manual_keeps_its_iterations(self, tmp_path: Path, dbt_runs: List[List[str]]) -> None:
        build = _build({"type": "manual", "iterations": 2, "delaySeconds": 0})
        assert dbt_runner.execute_dbt_build(build, tmp_path, tmp_path, force_run=True) == 0
        assert len(dbt_runs) == 2

    def test_a_type_the_schema_does_not_name_is_an_error(
        self, tmp_path: Path, dbt_runs: List[List[str]], capsys
    ) -> None:
        rc = dbt_runner.execute_dbt_build(
            _build({"type": "nightly"}), tmp_path, tmp_path, force_run=True
        )
        assert rc == 1
        assert dbt_runs == []
        out = _out(capsys)
        assert "Unknown trigger type: nightly" in out
        assert "schedule_and_dataset" in out


class TestPythonRunner:
    @pytest.mark.parametrize("kind", ["schedule_and_dataset", "dataset", "timetable"])
    def test_apply_runs_a_scheduled_build_once(
        self, kind: str, tmp_path: Path, script: Path, monkeypatch, capsys
    ) -> None:
        runs: List[List[str]] = []

        def _run(cmd: List[str], **_kwargs: Any) -> Any:
            runs.append(cmd)
            return SimpleNamespace(returncode=0, stdout="ran\n", stderr="")

        monkeypatch.setattr(python_runner.subprocess, "run", _run)
        build = {"id": "py", "execution": {"trigger": {"type": kind, "iterations": 3}}}
        rc = python_runner.execute_build(build, script, tmp_path, delay=0, force_run=True)
        assert rc == 0
        assert len(runs) == 1
        assert f"Trigger: {kind} (manual apply override)" in _out(capsys)

    def test_without_apply_its_scheduler_runs_it(
        self, tmp_path: Path, script: Path, monkeypatch, capsys
    ) -> None:
        def _not_run(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("a build its scheduler starts must not run here")

        monkeypatch.setattr(python_runner.subprocess, "run", _not_run)
        build = {"id": "py", "execution": {"trigger": {"type": "event"}}}
        assert python_runner.execute_build(build, script, tmp_path) == 0
        assert "trigger.type event means a scheduler starts this build" in _out(capsys)
