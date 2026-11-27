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
        #: Object Lock's default retention, ``(mode, days)``: each object then carries it.
        self.retention: Optional[Tuple[str, int]] = None
        self.lock_rule = True  # with object_lock: a Rule.DefaultRetention is set
        self.unlocked: Set[str] = set()  # keys that read back with no lock fields
        self.expired: Set[str] = set()  # keys whose retention ran out yesterday
        self.overwritten: Set[str] = set()  # keys with a second version (listed)
        self.deleted: Set[str] = set()  # keys hidden by a delete marker (listed)
        #: Lock fields as some other store answers them: replace the mode, the retain-until
        #: date, or the default rule's period (``{"Days": True}``).
        self.head_lock: Dict[str, Any] = {}
        self.rule_period: Optional[Dict[str, Any]] = None

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
        data = Body if isinstance(Body, bytes) else Body.read()
        assert IfNoneMatch == "*" and ContentLength == len(data)
        if Key in self.deny_put or "*" in self.deny_put:
            raise S3Error("AccessDenied", 403)
        if self.conflicts:
            self.conflicts -= 1
            raise S3Error("ConditionalRequestConflict", 409)
        if b64(data) != ChecksumSHA256:
            raise S3Error("BadDigest", 400)
        # A delete marker as the current version counts as no object (S3's conditional-writes
        # page): the write goes through, and the marker is no longer current.
        if (Bucket, Key) in self.objects:
            raise S3Error("PreconditionFailed", 412)
        self.deleted.discard(Key)
        self.put(Bucket, Key, data)
        self.writes.append(Key)
        return {}

    def delete_object(self, *, Bucket: str, Key: str) -> Dict[str, Any]:
        """Delete the key. On a versioned bucket (``versions``) a delete marker becomes the
        current version (listed by ``list_object_versions``); otherwise the key is gone."""
        self.calls.append("delete_object")
        self.objects.pop((Bucket, Key), None)
        self.checksums.pop((Bucket, Key), None)
        if self.versions:
            self.deleted.add(Key)
        return {}

    def put(self, bucket: str, key: str, data: bytes, checksum: Optional[str] = "") -> None:
        """An object as another writer left it (``checksum`` None: uploaded without one)."""
        self.objects[(bucket, key)] = data
        self.checksums[(bucket, key)] = b64(data) if checksum == "" else checksum

    def head_object(
        self, *, Bucket: str, Key: str, ChecksumMode: str, VersionId: str = ""
    ) -> Dict[str, Any]:
        self.calls.append("head_object")
        assert ChecksumMode == "ENABLED"
        if (Bucket, Key) not in self.objects:
            raise S3Error("404", 404)
        if VersionId and VersionId != self.version_of(Key):
            raise S3Error("NoSuchVersion", 404)
        head: Dict[str, Any] = {"ContentLength": len(self.objects[(Bucket, Key)])}
        checksum = self.checksums[(Bucket, Key)]
        if Key in self.misreport:
            checksum = b64(b"other")
        if checksum is not None:
            head.update(ChecksumSHA256=checksum, ChecksumType="FULL_OBJECT")
        if self.retention and Key not in self.unlocked:
            import datetime as dt

            days = -1 if Key in self.expired else self.retention[1]
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
            head.update(ObjectLockMode=self.retention[0], ObjectLockRetainUntilDate=until)
            head.update(self.head_lock)
        return head

    def get_object(self, *, Bucket: str, Key: str, VersionId: str = "") -> Dict[str, Any]:
        self.calls.append("get_object")
        if Key in self.deny_get:
            raise S3Error("AccessDenied", 403)
        if (Bucket, Key) not in self.objects:
            raise S3Error("NoSuchKey", 404)
        got: Dict[str, Any] = {"Body": io.BytesIO(self.objects[(Bucket, Key)])}
        if self.versions:
            got["VersionId"] = self.version_of(Key)
        return got

    @staticmethod
    def version_of(key: str) -> str:
        """The VersionId the fake gives ``key``'s one version."""
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    def list_object_versions(
        self, *, Bucket: str, Prefix: str, KeyMarker: str = "", VersionIdMarker: str = ""
    ) -> Dict[str, Any]:
        """Each key's one version (plus ``overwritten`` keys' older one, and ``deleted`` keys'
        delete marker), two keys a page, so a listing that does not follow the markers misses
        some."""
        self.calls.append("list_object_versions")
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        keys += sorted(k for k in self.deleted if k.startswith(Prefix))
        start = keys.index(KeyMarker) + 1 if KeyMarker else 0
        page = keys[start : start + 2]
        out: Dict[str, Any] = {"Versions": [], "DeleteMarkers": []}
        for key in page:
            if key in self.deleted:
                out["DeleteMarkers"].append({"Key": key, "VersionId": "marker", "IsLatest": True})
                continue
            out["Versions"].append({"Key": key, "VersionId": self.version_of(key)})
            if key in self.overwritten:
                out["Versions"].append({"Key": key, "VersionId": "older"})
        if start + 2 < len(keys):
            out.update(IsTruncated=True, NextKeyMarker=page[-1], NextVersionIdMarker="x")
        return out

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str, ContinuationToken: str = ""
    ) -> Dict[str, Any]:
        """Two keys a page, so a listing that does not follow the token misses some."""
        self.calls.append("list_objects_v2")
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + 2]
        out: Dict[str, Any] = {"Contents": [{"Key": k} for k in page]}
        if start + 2 < len(keys):
            out.update(IsTruncated=True, NextContinuationToken=str(start + 2))
        return out

    def get_bucket_versioning(self, *, Bucket: str) -> Dict[str, Any]:
        self.calls.append("get_bucket_versioning")
        return {"Status": self.versioning} if self.versioning else {}

    def get_object_lock_configuration(self, *, Bucket: str) -> Dict[str, Any]:
        self.calls.append("get_object_lock_configuration")
        if not self.object_lock:
            raise S3Error("ObjectLockConfigurationNotFoundError", 404)
        config: Dict[str, Any] = {"ObjectLockEnabled": "Enabled"}
        if self.lock_rule:
            mode, days = self.retention or ("GOVERNANCE", 1)
            period = {"Days": days} if self.rule_period is None else self.rule_period
            config["Rule"] = {"DefaultRetention": {"Mode": mode, **period}}
        return {"ObjectLockConfiguration": config}


def sealed(
    directory: Path,
    files: Optional[Dict[str, bytes]] = None,
    *,
    product: str = "acme.orders",
    environment: str = "dev",
    run_id: str = "run-1",
) -> Dict[str, Any]:
    """A sealed ``fluid-release/1`` directory, as ``_ship_release.publish`` writes one: its
    identity pins the bundle and the artifacts MANIFEST (here one that lists no file), as
    ``build_identity`` always does."""
    from fluid_build.forge.core.bundle import merkle_root

    files = {"reports/ship-report.json": b"{}"} if files is None else dict(files)
    files.setdefault("bundle.tgz", b"bundle")
    manifest = {"digest": merkle_root({}), "files": {}}
    files.setdefault("artifacts/MANIFEST.json", json.dumps(manifest).encode())
    directory.mkdir(parents=True)
    for rel, data in files.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_bytes(data)
    identity: Dict[str, Any] = {
        "product_id": product,
        "environment": environment,
        "fluid_version": "0.7.7",
        "source": SOURCE,
        "bundle": {"sha256": "sha256:" + hashlib.sha256(files["bundle.tgz"]).hexdigest()},
        "artifacts": {"manifest_digest": manifest["digest"]},
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
