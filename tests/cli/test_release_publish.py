# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --release-to s3://bucket/prefix/``: the sealed release, published
write-once under ``<prefix>/<product>/<env>/<identity hex>/``.

- Each file is one PutObject with ``ChecksumSHA256`` and ``If-None-Match: *``, read back with
  ``ChecksumMode=ENABLED``; ``release.json`` goes last.
- A 412 on an object with our SHA-256 is already published (a re-run is idempotent); any other
  object, a denied write or a bad read-back refuses the run, and says what was written.
- The run's report names where the release went, or why it did not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fluid_build.api import release as api_release
from fluid_build.cli import _release_publish as publish
from fluid_build.cli import ship
from tests.cli._fake_s3 import FakeS3, hex_of, sealed
from tests.cli.test_ship_release import (  # noqa: F401 - fixtures
    _UNTIL_4,
    _clean_env,
    _install,
    _ship,
    bundled,
    work,
)

pytestmark = pytest.mark.unit

TARGET = "s3://acme-releases/handoff/"


def _prefix(index: Any) -> str:
    return f"handoff/acme.orders/dev/{hex_of(index)}/"


# ---------------------------------------------------------------------------
# The target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, prefix",
    [
        ("s3://acme-releases", ""),
        ("s3://acme-releases/", ""),
        ("s3://acme-releases/a/b", "a/b/"),
        ("s3://acme-releases/a/b/", "a/b/"),
    ],
)
def test_a_target_is_a_bucket_and_a_prefix(value: str, prefix: str) -> None:
    assert publish.parse_target(value) == publish.Target("acme-releases", prefix)


@pytest.mark.parametrize(
    "value, problem",
    [
        ("acme-releases/a", "expected s3://bucket/prefix/"),
        ("s3://Acme/a", "is not an S3 bucket name"),
        ("s3://a..b/a", "is not an S3 bucket name"),
        ("s3://acme-releases/a/../b", "has characters a release key does not take"),
        ("s3://acme-releases/a b/", "has characters a release key does not take"),
        ("s3://acme-releases//a", "the prefix is not a/b/c"),
    ],
)
def test_a_target_that_is_not_one_is_refused(value: str, problem: str) -> None:
    with pytest.raises(publish.PublishError, match=problem):
        publish.parse_target(value)


def test_the_target_needs_a_release_and_boto3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "add --release-dir" in str(publish.refusal(TARGET, None, ()))
    assert "add --release-to" in str(publish.refusal("", tmp_path, ("versioning",)))
    assert publish.refusal(TARGET, tmp_path, ()) is None
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert "pip install 'data-product-forge[aws]'" in str(publish.refusal(TARGET, tmp_path, ()))


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_every_file_goes_up_with_its_checksum_and_the_index_last(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    done = publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    prefix = _prefix(index)
    assert done == publish.Published(f"s3://acme-releases/{prefix}", written=3, already=0)
    assert s3.writes == [
        f"{prefix}bundle.tgz",
        f"{prefix}reports/ship-report.json",
        f"{prefix}release.json",
    ]
    assert s3.objects[("acme-releases", f"{prefix}release.json")] == (
        (tmp_path / "rel" / "release.json").read_bytes()
    )
    assert api_release.verify_release(done.uri, clients=s3).intact


def test_a_rerun_of_the_same_release_writes_nothing_and_succeeds(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    s3 = FakeS3()
    publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    again = publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    assert (again.written, again.already) == (0, 3)
    assert len(s3.writes) == 3


def test_a_rerun_finishes_a_publish_that_stopped_halfway(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    s3.put("acme-releases", f"{_prefix(index)}bundle.tgz", b"bundle")
    done = publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    assert (done.written, done.already) == (2, 1)


def test_an_object_with_other_bytes_is_refused(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    s3.put("acme-releases", f"{_prefix(index)}reports/ship-report.json", b"[]")
    with pytest.raises(publish.PublishError) as raised:
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    message = str(raised.value)
    assert "reports/ship-report.json already holds other bytes" in message
    assert "holds no release.json, so no reader takes it as a release" in message
    assert "Delete the objects under it and run again" in message
    assert "1 object(s) were written" in message and "release.json is written last" in message
    assert not any(k.endswith("release.json") for k in s3.writes)


def test_other_bytes_beside_a_published_index_never_change(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    s3.objects[("acme-releases", f"{_prefix(index)}bundle.tgz")] = b"swapped"
    s3.checksums[("acme-releases", f"{_prefix(index)}bundle.tgz")] = None
    with pytest.raises(publish.PublishError) as raised:
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    assert "bundle.tgz already holds other bytes; a published release never changes" in str(
        raised.value
    )


def test_an_object_uploaded_without_a_checksum_is_not_taken_as_ours(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    s3.put("acme-releases", f"{_prefix(index)}bundle.tgz", b"bundle", checksum=None)
    with pytest.raises(publish.PublishError, match="already holds other bytes"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)


def test_another_index_for_this_identity_writes_nothing(tmp_path: Path) -> None:
    first = sealed(tmp_path / "one", run_id="run-1")
    sealed(tmp_path / "two", run_id="run-2")  # same identity, other provenance
    s3 = FakeS3()
    publish.publish_release(tmp_path / "one", TARGET, clients=s3)
    before = list(s3.writes)
    with pytest.raises(publish.PublishError) as raised:
        publish.publish_release(tmp_path / "two", TARGET, clients=s3)
    assert "release.json already holds another release of this identity" in str(raised.value)
    assert "nothing was written" in str(raised.value)
    assert s3.writes == before
    assert api_release.verify_release(f"s3://acme-releases/{_prefix(first)}", clients=s3).intact


def test_a_denied_write_is_refused_and_says_what_was_written(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    s3.deny_put.add(f"{_prefix(index)}reports/ship-report.json")
    with pytest.raises(publish.PublishError) as raised:
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    assert "is denied (AccessDenied)" in str(raised.value)
    assert "1 object(s) were written" in str(raised.value)


def test_a_write_that_raced_a_delete_is_tried_again(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    s3 = FakeS3()
    s3.conflicts = 2
    assert publish.publish_release(tmp_path / "rel", TARGET, clients=s3).written == 3
    s3 = FakeS3()
    s3.conflicts = 3
    with pytest.raises(publish.PublishError, match="ConditionalRequestConflict"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)


def test_a_bad_read_back_is_refused(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel")
    s3 = FakeS3()
    s3.misreport.add(f"{_prefix(index)}bundle.tgz")
    with pytest.raises(publish.PublishError, match="does not read back with the sha256"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)


def test_a_file_changed_after_the_seal_is_not_published(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    (tmp_path / "rel" / "bundle.tgz").write_bytes(b"swapped")
    s3 = FakeS3()
    with pytest.raises(publish.PublishError, match="is not intact, so it is not published"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    assert s3.calls == []


def test_a_file_over_five_gib_is_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed(tmp_path / "rel", {"bundle.tgz": b"x" * 10})
    monkeypatch.setattr(publish, "MAX_OBJECT_BYTES", 9)
    s3 = FakeS3()
    with pytest.raises(publish.PublishError, match="larger than 5 GiB"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3)
    assert s3.calls == []


def test_a_key_segment_the_release_cannot_name_is_refused(tmp_path: Path) -> None:
    sealed(tmp_path / "rel", environment="prod/eu")
    with pytest.raises(publish.PublishError, match="environment 'prod/eu' cannot name"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=FakeS3())


def test_the_bucket_checks_run_before_any_write(tmp_path: Path) -> None:
    sealed(tmp_path / "rel")
    s3 = FakeS3()
    with pytest.raises(publish.PublishError, match="does not have versioning enabled"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3, require=("versioning",))
    with pytest.raises(publish.PublishError, match="does not have Object Lock enabled"):
        publish.publish_release(tmp_path / "rel", TARGET, clients=s3, require=("object-lock",))
    assert s3.writes == []
    s3.versioning, s3.object_lock = "Enabled", True
    done = publish.publish_release(
        tmp_path / "rel", TARGET, clients=s3, require=("versioning", "object-lock")
    )
    assert done.written == 3


# ---------------------------------------------------------------------------
# fluid ship --release-to
# ---------------------------------------------------------------------------


def _report(work: Path) -> Any:
    return json.loads((work / "runtime" / "ship-report.json").read_text(encoding="utf-8"))


def test_ship_publishes_the_sealed_release(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    s3 = FakeS3()
    monkeypatch.setattr(publish, "_default_clients", s3)
    code = _ship([*_UNTIL_4, "--release-dir", "../rel", "--release-to", TARGET])
    assert code == 0
    remote = _report(work)["release"]["remote"]
    assert remote["status"] == "published" and remote["already_there"] == 0
    assert remote["uri"].startswith("s3://acme-releases/handoff/example.release/dev/")
    assert s3.writes[-1].endswith("/release.json")
    assert api_release.verify_release(remote["uri"], clients=s3).intact
    assert api_release.verify_release(work.parent / "rel").intact


def test_ship_exits_1_when_the_publish_fails_and_keeps_the_sealed_release(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, bundled)
    s3 = FakeS3()
    s3.deny_put.add("*")
    monkeypatch.setattr(publish, "_default_clients", s3)
    code = _ship([*_UNTIL_4, "--release-dir", "../rel", "--release-to", TARGET])
    assert code == 1
    shipped = _report(work)["release"]
    assert shipped["status"] == "published"  # sealed locally
    assert shipped["remote"]["status"] == "not-published"
    assert "is denied (AccessDenied)" in shipped["remote"]["reason"]
    assert api_release.verify_release(work.parent / "rel").intact


@pytest.mark.parametrize(
    "argv, problem",
    [
        (["--release-to", TARGET], "add --release-dir"),
        (["--release-dir", "../rel", "--release-to", ""], "--release-to needs s3://"),
        (["--release-dir", "../rel", "--release-to", "s3://Bad/"], "is not an S3 bucket name"),
        (
            ["--release-dir", "../rel", "--release-to", TARGET, "--release-to-require", "acl"],
            "unknown check(s) acl",
        ),
        (["--release-dir", "../rel", "--release-to-require", "versioning"], "add --release-to"),
    ],
)
def test_ship_refuses_a_target_it_cannot_publish_to_before_any_stage(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch, argv: Any, problem: str, caplog: Any
) -> None:
    fake = _install(monkeypatch, bundled)
    assert _ship([*_UNTIL_4, *argv]) == 2
    assert problem in caplog.text
    assert fake.calls == []


def test_explain_names_where_the_release_goes(
    work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _ship(["--explain", "--release-dir", "../rel", "--release-to", "s3://acme-releases/h"]) == 0
    )
    assert (
        "remote: then published, write-once, under "
        "s3://acme-releases/h/<product>/<env>/<identity hex>/"
    ) in capsys.readouterr().out


def test_the_flags_are_on_the_parser() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    ship.register(parser.add_subparsers())
    args = parser.parse_args(["ship", "--release-to", TARGET, "--release-to-require", "versioning"])
    assert (args.release_to, args.release_to_require) == (TARGET, ["versioning"])
