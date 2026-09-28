# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``fluid verify``: the bucket applies the retention and encryption the contract declares.

Two dimensions of the S3 + Glue verifier (``_verify_athena.py``), each present
only when the expose declares it, read from the live bucket in the binding's
region:

* ``retention`` — ``exposes[].lifecycle {retention, expire: true}``.
  ``s3:GetLifecycleConfiguration``; the enabled rules whose filter is a plain
  prefix covering the binding's prefix, and the earliest ``Expiration.Days``
  among them (S3 applies the earliest), must equal the contract's period in
  days. No such rule, or another number of days, fails. A rule filtered by tag
  or object size, or scoped to a narrower prefix inside the binding's, covers
  only some objects and cannot apply the retention; one that expires them
  sooner than the period cuts it short for those objects, and fails too.
* ``encryption`` — ``binding.encryption.kms``. The key is resolved with
  ``kms:DescribeKey`` (the product key by its alias, an alias or ARN as
  written) and must be ``Enabled``: S3 can neither write nor read an SSE-KMS
  object under a disabled key or one pending deletion, and a key pending
  deletion is brought back with ``kms:CancelKeyDeletion``, never by a
  re-apply, which would create a new key. Then every object under the prefix,
  up to :data:`MAX_OBJECTS_CHECKED`, must answer ``s3:HeadObject`` with
  ``ServerSideEncryption: aws:kms`` and that key's ARN. An object with SSE-S3 or
  another key fails, and names the object.

What is expected comes from ``iac/providers/aws_storage.py``, the derivation
``fluid apply`` emits from, so verify checks what apply wrote. On a shared
(pool) bucket apply writes nothing and the pool's owner holds the rule and the
key; the same checks then hold the pool to the contract.

A mismatch is CRITICAL (``fluid verify --strict`` fails). A check that could
not run (access denied, a throttled call) is an error, which fails
``fluid verify`` with or without ``--strict``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from fluid_build.cli._verify_athena import _error_code

LOG = logging.getLogger("fluid.cli.verify.storage")

#: Objects under the prefix whose encryption is read, in key order. A larger
#: prefix is reported as checked in part.
MAX_OBJECTS_CHECKED = 1000

#: How many offending objects a failure names.
_NAMED_OBJECTS = 5

ClientFactory = Callable[[str, str], Any]


@dataclass
class StoragePolicy:
    """The storage dimensions of one expose, and the key the Athena result may use."""

    dimensions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The ARN of the key the expose declares, when it was resolved: the key
    #: Athena encrypts a result with when the result goes under the binding's
    #: own bucket.
    kms_key_arn: Optional[str] = None

    @property
    def errors(self) -> List[str]:
        return [d["message"] for d in self.dimensions.values() if d["status"] == "error"]


def _why(exc: BaseException) -> str:
    return _error_code(exc) or type(exc).__name__


def _rule_scope(rule: Mapping[str, Any]) -> Optional[Tuple[str, bool]]:
    """``(prefix, every object)``: the prefix a rule applies under, and whether to all of it.

    ``Filter: {}`` and a missing filter are the whole bucket (``""``). A filter
    that also names tags or object sizes (``Tag``, ``ObjectSizeGreaterThan``,
    ``ObjectSizeLessThan``, or those inside ``And``) applies to only some of the
    objects under its prefix: ``False``. ``None`` for a filter this cannot read.
    """
    if "Filter" not in rule:
        return str(rule.get("Prefix") or ""), True
    rule_filter = rule.get("Filter") or {}
    if not isinstance(rule_filter, Mapping):
        return None
    conditions = rule_filter.get("And") if "And" in rule_filter else rule_filter
    if not isinstance(conditions, Mapping):
        return None
    every = set(rule_filter) <= {"Prefix", "And"} and set(conditions) <= {"Prefix"}
    return str(conditions.get("Prefix") or ""), every


@dataclass(frozen=True)
class _Rule:
    """An enabled lifecycle rule that expires objects after a number of days."""

    id: str
    prefix: str
    days: int
    #: Whether it applies to every object under ``prefix`` (no tag or size filter).
    every: bool

    def describe(self) -> Dict[str, Any]:
        return {"id": self.id, "prefix": self.prefix, "days": self.days, "every": self.every}


def _expiring_rules(rules: List[Mapping[str, Any]]) -> List[_Rule]:
    found: List[_Rule] = []
    for rule in rules:
        if rule.get("Status") != "Enabled":
            continue
        scope = _rule_scope(rule)
        days = (rule.get("Expiration") or {}).get("Days")
        if scope is None or days is None:
            continue
        found.append(_Rule(str(rule.get("ID") or ""), scope[0], int(days), scope[1]))
    return found


def _sooner(rule: _Rule, prefix: str, days: int) -> bool:
    """Whether ``rule`` deletes some objects under ``prefix`` before ``days``.

    S3 applies every enabled rule whose filter matches an object, and the
    earliest expiration wins. A rule filtered by tag or size on the prefix, or
    on a narrower prefix inside it, cannot apply the retention (it misses the
    other objects) but can still cut it short for the objects it does match.
    """
    reaches = prefix.startswith(rule.prefix) or rule.prefix.startswith(prefix)
    covers_all = rule.every and prefix.startswith(rule.prefix)
    return reaches and not covers_all and rule.days < days


def _retention_dimension(s3: Any, bucket: str, retention: Any) -> Dict[str, Any]:
    target = f"s3://{bucket}/{retention.prefix}"
    expected = {"days": retention.days, "period": retention.period, "prefix": retention.prefix}
    try:
        rules = list(s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules") or [])
    except Exception as exc:  # noqa: BLE001 — every AWS failure is reported, not raised
        if _error_code(exc) != "NoSuchLifecycleConfiguration":
            return {
                "status": "error",
                "expected": expected,
                "message": f"Could not read the lifecycle configuration of {bucket}: {_why(exc)}",
            }
        rules = []
    expiring = _expiring_rules(rules)
    # The rules that apply the retention: every object under the prefix.
    covering = [r for r in expiring if r.every and retention.prefix.startswith(r.prefix)]
    # The rules that cut it short for some of those objects.
    sooner = [r for r in expiring if _sooner(r, retention.prefix, retention.days)]
    # What is wrong with the rule that should apply the retention (none, or
    # another number of days), apart from the rules that cut it short.
    problems: List[str] = []
    actual: Dict[str, Any] = {
        "rules": [r.describe() for r in covering],
        "sooner": [r.describe() for r in sooner],
    }
    if not covering:
        problems.append(
            f"No enabled lifecycle rule expires the objects under {target}; the contract "
            f"keeps them {retention.period} ({retention.days} day(s)) and then expires them"
        )
    else:
        applied = min(covering, key=lambda r: r.days)
        actual.update(days=applied.days, rule=applied.id, rule_prefix=applied.prefix)
        if applied.days != retention.days:
            problems.append(
                f"The objects under {target} expire after {applied.days} day(s) (lifecycle rule "
                f"{applied.id or 'without an ID'} on {applied.prefix or 'the whole bucket'!r}); "
                f"the contract says {retention.period} ({retention.days} day(s))"
            )
    mismatch = bool(problems)
    for rule in sooner:
        # One prefix contains the other; the objects both reach are under the longer.
        reached = f"s3://{bucket}/{max(rule.prefix, retention.prefix, key=len)}"
        which = reached if rule.every else f"{reached} that match its tag or object-size filter"
        problems.append(
            f"Lifecycle rule {rule.id or 'without an ID'} expires the objects under {which} "
            f"after {rule.days} day(s), sooner than the contract's {retention.period} "
            f"({retention.days} day(s))"
        )
    if problems:
        failed: Dict[str, Any] = {
            "status": "fail",
            "expected": expected,
            "actual": actual,
            "message": "; ".join(problems),
        }
        # The reason picks the remedy (``_verify_athena._STORAGE_ACTIONS``);
        # no reason is the re-apply. Both problems need both remedies:
        # removing the sooner rule alone leaves the retention unapplied.
        if sooner:
            failed["reason"] = "mismatch-and-sooner-rule" if mismatch else "sooner-rule"
        return failed
    return {
        "status": "pass",
        "expected": expected,
        "actual": actual,
        "message": (
            f"The objects under {target} expire after {actual['days']} day(s) "
            f"(rule {actual['rule']})"
        ),
    }


def _list_keys(s3: Any, bucket: str, prefix: str) -> Tuple[List[str], bool]:
    """Up to :data:`MAX_OBJECTS_CHECKED` keys under ``prefix``, and whether there are more."""
    keys: List[str] = []
    token: Optional[str] = None
    while True:
        request: Dict[str, Any] = {
            "Bucket": bucket,
            "Prefix": prefix,
            "MaxKeys": min(1000, MAX_OBJECTS_CHECKED - len(keys) + 1),
        }
        if token:
            request["ContinuationToken"] = token
        page = s3.list_objects_v2(**request)
        keys.extend(str(item["Key"]) for item in page.get("Contents") or [])
        if len(keys) > MAX_OBJECTS_CHECKED:
            return keys[:MAX_OBJECTS_CHECKED], True
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return keys, False


class _HeadFailed(Exception):
    def __init__(self, key: str, why: str) -> None:
        super().__init__(key)
        self.key, self.why = key, why


def _objects_not_under(s3: Any, bucket: str, keys: List[str], key_arn: str) -> List[Dict[str, Any]]:
    """The objects that are not SSE-KMS with ``key_arn``, from ``HeadObject``."""
    wrong: List[Dict[str, Any]] = []
    for key in keys:
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 — reported by the caller
            raise _HeadFailed(key, _why(exc)) from exc
        sse = str(head.get("ServerSideEncryption") or "")
        used = str(head.get("SSEKMSKeyId") or "")
        if sse != "aws:kms" or used != key_arn:
            wrong.append({"key": key, "sse": sse or None, "kms_key": used or None})
    return wrong


def _key_state_dimension(
    key_ref: str, metadata: Mapping[str, Any], expected: Mapping[str, Any], target: str
) -> Optional[Dict[str, Any]]:
    """The failure for a key S3 cannot use, or ``None`` when its state is ``Enabled``.

    A key that exists is not a key that works: S3 refuses every write and read
    of an SSE-KMS object whose key is disabled or pending deletion, and once a
    key pending deletion is deleted its objects can never be read again. The
    ``reason`` picks the remedy (``_verify_athena._STORAGE_ACTIONS``): a key
    pending deletion must be brought back with ``kms:CancelKeyDeletion``, since
    re-applying would create a new key and leave the objects under this one.
    """
    state = str(metadata.get("KeyState") or "")
    if state == "Enabled":
        return None
    arn = str(metadata.get("Arn") or key_ref)
    reason = _KEY_STATE_REASONS.get(state, "key-not-enabled")
    when = metadata.get("DeletionDate")
    deleted_on = (
        f", and is deleted on {when.isoformat() if hasattr(when, 'isoformat') else when}"
        if when and reason == "key-pending-deletion"
        else ""
    )
    return {
        "status": "fail",
        "reason": reason,
        "expected": {**expected, "kms_key_arn": arn, "key_state": "Enabled"},
        "actual": {"key_state": state or None, "deletion_date": str(when) if when else None},
        "message": (
            f"The KMS key {key_ref} ({arn}) is {state or 'in no reported state'}, not Enabled"
            f"{deleted_on}: S3 cannot encrypt new objects under {target} with it, "
            "nor decrypt the ones it already encrypted"
        ),
    }


#: ``KeyState`` values with a remedy of their own (``_verify_athena._STORAGE_ACTIONS``).
_KEY_STATE_REASONS = {
    "PendingDeletion": "key-pending-deletion",
    "PendingReplicaDeletion": "key-pending-deletion",
    "Disabled": "key-disabled",
}


def _encryption_dimension(
    s3: Any, kms: Any, bucket: str, prefix: str, key_ref: str
) -> Tuple[Dict[str, Any], Optional[str]]:
    target = f"s3://{bucket}/{prefix}"
    expected: Dict[str, Any] = {"sse": "aws:kms", "kms_key": key_ref}
    try:
        metadata = kms.describe_key(KeyId=key_ref)["KeyMetadata"]
        key_arn = str(metadata["Arn"])
    except Exception as exc:  # noqa: BLE001 — every AWS failure is reported, not raised
        if _error_code(exc) == "NotFoundException":
            return {
                "status": "fail",
                "expected": expected,
                "message": f"The KMS key {key_ref} does not exist in this account and region",
            }, None
        return {
            "status": "error",
            "expected": expected,
            "message": f"Could not resolve the KMS key {key_ref}: {_why(exc)}",
        }, None
    unusable = _key_state_dimension(key_ref, metadata, expected, target)
    if unusable is not None:
        # Nothing Athena could write under this key either, so it is not
        # offered for the result.
        return unusable, None
    expected["kms_key_arn"] = key_arn
    try:
        keys, more = _list_keys(s3, bucket, prefix)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "expected": expected,
            "message": f"Could not list the objects under {target}: {_why(exc)}",
        }, key_arn
    if not keys:
        return {
            "status": "info",
            "expected": expected,
            "actual": {"objects_checked": 0},
            "message": f"No objects under {target} yet, so none could be checked",
        }, key_arn
    try:
        wrong = _objects_not_under(s3, bucket, keys, key_arn)
    except _HeadFailed as exc:
        return {
            "status": "error",
            "expected": expected,
            "message": f"Could not read the encryption of s3://{bucket}/{exc.key}: {exc.why}",
        }, key_arn
    checked = f"{len(keys)} object(s)" + (
        f" (the first {MAX_OBJECTS_CHECKED} by key)" if more else ""
    )
    actual: Dict[str, Any] = {"objects_checked": len(keys), "partial": more, "wrong": wrong}
    if wrong:
        named = ", ".join(
            f"{w['key']} ({w['sse'] or 'no SSE'}{', ' + w['kms_key'] if w['kms_key'] else ''})"
            for w in wrong[:_NAMED_OBJECTS]
        )
        extra = f" and {len(wrong) - _NAMED_OBJECTS} more" if len(wrong) > _NAMED_OBJECTS else ""
        return {
            "status": "fail",
            "expected": expected,
            "actual": actual,
            "message": (
                f"{len(wrong)} of {checked} under {target} are not SSE-KMS with {key_arn}: "
                f"{named}{extra}"
            ),
        }, key_arn
    return {
        "status": "pass",
        "expected": expected,
        "actual": actual,
        "message": f"{checked} under {target} are SSE-KMS with {key_arn}",
    }, key_arn


def storage_policy(
    expose_id: str,
    expose: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
    region: str,
    factory: ClientFactory,
) -> StoragePolicy:
    """The ``retention`` and ``encryption`` dimensions ``expose`` declares.

    ``binding`` is the expose's binding with its ``{{ env.* }}`` templates
    resolved, as ``fluid apply`` resolved them. Never raises for an AWS
    failure or a declaration apply would refuse: the reason is the dimension.
    """
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers import aws_storage

    result = StoragePolicy()
    loc = binding.get("location") or {}
    bucket = str(loc.get("bucket") or "")
    # policy.authz.columnRestrictions, checked against Lake Formation's permissions.
    from fluid_build.cli._verify_lf_columns import column_restrictions_dimension

    columns = column_restrictions_dimension(
        expose_id, expose, binding, region=region, factory=factory
    )
    if columns is not None:
        result.dimensions["columnRestrictions"] = columns
    try:
        retention = aws_storage.retention_for(
            {"exposeId": expose_id, "lifecycle": expose.get("lifecycle"), "binding": binding}
        )
    except UnsupportedBindingError as exc:
        result.dimensions["retention"] = {"status": "error", "message": str(exc)}
        retention = None
    try:
        encryption = aws_storage.encryption_for(binding)
    except UnsupportedBindingError as exc:
        result.dimensions["encryption"] = {"status": "error", "message": str(exc)}
        encryption = None
    if retention is None and encryption is None:
        return result
    try:
        s3 = factory("s3", region)
        kms = factory("kms", region) if encryption is not None else None
    except Exception as exc:  # noqa: BLE001 — e.g. boto3 missing; reported per dimension
        message = f"Could not create the AWS clients for the storage checks: {exc}"
        for name, declared in (("retention", retention), ("encryption", encryption)):
            if declared is not None:
                result.dimensions[name] = {"status": "error", "message": message}
        return result
    if retention is not None:
        result.dimensions["retention"] = _retention_dimension(s3, bucket, retention)
    if encryption is not None:
        key_ref = (
            aws_storage.product_key_alias(aws_storage.contract_ident(contract), bucket)
            if encryption.product_key
            else encryption.kms
        )
        dimension, result.kms_key_arn = _encryption_dimension(
            s3, kms, bucket, aws_storage.data_prefix(loc), key_ref
        )
        result.dimensions["encryption"] = dimension
    for name, dimension in result.dimensions.items():
        LOG.info(
            "verify_storage_policy expose=%s dimension=%s status=%s",
            expose_id,
            name,
            dimension["status"],
        )
    return result
