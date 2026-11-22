# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid schedule-sync`` and the one-slot pools of the writers of an output table.

A generated DAG lists its pools in ``FLUID_TABLE_POOLS``. With ``--airflow-url`` the sync
creates the ones Airflow lacks, on Airflow 2 (``/api/v1/pools``) and Airflow 3
(``/api/v2/pools``), and never changes one that exists; without it the report prints the
``airflow pools set`` lines. A fake Airflow answers on 127.0.0.1; ``rsync`` is stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from fluid_build.cli import schedule_sync
from fluid_build.cli._airflow_api import CREDENTIAL_REF_ENV, TOKEN_REF_ENV
from fluid_build.util import airflow_pools
from tests.cli._fake_airflow import FakeAirflow
from tests.cli.test_schedule_sync_airflow import (
    DAG_ID,
    _base,
    _drive,
    _live,
    _main,
    _Recorder,
    _which,
)

pytestmark = pytest.mark.unit

TABLE = "s3://lake/iceberg/raw/events"
POOL = airflow_pools.pool_name(TABLE)
DESCRIPTION = airflow_pools.pool_description(TABLE)
COMMAND = f'airflow pools set {POOL} 1 "{DESCRIPTION}"'


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv(TOKEN_REF_ENV, raising=False)
    monkeypatch.setenv(CREDENTIAL_REF_ENV, "env://AF_TEST_CREDENTIAL")
    monkeypatch.setenv("AF_TEST_CREDENTIAL", "admin:admin")


@pytest.fixture()
def dags_dir(tmp_path: Path) -> Path:
    path = tmp_path / "schedule" / "sales__dev" / f"{DAG_ID}.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "from airflow.sdk import DAG\n\n"
        f"FLUID_TABLE_POOLS = {{\n    {POOL!r}: {DESCRIPTION!r},\n}}\n\n"
        f"with DAG(dag_id={DAG_ID!r}, schedule='@daily') as dag:\n    pass\n",
        encoding="utf-8",
    )
    return tmp_path / "schedule"


def _push(dags_dir: Path, fake: FakeAirflow, tmp_path: Path, monkeypatch, capsys, *extra: str):
    report = tmp_path / "report.json"
    recorder = _Recorder(fake)
    _drive(monkeypatch, fake, _live(fake, dags_dir))
    with (
        patch.object(schedule_sync, "_which_or_raise", side_effect=_which),
        patch.object(schedule_sync.subprocess, "run", side_effect=recorder),
    ):
        rc, out = _main(
            _base(
                dags_dir,
                fake,
                "--destination",
                str(tmp_path / "airflow" / "dags"),
                "--report",
                str(report),
                *extra,
            ),
            capsys,
        )
    return rc, out, json.loads(report.read_text(encoding="utf-8"))


def _pool_posts(fake: FakeAirflow) -> List[str]:
    return [p for p in fake.paths() if p.startswith("POST") and p.endswith("/pools")]


@pytest.mark.parametrize("major, api", [(2, "v1"), (3, "v2")])
def test_a_missing_pool_is_created_with_one_slot(
    major, api, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(major) as fake:
        rc, out, doc = _push(dags_dir, fake, tmp_path, monkeypatch, capsys)
        assert fake.pools[POOL] == {"slots": 1, "description": DESCRIPTION}
        assert _pool_posts(fake) == [f"POST /api/{api}/pools"]
    assert rc == 0, out
    assert doc["airflow_check"]["pools"] == {"result": "passed", "created": [POOL], "existing": []}
    assert f"pool {POOL} created with 1 slot" in out


@pytest.mark.parametrize("major", [2, 3])
def test_a_pool_that_exists_is_left_as_it_is(
    major, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(major) as fake:
        fake.pools[POOL] = {"slots": 5, "description": "made by hand"}
        rc, out, doc = _push(dags_dir, fake, tmp_path, monkeypatch, capsys)
        assert fake.pools[POOL] == {"slots": 5, "description": "made by hand"}
        assert _pool_posts(fake) == [] and not any(m.startswith("PATCH") for m in fake.paths())
    assert rc == 0, out
    assert doc["airflow_check"]["pools"]["existing"] == [{"name": POOL, "slots": 5}]
    assert "with 5 slots; left as it is" in out


@pytest.mark.parametrize("major, api", [(2, "v1"), (3, "v2")])
def test_a_user_who_may_not_create_pools_fails_the_check_and_gets_the_commands(
    major, api, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(major, forbidden=(f"/api/{api}/pools",)) as fake:
        rc, out, doc = _push(dags_dir, fake, tmp_path, monkeypatch, capsys)
    assert rc == 1, out
    pools = doc["airflow_check"]["pools"]
    assert pools["result"] == "failed" and pools["commands"] == [COMMAND]
    assert doc["airflow_check"]["result"] == "failed"
    assert COMMAND in out


def test_without_the_airflow_check_the_report_prints_the_pool_commands(
    dags_dir: Path, tmp_path: Path, capsys
):
    report = tmp_path / "r.json"
    with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
        rc, out = _main(
            [
                "schedule-sync",
                "--scheduler",
                "airflow",
                "--dags-dir",
                str(dags_dir),
                "--destination",
                str(tmp_path / "d"),
                "--dry-run",
                "--report",
                str(report),
            ],
            capsys,
        )
    assert rc == 0, out
    assert COMMAND in out
    assert json.loads(report.read_text(encoding="utf-8"))["pools"] == {"commands": [COMMAND]}


def test_a_dags_dir_without_pools_adds_nothing_to_the_report(tmp_path: Path, capsys):
    plain = tmp_path / "schedule" / "p"
    plain.mkdir(parents=True)
    (plain / "d.py").write_text("from airflow.sdk import DAG\n", encoding="utf-8")
    report = tmp_path / "r.json"
    with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
        rc, _ = _main(
            [
                "schedule-sync",
                "--scheduler",
                "airflow",
                "--dags-dir",
                str(tmp_path / "schedule"),
                "--destination",
                str(tmp_path / "d"),
                "--dry-run",
                "--report",
                str(report),
            ],
            capsys,
        )
    assert rc == 0
    assert "pools" not in json.loads(report.read_text(encoding="utf-8"))


def test_check_only_and_a_dry_run_create_no_pool(
    dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(3) as fake:
        _drive(monkeypatch, fake, _live(fake, dags_dir))
        rc, out = _main(_base(dags_dir, fake, "--check-only"), capsys)
        assert rc == 0, out
        assert _pool_posts(fake) == [] and not any(p.endswith("/pools") for p in fake.paths())
    with FakeAirflow(3) as fake:
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            rc, out = _main(
                _base(dags_dir, fake, "--destination", str(tmp_path / "d"), "--dry-run"), capsys
            )
        assert fake.requests == []
    assert rc == 0 and "would create 1 one-slot pool(s)" in out and POOL in out
