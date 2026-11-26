# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --release-to s3://bucket/prefix/``: publish the sealed release
to S3, write-once.

Once the run has sealed ``DIR``, its files go to ``<prefix>/<product>/<code hex>/<env>/``: the
code digest (``release.json``'s ``code_digest``, of the commit, the external files and the
generator) is the same for every environment sealed from one commit and one lock, so each
environment's release sits beside the others of the same code, as Kargo keeps one Freight across
its Stages. A release that names no source commit has no code digest and is not published. Each
file is one single-part ``PutObject`` with ``ChecksumSHA256`` (S3 refuses bytes that are not that digest,
and a single-part upload keeps a full-object SHA-256) and ``If-None-Match: *`` (S3 refuses to
replace an object). ``release.json`` goes last: a reader that finds it finds every file it lists.

A key that already holds an object (HTTP 412) is published when its SHA-256, read back with
``HeadObject`` and ``ChecksumMode=ENABLED``, is ours, so a re-run of the same release finishes
or repeats the upload; any other object refuses the run. ``release.json`` is checked first: when
another run sealed this identity (another run id, other reports) and its release verifies intact
(``fluid_build.api.verify_release``, which ties the bundle, external files, artifacts and plan to
``identity``), that release stands and nothing is written; one that does not verify refuses. Every object written is read back the same way. A write that raced a delete of
its key (HTTP 409) is tried again, as S3 allows.

The rule is rclone's ``--immutable`` (a changed file is refused, an identical one is not),
compared by SHA-256; the checksum and the conditional write are the S3 API's own
(docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html). ``If-None-Match`` on
PutObject needs botocore 1.35.2, the ``[aws]`` extra's floor.

boto3 is imported only when a release is published; module scope is standard library
(``fluid ship`` imports this on ``--help``).
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: The largest object one single-part PutObject takes.
MAX_OBJECT_BYTES = 5 * 1024**3
#: ``--release-to-require`` checks of the bucket, before anything is written.
REQUIREMENTS = ("versioning", "object-lock")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_PREFIX_SEGMENT = re.compile(r"[A-Za-z0-9._=+-]{1,255}")
#: A product id (the contract's ``$defs.identifier``: a leading ``_`` too) or an environment.
_KEY_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._=+-]{0,254}")
_HEX = re.compile(r"sha256:([0-9a-f]{64})")
#: Tries of one write that raced a delete of its key (HTTP 409).
_CONFLICT_TRIES = 3

ClientFactory = Callable[[str, Optional[str]], Any]


class PublishError(RuntimeError):
    """The release cannot be published to the target, or not all of it was."""


@dataclass(frozen=True)
class Target:
    """``s3://bucket/prefix/``: ``prefix`` is "" or ends with ``/``."""

    bucket: str
    prefix: str

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


@dataclass(frozen=True)
class Published:
    """Where the release is, and how many objects this run wrote and found already there."""

    uri: str
    written: int
    already: int
    #: Another run sealed this identity and published it, intact, before this one: nothing was
    #: written, and the published release (other run times, other reports) stands.
    by_another_run: bool = False


def parse_target(value: str) -> Target:
    """``--release-to``, checked: ``s3://bucket/`` or ``s3://bucket/prefix/``.

    Raises:
        PublishError: anything else.
    """
    text = (value or "").strip()
    if not text.startswith("s3://"):
        raise PublishError(f"--release-to {text!r}: expected s3://bucket/prefix/")
    bucket, _, prefix = text[len("s3://") :].partition("/")
    if not _BUCKET.fullmatch(bucket) or ".." in bucket:
        raise PublishError(f"--release-to {text!r}: {bucket!r} is not an S3 bucket name")
    parts = [p for p in prefix.split("/") if p]
    if prefix.startswith("/") or "//" in prefix or len(prefix) > 512:
        raise PublishError(f"--release-to {text!r}: the prefix is not a/b/c")
    for part in parts:
        if part in (".", "..") or not _PREFIX_SEGMENT.fullmatch(part):
            raise PublishError(
                f"--release-to {text!r}: {part!r} has characters a release key does not take "
                "(letters, digits and ._=+- only in each segment)"
            )
    return Target(bucket, "".join(f"{p}/" for p in parts))


def refusal(target: str, release_dir: Optional[Path], require: Sequence[str]) -> Optional[str]:
    """Why ``--release-to`` cannot run, before any stage: no ``--release-dir``, a target that is
    not one, or no boto3. ``None`` when it can."""
    import importlib.util

    if release_dir is None:
        if target:
            return f"--release-to {target} publishes a sealed release: add --release-dir DIR"
        if require:
            return "--release-to-require checks the bucket of --release-to: add --release-to"
        return None
    if not target:
        if require:
            return "--release-to-require checks the bucket of --release-to: add --release-to"
        return None
    try:
        parse_target(target)
    except PublishError as exc:
        return str(exc)
    if importlib.util.find_spec("boto3") is None:
        return f"--release-to {target} needs boto3: pip install 'data-product-forge[aws]'"
    from . import _ship_release

    if _ship_release.source_identity(os.environ) is None:
        return (
            f"--release-to {target} files the release under the digest of its code, which needs "
            "the commit the CI built, and no CI variable names one (GITHUB_SHA, CI_COMMIT_SHA, "
            "BUILD_SOURCEVERSION, ...)"
        )
    return None


def _default_clients(service: str, region: Optional[str]) -> Any:
    """boto3's client, bounded: a bucket that does not answer fails in seconds."""
    from botocore.config import Config

    from fluid_build.util import s3

    config = Config(
        connect_timeout=10, read_timeout=120, retries={"mode": "standard", "max_attempts": 3}
    )
    return s3.default_clients(service, region, config=config)


def publish_release(
    release_dir: Path,
    target: str,
    *,
    require: Sequence[str] = (),
    clients: Optional[ClientFactory] = None,
    region: Optional[str] = None,
) -> Published:
    """Publish the sealed release in ``release_dir`` under ``target``, write-once.

    Raises:
        PublishError: the local release is not intact, an object at a key holds other bytes,
            a write or a read-back failed, or a ``require`` check of the bucket failed. The
            message names what was written.
    """
    from fluid_build.api import release as api_release
    from fluid_build.util import s3

    where = parse_target(target)
    checked = api_release.verify_release(release_dir)
    if not checked.intact:
        raise PublishError(
            f"the release in {release_dir} is not intact, so it is not published: "
            + "; ".join(checked.problems)
        )
    files, product, environment, identity_hex, code_hex = _local_files(release_dir)
    for segment, what in ((product, "product id"), (environment, "environment")):
        if not _KEY_SEGMENT.fullmatch(segment):
            raise PublishError(
                f"the release's {what} {segment!r} cannot name an S3 key (letters, digits and "
                "._=+-, starting with a letter, digit or _, at most 255)"
            )
    if code_hex is None:
        raise PublishError(
            f"the release in {release_dir} names no source commit, so it has no code digest to "
            "publish under: seal it in a CI run that names its commit"
        )
    prefix = f"{where.prefix}{product}/{code_hex}/{environment}/"
    uri = f"s3://{where.bucket}/{prefix}"
    try:
        client = (clients or _default_clients)("s3", region)
    except s3.S3Unavailable:
        raise PublishError(
            f"publishing to {uri} needs boto3: pip install 'data-product-forge[aws]'"
        )
    except Exception as exc:  # noqa: BLE001 - botocore's errors: no client, nothing written
        raise PublishError(f"no S3 client could be made for {uri}: {type(exc).__name__}") from None
    upload = _Upload(client, where.bucket, prefix, uri)
    for check in require:
        upload.require(check)
    if upload.check_index(files[-1], identity_hex):
        return Published(uri=uri, written=0, already=0, by_another_run=True)
    for item in files:
        upload.put(item)
    return Published(uri=uri, written=upload.written, already=upload.already)


@dataclass(frozen=True)
class _File:
    rel: str
    path: Path
    size: int
    sha256: str  # hex


def _local_files(release_dir: Path) -> Tuple[List[_File], str, str, str, Optional[str]]:
    """The release's files in upload order (``release.json`` last), its product id, environment,
    identity hex and code hex (``None`` without a source commit). The release was verified just before: every file is the bytes its index
    names."""
    import hashlib
    import json

    index_path = release_dir / "release.json"
    data = _read_regular(index_path)
    index: Dict[str, Any] = json.loads(data)
    identity = index["identity"]
    match = _HEX.fullmatch(str(index["identity_digest"]))
    if match is None:
        raise PublishError("release.json names no sha256 identity_digest")
    files: List[_File] = []
    for rel in sorted(index["files"]):
        path = release_dir / rel
        files.append(_File(rel, path, _size(path), str(index["files"][rel]).split(":", 1)[1]))
    files.append(_File("release.json", index_path, len(data), hashlib.sha256(data).hexdigest()))
    big = [f.rel for f in files if f.size > MAX_OBJECT_BYTES]
    if big:
        raise PublishError(
            f"{', '.join(big)} is larger than 5 GiB, the most one single-part upload takes "
            "(a multipart upload keeps no full-object SHA-256), so the release is not published"
        )
    return (
        files,
        str(identity.get("product_id") or ""),
        str(identity.get("environment") or ""),
        match.group(1),
        code.group(1) if (code := _HEX.fullmatch(str(index.get("code_digest")))) else None,
    )


def _open(path: Path) -> int:
    """``path`` opened without following a link, checked to be a regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        raise PublishError(f"{path} cannot be read: {exc.strerror or exc}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise PublishError(f"{path} is not a regular file")
    return fd


def _size(path: Path) -> int:
    fd = _open(path)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def _read_regular(path: Path) -> bytes:
    fd = _open(path)
    try:
        with os.fdopen(fd, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise PublishError(f"{path} cannot be read: {exc.strerror or exc}") from None


class _Upload:
    """The writes of one release, counted."""

    def __init__(self, client: Any, bucket: str, prefix: str, uri: str) -> None:
        self.client, self.bucket, self.prefix, self.uri = client, bucket, prefix, uri
        self.written = 0
        self.already = 0
        self.index_there = False  # release.json was already published before this run

    def _done(self) -> str:
        """What this run left in S3, for a refusal's message."""
        if not (self.written or self.already):
            return "nothing was written"
        return (
            f"{self.written} object(s) were written and {self.already} were already there; "
            "release.json is written last, so no reader takes this release as published"
        )

    def _fail(self, what: str) -> PublishError:
        return PublishError(f"{what}; {self._done()}")

    def require(self, check: str) -> None:
        """A ``--release-to-require`` check of the bucket."""
        from fluid_build.util import s3

        try:
            if check == "versioning":
                status = self.client.get_bucket_versioning(Bucket=self.bucket).get("Status")
                ok = status == "Enabled"
            else:
                config = self.client.get_object_lock_configuration(Bucket=self.bucket)
                ok = config.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled") == "Enabled"
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            code = s3.error_code(exc)
            if code != "ObjectLockConfigurationNotFoundError":
                raise PublishError(
                    f"--release-to-require {check}: the bucket {self.bucket} cannot be read "
                    f"({code or type(exc).__name__}); nothing was written"
                ) from None
            ok = False
        if not ok:
            label = "versioning" if check == "versioning" else "Object Lock"
            raise PublishError(
                f"--release-to-require {check}: the bucket {self.bucket} does not have {label} "
                "enabled; nothing was written"
            )

    def _head(self, key: str, *, denied_is_missing: bool = False) -> Optional[Dict[str, Any]]:
        """The object's metadata with its checksum, or None when there is none.
        ``denied_is_missing``: without ``s3:ListBucket``, S3 answers a missing key with 403, so
        a check before any write takes a refusal as no object (the write is still conditional)."""
        from fluid_build.util import s3

        try:
            head: Dict[str, Any] = self.client.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
            return head
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            code = s3.error_code(exc)
            if s3.is_missing_code(code) or (denied_is_missing and s3.is_denied_code(code)):
                return None
            raise self._fail(
                f"s3://{self.bucket}/{key} cannot be read back ({code or type(exc).__name__})"
            ) from None

    @staticmethod
    def _same(head: Dict[str, Any], item: _File) -> bool:
        from fluid_build.util import s3

        return (
            head.get("ChecksumSHA256") == s3.sha256_b64(item.sha256)
            and head.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT"
            and head.get("ContentLength", item.size) == item.size
        )

    def check_index(self, index: _File, identity_hex: str) -> bool:
        """Before any write: False when this release's ``release.json`` is not there or is ours;
        True when another run published this identity and the release there verifies intact
        (``identity`` pins the files that matter, so it holds what this run sealed). Raises
        otherwise: a release that is not intact, or whose identity is another one, is never
        taken as published."""
        from fluid_build.api import release as api_release

        head = self._head(self.prefix + index.rel, denied_is_missing=True)
        self.index_there = head is not None
        if head is None or self._same(head, index):
            return False
        there = api_release.verify_release(self.uri, clients=lambda _service, _region: self.client)
        if there.intact and there.identity_digest == f"sha256:{identity_hex}":
            return True
        if there.intact:
            raise PublishError(
                f"{self.uri} already holds the release of another identity "
                f"({there.identity_digest}) for this code and environment, and this run sealed "
                f"sha256:{identity_hex}: the bundle, the artifacts or the plan differ. A "
                "published release never changes; nothing was written"
            )
        why = "; ".join(there.problems)
        if there.status == api_release.UNDECIDED:
            raise PublishError(
                f"{self.uri}release.json already holds another release, and it cannot be checked "
                f"({why}); nothing was written. Run again, or check it with fluid runs "
                "verify-release"
            )
        raise PublishError(
            f"{self.uri}release.json already holds another release that does not verify as this "
            f"one ({why}). A published release never changes; nothing was written"
        )

    def put(self, item: _File) -> None:
        """Write one object, or find it already there; then read it back."""
        key = self.prefix + item.rel
        for attempt in range(1, _CONFLICT_TRIES + 1):
            try:
                self._write(item, key)
                break
            except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
                if self._after_refused_write(exc, item, key, last=attempt == _CONFLICT_TRIES):
                    return  # already there, with our bytes
        head = self._head(key)
        if head is None or not self._same(head, item):
            raise self._fail(
                f"s3://{self.bucket}/{key} does not read back with the sha256 the release names"
            )
        self.written += 1

    def _write(self, item: _File, key: str) -> None:
        from fluid_build.util import s3

        with os.fdopen(_open(item.path), "rb") as body:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentLength=item.size,
                ChecksumSHA256=s3.sha256_b64(item.sha256),
                IfNoneMatch="*",
            )

    def _after_refused_write(self, exc: Exception, item: _File, key: str, *, last: bool) -> bool:
        """What a refused write means: True when the object is already there with our bytes,
        False to try again (a write that raced a delete, HTTP 409, while tries are left).

        Raises:
            PublishError: anything else.
        """
        from fluid_build.util import s3

        shown = f"s3://{self.bucket}/{key}"
        code, status = s3.error_code(exc), s3.http_status(exc)
        if isinstance(exc, PublishError):
            raise exc
        if code in s3.PRECONDITION_CODES or status == 412:
            head = self._head(key)
            if head is not None and self._same(head, item):
                self.already += 1
                return True
            raise self._fail(f"{shown} already holds other bytes; {self._why_other_bytes()}")
        if (code in s3.CONFLICT_CODES or status == 409) and not last:
            return False  # S3 says the write may be tried again
        if s3.is_denied_code(code):
            raise self._fail(f"writing {shown} is denied (AccessDenied)")
        if code in ("BadDigest", "InvalidDigest", "XAmzContentSHA256Mismatch"):
            raise self._fail(
                f"{item.path} changed while it was uploaded: S3 refused bytes that are not the "
                "sha256 the release names"
            )
        raise self._fail(f"{shown} cannot be written ({code or type(exc).__name__})")

    def _why_other_bytes(self) -> str:
        if self.index_there:
            return "a published release never changes"
        # An earlier run stopped before its release.json.
        return (
            f"{self.uri} holds no release.json, so no reader takes it as a release: an earlier "
            "run stopped partway. Delete the objects under it and run again, or publish the "
            "directory that run sealed"
        )
