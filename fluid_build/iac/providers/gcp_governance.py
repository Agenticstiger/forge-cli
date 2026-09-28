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

"""Retention, encryption at rest and column access for a GCP BigQuery binding.

THE one derivation, read by the OpenTofu emitter (``iac/providers/gcp.py``, which
turns it into ``hashicorp/google`` resources) and by ``fluid verify``
(``cli/_verify_bigquery_governance.py``, which checks the live dataset and table
against it), so the two cannot disagree. The AWS counterpart is ``aws_storage.py``;
the contract fields are the same, so one contract is governed alike on both clouds.

**Retention** is ``exposes[].lifecycle {retention, expire: true}`` (fluid-schema
0.7.6). On BigQuery it is partition expiration, never a whole-table TTL
(``expiration_time``), which would delete the product itself. The table is
partitioned by day, on the column ``binding.location.partitionBy`` names (one
DATE, TIMESTAMP or DATETIME column) or, without one, by ingestion time; each
partition is deleted ``retention`` after its day ends (BigQuery counts from the
partition boundary), so no row is deleted before the declared period, as with
the S3 rule. dbt-bigquery's ``partition_by`` + ``partition_expiration_days`` is the
same pair. BigQuery cannot partition an existing table, so adding it replaces the
table (see :func:`partition_trigger_input`).

**Encryption** is ``binding.encryption.kms``: ``product`` (the default when the block
is present) is a Cloud KMS key this product creates in the dataset's location
(``google_kms_key_ring`` + ``google_kms_crypto_key``, 90-day rotation), usable by the
project's BigQuery service agent, and made the dataset's default and the table's
key; ``projects/.../cryptoKeys/...`` names an existing key; ``none`` leaves
Google-managed encryption, which BigQuery applies to all data by default. An AWS
alias or ARN is refused here, as the AWS emitter refuses a Cloud KMS name.
dbt-bigquery's ``kms_key_name`` names the same key.

**Column access** is ``exposes[].policy.authz.columnRestrictions`` through
``iac/column_access.py``: a Data Catalog taxonomy per product and dataset with
fine-grained access control on, a policy tag per set of restricted columns that
share their readers, attached through the table schema's ``policyTags`` (dbt's
``policy_tags``), and ``roles/datacatalog.categoryFineGrainedReader`` for exactly
those readers. The resource shapes follow cloud-foundation-fabric's
``data-catalog-policy-tag`` module.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from ..access import normalize_access_grants
from ..base import UnsupportedBindingError
from ..column_access import column_readers, restrictions_for
from ..naming import safe_ident
from ..principals import GCP, gcp_grants, principal_map, resolve_principal

#: ``binding.encryption.kms`` values that are not a key reference.
KMS_PRODUCT = "product"
KMS_NONE = "none"

#: A Cloud KMS crypto key's resource name. The schema carries the same pattern.
_GCP_KEY_RE = re.compile(
    r"projects/(?P<project>[a-z0-9.:-]{1,100})/locations/(?P<location>[a-z0-9-]{1,63})/"
    r"keyRings/(?P<ring>[A-Za-z0-9_-]{1,63})/cryptoKeys/(?P<key>[A-Za-z0-9_-]{1,63})"
)

#: A product key rotates to a new primary version every 90 days, the period CIS
#: GCP 1.10 and terraform-google-modules/kms's examples use.
KEY_ROTATION_PERIOD = "7776000s"

#: The crypto key's name inside the product's key ring.
PRODUCT_KEY_NAME = "bigquery"

#: What the BigQuery service agent needs on the key (BigQuery CMEK guide).
KMS_ENCRYPTER_ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"

#: What a principal needs on a policy tag to read the columns it is attached to.
FINE_GRAINED_READER_ROLE = "roles/datacatalog.categoryFineGrainedReader"

#: Only DAY partitions are emitted: the one granularity retention in days maps onto.
PARTITION_TYPE = "DAY"

#: The column types BigQuery time-unit partitioning accepts.
_PARTITION_COLUMN_TYPES = frozenset({"DATE", "TIMESTAMP", "DATETIME"})

#: ``accessPolicy`` verbs that read the rows; their principals are the expose's readers.
READ_VERBS = frozenset({"read", "select", "query", "admin", "owner"})

_MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class BqRetention:
    """One table's partition expiration."""

    period: str
    days: int
    #: The partition column, or ``None`` for ingestion-time partitioning.
    field: Optional[str]

    @property
    def expiration_ms(self) -> int:
        return self.days * _MS_PER_DAY


@dataclass(frozen=True)
class BqEncryption:
    """One binding's customer-managed key."""

    #: ``product``, or the existing key's resource name.
    kms: str
    #: The Cloud KMS location the key lives in (the dataset's).
    location: str

    @property
    def product_key(self) -> bool:
        return self.kms == KMS_PRODUCT


@dataclass(frozen=True)
class TagGroup:
    """Restricted columns of one table that share their readers: one policy tag."""

    #: Resource-name stem, unique in the module.
    key: str
    display_name: str
    columns: Tuple[str, ...]
    #: IAM members granted the fine-grained reader role on the tag.
    readers: Tuple[str, ...]


def _where(exposure: Mapping[str, Any], index: int) -> str:
    return f"exposes[{exposure.get('exposeId') or exposure.get('id') or index}]"


def dataset_location(loc: Mapping[str, Any]) -> str:
    """The dataset location ``_emit_bigquery`` writes (``US`` when the binding names none)."""
    return str(loc.get("region") or loc.get("location") or "US")


def kms_location(bq_location: str) -> str:
    """The Cloud KMS location a key for a dataset in ``bq_location`` must be in.

    A regional dataset uses the same region; the ``EU`` and ``US`` multi-regions use
    the ``europe`` and ``us`` key locations (BigQuery CMEK guide).
    """
    lowered = bq_location.strip().lower()
    return {"eu": "europe", "us": "us"}.get(lowered, lowered)


def taxonomy_region(bq_location: str) -> str:
    """The Data Catalog location a taxonomy for a dataset in ``bq_location`` must be in.

    Policy tags apply only to tables in the same location as their taxonomy; the
    multi-regions are ``eu`` and ``us``.
    """
    return bq_location.strip().lower()


def _bounded(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, a short hash keeping cut names distinct."""
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{text[: limit - 9]}-{digest}"


def product_key_ring(cid: str, dataset: str) -> str:
    """The key ring this product creates for ``dataset``.

    One per product and dataset: two environments of one product in the same project
    and location (whose overlays name different datasets) do not share a key, as the
    AWS emitter keys one alias per bucket. Key ring names allow letters, digits,
    ``_`` and ``-``, at most 63. A key ring cannot be deleted on GCP, so the name is
    stable and a re-apply adopts it (``GcpIacPlugin.discover_imports``).
    """
    return _bounded(re.sub(r"[^A-Za-z0-9_-]", "-", f"fluid-{cid}-{dataset}"), 63)


def product_key_name(project: str, location: str, ring: str) -> str:
    """The resource name of the product key, as BigQuery reports ``kmsKeyName``."""
    return f"projects/{project}/locations/{location}/keyRings/{ring}/cryptoKeys/{PRODUCT_KEY_NAME}"


def _display(text: str, limit: int = 200) -> str:
    """A Data Catalog display name: letters, digits, ``_``, ``-`` and spaces."""
    cleaned = re.sub(r"[^A-Za-z0-9_\- ]", "_", text).strip() or "fluid"
    return _bounded(cleaned, limit)


def taxonomy_display_name(cid: str, dataset: str) -> str:
    """The taxonomy's display name, unique per project and location as Data Catalog requires."""
    return _display(f"fluid {cid} {dataset}")


def _period_days(period: Any, *, where: str) -> int:
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
            f"{where}.lifecycle.retention is {period!r}: BigQuery partition expiration on "
            "daily partitions needs a period of at least one day.",
            ("Set a period of at least P1D, or drop expire: true.",),
        )
    return days


def _expire_requested(exposure: Mapping[str, Any]) -> bool:
    lifecycle = exposure.get("lifecycle")
    return isinstance(lifecycle, Mapping) and lifecycle.get("expire") is True


def retention_for(
    exposure: Mapping[str, Any], index: int = 0, *, is_view: bool = False
) -> Optional[BqRetention]:
    """The partition expiration ``exposure`` asks for, or ``None``.

    ``None`` unless ``lifecycle.expire`` is exactly ``true``. A missing or invalid
    period, a view (it stores nothing to expire) and an unusable partition column
    are refused.
    """
    if not _expire_requested(exposure):
        return None
    where = _where(exposure, index)
    if is_view:
        raise UnsupportedBindingError(
            "retention-view",
            f"{where}.lifecycle.expire is true on a BigQuery view, which stores no rows to "
            "expire.",
            ("Set expire: true on the expose of the table the view reads, or drop it here.",),
        )
    period = (exposure.get("lifecycle") or {}).get("retention")
    if not period:
        raise UnsupportedBindingError(
            "retention-period",
            f"{where}.lifecycle.expire is true but lifecycle.retention is not set, so there "
            "is no period to expire partitions after.",
            ("Set lifecycle.retention (e.g. P30D), or drop expire: true.",),
        )
    days = _period_days(period, where=where)
    loc = (exposure.get("binding") or {}).get("location") or {}
    partition_by = loc.get("partitionBy") if isinstance(loc, Mapping) else None
    field: Optional[str] = None
    if partition_by:
        columns = list(partition_by) if isinstance(partition_by, (list, tuple)) else []
        if len(columns) != 1 or not isinstance(columns[0], str):
            raise UnsupportedBindingError(
                "retention-partition-column",
                f"{where}.binding.location.partitionBy is {partition_by!r}; a BigQuery table "
                "is partitioned by at most one column.",
                (
                    "Name one DATE, TIMESTAMP or DATETIME column, or drop partitionBy to "
                    "partition by ingestion time.",
                ),
            )
        from .gcp import _bq_type

        schema = (exposure.get("contract") or {}).get("schema") or []
        declared = {c.get("name"): c for c in schema if isinstance(c, Mapping)}
        column = declared.get(columns[0])
        if column is None or _bq_type(column.get("type")) not in _PARTITION_COLUMN_TYPES:
            raise UnsupportedBindingError(
                "retention-partition-column",
                f"{where}.binding.location.partitionBy names {columns[0]!r}, which is not a "
                "DATE, TIMESTAMP or DATETIME column of the expose's schema, so BigQuery "
                "cannot partition by it.",
                (
                    "Name a date or timestamp column, or drop partitionBy to partition by "
                    "ingestion time.",
                ),
            )
        field = columns[0]
    return BqRetention(period=str(period), days=days, field=field)


def partition_trigger_input(retention: BqRetention) -> Dict[str, str]:
    """What forces the table's replacement: the partitioning's shape, not its expiry.

    BigQuery cannot partition an existing table, and the provider plans adding
    ``time_partitioning`` (without a column) as an in-place update the API then
    refuses. A ``terraform_data`` holding this value is the table's
    ``replace_triggered_by`` (the OpenTofu docs' pattern for a plain value): created
    for a table that exists, or changed, it plans the table's replacement, which the
    data-loss gate refuses without ``--allow-data-loss``. The period is left out, so a
    new retention is an in-place change of ``expiration_ms``.
    """
    return {"type": PARTITION_TYPE, "field": retention.field or ""}


def encryption_for(
    binding: Mapping[str, Any], bq_location: str, *, where: str = "binding"
) -> Optional[BqEncryption]:
    """The key ``binding`` asks for; ``None`` for no block or ``kms: none``."""
    block = binding.get("encryption")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise UnsupportedBindingError(
            "encryption-kms",
            f"{where}.encryption must be a mapping, got {type(block).__name__}.",
            ("Write binding.encryption as {kms: product}.",),
        )
    kms = block.get("kms", KMS_PRODUCT)
    location = kms_location(bq_location)
    if kms == KMS_NONE:
        return None
    if kms == KMS_PRODUCT:
        return BqEncryption(kms=KMS_PRODUCT, location=location)
    text = kms if isinstance(kms, str) else ""
    match = _GCP_KEY_RE.fullmatch(text)
    if not match:
        aws_shaped = text.startswith("alias/") or text.startswith("arn:")
        raise UnsupportedBindingError(
            "encryption-kms",
            f"{where}.encryption.kms is {kms!r}"
            + (", an AWS KMS key, on a gcp binding" if aws_shaped else "")
            + "; on GCP it must be 'product', 'none' or a Cloud KMS key name "
            "(projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>).",
            (
                "Omit kms (or set 'product') to have this product create its own key.",
                "Name an existing Cloud KMS key by its resource name.",
            ),
        )
    if match.group("location") != location:
        raise UnsupportedBindingError(
            "encryption-kms-location",
            f"{where}.encryption.kms is in location {match.group('location')!r}, but the "
            f"dataset is in {bq_location!r}; BigQuery uses only a key in the dataset's "
            f"location ({location!r}).",
            (f"Name a key in {location!r}, or use kms: product.",),
        )
    return BqEncryption(kms=text, location=location)


def expose_readers(
    contract: Mapping[str, Any], binding: Mapping[str, Any], *, where: str
) -> FrozenSet[str]:
    """Every IAM member that reads the expose: the ``accessPolicy`` read grants, mapped."""
    members: set[str] = set()
    for grant in gcp_grants(normalize_access_grants(contract), binding, where=where):
        if READ_VERBS & set(grant.permissions):
            members.add(f"{grant.principal_type}:{grant.principal}")
    return frozenset(members)


def tag_groups(
    contract: Mapping[str, Any],
    exposure: Mapping[str, Any],
    cid: str,
    dataset: str,
    table: str,
    index: int = 0,
) -> List[TagGroup]:
    """The policy tags one table's column restrictions become, in schema order."""
    restrictions = restrictions_for(exposure, index)
    if not restrictions:
        return []
    binding = exposure.get("binding") or {}
    where = f"{_where(exposure, index)}.policy.authz.columnRestrictions"
    mapping = principal_map(binding)

    def resolve(principal: str) -> Tuple[str, ...]:
        return resolve_principal(principal, mapping, platform=GCP, where=where)

    readers = expose_readers(contract, binding, where=f"{_where(exposure, index)} accessPolicy")
    by_column = column_readers(exposure, restrictions, resolve, readers, where=where)
    grouped: Dict[FrozenSet[str], List[str]] = {}
    for column, allowed in by_column.items():
        grouped.setdefault(allowed, []).append(column)
    groups: List[TagGroup] = []
    for allowed, columns in grouped.items():
        groups.append(
            TagGroup(
                key=safe_ident(f"{cid}_{dataset}_{table}_{columns[0]}"),
                display_name=_display(f"{table} {' '.join(columns)}"),
                columns=tuple(columns),
                readers=tuple(sorted(allowed)),
            )
        )
    return groups


def validate_bigquery_governance(
    contract: Mapping[str, Any], exposure: Mapping[str, Any], index: int, *, is_view: bool
) -> None:
    """Every derivation above for one BigQuery expose; raises what the emitter would."""
    binding = exposure.get("binding") or {}
    loc = binding.get("location") or {}
    where = _where(exposure, index)
    retention_for(exposure, index, is_view=is_view)
    encryption_for(binding, dataset_location(loc), where=f"{where}.binding")
    gcp_grants(normalize_access_grants(contract), binding, where=f"{where} accessPolicy")
    if is_view and restrictions_for(exposure, index):
        raise UnsupportedBindingError(
            "column-restriction-view",
            f"{where}.policy.authz.columnRestrictions is set on a BigQuery view; policy tags "
            "attach to table columns, so restrict the columns of the table the view reads.",
            (),
        )
    tag_groups(contract, exposure, "c", "d", "t", index)


def refuse_unsupported_target(exposure: Mapping[str, Any], index: int, target: str) -> None:
    """A GCP expose that is not a BigQuery table must not carry a policy it would drop.

    The GCS, Pub/Sub and Iceberg-storage emitters write no lifecycle rule, key or column
    control, so a contract asking for one there is refused rather than applied without it.
    """
    where = _where(exposure, index)
    binding = exposure.get("binding") or {}
    asked = []
    if _expire_requested(exposure):
        asked.append("lifecycle.expire")
    block = binding.get("encryption")
    if isinstance(block, Mapping) and block.get("kms", KMS_PRODUCT) != KMS_NONE:
        asked.append("binding.encryption")
    if restrictions_for(exposure, index):
        asked.append("policy.authz.columnRestrictions")
    if asked:
        raise UnsupportedBindingError(
            "gcp-governance-unsupported-target",
            f"{where} is a GCP {target} binding and declares {', '.join(asked)}, which the "
            "GCP emitter applies only to BigQuery tables. It would not be enforced.",
            ("Move the policy to a BigQuery table expose, or remove it from this one.",),
        )


__all__ = [
    "BqEncryption",
    "BqRetention",
    "FINE_GRAINED_READER_ROLE",
    "KEY_ROTATION_PERIOD",
    "KMS_ENCRYPTER_ROLE",
    "PARTITION_TYPE",
    "PRODUCT_KEY_NAME",
    "TagGroup",
    "dataset_location",
    "encryption_for",
    "expose_readers",
    "kms_location",
    "partition_trigger_input",
    "product_key_name",
    "product_key_ring",
    "refuse_unsupported_target",
    "retention_for",
    "tag_groups",
    "taxonomy_display_name",
    "taxonomy_region",
    "validate_bigquery_governance",
]
