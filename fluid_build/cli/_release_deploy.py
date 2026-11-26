# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid runs deploy SRC --env E``: copy the external files a sealed release names to where
its ``identity["deploy"]`` sends them (``fluid ship --deploy-targets``), then record a deploy
receipt in ``deploys/<env>/`` beside the release (never in ``receipts/<env>/``, where only
the verifier writes).

Before anything is written it verifies the release (intact, with a code digest), that it is the
release of ``--env``, optionally that an earlier environment's verify receipt lets it on
(``--require-receipt``, as ``fluid runs receipt check``), and that the credentials are the
account the release names. Then, every target but an Airflow DAG first and the DAGs last, so a
DAG folder never picks up a DAG before the files it runs:

- a target whose object already has the file's full-object SHA-256 is left as it is;
- otherwise the file is copied from the release object at the version that was verified (a
  release in S3 must sit in a versioned bucket, and its release.json have one version), with
  ``CopySourceIfMatch`` that version's ETag, ``If-Match`` the target's ETag when it exists or
  ``If-None-Match: *`` when it does not, so a target changed meanwhile is never overwritten,
  and ``ExpectedBucketOwner`` the release's account on every call to it;
- each copy is read back and must have the SHA-256 the release names; its VersionId is kept.

The guarantees are the ones ``iac/providers/aws_ingest.py::_pinned_copy`` gives an OpenTofu
copy (``copy_if_match``, SHA-256 checksum, a postcondition), made with boto3's CopyObject
conditions (botocore 1.40.61 and later). The skip of an unchanged object follows bep/s3deploy.
The model is Kargo's promotion: a deploy is not a check, so its receipt never gates.

boto3 is imported only when S3 is read.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from fluid_build.util import receipt_record as rr
from fluid_build.util.release_record import clean

from . import _release_receipt as receipts

PASS, FAIL, UNDECIDED = receipts.PASS, receipts.FAIL, receipts.UNDECIDED
LOG_SCHEMA = "fluid-deploy-log/1"
#: A copy that raced a write to its target (409) is tried this many times.
_CONFLICT_TRIES = 3
_UPGRADE = (
    "pip install -U 'data-product-forge[aws]' (a conditional copy needs boto3 and botocore "
    "1.40.61 or later)"
)
#: ``(env, region) -> (account, why)``: STS GetCallerIdentity, or a test's stand-in.
AccountOf = Callable[[Mapping[str, str], Optional[str]], Tuple[str, str]]


class DeployError(Exception):
    """A deploy that stopped. ``exit_code`` 1: refused or failed; 2: undecided."""

    def __init__(self, message: str, exit_code: int = FAIL) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class Deployed:
    """What ``deploy`` did: each target's ``action`` is ``copied``, ``unchanged`` (left as it
    was), ``would copy`` / ``would leave`` (``--dry-run``) or ``not reached``."""

    source: str
    environment: str
    exit_code: int
    reason: str
    dry_run: bool = False
    account: Optional[str] = None
    targets: List[Dict[str, Any]] = field(default_factory=list)
    receipt: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        done = "dry-run" if self.dry_run else "deployed"
        out["status"] = {PASS: done, FAIL: "failed"}.get(self.exit_code, "undecided")
        return out


def deploy_release(
    source: str,
    *,
    environment: str,
    require_receipt: Optional[str] = None,
    require_locked: bool = False,
    signer: Optional[receipts.SignerPolicy] = None,
    dry_run: bool = False,
    sign: bool = False,
    key_ref: Optional[str] = None,
    clients: Optional[receipts.ClientFactory] = None,
    region: Optional[str] = None,
    account_of: Optional[AccountOf] = None,
    now: Optional[datetime] = None,
) -> Deployed:
    """Deploy the release at ``source`` to its targets for ``environment``. Exit 0 deployed (or
    nothing to change), 1 refused or failed, 2 cannot decide. A failure once a target was
    reached still writes a FAILED deploy receipt; ``dry_run`` writes nothing at all."""
    factory = receipts._clients(clients)
    out = Deployed(clean(source), environment, UNDECIDED, "", dry_run=dry_run)
    try:
        checked = receipts._verified(source, factory, region)
        out.source = checked.source
        if checked.environment != environment:
            raise DeployError(
                f"--env {clean(environment, 40)}: {checked.source} is the release of "
                f"{clean(checked.environment, 40)}; a release deploys to the environment it "
                "was sealed for",
                UNDECIDED,
            )
        if source.startswith("s3://"):
            _pinned(source, checked, factory, region)
        deploy = _deploy_block(source, checked, factory, region)
        out.account = deploy["account"]
        if sign:
            receipts.sign_ready(key_ref)
        if require_receipt is not None:
            gate = receipts.check_receipt(
                source,
                environment=require_receipt,
                require_locked=require_locked,
                signer=signer,
                clients=factory,
                region=region,
            )
            if gate.exit_code != PASS:
                raise DeployError(
                    f"--require-receipt {require_receipt}: {gate.reason}", gate.exit_code
                )
        target_region = deploy.get("region") or region
        dest = _client(factory, target_region)
        _guard_conditions(dest, "CopyObject" if source.startswith("s3://") else "PutObject")
        _check_account(deploy["account"], account_of, target_region)
    except (DeployError, receipts.ReceiptError) as exc:
        out.exit_code, out.reason = exc.exit_code, str(exc)
        return out
    plan = sorted(deploy["targets"], key=_order_key)
    out.targets = [_record(t) for t in plan]
    copier = _Copier(source, checked, factory, region, dest, deploy["account"])
    failure: Optional[DeployError] = None
    for target, record in zip(plan, out.targets, strict=True):
        try:
            copier.deploy(target, record, dry_run)
        except DeployError as exc:
            failure = exc
            record["action"] = "failed"
            record["problem"] = str(exc)
            break
    if failure is not None:
        out.exit_code, out.reason = failure.exit_code, str(failure)
    else:
        copied = sum(1 for r in out.targets if r["action"] == "copied")
        out.exit_code = PASS
        out.reason = (
            f"dry run: {sum(1 for r in out.targets if r['action'] == 'would copy')} to copy"
            if dry_run
            else f"{copied} copied, {len(out.targets) - copied} unchanged"
        )
    if dry_run:
        return out
    try:
        done = receipts.write_receipt(
            source,
            checked,
            check=rr.DEPLOY,
            passed=failure is None,
            reason=out.reason,
            report_bytes=_log(out, checked),
            report_meta={"kind": LOG_SCHEMA},
            targets=[_receipt_target(r) for r in out.targets],
            sign=sign,
            key_ref=key_ref,
            clients=factory,
            region=region,
            now=now,
        )
    except receipts.ReceiptError as exc:
        out.exit_code = UNDECIDED
        out.reason = f"{out.reason}; the deploy receipt was not written: {exc}"
        return out
    out.receipt = done.uri
    return out


def _order_key(target: Mapping[str, Any]) -> Any:
    from . import _deploy_targets

    return _deploy_targets.order_key(target)


def _record(target: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "artifact": clean(target.get("artifact"), 100),
        "kind": clean(target.get("kind"), 40),
        "to": clean(target.get("to"), 1100),
        "sha256": f"sha256:{target['digest']['sha256']}",
        "action": "not reached",
    }


def _receipt_target(record: Mapping[str, Any]) -> Dict[str, Any]:
    """A target as the deploy receipt records it: read back when it was copied or left."""
    checked = record["action"] in ("copied", "unchanged")
    out = {
        "uri": record["to"],
        "version_id": record.get("version_id"),
        "sha256": record["sha256"],
        "checked": checked,
        "action": record["action"],
    }
    return out


def _log(out: Deployed, checked: Any) -> bytes:
    """The deploy log stored beside the receipt, as a verify report is."""
    log = {
        "schema": LOG_SCHEMA,
        "release": {
            "source": checked.source,
            "product": checked.product_id,
            "environment": checked.environment,
            "identity_digest": checked.identity_digest,
            "code_digest": checked.code_digest,
        },
        "account": out.account,
        "result": rr.PASSED if out.exit_code == PASS else rr.FAILED,
        "reason": out.reason,
        "targets": out.targets,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return json.dumps(log, indent=2, sort_keys=True).encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# Before any write
# ---------------------------------------------------------------------------


def _deploy_block(
    source: str, checked: Any, factory: receipts.ClientFactory, region: Optional[str]
) -> Dict[str, Any]:
    """``identity["deploy"]`` of the verified release: release.json read again at the version
    that was verified, and taken only with the sha256 it was verified with."""
    index_object = next((o for o in checked.objects if o.get("path") == "release.json"), None)
    if index_object is None:
        raise DeployError(f"{checked.source}: release.json was not read", UNDECIDED)
    from fluid_build.api import release as api_release

    try:
        if source.startswith("s3://"):
            from fluid_build.util import s3

            bucket, key = s3.split_uri(source.rstrip("/") + "/release.json")
            extra = (
                {"VersionId": index_object["version_id"]} if index_object.get("version_id") else {}
            )
            body = factory("s3", region).get_object(Bucket=bucket, Key=key, **extra)["Body"]
            try:
                data = body.read(api_release.MAX_INDEX_BYTES + 1)
            finally:
                api_release._close(body)
        else:
            data = _read_regular(Path(source) / "release.json", api_release.MAX_INDEX_BYTES + 1)
    except DeployError:
        raise
    except Exception as exc:  # noqa: BLE001 - botocore's errors and OSError: not decided
        raise DeployError(
            f"{checked.source}release.json cannot be read again ({type(exc).__name__})", UNDECIDED
        ) from None
    if hashlib.sha256(data).hexdigest() != index_object.get("sha256"):
        raise DeployError(f"{checked.source}: release.json changed since it was verified")
    deploy = json.loads(data.decode("utf-8")).get("identity", {}).get("deploy")
    if not isinstance(deploy, Mapping):
        raise DeployError(
            f"{checked.source} names no deploy targets: seal it with fluid ship --artifacts LOCK "
            "--deploy-targets FILE",
            UNDECIDED,
        )
    # verify_release checked its shape and that each target is an external file of the release
    return dict(deploy)


def _pinned(
    source: str, checked: Any, factory: receipts.ClientFactory, region: Optional[str]
) -> None:
    """In S3, the release's objects can be copied only as the versions that were verified, and
    its release.json has never been rewritten. A bucket without versioning gives no version to
    pin, so a swapped object would be copied; a second version of release.json (or a delete
    marker) would let deploy targets changed after the seal stand, since they sit outside the
    code digest. Both are refused, so the release prefix's writers must not hold
    s3:DeleteObjectVersion either, as for receipts.

    Raises:
        DeployError: (exit 2) no versions to pin; (exit 1) release.json was rewritten.
    """
    from fluid_build.util import s3

    if any(not o.get("version_id") for o in checked.objects):
        raise DeployError(
            f"{checked.source} sits in a bucket without versioning, so a copy cannot be pinned "
            "to the bytes that were verified: publish releases to a versioned bucket "
            "(fluid ship --release-to-require versioning)",
            UNDECIDED,
        )
    bucket, key = s3.split_uri(source.rstrip("/") + "/release.json")
    try:
        got = factory("s3", region).list_object_versions(Bucket=bucket, Prefix=key)
    except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
        code = s3.error_code(exc)
        raise DeployError(
            f"{checked.source}release.json: its versions cannot be listed "
            f"({code or type(exc).__name__}; s3:ListBucketVersions is needed)",
            UNDECIDED,
        ) from None
    entries = [
        e
        for kind in ("Versions", "DeleteMarkers")
        for e in got.get(kind) or []
        if isinstance(e, dict) and e.get("Key") == key
    ]
    verified = next(o["version_id"] for o in checked.objects if o.get("path") == "release.json")
    if got.get("IsTruncated") or [e.get("VersionId") for e in entries] != [verified]:
        raise DeployError(
            f"{checked.source}release.json has been written more than once (versions or a "
            "delete marker beside the one verified): a release is written once, so its deploy "
            "targets cannot be taken"
        )


def _read_regular(path: Path, limit: int) -> bytes:
    """Up to ``limit`` bytes of a regular file, never through a link."""
    import stat

    fd = os.open(path, receipts._READ_FLAGS)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise DeployError(f"{clean(path, keep_end=True)} is not a regular file", UNDECIDED)
        data: bytes = handle.read(limit)
    return data


def _client(factory: receipts.ClientFactory, region: Optional[str]) -> Any:
    from fluid_build.util import s3

    try:
        return factory("s3", region)
    except s3.S3Unavailable:
        raise DeployError(f"a deploy needs boto3: {_UPGRADE}", UNDECIDED) from None
    except Exception as exc:  # noqa: BLE001 - botocore's errors: no client
        raise DeployError(f"no S3 client could be made: {type(exc).__name__}", UNDECIDED) from None


def _guard_conditions(client: Any, operation: str) -> None:
    """The installed botocore takes ``If-Match`` and ``If-None-Match`` on ``operation``; an older
    one would drop neither and refuse the call, or worse, have no way to say them."""
    try:
        members = client.meta.service_model.operation_model(operation).input_shape.members
    except Exception:  # noqa: BLE001 - no model to read: the guard cannot pass
        raise DeployError(
            f"the S3 client's {operation} model cannot be read; {_UPGRADE}", UNDECIDED
        ) from None
    if "IfMatch" not in members or "IfNoneMatch" not in members:
        raise DeployError(f"this botocore cannot copy conditionally: {_UPGRADE}", UNDECIDED)


def _check_account(account: str, account_of: Optional[AccountOf], region: Optional[str]) -> None:
    """The credentials are the account the release deploys to: STS GetCallerIdentity."""
    if account_of is None:
        from fluid_build.iac.target_identity import aws_caller_account

        account_of = aws_caller_account
    found, why = account_of(os.environ, region)
    if not found:
        raise DeployError(f"the account of these credentials cannot be read: {why}", UNDECIDED)
    if found != account:
        raise DeployError(
            f"these credentials are account {clean(found, 20)}, and the release deploys to "
            f"account {account}"
        )


# ---------------------------------------------------------------------------
# The copies
# ---------------------------------------------------------------------------


class _Copier:
    """Copies one release's targets."""

    def __init__(
        self,
        source: str,
        checked: Any,
        factory: receipts.ClientFactory,
        region: Optional[str],
        dest: Any,
        account: str,
    ) -> None:
        self.source = source
        self.remote = source.startswith("s3://")
        self.versions = {str(o["path"]): o.get("version_id") for o in checked.objects}
        self.factory = factory
        self.region = region
        self.dest = dest
        self.account = account

    def deploy(self, target: Mapping[str, Any], record: Dict[str, Any], dry_run: bool) -> None:
        from fluid_build.util import s3

        bucket, key = s3.split_uri(str(target["to"]))
        digest = str(target["digest"]["sha256"])
        shown = clean(target["to"], 200)
        head = self._head(bucket, key, None, shown)
        if head is not None and _full_sha256(head) == digest:
            record["action"] = "would leave" if dry_run else "unchanged"
            record["version_id"] = _version(head)
            return
        if dry_run:
            record["action"] = "would copy"
            return
        conditions = (
            {"IfMatch": head["ETag"]}
            if head is not None and head.get("ETag")
            else {"IfNoneMatch": "*"}
        )
        if head is not None and not head.get("ETag"):
            raise DeployError(
                f"{shown}: its object states no ETag to copy over on condition", UNDECIDED
            )
        for attempt in range(1, _CONFLICT_TRIES + 1):
            try:
                written = self._write(target, bucket, key, conditions)
                break
            except DeployError:
                raise
            except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
                code = s3.error_code(exc)
                status = s3.http_status(exc)
                if code in s3.CONFLICT_CODES and attempt < _CONFLICT_TRIES:
                    continue
                raise _write_error(shown, code, status, exc, conditions) from None
        version = written.get("VersionId") if isinstance(written.get("VersionId"), str) else None
        back = self._head(bucket, key, version, shown)
        if back is None or _full_sha256(back) != digest:
            raise DeployError(
                f"{shown}: read back after the copy, it does not have the sha256 the release "
                f"names ({digest})"
            )
        record["action"] = "copied"
        record["version_id"] = _version(back)

    def _head(
        self, bucket: str, key: str, version: Optional[str], shown: str
    ) -> Optional[Dict[str, Any]]:
        from fluid_build.util import s3

        extra = {"VersionId": version} if version else {}
        try:
            head: Dict[str, Any] = self.dest.head_object(
                Bucket=bucket,
                Key=key,
                ChecksumMode="ENABLED",
                ExpectedBucketOwner=self.account,
                **extra,
            )
            return head
        except Exception as exc:  # noqa: BLE001 - botocore's errors, sorted by their code
            code = s3.error_code(exc)
            if s3.is_missing_code(code) and not version:
                return None
            if s3.is_denied_code(code):
                raise DeployError(
                    f"{shown}: reading it is refused ({code}): the bucket is not account "
                    f"{self.account}'s, or these credentials lack s3:GetObject on it"
                ) from None
            raise DeployError(
                f"{shown} cannot be read ({code or type(exc).__name__})", UNDECIDED
            ) from None

    def _write(
        self, target: Mapping[str, Any], bucket: str, key: str, conditions: Mapping[str, str]
    ) -> Dict[str, Any]:
        from fluid_build.util import s3

        rel = str(target["path"])
        common = dict(Bucket=bucket, Key=key, ExpectedBucketOwner=self.account, **conditions)
        if self.remote:
            src_bucket, prefix = s3.split_uri(self.source.rstrip("/") + "/")
            copy_source: Dict[str, str] = {"Bucket": src_bucket, "Key": prefix + rel}
            pinned = self.versions.get(rel)
            if pinned:
                copy_source["VersionId"] = pinned
            src_head = self.factory("s3", self.region).head_object(
                Bucket=src_bucket, Key=prefix + rel, **({"VersionId": pinned} if pinned else {})
            )
            copied: Dict[str, Any] = self.dest.copy_object(
                CopySource=copy_source,
                CopySourceIfMatch=src_head["ETag"],
                ChecksumAlgorithm="SHA256",
                **common,
            )
            return copied
        import stat

        fd = os.open(Path(self.source) / rel, receipts._READ_FLAGS)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise DeployError(f"{rel} is not a regular file", UNDECIDED)
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
            if digest.hexdigest() != str(target["digest"]["sha256"]):
                raise DeployError(f"{rel}: its bytes changed since the release was verified")
            handle.seek(0)
            # S3 checks the body against the checksum, so bytes that change after the hash above
            # are refused (BadDigest), never stored
            put: Dict[str, Any] = self.dest.put_object(
                Body=handle,
                ChecksumSHA256=s3.sha256_b64(str(target["digest"]["sha256"])),
                **common,
            )
        return put


def _write_error(
    shown: str, code: str, status: Optional[int], exc: BaseException, conditions: Mapping[str, str]
) -> DeployError:
    from fluid_build.util import s3

    if code in s3.PRECONDITION_CODES:
        changed = "was written meanwhile" if "IfNoneMatch" in conditions else "changed meanwhile"
        return DeployError(
            f"{shown} {changed}, or the release object is no longer the verified one (412); "
            "nothing was written. Deploy again to copy over what is there now"
        )
    if code == "NotImplemented" or status == 501:
        return DeployError(
            f"{shown}: S3 refused the conditional copy (501 NotImplemented): a bucket policy that "
            "enforces conditional writes (s3:if-match / s3:if-none-match) allows no CopyObject; "
            "lift that enforcement for the deploy role, or deploy with your own tooling"
        )
    if s3.is_denied_code(code):
        return DeployError(
            f"{shown}: the copy is refused ({code}): these credentials lack s3:PutObject or "
            "s3:GetObject on it, or s3:GetObject and s3:GetObjectVersion on the release, or a "
            "bucket policy enforcing conditional writes (s3:if-match / s3:if-none-match) refuses it"
        )
    if code in s3.CONFLICT_CODES:
        return DeployError(f"{shown}: the copy raced other writes {_CONFLICT_TRIES} times (409)")
    return DeployError(f"{shown}: the copy failed ({code or type(exc).__name__})", UNDECIDED)


def _full_sha256(head: Mapping[str, Any]) -> Optional[str]:
    """The object's full-object SHA-256 in hex, as S3 states it, or None."""
    import base64

    value = head.get("ChecksumSHA256")
    if not isinstance(value, str) or head.get("ChecksumType", "FULL_OBJECT") != "FULL_OBJECT":
        return None
    try:
        return base64.b64decode(value, validate=True).hex()
    except ValueError:
        return None


def _version(head: Mapping[str, Any]) -> Optional[str]:
    value = head.get("VersionId")
    return value if isinstance(value, str) and value != "null" else None
