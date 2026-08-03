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

"""AWS IaC plugin — FLUID contract → Glue + S3 + Kinesis + Redshift ``.tf.json``.

Translates AWS-bound exposures into a Glue catalog database + table (the
Iceberg-on-S3 mesh interface Athena reads natively), the backing S3 bucket,
Kinesis data streams, and Redshift Serverless namespaces + workgroups + a
``CREATE EXTERNAL SCHEMA`` bridge so Redshift queries the same Glue catalog
via Spectrum. A pure function of the contract; no credentials, no network.

**Packaging modes (RFC-packaging-modes.md file 3).** ``resolve_packaging``
decides per container kind whether this contract owns the container:

* ``LEGACY`` (no ``packaging`` block) — today's exact emit, byte-for-byte,
  including ``force_destroy: true`` and the ``{account}-fluid-data`` fallback
  bucket.
* ``OWNED`` — the container is a managed resource, same as LEGACY.
* ``REFERENCED`` — the S3 bucket / Glue database becomes a ``data`` source
  and every consumer switches to the ``data.`` address in the same branch
  (a half-applied switch leaves a dangling reference and fails ``tofu
  validate``). A referenced bucket carries **no** ``force_destroy``, and the
  grants it does emit narrow to the binding's ``location.path`` prefix so a
  tenant cannot reach another tenant's objects in the pool.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

from ...providers._sql_safety import quote_string_literal, validate_ident
from ...providers.aws.util import warehouse as _warehouse
from ..importer import ImportBlock
from ..naming import TofuExpr, safe_ident, tofu_ref
from ..packaging import (
    ContainerDecision,
    PackagingError,
    PackagingResolution,
    resolve_packaging,
)
from ..versions import required_providers

# Apply-time AWS account placeholder for the credential-free warehouse fallback.
# Resolves at ``tofu apply`` so ``main.tf.json`` stays account-agnostic while
# matching the native planner's ``{account_id}-fluid-data`` bucket. The backing
# ``data.aws_caller_identity.fluid_lf_caller`` source is emitted by ``emit_data``
# whenever a bucket-less Glue binding is present.
_CALLER_ACCOUNT_TOKEN = str(tofu_ref("data.aws_caller_identity.fluid_lf_caller.account_id"))


def _has_custom_aws_endpoint() -> bool:
    """True when an ``AWS_ENDPOINT_URL`` override targets a non-AWS endpoint.

    The ``hashicorp/aws`` provider reads ``AWS_ENDPOINT_URL`` (global) and
    ``AWS_ENDPOINT_URL_<SERVICE>`` (per-service, e.g. ``AWS_ENDPOINT_URL_S3``)
    natively. Their presence is the signal that a contract is being applied
    against an emulator (LocalStack / moto) rather than real AWS — see
    :meth:`AWSProvider.provider_block`.
    """
    return any(
        key == "AWS_ENDPOINT_URL" or key.startswith("AWS_ENDPOINT_URL_") for key in os.environ
    )


def _resolve_catalog_id() -> str:
    """Resolve the AWS account id for Glue catalog import ids.

    Priority: ``AWS_ACCOUNT_ID`` env var → ``sts:GetCallerIdentity``.
    Returns ``""`` on failure (no boto3, no creds, network error) —
    callers gate on a truthy return so the missing-catalog-id case
    suppresses the import block instead of emitting a malformed one.
    Cached on the process via a module-level dict on the first call.
    """
    if "AWS_ACCOUNT_ID" in os.environ and os.environ["AWS_ACCOUNT_ID"].strip():
        return os.environ["AWS_ACCOUNT_ID"].strip()
    cached = _CATALOG_ID_CACHE.get("value")
    if cached is not None:
        return cached
    try:
        import boto3

        identity = boto3.client("sts").get_caller_identity()
        value = str(identity.get("Account") or "")
    except Exception:  # noqa: BLE001 — best-effort; emit no import on failure
        value = ""
    _CATALOG_ID_CACHE["value"] = value
    return value


_CATALOG_ID_CACHE: Dict[str, str] = {}

# FLUID column type → Hive/Glue column type.
_HIVE_TYPES = {
    "string": "string",
    "str": "string",
    "text": "string",
    "integer": "int",
    "int": "int",
    "int32": "int",
    "bigint": "bigint",
    "int64": "bigint",
    "long": "bigint",
    "float": "float",
    "float32": "float",
    "double": "double",
    "float64": "double",
    "boolean": "boolean",
    "bool": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "binary": "binary",
    "bytes": "binary",
}


def _hive_type(raw: Any) -> str:
    t = str(raw or "string").strip().lower()
    if t.startswith(("decimal", "numeric")):
        # decimal(10,2) passes through; a bare type widens to a safe default.
        return t.replace("numeric", "decimal") if "(" in t else "decimal(38,9)"
    return _HIVE_TYPES.get(t, "string")


def _columns(schema: List[Mapping[str, Any]]) -> List[Dict[str, str]]:
    columns: List[Dict[str, str]] = []
    for col in schema or []:
        entry: Dict[str, str] = {"name": col.get("name"), "type": _hive_type(col.get("type"))}
        if col.get("description"):
            entry["comment"] = col["description"]
        columns.append(entry)
    return columns


@dataclass(frozen=True)
class _Placement:
    """One exposure's resolved container ownership (see the module docstring)."""

    bucket_referenced: bool
    database_referenced: bool
    pool: Optional[str]


#: Every container LEGACY — today's emit path.
_LEGACY_PLACEMENT = _Placement(bucket_referenced=False, database_referenced=False, pool=None)


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
        bucket_referenced=(
            resolution.decision_for("bucket", expose_id) is ContainerDecision.REFERENCED
        ),
        # ``database`` covers the AWS Glue catalog database as well as the
        # Snowflake database — one kind, per the RFC's normative mapping.
        database_referenced=(
            resolution.decision_for("database", expose_id) is ContainerDecision.REFERENCED
        ),
        pool=(pool_exposure.pool if pool_exposure is not None else resolution.pool),
    )


def _tags_for(base: Mapping[str, str], placement: _Placement) -> Dict[str, str]:
    """Contract tags plus ``fluid_pool`` when a packaging pool is in scope.

    Absent a ``packaging`` block there is no pool, so every existing
    contract's tags are unchanged.
    """
    tags = dict(base)
    if placement.pool:
        tags["fluid_pool"] = str(placement.pool)
    return tags


def _require_pool_prefix(placement: _Placement, path: str, *, what: str) -> None:
    """Fail closed when a bucket-level grant on a POOL bucket cannot be scoped.

    SECURITY: every bucket-level control this module emits — the Lake
    Formation location registration and the ``aws_s3_bucket_policy`` — is
    scoped by the binding's ``location.path``. With no path there is no
    prefix to scope to, and the control silently degrades to the whole
    bucket: a cross-account principal named in ``governance.lakeFormation.
    grants[]`` would get ``s3:GetObject`` on ``arn:aws:s3:::<pool>/*``,
    reaching every other tenant's objects, and (for the bucket policy,
    which is authoritative for the entire bucket) replacing whatever
    isolation the platform team had configured.

    Widening a shared pool is the precise failure this feature exists to
    prevent, so an unscopeable grant is an error rather than a quiet
    degradation — the same discipline as the resolver's ``pool-required``
    (a shared container must be addressable). Never fires for an owned or
    LEGACY bucket, where owning the whole bucket is the point.
    """
    if placement.bucket_referenced and not path:
        raise PackagingError(
            "shared-bucket-requires-path",
            f"{what} targets a shared (pool) bucket but the binding declares no "
            "`location.path` — a bucket-level grant on a pool would reach every "
            "other tenant's objects. Add a `location.path` prefix, or declare the "
            "bucket `isolated` if this product really owns it.",
        )


def _referenced_bucket_name(loc: Mapping[str, Any]) -> str:
    """The canonical name of a **shared (pool)** S3 bucket.

    THE one derivation for a REFERENCED bucket — used by
    :func:`_emit_referenced_containers` for the ``data.aws_s3_bucket`` key
    and by :func:`_emit_lakeformation` for every reference to it. PR2
    shipped these two sides on different resolvers (``normalize_location``
    vs the raw contract value), so a ``{{ env.* }}`` bucket declared the
    lookup under one key and referenced it under another — a dangling
    ``${data.aws_s3_bucket.…}`` that fails ``tofu validate``. One function,
    one answer.

    **Fails closed on an unresolvable name.** ``normalize_location`` falls
    back to ``{account}-fluid-data`` when the template does not resolve,
    which is right for a bucket this product *owns* and wrong for a pool:
    it would silently point the product at a different bucket than the one
    it declared, and register LF / bucket-policy grants there. A pool must
    be addressable — the same discipline as the resolver's ``pool-required``
    and :func:`_require_pool_prefix`.
    """
    bucket, _ = _warehouse.normalize_location(
        loc, account_ref=_CALLER_ACCOUNT_TOKEN, default_path=False
    )
    if "{{" in bucket or _CALLER_ACCOUNT_TOKEN in bucket:
        raise PackagingError(
            "shared-bucket-unresolved",
            f"the shared (pool) bucket {loc.get('bucket')!r} could not be resolved to a "
            "concrete name — a pool bucket must be addressable, and falling back to the "
            "`{account}-fluid-data` bucket would point this product at storage it never "
            "declared. Set the environment variable the template names, write the bucket "
            "name literally, or declare the bucket `isolated` if this product owns it.",
        )
    return bucket


def _glue_db_ref(db_name: str, database: Any, *, referenced: bool) -> Any:
    """How a consumer addresses the Glue catalog database.

    Owned → a resource cross-reference. REFERENCED → the **literal** database
    name, exactly as the Snowflake plugin inlines a pooled database, because
    ``hashicorp/aws`` ships ``aws_glue_catalog_database`` as a *resource only*
    — there is no matching data source (verified against the real provider:
    ``tofu validate`` rejects it with "The provider hashicorp/aws does not
    support data source"). A Glue database is addressed by name anyway, so a
    lookup would buy nothing beyond an existence check.

    The literal is contract-derived and deliberately NOT wrapped in
    ``TofuExpr``, so ``render_tofu_json`` escapes any ``${`` in it.
    """
    if referenced:
        return str(database)
    return tofu_ref(f"aws_glue_catalog_database.{db_name}.name")


def _s3_bucket_ref(bucket_key: str, *, referenced: bool, attr: str = "id") -> TofuExpr:
    """The address of an S3 bucket — resource or data source."""
    return tofu_ref(f"{'data.' if referenced else ''}aws_s3_bucket.{bucket_key}.{attr}")


class AwsIacPlugin:
    """``IacProviderPlugin`` for Amazon Web Services (Glue catalog + S3)."""

    name = "aws"
    # `archive` zips inline Lambda source via `data.archive_file`; `null`
    # backs the ``redshift-data`` ``CREATE EXTERNAL SCHEMA`` bridge (no
    # first-party ``aws_redshiftserverless_external_schema`` resource in
    # ``hashicorp/aws`` today — see :func:`_emit_redshift_external_schema`).
    required_providers = required_providers("aws", "archive", "null")
    # `tofu` reads whichever AWS_* var is set; the emitted `.tf.json`
    # stays credential-free regardless of the auth method.
    credential_env_vars = (
        # Static / temporary credentials.
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        # Named profile + shared config / credentials files.
        "AWS_PROFILE",
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        # AssumeRoleWithWebIdentity — OIDC federation (CI runners, EKS IRSA).
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_SESSION_NAME",
        # Region.
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )

    def emit(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        resources: Dict[str, Dict[str, Any]] = {}
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        base_tags = {"managed_by": "fluid", "fluid_contract": cid}
        packaging = resolve_packaging(contract)

        # Account-level Lake Formation settings: admins + LF-tag
        # definitions. Emitted once per contract, before per-exposure
        # resources so the LF tag-definitions exist before any
        # resource_lf_tags association references them.
        _emit_lf_account_settings(resources, contract, cid, base_tags)

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "aws":
                continue
            loc = binding.get("location") or {}
            fmt = binding.get("format") or "parquet"
            schema = (exposure.get("contract") or {}).get("schema") or []
            placement = _placement(packaging, exposure)
            tags = _tags_for(base_tags, placement)
            _emit_glue(
                resources, loc, fmt, schema, cid, tags, contract=contract, placement=placement
            )
            _emit_s3(resources, loc, cid, tags, placement=placement)
            _emit_kinesis(resources, loc, cid, tags)
            _emit_redshift_serverless(resources, loc, cid, tags)
            _emit_redshift_external_schema(resources, loc, cid, tags)
            # Per-exposure Lake Formation: location registration,
            # principal grants, LF-tag associations, row/column filters.
            # Only fires when the binding carries a governance.lakeFormation
            # block — every existing AWS contract is unaffected.
            _emit_lakeformation(resources, binding, loc, fmt, cid, tags, placement=placement)
        # Glue ETL jobs / Step Functions / the Lambda schedule path —
        # the planner's build & orchestration ops.
        _emit_from_actions(resources, actions, cid)
        # Second pass — wire ordering edges that the literal-string fields
        # on Redshift external schemas / planned-action resources don't
        # carry by value. See :func:`_wire_aws_deps`.
        _wire_aws_deps(resources, cid)
        return resources

    def emit_data(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> Dict[str, Any]:
        """``archive_file`` data sources — inline Lambda source, zipped by ``tofu``.

        Also emits ``aws_caller_identity`` when any Lake Formation
        resource references the caller's account ID (data-cells filters
        and certain LF grants need ``catalog_id``). The data source is
        a no-op when not referenced.

        Under a ``shared`` packaging mode this additionally looks up the pool
        S3 bucket rather than declaring it, so ``tofu`` never plans to create,
        modify or destroy a platform-owned bucket. (A pooled Glue database
        gets no data source — ``hashicorp/aws`` has none; consumers inline
        its literal name, see :func:`_glue_db_ref`.) Nothing is added for a
        LEGACY contract.
        """
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        data: Dict[str, Any] = {}
        archives: Dict[str, Any] = {}
        for action in actions or []:
            if isinstance(action, Mapping) and action.get("op") == "lambda.ensure_function":
                _emit_lambda_archive(archives, action, cid)
        if archives:
            data["archive_file"] = archives
        _emit_referenced_containers(data, contract, cid)
        # Lake Formation data-cells filters (and other LF resources) need
        # the calling AWS account ID as ``catalog_id``. Emit the
        # ``aws_caller_identity`` data source when any LF feature is used
        # so downstream resources can ``tofu_ref`` ``account_id`` off it.
        # ...and whenever a binding's warehouse falls back to the
        # ``{account}-fluid-data`` bucket, whose token references this source.
        if _contract_uses_lakeformation(contract) or _references_caller_account(contract):
            data.setdefault("aws_caller_identity", {})["fluid_lf_caller"] = {}
        return data

    def credential_env(self, env: Mapping[str, str]) -> Dict[str, str]:
        """The ``hashicorp/aws`` provider reads the standard ``AWS_*``
        environment (and ``~/.aws`` files) directly — no translation."""
        return {}

    def discover_imports(
        self, contract: Mapping[str, Any], actions: Iterable[Mapping[str, Any]] = ()
    ) -> List[ImportBlock]:
        """Brownfield ``tofu import`` candidates for each contract-declared AWS resource.

        Mirrors what :meth:`emit` produces; the apply engine calls
        ``tofu import`` for each block before ``tofu apply``. Imports
        that miss (the resource doesn't exist yet) are tolerated by
        ``_adopt_existing`` and left for ``tofu apply`` to create.

        Import IDs follow the ``hashicorp/aws`` provider's documented
        identifiers:

          * ``aws_glue_catalog_database`` — ``{catalog_id}:{name}``
            (catalog_id = AWS account id; required by the provider)
          * ``aws_glue_catalog_table`` — ``{catalog_id}:{database}:{name}``
          * ``aws_s3_bucket`` — bucket name
          * ``aws_kinesis_stream`` — stream name
          * ``aws_redshiftserverless_namespace`` — namespace name

        The catalog_id is read from ``AWS_ACCOUNT_ID`` if set; otherwise
        a call to ``sts:GetCallerIdentity`` resolves it (one call per
        invocation, cached). Without it, ``tofu import`` on the Glue
        resources fails ``Invalid import id`` and the apply then fails
        ``AlreadyExistsException`` — verified by the live brownfield
        test pinning this behaviour.

        REFERENCED containers are excluded (RFC file 3): ``_adopt_existing``
        runs on every apply, so an ungated shared pool bucket / Glue database
        would be ``tofu import``-ed into this product's state — re-owning the
        platform's pool, which is precisely the hazard shared mode exists to
        prevent. Leaf resources inside the pool (the Glue table) stay
        importable; only the containers are withheld.
        """
        cid = safe_ident(contract.get("id") or contract.get("name") or "product")
        packaging = resolve_packaging(contract)
        blocks: List[ImportBlock] = []
        seen: set[str] = set()

        def _add(address: str, resource_id: str) -> None:
            if address not in seen:
                seen.add(address)
                blocks.append(ImportBlock(to=address, id=resource_id))

        # Resolve catalog_id once — only when needed (Glue catalog refs).
        contract_needs_catalog_id = any(
            (b.get("binding") or {}).get("platform") == "aws"
            and ((b.get("binding") or {}).get("location") or {}).get("database")
            and str(((b.get("binding") or {}).get("format")) or "").lower() in _GLUE_CATALOG_FORMATS
            for b in contract.get("exposes") or []
        )
        catalog_id = _resolve_catalog_id() if contract_needs_catalog_id else ""

        for exposure in contract.get("exposes") or []:
            binding = exposure.get("binding") or {}
            if binding.get("platform") != "aws":
                continue
            loc = binding.get("location") or {}
            fmt = binding.get("format") or "parquet"
            placement = _placement(packaging, exposure)
            database = loc.get("database")
            table = loc.get("table")
            bucket = loc.get("bucket")
            stream = loc.get("stream")
            namespace = loc.get("namespace") or loc.get("workgroup")

            # Glue catalog resources — only file/lakehouse formats use
            # the Glue catalog (mirrors ``_emit_glue``'s gate).
            if database and catalog_id and str(fmt or "").lower() in _GLUE_CATALOG_FORMATS:
                db_key = safe_ident(f"{cid}_{database}")
                if not placement.database_referenced:
                    # provider id: ``{catalog_id}:{name}``
                    _add(
                        f"aws_glue_catalog_database.{db_key}",
                        f"{catalog_id}:{database}",
                    )
                if table:
                    table_key = safe_ident(f"{cid}_{database}_{table}")
                    # provider id: ``{catalog_id}:{database}:{name}``
                    _add(
                        f"aws_glue_catalog_table.{table_key}",
                        f"{catalog_id}:{database}:{table}",
                    )

            # S3 bucket — provider id is the bucket name.
            if bucket and not placement.bucket_referenced:
                bucket_key = safe_ident(f"{cid}_{bucket}")
                _add(f"aws_s3_bucket.{bucket_key}", bucket)

            # Kinesis stream — provider id is the stream name.
            if stream:
                stream_key = safe_ident(f"{cid}_{stream}")
                _add(f"aws_kinesis_stream.{stream_key}", stream)

            # Redshift Serverless namespace — provider id is the
            # namespace name.
            if namespace:
                ns_key = safe_ident(f"{cid}_{namespace}")
                _add(f"aws_redshiftserverless_namespace.{ns_key}", namespace)

        return blocks

    def provider_block(self) -> Dict[str, Any]:
        """Static provider configuration for the ``hashicorp/aws`` provider.

        On real AWS this is empty — the provider self-configures from the
        environment (credentials, region, and ``AWS_ENDPOINT_URL*`` service
        overrides are all read natively). When a custom endpoint IS set (i.e.
        the contract is being applied against LocalStack / moto / another
        emulator), emit the emulator-compatibility settings the provider can't
        infer from the environment:

        * ``s3_use_path_style`` — virtual-host addressing (``<bucket>.<host>``)
          can't resolve against a single-host emulator, so S3 bucket creates
          fail without path-style.
        * ``skip_credentials_validation`` / ``skip_requesting_account_id`` /
          ``skip_metadata_api_check`` / ``skip_region_validation`` — the
          STS / IAM / EC2-metadata validations the AWS provider runs on
          startup that an emulator doesn't fully implement.

        Gated on :func:`_has_custom_aws_endpoint`, so real-AWS applies are
        byte-for-byte unchanged (no provider block emitted). Mirrors
        LocalStack's documented Terraform provider setup.
        """
        if not _has_custom_aws_endpoint():
            return {}
        return {
            "s3_use_path_style": True,
            "skip_credentials_validation": True,
            "skip_requesting_account_id": True,
            "skip_metadata_api_check": True,
            "skip_region_validation": True,
        }


#: Bindings whose ``location.database`` field names a Glue catalog
#: database (the mesh-interface case). For Redshift-flavoured formats
#: the ``database`` field names a *Redshift* database internal to the
#: workgroup and must NOT trigger a Glue catalog emit — doing so used
#: to create a phantom Glue DB called ``"fluid"`` per Redshift test
#: that collided across runs and broke applies with
#: ``AlreadyExistsException``.
_GLUE_CATALOG_FORMATS: frozenset = frozenset(
    {"iceberg", "parquet", "csv", "json", "avro", "orc", "delta"}
)


def _emit_referenced_containers(
    data: Dict[str, Any], contract: Mapping[str, Any], cid: str
) -> None:
    """Add ``data`` lookups for every REFERENCED container that has one.

    The gates mirror :func:`_emit_s3` exactly — same truthiness check — so
    the emitted ``data`` block and the ``resource`` block can never disagree
    about which containers exist. A mismatch shows up as either an orphan
    data source or a dangling ``${data.…}`` reference that fails ``tofu
    validate``.
    """
    packaging = resolve_packaging(contract)
    if packaging.is_legacy:
        return
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if binding.get("platform") != "aws":
            continue
        loc = binding.get("location") or {}
        placement = _placement(packaging, exposure)

        # NB: a REFERENCED Glue database emits NO data source —
        # ``hashicorp/aws`` has no ``aws_glue_catalog_database`` data source,
        # so consumers inline the literal name instead (see
        # :func:`_glue_db_ref`). Only the S3 bucket is looked up.
        if loc.get("bucket") and placement.bucket_referenced:
            # Resolved through the single canonical derivation so the key
            # here and the ``${data.aws_s3_bucket.<key>.id}`` references
            # ``_emit_lakeformation`` writes can never disagree.
            bucket = _referenced_bucket_name(loc)
            data.setdefault("aws_s3_bucket", {}).setdefault(
                safe_ident(f"{cid}_{bucket}"), {"bucket": bucket}
            )


def _emit_glue(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    fmt: str,
    schema: List[Mapping[str, Any]],
    cid: str,
    tags: Dict[str, str],
    *,
    contract: Optional[Mapping[str, Any]] = None,
    placement: _Placement = _LEGACY_PLACEMENT,
) -> None:
    database = loc.get("database")
    if not database:
        return
    # Only file/lakehouse formats use the Glue catalog as their
    # storage-and-schema registry. Redshift-flavoured bindings (whose
    # ``database`` is internal to the workgroup) skip this emit.
    if str(fmt or "").lower() not in _GLUE_CATALOG_FORMATS:
        return
    db_name = safe_ident(f"{cid}_{database}")
    if not placement.database_referenced:
        # ``parameters`` and other Lake-Formation-managed fields drift
        # post-create (AWS sets things like ``CreatedBy``,
        # ``last_commit_time``, LF auto-flags). See the table emit below
        # for the same rationale.
        resources.setdefault("aws_glue_catalog_database", {}).setdefault(
            db_name,
            {
                "name": database,
                "lifecycle": {"ignore_changes": ["parameters"]},
            },
        )

    table = loc.get("table")
    if not table:
        return
    # The IaC plugin now owns the catalog-metadata enrichments the
    # old ``catalog_registrars.glue`` registrar used to push via
    # ``glue:UpdateTable`` (retired in this branch). Folding it here
    # gives a single source of truth + drift detection — the registrar
    # had write-once-on-create semantics and never reconciled the
    # parameters again. Mirrors how the Glue Terraform Registry
    # examples model catalog metadata + descriptions in one resource.
    storage: Dict[str, Any] = {"columns": _columns(schema)}
    # Single canonical warehouse writer (RFC §7): identical derivation to the
    # native planner.
    #
    # SECURITY: contract-derived bucket/path must NEVER reach a raw ``TofuExpr``.
    # ``TofuExpr`` tells the renderer to leave ``${...}`` un-escaped, so wrapping
    # contract content would let a malicious binding inject OpenTofu
    # interpolation (e.g. ``${file("/etc/passwd")}``) into the emitted module —
    # bypassing ``_escape_tofu_literals``. The ONLY deliberate interpolation here
    # is the emitter's own account-id fallback token. So: when the bucket is the
    # emitter fallback, the contract-derived path is explicitly escaped before it
    # goes inside the TofuExpr; otherwise the whole value is a plain literal that
    # the renderer escapes at render time. Fallback is decided on the raw input
    # (``bucket_uses_fallback``), which a contract cannot spoof.
    bucket, path = _warehouse.normalize_location(loc, account_ref=_CALLER_ACCOUNT_TOKEN)
    if _warehouse.bucket_uses_fallback(loc):
        safe_path = path.replace("${", "$${").replace("%{", "%%{")
        storage["location"] = TofuExpr(f"s3://{bucket}/{safe_path}")
    else:
        storage["location"] = f"s3://{bucket}/{path}"
    parameters: Dict[str, str] = {"classification": fmt, "managed_by": "fluid"}
    if "iceberg" in str(fmt).lower():
        # AWS Glue / Athena identify an Iceberg table via this parameter.
        parameters["table_type"] = "ICEBERG"

    # Contract-driven enrichments (absorbed from the retired registrar).
    description = ""
    if contract is not None:
        meta = contract.get("metadata") or {}
        layer = meta.get("layer")
        product_type = meta.get("productType") or meta.get("product_type")
        domain = contract.get("domain")
        version = contract.get("fluidVersion")
        if layer:
            parameters["fluid_layer"] = str(layer)
        if product_type:
            parameters["fluid_product_type"] = str(product_type)
        if domain:
            parameters["fluid_domain"] = str(domain)
        if version:
            parameters["fluid_version"] = str(version)
        # The packaging pool id, alongside the other ``fluid_*`` catalog
        # parameters, so a Glue/Athena consumer can attribute the table to
        # its platform pool. Absent for every contract with no packaging
        # block, which is what keeps the byte-parity pin green.
        if placement.pool:
            parameters["fluid_pool"] = str(placement.pool)
        # Column-level tags from the contract's ``schema[].tags`` field
        # (already in v0.7.3 — ``$defs.column.properties.tags``). Emitted
        # as the legacy ``forge.pii.<col>`` Glue parameter the retired
        # registrar used to push, so existing analyst dashboards built
        # on those parameter keys keep working. No new schema fields
        # needed — we read what the contract already carries.
        for col in schema or []:
            col_tags = col.get("tags") or []
            if col_tags:
                parameters[f"forge.pii.{col.get('name')}"] = ",".join(str(t) for t in col_tags)
        # ``fluid_contract`` carries the canonical FLUID YAML so the
        # AWS console + boto3 GetTable callers see the full contract
        # without leaving the catalog. Truncated at 50KB which is
        # well under Glue's ``Parameters`` value limit (512KB per
        # value, 8 MB per map).
        try:
            fluid_yaml = yaml.safe_dump(dict(contract), sort_keys=False)
            if len(fluid_yaml) <= 50_000:
                parameters["fluid_contract"] = fluid_yaml
        except Exception:  # noqa: BLE001 — best-effort, drop on yaml error
            pass

        # Table-level description — first non-empty of
        # ``metadata.description`` / contract-level ``description``.
        # We deliberately do NOT fall back to ``contract.name`` because
        # name is an identifier, not a free-text description; the
        # Glue console renders it as the header which would duplicate
        # the table name with no analyst value.
        description = meta.get("description") or contract.get("description") or ""

    table_body: Dict[str, Any] = {
        "name": table,
        "database_name": _glue_db_ref(db_name, database, referenced=placement.database_referenced),
        "table_type": "EXTERNAL_TABLE",
        "parameters": parameters,
        "storage_descriptor": storage,
        # AWS Glue silently augments tables post-create with operational
        # parameters (``CrawlerSchemaDeserializerVersion``,
        # ``UPDATED_BY_CRAWLER``, Lake Formation auto-flags, last-updated
        # timestamps, ...). Re-applying our credentials-free .tf.json
        # would then plan an "update" to reset those — non-idempotent
        # churn that has no semantic effect. ``ignore_changes`` on the
        # whole ``parameters`` map keeps post-apply state aligned;
        # forge-cli's own parameters (``classification`` / ``managed_by``
        # / ``table_type`` / ``fluid_*``) ARE set on Create so they
        # always start correctly. To force a contract-driven re-set
        # use ``--mode replace`` or ``tofu taint``.
        "lifecycle": {"ignore_changes": ["parameters"]},
    }
    if description:
        table_body["description"] = description

    resources.setdefault("aws_glue_catalog_table", {})[
        safe_ident(f"{cid}_{database}_{table}")
    ] = table_body


def _emit_s3(
    resources: Dict[str, Any],
    loc: Mapping[str, Any],
    cid: str,
    tags: Dict[str, str],
    *,
    placement: _Placement = _LEGACY_PLACEMENT,
) -> None:
    bucket = loc.get("bucket")
    if not bucket:
        return
    if placement.bucket_referenced:
        # A shared pool bucket is looked up in ``emit_data``, never created.
        # Critically it carries no ``force_destroy``: on a pool that flag
        # would let one tenant's ``tofu destroy`` delete every other
        # tenant's objects — the blast radius this feature exists to close.
        return
    resources.setdefault("aws_s3_bucket", {}).setdefault(
        safe_ident(f"{cid}_{bucket}"),
        {"bucket": bucket, "force_destroy": True, "tags": tags},
    )


def _emit_kinesis(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    stream = loc.get("stream")
    if not stream:
        return
    resources.setdefault("aws_kinesis_stream", {}).setdefault(
        safe_ident(f"{cid}_{stream}"),
        {
            "name": stream,
            # On-demand capacity — auto-scales, no shard-count math.
            "stream_mode_details": [{"stream_mode": "ON_DEMAND"}],
            "tags": tags,
        },
    )


def _emit_from_actions(
    resources: Dict[str, Any], actions: Iterable[Mapping[str, Any]], cid: str
) -> None:
    """Translate the planner's build / orchestration ops into ``hashicorp/aws`` resources.

    Covers Glue ETL jobs, Step Functions, and the Lambda schedule / event
    path — inline Lambda source is zipped by ``data.archive_file`` (see
    :meth:`AwsIacPlugin.emit_data`). MWAA is still skipped: ``aws_mwaa_environment``
    needs VPC ``network_configuration`` the contract does not carry.
    """
    for action in actions or []:
        if not isinstance(action, Mapping):
            continue
        op = action.get("op")
        if op == "glue.ensure_job":
            _emit_glue_job(resources, action, cid)
        elif op == "stepfunctions.ensure_state_machine":
            _emit_state_machine(resources, action, cid)
        elif op == "lambda.ensure_function":
            _emit_lambda_function(resources, action, cid)
        elif op == "lambda.add_permission":
            _emit_lambda_permission(resources, action, cid)
        elif op == "lambda.create_event_source_mapping":
            _emit_event_source_mapping(resources, action, cid)
        elif op == "eventbridge.ensure_schedule":
            _emit_scheduler_schedule(resources, action, cid)
        elif op == "eventbridge.ensure_rule":
            _emit_event_rule(resources, action, cid)
        elif op == "s3.ensure_notification":
            _emit_s3_notification(resources, action, cid)


def _emit_glue_job(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``glue.ensure_job`` → ``aws_glue_job`` (a Glue ETL job).

    The planner only emits this op when both an IAM ``role`` and an S3
    ``script_location`` are present — so the job is fully declarative.
    """
    name = action.get("name")
    role = action.get("role")
    script = action.get("script_location")
    if not (name and role and script):
        return
    body: Dict[str, Any] = {
        "name": name,
        "role_arn": role,
        "command": {"name": action.get("command_name", "glueetl"), "script_location": script},
    }
    for key in ("glue_version", "worker_type", "timeout", "max_retries", "description"):
        if action.get(key) is not None:
            body[key] = action[key]
    if action.get("number_of_workers") is not None:
        body["number_of_workers"] = int(action["number_of_workers"])
    if action.get("default_arguments"):
        body["default_arguments"] = action["default_arguments"]
    if action.get("connections"):
        body["connections"] = list(action["connections"])
    if action.get("tags"):
        body["tags"] = action["tags"]
    resources.setdefault("aws_glue_job", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_state_machine(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``stepfunctions.ensure_state_machine`` → ``aws_sfn_state_machine``."""
    name = action.get("state_machine_name")
    role = action.get("role_arn")
    definition = action.get("definition")
    if not (name and role and definition):
        return
    body: Dict[str, Any] = {"name": name, "role_arn": role, "definition": definition}
    if action.get("type"):
        body["type"] = action["type"]
    if action.get("tags"):
        body["tags"] = action["tags"]
    resources.setdefault("aws_sfn_state_machine", {})[safe_ident(f"{cid}_{name}")] = body


# ── Lambda schedule / event path ────────────────────────────────────


def _lambda_source(action: Mapping[str, Any]) -> str:
    """Extract the Python source from a ``lambda.ensure_function`` action.

    The planner returns the code as ``{"ZipFile": <source>}`` (the boto3
    inline-code shape) or, defensively, a bare string.
    """
    code = action.get("code")
    if isinstance(code, Mapping):
        return str(code.get("ZipFile") or "")
    return str(code or "")


def _lambda_res(cid: str, function_name: Any) -> str:
    """Resource name for a Lambda function — shared by ``emit`` and ``emit_data``."""
    return safe_ident(f"{cid}_lambda_{function_name}")


def _lambda_res_from_arn(arn: Any, cid: str) -> str:
    """Reconstruct a Lambda function's resource name from its ARN."""
    return _lambda_res(cid, str(arn or "").rsplit(":function:", 1)[-1])


def _lambda_ref(resources: Dict[str, Any], res: str, literal: Any, attr: str) -> Any:
    """Interpolate a co-emitted Lambda's attribute, else fall back to a literal.

    The planner co-emits a function with its permission / schedule / rule,
    so the interpolation is normally live; a contract that targets a
    pre-existing function keeps the literal ARN rather than dangling.
    """
    if res in resources.get("aws_lambda_function", {}):
        return tofu_ref(f"aws_lambda_function.{res}.{attr}")
    return literal


def _emit_lambda_archive(archives: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``lambda.ensure_function`` → a ``data.archive_file`` (inline source, zipped by tofu)."""
    function_name = action.get("function_name")
    source = _lambda_source(action)
    if not (function_name and source):
        return
    res = _lambda_res(cid, function_name)
    archives.setdefault(
        res,
        {
            "type": "zip",
            "output_path": TofuExpr(f"${{path.module}}/{res}.zip"),
            "source": [{"content": source, "filename": "index.py"}],
        },
    )


def _emit_lambda_function(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``lambda.ensure_function`` → ``aws_lambda_function`` (code via ``data.archive_file``)."""
    function_name = action.get("function_name")
    role = action.get("role")
    if not (function_name and role and _lambda_source(action)):
        return
    res = _lambda_res(cid, function_name)
    body: Dict[str, Any] = {
        "function_name": function_name,
        "role": role,
        "runtime": action.get("runtime", "python3.11"),
        "handler": action.get("handler", "index.handler"),
        "filename": tofu_ref(f"data.archive_file.{res}.output_path"),
        "source_code_hash": tofu_ref(f"data.archive_file.{res}.output_base64sha256"),
    }
    if action.get("timeout") is not None:
        body["timeout"] = int(action["timeout"])
    if action.get("memory_size") is not None:
        body["memory_size"] = int(action["memory_size"])
    env = action.get("environment")
    if isinstance(env, Mapping) and env:
        body["environment"] = {"variables": dict(env)}
    if action.get("tags"):
        body["tags"] = action["tags"]
    resources.setdefault("aws_lambda_function", {})[res] = body


def _emit_lambda_permission(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``lambda.add_permission`` → ``aws_lambda_permission``."""
    function_name = action.get("function_name")
    statement_id = action.get("statement_id")
    principal = action.get("principal")
    if not (function_name and statement_id and principal):
        return
    res = _lambda_res(cid, function_name)
    body: Dict[str, Any] = {
        "statement_id": statement_id,
        "action": action.get("action", "lambda:InvokeFunction"),
        "function_name": _lambda_ref(resources, res, function_name, "function_name"),
        "principal": principal,
    }
    if action.get("source_arn"):
        body["source_arn"] = action["source_arn"]
    resources.setdefault("aws_lambda_permission", {})[safe_ident(f"{cid}_{statement_id}")] = body


def _emit_event_source_mapping(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str
) -> None:
    """``lambda.create_event_source_mapping`` → ``aws_lambda_event_source_mapping``."""
    function_name = action.get("function_name")
    source_arn = action.get("event_source_arn")
    if not (function_name and source_arn):
        return
    res = _lambda_res(cid, function_name)
    body: Dict[str, Any] = {
        "event_source_arn": source_arn,
        "function_name": _lambda_ref(resources, res, function_name, "arn"),
    }
    # `starting_position` applies to stream sources (Kinesis / DynamoDB),
    # not SQS — emit it only when the planner supplied one.
    if action.get("starting_position"):
        body["starting_position"] = action["starting_position"]
    if action.get("batch_size") is not None:
        body["batch_size"] = int(action["batch_size"])
    if action.get("maximum_batching_window_in_seconds") is not None:
        body["maximum_batching_window_in_seconds"] = int(
            action["maximum_batching_window_in_seconds"]
        )
    if action.get("parallelization_factor") is not None:
        body["parallelization_factor"] = int(action["parallelization_factor"])
    resources.setdefault("aws_lambda_event_source_mapping", {})[
        safe_ident(f"{cid}_esm_{function_name}")
    ] = body


def _emit_scheduler_schedule(
    resources: Dict[str, Any], action: Mapping[str, Any], cid: str
) -> None:
    """``eventbridge.ensure_schedule`` → ``aws_scheduler_schedule``."""
    name = action.get("schedule_name")
    expression = action.get("schedule_expression")
    target = action.get("target") or {}
    target_arn = target.get("arn")
    role_arn = target.get("role_arn")
    if not (name and expression and target_arn and role_arn):
        return
    res = _lambda_res_from_arn(target_arn, cid)
    target_body: Dict[str, Any] = {
        "arn": _lambda_ref(resources, res, target_arn, "arn"),
        "role_arn": role_arn,
    }
    if target.get("input"):
        target_body["input"] = target["input"]
    ftw = action.get("flexible_time_window") or {}
    body: Dict[str, Any] = {
        "name": name,
        "schedule_expression": expression,
        "flexible_time_window": {"mode": ftw.get("mode", "OFF")},
        "target": target_body,
    }
    if action.get("timezone"):
        body["schedule_expression_timezone"] = action["timezone"]
    if action.get("state"):
        body["state"] = action["state"]
    if action.get("description"):
        body["description"] = action["description"]
    resources.setdefault("aws_scheduler_schedule", {})[safe_ident(f"{cid}_{name}")] = body


def _emit_event_rule(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``eventbridge.ensure_rule`` → ``aws_cloudwatch_event_rule`` + ``_event_target``."""
    name = action.get("rule_name")
    if not name:
        return
    rule_res = safe_ident(f"{cid}_{name}")
    rule_body: Dict[str, Any] = {"name": name}
    if action.get("event_pattern"):
        rule_body["event_pattern"] = action["event_pattern"]
    if action.get("state"):
        rule_body["state"] = action["state"]
    if action.get("description"):
        rule_body["description"] = action["description"]
    resources.setdefault("aws_cloudwatch_event_rule", {})[rule_res] = rule_body
    for target in action.get("targets") or []:
        if not isinstance(target, Mapping):
            continue
        arn = target.get("arn")
        target_id = target.get("id")
        if not (arn and target_id):
            continue
        lambda_res = _lambda_res_from_arn(arn, cid)
        resources.setdefault("aws_cloudwatch_event_target", {})[
            safe_ident(f"{cid}_{name}_{target_id}")
        ] = {
            "rule": tofu_ref(f"aws_cloudwatch_event_rule.{rule_res}.name"),
            "target_id": str(target_id),
            "arn": _lambda_ref(resources, lambda_res, arn, "arn"),
        }


def _emit_s3_notification(resources: Dict[str, Any], action: Mapping[str, Any], cid: str) -> None:
    """``s3.ensure_notification`` → ``aws_s3_bucket_notification`` (Lambda target)."""
    bucket = action.get("bucket")
    lambda_arn = action.get("lambda_function_arn")
    if not (bucket and lambda_arn):
        return
    res = _lambda_res_from_arn(lambda_arn, cid)
    lambda_block: Dict[str, Any] = {
        "lambda_function_arn": _lambda_ref(resources, res, lambda_arn, "arn"),
        "events": list(action.get("events") or ["s3:ObjectCreated:*"]),
    }
    filt = action.get("filter") or {}
    if filt.get("prefix"):
        lambda_block["filter_prefix"] = filt["prefix"]
    if filt.get("suffix"):
        lambda_block["filter_suffix"] = filt["suffix"]
    resources.setdefault("aws_s3_bucket_notification", {}).setdefault(
        safe_ident(f"{cid}_{bucket}_notification"),
        {"bucket": bucket, "lambda_function": [lambda_block]},
    )


# ---------------------------------------------------------------------------
# Redshift Serverless — namespace + workgroup + external schema bridge
#
# The `hashicorp/aws` provider models Redshift Serverless as two paired
# resources: a namespace (data/identity layer, holds the IAM roles) and a
# workgroup (compute layer, holds base capacity / network). The workgroup
# references the namespace by name so OpenTofu orders namespace → workgroup
# automatically.
#
# There is NO first-party resource in `hashicorp/aws` for
# `CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`. The community pattern
# (and the only one that works with the `hashicorp/aws ~> 5.0` pin) is a
# `null_resource` + `provisioner.local-exec` calling the `redshift-data`
# API. The plugin emits this bridge and orders it after the workgroup +
# the upstream Glue catalog database via an explicit `depends_on` (see
# :func:`_wire_aws_deps`). Re-apply is idempotent at the SQL layer
# (`IF NOT EXISTS`) and at the OpenTofu layer (`triggers` hash). The
# `aws` CLI must be on the apply host, which is already required for
# AWS auth.
# ---------------------------------------------------------------------------


def _emit_redshift_serverless(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    """``redshift_serverless`` binding → namespace + workgroup.

    A FLUID exposure with both ``namespace`` and ``workgroup`` set in the
    binding location provisions a Redshift Serverless compute pair. Self-
    guarded: missing inputs leave the workgroup external (the contract
    then only emits the external schema bridge against a pre-existing
    workgroup).
    """
    namespace = loc.get("namespace")
    workgroup = loc.get("workgroup")
    if not (namespace and workgroup):
        return

    ns_key = safe_ident(f"{cid}_rs_ns_{namespace}")
    ns_body: Dict[str, Any] = {"namespace_name": namespace, "tags": tags}
    if loc.get("database"):
        ns_body["db_name"] = loc["database"]
    iam_role = loc.get("iam_role_arn")
    if iam_role:
        ns_body["iam_roles"] = [iam_role]
        ns_body["default_iam_role_arn"] = iam_role
    if loc.get("admin_username"):
        ns_body["admin_username"] = loc["admin_username"]
    if loc.get("kms_key_id"):
        ns_body["kms_key_id"] = loc["kms_key_id"]
    resources.setdefault("aws_redshiftserverless_namespace", {}).setdefault(ns_key, ns_body)

    wg_key = safe_ident(f"{cid}_rs_wg_{workgroup}")
    wg_body: Dict[str, Any] = {
        # `namespace_name` value reference creates the namespace → workgroup
        # ordering edge OpenTofu needs (no `depends_on` necessary).
        "namespace_name": tofu_ref(f"aws_redshiftserverless_namespace.{ns_key}.namespace_name"),
        "workgroup_name": workgroup,
        "tags": tags,
    }
    if loc.get("base_capacity") is not None:
        wg_body["base_capacity"] = int(loc["base_capacity"])
    if loc.get("publicly_accessible") is not None:
        wg_body["publicly_accessible"] = bool(loc["publicly_accessible"])
    if loc.get("subnet_ids"):
        wg_body["subnet_ids"] = list(loc["subnet_ids"])
    if loc.get("security_group_ids"):
        wg_body["security_group_ids"] = list(loc["security_group_ids"])
    resources.setdefault("aws_redshiftserverless_workgroup", {}).setdefault(wg_key, wg_body)

    # Private VPC access: when the workgroup is not publicly accessible
    # AND the contract supplies ``private_endpoint_subnets``, emit an
    # ``aws_redshiftserverless_endpoint_access`` resource. Without this
    # the workgroup's natural hostname
    # (``<wg>.<acct>.<region>.redshift-serverless.amazonaws.com``) has
    # no published DNS entry in the workgroup's VPC and clients running
    # inside that VPC (e.g. dbt-redshift on an EC2) cannot resolve it
    # — ``getent hosts`` fails with NXDOMAIN even after the workgroup
    # is AVAILABLE. The endpoint-access resource creates a dedicated
    # VPC ENI with a published DNS hostname; its ``.address`` is what
    # the dbt-redshift profile uses as ``host``.
    ep_subnets = loc.get("private_endpoint_subnets")
    if ep_subnets:
        ep_key = safe_ident(f"{cid}_rs_ep_{workgroup}")
        # endpoint_name has length / charset constraints similar to the
        # workgroup. Reuse the workgroup name + ``-ep`` so the address
        # is deterministic and human-readable.
        endpoint_name = f"{workgroup}-ep"[:30]
        ep_body: Dict[str, Any] = {
            "endpoint_name": endpoint_name,
            "workgroup_name": tofu_ref(f"aws_redshiftserverless_workgroup.{wg_key}.workgroup_name"),
            "subnet_ids": list(ep_subnets),
        }
        if loc.get("private_endpoint_security_group_ids"):
            ep_body["vpc_security_group_ids"] = list(loc["private_endpoint_security_group_ids"])
        elif loc.get("security_group_ids"):
            # Default: reuse the workgroup's SG (port 5439 already open
            # from the right source SG).
            ep_body["vpc_security_group_ids"] = list(loc["security_group_ids"])
        resources.setdefault("aws_redshiftserverless_endpoint_access", {}).setdefault(
            ep_key, ep_body
        )


def _emit_redshift_external_schema(
    resources: Dict[str, Any], loc: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    """``redshift_external_schema`` binding → ``null_resource`` running
    ``CREATE EXTERNAL SCHEMA ... FROM DATA CATALOG`` via the ``redshift-data`` API.

    The data-mesh interface: the upstream FLUID product publishes an Iceberg
    table to a Glue catalog database (the mesh-shared artefact). A downstream
    Redshift consumer registers an external schema in its workgroup pointing
    at that Glue database — both Athena (native) and Redshift (via this
    schema) then read the same physical Iceberg table. The ``hashicorp/aws``
    provider has no resource for this operation in v5 (filed upstream); the
    documented community bridge is a ``null_resource`` + ``local-exec`` that
    runs the SQL via ``aws redshift-data execute-statement``.

    Idempotency: ``IF NOT EXISTS`` at the SQL layer; ``triggers`` hash at the
    OpenTofu layer (a new IAM role / region re-runs the local-exec). Ordering:
    when the same module also emits the workgroup or the upstream Glue
    database, :func:`_wire_aws_deps` attaches the matching ``depends_on``.

    Snowflake-style "external container" path: leave ``workgroup`` /
    ``glue_database`` referencing pre-existing infrastructure and the bridge
    fires against them — no resources from this module need to be created
    first.
    """
    external_schema = loc.get("external_schema")
    workgroup = loc.get("workgroup")
    glue_database = loc.get("glue_database")
    iam_role_arn = loc.get("iam_role_arn")
    if not (external_schema and workgroup and glue_database and iam_role_arn):
        return
    database = loc.get("database") or "fluid"
    region = loc.get("region") or ""

    # --- Injection defenses (two independent layers) -------------------------
    # This binding values are attacker-influenced contract content
    # (binding.location.*) and `fluid apply`/`fluid generate iac` do NOT
    # JSON-schema-validate them first, so both layers are load-bearing:
    #
    # 1. SQL layer — the schema is an identifier (validate_ident, fail-closed on
    #    a malicious name); glue_database/iam_role/region are string literals
    #    (quote_string_literal doubles embedded quotes). This prevents a value
    #    like ``glue_database = "x' UNION ..."`` from breaking out of the
    #    CREATE EXTERNAL SCHEMA SQL run against Redshift under the IAM role.
    # 2. Shell layer — the command runs through ``local-exec`` (i.e. /bin/sh).
    #    Every untrusted value is passed via the subprocess ``environment``
    #    (data, never spliced into the command string) and referenced as a
    #    double-quoted ``"$VAR"``, so the shell cannot re-parse metacharacters
    #    (``;`` ``$(`` `` ` `` etc.). The command string is therefore STATIC.
    schema_ident = validate_ident(external_schema)
    sql = (
        f"CREATE EXTERNAL SCHEMA IF NOT EXISTS {schema_ident} "
        f"FROM DATA CATALOG "
        f"DATABASE {quote_string_literal(glue_database)} "
        f"IAM_ROLE {quote_string_literal(iam_role_arn)}"
        + (f" REGION {quote_string_literal(region)}" if region else "")
        + ";"
    )
    cmd = (
        "aws redshift-data execute-statement "
        '--workgroup-name "$FLUID_REDSHIFT_WORKGROUP" '
        '--database "$FLUID_REDSHIFT_DATABASE" '
        '--sql "$FLUID_REDSHIFT_SQL"'
    )
    res_key = safe_ident(f"{cid}_redshift_ext_{workgroup}_{external_schema}")
    resources.setdefault("null_resource", {}).setdefault(
        res_key,
        {
            # `triggers` carries every input that should re-fire the
            # local-exec when changed; the dep-wiring pass also reads these
            # to find matching workgroup / Glue database resources.
            "triggers": {
                "schema": external_schema,
                "workgroup": workgroup,
                "database": database,
                "glue_database": glue_database,
                "iam_role": iam_role_arn,
                "region": region,
            },
            "provisioner": [
                {
                    "local-exec": {
                        "command": cmd,
                        # Untrusted values reach the subprocess as env vars
                        # (data), keeping `command` a constant string.
                        "environment": {
                            "FLUID_REDSHIFT_WORKGROUP": workgroup,
                            "FLUID_REDSHIFT_DATABASE": database,
                            "FLUID_REDSHIFT_SQL": sql,
                        },
                    }
                }
            ],
        },
    )


# ---------------------------------------------------------------------------
# Cross-resource dependency wiring (post-emit pass)
# ---------------------------------------------------------------------------


def _wire_aws_deps(resources: Dict[str, Any], cid: str) -> None:
    """Attach ``depends_on`` edges that the resource fields don't already carry.

    Some emitters reference upstream resources by literal name (Redshift's
    ``CREATE EXTERNAL SCHEMA`` SQL names its workgroup and Glue database
    inside a shell command — OpenTofu sees no edge). This pass walks the
    emitted ``null_resource`` entries, reads the ``triggers`` keys that
    encode the upstream identity, and attaches ``depends_on`` for matches
    that exist in this same module. External (pre-existing) upstreams
    produce no edge — the bridge then applies against infrastructure that
    already exists, exactly as before.
    """
    null_resources = resources.get("null_resource") or {}
    for res_name, body in null_resources.items():
        if "redshift_ext" not in res_name:
            continue
        triggers = body.get("triggers") or {}
        deps: List[str] = []
        workgroup = triggers.get("workgroup")
        if workgroup:
            wg_key = safe_ident(f"{cid}_rs_wg_{workgroup}")
            if wg_key in resources.get("aws_redshiftserverless_workgroup", {}):
                deps.append(f"aws_redshiftserverless_workgroup.{wg_key}")
        glue_database = triggers.get("glue_database")
        if glue_database:
            glue_key = safe_ident(f"{cid}_{glue_database}")
            if glue_key in resources.get("aws_glue_catalog_database", {}):
                deps.append(f"aws_glue_catalog_database.{glue_key}")
        if deps:
            body["depends_on"] = deps


# ---------------------------------------------------------------------------
# Lake Formation — emit
# ---------------------------------------------------------------------------
#
# Two emit surfaces:
#
#   * ``_emit_lf_account_settings`` — fires ONCE per contract before any
#     per-exposure emit. Honours top-level ``governance.lakeFormation``:
#     ``admins`` → ``aws_lakeformation_data_lake_settings``,
#     ``tagDefinitions`` → one ``aws_lakeformation_lf_tag`` per key.
#     Must run before per-resource ``resource_lf_tags`` associations so
#     the tag keys exist for the association to reference.
#
#   * ``_emit_lakeformation`` — fires per AWS exposure. Honours
#     ``binding.governance.lakeFormation``:
#     ``registerLocation`` → ``aws_lakeformation_resource`` on the
#         binding's ``s3://<bucket>/<path>``,
#     ``grants[]`` → one ``aws_lakeformation_permissions`` per principal
#         (with ``columns`` choosing ``table_with_columns`` vs ``table``),
#     ``tags{}`` → one ``aws_lakeformation_resource_lf_tags`` per table,
#     ``rowFilter`` → one ``aws_lakeformation_data_cells_filter``.
#
# Design notes:
#   - LF resources are emitted alongside the Glue catalog table they
#     reference; OpenTofu's value-reference edges (``${aws_glue_catalog_table
#     .{...}.name}``) provide the ordering, no manual ``depends_on``
#     needed. Where a reference would be circular (e.g. tag definitions
#     vs tag associations from different exposures), explicit
#     ``depends_on`` is set.
#   - Empty governance blocks emit nothing — every existing contract
#     stays at zero LF surface area.
#   - LF is Glue-catalog-backed, so the per-exposure emit only fires for
#     formats in ``_GLUE_CATALOG_FORMATS``. Redshift / Kinesis / Lambda
#     bindings ignore any governance.lakeFormation block by design (LF
#     doesn't manage those resources).


def _contract_uses_lakeformation(contract: Mapping[str, Any]) -> bool:
    """True if the contract has any LF block — top-level or per-exposure."""
    if (contract.get("governance") or {}).get("lakeFormation"):
        return True
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if (binding.get("governance") or {}).get("lakeFormation"):
            return True
    return False


def _references_caller_account(contract: Mapping[str, Any]) -> bool:
    """True if any AWS binding's warehouse falls back to the apply-time
    ``{aws_caller_identity}-fluid-data`` bucket, so the backing
    ``data.aws_caller_identity.fluid_lf_caller`` source must be declared (see
    :meth:`AwsIacPlugin.emit_data`).

    Reuses :func:`normalize_location` — the single function that interpolates
    ``_CALLER_ACCOUNT_TOKEN`` into the emitted HCL — so the declaration can never
    drift from the emission. The previous predicate hard-coded a narrower
    ``Glue-catalog format + table + no bucket`` check and missed standard tables
    and unresolved ``{{ env }}`` buckets, so those emitted the token without the
    backing data source → ``tofu plan`` failed with
    ``Reference to undeclared resource``."""
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if binding.get("platform") != "aws":
            continue
        loc = binding.get("location") or {}
        bucket, _ = _warehouse.normalize_location(
            loc, account_ref=_CALLER_ACCOUNT_TOKEN, default_path=False
        )
        if _CALLER_ACCOUNT_TOKEN in bucket:
            return True
    return False


def _emit_lf_account_settings(
    resources: Dict[str, Any], contract: Mapping[str, Any], cid: str, tags: Dict[str, str]
) -> None:
    gov = (contract.get("governance") or {}).get("lakeFormation") or {}
    admins = gov.get("admins") or []
    tag_defs = gov.get("tagDefinitions") or {}

    if admins:
        # ``aws_lakeformation_data_lake_settings`` is a singleton per
        # account+region. Use a stable resource name so re-applying with
        # the same contract is idempotent.
        resources.setdefault("aws_lakeformation_data_lake_settings", {})[
            safe_ident(f"{cid}_lf_settings")
        ] = {
            "admins": list(admins),
        }

    for tag_key, tag_values in tag_defs.items():
        if not tag_values:
            continue
        resources.setdefault("aws_lakeformation_lf_tag", {})[
            safe_ident(f"{cid}_lf_tag_{tag_key}")
        ] = {
            "key": str(tag_key),
            "values": list(tag_values),
        }


def _emit_lakeformation(
    resources: Dict[str, Any],
    binding: Mapping[str, Any],
    loc: Mapping[str, Any],
    fmt: str,
    cid: str,
    tags: Dict[str, str],
    *,
    placement: _Placement = _LEGACY_PLACEMENT,
) -> None:
    """Emit per-exposure LF resources. No-op when the binding has no
    ``governance.lakeFormation`` block.

    Under a REFERENCED bucket the grants narrow to the binding's
    ``location.path`` prefix rather than the bucket root (RFC §Security —
    "LF registers the ``path`` prefix, not the bucket"), and every Glue /
    S3 reference switches to its ``data.`` address.
    """
    gov = (binding.get("governance") or {}).get("lakeFormation") or {}
    if not gov:
        return
    # LF only meaningfully manages access to Glue-catalog-backed formats
    # (file formats on S3). Redshift/Kinesis bindings have their own
    # access-control models and are skipped here.
    if str(fmt or "").lower() not in _GLUE_CATALOG_FORMATS:
        return

    database = loc.get("database")
    table = loc.get("table")
    if not database:
        return

    # Resolve the bucket ONCE, up front, through the same derivation the
    # ``data.aws_s3_bucket`` lookup uses (:func:`_referenced_bucket_name` for a
    # pool; ``normalize_location`` otherwise) — PR2 resolved it only inside the
    # ``registerLocation`` branch, so a grants-only binding keyed the bucket
    # policy off the raw contract value while the lookup used the resolved name.
    #
    # ``default_path=False`` keeps LF's register-the-prefix semantics (no
    # ``{db}/{table}/`` default), which is what the raw ``location.path`` read
    # already gave. The gate stays on the RAW value: an exposure that declares
    # no bucket at all emits nothing here, exactly as before — it must never
    # acquire the ``{account}-fluid-data`` fallback through this path.
    raw_bucket = loc.get("bucket")
    bucket: Optional[str] = None
    path = (loc.get("path") or "").lstrip("/")
    if raw_bucket:
        if placement.bucket_referenced:
            bucket = _referenced_bucket_name(loc)
        else:
            bucket, _ = _warehouse.normalize_location(
                loc, account_ref=_CALLER_ACCOUNT_TOKEN, default_path=False
            )

    # 1. Register the S3 location with Lake Formation.
    if gov.get("registerLocation") and bucket:
        # Registering a *pool* bucket at its ROOT would hand this product's
        # LF service role access to every other tenant's data.
        _require_pool_prefix(placement, path, what="governance.lakeFormation.registerLocation")
        loc_key = safe_ident(f"{cid}_lf_loc_{bucket}_{path or 'root'}")
        resources.setdefault("aws_lakeformation_resource", {})[loc_key] = {
            "arn": f"arn:aws:s3:::{bucket}/{path}" if path else f"arn:aws:s3:::{bucket}",
            # ``use_service_linked_role: true`` is the default safe path
            # — LF uses the AWSServiceRoleForLakeFormationDataAccess SLR
            # to access objects under the registered location.
            "use_service_linked_role": True,
        }

    db_key = safe_ident(f"{cid}_{database}")
    table_key = safe_ident(f"{cid}_{database}_{table}") if table else None

    # Collected during the grant loop, drained after into a single
    # aws_s3_bucket_policy resource per bucket. A consumer asking for
    # genuine cross-account read needs BOTH the LF permission (catalog
    # read) AND a bucket policy (object read on the underlying S3
    # storage); for in-account principals the bucket policy is benign
    # (additive on top of IAM). The aws-lakeformation-best-practices
    # cross-account FAQ and the canonical Terraform pattern by Komminar
    # both spell this out: LF alone is not sufficient. So we always
    # emit the policy when LF grants reference IAM principals — no
    # opt-in flag needed.
    bucket_principals: List[str] = []

    # 2. Principal grants. Each grant becomes one aws_lakeformation_permissions
    #    resource targeting either .table or .table_with_columns (when
    #    columns / excludedColumns is set).
    for idx, grant in enumerate(gov.get("grants") or []):
        principal = grant.get("principal")
        perms = list(grant.get("permissions") or [])
        if not principal or not perms:
            continue
        body: Dict[str, Any] = {
            "principal": principal,
            "permissions": perms,
        }
        gp = grant.get("permissionsWithGrantOption")
        if gp:
            body["permissions_with_grant_option"] = list(gp)
        cols = grant.get("columns")
        excluded = grant.get("excludedColumns")
        if (cols or excluded) and table_key:
            twc: Dict[str, Any] = {
                "database_name": tofu_ref(f"aws_glue_catalog_table.{table_key}.database_name"),
                "name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
            }
            if cols:
                twc["column_names"] = list(cols)
            if excluded:
                twc["excluded_column_names"] = list(excluded)
            body["table_with_columns"] = [twc]
        elif table_key:
            body["table"] = [
                {
                    "database_name": tofu_ref(f"aws_glue_catalog_table.{table_key}.database_name"),
                    "name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
                }
            ]
        else:
            # Database-level grant when no table is bound.
            body["database"] = [
                {"name": _glue_db_ref(db_key, database, referenced=placement.database_referenced)}
            ]
        # Stable resource key — principal + perms hashed so multiple
        # grants on the same exposure don't collide.
        body_key = safe_ident(f"{cid}_lf_grant_{table or database}_{idx}")
        resources.setdefault("aws_lakeformation_permissions", {})[body_key] = body

        # Every IAM-principal LF grant on a Glue-catalog-backed S3
        # binding gets a matching bucket-policy statement. For
        # cross-account principals this is REQUIRED (LF alone doesn't
        # authorise object reads); for in-account principals it's
        # additive (they already have IAM read). One resource per
        # bucket — drained after the loop.
        if isinstance(principal, str) and principal.startswith("arn:"):
            bucket_principals.append(principal)

    # 2b. S3 bucket policy — one resource per bucket, statements for
    #     every LF-grant principal. We deliberately overwrite any
    #     existing fluid-emitted bucket policy on the same bucket
    #     because there is exactly one aws_s3_bucket_policy slot per
    #     bucket — multiple LF grants on the same exposure share one
    #     policy doc.
    if bucket_principals and bucket:
        # A pool bucket's grants MUST be prefix-scoped — without a path the
        # statements below degrade to the whole bucket and this authoritative
        # policy also replaces the platform team's own. Fail closed.
        _require_pool_prefix(placement, path, what="governance.lakeFormation.grants[]")
        bucket_key = safe_ident(f"{cid}_{bucket}")
        bucket_arn = f"arn:aws:s3:::{bucket}"
        # ListBucket targets the bucket ARN itself; GetObject targets
        # the per-object ARN under the configured path prefix (or
        # everything if no path).
        object_arn = f"arn:aws:s3:::{bucket}/{path}*" if path else f"arn:aws:s3:::{bucket}/*"
        statements: List[Dict[str, Any]] = []
        for sid_idx, p in enumerate(bucket_principals):
            list_statement: Dict[str, Any] = {
                "Sid": f"FluidLfBucketList{sid_idx}",
                "Effect": "Allow",
                "Principal": {"AWS": p},
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": bucket_arn,
            }
            if placement.bucket_referenced:
                # ListBucket is inherently bucket-scoped, so on a shared pool
                # it is narrowed with the standard ``s3:prefix`` condition —
                # otherwise this product's consumers could enumerate every
                # other tenant's keys in the pool. ``path`` is guaranteed
                # non-empty here by ``_require_pool_prefix`` above.
                list_statement["Condition"] = {"StringLike": {"s3:prefix": [f"{path}*"]}}
            statements.append(list_statement)
            statements.append(
                {
                    "Sid": f"FluidLfBucketGet{sid_idx}",
                    "Effect": "Allow",
                    "Principal": {"AWS": p},
                    "Action": ["s3:GetObject"],
                    "Resource": object_arn,
                }
            )
        policy_doc = json.dumps(
            {"Version": "2012-10-17", "Statement": statements},
            sort_keys=True,
            separators=(",", ":"),
        )
        policy_key = safe_ident(f"{cid}_lf_bucket_policy_{bucket}")
        # NOTE (v2): ``aws_s3_bucket_policy`` is authoritative for the whole
        # bucket, so two products sharing one pool would each rewrite the
        # other's policy on every apply. The prefix-scoped statements above
        # keep each grant narrow, but a tenancy registry that detects the
        # collision is RFC v2 work — flagged, not silently accepted.
        resources.setdefault("aws_s3_bucket_policy", {})[policy_key] = {
            "bucket": _s3_bucket_ref(bucket_key, referenced=placement.bucket_referenced),
            "policy": policy_doc,
        }

    # 3. LF-tag associations on the table (LF-TBAC).
    tag_assoc = gov.get("tags") or {}
    if tag_assoc and table_key:
        lf_tags = [{"key": str(k), "value": str(v)} for k, v in tag_assoc.items() if v]
        if lf_tags:
            assoc_key = safe_ident(f"{cid}_lf_tags_{table}")
            resources.setdefault("aws_lakeformation_resource_lf_tags", {})[assoc_key] = {
                "table": [
                    {
                        "database_name": tofu_ref(
                            f"aws_glue_catalog_table.{table_key}.database_name"
                        ),
                        "name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
                    }
                ],
                "lf_tag": lf_tags,
                # The tag KEYS must exist before this association can be
                # applied. The matching ``aws_lakeformation_lf_tag``
                # resources come from the contract-level
                # ``governance.lakeFormation.tagDefinitions`` block.
                "depends_on": [
                    f"aws_lakeformation_lf_tag.{safe_ident(f'{cid}_lf_tag_{k}')}" for k in tag_assoc
                ],
            }

    # 4. Row-level (and optional column-level) filter.
    row_filter = gov.get("rowFilter")
    if row_filter and table_key:
        filter_name = row_filter.get("name")
        row_expr = row_filter.get("rowExpression")
        if filter_name and row_expr:
            col_names = row_filter.get("columnNames")
            excluded_cols = row_filter.get("excludedColumnNames")
            all_cols = bool(row_filter.get("allColumns"))
            # Exactly one of column_names / column_wildcard must be set.
            # When the contract gives explicit columnNames, use those;
            # excludedColumnNames maps to column_wildcard with excludes;
            # otherwise default to wildcard (every column visible — the
            # row-only-filter case).
            col_block: Dict[str, Any]
            if col_names:
                col_block = {"column_names": list(col_names)}
            elif excluded_cols:
                col_block = {"column_wildcard": [{"excluded_column_names": list(excluded_cols)}]}
            else:
                # ``allColumns`` is the explicit form; absence defaults to it
                # because LF requires one of these and "wildcard" is the
                # natural row-only-filter behaviour.
                col_block = {"column_wildcard": [{}]}
            body = {
                "table_data": [
                    {
                        "table_catalog_id": tofu_ref(
                            "data.aws_caller_identity.fluid_lf_caller.account_id"
                        ),
                        "database_name": tofu_ref(
                            f"aws_glue_catalog_table.{table_key}.database_name"
                        ),
                        "table_name": tofu_ref(f"aws_glue_catalog_table.{table_key}.name"),
                        "name": filter_name,
                        "row_filter": [{"filter_expression": row_expr}],
                        **col_block,
                    }
                ]
            }
            filter_key = safe_ident(f"{cid}_lf_filter_{table}_{filter_name}")
            resources.setdefault("aws_lakeformation_data_cells_filter", {})[filter_key] = body
