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
partition boundary, not from when the rows were written). Without ``partitionBy``
the partition day is the day the rows landed, so no row is deleted sooner than
``retention`` after it was written: the S3 rule's semantics, and the parity default.
With ``partitionBy`` the day is the column's value, so retention is the age of the
event, not of the load: a backfill of rows whose date is already older than
``retention`` lands in expired partitions and BigQuery deletes it at once, which
the S3 rule (counting from the write) never does. Naming the column is the opt-in
to that; the emitter logs it. dbt-bigquery's ``partition_by`` +
``partition_expiration_days`` is the same pair. BigQuery cannot partition an
existing table, so adding it replaces the table (see :func:`partition_trigger_input`).

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

**Masking** is a restriction with ``access: mask`` (fluid-schema 0.7.6): the masked
columns get a policy tag of their own (a tag's data policy applies to every column
the tag carries, so they never share one with a denied column), a data policy on it
(``google_bigquery_datapolicy_data_policy``, ``DATA_MASKING_POLICY`` with the rule's
predefined expression), and ``roles/bigquerydatapolicy.maskedReader`` on the data
policy for exactly the masked principals. A masked principal reads the same column,
masked; a fine-grained reader of the tag reads it raw (BigQuery's column data
masking). The grant takes a minute or two to apply.

**Row filters** are ``exposes[].policy.authz.rowFilters`` through ``iac/row_access.py``:
a row access policy per filter (``google_bigquery_row_access_policy``), and one that
selects every row (``TRUE``) for every other reader and writer of the expose, since a
principal no row access policy names reads no row (BigQuery row-level security).

**Labels**: the contract's and the expose's ``labels``, and its classification,
jurisdiction and regulatory framework, go onto the dataset, table and key
(:func:`governance_labels`), so the regulation is on the resource and can be found
there, not only in the contract.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

from ..access import normalize_access_grants
from ..base import UnsupportedBindingError
from ..column_access import authz_readers, column_masks, column_readers, restrictions_for
from ..governance_labels import governance_labels
from ..naming import safe_ident
from ..principals import GCP, gcp_grants, principal_map, resolve_principal
from ..provider_match import canonical_cloud
from ..row_access import row_filters_for

LOG = logging.getLogger(__name__)

#: ``binding.encryption.kms`` values that are not a key reference.
KMS_PRODUCT = "product"
KMS_NONE = "none"

#: A Cloud KMS crypto key's resource name. The schema carries the same pattern.
_GCP_KEY_RE = re.compile(
    r"projects/(?P<project>[a-z0-9.:-]{1,100})/locations/(?P<location>[a-z0-9-]{1,63})/"
    r"keyRings/(?P<ring>[A-Za-z0-9_-]{1,63})/cryptoKeys/(?P<key>[A-Za-z0-9_-]{1,63})"
)

#: A Cloud KMS / BigQuery location id (``europe-west1``, ``us``, ``europe``).
_LOCATION_RE = re.compile(r"[a-z0-9-]{1,63}")

#: A product key rotates to a new primary version every 90 days, the period CIS
#: GCP 1.10 and terraform-google-modules/kms's examples use.
KEY_ROTATION_PERIOD = "7776000s"

#: The crypto key's name inside the product's key ring.
PRODUCT_KEY_NAME = "bigquery"

#: What the BigQuery service agent needs on the key (BigQuery CMEK guide).
KMS_ENCRYPTER_ROLE = "roles/cloudkms.cryptoKeyEncrypterDecrypter"

#: What a principal needs on a policy tag to read the columns it is attached to.
FINE_GRAINED_READER_ROLE = "roles/datacatalog.categoryFineGrainedReader"

#: Lets a principal read a policy tag's columns masked, by the tag's data policy.
MASKED_READER_ROLE = "roles/bigquerydatapolicy.maskedReader"

#: The longest data policy id BigQuery takes. It refuses a longer one ("should only contain
#: letters, numbers and underscores while under 200 characters"; measured 5 October 2026:
#: 200 characters accepted, 300 refused).
DATA_POLICY_ID_MAX = 199

#: ``columnRestrictions[].mask`` rule -> BigQuery data policy ``predefined_expression``.
MASK_EXPRESSIONS = {
    "last_four": "LAST_FOUR_CHARACTERS",
    "first_four": "FIRST_FOUR_CHARACTERS",
    "nullify": "ALWAYS_NULL",
}

#: The row access policy that lets every reader no filter names read every row.
ALL_ROWS_POLICY_ID = "fluid_all_rows"

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
    #: The ``tags`` and ``labels`` of the restrictions naming these columns, for the
    #: policy tag's description (a policy tag has no labels of its own).
    rule_tags: Tuple[str, ...] = ()
    rule_labels: Tuple[Tuple[str, str], ...] = ()
    #: The masking rule of a masked group (one of ``column_access.MASK_RULES``).
    mask: Optional[str] = None
    #: IAM members granted the masked reader role on the group's data policy.
    masked_readers: Tuple[str, ...] = ()

    @property
    def data_policy_id(self) -> str:
        """The data policy's id: ``[A-Za-z0-9_]``, unique in the project and location.

        A longer id than BigQuery takes keeps its first characters and ends in a hash of
        the whole, so two long ids stay distinct.
        """
        ident = safe_ident(f"{self.key}_mask")
        if len(ident) <= DATA_POLICY_ID_MAX:
            return ident
        digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]
        return f"{ident[: DATA_POLICY_ID_MAX - len(digest) - 1]}_{digest}"

    @property
    def description(self) -> str:
        """The policy tag's description: what it restricts, and the rules' tags."""
        text = (
            f"Restricted columns {', '.join(self.columns)}: readable only by the principals "
            "granted the fine-grained reader role on this tag."
        )
        if self.mask:
            text += (
                f" Masked ({self.mask}) for the principals granted the masked reader role on "
                f"its data policy {self.data_policy_id}."
            )
        if self.rule_tags:
            text += f" Tags: {', '.join(self.rule_tags)}."
        if self.rule_labels:
            text += " Labels: " + ", ".join(f"{k}={v}" for k, v in self.rule_labels) + "."
        # Data Catalog allows at most 2000 bytes.
        return _bounded(text, 1900)


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
        LOG.info(
            "bigquery_retention_event_time %s: partitioned by %s, so each row expires %s "
            "after the date in %s, not after it was written (a backfill of older rows is "
            "deleted at once). Drop binding.location.partitionBy to count from landing, "
            "as the S3 rule does.",
            where,
            field,
            period,
            field,
        )
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
    if not _LOCATION_RE.fullmatch(location):
        # The location becomes a path segment of the key ring's import id, so a
        # value that is not a location must not reach it (it could name another
        # project's key ring for the apply to adopt).
        raise UnsupportedBindingError(
            "encryption-kms-location",
            f"{where}.location.region is {bq_location!r}, which is not a BigQuery "
            "location, so no Cloud KMS location can be derived for its key.",
            ("Set binding.location.region to the dataset's location, e.g. europe-west1.",),
        )
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
    contract: Mapping[str, Any],
    binding: Mapping[str, Any],
    *,
    where: str,
    exposure: Optional[Mapping[str, Any]] = None,
    readers_where: str = "policy.authz.readers",
    any_grant: bool = False,
) -> FrozenSet[str]:
    """Every IAM member that reads the expose, mapped through ``binding.principals``.

    The ``accessPolicy`` read grants (which the emitter grants on the dataset) and
    the expose's own ``policy.authz.readers`` (which it does not: their table access
    is managed elsewhere). Both are readers a restriction narrows; leaving the second
    out gave a restricted column no reader at all, so a deny for one group locked it
    for everyone. With ``any_grant``, every member with a grant of any verb.
    """
    members: set[str] = set()
    for grant in gcp_grants(normalize_access_grants(contract), binding, where=where):
        if any_grant or READ_VERBS & set(grant.permissions):
            members.add(f"{grant.principal_type}:{grant.principal}")
    if exposure is not None:
        mapping = principal_map(binding)
        for reader in authz_readers(exposure):
            members.update(resolve_principal(reader, mapping, platform=GCP, where=readers_where))
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

    readers = expose_readers(
        contract,
        binding,
        where=f"{_where(exposure, index)} accessPolicy",
        exposure=exposure,
        readers_where=f"{_where(exposure, index)}.policy.authz.readers",
    )
    if not readers:
        # The AWS emitter refuses a restriction with no Lake Formation grant to
        # narrow; the GCP one would attach a policy tag nobody may read, so a deny
        # for one principal would lock the columns for every principal.
        raise UnsupportedBindingError(
            "column-restriction-no-readers",
            f"{where} restricts columns, but the expose has no reader on this gcp binding: "
            "no accessPolicy grant with read, select or query and no policy.authz.readers. "
            "A policy tag is readable only by the readers it names, so the restricted "
            "columns would be unreadable by everyone, not only by the principals the "
            "restrictions name.",
            (
                "Add the expose's readers: accessPolicy grants with read (forge-cli grants "
                "them on the dataset), or policy.authz.readers for readers whose table "
                "access is managed elsewhere.",
                "Or remove the restriction.",
            ),
        )
    by_column = column_readers(exposure, restrictions, resolve, readers, where=where)
    masks = column_masks(exposure, restrictions, resolve, readers, where=where)
    # A tag's data policy masks every column the tag carries, so a masked column
    # shares a tag only with columns masked alike for the same principals.
    grouped: Dict[Tuple[FrozenSet[str], Optional[str], FrozenSet[str]], List[str]] = {}
    for column, allowed in by_column.items():
        rule, masked = masks.get(column, (None, frozenset()))
        grouped.setdefault((allowed, rule, masked), []).append(column)
    groups: List[TagGroup] = []
    for (allowed, rule, masked), columns in grouped.items():
        rules = [r for r in restrictions if set(r.columns) & set(columns)]
        rule_tags = tuple(dict.fromkeys(t for r in rules for t in r.tags))
        rule_labels = tuple(sorted({pair for r in rules for pair in r.labels}))
        groups.append(
            TagGroup(
                key=safe_ident(f"{cid}_{dataset}_{table}_{columns[0]}"),
                display_name=_display(f"{table} {' '.join(columns)}"),
                columns=tuple(columns),
                readers=tuple(sorted(allowed)),
                rule_tags=rule_tags,
                rule_labels=rule_labels,
                mask=rule,
                masked_readers=tuple(sorted(masked)),
            )
        )
    return groups


@dataclass(frozen=True)
class RowPolicy:
    """One BigQuery row access policy of a table."""

    #: Resource-name stem, unique in the module.
    key: str
    policy_id: str
    predicate: str
    grantees: Tuple[str, ...]


def expose_members(
    contract: Mapping[str, Any], exposure: Mapping[str, Any], binding: Mapping[str, Any], index: int
) -> FrozenSet[str]:
    """Every IAM member with any grant on the expose, readers and writers alike.

    The members a row access policy must name for them to see rows: a writer's own
    checks (``fluid verify``, the drift gate) read the table too.
    """
    at = _where(exposure, index)
    return expose_readers(
        contract,
        binding,
        where=f"{at} accessPolicy",
        exposure=exposure,
        readers_where=f"{at}.policy.authz.readers",
        any_grant=True,
    )


def row_policies(
    contract: Mapping[str, Any],
    exposure: Mapping[str, Any],
    cid: str,
    table: str,
    index: int = 0,
) -> List[RowPolicy]:
    """The row access policies one table's row filters become, filters first.

    Each filter's grantees are every identity of its principal, whatever it holds on
    the expose: a row access policy never grants the table, so naming an identity
    narrows only what it reads through whichever grant it has, a writer's included
    (``roles/bigquery.dataEditor`` reads rows too). The last policy selects every row
    for every other member with a grant on the expose, and never for an identity a
    filter names: BigQuery shows a principal the union of the policies naming it, so
    a filtered identity in it would read every row.
    """
    filters = row_filters_for(exposure, index)
    if not filters:
        return []
    binding = exposure.get("binding") or {}
    where = f"{_where(exposure, index)}.policy.authz.rowFilters"
    mapping = principal_map(binding)
    members = expose_members(contract, exposure, binding, index)
    out: List[RowPolicy] = []
    filtered: set[str] = set()
    for flt in filters:
        identities = set(resolve_principal(flt.principal, mapping, platform=GCP, where=where))
        filtered.update(identities)
        if not identities:
            LOG.info(
                "row_filter_no_identity %s principal=%s: binding.principals maps it to no "
                "identity on GCP, so there is no one to filter",
                where,
                flt.principal,
            )
            continue
        if not identities & members:
            LOG.warning(
                "row_filter_principal_without_grant %s principal=%s: the contract grants it "
                "nothing on this expose; the filter narrows what it reads through any other "
                "grant",
                where,
                flt.principal,
            )
        out.append(
            RowPolicy(
                key=safe_ident(f"{cid}_{table}_{flt.name}"),
                policy_id=flt.name,
                predicate=flt.where,
                grantees=tuple(sorted(identities)),
            )
        )
    rest = sorted(members - filtered)
    if rest:
        out.append(
            RowPolicy(
                key=safe_ident(f"{cid}_{table}_{ALL_ROWS_POLICY_ID}"),
                policy_id=ALL_ROWS_POLICY_ID,
                predicate="TRUE",
                grantees=tuple(rest),
            )
        )
    return out


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
    if row_filters_for(exposure, index) and is_view:
        raise UnsupportedBindingError(
            "row-filter-view",
            f"{where}.policy.authz.rowFilters is set on a BigQuery view; row access policies "
            "attach to tables, so filter the rows of the table the view reads.",
            (),
        )
    tag_groups(contract, exposure, "c", "d", "t", index)


def gcp_owned(binding: Mapping[str, Any]) -> bool:
    """Is ``binding`` one the GCP emitter owns: platform gcp, or no cloud named at all?

    ``resolve_gcp_target`` resolves an explicit format (``iceberg``, say) whatever the
    platform, so an AWS Iceberg binding resolves to GCP Iceberg storage. Governance
    is dispatched on the platform first: an ``aws`` binding is the AWS emitter's,
    and its policies are checked against what that emitter writes.
    """
    if not isinstance(binding, Mapping):
        return False
    cloud = canonical_cloud(binding.get("platform")) or canonical_cloud(binding.get("provider"))
    return cloud in ("", GCP)


def refuse_mixed_dataset_encryption(contract: Mapping[str, Any]) -> None:
    """Refuse BigQuery tables of one dataset that declare different keys.

    The dataset's default key is the key of the tables in it: BigQuery gives a table
    created without one the dataset's default. A table declared unkeyed in a keyed
    dataset therefore gets the key, the provider then plans removing it, and
    ``encryption_configuration`` is ForceNew, so every later plan replaces the table
    (terraform-provider-google issue 26193, whose workaround is the same key on the
    table). With the unkeyed table first, the dataset got no default at all and
    ``fluid verify`` failed it on every run. One dataset, one key. A view stores no
    rows and carries no key, so an unkeyed view is left out; a keyed one sets the
    dataset's default and must agree too.
    """
    from .gcp import BIGQUERY_TABLE, BIGQUERY_VIEW, resolve_gcp_target

    seen: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for index, exposure in enumerate(contract.get("exposes") or []):
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        target = resolve_gcp_target(binding) if gcp_owned(binding) else None
        if target not in (BIGQUERY_TABLE, BIGQUERY_VIEW):
            continue
        loc = binding.get("location") or {}
        where = _where(exposure, index)
        encryption = encryption_for(binding, dataset_location(loc), where=f"{where}.binding")
        key = encryption.kms if encryption is not None else KMS_NONE
        if target == BIGQUERY_VIEW and encryption is None:
            continue
        dataset = (str(loc.get("project") or ""), str(loc.get("dataset") or "default"))
        first = seen.setdefault(dataset, (key, where))
        if first[0] != key:
            raise UnsupportedBindingError(
                "encryption-kms-mixed-dataset",
                f"{where} and {first[1]} are BigQuery tables in dataset {dataset[1]!r} with "
                f"different encryption (kms: {key!r} and {first[0]!r}). The dataset's "
                "default key is the key of every table in it: BigQuery gives an unkeyed "
                "table the default, and the provider would then replace that table on "
                "every apply.",
                (
                    "Declare the same binding.encryption on every table of the dataset.",
                    "Or put the tables with a different key in a dataset of their own.",
                ),
            )


def refuse_unsupported_target(exposure: Mapping[str, Any], index: int, target: str) -> None:
    """A GCP expose that is not a BigQuery table must not carry a policy it would drop.

    The GCS, Pub/Sub and Iceberg-storage emitters write no lifecycle rule, key, column
    control or row filter, so a contract asking for one there is refused rather than
    applied without it.
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
    if row_filters_for(exposure, index):
        asked.append("policy.authz.rowFilters")
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
    "ALL_ROWS_POLICY_ID",
    "FINE_GRAINED_READER_ROLE",
    "KEY_ROTATION_PERIOD",
    "KMS_ENCRYPTER_ROLE",
    "MASKED_READER_ROLE",
    "MASK_EXPRESSIONS",
    "PARTITION_TYPE",
    "PRODUCT_KEY_NAME",
    "RowPolicy",
    "TagGroup",
    "dataset_location",
    "encryption_for",
    "expose_members",
    "expose_readers",
    "gcp_owned",
    "governance_labels",
    "kms_location",
    "partition_trigger_input",
    "product_key_name",
    "product_key_ring",
    "refuse_mixed_dataset_encryption",
    "refuse_unsupported_target",
    "retention_for",
    "row_policies",
    "tag_groups",
    "taxonomy_display_name",
    "taxonomy_region",
    "validate_bigquery_governance",
]
