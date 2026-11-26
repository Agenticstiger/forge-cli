# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --artifacts LOCK.yaml``: external files in the sealed release.

- The lock names each file, its sha256 and its source; anything else, a path out of the lock's
  directory, a link, or bytes that are not the sha256 named is refused before any stage runs.
- The release copies each file to ``external/<name>/<file>`` and records the entries in its
  ``identity``, sorted by name, without the paths on this machine: two clean runs give one
  identity, changed bytes a new one.
- ``fluid runs status --release-dir`` names each external file and its source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.cli import _release_lock as lock
from fluid_build.cli import _ship_release as release
from fluid_build.cli import _ship_stages as st
from fluid_build.cli import ship
from fluid_build.cli.ops import release_status as rs
from tests.cli.test_ship_release import (  # noqa: F401 - fixtures
    _UNTIL_4,
    _clean_env,
    _install,
    _ship,
    bundled,
    work,
)

pytestmark = pytest.mark.unit

COMMIT = "c0ffee" * 6 + "c0ff"  # a 40-character commit hash
REPO = "https://git.example.com/data/orders-jobs"


@pytest.fixture(autouse=True)
def _no_lock_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(lock.LOCK_ENV, raising=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(**over: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "name": "orders-jobs",
        "kind": "spark-package",
        "path": "dist/orders_jobs-1.4.0.zip",
        "sha256": _sha(b"package"),
        "source": {"repo": REPO, "commit": COMMIT, "ref": "refs/tags/v1.4.0"},
    }
    entry.update(over)
    return entry


def _write_lock(directory: Path, *entries: Dict[str, Any]) -> Path:
    path = directory / "artifacts.lock.yaml"
    path.write_text(yaml.safe_dump({"artifacts": list(entries)}), encoding="utf-8")
    return path


def _package(directory: Path, data: bytes = b"package") -> Path:
    path = directory / "dist" / "orders_jobs-1.4.0.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# The format, without a file
# ---------------------------------------------------------------------------


def test_entries_are_sorted_by_name_and_recorded_without_a_local_path() -> None:
    dag = _entry(name="a-dag", kind="airflow-dag", path="build/dags/orders.py")
    entries = lock.parse_lock({"artifacts": [_entry(), dag]}, "lock")
    assert [e.name for e in entries] == ["a-dag", "orders-jobs"]
    assert entries[0].identity() == {
        "name": "a-dag",
        "kind": "airflow-dag",
        "path": "external/a-dag/orders.py",
        "digest": {"sha256": _sha(b"package")},
        "source": {
            "uri": f"git+{REPO}@refs/tags/v1.4.0",
            "digest": {"gitCommit": COMMIT},
        },
    }
    assert "build/" not in json.dumps([e.identity() for e in entries])


def test_a_source_is_normalized_as_the_ci_source_is() -> None:
    source = {"repo": "https://ci:secret@git.example.com/data/x.git?x=1", "commit": COMMIT.upper()}
    (entry,) = lock.parse_lock({"artifacts": [_entry(source=source)]}, "lock")
    assert entry.identity()["source"] == {
        "uri": "git+https://git.example.com/data/x.git",
        "digest": {"gitCommit": COMMIT},
    }


@pytest.mark.parametrize(
    "data, problem",
    [
        (["x"], "expected a mapping with an artifacts list"),
        ({"artifacts": [_entry()], "extra": 1}, "unsupported field(s) ['extra']"),
        ({"artifacts": []}, "expected a list of one entry or more"),
        ({"artifacts": [_entry(extra=1)]}, "unsupported field(s) ['extra']"),
        ({"artifacts": [{"name": "x", "kind": "file"}]}, "path, sha256, source required"),
        ({"artifacts": [_entry(name="../x")]}, ".name: expected letters"),
        ({"artifacts": [_entry(kind="jar")]}, ".kind: expected one of"),
        ({"artifacts": [_entry(kind=["file"])]}, ".kind: expected one of"),
        ({"artifacts": [_entry(kind={"a": 1})]}, ".kind: expected one of"),
        ({"artifacts": [_entry(path="/home/ci/dist/x.zip")]}, "is absolute"),
        ({"artifacts": [_entry(path="C:/dist/x.zip")]}, "is absolute"),
        ({"artifacts": [_entry(kind="file", path=".git/config")]}, "hidden file or directory"),
        ({"artifacts": [_entry(kind="file", path="dist/.env")]}, "hidden file or directory"),
        ({"artifacts": [_entry(path="\ud800/x.zip")]}, "has characters a release"),
        ({"artifacts": [_entry(path="my dist/x.zip")]}, "has characters a release"),
        ({"artifacts": [_entry(path="../dist/x.zip")]}, "leads out of its directory"),
        ({"artifacts": [_entry(kind="file", path=".")]}, "names no file"),
        ({"artifacts": [_entry(kind="file", path="./")]}, "names no file"),
        ({"artifacts": [_entry(path="dist\\x.zip")]}, "expected / between directories"),
        ({"artifacts": [_entry(path="dist/my package.zip")]}, "has characters a release"),
        ({"artifacts": [_entry(path="dist/x.tar")]}, "a spark-package is a .whl or .zip file"),
        ({"artifacts": [_entry(kind="airflow-dag")]}, None),  # a packaged DAG is a .zip
        ({"artifacts": [_entry(sha256="ABC")]}, ".sha256: expected the file's sha256"),
        ({"artifacts": [_entry(source={"repo": "/srv/repo", "commit": COMMIT})]}, ".repo:"),
        (
            {"artifacts": [_entry(source={"repo": "https://h.example/a@b", "commit": COMMIT})]},
            "with @ in its path",
        ),
        ({"artifacts": [_entry(source={"repo": REPO, "commit": "main"})]}, ".commit:"),
        ({"artifacts": [_entry(source={"repo": REPO, "commit": COMMIT, "ref": "a b"})]}, ".ref:"),
        ({"artifacts": [_entry(source={"repo": REPO})]}, ".commit:"),
        ({"artifacts": [_entry(source={"repo": REPO, "commit": COMMIT, "x": 1})]}, "['x']"),
        ({"artifacts": [_entry(), _entry(name="Orders-Jobs")]}, "two entries are named"),
        ({"artifacts": [_entry(name=f"n{i}") for i in range(257)]}, "at most 256"),
    ],
)
def test_an_entry_the_release_cannot_seal_is_refused(data: Any, problem: str) -> None:
    if problem is None:
        assert lock.parse_lock(data, "lock")
        return
    with pytest.raises(lock.LockError) as raised:
        lock.parse_lock(data, "lock")
    assert problem in str(raised.value)


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


def test_bytes_that_are_not_the_sha256_named_are_refused(tmp_path: Path) -> None:
    _package(tmp_path, b"other bytes")
    with pytest.raises(lock.LockError) as raised:
        lock.load(str(_write_lock(tmp_path, _entry())))
    message = str(raised.value)
    assert f"does not have the sha256 the lock names ({_sha(b'package')})" in message
    assert _sha(b"other bytes") not in message  # never the digest the file has


def test_a_link_is_refused(tmp_path: Path) -> None:
    real = _package(tmp_path / "elsewhere")
    (tmp_path / "dist").mkdir()
    os.symlink(real, tmp_path / "dist" / "orders_jobs-1.4.0.zip")
    with pytest.raises(lock.LockError) as raised:
        lock.load(str(_write_lock(tmp_path, _entry())))
    assert "Symlinked artifact paths are not allowed" in str(raised.value)


def test_a_lock_that_is_a_link_is_refused(tmp_path: Path) -> None:
    _package(tmp_path)
    target = _write_lock(tmp_path, _entry())
    os.symlink(target, tmp_path / "link.yaml")
    with pytest.raises(lock.LockError, match="Symlinked artifacts lock paths"):
        lock.load(str(tmp_path / "link.yaml"))


def test_a_path_out_of_the_locks_directory_is_refused(tmp_path: Path) -> None:
    _package(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(lock.LockError, match="leads out of its directory"):
        lock.load(str(_write_lock(project, _entry(path="../dist/orders_jobs-1.4.0.zip"))))


def test_a_linked_directory_that_leads_out_is_refused(tmp_path: Path) -> None:
    _package(tmp_path / "outside")
    project = tmp_path / "project"
    project.mkdir()
    os.symlink(tmp_path / "outside" / "dist", project / "dist")
    with pytest.raises(lock.LockError, match="leads out of the lock's directory"):
        lock.load(str(_write_lock(project, _entry())))


def test_a_link_inside_the_directory_to_a_hidden_one_is_refused(tmp_path: Path) -> None:
    # cfg -> .git: every segment of cfg/config is a plain name, and the link stays inside.
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(b"token")
    os.symlink(tmp_path / ".git", tmp_path / "cfg")
    entry = _entry(kind="file", path="cfg/config", sha256=_sha(b"token"))
    with pytest.raises(lock.LockError, match="goes through a linked directory"):
        lock.load(str(_write_lock(tmp_path, entry)))


def test_any_linked_directory_inside_is_refused(tmp_path: Path) -> None:
    _package(tmp_path)
    os.symlink(tmp_path / "dist", tmp_path / "build")
    with pytest.raises(lock.LockError, match="goes through a linked directory"):
        lock.load(str(_write_lock(tmp_path, _entry(path="build/orders_jobs-1.4.0.zip"))))


def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "dist" / "orders_jobs-1.4.0.zip").mkdir(parents=True)
    with pytest.raises(lock.LockError, match="is not a regular file"):
        lock.load(str(_write_lock(tmp_path, _entry())))


def test_a_key_given_twice_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "artifacts.lock.yaml"
    path.write_text("artifacts: []\nartifacts: []\n", encoding="utf-8")
    with pytest.raises(lock.LockError, match="cannot read it"):
        lock.load(str(path))


def test_a_relative_path_is_read_from_the_locks_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _package(project)
    (found,) = lock.load(str(_write_lock(project, _entry())))
    assert found.local == project.resolve() / "dist" / "orders_jobs-1.4.0.zip"


# ---------------------------------------------------------------------------
# fluid ship
# ---------------------------------------------------------------------------


def test_the_flag_without_a_release_is_refused(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _install(monkeypatch, work / "missing.tgz")
    assert _ship([*_UNTIL_4, "--artifacts", "lock.yaml"]) == 2
    assert fake.calls == []
    assert "names files to seal into a release, and this run publishes none" in caplog.text


def test_a_blank_flag_is_refused(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--artifacts "$LOCK"` with LOCK unset seals no external file without saying so."""
    monkeypatch.setenv(lock.LOCK_ENV, "lock.yaml")
    for value in ("", "  "):
        with pytest.raises(st.ShipUsageError, match="needs the path of a lock file"):
            ship._artifacts_lock(argparse.Namespace(artifacts=value), work / "rel")


def test_the_variable_is_read_only_by_a_run_that_seals_a_release(
    work: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One variable for every job of a pipeline: a job that seals nothing (an apply) leaves
    it alone, and the flag wins over it."""
    monkeypatch.setenv(lock.LOCK_ENV, "lock.yaml")
    args = argparse.Namespace(artifacts=None)
    assert ship._artifacts_lock(args, None) == ""
    assert ship._artifacts_lock(args, work / "rel") == str(work / "lock.yaml")
    args.artifacts = "other.yaml"
    assert ship._artifacts_lock(args, work / "rel") == str(work / "other.yaml")


def test_a_lock_the_release_cannot_seal_refuses_the_run_before_any_stage(
    work: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _install(monkeypatch, work / "missing.tgz")
    _package(work, b"other bytes")
    _write_lock(work, _entry())
    assert _ship([*_UNTIL_4, "--release-dir", "../rel", "--artifacts", "artifacts.lock.yaml"]) == 2
    assert fake.calls == []
    assert "does not have the sha256 the lock names" in caplog.text
    assert not (work.parent / "rel").exists()


def _release(work: Path, name: str) -> Dict[str, Any]:
    assert (
        _ship([*_UNTIL_4, "--release-dir", f"../{name}", "--artifacts", "artifacts.lock.yaml"]) == 0
    )
    return json.loads((work.parent / name / "release.json").read_text(encoding="utf-8"))


def test_a_release_seals_the_external_files_and_their_sources(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    _package(work)
    _write_lock(work, _entry())
    index = _release(work, "rel")
    rel = work.parent / "rel"
    assert (rel / "external/orders-jobs/orders_jobs-1.4.0.zip").read_bytes() == b"package"
    assert index["files"]["external/orders-jobs/orders_jobs-1.4.0.zip"] == (
        "sha256:" + _sha(b"package")
    )
    assert index["identity"]["external"] == [
        lock.parse_lock({"artifacts": [_entry()]}, "x")[0].identity()
    ]
    assert index["identity_digest"] == release.identity_digest(index["identity"])
    assert str(work) not in json.dumps(index["identity"])

    found = rs.find_releases([str(rel)], "example.release")
    (sealed,) = found.releases
    assert sealed.external == [
        {
            "name": "orders-jobs",
            "kind": "spark-package",
            "sha256": _sha(b"package"),
            "source": f"git+{REPO}@refs/tags/v1.4.0",
            "commit": COMMIT,
        }
    ]
    assert (
        f"  external orders-jobs (spark-package) sha256:{_sha(b'package')}"
        f"  from git+{REPO}@refs/tags/v1.4.0 {COMMIT}"
    ) in rs.describe(sealed)


def test_two_clean_runs_give_one_identity_and_changed_bytes_a_new_one(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    _package(work)
    _write_lock(work, _entry())
    first, second = _release(work, "first"), _release(work, "second")
    assert first["identity_digest"] == second["identity_digest"]
    _package(work, b"package, rebuilt")
    _write_lock(work, _entry(sha256=_sha(b"package, rebuilt")))
    third = _release(work, "third")
    assert third["identity_digest"] != first["identity_digest"]
    assert third["identity"]["external"][0]["digest"] == {"sha256": _sha(b"package, rebuilt")}


def test_a_file_changed_while_the_run_ran_publishes_nothing(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _install(monkeypatch, bundled)
    package = _package(work)
    _write_lock(work, _entry())
    stages = fake.__call__

    def stage(cmd, **kw):  # stage 2 rewrites the package after the lock was read
        if cmd[3:4] == ["validate"]:
            package.write_bytes(b"swapped")
        return stages(cmd, **kw)

    monkeypatch.setattr("fluid_build.cli.ship.subprocess.run", stage)
    code = _ship([*_UNTIL_4, "--release-dir", "../rel", "--artifacts", "artifacts.lock.yaml"])
    assert code == 1
    assert not (work.parent / "rel").exists()
    shipped = json.loads((work / "runtime" / "ship-report.json").read_text(encoding="utf-8"))
    assert shipped["release"]["status"] == "not-published"
    assert "does not have the sha256 the lock names" in shipped["release"]["reason"]


def test_the_copy_is_hashed_again_before_the_release_is_published(tmp_path: Path) -> None:
    source = _package(tmp_path / "src")
    (entry,) = lock.parse_lock({"artifacts": [_entry()]}, "x")
    artifact = lock.ExternalArtifact(entry, source)
    staging = tmp_path / "staging"
    staging.mkdir()
    source.write_bytes(b"changed after it was checked")
    with pytest.raises(release.ReleaseError, match="changed while the run ran"):
        release._copy_external(staging, {"external": [entry.identity()]}, [artifact])
    assert not (staging / "external").exists() or not any((staging / "external").rglob("*"))
    with pytest.raises(release.ReleaseError, match="names other external files"):
        release._copy_external(staging, {}, [artifact])


def test_the_copy_reads_no_link_and_leaves_nothing_behind(tmp_path: Path) -> None:
    source = _package(tmp_path / "src")
    os.symlink(source, tmp_path / "link.zip")
    with pytest.raises(release.ReleaseError, match="is not a regular file"):
        release._copy_hashed(tmp_path / "link.zip", tmp_path / "out" / "x.zip")
    assert not (tmp_path / "out" / "x.zip").exists()
    assert release._copy_hashed(source, tmp_path / "out" / "x.zip") == _sha(b"package")
    assert (tmp_path / "out" / "x.zip").read_bytes() == b"package"


def test_an_external_file_named_like_the_index_is_listed(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    (staging / "external" / "x").mkdir(parents=True)
    (staging / "release.json").write_text("{}", encoding="utf-8")
    (staging / "external" / "x" / "release.json").write_text("[]", encoding="utf-8")
    assert list(release._files(staging)) == ["external/x/release.json"]


def test_status_reads_as_many_external_files_as_a_lock_may_list() -> None:
    items = [{"name": f"n{i:03d}", "kind": "file"} for i in range(lock.MAX_ENTRIES + 5)]
    assert len(rs._external({"external": items})) == lock.MAX_ENTRIES == rs.MAX_EXTERNAL
