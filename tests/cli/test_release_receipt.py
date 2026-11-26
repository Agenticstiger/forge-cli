# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs receipt record|check``: an environment's receipt for a sealed release, and the
gate a later environment passes only when an earlier one checked the same code."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from fluid_build.cli import _release_publish as publish
from fluid_build.cli import _release_receipt as receipts
from fluid_build.cli.ops import _cli as ops_cli
from fluid_build.util import receipt_record as rr
from tests.cli._fake_s3 import FakeS3, hex_of, sealed

pytestmark = pytest.mark.unit

TARGET = "s3://acme-releases/handoff/"
PASSING = {"total": 1, "match": 1, "mismatch": 0, "error": 0}
T0 = datetime(2026, 11, 26, 9, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fluid_build.util.s3.installed", lambda: True)


def _report(
    path: Path, release: Any, summary: Dict[str, int] = PASSING, *, clients: Any = None
) -> Path:
    """A ``fluid verify --release <release> --out <path>`` report."""
    block = receipts.report_release_block(str(release), "acme.orders", clients=clients)
    path.write_text(json.dumps({"summary": summary, "release": block}), encoding="utf-8")
    return path


@pytest.fixture
def s3() -> FakeS3:
    """A versioned bucket the releases are published to."""
    bucket = FakeS3()
    bucket.versions = True
    return bucket


def _published(tmp_path: Path, s3: FakeS3, *envs: str) -> Dict[str, str]:
    """Each environment's release of one commit and one lock, published: ``{env: s3 uri}``."""
    out = {}
    for env in envs:
        sealed(tmp_path / env, environment=env)
        out[env] = publish.publish_release(tmp_path / env, TARGET, clients=s3).uri
    return out


def _names(s3: FakeS3, env: str) -> List[str]:
    return sorted(k.rsplit("/", 1)[1] for b, k in s3.objects if f"/receipts/{env}/" in k)


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, want",
    [
        (
            "s3://acme-dags/dags/orders.py#v1.2_a-b",
            receipts.Target("s3://acme-dags/dags/orders.py", version_id="v1.2_a-b"),
        ),
        (
            "git+https://git.example.com/ops/dags@" + "a" * 40,
            receipts.Target("git+https://git.example.com/ops/dags", commit="a" * 40),
        ),
        (
            "git+ssh://git@git.example.com/ops/dags.git@" + "b" * 64,
            receipts.Target("git+ssh://git@git.example.com/ops/dags.git", commit="b" * 64),
        ),
    ],
)
def test_a_target_is_an_object_version_or_a_commit(value: str, want: receipts.Target) -> None:
    assert receipts.parse_target(value) == want


@pytest.mark.parametrize(
    "value",
    [
        "s3://acme-dags/dags/orders.py",  # no version: the key can change
        "s3://acme-dags/#v1",
        "git+https://git.example.com/ops/dags@abc1234",  # a short commit
        "git+https://git.example.com/ops/dags",
        "https://git.example.com/ops/dags@" + "a" * 40,
        "git+https://user:secret@git.example.com/ops/dags@" + "a" * 40,
    ],
)
def test_a_target_that_is_not_one_is_refused(value: str) -> None:
    with pytest.raises(receipts.ReceiptError) as raised:
        receipts.parse_target(value)
    assert raised.value.exit_code == 2
    assert "secret" not in str(raised.value)


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


def test_record_writes_the_report_then_the_receipt_beside_the_release(
    tmp_path: Path, s3: FakeS3
) -> None:
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    done = receipts.record_receipt(uris["nonprod"], report_path=str(report), clients=s3, now=T0)
    index = json.loads((tmp_path / "nonprod" / "release.json").read_text(encoding="utf-8"))
    name = f"20261126T090000.123456Z-{hex_of(index, 'identity_digest')[:16]}.json"
    code_root = uris["nonprod"].removesuffix("nonprod/")
    assert done.uri == f"{code_root}receipts/nonprod/{name}"
    assert done.result == "PASSED" and done.written == [rr.report_name(name), name]
    assert [k.rsplit("/", 1)[1] for k in s3.writes[-2:]] == done.written  # the receipt last
    receipt = done.receipt
    assert receipt["code_digest"] == index["code_digest"]
    assert receipt["identity_digest"] == index["identity_digest"]
    assert receipt["environment"] == "nonprod" and receipt["time_verified"] == rr.iso(T0)
    assert receipt["resource_uri"] == uris["nonprod"]
    assert (
        receipt["report"]["sha256"] == "sha256:" + hashlib.sha256(report.read_bytes()).hexdigest()
    )
    assert {o["path"] for o in receipt["release_objects"]} >= {"release.json", "bundle.tgz"}
    assert all(o["version_id"] for o in receipt["release_objects"])
    stored = s3.objects[("acme-releases", done.uri.split("/", 3)[3])]
    assert rr.parse_receipt(name, stored) == (receipt, None)


def test_a_failed_check_is_recorded_as_failed(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod")
    summary = {"total": 2, "match": 1, "mismatch": 1, "error": 0}
    report = _report(tmp_path / "verify.json", uris["nonprod"], summary, clients=s3)
    done = receipts.record_receipt(uris["nonprod"], report_path=str(report), clients=s3)
    assert done.result == "FAILED"
    assert done.reason == "1 output port(s) differ from the contract"
    assert len(_names(s3, "nonprod")) == 2


def test_a_report_on_another_release_is_refused_and_nothing_is_written(
    tmp_path: Path, s3: FakeS3
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    on_prod = _report(tmp_path / "prod.json", uris["prod"], clients=s3)
    with pytest.raises(receipts.ReceiptError, match=r"another release \(identity_digest differs\)"):
        receipts.record_receipt(uris["nonprod"], report_path=str(on_prod), clients=s3)
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"summary": PASSING}), encoding="utf-8")
    with pytest.raises(receipts.ReceiptError, match="names no release: run fluid verify --release"):
        receipts.record_receipt(uris["nonprod"], report_path=str(bare), clients=s3)
    assert _names(s3, "nonprod") == []


def test_a_release_that_is_not_intact_gets_no_receipt(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    key = uris["nonprod"].split("/", 3)[3] + "bundle.tgz"
    s3.objects[("acme-releases", key)] = b"swapped"
    with pytest.raises(receipts.ReceiptError) as raised:
        receipts.record_receipt(uris["nonprod"], report_path=str(report), clients=s3)
    assert raised.value.exit_code == 1 and "is not intact" in str(raised.value)
    assert _names(s3, "nonprod") == []


def test_the_environment_must_be_the_releases(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    with pytest.raises(receipts.ReceiptError, match="is the release of nonprod") as raised:
        receipts.record_receipt(
            uris["nonprod"], report_path=str(report), environment="prod", clients=s3
        )
    assert raised.value.exit_code == 2


def test_an_s3_source_outside_the_published_layout_is_refused(tmp_path: Path, s3: FakeS3) -> None:
    sealed(tmp_path / "rel", environment="nonprod")
    for rel in (
        "bundle.tgz",
        "artifacts/MANIFEST.json",
        "reports/ship-report.json",
        "release.json",
    ):
        s3.put("acme-releases", f"loose/{rel}", (tmp_path / "rel" / rel).read_bytes())
    report = _report(tmp_path / "verify.json", "s3://acme-releases/loose/", clients=s3)
    with pytest.raises(receipts.ReceiptError, match="is not <prefix>/<product>/<code hex>/<env>/"):
        receipts.record_receipt("s3://acme-releases/loose/", report_path=str(report), clients=s3)


def test_targets_are_recorded_and_checked_when_asked(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    s3.put("acme-dags", "dags/orders.py", b"dag")
    version = s3.version_of("dags/orders.py")
    commit = "c" * 40
    targets = [
        f"s3://acme-dags/dags/orders.py#{version}",
        f"git+https://git.example.com/ops@{commit}",
    ]
    plain = receipts.record_receipt(
        uris["nonprod"], report_path=str(report), targets=targets, clients=s3, now=T0
    )
    assert plain.receipt["targets"] == [
        {"uri": "s3://acme-dags/dags/orders.py", "version_id": version, "checked": False},
        {"uri": "git+https://git.example.com/ops", "commit": commit, "checked": False},
    ]
    checked = receipts.record_receipt(
        uris["nonprod"], report_path=str(report), targets=targets, check_targets=True, clients=s3
    )
    assert checked.result == "PASSED"
    assert checked.receipt["targets"][0] == {
        "uri": "s3://acme-dags/dags/orders.py",
        "version_id": version,
        "checked": True,
        "sha256": "sha256:" + hashlib.sha256(b"dag").hexdigest(),
        "size": 3,
    }
    assert checked.receipt["targets"][1]["checked"] is False  # a commit is never checked
    missing = receipts.record_receipt(
        uris["nonprod"],
        report_path=str(report),
        targets=["s3://acme-dags/dags/orders.py#other"],
        check_targets=True,
        clients=s3,
    )
    assert missing.result == "FAILED" and "#other is not there" in missing.reason


def test_a_release_on_disk_gets_its_receipts_beside_it(tmp_path: Path, s3: FakeS3) -> None:
    sealed(tmp_path / "rel", environment="nonprod")
    report = _report(tmp_path / "verify.json", tmp_path / "rel")
    done = receipts.record_receipt(str(tmp_path / "rel"), report_path=str(report), now=T0)
    stored = tmp_path / "receipts" / "nonprod" / done.uri.rsplit("/", 1)[1]
    assert stored.is_file() and (stored.stat().st_mode & 0o077) == 0
    assert done.receipt["resource_uri"] == str((tmp_path / "rel").absolute())
    with pytest.raises(receipts.ReceiptError, match="a receipt is written once"):
        receipts.record_receipt(str(tmp_path / "rel"), report_path=str(report), now=T0)


def test_sign_stores_a_signed_verification_summary(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    signed: List[Tuple[str, Any]] = []

    def fake_sign(path: str, *, key_ref: Any = None) -> Dict[str, Any]:
        signed.append((path, key_ref))
        Path(path + ".sig").write_bytes(b"signature")
        return {"exit_code": 0, "sig_path": path + ".sig", "pem_path": None}

    monkeypatch.setattr(_signing, "cosign_available", lambda: True)
    monkeypatch.setattr(_signing, "sign_blob", fake_sign)
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    done = receipts.record_receipt(
        uris["nonprod"], report_path=str(report), sign=True, key_ref="awskms:///alias/k", clients=s3
    )
    stem = done.uri.rsplit("/", 1)[1].removesuffix(".json")
    assert done.written == [
        f"{stem}.report.json",
        f"{stem}.intoto.jsonl",
        f"{stem}.intoto.jsonl.sig",
        f"{stem}.json",
    ]
    assert signed[0][1] == "awskms:///alias/k"
    prefix = done.uri.split("/", 3)[3].rsplit("/", 1)[0]
    statement = json.loads(s3.objects[("acme-releases", f"{prefix}/{stem}.intoto.jsonl")])
    index = json.loads((tmp_path / "nonprod" / "release.json").read_bytes())
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == "https://slsa.dev/verification_summary/v1"
    assert (
        statement["subject"][0]["digest"]["sha256"]
        == hashlib.sha256((tmp_path / "nonprod" / "release.json").read_bytes()).hexdigest()
    )
    predicate = statement["predicate"]
    assert predicate["verificationResult"] == "PASSED"
    assert predicate["resourceUri"] == uris["nonprod"]
    extension = predicate[receipts.RECEIPT_EXTENSION]
    receipt_bytes = s3.objects[("acme-releases", done.uri.split("/", 3)[3])]
    assert extension["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert extension["identity_digest"] == index["identity_digest"]


def test_sign_without_cosign_is_refused_before_anything_is_written(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    monkeypatch.setattr(_signing, "cosign_available", lambda: False)
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    with pytest.raises(receipts.ReceiptError, match="--sign needs cosign") as raised:
        receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    assert raised.value.exit_code == 2 and _names(s3, "nonprod") == []


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _record(
    tmp_path: Path, s3: FakeS3, uri: str, when: datetime, summary: Dict[str, int] = PASSING
) -> Any:
    report = _report(tmp_path / f"verify-{when.timestamp()}.json", uri, summary, clients=s3)
    return receipts.record_receipt(uri, report_path=str(report), clients=s3, now=when)


def test_prod_passes_on_the_newest_nonprod_receipt_of_the_same_code(
    tmp_path: Path, s3: FakeS3
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 0, found.reason
    assert found.receipt and "/receipts/nonprod/" in found.receipt
    assert found.code_digest and uris["prod"].endswith(f"{found.code_digest[7:]}/prod/")
    assert len(found.locks) >= 6 and all(item["lock"] is None for item in found.locks)


def test_no_receipt_and_a_failed_newest_receipt_do_not_pass(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    none = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert none.exit_code == 1 and "holds no receipt for code" in none.reason
    _record(tmp_path, s3, uris["nonprod"], T0)
    _record(
        tmp_path,
        s3,
        uris["nonprod"],
        T0 + timedelta(hours=1),
        {"total": 1, "match": 0, "mismatch": 0, "error": 1},
    )
    failed = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert failed.exit_code == 1 and "receipt for this code failed" in failed.reason


def test_the_check_needs_more_than_a_list_page(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    for hour in range(3):  # 6 objects: three pages of two
        _record(tmp_path, s3, uris["nonprod"], T0 + timedelta(hours=hour))
    _record(
        tmp_path,
        s3,
        uris["nonprod"],
        T0 + timedelta(hours=5),
        {"total": 1, "match": 0, "mismatch": 1, "error": 0},
    )
    assert receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3).exit_code == 1


def test_a_receipt_whose_stored_report_changed_does_not_pass(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    done = _record(tmp_path, s3, uris["nonprod"], T0)
    key = done.uri.split("/", 3)[3].removesuffix(".json") + ".report.json"
    s3.objects[("acme-releases", key)] = b'{"summary": {}}'
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 1 and "is not the one it names" in found.reason


def test_a_receipt_saying_passed_over_a_failing_report_does_not_pass(
    tmp_path: Path, s3: FakeS3
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    done = _record(
        tmp_path, s3, uris["nonprod"], T0, {"total": 1, "match": 0, "mismatch": 1, "error": 0}
    )
    key = done.uri.split("/", 3)[3]
    edited = dict(done.receipt, result="PASSED", reason="")
    s3.objects[("acme-releases", key)] = json.dumps(edited).encode()
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 1 and "does not pass: 1 output port(s) differ" in found.reason


def test_a_newest_file_that_is_not_a_receipt_fails_closed(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    done = _record(tmp_path, s3, uris["nonprod"], T0)
    prefix = done.uri.split("/", 3)[3].rsplit("/", 1)[0]
    s3.put("acme-releases", f"{prefix}/20991231T000000.000000Z-{'0' * 16}.json", b"{}")
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert (
        found.exit_code == 1
        and "the newest receipt" in found.reason
        and "is not one" in found.reason
    )


def test_a_nonprod_release_that_is_no_longer_intact_does_not_pass(
    tmp_path: Path, s3: FakeS3
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    s3.objects[("acme-releases", uris["nonprod"].split("/", 3)[3] + "bundle.tgz")] = b"swapped"
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert (
        found.exit_code == 1
        and "the nonprod release" in found.reason
        and "not intact" in found.reason
    )


def test_the_check_never_reads_gates_or_provenance(tmp_path: Path, s3: FakeS3) -> None:
    """A prod release whose gates and provenance say anything at all is judged the same way:
    both sit outside its identity, so editing them changes nothing the check reads."""
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    key = uris["prod"].split("/", 3)[3] + "release.json"
    index = json.loads(s3.objects[("acme-releases", key)])
    index["gates"] = [{"name": "nonprod-receipt", "status": "passed"}]
    index["provenance"] = {"run_id": "forged"}
    rewritten = json.dumps(index).encode()
    index["files"] = index["files"]
    s3.put("acme-releases", key, rewritten)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 0, found.reason


def test_the_check_reports_locks_and_requires_them_only_when_asked(
    tmp_path: Path, s3: FakeS3
) -> None:
    s3.object_lock, s3.retention = True, ("GOVERNANCE", 1)
    uris = _published(tmp_path, s3, "nonprod", "prod")
    done = _record(tmp_path, s3, uris["nonprod"], T0)
    found = receipts.check_receipt(
        uris["prod"], environment="nonprod", require_locked=True, clients=s3
    )
    assert found.exit_code == 0, found.reason
    assert all(str(item["lock"]).startswith("GOVERNANCE until ") for item in found.locks)
    s3.unlocked.add(done.uri.split("/", 3)[3])
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 0
    assert [item["uri"] for item in found.locks if item["lock"] is None] == [done.uri]
    found = receipts.check_receipt(
        uris["prod"], environment="nonprod", require_locked=True, clients=s3
    )
    assert found.exit_code == 1 and "1 object(s) carry no Object Lock in force" in found.reason


def test_a_release_without_a_code_digest_cannot_be_gated(tmp_path: Path, s3: FakeS3) -> None:
    index = sealed(tmp_path / "rel", environment="prod")
    index["identity"].pop("source")
    from fluid_build.util.release_record import identity_digest

    index["identity_digest"] = identity_digest(index["identity"])
    index.pop("code_digest")
    (tmp_path / "rel" / "release.json").write_text(json.dumps(index), encoding="utf-8")
    found = receipts.check_receipt(str(tmp_path / "rel"), environment="nonprod")
    assert found.exit_code == 2 and "names no source commit" in found.reason


def test_on_disk_the_gate_reads_the_receipts_beside_the_release(tmp_path: Path) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    sealed(tmp_path / "prod", environment="prod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report), now=T0)
    found = receipts.check_receipt(
        str(tmp_path / "prod"), environment="nonprod", require_locked=False
    )
    assert found.exit_code == 0, found.reason
    locked = receipts.check_receipt(
        str(tmp_path / "prod"), environment="nonprod", require_locked=True
    )
    assert locked.exit_code == 1 and "on disk carries no Object Lock" in locked.reason


def test_on_disk_a_receipt_naming_a_release_elsewhere_does_not_pass(tmp_path: Path) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    sealed(tmp_path / "prod", environment="prod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    done = receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report), now=T0)
    path = tmp_path / "receipts" / "nonprod" / done.uri.rsplit("/", 1)[1]
    edited = dict(done.receipt, resource_uri="/elsewhere/nonprod")
    path.write_text(json.dumps(edited, indent=2, sort_keys=True), encoding="utf-8")
    found = receipts.check_receipt(str(tmp_path / "prod"), environment="nonprod")
    assert found.exit_code == 1 and "names a release outside" in found.reason


# ---------------------------------------------------------------------------
# fluid runs receipt, fluid runs status
# ---------------------------------------------------------------------------


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    parser = argparse.ArgumentParser()
    ops_cli.register_runs(parser.add_subparsers())
    args = parser.parse_args(["runs", "receipt", *argv])
    code = args.func(args, logging.getLogger("test"))
    return code, capsys.readouterr().out


def test_the_commands_exit_0_1_and_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: Any
) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    sealed(tmp_path / "prod", environment="prod")
    code, out = _run(capsys, "check", str(tmp_path / "prod"), "--env", "nonprod")
    assert code == 1 and "DOES NOT PASS" in out
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    code, out = _run(capsys, "record", str(tmp_path / "nonprod"), "--report", str(report))
    assert code == 0 and out.startswith("Receipt PASSED: ")
    code, out = _run(capsys, "check", str(tmp_path / "prod"), "--env", "nonprod", "--json")
    assert code == 0 and json.loads(out)["status"] == "passed"
    code, _ = _run(
        capsys, "record", str(tmp_path / "nonprod"), "--report", str(report), "--sign-key", "k"
    )
    assert code == 2 and "--sign-key signs the receipt: add --sign" in caplog.text
    code, _ = _run(
        capsys, "record", str(tmp_path / "nonprod"), "--report", str(tmp_path / "none.json")
    )
    assert code == 2


def test_runs_status_lists_the_newest_receipt_of_each_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    sealed(tmp_path / "prod", environment="prod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report), now=T0)
    parser = argparse.ArgumentParser()
    ops_cli.register_runs(parser.add_subparsers())
    args = parser.parse_args(
        ["runs", "status", "acme.orders", "--release-dir", str(tmp_path / "prod")]
    )
    assert args.func(args, logging.getLogger("test")) == 0
    out = capsys.readouterr().out
    assert f"receipt  nonprod PASSED {rr.iso(T0)}" in out
    args = parser.parse_args(
        ["runs", "status", "acme.orders", "--release-dir", str(tmp_path / "nonprod")]
    )
    args.func(args, logging.getLogger("test"))
    assert "(this release)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# fluid verify --release
# ---------------------------------------------------------------------------


def test_verify_release_names_the_release_in_the_report(tmp_path: Path, s3: FakeS3) -> None:
    from fluid_build.cli import verify
    from fluid_build.cli._common import CLIError

    uris = _published(tmp_path, s3, "nonprod")
    block = receipts.report_release_block(uris["nonprod"], "acme.orders", clients=s3)
    index = json.loads((tmp_path / "nonprod" / "release.json").read_text(encoding="utf-8"))
    assert block == {
        "source": uris["nonprod"],
        "product": "acme.orders",
        "environment": "nonprod",
        "identity_digest": index["identity_digest"],
        "code_digest": index["code_digest"],
    }
    with pytest.raises(
        receipts.ReceiptError, match="is a release of acme.orders, and the contract is other"
    ):
        receipts.report_release_block(uris["nonprod"], "other", clients=s3)
    args = argparse.Namespace(release=str(tmp_path / "nonprod"), out=None)
    with pytest.raises(CLIError) as raised:
        verify._release_block(args, "acme.orders")
    assert raised.value.exit_code == 2
    args = argparse.Namespace(release=str(tmp_path / "nonprod"), out=str(tmp_path / "r.json"))
    assert verify._release_block(args, "acme.orders")["identity_digest"] == index["identity_digest"]


def test_verify_has_the_release_flag() -> None:
    from fluid_build.cli import verify

    parser = argparse.ArgumentParser()
    verify.register(parser.add_subparsers())
    args = parser.parse_args(["verify", "c.yaml", "--release", "s3://b/p/", "--out", "r.json"])
    assert args.release == "s3://b/p/"


@pytest.mark.parametrize(
    "value",
    ["git+https://user:secret@git.example.com/ops/dags", "s3://user:secret@bucket/key"],
)
def test_a_refused_target_never_echoes_a_password(value: str) -> None:
    with pytest.raises(receipts.ReceiptError) as raised:
        receipts.parse_target(value)
    assert "secret" not in str(raised.value) and "***@" in str(raised.value)
