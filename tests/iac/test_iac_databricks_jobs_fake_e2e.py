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

"""Databricks dbt jobs through the real ``fluid`` CLI, ``tofu`` and provider, against a stand-in.

Every command is the real ``fluid`` entry point, run in-process, against
``_fake_databricks.FakeDatabricks``: no Databricks account or credential.

Pinned (Stage 1: real ``tofu``, stand-in workspace): a dbt build that runs on
Databricks becomes one serverless job with a ``dbt_task``, and its local project
the workspace files dbt reads (never ``profiles.yml``, ``target/`` or a hidden
file); ``fluid plan`` lists the job; a second plan is empty; a changed model
updates one file in place; a GIT build runs from its tag on a continuous trigger
and uploads nothing; removing the job does not need ``--allow-data-loss`` and is
said; the table a dbt build writes keeps what dbt's CREATE OR REPLACE made of it,
and the apply reports that drift instead of failing on it.

Not proven here: that Databricks accepts and runs the job (the serverless
environment version, the dbt adapter range, the commands, a WORKSPACE
``project_directory`` under ``/Workspace``), or that the jobs API echoes no
server default that would make a second plan non-empty (``performance_target``
was one candidate): that is the manual live tier.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pytest

from fluid_build.iac import runner
from fluid_build.iac.naming import digest_ident

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    run_fluid,
    write_contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

TABLE = "main.sales.orders"
ROOT = (
    "/Users/fake-user@example.test/.fluid/"
    f"{digest_ident('dbx.fake.orders')}/{digest_ident('dbt_daily')}/files"
)
GIT = "https://github.com/example-org/orders-dbt"


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    project = tmp_path / "dbt"
    (project / "models").mkdir(parents=True)
    (project / "target").mkdir()
    (project / "dbt_project.yml").write_text("name: orders\nversion: '1.0'\nprofile: orders\n")
    (project / "models" / "orders.sql").write_text("select 1 as id\n")
    # dbt reads none of these from the workspace: never uploaded.
    (project / "profiles.yml").write_text("orders:\n  target: dev\n")
    (project / "target" / "manifest.json").write_text("{}")
    (project / "models" / ".scratch.sql").write_text("select 2\n")
    return tmp_path


def job_contract(
    *,
    repository: str = "./dbt",
    trigger: Optional[Dict[str, Any]] = None,
    platform: Optional[str] = "databricks",
) -> Dict[str, Any]:
    document = make_contract()
    execution: Dict[str, Any] = {"trigger": trigger or {"type": "schedule", "cron": "0 2 * * *"}}
    if platform:
        execution["runtime"] = {"platform": platform}
    document["builds"] = [
        {
            "id": "dbt_daily",
            "engine": "dbt",
            "pattern": "hybrid-reference",
            "repository": repository,
            "properties": {"model": "orders", "select": "orders+"},
            "outputs": ["orders"],
            "execution": execution,
        }
    ]
    return document


def _only_job(fake: FakeDatabricks) -> Dict[str, Any]:
    [job] = fake.jobs.values()
    return job


def test_a_dbt_build_runs_as_a_serverless_job_with_its_project_uploaded(workspace, fake, capfd):
    path = write_contract(workspace, job_contract())

    rc, out = run_fluid(capfd, "plan", path, "--out", "plan.json")
    assert rc == 0, out
    plan = json.loads((workspace / "plan.json").read_text())
    scheduled = [
        action
        for action in plan.get("actions") or []
        if action.get("action_type") == "scheduleTask"
    ]
    assert [action["params"]["buildId"] for action in scheduled] == ["dbt_daily"], scheduled

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "also need USE CATALOG on catalog main" in out
    settings = _only_job(fake)["settings"]
    [task] = settings["tasks"]
    assert task["dbt_task"] == {
        "source": "WORKSPACE",
        "project_directory": f"/Workspace{ROOT}",
        "commands": ["dbt deps", "dbt build --select orders+"],
        "warehouse_id": fake.warehouse_id,
        "catalog": "main",
        "schema": "sales",
    }
    assert settings["schedule"] == {
        "quartz_cron_expression": "0 0 2 * * ?",
        "timezone_id": "UTC",
        "pause_status": "UNPAUSED",
    }
    [environment] = settings["environments"]
    assert environment["spec"] == {
        "environment_version": "2",
        "dependencies": ["dbt-databricks>=1.8.0,<2.0.0"],
    }
    assert settings["tags"] == {"fluid_product": "dbx.fake.orders", "fluid_build": "dbt_daily"}
    assert sorted(fake.workspace_files) == [f"{ROOT}/dbt_project.yml", f"{ROOT}/models/orders.sql"]
    assert fake.file_content(f"{ROOT}/models/orders.sql") == b"select 1 as id\n"

    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_a_changed_model_updates_its_file_in_place(workspace, fake, capfd):
    path = write_contract(workspace, job_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    job_id = _only_job(fake)["job_id"]

    (workspace / "dbt" / "models" / "orders.sql").write_text("select 2 as id\n")
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0 and "tofu plan: +0 ~1 -0" in out, out
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert fake.file_content(f"{ROOT}/models/orders.sql") == b"select 2 as id\n"
    assert _only_job(fake)["job_id"] == job_id


def test_a_git_build_runs_its_tag_continuously_and_uploads_nothing(workspace, fake, capfd):
    contract = job_contract(
        repository=f"{GIT}?tag=v1.2.0&path=transform", trigger={"type": "continuous"}
    )
    path = write_contract(workspace, contract)
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    settings = _only_job(fake)["settings"]
    assert settings["git_source"] == {"git_url": GIT, "git_provider": "gitHub", "git_tag": "v1.2.0"}
    assert settings["continuous"] == {"pause_status": "UNPAUSED"}
    assert "schedule" not in settings
    dbt_task = settings["tasks"][0]["dbt_task"]
    assert dbt_task["source"] == "GIT" and dbt_task["project_directory"] == "transform"
    assert fake.workspace_files == {}
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_removing_the_job_is_not_gated_and_is_said(workspace, fake, capfd):
    rc, out = apply_contract(capfd, write_contract(workspace, job_contract()))
    assert rc == 0, out
    # The build now runs locally: its job and uploaded project go; its table stays.
    rc, out = apply_contract(capfd, write_contract(workspace, job_contract(platform=None)))
    assert rc == 0, out
    assert "job(s) or workspace file(s) removed (no table data lost; not gated" in out
    assert fake.jobs == {} and fake.workspace_files == {}
    assert TABLE in fake.tables


def test_the_table_a_dbt_build_writes_keeps_what_dbt_made_and_the_drift_is_said(
    workspace, fake, capfd
):
    path = write_contract(workspace, job_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    # dbt's CREATE OR REPLACE typed amount from its SELECT and added a column.
    fake.tables[TABLE]["columns"] = [
        {"name": "id", "type": "bigint", "nullable": False},
        {"name": "amount", "type": "decimal(38,6)", "nullable": True},
        {"name": "region", "type": "string", "nullable": True},
        {"name": "loaded_at", "type": "timestamp", "nullable": True},
    ]
    marker = fake.tables[TABLE]["marker"]
    statements = len(fake.sql_log)

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "column drift NOT reconciled on main.sales.orders" in out
    assert "amount: contract=double live=decimal(38,6)" in out
    assert "loaded_at: present in the live table but not declared" in out
    assert fake.sql_log[statements:] == [] and fake.tables[TABLE]["marker"] == marker
