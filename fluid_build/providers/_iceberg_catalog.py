# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Resolve an Iceberg-table catalog identity from an expose binding.

``ResolvedIcebergCatalog`` is the single source of truth for *which* Iceberg
table a binding points at — catalog kind, warehouse, fully-qualified name,
FileIO, id/partition columns. It is consumed by the streaming-sink deriver
(``build_runners/kafka_connect/iceberg_sink.py``) and, later, by the plan-time
zero-drift cross-check (RFC-streaming-extension §6.8). The warehouse leg reuses
PR1's single canonical writer so the connector and the static Glue table can
never disagree (RFC §6.1 / §7).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

from ._sql_safety import validate_ident
from .aws.util.warehouse import get_iceberg_warehouse

# Apache Iceberg runtime class names (pinned to the connector surface validated
# in the OSS spike — RFC §14). Bumping the Iceberg runtime may change these.
GLUE_CATALOG_IMPL = "org.apache.iceberg.aws.glue.GlueCatalog"
S3_FILE_IO = "org.apache.iceberg.aws.s3.S3FileIO"
GCS_FILE_IO = "org.apache.iceberg.gcp.gcs.GCSFileIO"
ADLS_FILE_IO = "org.apache.iceberg.azure.adlsv2.ADLSFileIO"

# Iceberg catalog types the runtime recognizes for ``iceberg.catalog.type``.
# Anything else (polaris / snowflake-managed / unity — all REST-fronted) maps to
# ``rest`` so the connector talks to it over the REST protocol.
_KNOWN_CATALOG_TYPES = frozenset(
    {"rest", "hive", "hadoop", "jdbc", "nessie", "bigquery", "dynamodb"}
)

#: ``location.catalog`` values that mean "a catalog EXTERNAL to Snowflake".
#: THE shared predicate for the two halves of the dbt Iceberg loop: the dbt
#: ``catalogs.yml`` emitter maps these to ``catalog_type: iceberg_rest`` and
#: everything else to ``built_in``; the Snowflake IaC emitter must partition
#: identically or one side references infrastructure the other never creates
#: (a ``catalog: snowflake`` binding, say, must be Snowflake-managed to BOTH).
EXTERNAL_ICEBERG_CATALOGS = frozenset(
    {"glue", "polaris", "unity", "rest", "iceberg_rest", "nessie"}
)


def find_iceberg_expose_binding(contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The expose ``binding`` carrying the Iceberg-table identity for a sink.

    Shared by the Kafka-Connect and Debezium-Server runners so both resolve the
    SAME table identity (the RFC zero-drift spine). A simple ``format=iceberg``
    lookup; the validated build->expose join (build.outputs / exposeId) lands
    with the plan-time validator (RFC §6.8 #5).
    """
    for exposure in contract.get("exposes") or []:
        binding = exposure.get("binding") or {}
        if str(binding.get("format") or "").lower() == "iceberg":
            return binding
    return None


def _io_impl_for_warehouse(warehouse: str) -> Optional[str]:
    """Pick the Iceberg ``FileIO`` from the warehouse URI scheme.

    An object-store warehouse REQUIRES an ``io-impl`` (the connector's #1
    works-in-REST-demo-fails-on-cloud trap). REST / Nessie / Hive catalogs can
    front any cloud, so the FileIO follows the WAREHOUSE scheme, not the catalog
    kind: ``s3://`` -> S3FileIO, ``gs://`` -> GCSFileIO, ``abfss://`` -> ADLSFileIO.
    """
    w = (warehouse or "").lower()
    if w.startswith(("s3://", "s3a://", "s3n://")):
        return S3_FILE_IO
    if w.startswith(("gs://", "gcs://")):
        return GCS_FILE_IO
    if w.startswith(("abfs://", "abfss://")):
        return ADLS_FILE_IO
    return None


@dataclass(frozen=True)
class ResolvedIcebergCatalog:
    """Canonical, provider-neutral Iceberg-table identity for a binding."""

    catalog_type: str  # "glue" | "rest" | "nessie" | "hive" | ...
    warehouse: str  # s3://|gs://|abfss:// path (glue/object-store) or catalog name (rest)
    fq_table: str  # "<database>.<table>"
    catalog_impl: Optional[str] = None  # GlueCatalog for glue
    io_impl: Optional[str] = None  # S3 / GCS / ADLS FileIO per warehouse scheme
    region: Optional[str] = None
    uri: Optional[str] = None  # REST catalog endpoint
    id_columns: Tuple[str, ...] = ()  # -> iceberg.tables.default-id-columns
    partition_by: Tuple[str, ...] = ()  # -> iceberg.tables.default-partition-by
    extra_catalog_props: Mapping[str, str] = field(default_factory=dict)


def _catalog_kind(binding: Mapping[str, Any], sink: Any, loc: Mapping[str, Any]) -> str:
    """glue / rest, in precedence: explicit sink.catalog > location.catalog >
    platform default (aws -> glue, else rest)."""
    explicit = (getattr(sink, "catalog", None) if sink is not None else None) or loc.get("catalog")
    if explicit:
        return str(explicit).lower()
    return "glue" if str(binding.get("platform") or "").lower() == "aws" else "rest"


def _id_columns(contract: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    if not contract:
        return ()
    pk = (contract.get("metadata") or {}).get("primaryKey")
    if isinstance(pk, str):
        return (pk,)
    if isinstance(pk, (list, tuple)):
        return tuple(str(c) for c in pk)
    return ()


def resolve_iceberg_catalog(
    binding: Mapping[str, Any],
    *,
    contract: Optional[Mapping[str, Any]] = None,
    sink: Any = None,
    account_ref: str = "",
) -> ResolvedIcebergCatalog:
    """Resolve the Iceberg-table identity for ``binding`` (an ``exposes[].binding``).

    ``account_ref`` feeds the warehouse bucket fallback on the Glue path (a
    concrete account id at connector-config time). REST catalogs take an explicit
    ``uri`` + ``warehouse`` (catalog name) and don't use it.
    """
    loc = binding.get("location") or {}
    database = loc.get("database") or binding.get("database")
    table = loc.get("table") or binding.get("table")
    fq_table = f"{database}.{table}"
    kind = _catalog_kind(binding, sink, loc)

    partition_by = tuple(getattr(sink, "partition_by", None) or loc.get("partitionBy") or ())
    id_columns = _id_columns(contract)

    if kind == "glue":
        return ResolvedIcebergCatalog(
            catalog_type="glue",
            warehouse=get_iceberg_warehouse(loc, account_ref=account_ref),
            fq_table=fq_table,
            catalog_impl=GLUE_CATALOG_IMPL,
            io_impl=S3_FILE_IO,
            region=loc.get("region"),
            id_columns=id_columns,
            partition_by=partition_by,
        )

    # Non-Glue catalog: REST / Nessie / Hive / Polaris / Snowflake Open Catalog /
    # Unity (all REST-fronted) over any cloud storage. ``catalog_type`` is the
    # kind when the runtime recognizes it (nessie / hive / rest / ...), else REST;
    # the FileIO follows the WAREHOUSE scheme so GCS (gs://) and ADLS (abfss://)
    # work, not just S3 (RFC §6.3 — PR7's REST + GCP profiles).
    warehouse = loc.get("warehouse") or ""
    return ResolvedIcebergCatalog(
        catalog_type=kind if kind in _KNOWN_CATALOG_TYPES else "rest",
        warehouse=warehouse,
        fq_table=fq_table,
        uri=loc.get("uri"),
        io_impl=_io_impl_for_warehouse(warehouse),
        region=loc.get("region"),
        id_columns=id_columns,
        partition_by=partition_by,
    )


# ---------------------------------------------------------------------------
# Object-store warehouse URI
# ---------------------------------------------------------------------------

#: ``location.warehouse`` schemes that are already a full object-store URI.
_WAREHOUSE_SCHEMES = ("s3://", "gs://", "abfs://", "abfss://")


def iceberg_storage_uri(binding: Optional[Mapping[str, Any]], *, scheme: str = "gs") -> str:
    """The object-store URI backing an Iceberg binding, or ``""``.

    THE second cross-emitter contract, alongside
    :func:`iceberg_external_volume_name`. BigQuery's ``catalogs.yml`` takes a
    bare ``gs://`` URI as its ``external_volume`` (unlike Snowflake, which
    takes an object NAME), and the GCP IaC emitter has to create the bucket
    at exactly that URI.

    ``location.warehouse`` wins when it carries a scheme, but ONLY the
    requested one. A binding whose warehouse is ``s3://...`` yields ``""`` on
    the ``gs`` path rather than a URI BigQuery cannot resolve, so both
    emitters skip together instead of one emitting storage the other cannot
    back. A scheme with no bucket component (``gs://``, ``gs:///x``) is
    likewise treated as underivable.

    :func:`iceberg_bucket_name` is derived FROM this function, so the two can
    never disagree about which bucket is in play.
    """
    loc = (binding or {}).get("location") or {}
    warehouse = str(loc.get("warehouse") or "").strip()
    prefix = f"{scheme}://"
    if warehouse.startswith(_WAREHOUSE_SCHEMES):
        # A foreign scheme is not usable here. Returning it would point dbt
        # at storage this provider's IaC never creates.
        if not warehouse.startswith(prefix):
            return ""
        return warehouse if warehouse[len(prefix) :].split("/", 1)[0] else ""
    bucket = str(loc.get("bucket") or "").strip()
    if not bucket:
        return ""
    path = str(loc.get("path") or "").strip("/")
    return f"{prefix}{bucket}/{path}" if path else f"{prefix}{bucket}"


def iceberg_bucket_name(binding: Optional[Mapping[str, Any]], *, scheme: str = "gs") -> str:
    """The bucket component of :func:`iceberg_storage_uri`.

    Derived from that function rather than re-reading the binding, so the
    bucket the IaC creates is always the bucket the URI points into. Reading
    ``location.bucket`` directly here would invert the precedence: a binding
    carrying BOTH ``bucket`` and a different ``warehouse`` would have dbt
    write into the warehouse while the IaC created (and governed) the other
    one, which is exactly the drift this pair exists to prevent.
    """
    uri = iceberg_storage_uri(binding, scheme=scheme)
    prefix = f"{scheme}://"
    return uri[len(prefix) :].split("/", 1)[0] if uri.startswith(prefix) else ""


# ---------------------------------------------------------------------------
# Snowflake external-volume naming
# ---------------------------------------------------------------------------

# ``FLUID_<product>_VOL``. The prefix guarantees the first character is a
# letter (``validate_ident`` requires it) even when a contract id starts with a
# digit, and it namespaces the object so a fluid-created volume is obvious in
# ``SHOW EXTERNAL VOLUMES``.
_VOLUME_PREFIX = "FLUID_"
_VOLUME_SUFFIX = "_VOL"
# Snowflake caps an identifier at 255 characters. Stay well inside that so the
# prefix, suffix and truncation digest always fit.
_VOLUME_MAX_CORE = 200
_VOLUME_DIGEST_LEN = 8
_NON_IDENT_CHARS = re.compile(r"[^A-Za-z0-9_]")
_REPEATED_UNDERSCORES = re.compile(r"_+")


def iceberg_external_volume_name(
    contract: Optional[Mapping[str, Any]],
    binding: Optional[Mapping[str, Any]] = None,
) -> str:
    """Deterministic Snowflake EXTERNAL VOLUME name for an Iceberg binding.

    dbt's ``built_in`` catalog type (Snowflake Horizon) requires an
    ``external_volume: <snowflake object name>`` in ``catalogs.yml``, but the
    FLUID contract schema cannot carry one: ``bindingLocation`` in
    ``fluid-schema-0.7.6.json`` is ``additionalProperties: false`` and has no
    ``externalVolume`` key, so a first-class field would need a schema version
    bump. The name is therefore DERIVED here instead.

    **This function is the contract between two emitters.** The dbt
    ``catalogs.yml`` emitter (``engines/dbt/catalogs_yml.py``) writes this name,
    and the Snowflake IaC emitter (``iac/providers/snowflake.py``) will create an
    EXTERNAL VOLUME with exactly this name in a follow-up change. Both call this
    one function, which is why it must stay **pure and deterministic**: same
    contract plus binding in, same string out, no clock, no environment, no
    randomness. Changing the derivation renames a live Snowflake object, so treat
    it as a breaking change.

    An operator whose Snowflake admin already created a volume can override the
    derived name with ``binding.icebergConfig.properties.external_volume`` (or
    the camelCase ``externalVolume``). That map is
    ``additionalProperties: {type: string}`` in the schema, so the override is
    expressible today with no schema bump. Overrides are validated too.

    The result always satisfies :func:`._sql_safety.validate_ident`, so it can be
    interpolated into ``CREATE EXTERNAL VOLUME`` DDL. Raises ``ValueError`` when
    an explicit override is not a legal identifier.
    """
    override = _external_volume_override(binding)
    if override:
        return validate_ident(override)

    core = _ident_core(str((contract or {}).get("id") or "")) or "PRODUCT"
    if len(core) > _VOLUME_MAX_CORE:
        digest = hashlib.sha256(core.encode("utf-8")).hexdigest()[:_VOLUME_DIGEST_LEN].upper()
        keep = _VOLUME_MAX_CORE - _VOLUME_DIGEST_LEN - 1
        core = f"{core[:keep].rstrip('_')}_{digest}"
    return validate_ident(f"{_VOLUME_PREFIX}{core}{_VOLUME_SUFFIX}")


def iceberg_external_volume_is_override(binding: Optional[Mapping[str, Any]]) -> bool:
    """True when the binding names a pre-existing volume explicitly.

    The override semantics are "I already have a volume": the dbt side should
    reference it, and the IaC side must NOT emit a CREATE for it, or apply
    fails loudly against the operator's own object.
    """
    return bool(_external_volume_override(binding))


def _external_volume_override(binding: Optional[Mapping[str, Any]]) -> str:
    """Explicit volume name from ``binding.icebergConfig.properties``, if any."""
    iceberg_config = (binding or {}).get("icebergConfig") or {}
    properties = iceberg_config.get("properties") or {}
    if not isinstance(properties, Mapping):
        return ""
    for key in ("external_volume", "externalVolume"):
        value = properties.get(key)
        if value:
            return str(value).strip()
    return ""


def _ident_core(raw: str) -> str:
    """Fold arbitrary text into the upper-case identifier body of a volume name.

    Contract ids follow the schema's ``identifier`` pattern, which allows dots
    and hyphens (``gold.hr.employee_360_v1``); Snowflake unquoted identifiers do
    not. Every disallowed run collapses to a single underscore and the result is
    upper-cased, matching Snowflake's own unquoted-identifier folding.
    """
    folded = _NON_IDENT_CHARS.sub("_", raw)
    return _REPEATED_UNDERSCORES.sub("_", folded).strip("_").upper()
