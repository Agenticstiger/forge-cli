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
from datetime import datetime, timezone
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
_GIT_TARGET = re.compile(
    r"(?P<repo>git\+[a-z][a-z0-9+.-]*://[^\s@]+(?:@[^\s@/:]+)?[^\s@]*)@(?P<commit>[0-9a-f]{40}|[0-9a-f]{64})"
)
_CONFLICT_TRIES = 3


class ReceiptError(Exception):
    """A receipt that cannot be recorded or a check that cannot run. ``exit_code`` 1: refused
    (nothing was written); 2: undecided (a read failed, a tool is missing)."""

    def __init__(self, message: str, exit_code: int = FAIL) -> None:
        super().__init__(message)
        self.exit_code = exit_code


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
    return clean(re.sub(r"(://)[^/@\s]*@", r"\1***@", value), 120)


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
    authority = matched["repo"].split("://", 1)[1].split("/", 1)[0]
    if "@" in authority and ":" in authority.rsplit("@", 1)[0]:
        # not echoed: the value holds a password
        raise ReceiptError(
            "--target: a repository URL with a password in it is not recorded", UNDECIDED
        )
    return Target(matched["repo"], commit=matched["commit"])


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
        missing = s3.is_missing_code(code) or code == "NoSuchVersion"
        why = "is not there" if missing else f"cannot be read ({code or type(exc).__name__})"
        out["checked"] = True
        return out, f"{target.uri}#{target.version_id} {why}"
    out["checked"] = True
    checksum = head.get("ChecksumSHA256")
    if isinstance(checksum, str) and head.get("ChecksumType", "FULL_OBJECT") == "FULL_OBJECT":
        import base64

        try:
            out["sha256"] = "sha256:" + base64.b64decode(checksum, validate=True).hex()
        except ValueError:
            pass
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

    def lock(self, name: str) -> Optional[Tuple[str, datetime]]:
        """The Object Lock an entry carries, or None (none, or a store without locks)."""
        return None


class _DirStore(_Store):
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.shown = clean(f"{directory}{os.sep}", keep_end=True)

    def names(self) -> List[str]:
        try:
            listed = os.listdir(self.directory)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ReceiptError(f"{self.shown} cannot be listed ({type(exc).__name__})", UNDECIDED)
        return listed[: rr.MAX_RECEIPTS * 2]

    def read(self, name: str, limit: int) -> bytes:
        path = self.directory / name
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
        self.directory.mkdir(parents=True, exist_ok=True)
        if (
            self.directory.is_symlink()
            or self.directory.resolve() != self.directory.absolute().resolve()
        ):
            raise ReceiptError(
                f"{self.shown} is reached through a link; no receipt is written there"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.directory / name, flags, 0o600)
        except FileExistsError:
            raise ReceiptError(f"{self.shown}{name} already exists: a receipt is written once")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)


class _S3Store(_Store):
    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client, self.bucket, self.prefix = client, bucket, prefix
        self.shown = f"s3://{bucket}/{prefix}"

    def names(self) -> List[str]:
        from fluid_build.util import s3

        out: List[str] = []
        token: Optional[str] = None
        while True:
            extra = {"ContinuationToken": token} if token else {}
            try:
                page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, **extra)
            except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
                code = s3.error_code(exc)
                raise ReceiptError(
                    f"{self.shown} cannot be listed ({code or type(exc).__name__})", UNDECIDED
                ) from None
            for item in page.get("Contents") or []:
                key = item.get("Key")
                if (
                    isinstance(key, str)
                    and key.startswith(self.prefix)
                    and "/" not in key[len(self.prefix) :]
                ):
                    out.append(key[len(self.prefix) :])
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token or len(out) >= rr.MAX_RECEIPTS * 2:
                return out

    def read(self, name: str, limit: int) -> bytes:
        from fluid_build.api.release import _close
        from fluid_build.util import s3

        try:
            body = self.client.get_object(Bucket=self.bucket, Key=self.prefix + name)["Body"]
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

    def _head(self, name: str) -> Optional[Dict[str, Any]]:
        from fluid_build.util import s3

        try:
            head: Dict[str, Any] = self.client.head_object(
                Bucket=self.bucket, Key=self.prefix + name, ChecksumMode="ENABLED"
            )
            return head
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            if s3.is_missing_code(s3.error_code(exc)):
                return None
            raise ReceiptError(
                f"{self.shown}{name} cannot be read back ({s3.error_code(exc) or type(exc).__name__})",
                UNDECIDED,
            ) from None

    def lock(self, name: str) -> Optional[Tuple[str, datetime]]:
        return _lock_of(self._head(name))


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
    if not isinstance(checked.environment, str) or not checked.environment:
        raise ReceiptError(f"{checked.source} names no environment", UNDECIDED)
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
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
    except (UnicodeDecodeError, ValueError):
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
        from fluid_build.cli import _signing

        if not _signing.cosign_available():
            raise ReceiptError("--sign needs cosign on PATH (https://docs.sigstore.dev)", UNDECIDED)
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
        "ci": _attestation._collect_run_metadata(),
    }
    store = place.store(checked.environment)
    data = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    written: List[str] = []
    # The report first and the receipt last: a reader takes a receipt only with its report.
    store.write_once(rr.report_name(name), report_bytes)
    written.append(rr.report_name(name))
    if sign:
        written += _sign_and_store(store, name, data, receipt, key_ref)
    store.write_once(name, data)
    written.append(name)
    return Recorded(
        uri=f"{store.shown}{name}",
        result=receipt["result"],
        reason=receipt["reason"],
        receipt=receipt,
        written=written,
    )


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


def _sign_and_store(
    store: _Store, name: str, data: bytes, receipt: Mapping[str, Any], key_ref: Optional[str]
) -> List[str]:
    """Sign the receipt's Statement with cosign and store it with its signature (and, keyless,
    its certificate) beside the receipt, write-once."""
    from fluid_build.cli import _signing

    stem = name[: -len(".json")]
    body = json.dumps(statement(receipt, data), sort_keys=True, separators=(",", ":")) + "\n"
    with tempfile.TemporaryDirectory(prefix="fluid-receipt-") as scratch:
        path = Path(scratch) / f"{stem}.intoto.jsonl"
        path.write_text(body, encoding="utf-8")
        result = _signing.sign_blob(str(path), key_ref=key_ref)
        if result.get("exit_code") != 0:
            raise ReceiptError(
                "cosign could not sign the receipt; nothing was written beyond the report",
                UNDECIDED,
            )
        out = [(f"{stem}.intoto.jsonl", path.read_bytes())]
        out.append((f"{stem}.intoto.jsonl.sig", Path(result["sig_path"]).read_bytes()))
        if result.get("pem_path"):
            out.append((f"{stem}.intoto.jsonl.pem", Path(result["pem_path"]).read_bytes()))
    for stored, content in out:
        store.write_once(stored, content)
    return [stored for stored, _ in out]


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
) -> Tuple[str, Dict[str, Any]]:
    """The newest receipt in ``store`` for ``code_digest``. Fail closed: when the newest file
    that may be a receipt does not parse, that is the answer, not an older one."""
    names = rr.newest_first(store.names())[: rr.MAX_RECEIPTS]
    for name in names:
        receipt, problem = rr.parse_receipt(name, store.read(name, rr.MAX_RECEIPT_BYTES))
        if receipt is None:
            raise ReceiptError(f"the newest receipt, {store.shown}{name}, is not one: {problem}")
        if receipt["code_digest"] != code_digest:
            ignored.append(f"{name}: a receipt of other code")
            continue
        return name, receipt
    raise ReceiptError(f"{store.shown} holds no receipt for code {code_digest}")


def check_receipt(
    source: str,
    *,
    environment: str,
    require_locked: bool = False,
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
        name, receipt = _newest_receipt(store, code, ignored)
        reason = _receipt_problem(store, name, receipt, environment, place, factory, region)
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
        out.locks = _locks(place, checked, store, name)
        unlocked = [item["uri"] for item in out.locks if item["lock"] is None]
        if require_locked and unlocked and out.exit_code == PASS:
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
) -> Optional[str]:
    """Why the newest receipt does not let the release on, or None. Raises ReceiptError (2)
    when something it names cannot be read."""
    from fluid_build.api import release as api_release
    from fluid_build.api.verify_report import report_verdict

    if receipt["environment"] != environment:
        return f"the newest receipt names environment {clean(receipt['environment'], 40)}"
    if receipt["result"] != rr.PASSED:
        return f"the newest {environment} receipt for this code failed: {clean(receipt.get('reason'), 300)}"
    stored = store.read(rr.report_name(name), rr.MAX_REPORT_BYTES)
    if "sha256:" + hashlib.sha256(stored).hexdigest() != receipt["report"]["sha256"]:
        return "the report stored beside the newest receipt is not the one it names"
    try:
        report = json.loads(stored.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "the report stored beside the newest receipt is not JSON"
    passed, why = report_verdict(report if isinstance(report, Mapping) else {})
    if not passed:
        return f"the report the newest receipt names does not pass: {why}"
    if place.client is not None:
        release = place.release_of(environment)
    else:  # on disk: the release the receipt names, and only beside this one
        release = str(receipt.get("resource_uri", ""))
        if not release or Path(release).parent != Path(place.parent):
            return (
                f"the newest receipt names a release outside {clean(place.parent, keep_end=True)}"
            )
    there = api_release.verify_release(release, clients=factory, region=region)
    if there.status == api_release.UNDECIDED:
        raise ReceiptError(
            f"the {environment} release {there.source} cannot be checked: "
            + "; ".join(there.problems),
            UNDECIDED,
        )
    if not there.intact:
        return f"the {environment} release {there.source} is not intact: " + "; ".join(
            there.problems
        )
    if (
        there.identity_digest != receipt["identity_digest"]
        or there.code_digest != receipt["code_digest"]
    ):
        return (
            f"the {environment} release {there.source} is not the release the newest receipt names"
        )
    block = report.get("release") if isinstance(report, Mapping) else None
    if not isinstance(block, Mapping) or block.get("identity_digest") != receipt["identity_digest"]:
        return "the report the newest receipt names is not a report on its release"
    return None


def _locks(place: _Place, checked: Any, store: _Store, name: str) -> List[Dict[str, Optional[str]]]:
    """The lock each object of the release and of the receipt carries."""
    out: List[Dict[str, Optional[str]]] = []
    release = _S3Store(place.client, place.bucket, place.release_uri.split("/", 3)[3])
    entries: List[Tuple[_Store, str]] = [(release, str(o["path"])) for o in checked.objects]
    entries += [(store, name), (store, rr.report_name(name))]
    for where, rel in entries:
        held = where.lock(rel)
        out.append(
            {
                "uri": f"{where.shown}{rel}",
                "lock": (
                    f"{held[0]} until {held[1].strftime('%Y-%m-%dT%H:%M:%SZ')}" if held else None
                ),
            }
        )
    return out


# ---------------------------------------------------------------------------
# runs status
# ---------------------------------------------------------------------------


def newest_by_environment(
    release_dir: Path, code_digest: Optional[str]
) -> Dict[str, Dict[str, Any]]:
    """For ``fluid runs status``: the newest receipt of each environment beside a release on
    disk, for its code digest, as ``{env: {"result", "time_verified", "identity_digest"}}``.
    A receipt that does not parse, and every receipt of other code, is left out."""
    out: Dict[str, Dict[str, Any]] = {}
    if code_digest is None:
        return out
    root = release_dir.absolute().parent / rr.RECEIPTS_DIR
    try:
        environments = sorted(os.listdir(root))[:100]
    except OSError:
        return out
    for env in environments:
        if env.startswith(".") or not stat.S_ISDIR(os.lstat(root / env).st_mode):
            continue
        store = _DirStore(root / env)
        try:
            names = rr.newest_first(store.names())[: rr.MAX_RECEIPTS]
            for name in names:
                receipt, _ = rr.parse_receipt(name, store.read(name, rr.MAX_RECEIPT_BYTES))
                if receipt is not None and receipt["code_digest"] == code_digest:
                    out[clean(env, 40)] = {
                        "result": receipt["result"],
                        "time_verified": receipt["time_verified"],
                        "identity_digest": receipt["identity_digest"],
                    }
                    break
        except ReceiptError:
            continue
    return out
