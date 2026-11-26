# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Verify a sealed release (``fluid-release/1``), on disk or in S3, without changing it.

``fluid ship --release-dir DIR`` seals a release: ``release.json`` names the release's
``identity``, its ``identity_digest`` and the sha256 of every other file. ``fluid ship
--release-to s3://bucket/prefix/`` publishes the same bytes under
``<prefix>/<product>/<code hex>/<environment>/``. :func:`verify_release` reads one back and
says whether it is the release it claims to be:

- ``intact``: the index parses as a ``fluid-release/1`` record, its ``identity_digest`` is the
  digest of its ``identity``, its ``code_digest`` is the digest of the code ``identity`` names,
  an S3 key laid out as ``<code hex>/<env>/`` is this release's code and environment, every
  file the index lists is there with the sha256 it names, and the files ``identity`` pins (the
  bundle, each external file, the artifacts MANIFEST and what it lists, the plan) are listed with
  the digests ``identity`` names, so a file swapped with its index entry rewritten is caught;
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
from dataclasses import asdict, dataclass, field
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
#: Bytes of ``artifacts/MANIFEST.json`` or ``plan.json`` read to tie them to ``identity``.
MAX_MEMBER_BYTES = 64 * 1_048_576
_CHUNK = 1 << 20
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")
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
    #: The digest of the code the release runs, the same for every environment sealed from one
    #: commit and one lock; ``None`` when the release names no source commit.
    code_digest: Optional[str] = None
    files_checked: int = 0
    problems: Tuple[str, ...] = field(default_factory=tuple)
    #: Each object read, ``release.json`` first: ``{"path", "sha256", "version_id"}`` (the S3
    #: VersionId, ``None`` for a directory or a bucket without versioning), for a record of
    #: exactly which versions were checked.
    objects: Tuple[Dict[str, Optional[str]], ...] = field(default_factory=tuple)

    @property
    def intact(self) -> bool:
        return self.status == INTACT

    @property
    def exit_code(self) -> int:
        """0 intact, 1 not intact, 2 cannot decide: the check commands' convention."""
        return _EXIT[self.status]

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["problems"] = list(self.problems)
        out["objects"] = [dict(o) for o in self.objects]
        return out


class _NotIntact(Exception):
    """The release is not the one its index says."""


class _Undecided(Exception):
    """The release could not be read."""


def identity_intact(index: Mapping[str, Any]) -> bool:
    """Whether ``index`` (a parsed ``release.json``) has an ``identity_digest`` that is the
    digest of its ``identity``. Says nothing about the files: :func:`verify_release` does."""
    from fluid_build.util import release_record as record

    identity, digest = index.get("identity"), index.get("identity_digest")
    if not isinstance(identity, Mapping) or not isinstance(digest, str):
        return False
    try:
        return record.identity_digest(identity) == digest
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
        data = reader.index()
        index = _parse_index(data)
        reader.hashes["release.json"] = hashlib.sha256(data).hexdigest()
        try:
            return _check(shown, reader, index)
        except (TypeError, ValueError, AttributeError, KeyError, RecursionError) as exc:
            # a value of a type no release writes, somewhere inside a well-formed index
            raise _NotIntact(
                f"release.json holds a value no release writes ({type(exc).__name__})"
            ) from None
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
    stated = index.get("code_digest")
    shown_code = _clean(stated, 80) if isinstance(stated, str) else None

    def result(status: str, checked: int, problems: List[str]) -> ReleaseVerification:
        return ReleaseVerification(
            source=shown,
            status=status,
            identity_digest=shown_digest,
            product_id=shown_product,
            environment=shown_env,
            code_digest=shown_code,
            files_checked=checked,
            problems=tuple(problems),
            objects=tuple(
                {"path": _clean(rel), "sha256": hexd, "version_id": reader.versions.get(rel)}
                for rel, hexd in reader.hashes.items()
            ),
        )

    problems: List[str] = []
    if not identity_intact(index):
        problems.append("its identity_digest is not the digest of its identity")
    problems.extend(_code_problems(identity, stated))
    problems.extend(_layout_problems(reader, stated, environment))
    checked, undecided = _check_files(reader, index["files"], problems)
    if not problems:
        _check_bound(reader, identity, index["files"], problems, undecided)
    if problems:
        return result(NOT_INTACT, checked, problems)
    if undecided:
        return result(UNDECIDED, checked, undecided)
    return result(INTACT, checked, [])


def _code_problems(identity: Mapping[str, Any], stated: Any) -> List[str]:
    """Whether ``code_digest`` is the digest of the code ``identity`` names (none without a
    source commit). It sits outside ``identity``, so it is recomputed, never trusted."""
    from fluid_build.util import release_record as record

    source = identity.get("source")
    if source is not None and not _is_source(source):
        return ["its identity's source is not a commit (git+<repo> and a hex gitCommit)"]
    expected = record.code_digest(identity)
    if expected == stated:
        return []
    if expected is None:
        return ["it states a code_digest, and its identity names no source commit"]
    if stated is None:
        return ["its identity names a source commit, and it states no code_digest"]
    return ["its code_digest is not the digest of the code its identity names"]


def _is_source(source: Any) -> bool:
    """Whether ``source`` is what ``fluid ship`` writes: a hex commit, and maybe a ``git+`` uri."""
    if not isinstance(source, Mapping) or set(source) - {"uri", "digest"}:
        return False
    digest = source.get("digest")
    commit = digest.get("gitCommit") if isinstance(digest, Mapping) else None
    uri = source.get("uri", "git+")
    return (
        isinstance(commit, str)
        and re.fullmatch(r"[0-9a-f]{7,64}", commit) is not None
        and isinstance(uri, str)
        and uri.startswith("git+")
    )


def _layout_problems(reader: "_Reader", stated: Any, environment: Any) -> List[str]:
    """Under ``<product>/<code hex>/<env>/``, the key must be this release's code and env."""
    layout = reader.layout()
    if layout is None:
        return []
    code_hex, env = layout
    match = _DIGEST.fullmatch(stated) if isinstance(stated, str) else None
    found: List[str] = []
    if match is None or match.group(1) != code_hex:
        found.append(f"it sits under {code_hex}, the key of other code")
    if env != environment:
        found.append(f"it sits under the environment {_clean(env, 40)}, and it is another's")
    return found


def _check_files(
    reader: "_Reader", files: Mapping[str, Any], problems: List[str]
) -> Tuple[int, List[str]]:
    """Hash every file ``files`` lists: how many were read, and which could not be. A file
    that is not what the index names adds to ``problems``."""
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
            reader.hashes[rel] = actual
        except _NotIntact as exc:
            problems.append(f"{_clean(rel)}: {exc}")
            continue
        except _Undecided as exc:
            undecided.append(f"{_clean(rel)}: {exc}")
            continue
        checked += 1
        if "sha256:" + actual != expected:
            problems.append(f"{_clean(rel)}: its bytes are not the sha256 the index names")
    return checked, undecided


def _check_bound(
    reader: "_Reader",
    identity: Mapping[str, Any],
    files: Mapping[str, Any],
    problems: List[str],
    undecided: List[str],
) -> None:
    """Tie the files to the digests ``identity`` pins, so a file swapped with a ``files`` entry
    rewritten to match is not intact: the bundle's sha256, each external file's, the artifacts
    MANIFEST's Merkle root and each artifact it lists, and the plan's digests. ``files`` sits
    outside ``identity_digest``; these are inside it."""

    def listed(rel: str, digest: Any, what: str) -> None:
        if not isinstance(digest, str) or files.get(rel) != digest:
            problems.append(
                f"{_clean(rel)}: the index does not list it with the sha256 {what} names"
            )

    bundle = identity.get("bundle")
    if isinstance(bundle, Mapping) and "sha256" in bundle:
        listed("bundle.tgz", bundle.get("sha256"), "the identity's bundle")
    external = identity.get("external", [])
    if not isinstance(external, list):
        problems.append("its identity's external entry is not a list")
        external = []
    pinned_external = set()
    for item in external:
        item = item if isinstance(item, Mapping) else {}
        digest = item.get("digest")
        hex_digest = digest.get("sha256") if isinstance(digest, Mapping) else None
        path = item.get("path")
        rel = str(path) if isinstance(path, str) else "external/?"
        pinned_external.add(rel)
        listed(
            rel,
            f"sha256:{hex_digest}" if isinstance(hex_digest, str) else None,
            "the identity's external entry",
        )
    for rel in sorted(files):
        if rel.startswith("external/") and rel not in pinned_external:
            problems.append(f"{_clean(rel)}: the index lists it and the identity does not")
    artifacts = identity.get("artifacts")
    if isinstance(artifacts, Mapping) and "manifest_digest" in artifacts:
        _check_artifacts(reader, artifacts.get("manifest_digest"), files, problems, undecided)
    plan = identity.get("plan")
    if isinstance(plan, Mapping):
        _check_plan(reader, plan, files, problems, undecided)


def _read_json_member(
    reader: "_Reader", rel: str, files: Mapping[str, Any], problems: List[str], undecided: List[str]
) -> Optional[Mapping[str, Any]]:
    """``rel`` (a file the index lists, already hashed) as a JSON object, or None with why."""
    if rel not in files:
        problems.append(f"{rel}: the identity pins it and the index does not list it")
        return None
    try:
        data = reader.read(rel, MAX_MEMBER_BYTES)
        if "sha256:" + hashlib.sha256(data).hexdigest() != files[rel]:
            raise _NotIntact("it changed while it was read")
    except _NotIntact as exc:
        problems.append(f"{rel}: {exc}")
        return None
    except _Undecided as exc:
        undecided.append(f"{rel}: {exc}")
        return None
    try:
        parsed = json.loads(data)
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict):
        problems.append(f"{rel}: not a JSON object")
        return None
    return parsed


def _check_artifacts(
    reader: "_Reader",
    root: Any,
    files: Mapping[str, Any],
    problems: List[str],
    undecided: List[str],
) -> None:
    from fluid_build.forge.core.bundle import merkle_root

    manifest = _read_json_member(reader, "artifacts/MANIFEST.json", files, problems, undecided)
    if manifest is None:
        return
    listed = manifest.get("files")
    if not isinstance(listed, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in listed.items()
    ):
        problems.append("artifacts/MANIFEST.json: lists no files map")
        return
    try:
        computed = merkle_root(listed)
    except ValueError:  # text JSON can hold and UTF-8 cannot encode (a lone surrogate)
        computed = None
    if manifest.get("digest") != root or computed != root:
        problems.append(
            "artifacts/MANIFEST.json: its Merkle root is not the one the identity names"
        )
        return
    for rel in sorted(listed):
        if files.get(f"artifacts/{rel}") != listed[rel]:
            problems.append(
                f"{_clean(f'artifacts/{rel}')}: the index does not list it with the sha256 the "
                "artifacts MANIFEST names"
            )
    named = {f"artifacts/{rel}" for rel in listed} | {"artifacts/MANIFEST.json"}
    for rel in sorted(files):
        if rel.startswith("artifacts/") and rel not in named:
            problems.append(
                f"{_clean(rel)}: the index lists it and the artifacts MANIFEST does not"
            )


def _check_plan(
    reader: "_Reader",
    pinned: Mapping[str, Any],
    files: Mapping[str, Any],
    problems: List[str],
    undecided: List[str],
) -> None:
    from fluid_build.forge.core.plan_digest import compute_plan_digest

    plan = _read_json_member(reader, "plan.json", files, problems, undecided)
    if plan is None:
        return
    try:
        recomputed = compute_plan_digest(dict(plan))
    except (TypeError, ValueError, RecursionError):
        recomputed = None
    if (
        plan.get("planDigest") != pinned.get("plan_digest")
        or recomputed != pinned.get("plan_digest")
        or plan.get("bundleDigest") != pinned.get("bundle_digest")
    ):
        problems.append("plan.json: its digests are not the ones the identity names")


def _parse_index(data: bytes) -> Dict[str, Any]:
    from fluid_build.util import release_record as record

    try:
        index = json.loads(data)
    except (ValueError, RecursionError):
        raise _NotIntact(f"{record.RELEASE_INDEX} is not JSON") from None
    if not isinstance(index, dict) or index.get("schema") != record.RELEASE_SCHEMA:
        raise _NotIntact(f"{record.RELEASE_INDEX} is not a {record.RELEASE_SCHEMA} record")
    identity, digest, files = (index.get(k) for k in ("identity", "identity_digest", "files"))
    if not isinstance(identity, dict) or not isinstance(digest, str):
        raise _NotIntact(f"{record.RELEASE_INDEX} has no identity and identity_digest")
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise _NotIntact(
            f"{record.RELEASE_INDEX} has no files map of at most {MAX_FILES:,} entries"
        )
    return index


def _path_problem(rel: Any) -> Optional[str]:
    """Why ``rel`` is no path inside a release, or None."""
    if not isinstance(rel, str) or not rel or len(rel) > 1024 or "\\" in rel or "//" in rel:
        return "a path that is not one"
    parts = PurePosixPath(rel).parts
    if rel.startswith("/") or not parts or PurePosixPath(rel).as_posix() != rel:
        return "a path that is not one"
    if any(p in (".", "..") or len(p) > 255 or not p.isprintable() for p in parts):
        return "a path that leads out of the release or has characters it never writes"
    if rel == "release.json":
        return "the index's own name"
    return None


def _clean(value: Any, limit: int = 200) -> str:
    """Text from a record, printable and cut, as ``fluid runs status`` shows it, so a record
    cannot rewrite the terminal."""
    from fluid_build.util.release_record import clean

    return clean(value, limit)


class _Reader:
    def __init__(self) -> None:
        #: The sha256 of each object read, and its S3 VersionId when it has one.
        self.hashes: Dict[str, str] = {}
        self.versions: Dict[str, Optional[str]] = {}

    def index(self) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def sha256(self, rel: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def layout(self) -> Optional[Tuple[str, str]]:
        """``(code hex, environment)`` when the source sits at ``<code hex>/<env>/``."""
        return None

    def read(self, rel: str, limit: int) -> bytes:  # pragma: no cover - interface
        """At most ``limit`` bytes of ``rel``; more is not intact."""
        raise NotImplementedError


class _DirReader(_Reader):
    """A release directory. No link is followed: a file is opened with ``O_NOFOLLOW`` and must
    resolve to its own path (no linked directory on the way), and is checked on the open
    handle, so a FIFO or a device never blocks."""

    def __init__(self, root: str) -> None:
        super().__init__()
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
        except (FileNotFoundError, NotADirectoryError):  # a file where a directory should be
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

    def read(self, rel: str, limit: int) -> bytes:
        fd = self._open(rel)
        try:
            data = _read_at_most(fd, limit + 1)
        finally:
            os.close(fd)
        if len(data) > limit:
            raise _NotIntact(f"it is longer than {limit:,} bytes")
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

        super().__init__()
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

    def layout(self) -> Optional[Tuple[str, str]]:
        parts = self.prefix.rstrip("/").split("/")
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{64}", parts[-2]):
            return parts[-2], parts[-1]
        return None

    def _body(self, rel: str) -> Any:
        from fluid_build.util import s3

        try:
            pinned = self.versions.get(rel)  # a second read takes the version the first read did
            extra = {"VersionId": pinned} if pinned else {}
            got = self.client.get_object(Bucket=self.bucket, Key=self.prefix + rel, **extra)
            version = got.get("VersionId")
            self.versions.setdefault(rel, version if isinstance(version, str) else None)
            return got["Body"]
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

    def read(self, rel: str, limit: int) -> bytes:
        body = self._body(rel)
        try:
            data: bytes = body.read(limit + 1)
        except Exception as exc:  # noqa: BLE001 - a broken connection
            raise _Undecided(f"it cannot be read: {type(exc).__name__}") from None
        if len(data) > limit:
            raise _NotIntact(f"it is longer than {limit:,} bytes")
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
