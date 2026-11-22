# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid schedule-sync`` and the one-slot pools of the writers of an output table.

A generated DAG lists its pools in ``FLUID_TABLE_POOLS``. With ``--airflow-url`` the sync
makes the ones Airflow lacks before it pushes anything, on Airflow 2 (``/api/v1/pools``) and
Airflow 3 (``/api/v2/pools``), and never changes one that exists; without it the report prints
the ``airflow pools set`` lines. A fake Airflow answers on 127.0.0.1; ``rsync`` is stubbed.
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
OTHER_TABLE = "s3://lake/iceberg/curated/users"
OTHER = airflow_pools.pool_name(OTHER_TABLE)
OTHER_DESCRIPTION = airflow_pools.pool_description(OTHER_TABLE)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv(TOKEN_REF_ENV, raising=False)
    monkeypatch.setenv(CREDENTIAL_REF_ENV, "env://AF_TEST_CREDENTIAL")
    monkeypatch.setenv("AF_TEST_CREDENTIAL", "admin:admin")


def _dag_dir(tmp_path: Path, pools: dict) -> Path:
    path = tmp_path / "schedule" / "sales__dev" / f"{DAG_ID}.py"
    path.parent.mkdir(parents=True)
    body = "".join(f"    {name!r}: {text!r},\n" for name, text in pools.items())
    path.write_text(
        "from airflow.sdk import DAG\n\n"
        f"FLUID_TABLE_POOLS = {{\n{body}}}\n\n"
        f"with DAG(dag_id={DAG_ID!r}, schedule='@daily') as dag:\n    pass\n",
        encoding="utf-8",
    )
    return tmp_path / "schedule"


@pytest.fixture()
def dags_dir(tmp_path: Path) -> Path:
    return _dag_dir(tmp_path, {POOL: DESCRIPTION})


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
    doc = json.loads(report.read_text(encoding="utf-8")) if report.exists() else None
    return rc, out, doc, recorder


def _pool_posts(fake: FakeAirflow) -> List[str]:
    return [p for p in fake.paths() if p.startswith("POST") and p.endswith("/pools")]


@pytest.mark.parametrize("major, api", [(2, "v1"), (3, "v2")])
def test_a_missing_pool_is_created_with_one_slot_before_anything_is_pushed(
    major, api, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(major) as fake:
        rc, out, doc, recorder = _push(dags_dir, fake, tmp_path, monkeypatch, capsys)
        assert fake.pools[POOL] == {"slots": 1, "description": DESCRIPTION}
        assert _pool_posts(fake) == [f"POST /api/{api}/pools"]
        before_push = [
            r.method + " " + r.path for r in fake.requests[: recorder.requests_at_first_call]
        ]
    assert rc == 0, out
    assert f"POST /api/{api}/pools" in before_push
    assert doc["airflow_check"]["pools"] == {"result": "passed", "created": [POOL], "existing": []}
    assert f"pool {POOL} created with 1 slot" in out


@pytest.mark.parametrize("major", [2, 3])
def test_a_pool_that_exists_is_left_as_it_is(
    major, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(major) as fake:
        fake.pools[POOL] = {"slots": 5, "description": "made by hand"}
        rc, out, doc, _ = _push(dags_dir, fake, tmp_path, monkeypatch, capsys)
        assert fake.pools[POOL] == {"slots": 5, "description": "made by hand"}
        assert _pool_posts(fake) == [] and not any(m.startswith("PATCH") for m in fake.paths())
    assert rc == 0, out
    assert doc["airflow_check"]["pools"]["existing"] == [{"name": POOL, "slots": 5}]
    assert "with 5 slots; left as it is" in out


@pytest.mark.parametrize("major, api", [(2, "v1"), (3, "v2")])
def test_a_pool_that_cannot_be_made_stops_the_sync_before_the_push(
    major, api, dags_dir: Path, tmp_path: Path, monkeypatch, capsys
):
    with FakeAirflow(major, forbidden=(f"/api/{api}/pools",)) as fake:
        rc, out, doc, recorder = _push(dags_dir, fake, tmp_path, monkeypatch, capsys)
    assert rc != 0, out
    assert recorder.argvs == []  # no DAG was pushed, so none points at a missing pool
    assert COMMAND in out and "nothing was pushed" in out
    assert doc is None


@pytest.mark.parametrize("major", [2, 3])
def test_the_dry_run_says_which_pools_exist_and_which_it_would_create(
    major, tmp_path: Path, capsys
):
    dags = _dag_dir(tmp_path, {POOL: DESCRIPTION, OTHER: OTHER_DESCRIPTION})
    with FakeAirflow(major) as fake:
        fake.pools[POOL] = {"slots": 1, "description": "already there"}
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            rc, out = _main(
                _base(dags, fake, "--destination", str(tmp_path / "d"), "--dry-run"), capsys
            )
        assert rc == 0, out
        assert [p for p in fake.paths() if p.endswith("/pools")] == [
            f"GET /api/{'v1' if major == 2 else 'v2'}/pools"
        ]
        assert _pool_posts(fake) == [] and set(fake.pools) == {"default_pool", POOL}
    assert f"pools: 1 exist ({POOL}), would create 1 with 1 slot ({OTHER})" in out


def test_the_dry_run_says_so_when_it_cannot_read_the_pools(tmp_path: Path, monkeypatch, capsys):
    dags = _dag_dir(tmp_path, {POOL: DESCRIPTION})
    monkeypatch.setenv("AF_TEST_CREDENTIAL", "admin:wrong")
    with FakeAirflow(3) as fake:
        with patch.object(schedule_sync, "_which_or_raise", side_effect=_which):
            rc, out = _main(
                _base(dags, fake, "--destination", str(tmp_path / "d"), "--dry-run"), capsys
            )
    assert rc == 0, out
    assert "the pools were not read" in out and POOL in out


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


def test_check_only_touches_no_pool(dags_dir: Path, monkeypatch, capsys):
    with FakeAirflow(3) as fake:
        _drive(monkeypatch, fake, _live(fake, dags_dir))
        rc, out = _main(_base(dags_dir, fake, "--check-only"), capsys)
        assert rc == 0, out
        assert not any(p.endswith("/pools") for p in fake.paths())
