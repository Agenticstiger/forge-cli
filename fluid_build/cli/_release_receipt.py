# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs receipt record|check``: what each environment did with a sealed release, and
the gate a later environment passes only when an earlier one checked the same code.

``record SRC --report R`` writes a ``fluid-receipt/1`` (:mod:`fluid_build.util.receipt_record`)
beside the release at SRC, write-once, after it verifies SRC: the release's ``identity_digest``
and ``code_digest``, the verdict of the ``fluid verify --release SRC --out R`` report (stored
beside the receipt), and the deploy targets, each recorded as given or, with ``--check-targets``,
read back. ``check SRC --env nonprod`` verifies SRC, then passes only when the newest nonprod
receipt for SRC's code digest passed, its stored report still gives that verdict, and the
nonprod release it names is there and intact. It never reads a release's ``gates`` or
``provenance``. The model is Kargo's: Freight, named by a digest of its artifacts, is promoted
downstream only after an upstream Stage verified it. The receipt's fields follow SLSA's
verification summary attestation, and ``--sign`` wraps them in an in-toto Statement of that
predicate type, signed with cosign (:mod:`fluid_build.cli._signing`).

Receipts sit at ``<code hex>/receipts/<env>/`` beside ``<code hex>/<env>/``, the layout
``fluid ship --release-to`` publishes. For a release directory on disk, the receipts sit in
``receipts/<env>/`` beside the directory.

boto3 is imported only when an ``s3://`` source is read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.util import receipt_record as rr
from fluid_build.util.release_record import clean

ClientFactory = Callable[[str, Optional[str]], Any]

#: Who checked: a ``verifier.id`` as SLSA's verification summary names one.
VERIFIER_ID = "https://fluid-build.io/receipt-verifier/v1"
VSA_PREDICATE_TYPE = "https://slsa.dev/verification_summary/v1"
INTOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
#: The extension field of the signed Statement that carries the receipt itself.
RECEIPT_EXTENSION = "https://fluid-build.io/receipt/v1"

PASS, FAIL, UNDECIDED = 0, 1, 2
_S3_TARGET = re.compile(
    r"s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(?P<key>[^#\s]+)#(?P<version>[A-Za-z0-9._-]{1,1024})"
)
_GIT_TARGET = re.compile(r"git\+(?P<url>\S+)@(?P<commit>[0-9a-f]{40}|[0-9a-f]{64})")
_CONFLICT_TRIES = 3
#: An environment name, as a key segment of the published layout takes it.
_ENVIRONMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._=+-]{0,254}")
#: A receipt named later than now by more than this is not taken: a name from the future would
#: stand as the newest over every receipt written until then.
_CLOCK_SKEW_S = 300
#: Read a file without following a link, and without waiting on a FIFO (whose open blocks
#: until a writer comes): the regular-file check on the open handle then refuses it.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class ReceiptError(Exception):
    """A receipt that cannot be recorded or a check that cannot run. ``exit_code`` 1: refused
    (nothing was written); 2: undecided (a read failed, a tool is missing)."""

    def __init__(self, message: str, exit_code: int = FAIL) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class NoReceipt(ReceiptError):
    """No receipt of the environment for the code: the gate fails, and a listing says nothing."""


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Where a release went: an S3 object version, or a commit of a repository."""

    uri: str
    version_id: Optional[str] = None
    commit: Optional[str] = None

    @property
    def kind(self) -> str:
        return "s3" if self.uri.startswith("s3://") else "git"


def _shown(value: str) -> str:
    """A target as a message may show it: any ``user:password@`` cut to ``***@``."""
    text = re.sub(r"(://)[^/@\s]*@", r"\1***@", value)
    if not text.startswith("s3://"):  # a git URL's query or fragment may hold a token
        text = re.sub(r"[?#][^@]*", "?***", text)
    return clean(text, 120)


def parse_target(value: str) -> Target:
    """``s3://bucket/key#versionId`` (an object version: a key alone can change) or
    ``git+<repo>@<commit>`` (a full 40- or 64-hex commit).

    Raises:
        ReceiptError: (exit 2) neither, or a repository URL with a password in it.
    """
    text = value.strip()
    if text.startswith("s3://"):
        matched = _S3_TARGET.fullmatch(text)
        if matched is None:
            raise ReceiptError(
                f"--target {_shown(value)}: expected s3://bucket/key#versionId (an object "
                "version: a key alone can change)",
                UNDECIDED,
            )
        return Target(f"s3://{matched['bucket']}/{matched['key']}", version_id=matched["version"])
    matched = _GIT_TARGET.fullmatch(text)
    if matched is None:
        raise ReceiptError(
            f"--target {_shown(value)}: expected s3://bucket/key#versionId or "
            "git+<repo>@<full commit>",
            UNDECIDED,
        )
    from fluid_build.cli._ship_release import _repository

    # recorded as fluid ship records a repository: no user, password, query or fragment, so a
    # token in a clone URL never reaches the receipt
    repository = _repository(matched["url"])
    if repository is None:
        raise ReceiptError(
            f"--target {_shown(value)}: the repository is not an https, http, ssh or git URL",
            UNDECIDED,
        )
    return Target(f"git+{repository}", commit=matched["commit"])


def _target_record(
    target: Target, clients: ClientFactory, region: Optional[str], check: bool
) -> Tuple[Dict[str, Any], Optional[str]]:
    """What the receipt says of ``target``, and why it does not check out (``None``: it does,
    or it was not checked). A git target is recorded, never checked: Forge cannot read the
    repository it went to."""
    if target.kind == "git":
        return {"uri": target.uri, "commit": target.commit, "checked": False}, None
    out: Dict[str, Any] = {"uri": target.uri, "version_id": target.version_id, "checked": False}
    if not check:
        return out, None
    from fluid_build.util import s3

    bucket, key = s3.split_uri(target.uri)
    try:
        head = clients("s3", region).head_object(
            Bucket=bucket, Key=key, VersionId=target.version_id, ChecksumMode="ENABLED"
        )
    except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
        code = s3.error_code(exc)
        if s3.is_missing_code(code) or code == "NoSuchVersion":
            out["checked"] = True
            return out, f"{target.uri}#{target.version_id} is not there"
        # Not an answer about the target: no receipt, rather than a FAILED one that stays.
        raise ReceiptError(
            f"--check-targets: {target.uri}#{target.version_id} cannot be read "
            f"({code or type(exc).__name__}); nothing was written",
            UNDECIDED,
        ) from None
    out["checked"] = True
    sums = {
        name[len("Checksum") :].lower(): value
        for name, value in head.items()
        if name.startswith("Checksum") and name != "ChecksumType" and isinstance(value, str)
    }
    if sums:  # as S3 states them (base64), with the checksum type
        out["checksums"] = dict(sums, type=str(head.get("ChecksumType", "FULL_OBJECT")))
    checksum = sums.get("sha256")
    if checksum and head.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
        import base64

        try:
            out["sha256"] = "sha256:" + base64.b64decode(checksum, validate=True).hex()
        except ValueError:
            pass
    if isinstance(head.get("ETag"), str):
        out["etag"] = clean(head["ETag"], 80)
    if isinstance(head.get("ContentLength"), int):
        out["size"] = head["ContentLength"]
    return out, None


# ---------------------------------------------------------------------------
# Where receipts live
# ---------------------------------------------------------------------------


class _Store:
    """The receipts of one environment, write-once."""

    shown: str

    def names(self) -> List[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def read(self, name: str, limit: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def write_once(self, name: str, data: bytes) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    #: The S3 VersionId each entry was read at (``read``), so later reads take that version.
    versions: Dict[str, Optional[str]]

    def lock(self, name: str, version: Optional[str] = None) -> Optional[Tuple[str, datetime]]:
        """The Object Lock an entry (at ``version``) carries, or None (none, or no locks here)."""
        return None


def _within_cap(shown: str, names: List[str]) -> List[str]:
    """``names``, or a refusal past :data:`~fluid_build.util.receipt_record.MAX_ENTRIES`: a
    listing cut short could leave out the newest receipt, so it decides nothing."""
    if len(names) > rr.MAX_ENTRIES:
        raise ReceiptError(
            f"{shown} holds more than {rr.MAX_ENTRIES:,} entries, so the newest receipt cannot be "
            "told: move old receipts aside",
            UNDECIDED,
        )
    return names


class _DirStore(_Store):
    """``<root>/receipts/<env>/``; ``root`` (the release directory's parent) is the reader's own,
    and ``receipts`` and ``<env>`` must be real directories, never links."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.shown = clean(f"{directory}{os.sep}", keep_end=True)
        self.versions = {}

    def _linked(self) -> bool:
        """Whether ``receipts/`` or ``receipts/<env>/`` is a link (or not a directory)."""
        for part in (self.directory.parent, self.directory):
            try:
                mode = os.lstat(part).st_mode
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ReceiptError(f"{self.shown} cannot be read ({type(exc).__name__})", UNDECIDED)
            if not stat.S_ISDIR(mode):
                return True
        return False

    def names(self) -> List[str]:
        if self._linked():
            raise ReceiptError(f"{self.shown} is reached through a link or is not a folder")
        try:
            listed = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ReceiptError(f"{self.shown} cannot be listed ({type(exc).__name__})", UNDECIDED)
        return _within_cap(self.shown, listed)

    def read(self, name: str, limit: int) -> bytes:
        path = self.directory / name
        if self._linked():
            raise ReceiptError(f"{self.shown} is reached through a link or is not a folder")
        try:
            fd = os.open(path, _READ_FLAGS)
        except FileNotFoundError:
            raise ReceiptError(f"{self.shown}{name} is missing")
        except OSError as exc:
            raise ReceiptError(
                f"{self.shown}{name} cannot be read ({type(exc).__name__})", UNDECIDED
            )
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ReceiptError(f"{self.shown}{name} is not a regular file")
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ReceiptError(f"{self.shown}{name} is longer than {limit:,} bytes")
        return data

    def write_once(self, name: str, data: bytes) -> None:
        try:
            linked = self._linked()
            for part in (self.directory.parent, self.directory):  # one level at a time
                if not linked and not os.path.lexists(part):
                    os.mkdir(part, 0o700)
            linked = linked or self._linked()
        except OSError as exc:
            raise ReceiptError(
                f"{self.shown} cannot be made ({type(exc).__name__}); nothing was written there",
                UNDECIDED,
            ) from None
        if linked:
            raise ReceiptError(
                f"{self.shown} is reached through a link; no receipt is written there"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.directory / name, flags, 0o600)
        except FileExistsError:
            raise ReceiptError(f"{self.shown}{name} already exists: a receipt is written once")
        except OSError as exc:
            raise ReceiptError(
                f"{self.shown}{name} cannot be written ({type(exc).__name__})", UNDECIDED
            ) from None
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            raise ReceiptError(
                f"{self.shown}{name} cannot be written ({type(exc).__name__})", UNDECIDED
            ) from None


class _S3Store(_Store):
    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.shown = f"s3://{bucket}/{prefix}"
        self.versions = {}

    def names(self) -> List[str]:
        """The entries, each pinned to its one version. Listed with every version and delete
        marker: Object Lock keeps versions, not keys, so a receipt overwritten or deleted after
        it was written would otherwise drop out of the listing and let an older one decide.
        Any entry with a second version or a delete marker fails the check."""
        from fluid_build.util import s3

        seen: Dict[str, List[Optional[str]]] = {}
        markers: List[str] = []
        extra: Dict[str, str] = {}
        while True:
            try:
                page = self.client.list_object_versions(
                    Bucket=self.bucket, Prefix=self.prefix, **extra
                )
            except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
                code = s3.error_code(exc)
                raise ReceiptError(
                    f"{self.shown} cannot be listed ({code or type(exc).__name__}): reading "
                    "receipts needs s3:ListBucketVersions",
                    UNDECIDED,
                ) from None
            for kind, sink in (("Versions", None), ("DeleteMarkers", markers)):
                for item in page.get(kind) or []:
                    key = item.get("Key")
                    if not isinstance(key, str) or not key.startswith(self.prefix):
                        continue
                    name = key[len(self.prefix) :]
                    if "/" in name:
                        continue
                    if sink is not None:
                        sink.append(name)
                    else:
                        version = item.get("VersionId")
                        seen.setdefault(name, []).append(
                            version if isinstance(version, str) and version != "null" else None
                        )
            _within_cap(self.shown, list(seen) + markers)
            if not page.get("IsTruncated"):
                break
            following = {
                "KeyMarker": str(page.get("NextKeyMarker") or ""),
                "VersionIdMarker": str(page.get("NextVersionIdMarker") or ""),
            }
            if not following["KeyMarker"] or following == extra:
                raise ReceiptError(
                    f"{self.shown} cannot be listed to its end (a page with no next marker)",
                    UNDECIDED,
                )
            extra = following
        changed = sorted(set(markers) | {n for n, v in seen.items() if len(v) > 1})
        if changed:
            raise ReceiptError(
                f"{len(changed)} entr(ies) under {self.shown} were overwritten or deleted after "
                f"they were written ({clean(changed[0], 120)} first): a receipt is written once, "
                "so these receipts decide nothing"
            )
        for name, versions in seen.items():
            self.versions[name] = versions[0]
        return list(seen)

    def read(self, name: str, limit: int) -> bytes:
        from fluid_build.api.release import _close
        from fluid_build.util import s3

        try:
            pinned = self.versions.get(name)  # a second read takes the version the first did
            extra = {"VersionId": pinned} if pinned else {}
            got = self.client.get_object(Bucket=self.bucket, Key=self.prefix + name, **extra)
            version = got.get("VersionId")
            self.versions.setdefault(name, version if isinstance(version, str) else None)
            body = got["Body"]
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            code = s3.error_code(exc)
            if s3.is_missing_code(code):
                raise ReceiptError(f"{self.shown}{name} is missing") from None
            raise ReceiptError(
                f"{self.shown}{name} cannot be read ({code or type(exc).__name__})", UNDECIDED
            ) from None
        try:
            data: bytes = body.read(limit + 1)
        except Exception as exc:  # noqa: BLE001 - a broken connection
            raise ReceiptError(
                f"{self.shown}{name} cannot be read: {type(exc).__name__}", UNDECIDED
            ) from None
        finally:
            _close(body)
        if len(data) > limit:
            raise ReceiptError(f"{self.shown}{name} is longer than {limit:,} bytes")
        return data

    def write_once(self, name: str, data: bytes) -> None:
        from fluid_build.util import s3

        key, hex_digest = self.prefix + name, hashlib.sha256(data).hexdigest()
        for attempt in range(1, _CONFLICT_TRIES + 1):
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentLength=len(data),
                    ChecksumSHA256=s3.sha256_b64(hex_digest),
                    IfNoneMatch="*",
                )
                break
            except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
                code, status = s3.error_code(exc), s3.http_status(exc)
                if code in s3.PRECONDITION_CODES or status == 412:
                    raise ReceiptError(
                        f"{self.shown}{name} already exists: a receipt is written once"
                    ) from None
                if (code in s3.CONFLICT_CODES or status == 409) and attempt < _CONFLICT_TRIES:
                    time.sleep(0.5 * attempt)
                    continue
                if s3.is_denied_code(code):
                    raise ReceiptError(
                        f"writing {self.shown}{name} is denied (AccessDenied)"
                    ) from None
                raise ReceiptError(
                    f"{self.shown}{name} cannot be written ({code or type(exc).__name__})",
                    UNDECIDED,
                ) from None
        head = self._head(name)
        if head is None or head.get("ChecksumSHA256") != s3.sha256_b64(hex_digest):
            raise ReceiptError(f"{self.shown}{name} does not read back with the sha256 written")

    def _head(self, name: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
        from fluid_build.util import s3

        extra = {"VersionId": version} if version else {}
        try:
            head: Dict[str, Any] = self.client.head_object(
                Bucket=self.bucket, Key=self.prefix + name, ChecksumMode="ENABLED", **extra
            )
            return head
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            if s3.is_missing_code(s3.error_code(exc)):
                return None
            raise ReceiptError(
                f"{self.shown}{name} cannot be read back ({s3.error_code(exc) or type(exc).__name__})",
                UNDECIDED,
            ) from None

    def lock(self, name: str, version: Optional[str] = None) -> Optional[Tuple[str, datetime]]:
        return _lock_of(self._head(name, version or self.versions.get(name)))


def _lock_of(head: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, datetime]]:
    """An object's Object Lock while it holds: its mode and retain-until date, or None."""
    if not head:
        return None
    mode, until = head.get("ObjectLockMode"), head.get("ObjectLockRetainUntilDate")
    if mode not in ("GOVERNANCE", "COMPLIANCE") or not isinstance(until, datetime):
        return None
    if until.tzinfo is None or until <= datetime.now(timezone.utc):
        return None
    return str(mode), until


@dataclass(frozen=True)
class _Place:
    """Where a release is and where its receipts go."""

    release_uri: str
    #: ``<code hex>/`` (S3) or the release directory's parent (disk): ``<env>/`` and
    #: ``receipts/<env>/`` sit under it.
    parent: str
    client: Any = None
    bucket: str = ""

    def store(self, environment: str) -> _Store:
        if self.client is None:
            return _DirStore(Path(self.parent) / rr.RECEIPTS_DIR / environment)
        prefix = f"{self.parent}{rr.RECEIPTS_DIR}/{environment}/"
        return _S3Store(self.client, self.bucket, prefix)

    def release_of(self, environment: str) -> str:
        """Where the release of ``environment`` for the same code is (S3: ``<code hex>/<env>/``)."""
        return f"s3://{self.bucket}/{self.parent}{environment}/"


def _place(
    source: str, code_hex: str, environment: str, clients: ClientFactory, region: Optional[str]
) -> _Place:
    if not source.startswith("s3://"):
        directory = Path(source)
        return _Place(
            release_uri=str(directory.absolute()), parent=str(directory.absolute().parent)
        )
    from fluid_build.util import s3

    bucket, key = s3.split_uri(source)
    parts = key.strip("/").split("/")
    if len(parts) < 2 or parts[-2] != code_hex or parts[-1] != environment:
        raise ReceiptError(
            f"{clean(source)} is not <prefix>/<product>/<code hex>/<env>/, where fluid ship "
            "--release-to publishes: receipts go beside the release's environment",
            UNDECIDED,
        )
    try:
        client = clients("s3", region)
    except s3.S3Unavailable:
        raise ReceiptError(
            "an s3:// release needs boto3: pip install 'data-product-forge[aws]'", UNDECIDED
        ) from None
    except Exception as exc:  # noqa: BLE001 - botocore's errors: no client
        raise ReceiptError(f"no S3 client could be made: {type(exc).__name__}", UNDECIDED) from None
    parent = "/".join(parts[:-1]) + "/"
    return _Place(
        release_uri=f"s3://{bucket}/{key.strip('/')}/", parent=parent, client=client, bucket=bucket
    )


def _clients(clients: Optional[ClientFactory]) -> ClientFactory:
    if clients is not None:
        return clients
    from fluid_build.util import s3

    return s3.bounded_clients


# ---------------------------------------------------------------------------
# The verified release
# ---------------------------------------------------------------------------


def _verified(source: str, clients: ClientFactory, region: Optional[str]) -> Any:
    """``verify_release(source)``, intact and with a code digest.

    Raises:
        ReceiptError: exit 1 not intact; exit 2 undecided, or no code digest.
    """
    from fluid_build.api import release as api_release

    checked = api_release.verify_release(source, clients=clients, region=region)
    if checked.status == api_release.UNDECIDED:
        raise ReceiptError(
            f"{checked.source} cannot be checked: " + "; ".join(checked.problems), UNDECIDED
        )
    if not checked.intact:
        raise ReceiptError(f"{checked.source} is not intact: " + "; ".join(checked.problems))
    if checked.code_digest is None:
        raise ReceiptError(
            f"{checked.source} names no source commit, so it has no code digest to gate on: seal "
            "it in a CI run that names its commit",
            UNDECIDED,
        )
    if not isinstance(checked.environment, str) or not _ENVIRONMENT.fullmatch(checked.environment):
        raise ReceiptError(
            f"{checked.source} names no environment a receipts folder can take (letters, digits "
            "and ._=+-, starting with a letter, digit or _)",
            UNDECIDED,
        )
    return checked


def report_release_block(
    source: str, contract_id: str, *, clients: Optional[ClientFactory] = None
) -> Dict[str, Any]:
    """The ``release`` block ``fluid verify --release SRC --out R`` writes into its report: the
    release it checked, verified first, so ``receipt record`` can tie the report to it.

    Raises:
        ReceiptError: the release is not intact (1), cannot be checked (2), or is another
            product's (1).
    """
    from fluid_build.api import release as api_release

    checked = api_release.verify_release(source, clients=_clients(clients))
    if checked.status == api_release.UNDECIDED:
        raise ReceiptError(
            f"--release {checked.source} cannot be checked: " + "; ".join(checked.problems),
            UNDECIDED,
        )
    if not checked.intact:
        raise ReceiptError(
            f"--release {checked.source} is not intact: " + "; ".join(checked.problems)
        )
    if checked.product_id != contract_id:
        raise ReceiptError(
            f"--release {checked.source} is a release of {clean(checked.product_id, 120)}, and the "
            f"contract is {clean(contract_id, 120)}"
        )
    return {
        "source": checked.source,
        "product": checked.product_id,
        "environment": checked.environment,
        "identity_digest": checked.identity_digest,
        "code_digest": checked.code_digest,
    }


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


@dataclass
class Recorded:
    """What ``record`` wrote."""

    uri: str
    result: str
    reason: str
    receipt: Dict[str, Any]
    written: List[str] = field(default_factory=list)


def _read_report(path: str) -> Tuple[bytes, Dict[str, Any]]:
    """The ``fluid verify --out`` report at ``path``: a regular file, not a link, a JSON object."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise ReceiptError(
            f"--report {clean(path, keep_end=True)} cannot be read ({type(exc).__name__})",
            UNDECIDED,
        )
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ReceiptError(
                f"--report {clean(path, keep_end=True)} is not a regular file", UNDECIDED
            )
        data = handle.read(rr.MAX_REPORT_BYTES + 1)
    if len(data) > rr.MAX_REPORT_BYTES:
        raise ReceiptError(
            f"--report {clean(path, keep_end=True)} is larger than a verify report", UNDECIDED
        )
    try:
        report = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ReceiptError(
            f"--report {clean(path, keep_end=True)} is not JSON", UNDECIDED
        ) from None
    if not isinstance(report, dict):
        raise ReceiptError(
            f"--report {clean(path, keep_end=True)} is not a fluid verify report", UNDECIDED
        )
    return data, report


def _report_is_of(report: Mapping[str, Any], checked: Any) -> Optional[str]:
    """Why ``report`` is not a report on the release ``checked``, or None."""
    block = report.get("release")
    if not isinstance(block, Mapping):
        return "it names no release: run fluid verify --release SRC --out REPORT"
    for key, want in (
        ("identity_digest", checked.identity_digest),
        ("code_digest", checked.code_digest),
        ("product", checked.product_id),
        ("environment", checked.environment),
    ):
        if block.get(key) != want:
            return f"its release block names another release ({key} differs)"
    return None


def record_receipt(
    source: str,
    *,
    report_path: str,
    environment: Optional[str] = None,
    targets: Sequence[str] = (),
    check_targets: bool = False,
    sign: bool = False,
    key_ref: Optional[str] = None,
    clients: Optional[ClientFactory] = None,
    region: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Recorded:
    """Verify the release at ``source``, then write its receipt for its environment.

    The receipt is ``PASSED`` when the report's verdict passes and every checked target reads
    back; otherwise ``FAILED``, and still written: a failed deploy is a record too.

    Raises:
        ReceiptError: nothing was written: the release is not intact or cannot be checked, the
            report is not one on this release, ``environment`` is not the release's, a target is
            not one, or the write failed.
    """
    from fluid_build.api.verify_report import report_verdict

    parsed = [parse_target(t) for t in targets]
    if sign:
        from fluid_build._contract_loader import CLIError
        from fluid_build.cli import _signing

        if not _signing.cosign_available():
            raise ReceiptError("--sign needs cosign on PATH (https://docs.sigstore.dev)", UNDECIDED)
        if key_ref is not None:
            try:
                _signing._validate_key_ref(key_ref)
            except CLIError:
                raise ReceiptError(
                    "--sign-key: not a key path or a KMS URI cosign takes", UNDECIDED
                ) from None
    factory = _clients(clients)
    checked = _verified(source, factory, region)
    if environment is not None and environment != checked.environment:
        raise ReceiptError(
            f"--env {clean(environment, 40)}: {checked.source} is the release of "
            f"{clean(checked.environment, 40)}; a receipt names the environment its release was "
            "sealed for",
            UNDECIDED,
        )
    report_bytes, report = _read_report(report_path)
    other = _report_is_of(report, checked)
    if other:
        raise ReceiptError(f"--report {clean(report_path, keep_end=True)}: {other}")
    passed, reason = report_verdict(report)
    expose = report["release"].get("expose")
    if passed and expose is not None:
        passed, reason = (
            False,
            f"the report checked one expose ({clean(expose, 80)}), not the release",
        )
    target_records: List[Dict[str, Any]] = []
    for target in parsed:
        record, problem = _target_record(target, factory, region, check_targets)
        target_records.append(record)
        if problem and passed:
            passed, reason = False, problem
    from fluid_build.cli import _attestation

    place = _place(
        source, checked.code_digest.split(":", 1)[1], checked.environment, factory, region
    )
    moment = now or datetime.now(timezone.utc)
    name = rr.receipt_name(moment, checked.identity_digest)
    raw_summary = report.get("summary")
    summary: Mapping[str, Any] = raw_summary if isinstance(raw_summary, Mapping) else {}
    import fluid_build

    receipt: Dict[str, Any] = {
        "schema": rr.RECEIPT_SCHEMA,
        "product_id": checked.product_id,
        "environment": checked.environment,
        "code_digest": checked.code_digest,
        "identity_digest": checked.identity_digest,
        "result": rr.PASSED if passed else rr.FAILED,
        "reason": clean(reason, 300),
        "time_verified": rr.iso(moment),
        "verifier": {
            "id": VERIFIER_ID,
            "version": {"data-product-forge": str(getattr(fluid_build, "__version__", "unknown"))},
        },
        "resource_uri": place.release_uri,
        "release_objects": [dict(o) for o in checked.objects],
        "report": {
            "name": rr.report_name(name),
            "sha256": "sha256:" + hashlib.sha256(report_bytes).hexdigest(),
            "contract_id": clean(report.get("contract_id"), 120),
            "summary": {k: v for k, v in summary.items() if type(v) is int},
        },
        "targets": target_records,
        "ci": _no_userinfo(_attestation._collect_run_metadata()),
    }
    store = place.store(checked.environment)
    data = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    # Signed before anything is written, so a signing failure leaves nothing behind. Then the
    # report, the signature files, and the receipt last: a reader takes a receipt only with
    # its report.
    entries = [(rr.report_name(name), report_bytes)]
    if sign:
        entries += _signed(name, data, receipt, key_ref)
    entries.append((name, data))
    written: List[str] = []
    for stored, content in entries:
        store.write_once(stored, content)
        written.append(stored)
    return Recorded(
        uri=f"{store.shown}{name}",
        result=receipt["result"],
        reason=receipt["reason"],
        receipt=receipt,
        written=written,
    )


def _no_userinfo(value: Any) -> Any:
    """``value`` with any ``user:password@`` cut out of every URL in it: a CI's URLs can carry
    credentials, and a receipt is read by everyone who reads the bucket."""
    if isinstance(value, Mapping):
        return {str(k): _no_userinfo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_userinfo(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(://)[^/@\s]*@", r"\1", value)
    return value


def statement(receipt: Mapping[str, Any], receipt_bytes: bytes) -> Dict[str, Any]:
    """The in-toto Statement ``--sign`` signs: an SLSA verification summary of the release's
    ``release.json`` (the subject, by its sha256), with the receipt in an extension field."""
    index: Mapping[str, Any] = next(
        (o for o in receipt.get("release_objects") or [] if o.get("path") == "release.json"), {}
    )
    return {
        "_type": INTOTO_STATEMENT_TYPE,
        "subject": [
            {
                "name": "release.json",
                "digest": {"sha256": str(index.get("sha256", "")).removeprefix("sha256:")},
            }
        ],
        "predicateType": VSA_PREDICATE_TYPE,
        "predicate": {
            "verifier": receipt["verifier"],
            "timeVerified": receipt["time_verified"],
            "resourceUri": receipt["resource_uri"],
            "policy": {"uri": f"{RECEIPT_EXTENSION}#report-verdict"},
            "inputAttestations": [
                {
                    "uri": receipt["report"]["name"],
                    "digest": {"sha256": receipt["report"]["sha256"].removeprefix("sha256:")},
                }
            ],
            "verificationResult": receipt["result"],
            "verifiedLevels": ["SLSA_BUILD_LEVEL_UNEVALUATED"],
            RECEIPT_EXTENSION: {
                "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "environment": receipt["environment"],
                "code_digest": receipt["code_digest"],
                "identity_digest": receipt["identity_digest"],
            },
        },
    }


def _signed(
    name: str, data: bytes, receipt: Mapping[str, Any], key_ref: Optional[str]
) -> List[Tuple[str, bytes]]:
    """The receipt's Statement, signed with cosign, and its signature (and, keyless, its
    certificate): the files stored beside the receipt."""
    from fluid_build._contract_loader import CLIError
    from fluid_build.cli import _signing

    stem = name[: -len(".json")]
    body = json.dumps(statement(receipt, data), sort_keys=True, separators=(",", ":")) + "\n"
    with tempfile.TemporaryDirectory(prefix="fluid-receipt-") as scratch:
        path = Path(scratch) / f"{stem}.intoto.jsonl"
        path.write_text(body, encoding="utf-8")
        try:
            result = _signing.sign_blob(str(path), key_ref=key_ref)
        except CLIError as exc:
            raise ReceiptError(f"--sign: {exc.event}; nothing was written", UNDECIDED) from None
        if result.get("exit_code") != 0:
            raise ReceiptError("cosign could not sign the receipt; nothing was written", UNDECIDED)
        try:
            out = [(f"{stem}.intoto.jsonl", path.read_bytes())]
            out.append((f"{stem}.intoto.jsonl.sig", Path(result["sig_path"]).read_bytes()))
            if result.get("pem_path"):
                out.append((f"{stem}.intoto.jsonl.pem", Path(result["pem_path"]).read_bytes()))
        except (OSError, KeyError, TypeError):
            raise ReceiptError(
                "cosign wrote no signature file where it was asked to; nothing was written",
                UNDECIDED,
            ) from None
    return out


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


@dataclass
class Checked:
    """What ``check`` found."""

    source: str
    environment: str
    exit_code: int
    reason: str
    code_digest: Optional[str] = None
    receipt: Optional[str] = None
    receipt_release: Optional[str] = None
    #: Each object of the release and of the receipt with its lock: ``{"uri", "lock"}``,
    #: ``lock`` "MODE until DATE" or None. Empty for a release on disk.
    locks: List[Dict[str, Optional[str]]] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["status"] = {PASS: "passed", FAIL: "failed"}.get(self.exit_code, "undecided")
        return out


def _newest_receipt(
    store: _Store, code_digest: str, ignored: List[str]
) -> Tuple[str, Dict[str, Any], bytes]:
    """The newest receipt in ``store`` for ``code_digest``, with its bytes. Fail closed: when
    the newest file that may be a receipt does not parse, that is the answer, not an older one.
    A receipt of this code named later than now (past a few minutes of clock skew) decides
    nothing; another entry named so is passed over and noted."""
    # ``<code hex>/receipts/`` in S3 holds one code's receipts only; on disk one ``receipts/``
    # beside several releases holds several codes' receipts.
    shared = isinstance(store, _DirStore)
    latest = rr.receipt_name(
        datetime.now(timezone.utc) + timedelta(seconds=_CLOCK_SKEW_S), "sha256:" + "f" * 64
    )
    for name in rr.newest_first(store.names()):
        data = store.read(name, rr.MAX_RECEIPT_BYTES)
        receipt, problem = rr.parse_receipt(name, data)
        if name > latest:
            if receipt is not None and receipt["code_digest"] == code_digest:
                # a recorder whose clock runs ahead, or a forgery: either way not decidable
                raise ReceiptError(
                    f"the newest receipt, {store.shown}{name}, is named later than now: a "
                    "recorder's clock runs ahead, or the name is not its own",
                    UNDECIDED,
                )
            ignored.append(f"{name}: named later than now")
            continue
        if receipt is None:
            raise ReceiptError(f"the newest receipt, {store.shown}{name}, is not one: {problem}")
        if receipt["code_digest"] != code_digest:
            if not shared:
                raise ReceiptError(
                    f"{store.shown}{name} names other code than the folder it is in: the "
                    "receipts there decide nothing"
                )
            ignored.append(f"{name}: a receipt of other code")
            continue
        return name, receipt, data
    raise NoReceipt(f"{store.shown} holds no receipt for code {code_digest}")


@dataclass(frozen=True)
class SignerPolicy:
    """Who must have signed the receipt (``--require-signed``): a cosign key, or a keyless
    signer pinned by its certificate identity and OIDC issuer."""

    key_ref: Optional[str] = None
    certificate_identity: Optional[str] = None
    certificate_oidc_issuer: Optional[str] = None


def check_receipt(
    source: str,
    *,
    environment: str,
    require_locked: bool = False,
    signer: Optional[SignerPolicy] = None,
    clients: Optional[ClientFactory] = None,
    region: Optional[str] = None,
) -> Checked:
    """Whether the release at ``source`` may go on: it verifies, and the newest receipt of
    ``environment`` for its code digest passed, with its stored report, on an intact release
    of that environment. Exit 0 passes, 1 does not, 2 cannot decide. ``require_locked``: also
    every object of the release and of that receipt carries an Object Lock still in force."""
    factory = _clients(clients)
    shown = clean(source)
    ignored: List[str] = []
    try:
        checked = _verified(source, factory, region)
        code = str(checked.code_digest)
        place = _place(source, code.split(":", 1)[1], str(checked.environment), factory, region)
        store = place.store(environment)
        name, receipt, data = _newest_receipt(store, code, ignored)
        reason, there = _receipt_problem(store, name, receipt, environment, place, factory, region)
        if reason is None and signer is not None:
            reason = _versioned_problem(place) or _signature_problem(
                store, name, receipt, data, there, signer
            )
        locks = (
            _locks(place, checked, there, environment, store, name)
            if place.client is not None
            else []
        )
    except ReceiptError as exc:
        return Checked(shown, environment, exc.exit_code, str(exc), ignored=ignored)
    out = Checked(
        checked.source,
        environment,
        FAIL if reason else PASS,
        reason or f"the newest {environment} receipt for this code passed",
        code_digest=code,
        receipt=f"{store.shown}{name}",
        receipt_release=str(receipt["identity_digest"]),
        ignored=ignored,
    )
    if place.client is not None:
        out.locks = locks
        unlocked = [item["uri"] for item in locks if item["lock"] is None]
        unread = [item["uri"] for item in locks if item.get("error")]
        if require_locked and unread and out.exit_code == PASS:
            out.exit_code = UNDECIDED
            out.reason = f"--require-locked: the lock of {len(unread)} object(s) cannot be read"
        elif require_locked and unlocked and out.exit_code == PASS:
            out.exit_code = FAIL
            out.reason = (
                f"--require-locked: {len(unlocked)} object(s) carry no Object Lock in force"
            )
    elif require_locked and out.exit_code == PASS:
        out.exit_code = FAIL
        out.reason = "--require-locked: a release on disk carries no Object Lock"
    return out


def _receipt_problem(
    store: _Store,
    name: str,
    receipt: Mapping[str, Any],
    environment: str,
    place: _Place,
    factory: ClientFactory,
    region: Optional[str],
) -> Tuple[Optional[str], Any]:
    """Why the newest receipt does not let the release on (None when it does), and the
    environment's release it was checked against, once read. Raises ReceiptError (2) when
    something it names cannot be read."""
    from fluid_build.api import release as api_release
    from fluid_build.api.verify_report import report_verdict

    if receipt["environment"] != environment:
        return (f"the newest receipt names environment {clean(receipt['environment'], 40)}"), None
    if receipt["result"] != rr.PASSED:
        return (
            f"the newest {environment} receipt for this code failed: {clean(receipt.get('reason'), 300)}"
        ), None
    stored = store.read(rr.report_name(name), rr.MAX_REPORT_BYTES)
    if "sha256:" + hashlib.sha256(stored).hexdigest() != receipt["report"]["sha256"]:
        return ("the report stored beside the newest receipt is not the one it names"), None
    try:
        report = json.loads(stored.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return ("the report stored beside the newest receipt is not JSON"), None
    passed, why = report_verdict(report if isinstance(report, Mapping) else {})
    if not passed:
        return (f"the report the newest receipt names does not pass: {why}"), None
    release: Any
    if place.client is not None:
        release = place.release_of(environment)
    else:  # on disk: the release the receipt names, and only beside this one
        named = receipt.get("resource_uri")
        base = Path(named).name if isinstance(named, str) and named.isprintable() else ""
        # the release by its folder's name beside this one: the folder may have moved since
        release = str(Path(place.parent) / base)
        if base in ("", ".", ".."):
            return (
                (f"the newest receipt names a release outside {clean(place.parent, keep_end=True)}")
            ), None
    there = api_release.verify_release(release, clients=factory, region=region)
    if there.status == api_release.UNDECIDED:
        raise ReceiptError(
            f"the {environment} release {there.source} cannot be checked: "
            + "; ".join(there.problems),
            UNDECIDED,
        )
    if not there.intact:
        return (
            f"the {environment} release {there.source} is not intact: " + "; ".join(there.problems)
        ), there
    if there.environment != environment:
        return (
            f"the release the newest receipt names, {there.source}, is the release of "
            f"{clean(there.environment, 40)}, not {environment}"
        ), there
    if (
        there.identity_digest != receipt["identity_digest"]
        or there.code_digest != receipt["code_digest"]
    ):
        return (
            (
                f"the {environment} release {there.source} is not the release the newest receipt names"
            )
        ), there
    block = report.get("release") if isinstance(report, Mapping) else None
    if not isinstance(block, Mapping) or block.get("identity_digest") != receipt["identity_digest"]:
        return ("the report the newest receipt names is not a report on its release"), there
    return None, there


def _versioned_problem(place: _Place) -> Optional[str]:
    """With ``--require-signed``: the receipts bucket keeps every version. Without versioning a
    deleted receipt leaves no trace, so an older signed one could decide."""
    if place.client is None:
        return None
    from fluid_build.util import s3

    try:
        status = place.client.get_bucket_versioning(Bucket=place.bucket).get("Status")
    except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
        raise ReceiptError(
            f"--require-signed: the versioning of {place.bucket} cannot be read "
            f"({s3.error_code(exc) or type(exc).__name__})",
            UNDECIDED,
        ) from None
    if status != "Enabled":
        return (
            f"--require-signed: the bucket {place.bucket} does not keep versions, so a deleted "
            "receipt leaves no trace"
        )
    return None


def _signature_problem(
    store: _Store,
    name: str,
    receipt: Mapping[str, Any],
    data: bytes,
    there: Any,
    signer: SignerPolicy,
) -> Optional[str]:
    """Why the newest receipt's signature does not hold, or None: the Statement beside it
    verifies with cosign against ``signer`` and says what the receipt says (its sha256, its
    environment, code and identity, PASSED, and the release.json the environment's release
    was read at). SLSA's checks of a verification summary, minus the levels."""
    from fluid_build._contract_loader import CLIError
    from fluid_build.cli import _signing

    stem = name[: -len(".json")]
    keyless = signer.key_ref is None
    wanted = [f"{stem}.intoto.jsonl", f"{stem}.intoto.jsonl.sig"]
    if keyless:
        wanted.append(f"{stem}.intoto.jsonl.pem")
    try:
        files = {w: store.read(w, rr.MAX_RECEIPT_BYTES) for w in wanted}
    except ReceiptError as exc:
        if exc.exit_code == UNDECIDED:
            raise
        return f"--require-signed: the newest receipt is not signed ({exc})"
    if not _signing.cosign_available():
        raise ReceiptError("--require-signed needs cosign on PATH", UNDECIDED)
    with tempfile.TemporaryDirectory(prefix="fluid-receipt-") as scratch:
        paths = {}
        for stored, content in files.items():
            paths[stored] = Path(scratch) / stored
            paths[stored].write_bytes(content)
        try:
            result = _signing.verify_blob(
                str(paths[wanted[0]]),
                sig_path=str(paths[wanted[1]]),
                pem_path=str(paths[wanted[2]]) if keyless else None,
                key_ref=signer.key_ref,
                certificate_identity=signer.certificate_identity,
                certificate_oidc_issuer=signer.certificate_oidc_issuer,
            )
        except CLIError as exc:
            raise ReceiptError(f"--require-signed: {exc.event}", UNDECIDED) from None
    if result.get("exit_code") != 0:
        return "--require-signed: the newest receipt's signature does not verify for that signer"
    try:
        signed = json.loads(files[wanted[0]].decode("utf-8"))
        predicate = signed["predicate"]
        extension = predicate[RECEIPT_EXTENSION]
        subject = signed["subject"][0]["digest"]["sha256"]
    except (UnicodeDecodeError, ValueError, RecursionError, KeyError, IndexError, TypeError):
        return "--require-signed: the signed Statement is not one a receipt is signed as"
    index: Mapping[str, Any] = next(
        (o for o in there.objects if o.get("path") == "release.json"), {}
    )
    expected = {
        "type": (signed.get("_type"), INTOTO_STATEMENT_TYPE),
        "predicate type": (signed.get("predicateType"), VSA_PREDICATE_TYPE),
        "result": (predicate.get("verificationResult"), rr.PASSED),
        "receipt": (extension.get("receipt_sha256"), hashlib.sha256(data).hexdigest()),
        "environment": (extension.get("environment"), receipt["environment"]),
        "code digest": (extension.get("code_digest"), receipt["code_digest"]),
        "identity digest": (extension.get("identity_digest"), receipt["identity_digest"]),
        "subject": (subject, index.get("sha256")),
    }
    for what, (got, want) in expected.items():
        if got != want or want is None:
            return f"--require-signed: the signed Statement's {what} is not the receipt's"
    return None


def _locks(
    place: _Place, checked: Any, there: Any, environment: str, store: _Store, name: str
) -> List[Dict[str, Optional[str]]]:
    """The lock each object carries: of the release checked, of the environment's release its
    receipt names, and of the receipt and its report. ``lock`` is None for an object without
    one in force; ``error`` says why one could not be read."""
    out: List[Dict[str, Optional[str]]] = []
    entries: List[Tuple[_Store, str]] = []
    # the uris these releases were read from (a result's ``source`` is cut for display)
    for release, uri in ((checked, place.release_uri), (there, place.release_of(environment))):
        if release is None:
            continue
        objects = _S3Store(place.client, place.bucket, uri.split("/", 3)[3])
        for o in release.objects:
            objects.versions[str(o["path"])] = o.get("version_id")
        entries += [(objects, str(o["path"])) for o in release.objects]
    entries += [(store, name), (store, rr.report_name(name))]
    for where, rel in entries:
        item: Dict[str, Optional[str]] = {
            "uri": f"{where.shown}{rel}",
            "version_id": where.versions.get(rel),
            "lock": None,
        }
        try:
            held = where.lock(rel)
        except ReceiptError as exc:
            item["error"] = str(exc)
            held = None
        if held:
            item["lock"] = f"{held[0]} until {held[1].strftime('%Y-%m-%dT%H:%M:%SZ')}"
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# runs status
# ---------------------------------------------------------------------------


def newest_by_environment(
    release_dir: Path, code_digest: Optional[str]
) -> Dict[str, Dict[str, Any]]:
    """For ``fluid runs status``: the newest receipt of each environment beside a release on
    disk, for its code digest, as ``{env: {"result", "time_verified", "identity_digest"}}``,
    picked as ``receipt check`` picks it: when the newest file there is not a receipt, or the
    directory cannot be read, the entry is ``{"problem": ...}``. An environment with no
    receipt for the code is left out."""
    out: Dict[str, Dict[str, Any]] = {}
    if code_digest is None:
        return out
    root = release_dir.absolute().parent / rr.RECEIPTS_DIR
    try:
        environments = sorted(os.listdir(root))[:100]
    except OSError:
        return out
    for env in environments:
        try:
            if env.startswith(".") or not stat.S_ISDIR(os.lstat(root / env).st_mode):
                continue
        except OSError:
            continue
        try:
            _name, receipt, _data = _newest_receipt(_DirStore(root / env), code_digest, [])
        except NoReceipt:
            continue
        except ReceiptError as exc:
            out[clean(env, 40)] = {"problem": clean(str(exc), 300)}
            continue
        out[clean(env, 40)] = {
            "result": receipt["result"],
            "time_verified": receipt["time_verified"],
            "identity_digest": receipt["identity_digest"],
        }
    return out
