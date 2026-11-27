# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --deploy-targets`` and ``fluid runs deploy``: where a release's external files go,
sealed into the release, and the deploy that copies them there on condition, DAGs last, with a
deploy receipt that never gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.api import release as api_release
from fluid_build.cli import _deploy_targets as targets
from fluid_build.cli import _release_deploy as deploys
from fluid_build.cli import _release_lock as lock
from fluid_build.cli import _release_publish as publish
from fluid_build.cli import _release_receipt as receipts
from fluid_build.cli import _ship_release as release
from fluid_build.cli.ops import _cli as ops_cli
from fluid_build.util import receipt_record as rr
from tests.cli._fake_s3 import FakeS3, S3Error, b64, sealed
from tests.cli.test_ship_release import (  # noqa: F401 - fixtures
    _UNTIL_4,
    _install,
    _ship,
    bundled,
    work,
)

pytestmark = pytest.mark.unit

ACCOUNT = "123456789012"
TARGET = "s3://acme-releases/handoff/"
COMMIT = "c0ffee" * 6 + "c0ff"
REPO = "https://git.example.com/data/orders-jobs"
PACKAGE = b"package bytes"
DAG = b'dag = "orders"\n'
PASSING = {"total": 1, "match": 1, "mismatch": 0, "error": 0}
T0 = datetime(2026, 11, 26, 9, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fluid_build.util.s3.installed", lambda: True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entries() -> List[lock.LockEntry]:
    return lock.parse_lock(
        {
            "artifacts": [
                {
                    "name": "orders-jobs",
                    "kind": "spark-package",
                    "path": "dist/orders_jobs-1.4.0.zip",
                    "sha256": _sha(PACKAGE),
                    "source": {"repo": REPO, "commit": COMMIT},
                },
                {
                    "name": "orders-dag",
                    "kind": "airflow-dag",
                    "path": "dags/orders_dag.py",
                    "sha256": _sha(DAG),
                    "source": {"repo": REPO, "commit": COMMIT},
                },
            ]
        },
        "lock",
    )


def _deploy_block(**over: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "deploy": {
            "account": ACCOUNT,
            "targets": [
                # the DAG listed first: the seal puts it last
                {"artifact": "orders-dag", "to": "s3://acme-airflow/dags/orders_dag.py"},
                {"artifact": "orders-jobs", "to": "s3://acme-jobs/jobs/orders_jobs.zip"},
            ],
        }
    }
    data["deploy"].update(over)
    return targets.parse_targets(data, "targets", _entries())


# ---------------------------------------------------------------------------
# The targets file, without a file
# ---------------------------------------------------------------------------


def test_targets_are_sealed_with_the_lock_s_kind_path_and_digest_and_dags_last() -> None:
    block = _deploy_block(region="eu-north-1")
    assert block == {
        "account": ACCOUNT,
        "region": "eu-north-1",
        "targets": [
            {
                "artifact": "orders-jobs",
                "kind": "spark-package",
                "path": "external/orders-jobs/orders_jobs-1.4.0.zip",
                "digest": {"sha256": _sha(PACKAGE)},
                "to": "s3://acme-jobs/jobs/orders_jobs.zip",
            },
            {
                "artifact": "orders-dag",
                "kind": "airflow-dag",
                "path": "external/orders-dag/orders_dag.py",
                "digest": {"sha256": _sha(DAG)},
                "to": "s3://acme-airflow/dags/orders_dag.py",
            },
        ],
    }


def _bad(**block: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "account": ACCOUNT,
        "targets": [{"artifact": "orders-jobs", "to": "s3://acme-jobs/jobs/a.zip"}],
    }
    out.update(block)
    return {"deploy": out}


@pytest.mark.parametrize(
    "data, problem",
    [
        ([], "expected a mapping with a deploy block"),
        ({"deploy": {}, "extra": 1}, "unsupported field(s) ['extra']"),
        (_bad(owner="me"), "unsupported field(s) ['owner']"),
        (_bad(account=123456789012), "12-digit AWS account id"),
        (_bad(account="12345"), "12-digit AWS account id"),
        (_bad(region="Europe"), "an AWS region name"),
        (_bad(targets=[]), "a list of one target or more"),
        (_bad(targets=[{"artifact": "nope", "to": "s3://b-1/k"}]), "a name the --artifacts lock"),
        (_bad(targets=[{"artifact": "orders-jobs", "to": "s3://acme-jobs/jobs/"}]), "not a prefix"),
        (_bad(targets=[{"artifact": "orders-jobs", "to": "gs://acme-jobs/a"}]), "s3://bucket/key"),
        (_bad(targets=[{"artifact": "orders-jobs", "to": "s3://Acme/a"}]), "s3://bucket/key"),
        (_bad(targets=[{"artifact": "orders-jobs", "to": "s3://acme/a b"}]), "s3://bucket/key"),
        (
            _bad(targets=[{"artifact": "orders-jobs", "to": "s3://a-b/k", "mode": "x"}]),
            "unsupported field(s) ['mode']",
        ),
        (
            _bad(
                targets=[
                    {"artifact": "orders-jobs", "to": "s3://a-b/k"},
                    {"artifact": "orders-dag", "to": "s3://a-b/k"},
                ]
            ),
            "is also targets[0]'s",
        ),
    ],
)
def test_a_targets_file_the_release_cannot_seal_is_refused(data: Any, problem: str) -> None:
    with pytest.raises(targets.TargetsError) as raised:
        targets.parse_targets(data, "targets", _entries())
    assert problem in str(raised.value)


def test_deploy_targets_change_the_identity_digest_and_never_the_code_digest() -> None:
    identity: Dict[str, Any] = {"product_id": "acme.orders", "source": {"uri": "git+x"}}
    with_targets = dict(identity, deploy=_deploy_block())
    assert release.code_digest(identity) == release.code_digest(with_targets)
    assert release.identity_digest(identity) != release.identity_digest(with_targets)


# ---------------------------------------------------------------------------
# fluid ship --deploy-targets
# ---------------------------------------------------------------------------


@pytest.fixture
def lockdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with the lock's files, the lock and a targets file."""
    from tests.cli._ci_env import clear_ci_source

    monkeypatch.delenv(lock.LOCK_ENV, raising=False)
    monkeypatch.delenv(targets.TARGETS_ENV, raising=False)
    clear_ci_source(monkeypatch)
    _checkout(tmp_path)
    return tmp_path


def test_load_reads_the_file_against_the_sealed_files(lockdir: Path) -> None:
    external = lock.load(str(lockdir / "lock.yaml"))
    assert targets.load(str(lockdir / "targets.yaml"), external) == _deploy_block()


def test_a_file_larger_than_one_copy_is_refused(
    lockdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(targets, "MAX_OBJECT_BYTES", len(PACKAGE) - 1)
    with pytest.raises(targets.TargetsError, match="larger than 5 GiB"):
        targets.load(str(lockdir / "targets.yaml"), lock.load(str(lockdir / "lock.yaml")))


def test_the_variable_is_read_only_beside_a_release_and_a_blank_flag_is_refused(
    lockdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _ship_stages as st
    from fluid_build.cli import ship

    monkeypatch.chdir(lockdir)
    monkeypatch.setenv(targets.TARGETS_ENV, "targets.yaml")
    args = argparse.Namespace(deploy_targets=None)
    assert ship._deploy_targets_file(args, None) == ""
    assert ship._deploy_targets_file(args, lockdir / "rel") == str(lockdir / "targets.yaml")
    for value in ("", "  "):
        with pytest.raises(st.ShipUsageError, match="needs the path of a targets file"):
            ship._deploy_targets_file(argparse.Namespace(deploy_targets=value), lockdir / "rel")


def test_ship_refuses_deploy_targets_without_a_release_or_a_lock(lockdir: Path) -> None:
    from fluid_build.cli import _ship_stages as st

    opts = st.ShipOptions(contract=str(lockdir / "contract.fluid.yaml"), env="dev")
    opts.deploy_targets = str(lockdir / "targets.yaml")
    assert "this run publishes none" in str(release.refusal(None, [1, 2, 3, 4], {}, opts))
    problem = release.refusal(lockdir / "rel", [1, 2, 3, 4], {}, opts)
    assert "add --artifacts LOCK" in str(problem)
    opts.artifacts = str(lockdir / "lock.yaml")
    assert release.refusal(lockdir / "rel", [1, 2, 3, 4], {}, opts) is None
    (lockdir / "targets.yaml").write_text("deploy: {account: '1', targets: []}", encoding="utf-8")
    assert "12-digit" in str(release.refusal(lockdir / "rel", [1, 2, 3, 4], {}, opts))


def _checkout(directory: Path) -> None:
    """The lock's files, the lock and a targets file, in ``directory`` (the project)."""
    import yaml

    (directory / "dist").mkdir(exist_ok=True)
    (directory / "dist" / "orders_jobs-1.4.0.zip").write_bytes(PACKAGE)
    (directory / "dags").mkdir(exist_ok=True)
    (directory / "dags" / "orders_dag.py").write_bytes(DAG)
    entries = [
        {
            "name": e.name,
            "kind": e.kind,
            "path": e.path,
            "sha256": e.sha256,
            "source": {"repo": REPO, "commit": COMMIT},
        }
        for e in _entries()
    ]
    (directory / "lock.yaml").write_text(yaml.safe_dump({"artifacts": entries}), encoding="utf-8")
    raw = {
        "deploy": {
            "account": ACCOUNT,
            "targets": [
                {"artifact": t["artifact"], "to": t["to"]} for t in _deploy_block()["targets"]
            ],
        }
    }
    (directory / "targets.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_ship_seals_the_targets_into_the_release(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(lock.LOCK_ENV, raising=False)
    monkeypatch.delenv(targets.TARGETS_ENV, raising=False)
    _install(monkeypatch, bundled)
    _checkout(work)
    argv = [*_UNTIL_4, "--artifacts", "lock.yaml", "--deploy-targets", "targets.yaml"]
    assert _ship([*argv, "--release-dir", "../rel"]) == 0
    index = json.loads((work.parent / "rel" / "release.json").read_text(encoding="utf-8"))
    assert index["identity"]["deploy"] == _deploy_block()
    assert api_release.verify_release(work.parent / "rel").intact


def test_ship_refuses_a_targets_file_it_cannot_seal_before_any_stage(
    work: Path, bundled: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    monkeypatch.delenv(lock.LOCK_ENV, raising=False)
    monkeypatch.delenv(targets.TARGETS_ENV, raising=False)
    fake = _install(monkeypatch, bundled)
    _checkout(work)
    (work / "targets.yaml").write_text("deploy: {account: x, targets: []}", encoding="utf-8")
    argv = [*_UNTIL_4, "--artifacts", "lock.yaml", "--deploy-targets", "targets.yaml"]
    assert _ship([*argv, "--release-dir", "../rel"]) == 2
    assert "12-digit AWS account id" in caplog.text and fake.calls == []
    assert _ship([*_UNTIL_4, "--deploy-targets", "targets.yaml", "--release-dir", "../r2"]) == 2
    assert "add --artifacts LOCK" in caplog.text


# ---------------------------------------------------------------------------
# A published release with deploy targets, and an S3 that copies
# ---------------------------------------------------------------------------


class DeployS3(FakeS3):
    """:class:`FakeS3` with what a deploy calls: CopyObject with its source and destination
    conditions, ETags and a new VersionId per write, bucket owners, and a CopyObject model."""

    def __init__(self) -> None:
        super().__init__()
        self.versions, self.versioning = True, "Enabled"
        self.owner: Dict[str, str] = {}  # bucket -> account (default ACCOUNT)
        self.vids: Dict[Tuple[str, str], str] = {}
        self.copies: List[Dict[str, Any]] = []
        self.puts: List[Dict[str, Any]] = []
        self.targets_head: List[Dict[str, Any]] = []
        self.copy_errors: List[S3Error] = []  # raised by the next copies, in order
        self.changed_before_copy: Optional[Tuple[str, str, bytes]] = None
        self.copy_members = {"IfMatch": 1, "IfNoneMatch": 1}

        class _Op:
            def __init__(self, members: Dict[str, int]) -> None:
                self.input_shape = type("S", (), {"members": members})()

        fake = self

        class _Model:
            def operation_model(self, name: str) -> Any:
                return _Op(fake.copy_members)

        self.meta = type("M", (), {"service_model": _Model()})()

    def _owner_check(self, bucket: str, expected: Optional[str]) -> None:
        if expected is not None and expected != self.owner.get(bucket, ACCOUNT):
            raise S3Error("AccessDenied", 403)

    def etag(self, bucket: str, key: str) -> str:
        return '"' + hashlib.md5(self.objects[(bucket, key)]).hexdigest() + '"'  # noqa: S324

    def version(self, bucket: str, key: str) -> str:
        return self.vids.get((bucket, key)) or self.version_of(key)

    def write(self, bucket: str, key: str, data: bytes) -> None:
        if (bucket, key) in self.objects:  # a versioned bucket keeps the older version
            self.overwritten.add(key)
        self.put(bucket, key, data)
        self.vids[(bucket, key)] = f"v{len(self.writes) + 1}"
        self.writes.append(key)

    def put_object(self, **kw: Any) -> Dict[str, Any]:  # type: ignore[override]
        if "ContentLength" in kw:  # the release publish
            return super().put_object(**kw)
        self.calls.append("put_object")
        self.puts.append({k: v for k, v in kw.items() if k != "Body"})
        body = kw["Body"]
        bucket, key = kw["Bucket"], kw["Key"]
        data = body if isinstance(body, bytes) else body.read()
        self._owner_check(bucket, kw.get("ExpectedBucketOwner"))
        self._conditions(bucket, key, kw)
        assert kw["ChecksumSHA256"] == b64(data)
        self.write(bucket, key, data)
        return {"VersionId": self.version(bucket, key)}

    def _conditions(self, bucket: str, key: str, kw: Dict[str, Any]) -> None:
        if self.changed_before_copy:
            b, k, data = self.changed_before_copy
            self.changed_before_copy = None
            self.write(b, k, data)
        there = (bucket, key) in self.objects
        if kw.get("IfNoneMatch") == "*" and there:
            raise S3Error("PreconditionFailed", 412)
        if "IfMatch" in kw and (not there or self.etag(bucket, key) != kw["IfMatch"]):
            raise S3Error("PreconditionFailed", 412)

    def get_object(self, *, Bucket: str, Key: str, VersionId: str = "") -> Dict[str, Any]:
        got = super().get_object(Bucket=Bucket, Key=Key, VersionId=VersionId)
        if "VersionId" in got:
            got["VersionId"] = self.version(Bucket, Key)
        got["ETag"] = self.etag(Bucket, Key)
        return got

    def copy_object(self, **kw: Any) -> Dict[str, Any]:
        self.calls.append("copy_object")
        self.copies.append(dict(kw))
        if self.copy_errors:
            raise self.copy_errors.pop(0)
        bucket, key = kw["Bucket"], kw["Key"]
        self._owner_check(bucket, kw.get("ExpectedBucketOwner"))
        src = kw["CopySource"]
        if src.get("VersionId") and src["VersionId"] != self.version(src["Bucket"], src["Key"]):
            raise S3Error("NoSuchVersion", 404)
        if kw["CopySourceIfMatch"] != self.etag(src["Bucket"], src["Key"]):
            raise S3Error("PreconditionFailed", 412)
        self._conditions(bucket, key, kw)
        self.write(bucket, key, self.objects[(src["Bucket"], src["Key"])])
        return {"VersionId": self.version(bucket, key), "CopyObjectResult": {}}

    def head_object(  # type: ignore[override]
        self,
        *,
        Bucket: str,
        Key: str,
        ChecksumMode: str = "",
        VersionId: str = "",
        ExpectedBucketOwner: Optional[str] = None,
    ) -> Dict[str, Any]:
        if ExpectedBucketOwner is not None:
            self.targets_head.append({"Key": Key, "VersionId": VersionId})
        self._owner_check(Bucket, ExpectedBucketOwner)
        if (Bucket, Key) not in self.objects:
            raise S3Error("404", 404)
        if VersionId and VersionId != self.version(Bucket, Key):
            raise S3Error("NoSuchVersion", 404)
        head = super().head_object(Bucket=Bucket, Key=Key, ChecksumMode="ENABLED")
        head.update(ETag=self.etag(Bucket, Key), VersionId=self.version(Bucket, Key))
        return head


def _sealed_release(directory: Path, environment: str, block: Optional[Dict[str, Any]]) -> None:
    """A sealed release with the two external files, and ``block`` as its deploy targets."""
    entries = _entries()
    files = {
        "reports/ship-report.json": b"{}",
        entries[0].release_path: DAG,  # orders-dag sorts first
        entries[1].release_path: PACKAGE,
    }
    index = sealed(directory, files, environment=environment)
    identity = dict(index["identity"], external=[e.identity() for e in entries])
    if block is not None:
        identity["deploy"] = block
    index.update(
        identity=identity,
        identity_digest=release.identity_digest(identity),
        code_digest=release.code_digest(identity),
    )
    (directory / "release.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.fixture
def s3() -> DeployS3:
    return DeployS3()


def _published(tmp_path: Path, s3: DeployS3, env: str = "prod", block: Any = "default") -> str:
    _sealed_release(tmp_path / env, env, _deploy_block() if block == "default" else block)
    return publish.publish_release(tmp_path / env, TARGET, clients=s3).uri


def _deploy(uri: str, s3: DeployS3, env: str = "prod", **kw: Any) -> deploys.Deployed:
    kw.setdefault("account_of", lambda e, r: (ACCOUNT, ""))
    kw.setdefault("now", T0)
    return deploys.deploy_release(uri, environment=env, clients=s3, **kw)


def _target_writes(s3: DeployS3) -> List[str]:
    return [k for k in s3.writes if not k.startswith("handoff/")]


# ---------------------------------------------------------------------------
# verify_release binds the targets
# ---------------------------------------------------------------------------


def test_verify_release_takes_a_release_with_deploy_targets(tmp_path: Path) -> None:
    _sealed_release(tmp_path / "rel", "prod", _deploy_block())
    assert api_release.verify_release(tmp_path / "rel").intact


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda t: t.update(path="external/orders-jobs/other.zip"), "pins no external file so"),
        (lambda t: t.update(digest={"sha256": "0" * 64}), "pins no external file so"),
        (lambda t: t.update(to="https://acme/a"), "names no s3:// object of its own"),
    ],
)
def test_a_deploy_target_that_is_not_a_sealed_file_is_not_intact(
    tmp_path: Path, change: Any, problem: str
) -> None:
    block = _deploy_block()
    change(block["targets"][0])
    _sealed_release(tmp_path / "rel", "prod", block)
    found = api_release.verify_release(tmp_path / "rel")
    assert not found.intact and any(problem in p for p in found.problems)


# ---------------------------------------------------------------------------
# fluid runs deploy
# ---------------------------------------------------------------------------


def test_a_deploy_copies_each_target_on_condition_dags_last_and_records_a_receipt(
    tmp_path: Path, s3: DeployS3
) -> None:
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3)
    assert done.exit_code == 0, done.reason
    assert _target_writes(s3) == ["jobs/orders_jobs.zip", "dags/orders_dag.py"]  # the DAG last
    assert s3.objects[("acme-jobs", "jobs/orders_jobs.zip")] == PACKAGE
    assert s3.objects[("acme-airflow", "dags/orders_dag.py")] == DAG
    prefix = uri.split("/", 3)[3]
    for copy, rel in zip(
        s3.copies,
        ("external/orders-jobs/orders_jobs-1.4.0.zip", "external/orders-dag/orders_dag.py"),
        strict=True,
    ):
        src_key = prefix + rel
        assert copy["CopySource"] == {
            "Bucket": "acme-releases",
            "Key": src_key,
            "VersionId": s3.version_of(src_key),  # the version that was verified
        }
        assert copy["CopySourceIfMatch"] == s3.etag("acme-releases", src_key)
        assert copy["IfNoneMatch"] == "*" and "IfMatch" not in copy  # new keys
        assert copy["ChecksumAlgorithm"] == "SHA256"
        assert copy["ExpectedBucketOwner"] == ACCOUNT
    assert [t["action"] for t in done.targets] == ["copied", "copied"]
    assert all(t["version_id"] for t in done.targets)
    assert done.receipt and "/deploys/prod/" in done.receipt  # never where the gate reads
    stored = json.loads(s3.objects[("acme-releases", done.receipt.split("/", 3)[3])])
    assert stored["check"] == "deploy" and stored["result"] == "PASSED"
    assert [t["action"] for t in stored["targets"]] == ["copied", "copied"]
    assert all(t["checked"] and t["version_id"] for t in stored["targets"])
    log = s3.objects[
        ("acme-releases", done.receipt.split("/", 3)[3].replace(".json", ".report.json"))
    ]
    assert json.loads(log)["schema"] == deploys.LOG_SCHEMA


def test_an_unchanged_rerun_writes_no_target(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    assert _deploy(uri, s3).exit_code == 0
    before = list(_target_writes(s3))
    s3.copies.clear()
    again = _deploy(uri, s3, now=T0 + timedelta(seconds=1))
    assert again.exit_code == 0 and again.reason == "0 copied, 2 unchanged"
    assert s3.copies == [] and _target_writes(s3) == before
    assert [t["action"] for t in again.targets] == ["unchanged", "unchanged"]
    assert all(t["version_id"] for t in again.targets)


def test_a_target_with_other_bytes_is_copied_over_its_etag(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.put("acme-airflow", "dags/orders_dag.py", b"the old dag")
    etag = s3.etag("acme-airflow", "dags/orders_dag.py")
    assert _deploy(uri, s3).exit_code == 0
    dag_copy = s3.copies[-1]
    assert dag_copy["IfMatch"] == etag and "IfNoneMatch" not in dag_copy


def test_a_target_changed_meanwhile_is_refused_and_the_dag_is_left_alone(
    tmp_path: Path, s3: DeployS3
) -> None:
    uri = _published(tmp_path, s3)
    s3.put("acme-airflow", "dags/orders_dag.py", b"the old dag")
    # another writer creates the package's key between the HEAD and the copy
    s3.changed_before_copy = ("acme-jobs", "jobs/orders_jobs.zip", b"someone else's")
    done = _deploy(uri, s3)
    assert done.exit_code == 1 and "was written meanwhile, or the release object" in done.reason
    assert s3.objects[("acme-airflow", "dags/orders_dag.py")] == b"the old dag"
    assert [t["action"] for t in done.targets] == ["failed", "not reached"]
    stored = json.loads(s3.objects[("acme-releases", str(done.receipt).split("/", 3)[3])])
    assert stored["check"] == "deploy" and stored["result"] == "FAILED"


def test_a_copy_that_races_is_tried_again(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.copy_errors = [S3Error("ConditionalRequestConflict", 409)] * 2
    assert _deploy(uri, s3).exit_code == 0
    s3.copy_errors = [S3Error("ConditionalRequestConflict", 409)] * 3
    s3.put("acme-jobs", "jobs/orders_jobs.zip", b"x")
    done = _deploy(uri, s3, now=T0 + timedelta(seconds=1))
    assert done.exit_code == 1 and "raced other writes 3 times" in done.reason


def test_a_policy_that_enforces_conditional_writes_is_named(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.copy_errors = [S3Error("NotImplemented", 501)]
    done = _deploy(uri, s3)
    assert done.exit_code == 1
    assert "enforces conditional writes (s3:if-match / s3:if-none-match)" in done.reason
    assert _target_writes(s3) == []


def test_a_copy_that_reads_back_other_bytes_fails(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.misreport.add("jobs/orders_jobs.zip")
    done = _deploy(uri, s3)
    assert done.exit_code == 1 and "does not have the sha256 the release names" in done.reason


def test_a_target_bucket_of_another_account_is_refused(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.owner["acme-jobs"] = "999999999999"
    done = _deploy(uri, s3)
    assert done.exit_code == 1 and f"not account {ACCOUNT}'s" in done.reason
    assert "s3:ListBucket on the bucket" in done.reason
    assert _target_writes(s3) == []
    assert all(h for h in s3.targets_head)  # ExpectedBucketOwner on every target read


@pytest.mark.parametrize(
    "account_of, code, problem",
    [
        (lambda e, r: ("999999999999", ""), 1, "the release deploys to account 123456789012"),
        (lambda e, r: ("", "sts GetCallerIdentity failed (X)"), 2, "cannot be read"),
    ],
)
def test_credentials_of_another_account_are_refused_before_any_write(
    tmp_path: Path, s3: DeployS3, account_of: Any, code: int, problem: str
) -> None:
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3, account_of=account_of)
    assert done.exit_code == code and problem in done.reason
    assert _target_writes(s3) == [] and done.receipt is None


def test_an_old_botocore_decides_nothing(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.copy_members = {"IfNoneMatch": 1}
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and "1.40.61 or later" in done.reason
    assert _target_writes(s3) == []


def test_another_environment_s_release_is_refused(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3, env="nonprod")
    assert done.exit_code == 2 and "is the release of prod" in done.reason


def test_a_release_without_targets_or_not_intact_is_refused(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3, block=None)
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and "names no deploy targets" in done.reason
    key = uri.split("/", 3)[3] + "external/orders-dag/orders_dag.py"
    s3.objects[("acme-releases", key)] = b"swapped"
    assert _deploy(uri, s3).exit_code == 1


def test_a_dry_run_writes_nothing(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    written = list(s3.writes)
    done = _deploy(uri, s3, dry_run=True)
    assert done.exit_code == 0 and done.receipt is None and s3.writes == written
    assert [t["action"] for t in done.targets] == ["would copy", "would copy"]
    assert done.as_dict()["status"] == "dry-run"


def test_a_release_in_a_bucket_without_versioning_is_not_copied(
    tmp_path: Path, s3: DeployS3
) -> None:
    """No version to pin: an object swapped after the check would be what is copied."""
    s3.versions, s3.versioning = False, None
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and "versioning is off (Enabled is needed)" in done.reason
    assert _target_writes(s3) == [] and s3.copies == [] and done.receipt is None


def test_a_release_json_written_twice_is_refused(tmp_path: Path, s3: DeployS3) -> None:
    """Targets rewritten after the seal keep the code digest: a second version of release.json
    (or a delete marker) is what shows it, so the deploy takes none."""
    uri = _published(tmp_path, s3)
    key = uri.split("/", 3)[3] + "release.json"
    s3.overwritten.add(key)
    done = _deploy(uri, s3)
    assert done.exit_code == 1 and "written more than once" in done.reason
    assert _target_writes(s3) == [] and done.receipt is None
    s3.overwritten.clear()
    s3.deleted.add(key)
    assert _deploy(uri, s3).exit_code == 1


def test_sign_without_cosign_refuses_before_any_copy(
    tmp_path: Path, s3: DeployS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    monkeypatch.setattr(_signing, "cosign_available", lambda: False)
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3, sign=True)
    assert done.exit_code == 2 and "--sign needs cosign" in done.reason
    assert _target_writes(s3) == [] and s3.copies == []


def test_a_file_on_disk_changed_since_the_check_is_refused_by_name(
    tmp_path: Path, s3: DeployS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sealed_release(tmp_path / "prod", "prod", _deploy_block())
    real = receipts._verified

    def then_changed(*a: Any, **kw: Any) -> Any:
        checked = real(*a, **kw)
        (tmp_path / "prod" / "external/orders-jobs/orders_jobs-1.4.0.zip").write_bytes(b"EVIL")
        return checked

    monkeypatch.setattr(receipts, "_verified", then_changed)
    done = _deploy(str(tmp_path / "prod"), s3)
    assert done.exit_code == 1 and "its bytes changed since the release was verified" in (
        done.reason
    )
    assert s3.puts == []


def test_a_release_on_disk_is_put_with_its_checksum_and_the_same_conditions(
    tmp_path: Path, s3: DeployS3
) -> None:
    _sealed_release(tmp_path / "prod", "prod", _deploy_block())
    done = _deploy(str(tmp_path / "prod"), s3)
    assert done.exit_code == 0, done.reason
    assert [p["Key"] for p in s3.puts] == ["jobs/orders_jobs.zip", "dags/orders_dag.py"]
    assert all(p["IfNoneMatch"] == "*" and p["ExpectedBucketOwner"] == ACCOUNT for p in s3.puts)
    assert (tmp_path / "deploys" / "prod").is_dir() and not (tmp_path / "receipts").exists()


# ---------------------------------------------------------------------------
# --require-receipt, and receipts that never gate
# ---------------------------------------------------------------------------


def _verify_receipt(tmp_path: Path, s3: DeployS3, uri: str, now: datetime) -> None:
    block = receipts.report_release_block(uri, "acme.orders", clients=s3)
    report = tmp_path / f"verify-{now.timestamp()}.json"
    report.write_text(json.dumps({"summary": PASSING, "release": block}), encoding="utf-8")
    done = receipts.record_receipt(uri, report_path=str(report), clients=s3, now=now)
    assert done.result == "PASSED"


def test_require_receipt_needs_a_verify_receipt_and_a_deploy_receipt_is_none(
    tmp_path: Path, s3: DeployS3
) -> None:
    nonprod = _published(tmp_path, s3, "nonprod", block=_deploy_block())
    prod = _published(tmp_path, s3, "prod")
    done = _deploy(prod, s3, require_receipt="nonprod")
    assert done.exit_code == 1 and "holds no receipt" in done.reason
    assert _deploy(nonprod, s3, env="nonprod").exit_code == 0  # a deploy receipt in nonprod
    done = _deploy(prod, s3, require_receipt="nonprod", now=T0 + timedelta(seconds=1))
    assert done.exit_code == 1 and "receipts/nonprod/ holds no receipt" in done.reason
    assert _target_writes(s3) == ["jobs/orders_jobs.zip", "dags/orders_dag.py"]  # nonprod's


@pytest.mark.parametrize("deploy_first", [True, False])
def test_the_gate_passes_on_the_verify_receipt_whether_a_deploy_is_newer_or_older(
    tmp_path: Path, s3: DeployS3, deploy_first: bool
) -> None:
    nonprod = _published(tmp_path, s3, "nonprod", block=_deploy_block())
    prod = _published(tmp_path, s3, "prod")
    one, two = T0, T0 + timedelta(seconds=1)
    if deploy_first:
        assert _deploy(nonprod, s3, env="nonprod", now=one).exit_code == 0
        _verify_receipt(tmp_path, s3, nonprod, two)
    else:
        _verify_receipt(tmp_path, s3, nonprod, one)
        s3.objects = {k: v for k, v in s3.objects.items() if k[0] == "acme-releases"}
        assert _deploy(nonprod, s3, env="nonprod", now=two).exit_code == 0
    found = receipts.check_receipt(prod, environment="nonprod", clients=s3)
    assert found.exit_code == 0, found.reason
    stored = json.loads(s3.objects[("acme-releases", str(found.receipt).split("/", 3)[3])])
    assert stored["check"] == "verify"
    s3.objects = {k: v for k, v in s3.objects.items() if k[0] == "acme-releases"}
    done = _deploy(prod, s3, require_receipt="nonprod", now=T0 + timedelta(seconds=5))
    assert done.exit_code == 0, done.reason


def _deploys_key(receipt_uri: str) -> str:
    return receipt_uri.split("/", 3)[3]


def test_a_verify_receipt_a_deployer_writes_under_deploys_does_not_move_the_gate(
    tmp_path: Path, s3: DeployS3
) -> None:
    """A deployer writes deploys/<env>/ only: a PASSED verify receipt it puts there (the bytes of
    a real one) is never read by the gate."""
    nonprod = _published(tmp_path, s3, "nonprod", block=_deploy_block())
    prod = _published(tmp_path, s3, "prod")
    _verify_receipt(tmp_path, s3, nonprod, T0)
    forged = {
        k.replace("/receipts/nonprod/", "/deploys/nonprod/"): v
        for (b, k), v in s3.objects.items()
        if "/receipts/nonprod/" in k
    }
    for b, k in [bk for bk in s3.objects if "/receipts/nonprod/" in bk[1]]:
        del s3.objects[(b, k)]
    for k, v in forged.items():
        s3.put("acme-releases", k, v)
    found = receipts.check_receipt(prod, environment="nonprod", clients=s3)
    assert found.exit_code == 1 and "receipts/nonprod/ holds no receipt" in found.reason


def test_a_deploy_receipt_in_the_gate_s_folder_fails_closed(tmp_path: Path, s3: DeployS3) -> None:
    """receipts/<env>/ holds verify receipts only: anything else there decides nothing, even
    with an older PASSED verify receipt beside it."""
    nonprod = _published(tmp_path, s3, "nonprod", block=_deploy_block())
    prod = _published(tmp_path, s3, "prod")
    _verify_receipt(tmp_path, s3, nonprod, T0)
    done = _deploy(nonprod, s3, env="nonprod", now=T0 + timedelta(seconds=1))
    for suffix in (".json", ".report.json"):
        src = _deploys_key(str(done.receipt)).removesuffix(".json") + suffix
        s3.put(
            "acme-releases",
            src.replace("/deploys/", "/receipts/"),
            s3.objects[("acme-releases", src)],
        )
    found = receipts.check_receipt(prod, environment="nonprod", clients=s3)
    assert found.exit_code == 1 and "records a deploy" in found.reason


def test_deploy_receipts_keep_the_folder_rules_and_never_gate(tmp_path: Path, s3: DeployS3) -> None:
    nonprod = _published(tmp_path, s3, "nonprod", block=_deploy_block())
    prod = _published(tmp_path, s3, "prod")
    _verify_receipt(tmp_path, s3, nonprod, T0)
    done = _deploy(nonprod, s3, env="nonprod", now=T0 + timedelta(seconds=1))
    # a delete marker on the deploy receipt: the gate never reads deploys/, so it still passes
    s3.deleted.add(_deploys_key(str(done.receipt)))
    del s3.objects[("acme-releases", _deploys_key(str(done.receipt)))]
    assert receipts.check_receipt(prod, environment="nonprod", clients=s3).exit_code == 0
    store = receipts._place(nonprod, str(done.receipt).split("/")[-4], "nonprod", s3, None).store(
        "nonprod", rr.DEPLOYS_DIR
    )
    with pytest.raises(receipts.ReceiptError, match="overwritten or deleted"):
        receipts._newest_receipt(store, "sha256:" + str(done.receipt).split("/")[-4], [], rr.DEPLOY)


def test_a_receipt_with_another_check_is_not_one() -> None:
    name = rr.receipt_name(T0, "sha256:" + "a" * 64)
    receipt = {
        "schema": rr.RECEIPT_SCHEMA,
        "check": "promote",
        "code_digest": "sha256:" + "b" * 64,
        "identity_digest": "sha256:" + "a" * 64,
        "result": "PASSED",
        "environment": "nonprod",
        "product_id": "acme.orders",
        "time_verified": rr.iso(T0),
        "report": {"sha256": "sha256:" + "c" * 64},
    }
    parsed, problem = rr.parse_receipt(name, json.dumps(receipt).encode())
    assert parsed is None and problem == "its check is not verify or deploy"


def test_a_missing_lock_names_the_permission_that_reads_it(tmp_path: Path) -> None:
    s3 = DeployS3()
    s3.object_lock, s3.retention = True, ("GOVERNANCE", 1)
    nonprod = _published(tmp_path, s3, "nonprod", block=_deploy_block())
    prod = _published(tmp_path, s3, "prod")
    _verify_receipt(tmp_path, s3, nonprod, datetime.now(timezone.utc) - timedelta(seconds=5))
    s3.unlocked.add(prod.split("/", 3)[3] + "release.json")
    found = receipts.check_receipt(prod, environment="nonprod", clients=s3, require_locked=True)
    assert found.exit_code == 1 and "a reader without s3:GetObjectRetention sees no lock" in (
        found.reason
    )


def test_runs_status_lists_the_newest_deploy_receipt_beside_the_verify_one(
    tmp_path: Path, s3: DeployS3
) -> None:
    from fluid_build.cli.ops import release_status as rs

    _sealed_release(tmp_path / "nonprod", "nonprod", _deploy_block())
    rel = tmp_path / "nonprod"
    block = receipts.report_release_block(str(rel), "acme.orders")
    report = tmp_path / "verify.json"
    report.write_text(json.dumps({"summary": PASSING, "release": block}), encoding="utf-8")
    receipts.record_receipt(str(rel), report_path=str(report), now=T0)
    assert _deploy(str(rel), s3, env="nonprod", now=T0 + timedelta(seconds=1)).exit_code == 0
    index = json.loads((rel / "release.json").read_text(encoding="utf-8"))
    found = receipts.newest_by_environment(rel, index["code_digest"])
    assert found["nonprod"]["result"] == "PASSED"
    assert found["nonprod"]["deploy"]["time_verified"] == rr.iso(T0 + timedelta(seconds=1))
    (sealed_release,) = rs.find_releases([str(rel)], "acme.orders").releases
    lines = rs.describe(sealed_release)
    assert any(line.startswith("  receipt  nonprod PASSED") for line in lines)
    assert any(line.startswith("  deployed nonprod PASSED") for line in lines)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    ops_cli.register_runs(parser.add_subparsers())
    return parser.parse_args(["runs", "deploy", *argv])


@pytest.mark.parametrize(
    "argv, problem",
    [
        (["SRC", "--env", " "], "--env needs an environment name"),
        (["SRC", "--env", "prod", "--require-locked"], "apply to --require-receipt ENV"),
        (["SRC", "--env", "prod", "--sign-key", "k"], "add --sign"),
        (["SRC", "--env", "prod", "--sign", "--dry-run"], "writes no receipt to sign"),
        (
            ["SRC", "--env", "prod", "--require-receipt", "nonprod", "--require-signed"],
            "--require-signed needs the signer",
        ),
    ],
)
def test_the_command_refuses_flags_that_do_not_go_together(
    argv: List[str], problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    args = _parse(*argv)
    assert args.func(args, logging.getLogger("t")) == 2
    assert problem in caplog.text


def test_the_command_prints_each_target_and_exits_with_the_deploy(
    tmp_path: Path, s3: DeployS3, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    uri = _published(tmp_path, s3)
    monkeypatch.setattr("fluid_build.util.s3.bounded_clients", s3)
    monkeypatch.setattr(
        "fluid_build.iac.target_identity.aws_caller_account", lambda e, r: (ACCOUNT, "")
    )
    args = _parse(uri, "--env", "prod")
    assert args.func(args, logging.getLogger("t")) == 0
    out = capsys.readouterr().out
    assert "deployed (prod)" in out and "copied      s3://acme-airflow/dags/orders_dag.py#" in out
    args = _parse(uri, "--env", "prod", "--json")
    assert args.func(args, logging.getLogger("t")) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "deployed"


# ---------------------------------------------------------------------------
# A bucket whose versioning is Suspended, and the other edges of a copy
# ---------------------------------------------------------------------------


class SuspendedS3(DeployS3):
    """Versioning Suspended: every object written now is the "null" version, which the next
    write replaces in place (no older version is kept or listed)."""

    def __init__(self) -> None:
        super().__init__()
        self.versioning = "Suspended"

    def version(self, bucket: str, key: str) -> str:
        return "null"

    @staticmethod
    def version_of(key: str) -> str:
        return "null"

    def write(self, bucket: str, key: str, data: bytes) -> None:
        self.put(bucket, key, data)
        self.writes.append(key)


def test_a_suspended_bucket_pins_nothing_and_nothing_is_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "null" version is replaced in place: bytes written after the check would be copied."""
    s3 = SuspendedS3()
    uri = _published(tmp_path, s3)
    real = deploys._Copier._write

    def writer_races(self: Any, target: Any, *a: Any, **kw: Any) -> Any:
        src = uri.split("/", 3)[3] + "external/orders-jobs/orders_jobs-1.4.0.zip"
        s3.put("acme-releases", src, b"EVIL package")
        return real(self, target, *a, **kw)

    monkeypatch.setattr(deploys._Copier, "_write", writer_races)
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and "versioning is Suspended (Enabled is needed)" in done.reason
    assert _target_writes(s3) == [] and s3.copies == [] and done.receipt is None


def test_a_suspended_bucket_s_release_json_rewritten_in_place_is_not_taken(
    tmp_path: Path,
) -> None:
    s3 = SuspendedS3()
    uri = _published(tmp_path, s3)
    key = uri.split("/", 3)[3] + "release.json"
    index = json.loads(s3.objects[("acme-releases", key)])
    for t in index["identity"]["deploy"]["targets"]:
        if t["artifact"] == "orders-jobs":
            t["to"] = "s3://acme-jobs/somewhere/else.zip"
    index["identity_digest"] = release.identity_digest(index["identity"])
    s3.put("acme-releases", key, (json.dumps(index, indent=2, sort_keys=True) + "\n").encode())
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and ("acme-jobs", "somewhere/else.zip") not in s3.objects


def test_a_verified_read_s_null_version_is_no_version() -> None:
    from fluid_build.util import s3

    assert [s3.version_id(v) for v in ("null", "", None, 3, "v1")] == [None] * 4 + ["v1"]


def test_the_copy_takes_the_etag_the_check_read_not_a_fresh_one(
    tmp_path: Path, s3: DeployS3
) -> None:
    uri = _published(tmp_path, s3)
    checked = receipts._verified(uri, s3, None)
    etags = {o["path"]: o["etag"] for o in checked.objects}
    assert _deploy(uri, s3).exit_code == 0
    prefix = uri.split("/", 3)[3]
    for copy in s3.copies:
        rel = copy["CopySource"]["Key"][len(prefix) :]
        assert copy["CopySourceIfMatch"] == etags[rel]


def test_a_copy_whose_answer_was_lost_and_whose_retry_meets_it_counts_as_copied(
    tmp_path: Path,
) -> None:
    class LostResponse(DeployS3):
        lost = True

        def copy_object(self, **kw: Any) -> Dict[str, Any]:
            out = super().copy_object(**kw)
            if self.lost:  # applied, the answer lost, botocore's retry meets the key: 412
                self.lost = False
                raise S3Error("PreconditionFailed", 412)
            return out

    s3 = LostResponse()
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3)
    assert done.exit_code == 0, done.reason
    assert [t["action"] for t in done.targets] == ["copied", "copied"]
    assert all(t["version_id"] for t in done.targets)


def test_a_412_without_a_code_is_read_by_its_status(tmp_path: Path, s3: DeployS3) -> None:
    uri = _published(tmp_path, s3)
    s3.put("acme-jobs", "jobs/orders_jobs.zip", b"someone else's")
    s3.copy_errors = [S3Error("", 412)]
    done = _deploy(uri, s3)
    assert done.exit_code == 1 and "changed meanwhile" in done.reason


def test_a_copy_s3_refused_fails_with_exit_1_and_a_failed_receipt(
    tmp_path: Path, s3: DeployS3
) -> None:
    uri = _published(tmp_path, s3)
    s3.copy_errors = [S3Error("NoSuchBucket", 404)]
    done = _deploy(uri, s3)
    stored = json.loads(s3.objects[("acme-releases", str(done.receipt).split("/", 3)[3])])
    assert done.exit_code == 1 and stored["result"] == "FAILED"


def test_a_dag_relabelled_as_a_file_is_not_intact(tmp_path: Path) -> None:
    block = _deploy_block()
    for t in block["targets"]:
        if t["kind"] == "airflow-dag":
            t["kind"] = "file"
            t["to"] = "s3://acme-airflow/aaa/orders_dag.py"
    _sealed_release(tmp_path / "rel", "prod", block)
    found = api_release.verify_release(tmp_path / "rel")
    assert not found.intact and any("pins no external file so" in p for p in found.problems)


def test_a_target_uri_s3_does_not_take_is_not_intact(tmp_path: Path) -> None:
    block = _deploy_block()
    block["targets"][0]["to"] = "s3://Bad_Bucket/key"
    _sealed_release(tmp_path / "rel", "prod", block)
    assert not api_release.verify_release(tmp_path / "rel").intact


def test_a_bad_deploy_entry_keeps_the_verify_result_in_status(tmp_path: Path) -> None:
    _sealed_release(tmp_path / "nonprod", "nonprod", _deploy_block())
    rel = tmp_path / "nonprod"
    block = receipts.report_release_block(str(rel), "acme.orders")
    report = tmp_path / "verify.json"
    report.write_text(json.dumps({"summary": PASSING, "release": block}), encoding="utf-8")
    receipts.record_receipt(str(rel), report_path=str(report), now=T0)
    junk = tmp_path / "deploys" / "nonprod"
    junk.mkdir(parents=True)
    (junk / "20261126T090001.000000Z-0000000000000000.json").write_text("{}", encoding="utf-8")
    index = json.loads((rel / "release.json").read_text(encoding="utf-8"))
    found = receipts.newest_by_environment(rel, index["code_digest"])["nonprod"]
    assert found["result"] == "PASSED" and "problem" in found["deploy"]


@pytest.mark.parametrize("environment", ["receipts", "deploys"])
def test_an_environment_named_as_a_folder_is_refused_at_the_seal(
    lockdir: Path, environment: str
) -> None:
    from fluid_build.cli import _ship_stages as st

    opts = st.ShipOptions(contract=str(lockdir / "contract.fluid.yaml"), env=environment)
    assert "cannot be one of those" in str(release.refusal(lockdir / "rel", [1, 2, 3, 4], {}, opts))


def test_a_deploy_statement_names_its_own_policy_and_never_stands_for_a_check() -> None:
    receipt: Dict[str, Any] = {
        "check": "deploy",
        "verifier": {},
        "time_verified": rr.iso(T0),
        "resource_uri": "s3://b-1/p/",
        "report": {"name": "x.report.json", "sha256": "sha256:" + "a" * 64},
        "result": "PASSED",
        "environment": "nonprod",
        "code_digest": "sha256:" + "b" * 64,
        "identity_digest": "sha256:" + "c" * 64,
        "release_objects": [],
    }
    signed = receipts.statement(receipt, b"{}")
    assert signed["predicate"]["policy"]["uri"].endswith("#deploy-log")
    assert signed["predicate"][receipts.RECEIPT_EXTENSION]["check"] == "deploy"
    receipt["check"] = "verify"
    assert receipts.statement(receipt, b"{}")["predicate"]["policy"]["uri"].endswith(
        "#report-verdict"
    )


def test_objects_written_while_versioning_was_suspended_are_named(tmp_path: Path) -> None:
    """Versioning Enabled now, and the release's objects still the "null" version they were
    written as: publish again."""
    s3 = SuspendedS3()
    uri = _published(tmp_path, s3)
    s3.versioning = "Enabled"
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and "written while the bucket's versioning was off or " in (
        done.reason
    )
    assert "publish from a new commit" in done.reason and _target_writes(s3) == []


def test_an_object_read_with_no_etag_is_named(
    tmp_path: Path, s3: DeployS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = _published(tmp_path, s3)
    real = DeployS3.get_object

    def no_etag(self: Any, **kw: Any) -> Dict[str, Any]:
        got = real(self, **kw)
        got.pop("ETag", None)
        return got

    monkeypatch.setattr(DeployS3, "get_object", no_etag)
    done = _deploy(uri, s3)
    assert done.exit_code == 2 and "was read with no ETag" in done.reason


def test_a_dry_run_that_cannot_read_a_target_decides_nothing(
    tmp_path: Path, s3: DeployS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = _published(tmp_path, s3)
    real = DeployS3.head_object

    def slow(self: Any, **kw: Any) -> Dict[str, Any]:
        if kw.get("ExpectedBucketOwner"):
            raise S3Error("SlowDown", 503)
        return real(self, **kw)

    monkeypatch.setattr(DeployS3, "head_object", slow)
    done = _deploy(uri, s3, dry_run=True)
    assert done.exit_code == 2 and done.receipt is None
    done = _deploy(uri, s3)  # a real deploy that stopped: exit 1 and a FAILED receipt
    assert done.exit_code == 1 and done.receipt is not None


def test_a_read_back_refused_names_s3_get_object_version(
    tmp_path: Path, s3: DeployS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    uri = _published(tmp_path, s3)
    real = DeployS3.head_object

    def no_versions(self: Any, **kw: Any) -> Dict[str, Any]:
        if kw.get("ExpectedBucketOwner") and kw.get("VersionId"):
            raise S3Error("AccessDenied", 403)
        return real(self, **kw)

    monkeypatch.setattr(DeployS3, "head_object", no_versions)
    done = _deploy(uri, s3)
    assert done.exit_code == 1 and "lack s3:GetObjectVersion on it" in done.reason


def test_a_mirrored_layout_is_undecided_before_any_target_is_written(
    tmp_path: Path, s3: DeployS3
) -> None:
    uri = _published(tmp_path, s3)
    src = uri.split("/", 3)[3]
    for (bucket, key), data in list(s3.objects.items()):
        if bucket == "acme-releases" and key.startswith(src):
            s3.put(bucket, "mirror/" + key[len(src) :], data)
    done = _deploy("s3://acme-releases/mirror/", s3)
    assert done.exit_code == 2, done.reason
    assert "is not <prefix>/<product>/<code hex>/<env>/" in done.reason
    assert _target_writes(s3) == [] and s3.copies == []
    assert done.receipt is None


def test_a_receipt_that_cannot_be_written_after_the_copies_fails_and_names_the_targets(
    tmp_path: Path,
) -> None:
    class NoReceiptWrites(DeployS3):
        def put_object(self, **kw: Any) -> Dict[str, Any]:  # type: ignore[override]
            if "/deploys/prod/" in kw.get("Key", ""):
                raise S3Error("AccessDenied", 403)
            return super().put_object(**kw)

    s3 = NoReceiptWrites()
    uri = _published(tmp_path, s3)
    done = _deploy(uri, s3)
    assert done.exit_code == 1, done.reason
    assert done.receipt is None
    assert "no deploy receipt exists" in done.reason
    assert "s3://acme-jobs/jobs/orders_jobs.zip" in done.reason
    assert "s3://acme-airflow/dags/orders_dag.py" in done.reason
