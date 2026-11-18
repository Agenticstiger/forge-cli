# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The dbt projects a bundle packs, and how a plan binds them.

``build_bundle_tgz(dbt_projects=...)`` packs each build's files under
``sources/dbt/<build id>/`` byte for byte, inside the merkle root;
``inject_digests`` records them as ``dbtProjects`` under ``planDigest``; and
``verify_plan_binding`` refuses a record that is not the bundle's.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.forge.core import bundle as bundle_mod
from fluid_build.forge.core.bundle import (
    build_bundle_tgz,
    merkle_root,
    read_bundle_dbt_trees,
    validate_manifest,
)
from fluid_build.forge.core.plan_digest import (
    DBT_PROJECTS_FIELD,
    PlanBindingError,
    compute_plan_digest,
    inject_digests,
    read_bundle_digest,
    verify_plan_binding,
)

CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "demo.orders",
    "builds": [{"id": "transform", "engine": "dbt-duckdb", "repository": "./dbt"}],
}

PROJECT: Dict[str, bytes] = {
    "dbt_project.yml": b"name: p\n",
    "models/orders.sql": b"select 1 as id\r\n",  # raw: no newline normalisation
    "seeds/s.csv": b"id\n1",  # raw: no trailing newline added
}


def _members(tgz: Path) -> Dict[str, bytes]:
    with tarfile.open(tgz, "r:gz") as tar:
        return {
            m.name: tar.extractfile(m).read()  # type: ignore[union-attr]
            for m in tar.getmembers()
            if m.isfile()
        }


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class TestPacking:
    def test_files_are_packed_as_they_are_under_their_build(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT})
        members = _members(tgz)
        for rel, data in PROJECT.items():
            assert members[f"sources/dbt/transform/{rel}"] == data
        manifest = json.loads(members["MANIFEST.json"])
        assert manifest["files"]["sources/dbt/transform/models/orders.sql"] == _sha(
            PROJECT["models/orders.sql"]
        )
        validate_manifest(tgz)

    def test_the_merkle_root_covers_the_project(self, tmp_path: Path) -> None:
        without, with_project = tmp_path / "a.tgz", tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, without)
        build_bundle_tgz(CONTRACT, with_project, dbt_projects={"transform": PROJECT})
        changed = {**PROJECT, "models/orders.sql": b"select 2 as id\n"}
        other = tmp_path / "c.tgz"
        build_bundle_tgz(CONTRACT, other, dbt_projects={"transform": changed})
        digests = {read_bundle_digest(p) for p in (without, with_project, other)}
        assert len(digests) == 3

    def test_rebundling_is_byte_identical(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.tgz", tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, a, dbt_projects={"transform": dict(PROJECT)})
        reordered = dict(reversed(list(PROJECT.items())))
        build_bundle_tgz(CONTRACT, b, dbt_projects={"transform": reordered})
        assert a.read_bytes() == b.read_bytes()

    def test_no_project_leaves_the_bundle_as_it_was(self, tmp_path: Path) -> None:
        a, b = tmp_path / "a.tgz", tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, a)
        build_bundle_tgz(CONTRACT, b, dbt_projects={})
        assert a.read_bytes() == b.read_bytes()
        assert not any(name.startswith("sources/dbt/") for name in _members(a))

    def test_headers_are_normalised(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT})
        with tarfile.open(tgz, "r:gz") as tar:
            for member in tar.getmembers():
                assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
                assert member.mtime == bundle_mod._DEFAULT_MTIME
                assert member.mode == (0o755 if member.isdir() else 0o644)

    def test_read_back_by_build(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT, "other": PROJECT})
        trees = read_bundle_dbt_trees(tgz)
        assert sorted(trees) == ["other", "transform"]
        assert trees["transform"] == {rel: _sha(data) for rel, data in PROJECT.items()}

    def test_a_bundle_without_projects_reads_back_empty(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz)
        assert read_bundle_dbt_trees(tgz) == {}


class TestRefusals:
    @pytest.mark.parametrize("build_id", ["../x", "a/b", ".hidden", "", "a\\b"])
    def test_a_build_id_that_is_not_one_directory(self, tmp_path: Path, build_id: str) -> None:
        with pytest.raises(ValueError, match="cannot be a bundle directory"):
            build_bundle_tgz(CONTRACT, tmp_path / "b.tgz", dbt_projects={build_id: PROJECT})

    @pytest.mark.parametrize(
        "rel", ["../escape.sql", "/abs.sql", "models/../../x.sql", "models/.h.sql", "a//b.sql"]
    )
    def test_a_file_path_that_is_not_plain(self, tmp_path: Path, rel: str) -> None:
        with pytest.raises(ValueError, match="not a plain relative path"):
            build_bundle_tgz(CONTRACT, tmp_path / "b.tgz", dbt_projects={"transform": {rel: b"x"}})

    def test_more_dbt_bytes_than_a_bundle_carries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bundle_mod, "MAX_DBT_SOURCE_BYTES", 10)
        with pytest.raises(ValueError, match="bytes of dbt project files at most"):
            build_bundle_tgz(CONTRACT, tmp_path / "b.tgz", dbt_projects={"transform": PROJECT})

    def test_more_entries_than_the_reader_takes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bundle_mod, "MAX_BUNDLE_MEMBERS", 8)
        out = tmp_path / "b.tgz"
        with pytest.raises(ValueError, match="entries"):
            build_bundle_tgz(CONTRACT, out, dbt_projects={"transform": PROJECT})
        assert not out.exists()

    @pytest.mark.parametrize(
        "rel",
        [
            "models/" + "n" * 101 + ".sql",  # a file name over the format's 100 bytes
            "/".join(["d" * 40] * 6) + "/x.sql",  # no split fits prefix 155 + name 100
        ],
    )
    def test_a_path_the_tar_format_cannot_hold(self, tmp_path: Path, rel: str) -> None:
        out = tmp_path / "b.tgz"
        with pytest.raises(ValueError, match="too long a path"):
            build_bundle_tgz(CONTRACT, out, dbt_projects={"transform": {rel: b"x"}})
        assert not out.exists()

    def test_a_long_path_the_format_splits_is_packed(self, tmp_path: Path) -> None:
        rel = "models/" + "d" * 110 + "/x.sql"  # 150 bytes: prefix + name
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": {rel: b"x"}})
        validate_manifest(tgz)
        assert read_bundle_dbt_trees(tgz) == {"transform": {rel: _sha(b"x")}}

    def test_a_tampered_project_file_fails_the_manifest(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT})
        members = _members(tgz)
        members["sources/dbt/transform/models/orders.sql"] = b"select 666\n"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in sorted(members.items()):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        tgz.write_bytes(buf.getvalue())
        with pytest.raises(ValueError, match="SHA-256 mismatch.*orders.sql"):
            validate_manifest(tgz)


class _PermissiveSchema:
    def get_schema(self, version):
        return {"type": "object"}


class TestValidate:
    """``fluid validate <bundle>``: one finding per packed project, never one per file."""

    def _codes(self, tgz: Path) -> Dict[str, str]:
        from fluid_build.forge.core.validators import validate_bundle

        report = validate_bundle(tgz, schema_manager=_PermissiveSchema(), strict=True)
        return {i.code: i.severity for i in report.issues}

    def test_a_bound_project_is_one_info(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT})
        assert self._codes(tgz) == {"DBT-PROJECT-BOUND": "info"}

    def test_a_project_no_dbt_build_runs_warns(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT, "ghost": PROJECT})
        codes = self._codes(tgz)
        assert codes["DBT-PROJECT-ORPHAN"] == "warning"
        assert codes["DBT-PROJECT-BOUND"] == "info"

    def test_a_project_without_dbt_project_yml_warns(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        partial = {k: v for k, v in PROJECT.items() if k != "dbt_project.yml"}
        build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": partial})
        assert self._codes(tgz) == {"DBT-PROJECT-INCOMPLETE": "warning"}


def _plan() -> Dict[str, Any]:
    return {
        "format_version": "0.7.5",
        "generated_at": 1.0,
        "mode": "amend-and-build",
        "contract": copy.deepcopy(CONTRACT),
        "actions": [],
    }


@pytest.fixture
def bound(tmp_path: Path):
    tgz = tmp_path / "b.tgz"
    build_bundle_tgz(CONTRACT, tgz, dbt_projects={"transform": PROJECT})
    return tgz, inject_digests(_plan(), bundle_path=tgz)


class TestPlanRecord:
    def test_a_bound_plan_records_each_project(self, bound) -> None:
        _, plan = bound
        record = plan[DBT_PROJECTS_FIELD]["transform"]
        files = {rel: _sha(data) for rel, data in PROJECT.items()}
        assert record == {"digest": merkle_root(files), "files": files}

    def test_the_record_is_under_plan_digest(self, bound) -> None:
        _, plan = bound
        edited = copy.deepcopy(plan)
        edited[DBT_PROJECTS_FIELD]["transform"]["files"]["models/orders.sql"] = _sha(b"x")
        assert compute_plan_digest(edited) != plan["planDigest"]

    def test_a_bundle_without_projects_records_none(self, tmp_path: Path) -> None:
        tgz = tmp_path / "b.tgz"
        build_bundle_tgz(CONTRACT, tgz)
        assert DBT_PROJECTS_FIELD not in inject_digests(_plan(), bundle_path=tgz)

    def test_a_raw_plan_records_none(self) -> None:
        assert DBT_PROJECTS_FIELD not in inject_digests(_plan())

    def test_a_rebind_keeps_the_record(self, bound) -> None:
        tgz, plan = bound
        again = inject_digests({**plan, "cost_estimate": {}}, bundle_path=tgz, force=True)
        assert again[DBT_PROJECTS_FIELD] == plan[DBT_PROJECTS_FIELD]


class TestVerify:
    def test_the_plan_and_its_bundle_pass(self, bound) -> None:
        tgz, plan = bound
        verify_plan_binding(plan, bundle_path=tgz)

    def test_an_edited_record_is_plan_tamper(self, bound) -> None:
        tgz, plan = bound
        plan[DBT_PROJECTS_FIELD]["transform"]["files"]["models/orders.sql"] = _sha(b"x")
        with pytest.raises(PlanBindingError) as exc:
            verify_plan_binding(plan, bundle_path=tgz)
        assert exc.value.kind == "plan-tamper"

    def test_a_record_that_is_not_the_bundles_is_bundle_mismatch(self, bound) -> None:
        tgz, plan = bound
        plan[DBT_PROJECTS_FIELD]["transform"]["files"]["models/orders.sql"] = _sha(b"x")
        plan["planDigest"] = compute_plan_digest(plan)  # re-signed: not a tamper
        with pytest.raises(PlanBindingError) as exc:
            verify_plan_binding(plan, bundle_path=tgz)
        assert exc.value.kind == "bundle-mismatch"

    def test_a_stripped_record_is_bundle_mismatch(self, bound) -> None:
        tgz, plan = bound
        del plan[DBT_PROJECTS_FIELD]
        plan["planDigest"] = compute_plan_digest(plan)
        with pytest.raises(PlanBindingError) as exc:
            verify_plan_binding(plan, bundle_path=tgz)
        assert exc.value.kind == "bundle-mismatch"

    def test_a_raw_plan_carrying_a_record_is_refused(self) -> None:
        plan = inject_digests(_plan())
        plan[DBT_PROJECTS_FIELD] = {"transform": {"digest": "sha256:0", "files": {}}}
        plan["planDigest"] = compute_plan_digest(plan)
        with pytest.raises(PlanBindingError) as exc:
            verify_plan_binding(plan)
        assert exc.value.kind == "binding-mode-mismatch"
