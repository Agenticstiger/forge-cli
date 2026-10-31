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

"""Databricks dbt jobs (Phase 2), Stage 1 unit: ``builds[]`` -> ``databricks_job`` + files.

Pure functions only: no ``tofu``, no network. The module docstring of
``iac/providers/databricks_jobs.py`` is the spec these tests pin.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.iac import IAC_PLUGINS, build_module
from fluid_build.iac.naming import TofuExpr, digest_ident, unique_ident
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_jobs as jobs
from fluid_build.iac.source import contract_source

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

WAREHOUSE = "wh-0123456789abcdef"
GIT = "https://github.com/o/r?tag=v1.2.0"
HOME = "${data.databricks_current_user.me.home}"
# One folder per product and build, never shared: ``sales.orders`` folds to a digest.
ROOT = f".fluid/{digest_ident('sales.orders')}/{digest_ident('transform')}/files"


def _set(target: Any, path: str, value: Any) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target.setdefault(key, {})
    if isinstance(target, list):
        target[int(keys[-1])] = value
    else:
        target[keys[-1]] = value


def _contract(repository: str = GIT, **overrides: Any) -> Dict[str, Any]:
    """One Databricks table, written by one dbt build that runs as a Databricks job."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "Main", "schema": "sales", "table": "orders"},
                },
                "contract": {"schema": [{"name": "id", "type": "bigint"}]},
            }
        ],
        "builds": [
            {
                "id": "transform",
                "engine": "dbt",
                "repository": repository,
                "outputs": ["orders"],
                "properties": {"model": "orders"},
                "execution": {"runtime": {"platform": "databricks"}},
            }
        ],
    }
    for path, value in overrides.items():
        _set(contract, path.replace("__", "."), value)
    return contract


def _compile(
    contract: Dict[str, Any],
    tmp_path: Optional[Path] = None,
    *,
    validating: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> dbx._Compiled:
    source = tmp_path / "contract.fluid.yaml" if tmp_path is not None else None
    with contract_source(source):
        return dbx._compile(
            contract, {dbx.WAREHOUSE_ENV: WAREHOUSE, **(env or {})}, validating=validating
        )


def _kinds(compiled: dbx._Compiled) -> List[str]:
    return [refusal.kind for refusal in compiled.refusals]


def _job(compiled: dbx._Compiled) -> Dict[str, Any]:
    assert not compiled.refusals, _kinds(compiled)
    (body,) = compiled.resources["databricks_job"].values()
    return body


def _task(compiled: dbx._Compiled) -> Dict[str, Any]:
    (task,) = _job(compiled)["task"]
    return task


def _refused(contract: Dict[str, Any], kind: str, tmp_path: Optional[Path] = None) -> None:
    compiled = _compile(contract, tmp_path)
    assert f"databricks-job-{kind}" in _kinds(compiled), _kinds(compiled)
    # A refused job emits no resource: the apply never gets half a job.
    assert "databricks_job" not in compiled.resources


def _project(tmp_path: Path, files: Optional[Dict[str, str]] = None, config: str = "") -> Path:
    """A local dbt project at ``tmp_path/dbt`` beside the contract."""
    root = tmp_path / "dbt"
    root.mkdir(exist_ok=True)
    (root / "dbt_project.yml").write_text(f"name: p\n{config}", encoding="utf-8")
    for rel, text in (files if files is not None else {"models/m.sql": "select 1\n"}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


# ── WORKSPACE source ────────────────────────────────────────────────────────
class TestWorkspaceJob:
    def _compiled(self, tmp_path: Path, **overrides: Any) -> dbx._Compiled:
        _project(tmp_path)
        return _compile(_contract("./dbt", **overrides), tmp_path)

    def test_commands_are_dbt_deps_then_dbt_build_with_the_runners_model_selector(self, tmp_path):
        # One output at most and properties.model: the runner's +model+.
        task = _task(self._compiled(tmp_path))
        assert task["dbt_task"]["commands"] == ["dbt deps", "dbt build --select +orders+"]

    def test_select_wins_over_models_which_wins_over_model(self, tmp_path):
        props = {"select": ["tag:a", "tag:b"], "models": "legacy", "model": "orders"}
        task = _task(self._compiled(tmp_path, builds__0__properties=props))
        assert task["dbt_task"]["commands"][1] == "dbt build --select tag:a tag:b"

        props = {"models": "legacy", "model": "orders"}
        task = _task(self._compiled(tmp_path, builds__0__properties=props))
        assert task["dbt_task"]["commands"][1] == "dbt build --select legacy"

    def test_model_selector_needs_at_most_one_output(self, tmp_path):
        contract = _contract("./dbt", builds__0__outputs=["orders", "orders2"])
        _project(tmp_path)
        task = _task(_compile(contract, tmp_path))
        assert task["dbt_task"]["commands"][1] == "dbt build"

    def test_no_selector_means_a_plain_dbt_build(self, tmp_path):
        task = _task(self._compiled(tmp_path, builds__0__properties={}))
        assert task["dbt_task"]["commands"] == ["dbt deps", "dbt build"]

    def test_vars_are_compact_sorted_json(self, tmp_path):
        props = {"model": "orders", "vars": {"b": 1, "a": {"z": True, "y": "x"}}}
        task = _task(self._compiled(tmp_path, builds__0__properties=props))
        command = task["dbt_task"]["commands"][1]
        assert command == ('dbt build --select +orders+ --vars \'{"a":{"y":"x","z":true},"b":1}\'')

    @pytest.mark.parametrize(
        "vars_",
        [{"token": "{{ env.SECRET }}"}, {"a": {"b": ["x {{env.PW}} y"]}}],
        ids=["flat", "nested"],
    )
    def test_an_env_template_in_vars_is_refused(self, tmp_path, vars_):
        _project(tmp_path)
        contract = _contract("./dbt", builds__0__properties={"model": "orders", "vars": vars_})
        _refused(contract, "vars-template", tmp_path)

    def test_the_dbt_task_runs_on_the_warehouse_in_the_written_exposes_catalog_and_schema(
        self, tmp_path
    ):
        dbt_task = _task(self._compiled(tmp_path))["dbt_task"]
        assert dbt_task["warehouse_id"] == WAREHOUSE
        assert dbt_task["catalog"] == "main"
        assert dbt_task["schema"] == "sales"
        assert dbt_task["source"] == "WORKSPACE"
        assert dbt_task["project_directory"] == f"/Workspace{HOME}/{ROOT}"
        assert isinstance(dbt_task["project_directory"], TofuExpr)

    def test_the_job_pins_the_serverless_environment_one_run_and_tags(self, tmp_path):
        job = _job(self._compiled(tmp_path))
        assert job["name"] == "fluid sales.orders transform"
        assert job["max_concurrent_runs"] == 1
        assert job["tags"] == {"fluid_product": "sales.orders", "fluid_build": "transform"}
        assert job["environment"] == [
            {
                "environment_key": "dbt",
                "spec": {
                    "environment_version": jobs.ENVIRONMENT_VERSION,
                    "dependencies": [jobs.DBT_DATABRICKS],
                },
            }
        ]
        (task,) = job["task"]
        assert task["task_key"] == "dbt" and task["environment_key"] == "dbt"

    def test_the_job_depends_on_every_workspace_file(self, tmp_path):
        _project(tmp_path, {"models/a.sql": "a", "models/sub/b.sql": "b"})
        compiled = _compile(_contract("./dbt"), tmp_path)
        files = compiled.resources["databricks_workspace_file"]
        assert len(files) == 3  # dbt_project.yml and the two models
        assert sorted(_job(compiled)["depends_on"]) == sorted(
            f"databricks_workspace_file.{key}" for key in files
        )

    def test_the_current_user_data_source_is_read_for_the_home_folder(self, tmp_path):
        assert self._compiled(tmp_path).data["databricks_current_user"] == {"me": {}}

    def test_uploads_what_dbt_reads(self, tmp_path):
        config = (
            "model-paths: [models, extra_models]\n"
            "seed-paths: [seeds]\n"
            "asset-paths: [assets]\n"
        )
        _project(
            tmp_path,
            {
                "packages.yml": "packages: []\n",
                "selectors.yml": "selectors: []\n",
                "models/a.sql": "a",
                "models/sub/b.sql": "b",
                "extra_models/e.sql": "e",
                "macros/m.sql": "m",
                "seeds/s.csv": "x\n1\n",
                "snapshots/sn.sql": "s",
                "tests/t.sql": "t",
                "analyses/an.sql": "a",
                "assets/logo.png": "png",
            },
            config,
        )
        compiled = _compile(_contract("./dbt"), tmp_path)
        assert not compiled.refusals
        assert compiled.jobs[0].files == (
            "analyses/an.sql",
            "assets/logo.png",
            "dbt_project.yml",
            "extra_models/e.sql",
            "macros/m.sql",
            "models/a.sql",
            "models/sub/b.sql",
            "packages.yml",
            "seeds/s.csv",
            "selectors.yml",
            "snapshots/sn.sql",
            "tests/t.sql",
        )

    def test_packages_yml_is_uploaded_only_when_present(self, tmp_path):
        _project(tmp_path)
        compiled = _compile(_contract("./dbt"), tmp_path)
        assert compiled.jobs[0].files == ("dbt_project.yml", "models/m.sql")

    def test_never_uploads_profiles_dot_files_target_or_pycache(self, tmp_path):
        _project(
            tmp_path,
            {
                "models/ok.sql": "ok",
                "profiles.yml": "secret: 1",
                "models/profiles.yml": "secret: 2",
                "seeds/profiles.yml": "secret: 3",
                "models/.hidden.sql": "h",
                "models/.git/config": "g",
                "models/.cache/x.sql": "c",
                "models/__pycache__/x.pyc": "p",
                "target/run_results.json": "{}",
                ".env": "TOKEN=1",
                "README.md": "readme",
            },
        )
        compiled = _compile(_contract("./dbt"), tmp_path)
        assert not compiled.refusals
        assert compiled.jobs[0].files == ("dbt_project.yml", "models/ok.sql")

    def test_a_missing_default_directory_is_tolerated(self, tmp_path):
        _project(tmp_path, {})  # no models/, no macros/, ...
        compiled = _compile(_contract("./dbt"), tmp_path)
        assert not compiled.refusals
        assert compiled.jobs[0].files == ("dbt_project.yml",)

    def test_a_file_name_cannot_inject_interpolation_into_the_module(self, tmp_path, monkeypatch):
        _project(tmp_path, {"models/m${x}.sql": "select 1"})
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        contract = _contract("./dbt")
        compiled = _compile(contract, tmp_path)
        (body,) = [
            b
            for key, b in compiled.resources["databricks_workspace_file"].items()
            if "m__x__sql" in key
        ]
        assert isinstance(body["path"], TofuExpr)
        with contract_source(tmp_path / "contract.fluid.yaml"):
            doc = json.loads(build_module(IAC_PLUGINS["databricks"], contract))
        paths = sorted(f["path"] for f in doc["resource"]["databricks_workspace_file"].values())
        # The home reference stays an expression; the file part is escaped as any literal.
        assert f"{HOME}/{ROOT}/models/m$${{x}}.sql" in paths
        assert not any(p.endswith("models/m${x}.sql") for p in paths)
        assert all(p.startswith(HOME + "/") for p in paths)


class TestUploadFolders:
    """One upload folder per product and build: two that fold alike never share one."""

    def _paths(self, compiled) -> set:
        return {b["path"] for b in compiled.resources["databricks_workspace_file"].values()}

    def test_two_builds_that_fold_alike_upload_to_different_folders(self, tmp_path, monkeypatch):
        _project(tmp_path)
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        contract = _contract("./dbt")
        second = json.loads(json.dumps(contract["builds"][0]))
        contract["builds"][0]["id"], second["id"] = "t-1", "t_1"
        contract["builds"].append(second)
        compiled = _compile(contract, tmp_path)
        assert not compiled.refusals
        jobs_ = compiled.resources["databricks_job"].values()
        dirs = {job["task"][0]["dbt_task"]["project_directory"] for job in jobs_}
        assert len(dirs) == 2
        files = list(compiled.resources["databricks_workspace_file"].values())
        assert len({f["path"] for f in files}) == len(files)

    def test_two_products_that_fold_alike_upload_to_different_folders(self, tmp_path, monkeypatch):
        _project(tmp_path)
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        first = _compile(_contract("./dbt"), tmp_path)
        other = _contract("./dbt")
        other["id"] = "sales_orders"
        second = _compile(other, tmp_path)
        assert not self._paths(first) & self._paths(second)


# ── WORKSPACE source: what is refused ───────────────────────────────────────
class TestWorkspaceRefusals:
    def test_a_symlinked_file_is_refused(self, tmp_path):
        outside = tmp_path / "outside.sql"
        outside.write_text("select secret", encoding="utf-8")
        _project(tmp_path)
        (tmp_path / "dbt" / "models" / "link.sql").symlink_to(outside)
        _refused(_contract("./dbt"), "project-link", tmp_path)

    def test_a_symlinked_directory_inside_a_path_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "x.sql").write_text("select secret", encoding="utf-8")
        _project(tmp_path)
        (tmp_path / "dbt" / "models" / "sub").symlink_to(outside, target_is_directory=True)
        _refused(_contract("./dbt"), "project-link", tmp_path)

    def test_a_symlinked_path_directory_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        _project(tmp_path, {})
        (tmp_path / "dbt" / "models").symlink_to(outside, target_is_directory=True)
        _refused(_contract("./dbt"), "project-path", tmp_path)

    def test_a_path_key_through_a_symlinked_component_is_refused(self, tmp_path, monkeypatch):
        # lnk -> a directory outside the project, model-paths: [lnk/leak]: the walk
        # would upload whatever lnk points at (a CI host's ~/.aws, say).
        outside = tmp_path / "outside"
        (outside / "leak").mkdir(parents=True)
        (outside / "leak" / "credentials").write_text("AWS_SECRET=abc", encoding="utf-8")
        _project(tmp_path, {})
        project = tmp_path / "dbt"
        (project / "dbt_project.yml").write_text(
            "name: p\nmodel-paths: [lnk/leak]\n", encoding="utf-8"
        )
        (project / "lnk").symlink_to(outside, target_is_directory=True)
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        compiled = _compile(_contract("./dbt"), tmp_path)
        assert "databricks-job-project-path" in [r.kind for r in compiled.refusals]
        assert "databricks_workspace_file" not in compiled.resources

    @pytest.mark.parametrize("entry", [".git", "models/.secrets", "."])
    def test_a_hidden_or_root_path_key_is_refused(self, tmp_path, monkeypatch, entry):
        # .git holds a checkout's token (actions/checkout persists it in .git/config).
        _project(tmp_path, {})
        project = tmp_path / "dbt"
        (project / ".git").mkdir()
        (project / ".git" / "config").write_text("extraheader = token", encoding="utf-8")
        (project / "dbt_project.yml").write_text(
            f"name: p\nmodel-paths: ['{entry}']\n", encoding="utf-8"
        )
        monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
        compiled = _compile(_contract("./dbt"), tmp_path)
        assert "databricks-job-project-path" in [r.kind for r in compiled.refusals]
        assert "databricks_workspace_file" not in compiled.resources

    def test_a_symlinked_top_level_project_file_is_refused(self, tmp_path):
        outside = tmp_path / "packages.yml"
        outside.write_text("packages: []", encoding="utf-8")
        _project(tmp_path)
        (tmp_path / "dbt" / "packages.yml").symlink_to(outside)
        _refused(_contract("./dbt"), "project-link", tmp_path)

    @pytest.mark.parametrize(
        "config",
        [
            "model-paths: ['../shared']\n",
            "model-paths: [models, '../../etc']\n",
            "macro-paths: ['/etc']\n",
            "seed-paths: '/etc'\n",
            "test-paths: ['a/../../b']\n",
        ],
    )
    def test_a_path_key_that_leaves_the_project_is_refused(self, tmp_path, config):
        _project(tmp_path, config=config)
        _refused(_contract("./dbt"), "project-path", tmp_path)

    def test_a_file_over_the_per_file_cap_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_FILE_BYTES", 20)
        _project(tmp_path, {"models/big.sql": "x" * 21})
        _refused(_contract("./dbt"), "file-size", tmp_path)

    def test_a_file_at_the_per_file_cap_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_FILE_BYTES", 20)
        _project(tmp_path, {"models/ok.sql": "x" * 20})
        assert not _compile(_contract("./dbt"), tmp_path).refusals

    def test_a_project_over_the_file_cap_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_PROJECT_FILES", 2)
        _project(tmp_path, {"models/a.sql": "a", "models/b.sql": "b"})  # 3 with dbt_project.yml
        _refused(_contract("./dbt"), "project-size", tmp_path)

    def test_a_project_over_the_byte_cap_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "MAX_PROJECT_BYTES", 30)
        _project(tmp_path, {"models/a.sql": "a" * 20, "models/b.sql": "b" * 20})
        _refused(_contract("./dbt"), "project-size", tmp_path)

    def test_a_directory_without_dbt_project_yml_is_refused(self, tmp_path):
        (tmp_path / "dbt").mkdir()
        (tmp_path / "dbt" / "models").mkdir()
        _refused(_contract("./dbt"), "project-missing", tmp_path)

    def test_a_repository_directory_that_does_not_exist_is_refused(self, tmp_path):
        _refused(_contract("./nowhere"), "project-missing", tmp_path)

    def test_an_unreadable_dbt_project_yml_is_refused(self, tmp_path):
        root = _project(tmp_path)
        root.joinpath("dbt_project.yml").write_text("a: [unclosed\n", encoding="utf-8")
        _refused(_contract("./dbt"), "project-unreadable", tmp_path)

    def test_no_repository_is_refused(self, tmp_path):
        _refused(_contract(""), "repository-missing", tmp_path)

    def test_no_contract_source_is_refused_when_not_validating(self):
        _refused(_contract("./dbt"), "source-unknown", None)

    def test_no_contract_source_is_not_refused_while_validating(self):
        compiled = _compile(_contract("./dbt"), None, validating=True)
        assert not compiled.refusals
        (job,) = compiled.jobs
        assert job.source == "WORKSPACE" and job.files == ()


# ── GIT source ──────────────────────────────────────────────────────────────
class TestGitSource:
    def _git(self, repository: str) -> Dict[str, Any]:
        compiled = _compile(_contract(repository))
        job = _job(compiled)
        (task,) = job["task"]
        assert "depends_on" not in job
        assert "databricks_workspace_file" not in compiled.resources
        return {"git_source": job["git_source"], "dbt_task": task["dbt_task"]}

    def test_tag_and_path(self):
        found = self._git("https://github.com/o/r?tag=v1.2.0&path=sub/dir")
        assert found["git_source"] == {
            "url": "https://github.com/o/r",
            "provider": "gitHub",
            "tag": "v1.2.0",
        }
        assert found["dbt_task"]["source"] == "GIT"
        assert found["dbt_task"]["project_directory"] == "sub/dir"

    def test_branch(self):
        found = self._git("https://github.com/o/r?branch=release/1.x")
        assert found["git_source"] == {
            "url": "https://github.com/o/r",
            "provider": "gitHub",
            "branch": "release/1.x",
        }
        assert "project_directory" not in found["dbt_task"]

    @pytest.mark.parametrize(
        "commit",
        ["abc1234", "0123456789abcdef0123456789ABCDEF01234567"],  # pragma: allowlist secret
    )
    def test_commit_of_seven_to_forty_hex(self, commit):
        found = self._git(f"https://github.com/o/r?commit={commit}")
        assert found["git_source"]["commit"] == commit

    @pytest.mark.parametrize(
        "host, provider",
        [
            ("github.com", "gitHub"),
            ("gitlab.com", "gitLab"),
            ("bitbucket.org", "bitbucketCloud"),
            ("dev.azure.com", "azureDevOpsServices"),
            ("acme.visualstudio.com", "azureDevOpsServices"),
        ],
    )
    def test_host_maps_to_its_provider(self, host, provider):
        found = self._git(f"https://{host}/o/r?tag=v1")
        assert found["git_source"]["provider"] == provider
        assert found["git_source"]["url"] == f"https://{host}/o/r"

    def test_the_git_url_is_stored_without_a_query_or_fragment(self):
        found = self._git("https://github.com/o/r.git?tag=v1&path=x")
        assert found["git_source"]["url"] == "https://github.com/o/r.git"

    @pytest.mark.parametrize(
        "repository, kind",
        [
            ("https://github.com/o/r", "git-ref"),
            ("https://github.com/o/r?path=sub", "git-ref"),
            ("https://github.com/o/r?tag=v1&branch=main", "git-ref"),
            ("https://github.com/o/r?tag=v1&tag=v2", "git-ref"),
            ("https://github.com/o/r?tag=v1&depth=1", "git-ref"),
            ("https://github.com/o/r?tag=", "git-ref"),
            ("https://github.com/o/r?commit=xyz1234", "git-ref"),
            ("https://github.com/o/r?commit=abc12", "git-ref"),
            ("https://github.com/o/r?commit=" + "a" * 41, "git-ref"),
            ("https://github.com/o/r?tag=-rf", "git-ref"),
            ("https://github.com/o/r?tag=v1&path=../x", "git-ref"),
            ("https://github.com/o/r?tag=v1&path=a/../../x", "git-ref"),
            ("https://github.com/o/r?tag=v1&path=/abs", "git-ref"),
            ("https://github.com/o/r?tag=v1&path=a%20b", "git-ref"),
            ("http://github.com/o/r?tag=v1", "git-url"),
            ("git://github.com/o/r?tag=v1", "git-url"),
            ("git@github.com:o/r.git", "git-url"),
            ("https://user:pass@github.com/o/r?tag=v1", "git-url"),  # pragma: allowlist secret
            ("https://token@github.com/o/r?tag=v1", "git-url"),
            ("https://git.example.com/o/r?tag=v1", "git-host"),
            ("https://github.com.evil.example/o/r?tag=v1", "git-host"),
            ("https://visualstudio.com/o/r?tag=v1", "git-host"),
        ],
    )
    def test_refused(self, repository, kind):
        _refused(_contract(repository), kind)

    def test_a_refused_credential_in_the_url_is_not_echoed(self):
        compiled = _compile(_contract("https://user:hunter2@github.com/o/r?tag=v1"))
        assert "hunter2" not in " ".join(str(r) for r in compiled.refusals)


# ── triggers and settings ───────────────────────────────────────────────────
def _trigger_job(trigger: Any) -> dbx._Compiled:
    return _compile(_contract(builds__0__execution__trigger=trigger))


class TestTriggers:
    def test_cron_becomes_an_unpaused_quartz_schedule_in_its_timezone(self):
        job = _job(
            _trigger_job({"type": "schedule", "cron": "0 9 * * 1-5", "timezone": "Europe/Zurich"})
        )
        assert job["schedule"] == {
            "quartz_cron_expression": "0 0 9 ? * MON,TUE,WED,THU,FRI",
            "timezone_id": "Europe/Zurich",
            "pause_status": "UNPAUSED",
        }
        assert "continuous" not in job

    def test_the_default_timezone_is_utc(self):
        job = _job(_trigger_job({"cron": "@daily"}))
        assert job["schedule"]["timezone_id"] == "UTC"
        assert job["schedule"]["quartz_cron_expression"] == "0 0 0 * * ?"

    def test_quartz_month_names_are_upper_cased(self):
        job = _job(_trigger_job({"cron": "0 0 1 jul *"}))
        assert job["schedule"]["quartz_cron_expression"] == "0 0 0 1 JUL ?"

    def test_schedule_is_the_cron_when_cron_is_absent(self):
        job = _job(_trigger_job({"type": "schedule", "schedule": "0 6 * * *"}))
        assert job["schedule"]["quartz_cron_expression"] == "0 0 6 * * ?"

    def test_the_same_cron_in_cron_and_schedule_is_one_schedule(self):
        job = _job(_trigger_job({"cron": "0 6 * * *", "schedule": " 0 6 * * * "}))
        assert job["schedule"]["quartz_cron_expression"] == "0 0 6 * * ?"

    def test_cron_and_schedule_that_disagree_are_refused(self):
        _refused(
            _contract(builds__0__execution__trigger={"cron": "0 6 * * *", "schedule": "0 7 * * *"}),
            "trigger-conflict",
        )

    def test_continuous_is_an_unpaused_continuous_block(self):
        job = _job(_trigger_job({"type": "continuous"}))
        assert job["continuous"] == {"pause_status": "UNPAUSED"}
        assert "schedule" not in job

    @pytest.mark.parametrize(
        "trigger", [{"type": "manual"}, {}, None], ids=["manual", "empty", "none"]
    )
    def test_manual_or_no_trigger_has_neither_block(self, trigger):
        compiled = _trigger_job(trigger)
        job = _job(compiled)
        assert "schedule" not in job and "continuous" not in job
        assert compiled.jobs[0].trigger == "manual"

    def test_the_planner_lists_the_trigger_kind(self):
        assert _trigger_job({"cron": "@daily"}).jobs[0].trigger == "schedule"
        assert _trigger_job({"type": "continuous"}).jobs[0].trigger == "continuous"

    def test_type_schedule_without_a_cron_is_refused(self):
        _refused(_contract(builds__0__execution__trigger={"type": "schedule"}), "cron")

    @pytest.mark.parametrize("cron", ["0 0 1 * 1", "61 * * * *", "* * *"])
    def test_a_cron_quartz_cannot_express_is_refused(self, cron):
        _refused(_contract(builds__0__execution__trigger={"cron": cron}), "cron")

    @pytest.mark.parametrize("kind", ["event", "dataset", "dependency", "timetable"])
    def test_a_trigger_the_job_cannot_map_is_refused(self, kind):
        _refused(_contract(builds__0__execution__trigger={"type": kind}), "trigger-unmapped")

    def test_an_unknown_timezone_is_refused(self):
        trigger = {"cron": "@daily", "timezone": "Mars/Olympus"}
        _refused(_contract(builds__0__execution__trigger=trigger), "timezone")


class TestRetriesTimeoutNotificationsRunAs:
    def test_max_attempts_counts_the_first_run(self):
        retries = {"maxAttempts": 3}
        task = _task(_compile(_contract(builds__0__execution__retries=retries)))
        assert task["max_retries"] == 2

    def test_one_attempt_is_no_retry(self):
        task = _task(_compile(_contract(builds__0__execution__retries={"maxAttempts": 1})))
        assert task["max_retries"] == 0

    def test_initial_delay_is_the_retry_interval_in_milliseconds(self):
        retries = {"maxAttempts": 3, "initialDelay": "5m"}
        task = _task(_compile(_contract(builds__0__execution__retries=retries)))
        assert task["min_retry_interval_millis"] == 300000

    def test_no_retries_sets_no_retry_fields(self):
        task = _task(_compile(_contract()))
        assert "max_retries" not in task and "min_retry_interval_millis" not in task

    @pytest.mark.parametrize(
        "retries",
        [
            {"maxAttempts": 3, "backoffStrategy": "exponential"},
            {"maxAttempts": 3, "maxDelay": "1h"},
            {"maxAttempts": 3, "initialDelay": "soon"},
        ],
        ids=["exponential", "max-delay", "bad-delay"],
    )
    def test_retries_a_job_cannot_honour_are_refused(self, retries):
        compiled = _compile(_contract(builds__0__execution__retries=retries))
        assert _kinds(compiled)[0] in ("databricks-job-retries", "databricks-job-duration")
        assert "databricks_job" not in compiled.resources

    @pytest.mark.parametrize(
        "timeout, seconds",
        [
            ("2h", 7200),
            ("PT1H30M", 5400),
            ("pt45s", 45),
            (45, 45),
            ("45", 45),
            ("30m", 1800),
            ("1d", 86400),
        ],
    )
    def test_timeout_is_whole_seconds_on_the_task(self, timeout, seconds):
        contract = _contract(
            builds__0__execution__runtime={"platform": "databricks", "timeout": timeout}
        )
        assert _task(_compile(contract))["timeout_seconds"] == seconds

    @pytest.mark.parametrize("timeout", ["soon", "PT", "1w", "-5"])
    def test_an_unparseable_timeout_is_refused(self, timeout):
        contract = _contract(
            builds__0__execution__runtime={"platform": "databricks", "timeout": timeout}
        )
        _refused(contract, "duration")

    def test_email_notifications_map_failure_success_and_always(self):
        notifications = [
            {"type": "email", "target": "fail@corp.com", "condition": "failure"},
            {"type": "email", "target": "ok@corp.com", "condition": "success"},
            {"type": "email", "target": "both@corp.com", "condition": "always"},
            {"type": "email", "target": "default@corp.com"},
        ]
        job = _job(_compile(_contract(builds__0__execution__notifications=notifications)))
        assert job["email_notifications"] == {
            "on_failure": ["both@corp.com", "default@corp.com", "fail@corp.com"],
            "on_success": ["both@corp.com", "ok@corp.com"],
        }

    def test_no_notifications_set_no_block(self):
        assert "email_notifications" not in _job(_compile(_contract()))

    @pytest.mark.parametrize(
        "entry",
        [
            {"type": "slack", "target": "#data"},
            {"type": "email", "target": "not-an-address"},
            {"target": "a@corp.com"},
        ],
        ids=["slack", "bad-address", "untyped"],
    )
    def test_a_notification_that_would_notify_no_one_is_refused(self, entry):
        _refused(_contract(builds__0__execution__notifications=[entry]), "notification")

    def test_a_listed_service_principal_is_the_jobs_run_as_lower_cased(self):
        account = "ABCDEF01-2345-6789-ABCD-EF0123456789"
        runtime = {"platform": "databricks", "serviceAccount": account}
        listed = {"FLUID_DATABRICKS_RUN_AS_ALLOWLIST": f" other-id , {account.lower()} "}
        job = _job(_compile(_contract(builds__0__execution__runtime=runtime), env=listed))
        assert job["run_as"] == {"service_principal_name": account.lower()}

    @pytest.mark.parametrize(
        "listed", ["", "11111111-2222-3333-4444-555555555555"], ids=["unset", "another"]
    )
    def test_a_service_principal_the_operator_does_not_list_is_refused(self, listed):
        # The applier may hold Service Principal User on several: the contract
        # alone never picks the one its dbt code runs as.
        account = "abcdef01-2345-6789-abcd-ef0123456789"
        runtime = {"platform": "databricks", "serviceAccount": account}
        compiled = _compile(
            _contract(builds__0__execution__runtime=runtime),
            env={"FLUID_DATABRICKS_RUN_AS_ALLOWLIST": listed},
        )
        assert _kinds(compiled) == ["databricks-job-run-as-unlisted"]
        assert "FLUID_DATABRICKS_RUN_AS_ALLOWLIST" in " ".join(compiled.refusals[0].remediation)
        assert "databricks_job" not in compiled.resources

    @pytest.mark.parametrize("account", ["bot@corp.com", "robot", "sa-123", "not-a-uuid-0000"])
    def test_any_other_service_account_is_refused(self, account):
        runtime = {"platform": "databricks", "serviceAccount": account}
        _refused(_contract(builds__0__execution__runtime=runtime), "run-as")

    @pytest.mark.parametrize(
        "extra",
        [
            {"environment": {"A": "1"}},
            {"image": "repo/img:1"},
            {"executor": "spark"},
            {"resources": {"cpu": "2"}},
            {"resources": {"memory": "4Gi"}},
            {"resources": {"disk": "10Gi"}},
        ],
        ids=["environment", "image", "executor", "cpu", "memory", "disk"],
    )
    def test_runtime_settings_a_serverless_job_ignores_are_refused(self, extra):
        runtime = {"platform": "databricks", **extra}
        _refused(_contract(builds__0__execution__runtime=runtime), "runtime-unmapped")


class TestBuildEligibility:
    def test_a_dbt_adapter_for_another_platform_is_refused(self):
        _refused(_contract(builds__0__engine="dbt-snowflake"), "adapter")

    def test_the_dbt_databricks_adapter_is_accepted(self):
        assert _job(_compile(_contract(builds__0__engine="dbt-databricks")))

    def test_a_build_writing_no_databricks_expose_is_refused(self):
        _refused(_contract(builds__0__outputs=["somewhere_else"]), "no-target")

    def test_properties_target_is_a_validate_warning_not_an_error(self):
        contract = _contract(builds__0__properties={"model": "orders", "target": "prod"})
        errors, warnings = dbx.validate_databricks_binding(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE})
        assert errors == []
        assert any("properties.target" in w and "'prod'" in w for w in warnings)

    def test_validate_reports_a_job_refusal_as_an_error(self):
        contract = _contract(builds__0__execution__trigger={"type": "event"})
        errors, _ = dbx.validate_databricks_binding(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE})
        assert any("event" in e for e in errors)

    def test_a_dbt_build_without_a_platform_makes_no_job_but_its_table_is_dbt_written(self):
        contract = _contract(builds__0__execution={})
        compiled = _compile(contract)
        assert not compiled.refusals
        assert "databricks_job" not in compiled.resources and compiled.jobs == []
        table = compiled.resources["databricks_sql_table"]["sales_orders_main_sales_orders"]
        assert table["lifecycle"] == {"ignore_changes": list(dbx._DBT_OWNED)}
        assert jobs.dbt_written_exposes(contract, compiled.tables) == {"orders"}

    def test_a_dbt_adapter_engine_also_writes_the_table_as_dbt(self):
        contract = _contract(builds__0__engine="dbt-snowflake", builds__0__execution={})
        compiled = _compile(contract)
        assert jobs.dbt_written_exposes(contract, compiled.tables) == {"orders"}

    def test_a_python_build_does_not_make_the_table_dbt_written(self):
        contract = _contract(builds__0__engine="python", builds__0__execution={})
        compiled = _compile(contract)
        assert jobs.dbt_written_exposes(contract, compiled.tables) == set()


# ── tables: dbt-written or module-owned ─────────────────────────────────────
def _two_tables(**overrides: Any) -> Dict[str, Any]:
    contract = _contract(**overrides)
    contract["exposes"].append(
        {
            "exposeId": "customers",
            "kind": "table",
            "binding": {
                "platform": "databricks",
                "format": "delta_table",
                "location": {"database": "main", "schema": "sales", "table": "customers"},
            },
            "contract": {"schema": [{"name": "cid", "type": "long"}]},
            "policy": {"authz": {"readers": ["group:bi"]}},
        }
    )
    return contract


class TestTables:
    _OWNED = ["column", "partitions", "cluster_keys", "properties", "comment"]

    def test_a_table_a_dbt_build_writes_ignores_dbt_owned_changes_and_sets_no_properties(self):
        tables = _compile(_two_tables()).resources["databricks_sql_table"]
        written = tables["sales_orders_main_sales_orders"]
        assert written["lifecycle"] == {"ignore_changes": self._OWNED}
        assert "properties" not in written

    def test_any_other_table_sets_column_mapping_and_has_no_lifecycle(self):
        tables = _compile(_two_tables()).resources["databricks_sql_table"]
        other = tables["sales_orders_main_sales_customers"]
        assert other["properties"] == {"delta.columnMapping.mode": "name"}
        assert "lifecycle" not in other

    def test_a_build_with_no_outputs_may_write_every_table(self):
        contract = _two_tables()
        del contract["builds"][0]["outputs"]
        tables = _compile(contract).resources["databricks_sql_table"]
        assert all(t["lifecycle"] == {"ignore_changes": self._OWNED} for t in tables.values())
        assert all("properties" not in t for t in tables.values())

    def test_managed_objects_reports_dbt_written_readers_and_normalized_types(self):
        contract = _two_tables(
            accessPolicy={"grants": [{"principal": "group:analysts", "permissions": ["read"]}]}
        )
        contract["exposes"][0]["contract"]["schema"] = [
            {"name": "id", "type": "long"},
            {"name": "amt", "type": "decimal"},
        ]
        tables, job_list = dbx.managed_objects(contract, {dbx.WAREHOUSE_ENV: WAREHOUSE})
        by_name = {t.full_name: t for t in tables}
        orders, customers = by_name["main.sales.orders"], by_name["main.sales.customers"]
        assert (orders.expose_id, orders.dbt_written) == ("orders", True)
        assert (customers.expose_id, customers.dbt_written) == ("customers", False)
        assert [c["type"] for c in orders.columns] == ["bigint", "decimal(10,0)"]
        assert [c["type"] for c in customers.columns] == ["bigint"]
        assert orders.readers == ("analysts",)
        assert sorted(customers.readers) == ["analysts", "bi"]
        assert [j.name for j in job_list] == ["fluid sales.orders transform"]

    def test_managed_objects_needs_no_contract_source_for_a_local_project(self):
        # It runs while validating: the project files are not read.
        tables, job_list = dbx.managed_objects(
            _two_tables(builds__0__repository="./dbt"), {dbx.WAREHOUSE_ENV: WAREHOUSE}
        )
        assert [j.source for j in job_list] == ["WORKSPACE"] and len(tables) == 2


def test_the_module_is_deterministic_for_one_project(tmp_path, monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)
    _project(tmp_path, {"models/b.sql": "b", "models/a.sql": "a"})
    contract = _contract("./dbt")
    with contract_source(tmp_path / "contract.fluid.yaml"):
        first = build_module(IAC_PLUGINS["databricks"], contract)
        second = build_module(IAC_PLUGINS["databricks"], contract)
    assert first == second
    assert os.fspath(tmp_path) not in first  # no local path leaks into the module


class TestUploadRoot:
    """One folder per product and build in the applier's home, never shared."""

    @staticmethod
    def _root(product: str, build_id: str = "transform") -> str:
        from fluid_build.iac.providers.databricks_jobs import _Build

        return _Build({}, build_id, "builds[0]", product, WAREHOUSE).root

    def test_an_id_written_as_another_ids_folded_form_gets_its_own_folder(self):
        # `sales_eu_<digest>` is how `sales-eu` folds: as a product id of its own
        # it must not land in that product's folder and replace its models.
        assert self._root("sales-eu") != self._root(unique_ident("sales-eu"))
        assert self._root("p", "x-y") != self._root("p", unique_ident("x-y"))

    def test_ids_that_fold_alike_get_their_own_folders(self):
        assert len({self._root(p) for p in ("sales.orders", "sales-orders", "sales_orders")}) == 3

    def test_the_same_ids_get_the_same_folder(self):
        assert self._root("sales.orders") == self._root("sales.orders")

    def test_each_folder_name_carries_a_128_bit_digest(self):
        # An 8-hex digest of a crafted id that folds alike is a short search away.
        product, build = self._root("sales.orders").split("/")[2:4]
        for segment in (product, build):
            assert re.fullmatch(r"[A-Za-z0-9_]+_[0-9a-f]{32}", segment), segment
