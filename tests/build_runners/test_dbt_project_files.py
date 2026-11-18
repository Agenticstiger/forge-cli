# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What forge reads of a dbt project as a whole: the selection, the limits, the check.

``build_runners/dbt/project_files.py`` is the one definition of "the files dbt
reads" that ``fluid bundle`` packs and the Databricks job emitter uploads, and
the check ``fluid apply`` and the dbt runner run against the copy a bundle packs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

import pytest

from fluid_build.build_runners.dbt import project_files as pf
from fluid_build.build_runners.dbt.project_files import (
    BoundProject,
    DbtProjectFilesError,
    binds_dbt_project,
    bound_project,
    check_project,
    collect_bundle_projects,
    diff_trees,
    file_digests,
    read_project_files,
    reset_bound_projects,
    set_bound_projects,
)

#: Every file dbt reads in the project :func:`_project` writes.
READ = {
    "dbt_project.yml",
    "packages.yml",
    "package-lock.yml",
    "dependencies.yml",
    "selectors.yml",
    "models/orders.sql",
    "models/schema.yml",
    "models/staging/stg_orders.sql",
    "models/logs/log_events.sql",
    "macros/m.sql",
    "seeds/s.csv",
    "snapshots/snap.sql",
    "tests/t.sql",
    "analyses/a.sql",
}


def _write(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _project(root: Path, dbt_project: str = "name: p\nprofile: p\n") -> Path:
    """A dbt project holding every file dbt reads, and every file it must not take."""
    project = root / "dbt"
    _write(project, {rel: f"-- {rel}\n" for rel in READ if rel != "dbt_project.yml"})
    _write(
        project,
        {
            "dbt_project.yml": dbt_project,
            # Never taken: what dbt writes, credentials, hidden and cache files.
            "target/manifest.json": "{}",
            "target/run_results.json": "{}",
            "logs/dbt.log": "log",
            "dbt_packages/pkg/macros/x.sql": "select 1",
            "dbt_modules/old/x.sql": "select 1",
            ".user.yml": "id: x",
            "profiles.yml": "p: {outputs: {dev: {password: s3cret}}}",
            "models/profiles.yml": "secret: 2",
            "models/Profiles.YAML": "secret: 3",
            "seeds/profiles.yaml": "secret: 4",
            "models/.hidden.sql": "select 3",
            ".git/config": "[credential]",
            "models/.ipynb_checkpoints/x.sql": "select 4",
            "models/__pycache__/m.cpython-312.pyc": "x",
            "README.md": "not read by dbt",
            "config/dbt/profiles.yml": "p: {}",
        },
    )
    return project


class TestSelection:
    def test_takes_what_dbt_reads_and_nothing_else(self, tmp_path: Path) -> None:
        assert set(read_project_files(_project(tmp_path))) == READ

    def test_bytes_are_taken_as_they_are_on_disk(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        raw = b"select 1 as id\r\n-- caf\xc3\xa9 no trailing newline"
        (project / "models" / "orders.sql").write_bytes(raw)
        assert read_project_files(project)["models/orders.sql"] == raw

    def test_configured_paths_replace_the_defaults(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "name: p\nmodel-paths: [src]\nmacro-paths: [lib, macros]\n")
        _write(project, {"src/a.sql": "select 1", "lib/b.sql": "{% macro b() %}{% endmacro %}"})
        files = set(read_project_files(project))
        assert {"src/a.sql", "lib/b.sql", "macros/m.sql"} <= files
        assert not any(rel.startswith("models/") for rel in files)

    def test_a_directory_dbt_writes_into_is_skipped_inside_a_read_one(self, tmp_path: Path) -> None:
        project = _project(
            tmp_path, "name: p\ntarget-path: models/compiled\npackages-install-path: models/pkg\n"
        )
        _write(project, {"models/compiled/x.sql": "x", "models/pkg/y.sql": "y"})
        files = set(read_project_files(project))
        assert "models/orders.sql" in files
        assert not any(rel.startswith(("models/compiled/", "models/pkg/")) for rel in files)

    def test_a_missing_default_directory_is_no_error(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        _write(project, {"dbt_project.yml": "name: p\n", "models/a.sql": "select 1"})
        assert set(read_project_files(project)) == {"dbt_project.yml", "models/a.sql"}


class TestRefusals:
    @pytest.mark.parametrize(
        "paths, why",
        [
            ("[../outside]", "outside the project"),
            ("['/etc']", "outside the project"),
            ("['.']", "the project root"),
            ("[.git]", "hidden directory"),
            ("5", "not a list of directories"),
        ],
    )
    def test_a_path_key_outside_plain_directories(
        self, tmp_path: Path, paths: str, why: str
    ) -> None:
        project = _project(tmp_path, f"name: p\nmodel-paths: {paths}\n")
        with pytest.raises(DbtProjectFilesError, match="model-paths") as exc:
            read_project_files(project)
        assert why in str(exc.value)

    def test_a_linked_file(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        outside = tmp_path / "outside.sql"
        outside.write_text("select 'secret'", encoding="utf-8")
        os.symlink(outside, project / "models" / "leak.sql")
        with pytest.raises(DbtProjectFilesError, match="models/leak.sql.*symbolic link"):
            read_project_files(project)

    def test_a_linked_directory(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        shared = tmp_path / "shared_macros"
        _write(shared, {"x.sql": "select 1"})
        os.symlink(shared, project / "macros" / "shared")
        with pytest.raises(DbtProjectFilesError, match="macros/shared.*symbolic link"):
            read_project_files(project)

    def test_a_path_key_through_a_link(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "name: p\nmodel-paths: [lnk/leak]\n")
        _write(tmp_path / "elsewhere", {"leak/a.sql": "select 1"})
        os.symlink(tmp_path / "elsewhere", project / "lnk")
        with pytest.raises(DbtProjectFilesError, match="through the symbolic link 'lnk'"):
            read_project_files(project)

    def test_a_linked_top_level_file(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        real = tmp_path / "shared_packages.yml"
        real.write_text("packages: []\n", encoding="utf-8")
        (project / "packages.yml").unlink()
        os.symlink(real, project / "packages.yml")
        with pytest.raises(DbtProjectFilesError, match="packages.yml.*symbolic link"):
            read_project_files(project)

    def test_an_unreadable_dbt_project_yml(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "name: [unclosed\n")
        with pytest.raises(DbtProjectFilesError, match="dbt_project.yml.*could not be read"):
            read_project_files(project)

    def test_a_file_over_the_member_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)
        monkeypatch.setattr(pf, "MAX_FILE_BYTES", 100)
        (project / "seeds" / "big.csv").write_text("x" * 101, encoding="utf-8")
        with pytest.raises(DbtProjectFilesError, match="seeds/big.csv is over 100 bytes"):
            read_project_files(project)

    def test_a_project_over_the_bundle_limit(self, tmp_path: Path) -> None:
        with pytest.raises(DbtProjectFilesError, match="the most one bundle packs"):
            read_project_files(_project(tmp_path), budget=10)


def _contract(*builds: Dict) -> Dict:
    return {"id": "demo.p", "builds": list(builds)}


def _dbt_build(build_id: str = "transform", repository: str = "./dbt", **extra) -> Dict:
    return {"id": build_id, "engine": "dbt-duckdb", "repository": repository, **extra}


class TestWhichBuildsBindAProject:
    @pytest.mark.parametrize(
        "build, binds",
        [
            (_dbt_build(), True),
            ({"id": "t", "engine": "dbt"}, True),
            (_dbt_build(execution={"runtime": {"platform": "databricks"}}), True),
            (_dbt_build(repository="https://github.com/o/r?tag=v1"), False),
            (_dbt_build(repository="git@github.com:o/r.git"), False),
            (_dbt_build(pattern="embedded-logic", properties={"sql": "SELECT 1"}), False),
            (_dbt_build(properties={"language": "flink_sql"}), False),
            ({"id": "t", "engine": "sql", "properties": {"sql": "SELECT 1"}}, False),
            ({"id": "t", "engine": "spark"}, False),
            ("not a build", False),
        ],
    )
    def test_binds(self, build, binds: bool) -> None:
        assert binds_dbt_project(build) is binds


class TestCollectBundleProjects:
    def test_each_dbt_build_by_id(self, tmp_path: Path) -> None:
        _project(tmp_path)
        contract = _contract(
            _dbt_build("a"), _dbt_build("b"), {"id": "c", "engine": "sql", "properties": {}}
        )
        projects = collect_bundle_projects(contract, tmp_path / "contract.fluid.yaml")
        assert sorted(projects) == ["a", "b"]
        assert set(projects["a"]) == READ

    def test_a_missing_project_names_the_build_and_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(DbtProjectFilesError, match=r"build 'transform': no dbt_project.yml"):
            collect_bundle_projects(_contract(_dbt_build()), tmp_path / "contract.fluid.yaml")

    def test_a_project_outside_the_workspace(self, tmp_path: Path) -> None:
        _project(tmp_path)
        contract_path = tmp_path / "product" / "contract.fluid.yaml"
        contract_path.parent.mkdir()
        with pytest.raises(DbtProjectFilesError, match="outside the contract's workspace"):
            collect_bundle_projects(_contract(_dbt_build(repository="../dbt")), contract_path)

    @pytest.mark.parametrize("bad_id", [None, "../escape", "a/b", ".hidden", ""])
    def test_an_id_that_cannot_be_a_bundle_path(self, tmp_path: Path, bad_id) -> None:
        _project(tmp_path)
        build = _dbt_build()
        build["id"] = bad_id
        with pytest.raises(DbtProjectFilesError, match=r"builds\[0\]: a dbt build needs an id"):
            collect_bundle_projects(_contract(build), tmp_path / "contract.fluid.yaml")

    def test_two_builds_with_one_id(self, tmp_path: Path) -> None:
        _project(tmp_path)
        contract = _contract(_dbt_build("a"), _dbt_build("a"))
        with pytest.raises(DbtProjectFilesError, match="two dbt builds share the id 'a'"):
            collect_bundle_projects(contract, tmp_path / "contract.fluid.yaml")

    def test_the_limit_counts_every_project_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)
        one = sum(len(data) for data in read_project_files(project).values())
        monkeypatch.setattr(pf, "MAX_PROJECT_BYTES", one + one // 2)
        contract = _contract(_dbt_build("a"), _dbt_build("b"))
        with pytest.raises(DbtProjectFilesError, match="build 'b'"):
            collect_bundle_projects(contract, tmp_path / "contract.fluid.yaml")


class TestCheck:
    def test_the_same_files_pass(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        assert check_project(project, file_digests(read_project_files(project))) is None

    def test_a_changed_added_and_removed_file_are_named(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        bound = file_digests(read_project_files(project))
        (project / "models" / "orders.sql").write_text("select 2", encoding="utf-8")
        (project / "models" / "new.sql").write_text("select 3", encoding="utf-8")
        (project / "macros" / "m.sql").unlink()
        assert check_project(project, bound) == (
            "changed models/orders.sql; added models/new.sql; removed macros/m.sql"
        )

    def test_a_link_that_appeared_is_a_difference(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        bound = file_digests(read_project_files(project))
        os.symlink(tmp_path / "x", project / "models" / "lnk.sql")
        assert "symbolic link" in (check_project(project, bound) or "")

    def test_excluded_files_never_count(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        bound = file_digests(read_project_files(project))
        (project / "target" / "manifest.json").write_text('{"changed": 1}', encoding="utf-8")
        (project / "profiles.yml").write_text("rotated", encoding="utf-8")
        _write(project, {"logs/new.log": "x", "dbt_packages/p/y.sql": "y"})
        assert check_project(project, bound) is None

    def test_a_long_list_is_cut(self) -> None:
        expected = {f"models/m{i:02}.sql": "sha256:a" for i in range(13)}
        text = diff_trees(expected, {}).describe()
        assert text.startswith("removed models/m00.sql, ")
        assert text.endswith("models/m09.sql and 3 more")


class TestBoundProjects:
    def test_bound_for_the_run_and_reset_after(self) -> None:
        bound = BoundProject(build_id="t", files={"a.sql": "sha256:1"}, bundle="b.tgz")
        assert bound_project("t") is None
        token = set_bound_projects({"t": bound})
        try:
            assert bound_project("t") == bound
            assert bound_project("other") is None
        finally:
            reset_bound_projects(token)
        assert bound_project("t") is None
