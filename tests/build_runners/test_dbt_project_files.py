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
    ".dbtignore",
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

    def test_an_output_directory_is_skipped_whatever_its_case(self, tmp_path: Path) -> None:
        # On a case-insensitive file system Target/ opens target/: it is skipped by
        # identity, not by name. On a case-sensitive one there is no Target/ at all.
        project = _project(tmp_path, "name: p\nmodel-paths: [Target, models]\n")
        files = set(read_project_files(project))
        assert "models/orders.sql" in files
        assert not any(rel.lower().startswith("target/") for rel in files)

    def test_a_missing_default_directory_is_no_error(self, tmp_path: Path) -> None:
        project = tmp_path / "p"
        _write(project, {"dbt_project.yml": "name: p\n", "models/a.sql": "select 1"})
        assert set(read_project_files(project)) == {"dbt_project.yml", "models/a.sql"}


def _refusal(project: Path, **kwargs) -> str:
    """Why :func:`read_project_files` refuses ``project``."""
    with pytest.raises(DbtProjectFilesError) as exc:
        read_project_files(project, **kwargs)
    return str(exc.value)


def _link(target: Path, link: Path) -> Path:
    os.symlink(target, link)
    return link.parent


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
        text = _refusal(_project(tmp_path, f"name: p\nmodel-paths: {paths}\n"))
        assert "model-paths" in text and why in text

    def test_a_linked_file(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        _write(tmp_path, {"outside.sql": "select 'secret'"})
        _link(tmp_path / "outside.sql", project / "models" / "leak.sql")
        assert "models/leak.sql in the dbt project is a symbolic link" in _refusal(project)

    def test_a_linked_directory(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        _write(tmp_path / "shared_macros", {"x.sql": "select 1"})
        _link(tmp_path / "shared_macros", project / "macros" / "shared")
        assert "macros/shared in the dbt project is a symbolic link" in _refusal(project)

    def test_a_path_key_through_a_link(self, tmp_path: Path) -> None:
        project = _project(tmp_path, "name: p\nmodel-paths: [lnk/leak]\n")
        _write(tmp_path / "elsewhere", {"leak/a.sql": "select 1"})
        _link(tmp_path / "elsewhere", project / "lnk")
        assert "through the symbolic link 'lnk'" in _refusal(project)

    def test_a_linked_top_level_file(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        _write(tmp_path, {"shared_packages.yml": "packages: []\n"})
        (project / "packages.yml").unlink()
        _link(tmp_path / "shared_packages.yml", project / "packages.yml")
        assert "packages.yml in the dbt project is a symbolic link" in _refusal(project)

    def test_an_unreadable_dbt_project_yml(self, tmp_path: Path) -> None:
        text = _refusal(_project(tmp_path, "name: [unclosed\n"))
        assert "dbt_project.yml in" in text and "could not be read" in text

    def test_a_file_over_the_member_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)
        monkeypatch.setattr(pf, "MAX_FILE_BYTES", 100)
        _write(project, {"seeds/big.csv": "x" * 101})
        assert "seeds/big.csv is over 100 bytes" in _refusal(project)

    def test_a_file_swapped_for_a_link_after_the_walk_is_not_read(self, tmp_path: Path) -> None:
        _write(tmp_path, {"secret.txt": "s3cret", "models/.keep": ""})
        link = _link(tmp_path / "secret.txt", tmp_path / "models" / "a.sql") / "a.sql"
        with pytest.raises(DbtProjectFilesError, match="models/a.sql could not be read"):
            pf._read_bounded(link, "models/a.sql")

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
    def test_a_file_swapped_for_a_fifo_is_not_read(self, tmp_path: Path) -> None:
        fifo = tmp_path / "pipe.sql"
        os.mkfifo(fifo)
        with pytest.raises(DbtProjectFilesError, match="no longer a regular file"):
            pf._read_bounded(fifo, "models/pipe.sql")

    def test_a_project_over_the_bundle_limit(self, tmp_path: Path) -> None:
        assert "the most one bundle packs" in _refusal(_project(tmp_path), budget=10)


#: A dbt build whose project is ``./dbt``; tests vary it with ``{**DBT, ...}``.
DBT: Dict = {"id": "transform", "engine": "dbt-duckdb", "repository": "./dbt"}


class TestWhichBuildsBindAProject:
    @pytest.mark.parametrize(
        "build, binds",
        [
            (DBT, True),
            ({"id": "t", "engine": "dbt"}, True),
            ({**DBT, "execution": {"runtime": {"platform": "databricks"}}}, True),
            ({**DBT, "repository": "https://github.com/o/r?tag=v1"}, False),
            ({**DBT, "repository": "git@github.com:o/r.git"}, False),
            ({**DBT, "pattern": "embedded-logic", "properties": {"sql": "SELECT 1"}}, False),
            ({**DBT, "properties": {"language": "flink_sql"}}, False),
            ({"id": "t", "engine": "sql", "properties": {"sql": "SELECT 1"}}, False),
            ({"id": "t", "engine": "spark"}, False),
            ("not a build", False),
        ],
    )
    def test_binds(self, build, binds: bool) -> None:
        assert binds_dbt_project(build) is binds


def _collect_refusal(contract: Dict, contract_dir: Path) -> str:
    """Why :func:`collect_bundle_projects` refuses ``contract`` in ``contract_dir``."""
    with pytest.raises(DbtProjectFilesError) as exc:
        collect_bundle_projects(contract, contract_dir / "contract.fluid.yaml")
    return str(exc.value)


class TestCollectBundleProjects:
    def test_each_dbt_build_by_id(self, tmp_path: Path) -> None:
        _project(tmp_path)
        builds = [{**DBT, "id": "a"}, {**DBT, "id": "b"}, {"id": "c", "engine": "sql"}]
        contract = {"id": "demo.p", "builds": builds}
        projects = collect_bundle_projects(contract, tmp_path / "contract.fluid.yaml")
        assert sorted(projects) == ["a", "b"]
        assert set(projects["a"]) == READ

    def test_a_missing_project_names_the_build_and_the_path(self, tmp_path: Path) -> None:
        text = _collect_refusal({"builds": [DBT]}, tmp_path)
        assert text.startswith("build 'transform': no dbt_project.yml at ")

    def test_a_project_outside_the_workspace(self, tmp_path: Path) -> None:
        _project(tmp_path)
        (tmp_path / "product").mkdir()
        contract = {"builds": [{**DBT, "repository": "../dbt"}]}
        assert "outside the contract's workspace" in _collect_refusal(
            contract, tmp_path / "product"
        )

    @pytest.mark.parametrize("bad_id", [None, "../escape", "a/b", ".hidden", ""])
    def test_an_id_that_cannot_be_a_bundle_path(self, tmp_path: Path, bad_id) -> None:
        _project(tmp_path)
        text = _collect_refusal({"builds": [{**DBT, "id": bad_id}]}, tmp_path)
        assert text.startswith("builds[0]: a dbt build needs an id")

    def test_two_builds_with_one_id(self, tmp_path: Path) -> None:
        _project(tmp_path)
        contract = {"builds": [{**DBT, "id": "a"}, {**DBT, "id": "a"}]}
        assert "two dbt builds share the id 'a'" in _collect_refusal(contract, tmp_path)

    def test_the_limit_counts_every_project_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _project(tmp_path)
        one = sum(len(data) for data in read_project_files(project).values())
        monkeypatch.setattr(pf, "MAX_PROJECT_BYTES", one + one // 2)
        contract = {"builds": [{**DBT, "id": "a"}, {**DBT, "id": "b"}]}
        assert _collect_refusal(contract, tmp_path).startswith("build 'b': its files pass")


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
        assert text.endswith("models/m05.sql (+7 more)")


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
