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

"""Data retention and encryption at rest for an AWS S3 binding: what the contract asks for.

THE one derivation, read by the OpenTofu emitter (``iac/providers/aws.py``,
which turns it into resources) and by ``fluid verify``
(``cli/_verify_storage_policy.py``, which checks the live bucket against it),
so the two cannot disagree about the prefix, the number of days or the key.

**Retention** is the expose's existing ``lifecycle.retention`` (an ISO-8601
duration, in every bundled schema since 0.7.1), applied only when the same
block says ``expire: true`` (new in fluid-schema 0.7.6). Without the opt-in the
field stays what it has always been, a declaration: a contract that has carried
``retention: P7Y`` for years does not start deleting objects because forge-cli
was upgraded. Years and months are counted as 365 and 30 days, the approximation
the run-state retention sweeper already uses (``build_runners/_retention.py``),
and a part of a day rounds UP, since an S3 lifecycle rule counts whole days and
must never delete an object before the declared period.

**Encryption** is ``binding.encryption.kms`` (new in 0.7.6): ``product`` (the
default when the block is present) is a customer managed KMS key this product
creates for each bucket it owns; an ``alias/...`` or ``arn:...:kms:...`` names a
key that already exists; ``none`` leaves the bucket's encryption to AWS (S3
encrypts every new object with SSE-S3 by default). An absent block changes
nothing: the emit stays byte-identical to the one before these fields existed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ..base import UnsupportedBindingError
from ..naming import safe_ident

#: ``binding.encryption.kms`` values that are not a key reference.
KMS_PRODUCT = "product"
KMS_NONE = "none"

#: An alias as KMS spells it (``alias/`` then letters, digits, ``/``, ``_``,
#: ``-``) and a key or alias ARN. The schema carries the same patterns.
_KMS_ALIAS_RE = re.compile(r"alias/[A-Za-z0-9/_-]{1,250}")
_KMS_ARN_RE = re.compile(
    r"arn:aws[a-z-]*:kms:[a-z0-9-]+:[0-9]{12}:(key|alias)/[A-Za-z0-9/_-]{1,250}"
)

#: The AWS managed S3 key. Lake Formation cannot use its service-linked role on
#: a location encrypted with an AWS managed key ("you can't use the Lake
#: Formation service-linked role. You must use a custom role", LF developer
#: guide, *Registering an encrypted Amazon S3 location*), and forge-cli
#: registers with the service-linked role.
AWS_MANAGED_S3_ALIASES = frozenset({"alias/aws/s3"})

#: A product key is scheduled for deletion this many days after ``tofu
#: destroy``: the minimum KMS allows (7 to 30, default 30). Until then
#: ``kms:CancelKeyDeletion`` brings it back. Destroy also force-destroys the
#: bucket, so no object outlives its key. Dropping ``binding.encryption`` from a
#: contract whose bucket stays would leave objects under a key pending
#: deletion; ``fluid apply`` refuses any plan that removes a resource without
#: ``--allow-data-loss`` (``_apply_opentofu_engine._data_loss_blocked``).
KEY_DELETION_WINDOW_DAYS = 7

#: A noncurrent object version (a versioning-enabled bucket keeps one per
#: overwrite, and one when the expiration adds its delete marker) is removed
#: this many days after it stops being current, S3's minimum. With it, nothing
#: under the prefix outlives the retention period by more than a day; a longer
#: value would keep a version superseded late in its life for up to twice the
#: period. S3 then removes the expired object delete markers itself, because the
#: rule's expiration counts ``Days`` (S3 User Guide, lifecycle configuration
#: examples).
NONCURRENT_VERSION_DAYS = 1

#: ``fluid verify``'s default Athena results prefix under the binding's bucket
#: (``cli/_verify_athena.RESULTS_PREFIX``; a test pins the two equal, so the
#: ``cli`` module need not import the ``iac`` package). An emitted lifecycle
#: configuration replaces every rule on the bucket, an operator's rule for these
#: files included, so it carries one for them.
VERIFY_RESULTS_PREFIX = ".fluid/athena-results/"

#: Parts of a multipart upload that never completed are bytes of the product
#: too; they are removed after this many days, or after the retention period
#: when that is shorter.
ABORT_INCOMPLETE_UPLOAD_DAYS = 7


@dataclass(frozen=True)
class Retention:
    """One expose's lifecycle rule: objects under ``prefix`` expire after ``days``."""

    #: The contract's value, e.g. ``P30D``.
    period: str
    days: int
    #: Bucket-relative key prefix, always ending in ``/``.
    prefix: str
    #: The rule's ``ID`` in the bucket's lifecycle configuration.
    rule_id: str


@dataclass(frozen=True)
class Encryption:
    """One binding's encryption at rest."""

    #: ``product``, or the ``alias/...`` / ARN of an existing key.
    kms: str

    @property
    def product_key(self) -> bool:
        return self.kms == KMS_PRODUCT


def contract_ident(contract: Mapping[str, Any]) -> str:
    """The contract's resource-name stem, exactly as the AWS emitter derives it."""
    return safe_ident(contract.get("id") or contract.get("name") or "product")


def data_prefix(loc: Mapping[str, Any]) -> str:
    """The bucket-relative prefix the binding's objects land under, ending in ``/``.

    The same path ``normalize_location`` gives the Glue table (``location.path``,
    else ``{database}/{table}/``), made a prefix the way the duckdb runner makes
    it one (``_object_store_uri``): an S3 prefix is a plain string match, so
    ``orders`` would also match ``orders_archive/``. ``""`` when the binding
    names neither a path nor a database and table.
    """
    path = loc.get("path")
    if path is None:
        database, table = loc.get("database"), loc.get("table")
        path = f"{database}/{table}/" if database and table else ""
    stripped = str(path).strip("/")
    return f"{stripped}/" if stripped else ""


def _period_days(period: Any, *, where: str) -> int:
    # Function-local import, as ``util/freshness.py`` does for the other
    # parser: importing ``fluid_build.build_runners`` eagerly would put the
    # whole runner package on the IaC import path.
    from fluid_build.build_runners._retention import parse_iso_duration

    try:
        span = parse_iso_duration(str(period))
    except ValueError:
        raise UnsupportedBindingError(
            "retention-period",
            f"{where}.lifecycle.retention is {period!r}, which is not an ISO-8601 duration.",
            ("Write the period as an ISO-8601 duration, e.g. P30D, P12W or P7Y.",),
        ) from None
    days = math.ceil(span.total_seconds() / 86400)
    if days < 1:
        raise UnsupportedBindingError(
            "retention-period",
            f"{where}.lifecycle.retention is {period!r}: an S3 lifecycle rule expires "
            "objects after at least one day, so a zero period cannot be applied.",
            ("Set a period of at least P1D, or drop expire: true.",),
        )
    return days


def retention_for(exposure: Mapping[str, Any], index: int = 0) -> Optional[Retention]:
    """The lifecycle rule ``exposure`` asks for, or ``None`` when it asks for none.

    ``None`` unless ``exposure.lifecycle.expire`` is exactly ``true``. With it,
    a missing or invalid period and a binding with no prefix to scope the rule
    to fail closed: forge-cli never emits a rule that expires a whole bucket.
    """
    lifecycle = exposure.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or lifecycle.get("expire") is not True:
        return None
    expose_id = exposure.get("exposeId") or exposure.get("id")
    where = f"exposes[{expose_id or index}]"
    period = lifecycle.get("retention")
    if not period:
        raise UnsupportedBindingError(
            "retention-period",
            f"{where}.lifecycle.expire is true but lifecycle.retention is not set, so there "
            "is no period to expire objects after.",
            ("Set lifecycle.retention (e.g. P30D), or drop expire: true.",),
        )
    days = _period_days(period, where=where)
    binding = exposure.get("binding") or {}
    loc = binding.get("location") or {}
    prefix = data_prefix(loc)
    if not prefix:
        raise UnsupportedBindingError(
            "retention-requires-prefix",
            f"{where}.lifecycle.expire is true but the binding names no location.path (nor "
            "a database and table to derive one from), so the lifecycle rule would expire "
            "every object in the bucket.",
            ("Add binding.location.path, the prefix this expose's objects land under.",),
        )
    rule_id = "fluid-retention-" + (safe_ident(expose_id) if expose_id else str(index))
    return Retention(period=str(period), days=days, prefix=prefix, rule_id=rule_id)


def encryption_for(binding: Mapping[str, Any]) -> Optional[Encryption]:
    """The encryption ``binding`` asks for; ``None`` for no block or ``kms: none``."""
    block = binding.get("encryption")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise UnsupportedBindingError(
            "encryption-kms",
            f"binding.encryption must be a mapping, got {type(block).__name__}.",
            ("Write binding.encryption as {kms: product}.",),
        )
    kms = block.get("kms", KMS_PRODUCT)
    if kms == KMS_NONE:
        return None
    if kms == KMS_PRODUCT:
        return Encryption(kms=KMS_PRODUCT)
    text = str(kms) if isinstance(kms, str) else ""
    if not (_KMS_ALIAS_RE.fullmatch(text) or _KMS_ARN_RE.fullmatch(text)):
        raise UnsupportedBindingError(
            "encryption-kms",
            f"binding.encryption.kms is {kms!r}; it must be 'product', 'none', a KMS alias "
            "(alias/...) or a KMS key or alias ARN.",
            (
                "Omit kms (or set 'product') to have this product create its own key.",
                "Name an existing key by alias/<name> or by its ARN.",
            ),
        )
    return Encryption(kms=text)


def product_key_alias(cid: str, bucket: str) -> str:
    """The alias of the key this product creates for ``bucket``.

    One key per product and bucket it owns: bucket names are globally unique,
    so two environments of one product in the same account and region (whose
    overlays name different buckets) get different aliases instead of
    colliding on one. KMS allows letters, digits, ``/``, ``_`` and ``-`` in an
    alias, so a bucket's dots become ``-``.
    """

    def clean(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "-", text)

    return f"alias/fluid/{clean(cid)}/{clean(bucket)}"
