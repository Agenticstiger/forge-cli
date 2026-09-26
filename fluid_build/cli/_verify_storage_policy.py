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
  or object size covers only some objects and is not counted.
* ``encryption`` — ``binding.encryption.kms``. The key is resolved with
  ``kms:DescribeKey`` (the product key by its alias, an alias or ARN as
  written), then every object under the prefix, up to
  :data:`MAX_OBJECTS_CHECKED`, must answer ``s3:HeadObject`` with
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


def _rule_prefix(rule: Mapping[str, Any]) -> Optional[str]:
    """The prefix a rule applies to, or ``None`` when it applies to only some objects there.

    ``Filter: {}`` and a missing filter are the whole bucket (``""``); a rule
    whose filter also names tags or object sizes covers a subset and is not
    counted as applying the retention to the prefix.
    """
    if "Filter" not in rule:
        return str(rule.get("Prefix") or "")
    rule_filter = rule.get("Filter") or {}
    if not isinstance(rule_filter, Mapping):
        return None
    if set(rule_filter) <= {"Prefix"}:
        return str(rule_filter.get("Prefix") or "")
    conjunction = rule_filter.get("And")
    if set(rule_filter) == {"And"} and isinstance(conjunction, Mapping):
        if set(conjunction) <= {"Prefix"}:
            return str(conjunction.get("Prefix") or "")
    return None


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
    covering: List[Tuple[str, str, int]] = []
    for rule in rules:
        if rule.get("Status") != "Enabled":
            continue
        prefix = _rule_prefix(rule)
        days = (rule.get("Expiration") or {}).get("Days")
        if prefix is None or days is None or not retention.prefix.startswith(prefix):
            continue
        covering.append((str(rule.get("ID") or ""), prefix, int(days)))
    if not covering:
        return {
            "status": "fail",
            "expected": expected,
            "actual": {"rules": []},
            "message": (
                f"No enabled lifecycle rule expires the objects under {target}; the contract "
                f"keeps them {retention.period} ({retention.days} day(s)) and then expires them"
            ),
        }
    rule_id, prefix, days = min(covering, key=lambda c: c[2])
    actual = {
        "days": days,
        "rule": rule_id,
        "rule_prefix": prefix,
        "rules": [{"id": i, "prefix": p, "days": d} for i, p, d in covering],
    }
    if days != retention.days:
        return {
            "status": "fail",
            "expected": expected,
            "actual": actual,
            "message": (
                f"The objects under {target} expire after {days} day(s) (lifecycle rule "
                f"{rule_id or 'without an ID'} on {prefix or 'the whole bucket'!r}); the "
                f"contract says {retention.period} ({retention.days} day(s))"
            ),
        }
    return {
        "status": "pass",
        "expected": expected,
        "actual": actual,
        "message": f"The objects under {target} expire after {days} day(s) (rule {rule_id})",
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


def _encryption_dimension(
    s3: Any, kms: Any, bucket: str, prefix: str, key_ref: str
) -> Tuple[Dict[str, Any], Optional[str]]:
    target = f"s3://{bucket}/{prefix}"
    expected: Dict[str, Any] = {"sse": "aws:kms", "kms_key": key_ref}
    try:
        key_arn = str(kms.describe_key(KeyId=key_ref)["KeyMetadata"]["Arn"])
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
