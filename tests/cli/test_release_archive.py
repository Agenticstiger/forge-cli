# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --release-archive PATH``: the sealed release as one
reproducible ``.tgz``, written only after the seal, never left half-written."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.cli import _release_archive as archive
from fluid_build.cli import ship
from tests.cli._fake_s3 import FakeS3
from tests.cli.test_ship_release import (  # noqa: F401 - fixtures
    _UNTIL_4,
    _clean_env,
    _install,
    _ship,
    bundled,
    work,
)

pytestmark = pytest.mark.unit


def _report(work: Path) -> Any:
    return json.loads((work / "runtime" / "ship-report.json").read_text(encoding="utf-8"))


def _tree(root: Path) -> Dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _members(path: Path) -> Dict[str, bytes]:
    with tarfile.open(path, mode="r:gz") as tar:
        out = {}
        for member in tar.getmembers():
            if member.isfile():
                body = tar.extractfile(member)
                assert body is not None
                out[member.name] = body.read()
        return out


def test_ship_writes_the_sealed_release_as_one_archive_and_prints_its_sha256(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install(monkeypatch, bundled)
    assert _ship([*_UNTIL_4, "--release-dir", "../rel", "--release-archive", "../out.tgz"]) == 0
    tgz = work.parent / "out.tgz"
    digest = hashlib.sha256(tgz.read_bytes()).hexdigest()
    assert f"Release archive: {tgz} (sha256:{digest})" in capsys.readouterr().out
    block = _report(work)["release"]["archive"]
    assert block == {"path": str(tgz), "status": "written", "sha256": f"sha256:{digest}"}
    # the sealed directory as it is: release.json at the root, no folder around it
    assert _members(tgz) == _tree(work.parent / "rel")
    assert "release.json" in _members(tgz)
    assert list(work.parent.glob(".out.tgz.*")) == []


def test_the_archive_is_reproducible(tmp_path: Path) -> None:
    from tests.cli._fake_s3 import sealed

    sealed(
        tmp_path / "rel", {"reports/ship-report.json": b"{}", "external/pkg/a.zip": b"z" * 70000}
    )
    one = archive.write_archive(tmp_path / "rel", str(tmp_path / "a.tgz"))
    os.utime(tmp_path / "rel" / "bundle.tgz", (1, 1))  # a file's mtime is no part of it
    two = archive.write_archive(tmp_path / "rel", str(tmp_path / "sub" / "b.tgz"))
    assert one == two
    data = (tmp_path / "a.tgz").read_bytes()
    assert data == (tmp_path / "sub" / "b.tgz").read_bytes()
    assert one == hashlib.sha256(data).hexdigest()
    # gzip header: mtime 0 and no file name (flags byte has no FNAME)
    assert data[4:8] == b"\0\0\0\0" and not data[3] & 0x08
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        members = tar.getmembers()
    files = [m.name for m in members if m.isfile()]
    assert files == sorted(files) and "release.json" in files
    for member in members:
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")
        assert member.mtime == 0
        assert member.mode == (0o755 if member.isdir() else 0o644)


def test_a_failed_seal_leaves_no_archive(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled, fail="validate-artifacts")
    assert _ship([*_UNTIL_4, "--release-dir", "../rel", "--release-archive", "../out.tgz"]) == 1
    assert not (work.parent / "out.tgz").exists()
    assert list(work.parent.glob(".out.tgz.*")) == []
    assert "archive" not in _report(work)["release"]


def test_a_failed_archive_leaves_nothing_and_keeps_the_sealed_release(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.forge.core import bundle

    def broken(out: Any, entries: Any) -> None:
        out.write(b"partial")
        raise OSError(28, "No space left on device")

    _install(monkeypatch, bundled)
    monkeypatch.setattr(bundle, "write_tgz_stream", broken)
    assert _ship([*_UNTIL_4, "--release-dir", "../rel", "--release-archive", "../out.tgz"]) == 1
    assert not (work.parent / "out.tgz").exists()
    assert list(work.parent.glob(".out.tgz.*")) == []
    block = _report(work)["release"]
    assert block["status"] == "published" and (work.parent / "rel" / "release.json").is_file()
    assert block["archive"]["status"] == "not-written"
    assert "No space left on device" in block["archive"]["reason"]


def test_an_archive_already_there_is_replaced_whole(tmp_path: Path) -> None:
    from tests.cli._fake_s3 import sealed

    sealed(tmp_path / "rel")
    (tmp_path / "a.tgz").write_bytes(b"an older archive")
    digest = archive.write_archive(tmp_path / "rel", str(tmp_path / "a.tgz"))
    assert hashlib.sha256((tmp_path / "a.tgz").read_bytes()).hexdigest() == digest


def test_a_link_inside_the_sealed_release_is_refused(tmp_path: Path) -> None:
    from tests.cli._fake_s3 import sealed

    sealed(tmp_path / "rel")
    (tmp_path / "rel" / "reports" / "evil").symlink_to("/etc/hosts")
    with pytest.raises(archive.ArchiveError, match="is not a regular file"):
        archive.write_archive(tmp_path / "rel", str(tmp_path / "a.tgz"))
    assert not (tmp_path / "a.tgz").exists() and list(tmp_path.glob(".a.tgz.*")) == []


@pytest.mark.parametrize(
    "argv, problem",
    [
        (["--release-archive", "../out.tgz"], "add --release-dir DIR"),
        (["--release-dir", "../rel", "--release-archive", ""], "needs the path of the .tgz"),
        (["--release-dir", "../rel", "--release-archive", "  "], "needs the path of the .tgz"),
        (["--release-dir", "../rel", "--release-archive", "../rel/a.tgz"], "lies inside"),
        (["--release-dir", "../rel", "--release-archive", "../rel"], "lies inside"),
        (["--release-dir", "../rel", "--release-archive", "../folder"], "is not a file"),
        (["--release-dir", "../rel", "--release-archive", "../link.tgz"], "is a link"),
    ],
)
def test_ship_refuses_an_archive_it_cannot_write_before_any_stage(
    work: Path,
    bundled: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: Any,
    argv: Any,
    problem: str,
) -> None:
    (work.parent / "folder").mkdir()
    (work.parent / "link.tgz").symlink_to(work.parent / "elsewhere.tgz")
    fake = _install(monkeypatch, bundled)
    assert _ship([*_UNTIL_4, *argv]) == 2
    assert problem in caplog.text
    assert fake.calls == []


def test_without_the_flag_nothing_changes(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    assert _ship([*_UNTIL_4, "--release-dir", "../rel"]) == 0
    assert "archive" not in _report(work)["release"]
    assert sorted(p.name for p in work.parent.iterdir()) == ["project", "rel"]


def test_with_release_to_the_archive_is_written_first_and_kept_when_the_upload_fails(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _release_publish as publish

    monkeypatch.setattr("fluid_build.util.s3.installed", lambda: True)
    for name, value in {
        "GITHUB_RUN_ID": "42",
        "GITHUB_SHA": "c0ffee" * 6 + "c0ff",
        "GITHUB_REPOSITORY": "org/orders",
        "GITHUB_SERVER_URL": "https://github.com",
    }.items():
        monkeypatch.setenv(name, value)
    _install(monkeypatch, bundled)
    s3 = FakeS3()
    s3.deny_put.add("*")
    monkeypatch.setattr(publish, "_default_clients", s3)
    argv = [*_UNTIL_4, "--release-dir", "../rel", "--release-archive", "../out.tgz"]
    assert _ship([*argv, "--release-to", "s3://acme-releases/handoff/"]) == 1
    block = _report(work)["release"]
    assert block["remote"]["status"] == "not-published"
    assert block["archive"]["status"] == "written"
    assert (work.parent / "out.tgz").is_file()


def test_explain_names_the_archive(work: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _ship(["--explain", "--release-dir", "../rel", "--release-archive", "../o.tgz"]) == 0
    assert f"archive: then written as one .tgz to {work.parent / 'o.tgz'}" in (
        capsys.readouterr().out
    )


def test_the_flag_is_on_the_parser() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers())
    args = parser.parse_args(["ship", "--release-archive", "out.tgz"])
    assert args.release_archive == "out.tgz"
