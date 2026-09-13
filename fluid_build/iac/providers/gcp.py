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

"""GCP IaC plugin — FLUID contract → BigQuery / GCS / Pub-Sub / IAM ``.tf.json``.

Walks ``exposes[]`` and translates each ``binding.format`` into the
matching ``hashicorp/google`` resource; the contract's **access grants**
become BigQuery dataset access entries and Cloud Storage IAM members. A
pure function of the contract; no credentials, no network.

Access grants are read through :mod:`fluid_build.iac.access`, which prefers
the schema-valid ``accessPolicy`` surface and still accepts the deprecated
``metadata.policies`` for back-compat — see that module for why.

**Packaging modes (RFC-packaging-modes.md file 4).** ``resolve_packaging``
decides per container kind whether this contract owns the container:

* ``LEGACY`` (no ``packaging`` block) — today's exact emit, byte-for-byte.
* ``OWNED`` — the container is a managed resource, same as LEGACY.
* ``REFERENCED`` — the container becomes a ``data`` source and the grants
  move **down one level**, because a tenant must not widen a platform-owned
  pool: a shared dataset drops its dataset-level ``access[]`` block (which
  is authoritative — it would rewrite the pool's whole ACL) in favour of
  per-table ``google_bigquery_table_iam_member``, and a shared bucket's IAM
  members gain an object-prefix condition so the grant covers only this
  product's ``location.path``. A shared bucket also carries no
  ``force_destroy`` — it is not ours to destroy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..access import (
    GROUP,
    AccessGrant,
    grants_from_legacy_policies,
    normalize_access_grants,
    role_grants,
)
from ..importer import ImportBlock
from ..naming import safe_ident, tofu_ref
from ..packaging import (
    ContainerDecision,
    PackagingError,
    PackagingResolution,
    resolve_packaging,
)
from ..provider_match import is_cloud
from ..versions import required_providers

# FLUID column type → BigQuery type (best-effort; unknown types upper-cased).
_BQ_TYPES = {
    "string": "STRING",
    "str": "STRING",
    "text": "STRING",
    "integer": "INT64",
    "int": "INT64",
    "int64": "INT64",
    "bigint": "INT64",
    "float": "FLOAT64",
    "float64": "FLOAT64",
    "double": "FLOAT64",
    "numeric": "NUMERIC",
    "decimal": "NUMERIC",
    "boolean": "BOOL",
    "bool": "BOOL",
    "timestamp": "TIMESTAMP",
    "datetime": "DATETIME",
    "date": "DATE",
    "time": "TIME",
    "bytes": "BYTES",
    "json": "JSON",
}

# FLUID permission → BigQuery dataset access role. BigQuery dataset
# ``access`` entries take the legacy ACL roles (READER/WRITER/OWNER).
_BQ_PERMISSION_ROLES = {
    "read": "READER",
    "select": "READER",
    "query": "READER",
    "write": "WRITER",
    "insert": "WRITER",
    "update": "WRITER",
    "delete": "WRITER",
    "admin": "OWNER",
    "owner": "OWNER",
}
_GCS_PERMISSION_ROLES = {
    "read": "roles/storage.objectViewer",
    "view": "roles/storage.objectViewer",
    "list": "roles/storage.objectViewer",
    "write": "roles/storage.objectCreator",
    "create": "roles/storage.objectCreator",
    "delete": "roles/storage.objectAdmin",
    "admin": "roles/storage.admin",
    "owner": "roles/storage.admin",
}
# FLUID permission → BigQuery *table-level* IAM role. Unlike the dataset
# ``access`` block (legacy ACL roles), table IAM takes standard IAM roles.
_BQ_TABLE_IAM_ROLES = {
    "read": "roles/bigquery.dataViewer",
    "select": "roles/bigquery.dataViewer",
    "query": "roles/bigquery.dataViewer",
    "write": "roles/bigquery.dataEditor",
    "insert": "roles/bigquery.dataEditor",
    "update": "roles/bigquery.dataEditor",
    "delete": "roles/bigquery.dataEditor",
    "admin": "roles/bigquery.dataOwner",
    "owner": "roles/bigquery.dataOwner",
}


def _bq_type(raw: Any) -> str:
    base = str(raw or "STRING").strip().lower().split("(", 1)[0]
    return _BQ_TYPES.get(base, str(raw).upper() if raw else "STRING")


def _bq_schema(schema: List[Mapping[str, Any]]) -> str:
    """FLUID contract schema → BigQuery schema JSON string."""
    fields = [
        {
            "name": col.get("name"),
            "type": _bq_type(col.get("type")),
            "mode": "REQUIRED" if col.get("required") else "NULLABLE",
            "description": col.get("description", ""),
        }
        for col in schema or []
    ]
    return json.dumps(fields, sort_keys=True)


def _bq_access_entries(grants: Sequence[AccessGrant]) -> List[Dict[str, str]]:
    """Normalized grants → a ``google_bigquery_dataset`` ``access`` block.

    The BigQuery field is chosen from the grant's **declared** principal
    type rather than guessed from the string. The previous heuristic
    (``"@" in principal`` → user, else group) mis-filed every group as
    ``user_by_email``, since group addresses contain ``@`` too.

    Service accounts use ``user_by_email`` — BigQuery's own convention for
    SA identities, and what makes a cross-project grant to
    ``consumer@other-project.iam.gserviceaccount.com`` work.
    """
    entries = []
    for role, grant in role_grants(grants, _BQ_PERMISSION_ROLES):
        field = "group_by_email" if grant.principal_type == GROUP else "user_by_email"
        entries.append({"role": role, field: grant.principal})
    return sorted(entries, key=lambda e: json.dumps(e, sort_keys=True))


def _gcs_member(grant: AccessGrant) -> str:
    """Format a normalized grant as a Cloud Storage IAM member string.

    The type is declared by ``accessPolicy`` (or inferred once, centrally,
    for the deprecated surface) — see :mod:`fluid_build.iac.access`.
    """
    return f"{grant.principal_type}:{grant.principal}"


def _legacy_gcs_member(principal: str) -> str:
    """Deprecated shim: format a bare principal string as an IAM member.

    Retained only for out-of-tree callers that pass a raw string; new code
    passes an :class:`AccessGrant` to :func:`_gcs_member`.
    """
    if "@" not in principal:
        return f"group:{principal}"
    if principal.lower().endswith(".gserviceaccount.com"):
        return f"serviceAccount:{principal}"
    return f"user:{principal}"


@dataclass(frozen=True)
class _Placement:
    """One exposure's resolved container ownership (see the module docstring)."""

    dataset_referenced: bool
    bucket_referenced: bool
    pool: Optional[str]


#: Every container LEGACY — today's emit path.
_LEGACY_PLACEMENT = _Placement(dataset_referenced=False, bucket_referenced=False, pool=None)


def _expose_id(exposure: Mapping[str, Any]) -> Optional[str]:
    """The exposure's id, for the resolver's per-exposure override lookup."""
    candidate = exposure.get("exposeId") or exposure.get("id")
    return candidate if isinstance(candidate, str) and candidate else None


def _placement(resolution: PackagingResolution, exposure: Mapping[str, Any]) -> _Placement:
    """Resolve one exposure's placement from the packaging chokepoint."""
    if resolution.is_legacy:
        return _LEGACY_PLACEMENT
    expose_id = _expose_id(exposure)
    pool_exposure = resolution.exposure_for(expose_id) if expose_id else None
    return _Placement(
        dataset_referenced=(
            resolution.decision_for("dataset", expose_id) is ContainerDecision.REFERENCED
        ),
        bucket_referenced=(
            resolution.decision_for("bucket", expose_id) is ContainerDecision.REFERENCED
        ),
        pool=(pool_exposure.pool if pool_exposure is not None else resolution.pool),
    )


def _label_value(value: Any) -> str:
    """Coerce a string into a valid GCP label value.

    GCP label values allow lowercase letters, digits, ``-`` and ``_``, max 63
    characters. Only ever applied to the emitter-controlled pool id.
    """
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(value).lower())
    return cleaned[:63]


def _labels_for(base: Mapping[str, str], placement: _Placement) -> Dict[str, str]:
    """Contract labels plus ``fluid_pool`` when a packaging pool is in scope.

    Absent a ``packaging`` block there is no pool, so every existing
    contract's labels are unchanged.
    """
    labels = dict(base)
    if placement.pool:
        labels["fluid_pool"] = _label_value(placement.pool)
    return labels


def _cel_string(value: Any) -> str:
    """Escape ``value`` for embedding inside a double-quoted CEL string literal.

    SECURITY: the IAM condition below is a CEL *expression*, so contract
    content interpolated into it must not be able to close the string literal
    and append its own terms. An unescaped ``"`` in a ``path`` would turn
    ``startsWith("…/x")`` into ``startsWith("…/x") || true || ("")`` —
    widening a deliberately-narrow grant to every object in the pool, which
    is the exact opposite of what the condition exists to do. Backslash first
    (so an escaped quote is not double-escaped), then the quote; control
    characters are dropped rather than escaped since no legitimate GCS object
    prefix contains them.
    """
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return "".join(ch for ch in text if ch >= " " and ch != "\x7f")


def _object_prefix_condition(bucket: str, path: Any) -> Optional[Dict[str, str]]:
    """An IAM condition narrowing a bucket-wide grant to one object prefix.

    A shared bucket's IAM is bucket-scoped, so a tenant grant would otherwise
    reach every other tenant's objects. GCP's documented narrowing idiom is a
    condition on ``resource.name.startsWith`` against the object's full
    resource path. Returns ``None`` when the binding declares no ``path`` —
    there is then no prefix to scope to and the grant stays bucket-wide
    (surfaced by the validator, RFC file 9).

    Requires uniform bucket-level access on the pool bucket (IAM conditions
    are not evaluated for legacy ACLs) — a documented precondition of shared
    GCS pools.
    """
    prefix = str(path or "").strip().lstrip("/")
    if not prefix:
        return None
    resource = f"projects/_/buckets/{_cel_string(bucket)}/objects/{_cel_string(prefix)}"
    return {
        "title": "fluid-object-prefix",
        "description": f"Limit access to objects under {prefix}",
        "expression": f'resource.name.startsWith("{resource}")',
    }


class GcpIacPlugin:
    """``IacProviderPlugin`` for Google Cloud."""

    name = "gcp"
    required_providers = required_providers("google")
    # `tofu` reads whichever GOOGLE_* var is set; the emitted `.tf.json`
    # stays credential-free regardless of the auth method.
    credential_env_vars = (
        # Service account key / Application Default Credentials /
        # Workload Identity Federation config file (keyless CI auth).
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CREDENTIALS",
        # Short-lived OAuth 2.0 access token.
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        # Service account impersonation.
        "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
        # Project / region.
        "GOOGLE_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_REGION",
    )

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        base_labels = {"managed_by": "fluid", "fluid_contract": cid}
        # Contract-global access control applies to every exposure's
        # resource. Read from the schema-valid `accessPolicy` surface, with
        # the deprecated (schema-invalid) `metadata.policies` appended for
        # back-compat — see `iac/access.py` for why that split exists.
        grants = normalize_access_grants(contract)
        packaging = resolve_packaging(contract)

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            target = resolve_gcp_target(binding)
            loc = binding.get("location") or {}
            schema = (exposure.get("contract") or {}).get("schema") or []
            placement = _placement(packaging, exposure)
            labels = _labels_for(base_labels, placement)
            if target in (BIGQUERY_TABLE, BIGQUERY_VIEW):
                _emit_bigquery(
                    resources,
                    exposure,
                    loc,
                    schema,
                    cid,
                    labels,
                    is_view=(target == BIGQUERY_VIEW),
                    grants=grants,
                    placement=placement,
                )
            elif target == GCS_BUCKET:
                # An expose that NAMES the bucket as its port (``format:
                # gcs_bucket``) owns it. One resolved from the location shape
                # merely cites the container its files live in, so a declared
                # ``path`` makes it a prefix tenant — same reasoning, and the
                # same flag, as the Iceberg warehouse route.
                _emit_gcs(
                    resources,
                    loc,
                    cid,
                    labels,
                    grants=grants,
                    placement=placement,
                    prefix_only=(
                        _FORMAT_TARGETS.get(_normalized_format(binding)) is not GCS_BUCKET
                        and bool(str(loc.get("path") or "").strip("/"))
                    ),
                )
            elif target == ICEBERG_STORAGE:
                _emit_iceberg_storage(
                    resources, binding, loc, cid, labels, grants=grants, placement=placement
                )
            elif target == PUBSUB_TOPIC:
                _emit_pubsub(resources, loc, cid, labels)
        # Cloud Run / Cloud Scheduler / Pub-Sub event resources — the
        # planner already interpreted the loose `execution.trigger`
        # surface into structured `run.*` / `scheduler.*` / `ps.*` ops.
        _emit_from_actions(resources, actions, cid)
        return resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """``data`` sources for REFERENCED (platform-owned pool) containers.

        Empty for every LEGACY contract — GCP emitted only ``resource``
        blocks before packaging modes, and still does absent a ``packaging``
        block. Under ``shared`` the pool dataset / bucket is looked up rather
        than created, so ``tofu`` never plans to manage or destroy it, and
        :func:`_emit_bigquery` / :func:`_emit_gcs` point their leaf resources
        at these addresses.
        """
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        packaging = resolve_packaging(contract)
        if packaging.is_legacy:
            return {}
        data: Dict[str, Dict[str, Any]] = {}
        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            # Resolve through the same chokepoint :meth:`emit` uses — the two
            # must agree, or a container is either looked up but unused
            # (orphan data source) or referenced but never declared.
            target = resolve_gcp_target(binding)
            loc = binding.get("location") or {}
            placement = _placement(packaging, exposure)
            if target in (BIGQUERY_TABLE, BIGQUERY_VIEW) and placement.dataset_referenced:
                dataset = loc.get("dataset") or "default"
                data.setdefault("google_bigquery_dataset", {}).setdefault(
                    safe_ident(f"{cid}_{dataset}"), {"dataset_id": dataset}
                )
            elif target == GCS_BUCKET and placement.bucket_referenced:
                bucket = loc.get("bucket") or f"{cid}-bucket"
                data.setdefault("google_storage_bucket", {}).setdefault(
                    safe_ident(f"{cid}_{bucket}"), {"name": bucket}
                )
            elif target == ICEBERG_STORAGE and placement.bucket_referenced:
                # Must mirror the ``emit`` branch. Under shared packaging
                # ``_emit_gcs`` references ``${data.google_storage_bucket…}``
                # for each grant, so omitting the lookup here makes every
                # apply fail `tofu validate` with "Reference to undeclared
                # resource". Derived through the shared helper so the key
                # matches the one ``emit`` produces.
                from ...providers._iceberg_catalog import iceberg_bucket_name

                bucket = iceberg_bucket_name(binding)
                if bucket:
                    data.setdefault("google_storage_bucket", {}).setdefault(
                        safe_ident(f"{cid}_{bucket}"), {"name": bucket}
                    )
        return data

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """The ``hashicorp/google`` provider reads the standard ``GOOGLE_*``
        environment (and Application Default Credentials) directly — no
        translation."""
        return {}

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Brownfield ``tofu import`` candidates for each contract-declared GCP resource.

        Mirrors what :meth:`emit` produces; the apply engine calls
        ``tofu import`` for each block before ``tofu apply``. Imports
        that miss (the resource doesn't exist yet) are tolerated by
        ``_adopt_existing`` and left for ``tofu apply`` to create.

        Import IDs follow the ``hashicorp/google`` provider's documented
        identifiers:
          * ``google_bigquery_dataset`` — ``projects/{project}/datasets/{dataset}``
          * ``google_bigquery_table``   — ``projects/{project}/datasets/{dataset}/tables/{table}``
          * ``google_storage_bucket``   — ``{bucket}`` (provider defaults project)
          * ``google_pubsub_topic``     — ``projects/{project}/topics/{topic}``

        The project segment is read from ``GOOGLE_PROJECT`` /
        ``GOOGLE_CLOUD_PROJECT`` at import time; when not set, the
        provider falls back to ADC, so the import id still resolves.
        Returned blocks always use the ``{project}`` literal so a
        future caller-side project resolver can substitute the real
        project id; until then the placeholder is interpolated
        upstream by the apply engine's env (the placeholder ``_``
        is rejected by the provider, so we use the env-var lookup).

        REFERENCED containers are excluded: a shared dataset / bucket is a
        platform-owned pool, and ``tofu import``-ing it would adopt it into
        this product's state — re-owning infrastructure the contract
        explicitly declared it does not own (RFC file 4).
        """
        import os

        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        packaging = resolve_packaging(contract)
        project = (
            os.environ.get("GOOGLE_PROJECT")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("CLOUDSDK_CORE_PROJECT")
            or ""
        )
        blocks: List[ImportBlock] = []
        seen: set[str] = set()

        def _add(address: str, resource_id: str) -> None:
            if address not in seen:
                seen.add(address)
                blocks.append(ImportBlock(to=address, id=resource_id))

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            # Normalised through the shared cloud table, not a literal
            # ``== "gcp"``: ``platform: bigquery`` auto-detects as GCP and
            # emits, so it must be importable too or a brownfield apply tries
            # to create objects that already exist.
            if not is_cloud(binding, "gcp"):
                continue
            loc = binding.get("location") or {}
            placement = _placement(packaging, exposure)
            # Dispatch on the resolved target, exactly as ``emit`` does. This
            # block used to read the ``location`` keys directly, which is not
            # the same question: an Iceberg expose's bucket comes from
            # ``iceberg_bucket_name`` (where ``warehouse`` beats
            # ``location.bucket``), so a warehouse-only binding produced no
            # import at all and a binding carrying both produced an import for
            # the WRONG bucket — leaving `tofu apply` to 409 on a bucket that
            # already exists, the very failure ``_bq_table_name`` fixed for
            # tables.
            target = resolve_gcp_target(binding)
            dataset = loc.get("dataset") if target in (BIGQUERY_TABLE, BIGQUERY_VIEW) else None
            # The same fallback ``_emit_bigquery`` uses — an exposure with no
            # ``location.table`` still declares a table, named for its id.
            table = _bq_table_name(exposure, loc) if dataset else None
            if target is ICEBERG_STORAGE:
                from ...providers._iceberg_catalog import iceberg_bucket_name

                bucket = iceberg_bucket_name(binding)
            else:
                bucket = loc.get("bucket") if target is GCS_BUCKET else None
            topic = loc.get("topic") if target is PUBSUB_TOPIC else None

            if dataset:
                ds_key = safe_ident(f"{cid}_{dataset}")
                ds_id = f"projects/{project}/datasets/{dataset}" if project else dataset
                if not placement.dataset_referenced:
                    _add(f"google_bigquery_dataset.{ds_key}", ds_id)
                if table:
                    tbl_key = safe_ident(f"{cid}_{table}")
                    tbl_id = (
                        f"projects/{project}/datasets/{dataset}/tables/{table}"
                        if project
                        else f"{dataset}/{table}"
                    )
                    _add(f"google_bigquery_table.{tbl_key}", tbl_id)

            if bucket and not placement.bucket_referenced:
                bkt_key = safe_ident(f"{cid}_{bucket}")
                _add(f"google_storage_bucket.{bkt_key}", bucket)

            if topic:
                topic_key = safe_ident(f"{cid}_{topic}")
                topic_id = f"projects/{project}/topics/{topic}" if project else topic
                _add(f"google_pubsub_topic.{topic_key}", topic_id)

        return blocks

    def provider_block(self) -> Dict[str, Any]:
        """No static provider configuration — the ``hashicorp/google``
        provider self-configures from the environment."""
        return {}


def _emit_bigquery(
    resources: Dict[str, Any],
    exposure: Mapping[str, Any],
    loc: Mapping[str, Any],
    schema: List[Mapping[str, Any]],
    cid: str,
    labels: Dict[str, str],
    *,
    is_view: bool,
    grants: Sequence[AccessGrant],
    placement: _Placement = _LEGACY_PLACEMENT,
) -> None:
    dataset = loc.get("dataset") or "default"
    table = _bq_table_name(exposure, loc)
    ds_name = safe_ident(f"{cid}_{dataset}")
    tbl_name = safe_ident(f"{cid}_{table}")

    if placement.dataset_referenced:
        # Shared pool dataset: looked up (see ``emit_data``), never created.
        # The dataset-level ``access[]`` block is deliberately NOT emitted —
        # it is authoritative in the BigQuery API, so a tenant writing it
        # would replace the pool's entire ACL and evict its other tenants.
        # The same grants move down to table level instead.
        ds_ref: Any = tofu_ref(f"data.google_bigquery_dataset.{ds_name}.dataset_id")
    else:
        dataset_body: Dict[str, Any] = {
            "dataset_id": dataset,
            "location": loc.get("region") or loc.get("location") or "US",
            "labels": labels,
        }
        # Access grants → the dataset ACL (mirrors the retired native
        # `iam.bind_bq_dataset`, which appended BigQuery access entries).
        access = _bq_access_entries(grants)
        if access:
            dataset_body["access"] = access
        resources.setdefault("google_bigquery_dataset", {}).setdefault(ds_name, dataset_body)
        ds_ref = tofu_ref(f"google_bigquery_dataset.{ds_name}.dataset_id")

    body: Dict[str, Any] = {
        "dataset_id": ds_ref,
        "table_id": table,
        "labels": labels,
        # Let `tofu destroy` clean the table — the spike applies and destroys.
        "deletion_protection": False,
    }
    if is_view:
        body["view"] = {"query": loc.get("query", ""), "use_legacy_sql": False}
    elif schema:
        body["schema"] = _bq_schema(schema)
    resources.setdefault("google_bigquery_table", {})[tbl_name] = body

    # Table-level IAM replaces the suppressed dataset ACL under shared mode:
    # the same principals, the same intent, the narrowest scope that still
    # grants it (RFC §Security — "GCP grants move to table level").
    if placement.dataset_referenced:
        for role, grant in role_grants(grants, _BQ_TABLE_IAM_ROLES):
            member = _gcs_member(grant)
            resources.setdefault("google_bigquery_table_iam_member", {})[
                safe_ident(f"{cid}_{dataset}_{table}_{role}_{member}")
            ] = {
                "dataset_id": ds_ref,
                "table_id": tofu_ref(f"google_bigquery_table.{tbl_name}.table_id"),
                "role": role,
                "member": member,
            }

    # Cross-project access needs no new schema fields: declare the consumer
    # in ``accessPolicy.grants[]`` as
    # ``serviceAccount:consumer@other-project.iam.gserviceaccount.com`` and
    # the email lands in BQ's ``user_by_email`` field on the dataset's
    # ``access[]`` block. (``accessPolicy`` is the schema-valid surface;
    # ``metadata.policies`` also emits but fails ``fluid validate`` — see
    # ``iac/access.py``.)


#: ``binding.format`` values marking an Iceberg-table expose. Matches the
#: Snowflake IaC emitter's set so the two providers agree on what Iceberg is.
_ICEBERG_FORMATS = ("iceberg", "iceberg_table")

# ── Target resolution — the ONE dispatch table ────────────────────────
#
# Every consumer that has to answer "which GCP resource does this exposure
# become?" — :meth:`GcpIacPlugin.emit`, :meth:`~GcpIacPlugin.emit_data`,
# :meth:`~GcpIacPlugin.discover_imports` and the validate-time gate
# :func:`validate_gcp_binding` — goes through :func:`resolve_gcp_target`.
# They MUST agree: emit and emit_data disagreeing leaves a ``${data.…}``
# reference with no declaration (``tofu validate``: "Reference to undeclared
# resource"); emit and discover_imports disagreeing makes a brownfield apply
# try to create a table that already exists; emit and the validator
# disagreeing either blocks a contract that would have worked or waves
# through one that emits nothing.

#: The GCP resource kinds an exposure can resolve to.
BIGQUERY_TABLE = "bigquery_table"
BIGQUERY_VIEW = "bigquery_view"
GCS_BUCKET = "gcs_bucket"
ICEBERG_STORAGE = "iceberg"
PUBSUB_TOPIC = "pubsub_topic"

#: ``binding.format`` spellings that name a GCP target outright, whatever the
#: binding's platform. These are the five this emitter has always dispatched
#: on; keeping them platform-agnostic preserves the emit of a contract that
#: declares a format but no platform.
_FORMAT_TARGETS: Dict[str, str] = {
    "bigquery_table": BIGQUERY_TABLE,
    "bigquery_view": BIGQUERY_VIEW,
    "gcs_bucket": GCS_BUCKET,
    "pubsub_topic": PUBSUB_TOPIC,
    **{fmt: ICEBERG_STORAGE for fmt in _ICEBERG_FORMATS},
}

#: ``binding.format`` values that describe an *access surface* or a store this
#: emitter does not own, rather than a GCP container. Emitting no
#: ``hashicorp/google`` resource for one of these is correct, so
#: :func:`validate_gcp_binding` stays quiet about them instead of reporting a
#: no-op. Everything else that resolves to nothing IS a no-op and is reported.
_NO_GCP_CONTAINER_FORMATS = frozenset(
    {
        # Consumer-served API ports — no infrastructure to declare.
        "http_api",
        "grpc_api",
        # Kafka: self-managed or Confluent, not Pub/Sub (which has its own
        # ``pubsub_topic`` format).
        "kafka_topic",
        # Stores on another platform, or a GCP one this emitter does not
        # provision yet (Cloud SQL / AlloyDB). Named explicitly so adding
        # support later is a deletion from this set, not a hunt.
        "snowflake_table",
        "snowflake_view",
        "s3_file",
        "athena_table",
        "glue_table",
        "redshift_table",
        "redshift_serverless",
        "redshift_external_schema",
        "postgres_table",
        "pgvector_table",
    }
)


#: ``binding.location`` key → (target, service label), in the precedence
#: :func:`resolve_gcp_target` applies. The resolver ITERATES this, and
#: :func:`validate_gcp_binding` renders the labels into its remediation text,
#: so what the message tells the user to add cannot drift from what the
#: resolver actually reads. A key naming no container the emitter can build
#: does not belong here: ``subscription``, for instance, supplies no topic
#: name, and inferring Pub/Sub from it made ``_emit_pubsub`` fall back to a
#: fabricated ``<contract>-topic`` that appears nowhere in the contract.
_GCP_LOCATION_TARGETS = (
    ("dataset", BIGQUERY_TABLE, "BigQuery"),
    ("bucket", GCS_BUCKET, "Cloud Storage"),
    ("topic", PUBSUB_TOPIC, "Pub/Sub"),
)


def _normalized_format(binding: Mapping[str, Any]) -> str:
    """``binding.format``, lower-cased and stripped — the one spelling rule.

    Every comparison against a format literal goes through this. A raw
    ``binding.get("format") == "gcs_bucket"`` next to a table lookup that
    normalises is the drift this module exists to remove.
    """
    return str(binding.get("format") or "").strip().lower()


def resolve_gcp_target(binding: Mapping[str, Any]) -> Optional[str]:
    """The GCP resource kind ``binding`` resolves to, or ``None``.

    Resolution order, mirroring what the AWS and Snowflake emitters already do
    (both dispatch on the shape of ``binding.location`` and let ``format``
    merely refine the result):

    1. An explicit GCP ``binding.format`` (:data:`_FORMAT_TARGETS`) wins —
       except an Iceberg expose with no derivable bucket, which resolves to
       nothing because :func:`_emit_iceberg_storage` would emit nothing for
       it. Resolving it to a target anyway would make this function disagree
       with the emitter, which is the one thing it exists to prevent.
    2. Otherwise, for a GCP-platform binding, the ``location`` shape decides
       (:data:`_GCP_LOCATION_TARGETS`).
    3. Nothing → ``None``, and :func:`validate_gcp_binding` explains why.

    Step 2 is what stops a ``platform: gcp`` exposure whose ``format`` is
    absent, or is the schema-valid ``gcs_file`` (which is *not* one of the
    five spellings this emitter grew), from silently emitting nothing.
    """
    if not isinstance(binding, Mapping):
        return None
    target = _FORMAT_TARGETS.get(_normalized_format(binding))
    if target is ICEBERG_STORAGE:
        from ...providers._iceberg_catalog import iceberg_bucket_name

        return ICEBERG_STORAGE if iceberg_bucket_name(binding) else None
    if target:
        return target
    if not is_cloud(binding, "gcp"):
        return None
    loc = binding.get("location") or {}
    if not isinstance(loc, Mapping):
        return None
    for key, shape_target, _service in _GCP_LOCATION_TARGETS:
        if loc.get(key):
            return shape_target
    return None


#: ``binding.format`` → the ``binding.location`` key that format's container
#: needs. A format here names a GCP container outright, so a location that
#: omits the key is a contract error, not a matter of taste. ``gcs_file`` is
#: the schema-valid GCS spelling; the emitter's own ``gcs_bucket`` /
#: ``bigquery_table`` / ``pubsub_topic`` spellings resolve through
#: :data:`_FORMAT_TARGETS` and never reach the gate.
_FORMAT_REQUIRES_LOCATION_KEY = {"gcs_file": "bucket"}


def validate_gcp_binding(contract: Mapping[str, Any]) -> Tuple[List[str], List[str]]:
    """Validate-time gate for GCP exposures — the loud half of the emitter.

    :meth:`GcpIacPlugin.emit` is pure and emit-when-derivable: an exposure it
    cannot resolve to a GCP resource produces nothing rather than something
    broken. Right for an emitter, silent on its own — the user gets an empty
    module and learns nothing until ``fluid apply`` has no table to write to.
    Same shape as ``confluent.validate_confluent_binding`` and
    ``iac.iceberg_validation``: the emitter stays quiet, the validator
    explains.

    Both halves resolve through :func:`resolve_gcp_target`, so this can neither
    block a contract that would have emitted nor wave through one that emits
    nothing — the failure mode a second, hand-mirrored dispatch table here
    would reintroduce.

    **Error vs warning.** ``fluid validate`` runs for EVERY contract, including
    ones that never reach ``fluid generate iac``, so a hard error here stops a
    pipeline that a mere reporting gap would not. Only a format that *names* a
    GCP container while omitting the location key that container needs
    (:data:`_FORMAT_REQUIRES_LOCATION_KEY`) is unambiguously broken and errors.
    Everything else — a generic file format, or no format at all — warns: since
    #546 the empty module is itself a hard ``generate_iac_empty_module``
    failure at the stage that actually needs the resource, so the loud stop is
    already in the right place and this only has to explain it early.

    Scoped to exposures whose ``binding.platform`` is GCP: a format-only
    binding (no platform) is a different contract error, and the JSON-schema
    check already names it. Iceberg exposes are left to
    ``iac.iceberg_validation``, which owns a more specific message for the
    same input. Returns ``(errors, warnings)``.
    """
    errors: List[str] = []
    warnings: List[str] = []
    for exposure in contract.get("exposes") or []:
        if not isinstance(exposure, Mapping):
            continue
        binding = exposure.get("binding") or {}
        if not is_cloud(binding, "gcp") or resolve_gcp_target(binding) is not None:
            continue
        eid = exposure.get("exposeId") or exposure.get("id") or "?"
        fmt = str(binding.get("format") or "").strip().lower()
        if fmt in _NO_GCP_CONTAINER_FORMATS:
            # No ``hashicorp/google`` resource exists for this port by design.
            continue
        if fmt in _ICEBERG_FORMATS:
            # An Iceberg expose with no derivable bucket — ``iceberg_validation``
            # reports it, naming the specific prerequisite that is missing.
            # Two errors for one cause would read as two problems.
            continue
        got = (
            f"binding.format is '{binding.get('format')}'"
            if fmt
            else "it declares no binding.format"
        )
        keys = ", ".join(f"{key} ({service})" for key, _target, service in _GCP_LOCATION_TARGETS)
        required = _FORMAT_REQUIRES_LOCATION_KEY.get(fmt)
        missing = (
            f"binding.location has no '{required}' key"
            if required
            else f"binding.location names none of {keys}"
        )
        message = (
            f"expose '{eid}': platform=gcp resolves to no GCP resource — {got} and "
            f"{missing}. `fluid generate iac` and `fluid apply` would emit nothing for "
            f"this port. Add the binding.location key for the container it lives in."
        )
        if _FORMAT_REQUIRES_LOCATION_KEY.get(fmt):
            errors.append(message)
        else:
            warnings.append(message)
    return errors, warnings


def _bq_table_name(exposure: Mapping[str, Any], loc: Mapping[str, Any]) -> str:
    """The BigQuery table id an exposure emits under.

    Shared by :func:`_emit_bigquery` and :meth:`GcpIacPlugin.discover_imports`
    so the import block addresses the table the emitter actually declares —
    they disagreed while only the emitter fell back to the exposeId, which
    left a contract with no ``location.table`` un-importable and so
    un-adoptable on brownfield apply.
    """
    return loc.get("table") or loc.get("view") or exposure.get("exposeId") or "table"


def _emit_iceberg_storage(
    resources: Dict[str, Any],
    binding: Mapping[str, Any],
    loc: Mapping[str, Any],
    cid: str,
    labels: Dict[str, str],
    *,
    grants: Sequence[AccessGrant],
    placement: _Placement = _LEGACY_PLACEMENT,
) -> None:
    """Emit the GCS bucket backing a BigQuery Iceberg table.

    dbt materializes BigQuery Iceberg through ``catalogs.yml`` with
    ``catalog_type: biglake_metastore``. Its documentation is explicit that
    the metastore itself needs no setup because it is built into BigQuery, so
    the one prerequisite dbt names and does not create is the storage bucket.

    The bucket name comes from the shared
    :func:`~fluid_build.providers._iceberg_catalog.iceberg_bucket_name`, whose
    sibling :func:`~fluid_build.providers._iceberg_catalog.iceberg_storage_uri`
    produces the exact ``gs://`` URI the dbt emitter writes into
    ``external_volume``. Both derive from the same binding, so dbt cannot end
    up pointed at a bucket ``fluid apply`` never created.

    Before this, an ``iceberg`` expose on a GCP binding emitted nothing at all:
    the dispatch only handled bigquery_table/view, gcs_bucket and pubsub_topic.
    """
    from ...providers._iceberg_catalog import iceberg_bucket_name

    bucket = iceberg_bucket_name(binding)
    if not bucket:
        # Nothing derivable, so there is no bucket to create. The dbt side
        # skips the integration for the same reason.
        return
    # Reuse the GCS emitter so bucket settings, labels and access-grant IAM
    # stay identical to a plain gcs_bucket expose. It reads ``bucket`` from
    # the location, so pass a view with the derived name resolved.
    # A declared ``path`` means this product owns a PREFIX of the warehouse,
    # not the bucket. Sharing one warehouse root across products namespaced
    # by prefix is the normal Iceberg convention, so whole-bucket
    # force_destroy would let one product's destroy take another's data with
    # it. The owned-bucket case (no path) keeps the default.
    _emit_gcs(
        resources,
        {**loc, "bucket": bucket},
        cid,
        labels,
        grants=grants,
        placement=placement,
        prefix_only=bool(str(loc.get("path") or "").strip("/")),
    )


def _emit_gcs(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    cid: str,
    labels: Dict[str, str],
    *,
    grants: Sequence[AccessGrant],
    placement: _Placement = _LEGACY_PLACEMENT,
    prefix_only: bool = False,
) -> None:
    """Emit the exposure's Cloud Storage bucket and its access-grant IAM.

    ``prefix_only`` says this product owns a PREFIX of the bucket rather than
    the bucket — see the ``force_destroy`` comment below. It defaults to False
    so an explicit ``format: gcs_bucket`` expose, which names the bucket
    itself as the port, keeps declaring ownership.
    """
    bucket = loc.get("bucket") or f"{cid}-bucket"
    bkt_res = safe_ident(f"{cid}_{bucket}")
    if placement.bucket_referenced:
        # Shared pool bucket: looked up (see ``emit_data``), never created —
        # and notably carrying no ``force_destroy``, which on a pool would
        # let one tenant's `tofu destroy` empty every tenant's objects.
        bkt_ref: Any = tofu_ref(f"data.google_storage_bucket.{bkt_res}.name")
    else:
        bucket_body: Dict[str, Any] = {
            "name": bucket,
            "location": loc.get("region") or loc.get("location") or "US",
            "uniform_bucket_level_access": True,
            "labels": labels,
        }
        # ``force_destroy`` overrides GCS's refusal to delete a non-empty
        # bucket, so it is only safe on a bucket this product OWNS. The caller
        # decides that, because ownership is a property of the exposure's
        # kind, not of the location: ``format: gcs_bucket`` names the bucket
        # itself as the port, while an Iceberg warehouse or a file-ish expose
        # that merely cites a ``bucket`` owns a PREFIX of a root that is
        # conventionally shared between products. Setting the flag here, in
        # one place, is what keeps the two from drifting apart.
        if not prefix_only:
            bucket_body["force_destroy"] = True
        resources.setdefault("google_storage_bucket", {})[bkt_res] = bucket_body
        bkt_ref = tofu_ref(f"google_storage_bucket.{bkt_res}.name")
    # Access grants → additive bucket IAM members (mirrors the retired
    # native `iam.bind_gcs_bucket`).
    condition = (
        _object_prefix_condition(bucket, loc.get("path")) if placement.bucket_referenced else None
    )
    bucket_grants = role_grants(grants, _GCS_PERMISSION_ROLES)
    if bucket_grants and placement.bucket_referenced and condition is None:
        # SECURITY: bucket IAM on GCS is bucket-scoped. Without a prefix
        # condition the member reads every tenant's objects in the pool —
        # the grant silently degrades to exactly what shared mode exists to
        # prevent. Fail closed rather than emit an unconditioned member;
        # same discipline as the resolver's ``pool-required``. Note this
        # also catches a path of ``"/"`` or whitespace, which normalises
        # away to an empty prefix and would look scoped to a reviewer.
        raise PackagingError(
            "shared-bucket-requires-path",
            f"bucket {bucket!r} is shared (pool) and the contract grants "
            "access to it, but the binding declares no usable `location.path` — a "
            "bucket-level IAM grant on a pool would reach every other tenant's "
            "objects. Add a `location.path` prefix, or declare the bucket "
            "`isolated` if this product really owns it.",
        )
    for role, grant in bucket_grants:
        member = _gcs_member(grant)
        name = safe_ident(f"{cid}_{bucket}_{role}_{member}")
        member_body: Dict[str, Any] = {
            "bucket": bkt_ref,
            "role": role,
            "member": member,
        }
        if condition:
            member_body["condition"] = condition
        resources.setdefault("google_storage_bucket_iam_member", {})[name] = member_body


def _emit_pubsub(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, labels: Dict[str, str]
) -> None:
    topic = loc.get("topic") or f"{cid}-topic"
    topic_res = safe_ident(f"{cid}_{topic}")
    resources.setdefault("google_pubsub_topic", {})[topic_res] = {
        "name": topic,
        "labels": labels,
    }
    subscription = loc.get("subscription")
    if subscription:
        resources.setdefault("google_pubsub_subscription", {})[
            safe_ident(f"{cid}_{subscription}")
        ] = {
            "name": subscription,
            "topic": tofu_ref(f"google_pubsub_topic.{topic_res}.name"),
            "labels": labels,
        }


def _emit_from_actions(
    resources: Dict[str, Any], actions: Iterable[Mapping[str, Any]], cid: str
) -> None:
    """Translate the planner's schedule / event ops into ``hashicorp/google`` resources.

    The planner interprets the loose ``execution.trigger`` surface into
    structured ``run.*`` / ``scheduler.*`` / ``ps.*`` / ``composer.*`` ops;
    this maps each to its declarative resource. ``composer.trigger_dag``
    (kicking off a one-off run) has no declarative form and is skipped.
    """
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        op = action.get("op")
        if op == "run.ensure_service":
            _emit_cloud_run(resources, action, cid)
        elif op == "scheduler.ensure_job":
            _emit_cloud_scheduler(resources, action, cid)
        elif op == "ps.ensure_topic":
            _emit_planned_topic(resources, action, cid)
        elif op == "ps.ensure_subscription":
            _emit_planned_subscription(resources, action, cid)
        elif op == "iam.bind_bq_table":
            _emit_bq_table_iam(resources, action, cid)
        elif op == "composer.deploy_dag":
            _emit_composer_dag(resources, action, cid)


def _emit_cloud_run(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``run.ensure_service`` → ``google_cloud_run_v2_service``."""
    name = action.get("service_name")
    region = action.get("region")
    image = action.get("image")
    if not (name and region and image):
        return
    container: Dict[str, Any] = {
        "image": image,
        "resources": {
            "limits": {
                "cpu": str(action.get("cpu", "1")),
                "memory": str(action.get("memory", "512Mi")),
            }
        },
    }
    env = [
        {"name": str(k), "value": str(v)} for k, v in sorted((action.get("env_vars") or {}).items())
    ]
    if env:
        container["env"] = env
    template: Dict[str, Any] = {
        "containers": [container],
        "scaling": {
            "min_instance_count": int(action.get("min_instances", 0)),
            "max_instance_count": int(action.get("max_instances", 1)),
        },
        "max_instance_request_concurrency": int(action.get("concurrency", 1)),
    }
    if action.get("timeout"):
        template["timeout"] = f"{action['timeout']}s"
    if action.get("service_account"):
        template["service_account"] = action["service_account"]
    if action.get("vpc_connector"):
        template["vpc_access"] = {"connector": action["vpc_connector"]}
    body: Dict[str, Any] = {
        "name": name,
        "location": region,
        # The spike applies and destroys — let `tofu destroy` clean up.
        "deletion_protection": False,
        "template": template,
    }
    if action.get("labels"):
        body["labels"] = action["labels"]
    resources.setdefault("google_cloud_run_v2_service", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_cloud_scheduler(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``scheduler.ensure_job`` → ``google_cloud_scheduler_job``."""
    name = action.get("job_name")
    schedule = action.get("schedule")
    http = (action.get("target") or {}).get("http_target") or {}
    uri = http.get("uri")
    if not (name and schedule and uri):
        return
    http_target: Dict[str, Any] = {"uri": uri, "http_method": http.get("http_method", "POST")}
    if http.get("headers"):
        http_target["headers"] = http["headers"]
    if http.get("body"):
        http_target["body"] = http["body"]
    oidc = http.get("oidc_token") or {}
    if oidc.get("service_account_email"):
        token = {"service_account_email": oidc["service_account_email"]}
        if oidc.get("audience"):
            token["audience"] = oidc["audience"]
        http_target["oidc_token"] = token
    body: Dict[str, Any] = {"name": name, "schedule": schedule, "http_target": http_target}
    if action.get("location"):
        body["region"] = action["location"]
    if action.get("timezone"):
        body["time_zone"] = action["timezone"]
    if action.get("description"):
        body["description"] = action["description"]
    if action.get("attempt_deadline"):
        body["attempt_deadline"] = action["attempt_deadline"]
    retry = action.get("retry_config")
    if isinstance(retry, Mapping):
        kept = {k: v for k, v in retry.items() if v is not None}
        if kept:
            body["retry_config"] = kept
    resources.setdefault("google_cloud_scheduler_job", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_planned_topic(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``ps.ensure_topic`` → ``google_pubsub_topic`` (the event-trigger topic)."""
    topic = action.get("topic")
    if not topic:
        return
    body: Dict[str, Any] = {"name": topic}
    if action.get("labels"):
        body["labels"] = action["labels"]
    if action.get("message_retention_duration"):
        body["message_retention_duration"] = action["message_retention_duration"]
    resources.setdefault("google_pubsub_topic", {}).setdefault(safe_ident(f"{cid}_{topic}"), body)


def _emit_planned_subscription(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str
) -> None:
    """``ps.ensure_subscription`` → ``google_pubsub_subscription`` (push to Cloud Run)."""
    subscription = action.get("subscription")
    topic = action.get("topic")
    if not (subscription and topic):
        return
    topic_res = safe_ident(f"{cid}_{topic}")
    body: Dict[str, Any] = {
        "name": subscription,
        "topic": tofu_ref(f"google_pubsub_topic.{topic_res}.id"),
    }
    if action.get("ack_deadline_seconds"):
        body["ack_deadline_seconds"] = int(action["ack_deadline_seconds"])
    if action.get("message_retention_duration"):
        body["message_retention_duration"] = action["message_retention_duration"]
    if action.get("retain_acked_messages") is not None:
        body["retain_acked_messages"] = bool(action["retain_acked_messages"])
    if action.get("filter"):
        body["filter"] = action["filter"]
    if action.get("labels"):
        body["labels"] = action["labels"]
    push = action.get("push_config") or {}
    if push.get("push_endpoint"):
        push_config: Dict[str, Any] = {"push_endpoint": push["push_endpoint"]}
        if push.get("attributes"):
            push_config["attributes"] = push["attributes"]
        oidc = push.get("oidc_token") or {}
        if oidc.get("service_account_email"):
            token = {"service_account_email": oidc["service_account_email"]}
            if oidc.get("audience"):
                token["audience"] = oidc["audience"]
            push_config["oidc_token"] = token
        body["push_config"] = push_config
    dlp = action.get("dead_letter_policy")
    if isinstance(dlp, Mapping) and dlp.get("dead_letter_topic"):
        body["dead_letter_policy"] = {
            "dead_letter_topic": dlp["dead_letter_topic"],
            "max_delivery_attempts": int(dlp.get("max_delivery_attempts", 5)),
        }
    resources.setdefault("google_pubsub_subscription", {})[
        safe_ident(f"{cid}_{subscription}")
    ] = body


def _emit_bq_table_iam(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``iam.bind_bq_table`` → ``google_bigquery_table_iam_member`` (table-scoped IAM).

    Dataset-level IAM is folded into the dataset ``access`` block by the
    ``exposes[]`` walk; this adds the finer table-level grants.
    """
    dataset = action.get("dataset")
    table = action.get("table")
    if not (dataset and table):
        return
    action_grants = grants_from_legacy_policies(action.get("policies"))
    for role, grant in role_grants(action_grants, _BQ_TABLE_IAM_ROLES):
        member = _gcs_member(grant)
        name = safe_ident(f"{cid}_{dataset}_{table}_{role}_{member}")
        resources.setdefault("google_bigquery_table_iam_member", {})[name] = {
            "dataset_id": dataset,
            "table_id": table,
            "role": role,
            "member": member,
        }


def _emit_composer_dag(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``composer.deploy_dag`` → ``google_storage_bucket_object`` (the DAG file).

    A Composer environment's DAG bucket is auto-named and not derivable
    from the contract — the operator supplies it via the trigger's
    ``dag_gcs_bucket`` property. Without it (or a rendered DAG) the deploy
    cannot be declarative and the op is skipped.
    """
    bucket = action.get("dag_bucket")
    dag_id = action.get("dag_id")
    content = action.get("dag_content")
    if not (bucket and dag_id and content):
        return
    resources.setdefault("google_storage_bucket_object", {})[safe_ident(f"{cid}_dag_{dag_id}")] = {
        "name": f"dags/{dag_id}.py",
        "bucket": bucket,
        "content": content,
    }
