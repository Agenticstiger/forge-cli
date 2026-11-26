# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.api.verify_release`` and ``fluid runs verify-release SRC``: a sealed release, on
disk or in S3, checked read-only. Exit 0 intact, 1 not intact, 2 cannot decide."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, List

import pytest

from fluid_build.api import release as api_release
from fluid_build.api import verify_release
from fluid_build.cli import _release_publish as publish
from fluid_build.cli.ops import _cli as ops_cli
from fluid_build.util import s3 as s3_util
from tests.cli._fake_s3 import FakeS3, hex_of, sealed

pytestmark = pytest.mark.unit


def _published(tmp_path: Path) -> Any:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    done = publish.publish_release(tmp_path / "rel", "s3://acme-releases/r/", clients=s3)
    return index, s3, done.uri


def _key(uri: str, rel: str) -> Any:
    bucket, key = s3_util.split_uri(uri)
    return (bucket, key + rel)


# ---------------------------------------------------------------------------
# A directory
# ---------------------------------------------------------------------------


def test_an_untouched_release_is_intact(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    result = verify_release(tmp_path / "rel")
    assert (result.status, result.exit_code, result.files_checked) == ("intact", 0, 2)
    assert result.identity_digest == index["identity_digest"]
    assert (result.product_id, result.environment) == ("acme.orders", "dev")


@pytest.mark.parametrize(
    "tamper, problem",
    [
        (lambda d: (d / "bundle.tgz").write_bytes(b"swapped"), "bundle.tgz: its bytes are not"),
        (lambda d: (d / "bundle.tgz").unlink(), "bundle.tgz: it is missing"),
        (lambda d: _edit(d, identity={"product_id": "other"}), "identity_digest is not the digest"),
        (lambda d: _edit(d, files={"../x": "sha256:" + "0" * 64}), "../x: the index lists it"),
        (lambda d: _edit(d, files={"release.json": "sha256:" + "0" * 64}), "index's own name"),
        (lambda d: _edit(d, files={"bundle.tgz": "md5:1"}), "bundle.tgz: the index lists it"),
        (lambda d: _edit(d, schema="other/1"), "is not a fluid-release/1 record"),
        (lambda d: (d / "release.json").write_text("{", encoding="utf-8"), "is not JSON"),
        (lambda d: (d / "release.json").unlink(), "release.json"),
    ],
)
def test_a_changed_release_is_not_intact(tmp_path: Path, tamper: Any, problem: str) -> None:
    sealed(tmp_path / "rel")
    tamper(tmp_path / "rel")
    result = verify_release(tmp_path / "rel")
    assert (result.status, result.exit_code) == ("not-intact", 1)
    assert any(problem in p for p in result.problems), result.problems


def _edit(directory: Path, **changes: Any) -> None:
    path = directory / "release.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(index.get(key), dict):
            index[key].update(value)
        else:
            index[key] = value
    path.write_text(json.dumps(index), encoding="utf-8")


def test_a_file_reached_through_a_link_is_not_intact(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    real = tmp_path / "elsewhere"
    (tmp_path / "rel" / "reports").rename(real)
    os.symlink(real, tmp_path / "rel" / "reports")
    result = verify_release(tmp_path / "rel")
    assert result.status == "not-intact"
    assert "reports/ship-report.json: it is reached through a link" in result.problems


def test_a_linked_file_is_not_intact(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    (tmp_path / "rel" / "bundle.tgz").rename(tmp_path / "bundle.tgz")
    os.symlink(tmp_path / "bundle.tgz", tmp_path / "rel" / "bundle.tgz")
    assert verify_release(tmp_path / "rel").status == "not-intact"


def test_a_missing_directory_is_not_intact(tmp_path: Path) -> None:
    result = verify_release(tmp_path / "nowhere")
    assert result.exit_code == 1 and "does not exist" in result.problems[0]


@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else True, reason="POSIX, not root")
def test_an_unreadable_file_cannot_be_decided(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    (tmp_path / "rel" / "bundle.tgz").chmod(0)
    try:
        result = verify_release(tmp_path / "rel")
    finally:
        (tmp_path / "rel" / "bundle.tgz").chmod(0o600)
    assert (result.status, result.exit_code) == ("undecided", 2)
    assert result.problems[0].startswith("bundle.tgz: it cannot be read")


def test_the_identity_check_is_the_one_runs_status_makes(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    assert api_release.identity_intact(index)
    assert not api_release.identity_intact({**index, "identity_digest": "sha256:" + "0" * 64})
    assert not api_release.identity_intact({"identity": [], "identity_digest": "x"})


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def test_a_published_release_is_intact_and_read_only(tmp_path: Path) -> None:
    index, s3, uri = _published(tmp_path)
    s3.calls.clear()
    result = verify_release(uri, clients=s3)
    assert (result.status, result.files_checked) == ("intact", 2)
    assert uri.endswith(f"/{hex_of(index)}/")
    assert set(s3.calls) == {"get_object"}


def test_a_tampered_object_is_not_intact(tmp_path: Path) -> None:
    _index, s3, uri = _published(tmp_path)
    s3.objects[_key(uri, "bundle.tgz")] = b"swapped"
    result = verify_release(uri, clients=s3)
    assert result.exit_code == 1
    assert result.problems == ("bundle.tgz: its bytes are not the sha256 the index names",)


def test_a_missing_object_or_index_is_not_intact(tmp_path: Path) -> None:
    _index, s3, uri = _published(tmp_path)
    del s3.objects[_key(uri, "bundle.tgz")]
    assert verify_release(uri, clients=s3).problems == ("bundle.tgz: it is missing",)
    del s3.objects[_key(uri, "release.json")]
    result = verify_release(uri, clients=s3)
    assert result.exit_code == 1 and "no release is published here" in result.problems[0]


def test_a_release_under_another_identity_key_is_not_intact(tmp_path: Path) -> None:
    _index, s3, uri = _published(tmp_path)
    other = uri.rstrip("/").rsplit("/", 1)[0] + "/" + "a" * 64 + "/"
    for rel in ("release.json", "bundle.tgz", "reports/ship-report.json"):
        s3.objects[_key(other, rel)] = s3.objects[_key(uri, rel)]
    result = verify_release(other, clients=s3)
    assert result.exit_code == 1 and "the key of another identity" in result.problems[0]


def test_a_denied_read_cannot_be_decided(tmp_path: Path) -> None:
    _index, s3, uri = _published(tmp_path)
    s3.deny_get.add(_key(uri, "bundle.tgz")[1])
    result = verify_release(uri, clients=s3)
    assert (result.status, result.problems) == (
        "undecided",
        ("bundle.tgz: reading it is denied (AccessDenied)",),
    )
    s3.deny_get.add(_key(uri, "release.json")[1])
    assert verify_release(uri, clients=s3).exit_code == 2


def test_without_boto3_an_s3_source_cannot_be_decided(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(service: str, region: Any, **_: Any) -> Any:
        raise s3_util.S3Unavailable("no boto3")

    monkeypatch.setattr(s3_util, "default_clients", missing)
    result = verify_release("s3://acme-releases/r/")
    assert result.exit_code == 2
    assert result.problems == (
        "reading an s3:// release needs boto3: pip install 'data-product-forge[aws]'",
    )


# ---------------------------------------------------------------------------
# fluid runs verify-release
# ---------------------------------------------------------------------------


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    parser = argparse.ArgumentParser()
    ops_cli.register_runs(parser.add_subparsers())
    args = parser.parse_args(["runs", "verify-release", *argv])
    code = args.func(args, logging.getLogger("test"))
    return code, capsys.readouterr().out


def test_the_command_exits_0_1_and_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    index = sealed(tmp_path / "rel")
    code, out = _run(capsys, str(tmp_path / "rel"))
    assert code == 0
    assert f"{tmp_path / 'rel'}: intact" in out and index["identity_digest"] in out
    (tmp_path / "rel" / "bundle.tgz").write_bytes(b"swapped")
    code, out = _run(capsys, str(tmp_path / "rel"))
    assert code == 1 and "NOT INTACT" in out and "bundle.tgz: its bytes are not" in out
    monkeypatch.setattr(
        s3_util, "default_clients", lambda *a, **k: (_ for _ in ()).throw(s3_util.S3Unavailable())
    )
    code, out = _run(capsys, "s3://acme-releases/r/")
    assert code == 2 and "cannot decide" in out and "data-product-forge[aws]" in out


def test_the_command_writes_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sealed(tmp_path / "rel")
    code, out = _run(capsys, str(tmp_path / "rel"), "--json")
    shown: List[str] = list(json.loads(out))
    assert code == 0 and json.loads(out)["status"] == "intact"
    assert shown == sorted(api_release.ReleaseVerification("", "intact").as_dict())


# ---------------------------------------------------------------------------
# The files identity pins
# ---------------------------------------------------------------------------


def _pinned(directory: Path) -> Path:
    """A release whose identity pins its bundle, an external file, its artifacts and its plan,
    as ``fluid ship`` seals one."""
    import hashlib

    from fluid_build.cli import _ship_release as release
    from fluid_build.forge.core.bundle import merkle_root
    from fluid_build.forge.core.plan_digest import compute_plan_digest

    def sha(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    listed = {"odcs/orders.yaml": sha(b"odcs")}
    manifest = {"digest": merkle_root(listed), "files": listed}
    plan: Any = {"bundleDigest": "sha256:" + "b" * 64, "actions": []}
    plan["planDigest"] = compute_plan_digest(plan)
    files = {
        "bundle.tgz": b"bundle",
        "external/jobs/jobs.zip": b"jobs",
        "artifacts/odcs/orders.yaml": b"odcs",
        "artifacts/MANIFEST.json": json.dumps(manifest).encode(),
        "plan.json": json.dumps(plan).encode(),
    }
    directory.mkdir(parents=True)
    for rel, data in files.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(data)
    identity = {
        "product_id": "acme.orders",
        "environment": "dev",
        "bundle": {"manifest_digest": "sha256:" + "b" * 64, "sha256": sha(b"bundle")},
        "artifacts": {"manifest_digest": manifest["digest"]},
        "plan": {"bundle_digest": plan["bundleDigest"], "plan_digest": plan["planDigest"]},
        "external": [
            {
                "name": "jobs",
                "kind": "spark-package",
                "path": "external/jobs/jobs.zip",
                "digest": {"sha256": hashlib.sha256(b"jobs").hexdigest()},
            }
        ],
    }
    index = {
        "schema": release.RELEASE_SCHEMA,
        "identity": identity,
        "identity_digest": release.identity_digest(identity),
        "files": release._files(directory),
    }
    (directory / "release.json").write_text(json.dumps(index), encoding="utf-8")
    return directory


def _swap(directory: Path, rel: str, data: bytes, *, listed: bool = True) -> None:
    """Replace ``rel`` and rewrite its ``files`` entry to match, keeping the identity."""
    import hashlib

    (directory / rel).write_bytes(data)
    index = json.loads((directory / "release.json").read_text(encoding="utf-8"))
    if listed:
        index["files"][rel] = "sha256:" + hashlib.sha256(data).hexdigest()
    else:
        del index["files"][rel]
    (directory / "release.json").write_text(json.dumps(index), encoding="utf-8")


def test_a_release_whose_identity_pins_its_files_is_intact(tmp_path: Path) -> None:
    result = verify_release(_pinned(tmp_path / "rel"))
    assert (result.status, result.files_checked) == ("intact", 5), result.problems


@pytest.mark.parametrize(
    "rel, problem",
    [
        ("bundle.tgz", "bundle.tgz: the index does not list it with the sha256 the identity's"),
        ("external/jobs/jobs.zip", "external/jobs/jobs.zip: the index does not list it"),
        ("artifacts/odcs/orders.yaml", "the sha256 the artifacts MANIFEST names"),
        ("artifacts/MANIFEST.json", "artifacts/MANIFEST.json: "),
        ("plan.json", "plan.json: "),
    ],
)
def test_a_pinned_file_swapped_with_its_entry_rewritten_is_not_intact(
    tmp_path: Path, rel: str, problem: str
) -> None:
    directory = _pinned(tmp_path / "rel")
    _swap(directory, rel, b'{"swapped": true}')
    result = verify_release(directory)
    assert result.status == "not-intact"
    assert any(p.startswith(problem) or problem in p for p in result.problems), result.problems


@pytest.mark.parametrize("rel", ["bundle.tgz", "external/jobs/jobs.zip", "plan.json"])
def test_a_pinned_file_dropped_from_the_index_is_not_intact(tmp_path: Path, rel: str) -> None:
    directory = _pinned(tmp_path / "rel")
    _swap(directory, rel, b"x", listed=False)
    assert verify_release(directory).status == "not-intact"


def test_a_pinned_release_in_s3_is_bound_too(tmp_path: Path) -> None:
    directory = _pinned(tmp_path / "rel")
    s3 = FakeS3()
    uri = publish.publish_release(directory, "s3://acme-releases/r/", clients=s3).uri
    assert verify_release(uri, clients=s3).intact
    index = json.loads(s3.objects[_key(uri, "release.json")])
    import hashlib

    s3.objects[_key(uri, "bundle.tgz")] = b"evil"
    index["files"]["bundle.tgz"] = "sha256:" + hashlib.sha256(b"evil").hexdigest()
    s3.objects[_key(uri, "release.json")] = json.dumps(index).encode()
    assert verify_release(uri, clients=s3).status == "not-intact"


# ---------------------------------------------------------------------------
# Records a release never writes
# ---------------------------------------------------------------------------


def _reseal(directory: Path, **identity: Any) -> None:
    """Change ``identity`` and recompute ``identity_digest``, as a writer of the record can."""
    from fluid_build.cli import _ship_release as release

    path = directory / "release.json"
    index = json.loads(path.read_text(encoding="utf-8"))
    index["identity"].update(identity)
    index["identity_digest"] = release.identity_digest(index["identity"])
    path.write_text(json.dumps(index), encoding="utf-8")


@pytest.mark.parametrize("external", [5, True, "x", {"a": 1}])
def test_an_external_entry_that_is_not_a_list_is_not_intact(tmp_path: Path, external: Any) -> None:
    sealed(tmp_path / "rel")
    _reseal(tmp_path / "rel", external=external)
    result = verify_release(tmp_path / "rel")
    assert result.exit_code == 1, result.problems


def test_a_manifest_text_utf8_cannot_encode_is_not_intact(tmp_path: Path) -> None:
    directory = _pinned(tmp_path / "rel")
    path = directory / "artifacts/MANIFEST.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"]["odcs/orders.yaml"] = "\ud800"
    _swap(directory, "artifacts/MANIFEST.json", json.dumps(manifest).encode())
    assert verify_release(directory).exit_code == 1


def test_a_file_under_a_file_is_missing(tmp_path: Path) -> None:
    sealed(tmp_path / "rel", {"a": b"x", "b/c": b"y"})
    (tmp_path / "rel" / "b" / "c").unlink()
    (tmp_path / "rel" / "b").rmdir()
    (tmp_path / "rel" / "b").write_bytes(b"a file")
    result = verify_release(tmp_path / "rel")
    assert (result.exit_code, result.problems) == (1, ("b/c: it is missing",))


@pytest.mark.parametrize("rel", ["artifacts/evil.sh", "external/evil/evil.sh"])
def test_a_file_the_identity_does_not_pin_under_its_dirs_is_not_intact(
    tmp_path: Path, rel: str
) -> None:
    directory = _pinned(tmp_path / "rel")
    (directory / rel).parent.mkdir(parents=True, exist_ok=True)
    (directory / rel).write_bytes(b"#!/bin/sh")
    _swap(directory, rel, b"#!/bin/sh")
    result = verify_release(directory)
    assert result.exit_code == 1
    assert any(p.startswith(f"{rel}: the index lists it and the") for p in result.problems)


def test_a_large_plan_and_a_name_the_generators_may_write_verify(tmp_path: Path) -> None:
    directory = _pinned(tmp_path / "rel")
    plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
    from fluid_build.forge.core.plan_digest import compute_plan_digest

    plan["actions"] = [{"op": "noop", "pad": "x" * 1000} for _ in range(2000)]  # ~2 MB
    plan["planDigest"] = compute_plan_digest(plan)
    _swap(directory, "plan.json", json.dumps(plan).encode())
    _reseal(
        directory, plan={"bundle_digest": plan["bundleDigest"], "plan_digest": plan["planDigest"]}
    )
    (directory / "reports" / "my report@1.json").parent.mkdir(exist_ok=True)
    _swap(directory, "reports/my report@1.json", b"{}")
    result = verify_release(directory)
    assert result.intact, result.problems
