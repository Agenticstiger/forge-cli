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
from tests.cli._fake_s3 import FakeS3, hex_of
from tests.cli._release_fixtures import BUNDLE_DIGEST, release_block, sealed

pytestmark = pytest.mark.unit

TARGET = "s3://acme-releases/handoff/"
PASSING = {"total": 1, "match": 1, "mismatch": 0, "error": 0}
T0 = datetime(2026, 11, 26, 9, 0, 0, 123456, tzinfo=timezone.utc)
#: The password in the URLs that must never reach a receipt or a message.
PW = "secret"


@pytest.fixture(autouse=True)
def _boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fluid_build.util.s3.installed", lambda: True)


def _report(
    path: Path, release: Any, summary: Dict[str, int] = PASSING, *, clients: Any = None
) -> Path:
    """A ``fluid verify --release <release> --out <path>`` report."""
    block = release_block(str(release), "acme.orders", clients=clients)
    path.write_text(json.dumps({"summary": summary, "release": block}), encoding="utf-8")
    return path


@pytest.fixture
def s3() -> FakeS3:
    """A versioned bucket the releases are published to."""
    bucket = FakeS3()
    bucket.versions, bucket.versioning = True, "Enabled"
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
            receipts.Target("git+ssh://git.example.com/ops/dags.git", commit="b" * 64),
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
        "git+file:///srv/ops/dags@" + "a" * 40,
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
        "checksums": {
            "sha256": s3.checksums[("acme-dags", "dags/orders.py")],
            "type": "FULL_OBJECT",
        },
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
        Path(path + ".sigstore.json").write_bytes(b"bundle")
        return {"exit_code": 0, "bundle_path": path + ".sigstore.json"}

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
        f"{stem}.intoto.jsonl.sigstore.json",
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
    s3.put("acme-releases", f"{prefix}/20261126T110000.000000Z-{'0' * 16}.json", b"{}")
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
    # moved: the release is found by its folder's name beside this one
    edited = dict(done.receipt, resource_uri="/elsewhere/nonprod")
    path.write_text(json.dumps(edited, indent=2, sort_keys=True), encoding="utf-8")
    assert receipts.check_receipt(str(tmp_path / "prod"), environment="nonprod").exit_code == 0
    # a receipt naming the prod release is no nonprod check
    edited = dict(done.receipt, resource_uri=str(tmp_path / "prod"))
    path.write_text(json.dumps(edited, indent=2, sort_keys=True), encoding="utf-8")
    found = receipts.check_receipt(str(tmp_path / "prod"), environment="nonprod")
    assert found.exit_code == 1 and "is the release of prod, not nonprod" in found.reason


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


def test_verify_release_names_the_release_in_the_report(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import verify
    from fluid_build.cli._common import CLIError

    uris = _published(tmp_path, s3, "nonprod")
    block = release_block(uris["nonprod"], "acme.orders", clients=s3)
    index = json.loads((tmp_path / "nonprod" / "release.json").read_text(encoding="utf-8"))
    assert block == {
        "source": uris["nonprod"],
        "product": "acme.orders",
        "environment": "nonprod",
        "identity_digest": index["identity_digest"],
        "code_digest": index["code_digest"],
        "contract_digest": BUNDLE_DIGEST,
    }
    with pytest.raises(
        receipts.ReceiptError, match="is a release of acme.orders, and the contract is other"
    ):
        release_block(uris["nonprod"], "other", clients=s3)
    monkeypatch.setattr(verify, "_contract_bundle_digest", lambda path, env: BUNDLE_DIGEST)
    args = argparse.Namespace(
        release=str(tmp_path / "nonprod"), out=None, contract="c.yaml", env="nonprod"
    )
    with pytest.raises(CLIError) as raised:
        verify._release_block(args, "acme.orders")
    assert raised.value.exit_code == 2
    args = argparse.Namespace(
        release=str(tmp_path / "nonprod"),
        out=str(tmp_path / "r.json"),
        contract="c.yaml",
        env="nonprod",
    )
    assert verify._release_block(args, "acme.orders")["identity_digest"] == index["identity_digest"]


def test_verify_has_the_release_flag() -> None:
    from fluid_build.cli import verify

    parser = argparse.ArgumentParser()
    verify.register(parser.add_subparsers())
    args = parser.parse_args(["verify", "c.yaml", "--release", "s3://b/p/", "--out", "r.json"])
    assert args.release == "s3://b/p/"


@pytest.mark.parametrize(
    "value",
    [
        f"git+https://user:{PW}@git.example.com/ops/dags",
        f"s3://user:{PW}@bucket/key",
    ],
)
def test_a_refused_target_never_echoes_a_password(value: str) -> None:
    with pytest.raises(receipts.ReceiptError) as raised:
        receipts.parse_target(value)
    assert "secret" not in str(raised.value) and "***@" in str(raised.value)


# ---------------------------------------------------------------------------
# Review findings: each fails closed or gives its exit code, never a traceback
# ---------------------------------------------------------------------------


def test_require_locked_covers_the_release_the_receipt_vouches_for(
    tmp_path: Path, s3: FakeS3
) -> None:
    s3.object_lock, s3.retention = True, ("GOVERNANCE", 1)
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    nonprod = uris["nonprod"].split("/", 3)[3]
    s3.unlocked |= {k for b, k in s3.objects if k.startswith(nonprod)}
    found = receipts.check_receipt(
        uris["prod"], environment="nonprod", require_locked=True, clients=s3
    )
    assert found.exit_code == 1 and "carry no Object Lock in force" in found.reason
    assert any(item["uri"].startswith(uris["nonprod"]) for item in found.locks)


def test_a_lock_that_cannot_be_read_is_reported_not_raised(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.cli._fake_s3 import S3Error

    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    head = s3.head_object
    calls = {"n": 0}

    def flaky(**kw: Any) -> Dict[str, Any]:
        calls["n"] += 1
        if "/receipts/" in kw["Key"] and calls["n"] > 1:
            raise S3Error("InternalError", 500)
        return head(**kw)

    monkeypatch.setattr(s3, "head_object", flaky)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 0
    assert any("InternalError" in str(item.get("error")) for item in found.locks)
    found = receipts.check_receipt(
        uris["prod"], environment="nonprod", require_locked=True, clients=s3
    )
    assert found.exit_code == 2 and "cannot be read" in found.reason


def test_a_listing_past_the_cap_decides_nothing(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    _record(tmp_path, s3, uris["nonprod"], T0 + timedelta(hours=1))
    monkeypatch.setattr(rr, "MAX_ENTRIES", 3)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 2 and "the newest receipt cannot be told" in found.reason


@pytest.mark.parametrize(
    "stamp", ["20261399T000000.000000Z", "20260230T000000.000000Z", "00000101T000000.000000Z"]
)
def test_a_name_with_no_real_time_is_not_a_receipt(stamp: str) -> None:
    receipt, problem = rr.parse_receipt(f"{stamp}-{'0' * 16}.json", b"{}")
    assert receipt is None and problem == "its name carries no real time"


def test_deeply_nested_json_is_not_a_receipt_or_a_report(tmp_path: Path) -> None:
    deep = b"[" * 400_000 + b"]" * 400_000
    assert rr.parse_receipt(f"20261126T090000.000000Z-{'0' * 16}.json", deep)[1] == (
        "it is not JSON"
    )
    (tmp_path / "deep.json").write_bytes(deep)
    with pytest.raises(receipts.ReceiptError, match="is not JSON"):
        receipts._read_report(str(tmp_path / "deep.json"))


def test_a_fifo_is_refused_not_waited_on(tmp_path: Path) -> None:
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs here")
    os.mkfifo(tmp_path / "report.json")
    with pytest.raises(receipts.ReceiptError, match="is not a regular file"):
        receipts._read_report(str(tmp_path / "report.json"))
    store = receipts._DirStore(tmp_path)
    with pytest.raises(receipts.ReceiptError, match="is not a regular file"):
        store.read("report.json", 100)


def test_a_receipts_path_that_is_not_a_folder_is_refused(tmp_path: Path) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    (tmp_path / "receipts").write_text("a file", encoding="utf-8")
    with pytest.raises(receipts.ReceiptError, match="reached through a link"):
        receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report))


def test_on_disk_a_resource_uri_that_is_not_a_plain_path_does_not_pass(tmp_path: Path) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    sealed(tmp_path / "prod", environment="prod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    done = receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report), now=T0)
    path = tmp_path / "receipts" / "nonprod" / done.uri.rsplit("/", 1)[1]
    for bad in (f"{tmp_path}/nonprod\u0000x", 7, None):
        edited = dict(done.receipt, resource_uri=bad)
        path.write_text(json.dumps(edited), encoding="utf-8")
        found = receipts.check_receipt(str(tmp_path / "prod"), environment="nonprod")
        assert found.exit_code == 1 and "names a release outside" in found.reason


def test_runs_status_shows_a_bad_newest_receipt_as_the_gate_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    sealed(tmp_path / "prod", environment="prod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report), now=T0)
    (tmp_path / "receipts" / "nonprod" / f"20261126T110000.000000Z-{'0' * 16}.json").write_text(
        "{}", encoding="utf-8"
    )
    (tmp_path / "receipts" / "odd").write_text("not a folder", encoding="utf-8")
    parser = argparse.ArgumentParser()
    ops_cli.register_runs(parser.add_subparsers())
    args = parser.parse_args(
        ["runs", "status", "acme.orders", "--release-dir", str(tmp_path / "prod")]
    )
    assert args.func(args, logging.getLogger("test")) == 0
    out = capsys.readouterr().out
    assert "receipt  nonprod cannot be told: the newest receipt" in out
    assert "PASSED" not in out


def test_an_object_path_over_200_characters_is_kept_whole(tmp_path: Path) -> None:
    from fluid_build.api.release import verify_release

    long = "reports/" + "r" * 230 + ".json"
    sealed(tmp_path / "rel", {"reports/ship-report.json": b"{}", long: b"1"})
    paths = {o["path"] for o in verify_release(tmp_path / "rel").objects}
    assert long in paths


@pytest.mark.parametrize(
    "url",
    [
        f"git+https://user:{PW}@git.example.com/ops/dags",
        "git+https://ghp_secret@github.com/ops/dags",
        "git+https://gitlab.com/ops/dags?private_token=glpat-secret",
        "git+https://gitlab.com/ops/dags#secret",
    ],
)
def test_a_git_target_never_records_a_credential(url: str) -> None:
    target = receipts.parse_target(f"{url}@{'a' * 40}")
    assert "secret" not in target.uri and target.uri.startswith("git+https://")
    with pytest.raises(receipts.ReceiptError) as raised:
        receipts.parse_target(url)  # no commit: refused, and the message hides the secret
    assert "secret" not in str(raised.value)


def test_a_linked_receipts_folder_is_never_written_or_read(tmp_path: Path) -> None:
    sealed(tmp_path / "rel" / "nonprod", environment="nonprod")
    sealed(tmp_path / "rel" / "prod", environment="prod")
    report = _report(tmp_path / "verify.json", tmp_path / "rel" / "nonprod")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "rel" / "receipts").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(receipts.ReceiptError, match="reached through a link"):
        receipts.record_receipt(str(tmp_path / "rel" / "nonprod"), report_path=str(report))
    assert list((tmp_path / "elsewhere").iterdir()) == []
    found = receipts.check_receipt(str(tmp_path / "rel" / "prod"), environment="nonprod")
    assert found.exit_code == 1 and "reached through a link" in found.reason


def test_a_receipt_named_in_the_future_decides_nothing(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    future = _record(tmp_path, s3, uris["nonprod"], datetime(2099, 1, 1, tzinfo=timezone.utc))
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 2 and "is named later than now" in found.reason
    # a future name that is no receipt is passed over
    key = future.uri.split("/", 3)[3]
    s3.objects.pop(("acme-releases", key))
    s3.objects.pop(("acme-releases", key.removesuffix(".json") + ".report.json"))
    s3.put("acme-releases", key, b"junk")
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 0 and found.ignored == [
        future.uri.rsplit("/", 1)[1] + ": named later than now"
    ]


@pytest.mark.parametrize("how", ["overwritten", "deleted"])
def test_a_receipt_overwritten_or_deleted_after_it_was_written_fails(
    tmp_path: Path, s3: FakeS3, how: str
) -> None:
    """Object Lock keeps versions, not keys: a newer FAILED receipt hidden by a new version or a
    delete marker must not let an older PASSED one decide."""
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    failing = {"total": 1, "match": 0, "mismatch": 1, "error": 0}
    newer = _record(tmp_path, s3, uris["nonprod"], T0 + timedelta(hours=1), failing)
    key = newer.uri.split("/", 3)[3]
    if how == "deleted":
        s3.objects.pop(("acme-releases", key))
        s3.deleted.add(key)
    else:
        s3.overwritten.add(key)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 1 and "were overwritten or deleted after they were written" in (
        found.reason
    )


def test_in_s3_a_receipt_of_other_code_under_this_code_fails(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    failing = {"total": 1, "match": 0, "mismatch": 1, "error": 0}
    newer = _record(tmp_path, s3, uris["nonprod"], T0 + timedelta(hours=1), failing)
    key = newer.uri.split("/", 3)[3]
    s3.objects[("acme-releases", key)] = json.dumps(
        dict(newer.receipt, code_digest="sha256:" + "0" * 64)
    ).encode()
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 1 and "names other code than the folder it is in" in found.reason


def test_a_cosign_that_writes_no_signature_file_writes_nothing(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    monkeypatch.setattr(_signing, "cosign_available", lambda: True)
    monkeypatch.setattr(
        _signing,
        "sign_blob",
        lambda path, key_ref=None: {"exit_code": 0},
    )
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    with pytest.raises(receipts.ReceiptError, match="wrote no bundle"):
        receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    assert _names(s3, "nonprod") == []


def test_an_environment_that_cannot_name_a_folder_is_refused(tmp_path: Path) -> None:
    index = sealed(tmp_path / "rel", environment="../../x")
    with pytest.raises(receipts.ReceiptError, match="names no environment a receipts folder"):
        receipts._verified(str(tmp_path / "rel"), FakeS3(), None)
    assert index["identity"]["environment"] == "../../x"


def test_the_ci_metadata_carries_no_userinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _attestation

    monkeypatch.setattr(
        _attestation,
        "_collect_run_metadata",
        lambda: {"builder": {"id": f"https://bot:{PW}@ci.example.com/job/1"}},
    )
    sealed(tmp_path / "nonprod", environment="nonprod")
    report = _report(tmp_path / "verify.json", tmp_path / "nonprod")
    done = receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report))
    assert done.receipt["ci"] == {"builder": {"id": "https://ci.example.com/job/1"}}


def test_a_report_on_one_expose_records_a_failed_receipt(tmp_path: Path) -> None:
    sealed(tmp_path / "nonprod", environment="nonprod")
    block = release_block(str(tmp_path / "nonprod"), "acme.orders")
    block["expose"] = "orders"
    (tmp_path / "one.json").write_text(
        json.dumps({"summary": PASSING, "release": block}), encoding="utf-8"
    )
    done = receipts.record_receipt(
        str(tmp_path / "nonprod"), report_path=str(tmp_path / "one.json")
    )
    assert done.result == "FAILED" and "checked one expose (orders)" in done.reason


def test_a_target_that_cannot_be_read_writes_nothing(tmp_path: Path, s3: FakeS3) -> None:
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    s3.put("acme-dags", "dags/orders.py", b"dag")
    s3.deny_get.add("dags/orders.py")
    original = s3.head_object

    def denied(**kw: Any) -> Dict[str, Any]:
        from tests.cli._fake_s3 import S3Error

        if kw["Key"] == "dags/orders.py":
            raise S3Error("SlowDown", 503)
        return original(**kw)

    s3.head_object = denied  # type: ignore[method-assign]
    with pytest.raises(
        receipts.ReceiptError, match="cannot be read \\(SlowDown\\); nothing was written"
    ) as raised:
        receipts.record_receipt(
            uris["nonprod"],
            report_path=str(report),
            targets=[f"s3://acme-dags/dags/orders.py#{s3.version_of('dags/orders.py')}"],
            check_targets=True,
            clients=s3,
        )
    assert raised.value.exit_code == 2 and _names(s3, "nonprod") == []


def test_a_bad_sign_key_writes_nothing(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    monkeypatch.setattr(_signing, "cosign_available", lambda: True)
    uris = _published(tmp_path, s3, "nonprod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    with pytest.raises(receipts.ReceiptError, match="--sign-key: not a key path") as raised:
        receipts.record_receipt(
            uris["nonprod"], report_path=str(report), sign=True, key_ref="--foo bar", clients=s3
        )
    assert raised.value.exit_code == 2 and _names(s3, "nonprod") == []

    def failing(path: str, *, key_ref: Any = None) -> Dict[str, Any]:
        return {"exit_code": 1}

    monkeypatch.setattr(_signing, "sign_blob", failing)
    with pytest.raises(receipts.ReceiptError, match="could not sign"):
        receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    assert _names(s3, "nonprod") == []


# ---------------------------------------------------------------------------
# --require-signed
# ---------------------------------------------------------------------------


@pytest.fixture
def cosign(monkeypatch: pytest.MonkeyPatch, s3: FakeS3) -> Dict[str, Any]:
    """A fake cosign: signs by writing the blob's sha256 as its bundle, and verifies a bundle
    when it is that sha256 and the signer is the one ``state["signer"]`` names. The bucket
    locks what it holds (GOVERNANCE, a day), as ``--require-signed`` needs."""
    from fluid_build.cli import _signing

    s3.object_lock, s3.retention = True, ("GOVERNANCE", 1)

    state: Dict[str, Any] = {
        "signer": ("ci@example.com", "https://issuer.example.com"),
        "calls": [],
    }

    def sign(path: str, *, key_ref: Any = None) -> Dict[str, Any]:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest().encode()
        Path(path + ".sigstore.json").write_bytes(digest)
        return {"exit_code": 0, "bundle_path": path + ".sigstore.json"}

    def verify(path: str, **kw: Any) -> Dict[str, Any]:
        state["calls"].append(kw)
        good = (
            Path(kw["bundle_path"]).read_bytes()
            == hashlib.sha256(Path(path).read_bytes()).hexdigest().encode()
        )
        who = (kw.get("certificate_identity"), kw.get("certificate_oidc_issuer"))
        ok = good and (kw.get("key_ref") == "k" or who == state["signer"])
        return {"exit_code": 0 if ok else 1}

    monkeypatch.setattr(_signing, "cosign_available", lambda: True)
    monkeypatch.setattr(_signing, "sign_blob", sign)
    monkeypatch.setattr(_signing, "verify_blob", verify)
    return state


SIGNER = receipts.SignerPolicy(
    certificate_identity="ci@example.com", certificate_oidc_issuer="https://issuer.example.com"
)


@pytest.mark.parametrize("field", ["check", "policy"])
def test_require_signed_refuses_a_statement_of_a_deploy(
    tmp_path: Path,
    s3: FakeS3,
    cosign: Dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """A signed Statement that says it records a deploy (its check, or its policy) never stands
    for a verification, whatever else it says."""
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    real = receipts.statement

    def of_a_deploy(receipt: Any, data: bytes) -> Dict[str, Any]:
        out = real(receipt, data)
        if field == "check":
            out["predicate"][receipts.RECEIPT_EXTENSION]["check"] = "deploy"
        else:
            out["predicate"]["policy"]["uri"] = f"{receipts.RECEIPT_EXTENSION}#deploy-log"
        return out

    monkeypatch.setattr(receipts, "statement", of_a_deploy)
    receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 1 and f"Statement's {field} is not the receipt's" in found.reason


def test_require_signed_passes_a_receipt_signed_by_the_pinned_signer(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any]
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 0, found.reason
    other = receipts.SignerPolicy(
        certificate_identity="someone@example.com",
        certificate_oidc_issuer="https://issuer.example.com",
    )
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=other, clients=s3)
    assert (
        found.exit_code == 1
        and "did not verify the newest receipt's signature for that signer" in found.reason
    )


def test_require_signed_fails_an_unsigned_or_forged_receipt(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any]
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3, now=T0)
    # a newer receipt nobody signed, written by whoever can write the folder
    receipts.record_receipt(
        uris["nonprod"], report_path=str(report), clients=s3, now=T0 + timedelta(hours=1)
    )
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 1 and "the newest receipt is not signed" in found.reason
    assert receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3).exit_code == 0


def test_require_signed_fails_a_receipt_changed_after_it_was_signed(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any]
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    done = receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    key = done.uri.split("/", 3)[3]
    edited = dict(
        done.receipt, targets=[{"uri": "s3://other/k", "version_id": "v", "checked": False}]
    )
    s3.objects[("acme-releases", key)] = json.dumps(edited).encode()
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert (
        found.exit_code == 1 and "signed Statement's receipt is not the receipt's" in found.reason
    )


def test_require_signed_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: Any
) -> None:
    sealed(tmp_path / "prod", environment="prod")
    for extra, problem in (
        (["--require-signed"], "--require-signed needs the signer"),
        (["--require-signed", "--certificate-identity", "x"], "--require-signed needs the signer"),
        (["--require-signed", "--verify-key", "k", "--certificate-identity", "x"], "not both"),
        (["--verify-key", "k"], "name the signer of --require-signed"),
    ):
        code, _ = _run(capsys, "check", str(tmp_path / "prod"), "--env", "nonprod", *extra)
        assert code == 2 and problem in caplog.text


FAILING = {"total": 1, "match": 0, "mismatch": 0, "error": 1}


def test_require_signed_needs_object_lock_with_a_default_retention(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any]
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    assert (
        receipts.check_receipt(
            uris["prod"], environment="nonprod", signer=SIGNER, clients=s3
        ).exit_code
        == 0
    )
    for change in ({"lock_rule": False}, {"object_lock": False}):
        for attr, value in change.items():
            setattr(s3, attr, value)
        found = receipts.check_receipt(
            uris["prod"], environment="nonprod", signer=SIGNER, clients=s3
        )
        assert found.exit_code == 2 and "has no Object Lock with a default retention" in (
            found.reason
        )
        s3.lock_rule, s3.object_lock = True, True


def test_require_signed_on_a_release_on_disk_decides_nothing(
    tmp_path: Path, cosign: Dict[str, Any]
) -> None:
    for env in ("nonprod", "prod"):
        sealed(tmp_path / env, environment=env)
    report = _report(tmp_path / "a.json", tmp_path / "nonprod")
    receipts.record_receipt(str(tmp_path / "nonprod"), report_path=str(report), sign=True)
    found = receipts.check_receipt(str(tmp_path / "prod"), environment="nonprod", signer=SIGNER)
    assert found.exit_code == 2 and "keeps no trace of a deleted receipt" in found.reason


def test_a_rollback_by_deleting_the_newer_failed_receipt_fails_closed(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any]
) -> None:
    """The newer signed FAILED receipt removed with every version (s3:DeleteObjectVersion, once
    its lock ran out): the older PASSED one is older still, its lock ran out first, and a
    receipt without a lock in force gates nothing."""
    uris = _published(tmp_path, s3, "nonprod", "prod")
    good = _report(tmp_path / "a.json", uris["nonprod"], clients=s3)
    bad = _report(tmp_path / "b.json", uris["nonprod"], FAILING, clients=s3)
    first = receipts.record_receipt(
        uris["nonprod"], report_path=str(good), sign=True, clients=s3, now=T0
    )
    done = receipts.record_receipt(
        uris["nonprod"], report_path=str(bad), sign=True, clients=s3, now=T0 + timedelta(hours=1)
    )
    assert (
        receipts.check_receipt(
            uris["prod"], environment="nonprod", signer=SIGNER, clients=s3
        ).exit_code
        == 1
    )
    stem = done.uri.split("/", 3)[3][: -len(".json")]
    for key in [k for (_b, k) in list(s3.objects) if k.startswith(stem)]:
        del s3.objects[("acme-releases", key)]  # every version gone: no version, no marker
    old = first.uri.split("/", 3)[3][: -len(".json")]
    s3.expired |= {k for (_b, k) in s3.objects if k.startswith(old)}
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 1 and "carry no Object Lock in force" in found.reason
    assert "record a fresh nonprod receipt" in found.reason


def test_require_signed_needs_the_statement_locked_too(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any]
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    done = receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    stem = done.uri.split("/", 3)[3][: -len(".json")]
    s3.unlocked.add(f"{stem}.intoto.jsonl.sigstore.json")
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 1 and "intoto.jsonl.sigstore.json first" in found.reason


def test_a_retry_that_finds_its_own_write_is_written(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    """botocore re-sends a put whose response was lost and gets 412: the object holds this
    run's bytes, so the receipt was written."""
    from tests.cli._fake_s3 import S3Error

    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "a.json", uris["nonprod"], clients=s3)
    real = s3.put_object

    def lost_ack(**kw: Any) -> Dict[str, Any]:
        real(**kw)
        if "/receipts/" in kw["Key"]:
            raise S3Error("PreconditionFailed", 412)
        return {}

    monkeypatch.setattr(s3, "put_object", lost_ack)
    done = receipts.record_receipt(uris["nonprod"], report_path=str(report), clients=s3, now=T0)
    assert done.result == "PASSED"
    monkeypatch.setattr(s3, "put_object", real)
    assert receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3).exit_code == 0
    # a 412 over other bytes is still a receipt that exists
    s3.put_object = lambda **kw: (_ for _ in ()).throw(S3Error("PreconditionFailed", 412))  # type: ignore[method-assign]
    s3.objects[
        ("acme-releases", done.uri.split("/", 3)[3].removesuffix(".json") + ".report.json")
    ] = b"x"
    with pytest.raises(receipts.ReceiptError, match="already exists"):
        receipts.record_receipt(uris["nonprod"], report_path=str(report), clients=s3, now=T0)


def test_cosign_stderr_is_named_when_it_does_not_verify(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    monkeypatch.setattr(
        _signing,
        "verify_blob",
        lambda path, **kw: {
            "exit_code": 1,
            "stderr_tail": "x\nError: getting tlog: \x1b[31mtimeout",
        },
    )
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 1 and "transparency log or certificate authority" in found.reason
    assert "a bundle signed by another cosign major" in found.reason
    assert found.reason.endswith(": Error: getting tlog: ?[31mtimeout")


def test_a_listing_whose_pages_do_not_advance_decides_nothing(
    tmp_path: Path, s3: FakeS3, monkeypatch: pytest.MonkeyPatch
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    _record(tmp_path, s3, uris["nonprod"], T0)
    _record(tmp_path, s3, uris["nonprod"], T0 + timedelta(hours=1))

    def stuck(**kw: Any) -> Dict[str, Any]:
        return {"Versions": [], "DeleteMarkers": [], "IsTruncated": True}

    monkeypatch.setattr(s3, "list_object_versions", stuck)
    found = receipts.check_receipt(uris["prod"], environment="nonprod", clients=s3)
    assert found.exit_code == 2 and "cannot be listed to its end" in found.reason


@pytest.mark.parametrize("extension", ["x", [1]])
def test_a_signed_statement_of_another_shape_does_not_pass(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any], extension: Any
) -> None:
    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    done = receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    key = done.uri.split("/", 3)[3].removesuffix(".json") + ".intoto.jsonl"
    statement = json.loads(s3.objects[("acme-releases", key)])
    statement["predicate"][receipts.RECEIPT_EXTENSION] = extension
    body = json.dumps(statement).encode()
    s3.objects[("acme-releases", key)] = body
    s3.objects[("acme-releases", key + ".sigstore.json")] = (
        hashlib.sha256(body).hexdigest().encode()
    )
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 1 and "is not one a receipt is signed as" in found.reason


def test_a_cosign_that_does_not_answer_decides_nothing(
    tmp_path: Path, s3: FakeS3, cosign: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.cli import _signing

    uris = _published(tmp_path, s3, "nonprod", "prod")
    report = _report(tmp_path / "verify.json", uris["nonprod"], clients=s3)
    receipts.record_receipt(uris["nonprod"], report_path=str(report), sign=True, clients=s3)
    monkeypatch.setattr(_signing, "verify_blob", lambda path, **kw: {"exit_code": 124})
    found = receipts.check_receipt(uris["prod"], environment="nonprod", signer=SIGNER, clients=s3)
    assert found.exit_code == 2 and "cosign did not answer" in found.reason
