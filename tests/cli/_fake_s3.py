# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An in-memory S3 for the release publish and verify tests: the calls they make, with the
semantics S3 documents for them (a conditional write refused with 412, a checksum that must
match the body, ``ChecksumMode=ENABLED`` to read it back). No boto3, no network."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fluid_build.cli import _ship_release as release

#: The commit a test release was built from, as a CI names it.
SOURCE = {
    "uri": "git+https://git.example.com/data/orders",
    "digest": {"gitCommit": "c0ffee" * 6 + "c0ff"},
}


class S3Error(Exception):
    """What botocore's ``ClientError`` carries that the code reads."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}


def b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


class FakeS3:
    def __init__(self) -> None:
        self.objects: Dict[Tuple[str, str], bytes] = {}
        self.checksums: Dict[Tuple[str, str], Optional[str]] = {}
        self.writes: List[str] = []  # keys written, in order
        self.calls: List[str] = []  # every method called
        self.deny_put: Set[str] = set()  # keys a write is denied for; "*": every key
        self.deny_get: Set[str] = set()
        self.conflicts = 0  # the next N writes race a delete (409)
        self.versioning: Optional[str] = None
        self.object_lock = False
        self.misreport: Set[str] = set()  # keys whose read-back checksum is wrong
        self.versions = False  # a versioned bucket: GetObject answers a VersionId

    def __call__(self, service: str, region: Optional[str]) -> "FakeS3":
        assert service == "s3"
        return self

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: Any,
        ContentLength: int,
        ChecksumSHA256: str,
        IfNoneMatch: str,
    ) -> Dict[str, Any]:
        self.calls.append("put_object")
        data = Body.read()
        assert IfNoneMatch == "*" and ContentLength == len(data)
        if Key in self.deny_put or "*" in self.deny_put:
            raise S3Error("AccessDenied", 403)
        if self.conflicts:
            self.conflicts -= 1
            raise S3Error("ConditionalRequestConflict", 409)
        if b64(data) != ChecksumSHA256:
            raise S3Error("BadDigest", 400)
        if (Bucket, Key) in self.objects:
            raise S3Error("PreconditionFailed", 412)
        self.put(Bucket, Key, data)
        self.writes.append(Key)
        return {}

    def put(self, bucket: str, key: str, data: bytes, checksum: Optional[str] = "") -> None:
        """An object as another writer left it (``checksum`` None: uploaded without one)."""
        self.objects[(bucket, key)] = data
        self.checksums[(bucket, key)] = b64(data) if checksum == "" else checksum

    def head_object(self, *, Bucket: str, Key: str, ChecksumMode: str) -> Dict[str, Any]:
        self.calls.append("head_object")
        assert ChecksumMode == "ENABLED"
        if (Bucket, Key) not in self.objects:
            raise S3Error("404", 404)
        head: Dict[str, Any] = {"ContentLength": len(self.objects[(Bucket, Key)])}
        checksum = self.checksums[(Bucket, Key)]
        if Key in self.misreport:
            checksum = b64(b"other")
        if checksum is not None:
            head.update(ChecksumSHA256=checksum, ChecksumType="FULL_OBJECT")
        return head

    def get_object(self, *, Bucket: str, Key: str) -> Dict[str, Any]:
        self.calls.append("get_object")
        if Key in self.deny_get:
            raise S3Error("AccessDenied", 403)
        if (Bucket, Key) not in self.objects:
            raise S3Error("NoSuchKey", 404)
        got: Dict[str, Any] = {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        if self.versions:
            got["VersionId"] = hashlib.sha256(Key.encode()).hexdigest()[:32]
        return got

    def get_bucket_versioning(self, *, Bucket: str) -> Dict[str, Any]:
        self.calls.append("get_bucket_versioning")
        return {"Status": self.versioning} if self.versioning else {}

    def get_object_lock_configuration(self, *, Bucket: str) -> Dict[str, Any]:
        self.calls.append("get_object_lock_configuration")
        if not self.object_lock:
            raise S3Error("ObjectLockConfigurationNotFoundError", 404)
        return {"ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"}}


def sealed(
    directory: Path,
    files: Optional[Dict[str, bytes]] = None,
    *,
    product: str = "acme.orders",
    environment: str = "dev",
    run_id: str = "run-1",
) -> Dict[str, Any]:
    """A sealed ``fluid-release/1`` directory, as ``_ship_release.publish`` writes one."""
    files = {"bundle.tgz": b"bundle", "reports/ship-report.json": b"{}"} if files is None else files
    directory.mkdir(parents=True)
    for rel, data in files.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(data)
    identity: Dict[str, Any] = {
        "product_id": product,
        "environment": environment,
        "fluid_version": "0.7.7",
        "source": SOURCE,
    }
    index = {
        "schema": release.RELEASE_SCHEMA,
        "identity": identity,
        "identity_digest": release.identity_digest(identity),
        "code_digest": release.code_digest(identity),
        "gates": [],
        "files": release._files(directory),
        "provenance": {"run_id": run_id, "finished_at": "2026-11-26T10:00:00Z"},
    }
    (directory / "release.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return index


def hex_of(index: Dict[str, Any], key: str = "code_digest") -> str:
    return str(index[key]).split(":", 1)[1]
