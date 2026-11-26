# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Verify a sealed release (``fluid-release/1``), on disk or in S3, without changing it.

``fluid ship --release-dir DIR`` seals a release: ``release.json`` names the release's
``identity``, its ``identity_digest`` and the sha256 of every other file. ``fluid ship
--release-to s3://bucket/prefix/`` publishes the same bytes under
``<prefix>/<product>/<environment>/<identity hex>/``. :func:`verify_release` reads one back and
says whether it is the release it claims to be:

- ``intact``: the index parses as a ``fluid-release/1`` record, its ``identity_digest`` is the
  digest of its ``identity``, an S3 key that ends in an identity's hex is this identity's, and
  every file the index lists is there with the sha256 it names;
- ``not-intact``: one of those is false (a missing index, a changed or missing file);
- ``undecided``: the release could not be read (access denied, a network error, no boto3).

The check proves the bytes match the index; it does not say who wrote the index. A file the
index does not list is not part of the release and is not read. Nothing is written, and an S3
release is read with ``GetObject`` only.

Standard library at module scope; boto3 (the ``[aws]`` extra) is imported only to read an
``s3://`` source, and a test passes ``clients`` instead.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

__all__ = [
    "INTACT",
    "NOT_INTACT",
    "UNDECIDED",
    "ReleaseVerification",
    "identity_intact",
    "verify_release",
]

INTACT = "intact"
NOT_INTACT = "not-intact"
UNDECIDED = "undecided"

#: Bytes of one ``release.json`` read, as ``fluid runs status`` reads it.
MAX_INDEX_BYTES = 1_048_576
#: Files one index may list.
MAX_FILES = 10_000
_CHUNK = 1 << 20
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
_SEGMENT = re.compile(r"[A-Za-z0-9._=+-]{1,255}")
_EXIT = {INTACT: 0, NOT_INTACT: 1, UNDECIDED: 2}


@dataclass(frozen=True)
class ReleaseVerification:
    """What :func:`verify_release` found. ``problems`` says why a release is not intact or was
    not decided, one line each, in the order found."""

    source: str
    status: str
    identity_digest: Optional[str] = None
    product_id: Optional[str] = None
    environment: Optional[str] = None
    files_checked: int = 0
    problems: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def intact(self) -> bool:
        return self.status == INTACT

    @property
    def exit_code(self) -> int:
        """0 intact, 1 not intact, 2 cannot decide: the check commands' convention."""
        return _EXIT[self.status]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "status": self.status,
            "identity_digest": self.identity_digest,
            "product_id": self.product_id,
            "environment": self.environment,
            "files_checked": self.files_checked,
            "problems": list(self.problems),
        }


class _NotIntact(Exception):
    """The release is not the one its index says."""


class _Undecided(Exception):
    """The release could not be read."""


def identity_intact(index: Mapping[str, Any]) -> bool:
    """Whether ``index`` (a parsed ``release.json``) has an ``identity_digest`` that is the
    digest of its ``identity``. Says nothing about the files: :func:`verify_release` does."""
    from fluid_build.cli import _ship_release as ship_release

    identity, digest = index.get("identity"), index.get("identity_digest")
    if not isinstance(identity, Mapping) or not isinstance(digest, str):
        return False
    try:
        return ship_release.identity_digest(identity) == digest
    except (TypeError, ValueError, RecursionError):
        return False


def verify_release(
    source: Union[str, "os.PathLike[str]"],
    *,
    clients: Optional[Callable[[str, Optional[str]], Any]] = None,
    region: Optional[str] = None,
) -> ReleaseVerification:
    """Check the release at ``source``: a directory, or ``s3://bucket/key/`` (the prefix that
    holds its ``release.json``). Read-only. ``clients``: ``(service, region) -> client``, for an
    ``s3://`` source (default: boto3's, from the default credential chain); ``region`` None is
    the profile's."""
    text = os.fspath(source)
    shown = _clean(text, 300)
    try:
        reader: _Reader = (
            _S3Reader(text, clients, region) if text.startswith("s3://") else _DirReader(text)
        )
        index = _parse_index(reader.index())
        return _check(shown, reader, index)
    except _NotIntact as exc:
        return ReleaseVerification(shown, NOT_INTACT, problems=(str(exc),))
    except _Undecided as exc:
        return ReleaseVerification(shown, UNDECIDED, problems=(str(exc),))


def _check(shown: str, reader: "_Reader", index: Mapping[str, Any]) -> ReleaseVerification:
    identity = index["identity"]
    digest = index["identity_digest"]
    product = identity.get("product_id")
    environment = identity.get("environment")
    shown_digest = _clean(digest, 80)
    shown_product = _clean(product, 120) if isinstance(product, str) else None
    shown_env = _clean(environment, 40) if isinstance(environment, str) else None

    def result(status: str, checked: int, problems: List[str]) -> ReleaseVerification:
        return ReleaseVerification(
            source=shown,
            status=status,
            identity_digest=shown_digest,
            product_id=shown_product,
            environment=shown_env,
            files_checked=checked,
            problems=tuple(problems),
        )

    problems: List[str] = []
    if not identity_intact(index):
        problems.append("its identity_digest is not the digest of its identity")
    named = reader.identity_hex()
    match = _DIGEST.fullmatch(digest)
    if named is not None and (match is None or match.group(1) != named):
        problems.append(f"it sits under {named}, the key of another identity")
    files = index["files"]
    checked = 0
    undecided: List[str] = []
    for rel in sorted(files):
        expected = files[rel]
        problem = _path_problem(rel) or (
            None if isinstance(expected, str) and _DIGEST.fullmatch(expected) else "no sha256"
        )
        if problem:
            problems.append(f"{_clean(rel)}: the index lists it with {problem}")
            continue
        try:
            actual = reader.sha256(rel)
        except _NotIntact as exc:
            problems.append(f"{_clean(rel)}: {exc}")
            continue
        except _Undecided as exc:
            undecided.append(f"{_clean(rel)}: {exc}")
            continue
        checked += 1
        if "sha256:" + actual != expected:
            problems.append(f"{_clean(rel)}: its bytes are not the sha256 the index names")
    if problems:
        return result(NOT_INTACT, checked, problems)
    if undecided:
        return result(UNDECIDED, checked, undecided)
    return result(INTACT, checked, [])


def _parse_index(data: bytes) -> Dict[str, Any]:
    from fluid_build.cli import _ship_release as ship_release

    try:
        index = json.loads(data)
    except (ValueError, RecursionError):
        raise _NotIntact(f"{ship_release.RELEASE_INDEX} is not JSON") from None
    if not isinstance(index, dict) or index.get("schema") != ship_release.RELEASE_SCHEMA:
        raise _NotIntact(
            f"{ship_release.RELEASE_INDEX} is not a {ship_release.RELEASE_SCHEMA} record"
        )
    identity, digest, files = (index.get(k) for k in ("identity", "identity_digest", "files"))
    if not isinstance(identity, dict) or not isinstance(digest, str):
        raise _NotIntact(f"{ship_release.RELEASE_INDEX} has no identity and identity_digest")
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise _NotIntact(
            f"{ship_release.RELEASE_INDEX} has no files map of at most {MAX_FILES:,} entries"
        )
    return index


def _path_problem(rel: Any) -> Optional[str]:
    """Why ``rel`` is no path inside a release, or None."""
    if not isinstance(rel, str) or not rel or len(rel) > 1024 or "\\" in rel or "//" in rel:
        return "a path that is not one"
    parts = PurePosixPath(rel).parts
    if rel.startswith("/") or not parts or PurePosixPath(rel).as_posix() != rel:
        return "a path that is not one"
    if any(p in (".", "..") or not _SEGMENT.fullmatch(p) for p in parts):
        return "a path that leads out of the release or has characters it never writes"
    if rel == "release.json":
        return "the index's own name"
    return None


def _clean(value: Any, limit: int = 200) -> str:
    """Text from a record, printable and cut, so a record cannot rewrite the terminal."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


class _Reader:
    def index(self) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def sha256(self, rel: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def identity_hex(self) -> Optional[str]:
        return None


class _DirReader(_Reader):
    """A release directory. No link is followed: a file is opened with ``O_NOFOLLOW`` and must
    resolve to its own path (no linked directory on the way), and is checked on the open
    handle, so a FIFO or a device never blocks."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        try:
            mode = os.stat(self.root).st_mode
        except FileNotFoundError:
            raise _NotIntact(f"{_clean(root)} does not exist") from None
        except OSError as exc:
            raise _Undecided(f"{_clean(root)} cannot be read: {exc.strerror or exc}") from None
        if not stat.S_ISDIR(mode):
            raise _NotIntact(f"{_clean(root)} is not a directory")
        self.real = self.root.resolve()

    def _open(self, rel: str) -> int:
        path = self.root / rel
        try:
            if path.resolve().relative_to(self.real).as_posix() != rel:
                raise _NotIntact("it is reached through a link")
        except ValueError:
            raise _NotIntact("it is reached through a link") from None
        except OSError as exc:  # a link loop
            raise _NotIntact(f"it cannot be opened: {exc.strerror or exc}") from None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            raise _NotIntact("it is missing") from None
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # O_NOFOLLOW met a link
                raise _NotIntact("it is a link") from None
            raise _Undecided(f"it cannot be read: {exc.strerror or exc}") from None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise _NotIntact("it is not a regular file")
        return fd

    def index(self) -> bytes:
        try:
            fd = self._open("release.json")
        except _NotIntact as exc:
            raise _NotIntact(f"release.json: {exc}: no release is sealed here") from None
        except _Undecided as exc:
            raise _Undecided(f"release.json: {exc}") from None
        try:
            data = _read_at_most(fd, MAX_INDEX_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_INDEX_BYTES:
            raise _NotIntact(f"release.json is longer than {MAX_INDEX_BYTES:,} bytes")
        return data

    def sha256(self, rel: str) -> str:
        fd = self._open(rel)
        digest = hashlib.sha256()
        try:
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
        except OSError as exc:
            raise _Undecided(f"it cannot be read: {exc.strerror or exc}") from None
        finally:
            os.close(fd)


def _read_at_most(fd: int, limit: int) -> bytes:
    data = b""
    try:
        while len(data) < limit:
            chunk = os.read(fd, limit - len(data))
            if not chunk:
                break
            data += chunk
    except OSError as exc:
        raise _Undecided(f"release.json cannot be read: {exc.strerror or exc}") from None
    return data


class _S3Reader(_Reader):
    """A release under an S3 prefix, read with ``GetObject`` only."""

    def __init__(
        self,
        uri: str,
        clients: Optional[Callable[[str, Optional[str]], Any]],
        region: Optional[str],
    ) -> None:
        from fluid_build.util import s3

        bucket, key = s3.split_uri(uri)
        if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise _Undecided(f"{_clean(uri)} is not s3://bucket/prefix/")
        self.uri = f"s3://{bucket}/{key.rstrip('/')}/" if key.strip("/") else f"s3://{bucket}/"
        self.bucket = bucket
        self.prefix = key.rstrip("/") + "/" if key.strip("/") else ""
        try:
            self.client = (clients or s3.default_clients)("s3", region)
        except s3.S3Unavailable:
            raise _Undecided(
                "reading an s3:// release needs boto3: pip install 'data-product-forge[aws]'"
            ) from None
        except Exception as exc:  # noqa: BLE001 - botocore's errors: no client, no decision
            raise _Undecided(f"no S3 client could be made: {type(exc).__name__}") from None

    def identity_hex(self) -> Optional[str]:
        last = self.prefix.rstrip("/").rsplit("/", 1)[-1]
        return last if re.fullmatch(r"[0-9a-f]{64}", last) else None

    def _body(self, rel: str) -> Any:
        from fluid_build.util import s3

        try:
            return self.client.get_object(Bucket=self.bucket, Key=self.prefix + rel)["Body"]
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            code = s3.error_code(exc)
            if s3.is_missing_code(code):
                raise _NotIntact("it is missing") from None
            if s3.is_denied_code(code):
                raise _Undecided("reading it is denied (AccessDenied)") from None
            raise _Undecided(f"it cannot be read ({code or type(exc).__name__})") from None

    def index(self) -> bytes:
        try:
            body = self._body("release.json")
        except _NotIntact:
            raise _NotIntact(f"{self.uri}release.json is missing: no release is published here")
        except _Undecided as exc:
            raise _Undecided(f"{self.uri}release.json: {exc}") from None
        try:
            data: bytes = body.read(MAX_INDEX_BYTES + 1)
        except Exception as exc:  # noqa: BLE001 - a broken connection
            raise _Undecided(f"{self.uri}release.json cannot be read: {type(exc).__name__}")
        if len(data) > MAX_INDEX_BYTES:
            raise _NotIntact(f"release.json is longer than {MAX_INDEX_BYTES:,} bytes")
        return data

    def sha256(self, rel: str) -> str:
        body = self._body(rel)
        digest = hashlib.sha256()
        try:
            while True:
                chunk = body.read(_CHUNK)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
        except Exception as exc:  # noqa: BLE001 - a broken connection
            raise _Undecided(f"it cannot be read: {type(exc).__name__}") from None
