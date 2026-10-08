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
import os
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple

from ._sql_safety import validate_ident
from .aws.util.warehouse import (
    _ENV_TEMPLATE_RE,
    get_iceberg_warehouse,
    normalize_location,
    resolve_env_templates,
)

# Apache Iceberg runtime class names (pinned to the connector surface validated
# in the OSS spike — RFC §14). Bumping the Iceberg runtime may change these.
GLUE_CATALOG_IMPL = "org.apache.iceberg.aws.glue.GlueCatalog"
DYNAMODB_CATALOG_IMPL = "org.apache.iceberg.aws.dynamodb.DynamoDbCatalog"
S3_FILE_IO = "org.apache.iceberg.aws.s3.S3FileIO"
GCS_FILE_IO = "org.apache.iceberg.gcp.gcs.GCSFileIO"
ADLS_FILE_IO = "org.apache.iceberg.azure.adlsv2.ADLSFileIO"

# ---------------------------------------------------------------------------
# Catalog kinds — THE one classification every emitter reads
# ---------------------------------------------------------------------------
#
# ``binding.location.catalog`` (and ``sink.catalog``) is a free string, and
# before this table four emitters each classified it by hand: the sink
# deriver mapped unknown kinds to ``rest``, dbt ``catalogs.yml`` and the
# Snowflake IaC mapped them to Snowflake-managed, the sink validator checked
# only the literal ``rest``, and the AWS IaC ignored the catalog and emitted a
# Glue table for every Iceberg expose. ``catalog: lakekeeper`` therefore
# streamed over REST while dbt wrote a Snowflake-managed table. One row per
# kind, and every consumer derives its answer from the row.
#
# The shape borrows dbt-adapters' ``_V2_TO_V1_TYPE`` hook (a user-facing kind
# mapped to each emitter's wire value, identity by default; dbt-snowflake
# impl.py) and Airbyte's typed Polaris preset (REST on the wire, but the
# warehouse is a catalog NAME). Wire values come only from Apache Iceberg's
# ``CatalogUtil`` set (hadoop/hive/rest/glue/nessie/jdbc/bigquery): no engine
# or SDK has a ``lakekeeper`` type, and CatalogUtil throws on one.

#: AWS Glue Data Catalog through the native ``GlueCatalog``.
FAMILY_GLUE = "glue"
#: The Iceberg REST protocol: ``uri`` + ``warehouse`` (a catalog name).
FAMILY_REST = "rest"
#: A runtime-native, non-REST client (Nessie's own API, Hive metastore, JDBC...).
FAMILY_NATIVE = "native"
#: Snowflake's built-in (Horizon) catalog over a Snowflake EXTERNAL VOLUME.
FAMILY_SNOWFLAKE_MANAGED = "snowflake-managed"
#: A value this table does not know. ``fluid validate`` rejects it.
FAMILY_UNKNOWN = "unknown"


@dataclass(frozen=True)
class CatalogKind:
    """What one ``location.catalog`` value means to every emitter."""

    name: str
    family: str
    #: ``iceberg.catalog.type`` (Kafka Connect) / ``type`` (Debezium Server).
    #: Mutually exclusive with ``catalog_impl``: Iceberg's ``CatalogUtil``
    #: throws when both are set.
    runtime_type: Optional[str]
    #: ``catalog-impl`` for a catalog the runtime has no ``type`` for.
    catalog_impl: Optional[str]
    #: dbt ``catalogs.yml`` on Snowflake: ``iceberg_rest`` (a catalog external
    #: to Snowflake, reached through a catalog integration), ``built_in``
    #: (Snowflake-managed), or ``None`` when Snowflake has no integration for
    #: it. The Snowflake IaC emitter partitions on the same column.
    snowflake_catalog_type: Optional[str]
    #: ``binding.location`` keys a streaming sink needs for this catalog.
    sink_requires: Tuple[str, ...] = ()

    @property
    def speaks_rest(self) -> bool:
        """Does the writer reach this catalog over the Iceberg REST protocol?"""
        return self.runtime_type == "rest"


_REST_REQUIRES = ("uri", "warehouse")

_CATALOG_KINDS: Mapping[str, CatalogKind] = MappingProxyType(
    {
        k.name: k
        for k in (
            CatalogKind("glue", FAMILY_GLUE, None, GLUE_CATALOG_IMPL, "iceberg_rest"),
            CatalogKind("rest", FAMILY_REST, "rest", None, "iceberg_rest", _REST_REQUIRES),
            CatalogKind(
                "lakekeeper",
                FAMILY_REST,
                "rest",
                None,
                "iceberg_rest",
                _REST_REQUIRES,
            ),
            CatalogKind(
                "polaris",
                FAMILY_REST,
                "rest",
                None,
                "iceberg_rest",
                _REST_REQUIRES,
            ),
            CatalogKind(
                "unity",
                FAMILY_REST,
                "rest",
                None,
                "iceberg_rest",
                _REST_REQUIRES,
            ),
            # Native NessieCatalog for the sinks (uri ends /api/v1|v2, the
            # warehouse is an object-store location), while Snowflake reaches
            # Nessie through its Iceberg REST endpoint. The stock Apache Kafka
            # Connect runtime does not bundle iceberg-nessie.
            CatalogKind("nessie", FAMILY_NATIVE, "nessie", None, "iceberg_rest", _REST_REQUIRES),
            # BigLake metastore. ``type=bigquery`` exists in Iceberg >= 1.10.
            # BigQueryMetastoreCatalog.initialize refuses to start without
            # ``gcp.bigquery.project-id``, which the resolver maps from
            # ``location.project`` (apache-iceberg-1.10.0
            # BigQueryMetastoreCatalog.java:84-87).
            CatalogKind("bigquery", FAMILY_NATIVE, "bigquery", None, "iceberg_rest", ("project",)),
            CatalogKind("hive", FAMILY_NATIVE, "hive", None, None),
            # JdbcCatalog.initialize refuses a missing uri and an empty warehouse
            # (apache-iceberg-1.10.0 JdbcCatalog.java:114-119). The warehouse
            # may be derived: see :data:`BUCKET_WAREHOUSE_KINDS`.
            CatalogKind("jdbc", FAMILY_NATIVE, "jdbc", None, None, ("uri", "warehouse")),
            CatalogKind("hadoop", FAMILY_NATIVE, "hadoop", None, None, ("warehouse",)),
            # CatalogUtil has no ``dynamodb`` type: it is reached by impl only.
            # DynamoDbCatalog.initialize refuses an empty warehouse
            # (apache-iceberg-1.10.0 DynamoDbCatalog.java:132-134).
            CatalogKind(
                "dynamodb", FAMILY_NATIVE, None, DYNAMODB_CATALOG_IMPL, None, ("warehouse",)
            ),
            # Horizon over an EXTERNAL VOLUME; a streaming writer reaches it
            # through Snowflake's Iceberg REST endpoint.
            CatalogKind(
                "snowflake-managed",
                FAMILY_SNOWFLAKE_MANAGED,
                "rest",
                None,
                "built_in",
                _REST_REQUIRES,
            ),
        )
    }
)

#: Kinds whose ``warehouse`` is an object-store location the resolver may
#: derive from an explicit ``location.bucket`` (and ``path``) when
#: ``location.warehouse`` is absent. Never from the account-derived fallback
#: bucket the Glue row uses: no IaC creates that bucket for a non-Glue table.
BUCKET_WAREHOUSE_KINDS = frozenset({"dynamodb", "jdbc"})

#: The object-store scheme a derived warehouse gets, by canonical platform.
_BUCKET_WAREHOUSE_SCHEMES: Mapping[str, str] = MappingProxyType({"aws": "s3", "gcp": "gs"})

#: BigQueryMetastoreCatalog's own property names (apache-iceberg-1.10.0
#: bigquery/src/main/java/org/apache/iceberg/gcp/bigquery/
#: BigQueryMetastoreCatalog.java:62-63). Without a location the catalog uses
#: ``us`` (:68, :90).
BIGQUERY_PROJECT_ID = "gcp.bigquery.project-id"
BIGQUERY_LOCATION = "gcp.bigquery.location"

#: Accepted spellings for a canonical kind (after case and ``-``/``_`` folding).
_CATALOG_ALIASES: Mapping[str, str] = MappingProxyType(
    {"iceberg-rest": "rest", "snowflake": "snowflake-managed"}
)

#: A value outside the table. Emitters keep their historic fallbacks for it
#: (REST for a sink, Snowflake-managed for dbt) and ``fluid validate`` refuses it.
_UNKNOWN_KIND = CatalogKind("", FAMILY_UNKNOWN, "rest", None, "built_in")


class UnknownIcebergCatalogError(ValueError):
    """An Iceberg ``location.catalog`` value no row of the table knows.

    Typed so a caller can tell the contract's refusal (the remedy is in the
    message) from a planner that failed to run: the AWS provider used to log it
    as ``plan_failed`` and then re-raise it, so the user read it twice, once as
    a raw JSON log line.
    """


def _fold(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def canonical_catalog_kind(value: Any) -> str:
    """The canonical kind for a ``catalog`` value: ``""`` when absent, the
    table's name for a known kind or alias, else the folded value as given."""
    folded = _fold(value)
    return _CATALOG_ALIASES.get(folded, folded)


def catalog_kind_info(value: Any) -> CatalogKind:
    """The table row for ``value`` (any spelling), or the UNKNOWN row."""
    return _CATALOG_KINDS.get(canonical_catalog_kind(value), _UNKNOWN_KIND)


def known_catalog_kinds() -> Tuple[str, ...]:
    """Every accepted spelling, canonical names first — for error messages."""
    return tuple(sorted(_CATALOG_KINDS)) + tuple(sorted(_CATALOG_ALIASES))


def default_catalog_kind(binding: Mapping[str, Any]) -> str:
    """The kind an Iceberg binding gets when it names none: Glue on AWS and on
    Confluent, Snowflake-managed on Snowflake, a REST catalog anywhere else.

    Confluent is Glue because the Tableflow IaC publishes a table with no
    ``catalog`` to AWS Glue (``iac/providers/confluent.py``), so the policy
    compiler and dbt read the catalog the table is actually in.

    GCP is the one platform where an absent catalog means two things: the
    streaming sink has always written a REST catalog there, while dbt-bigquery
    and the GCP IaC create a BigLake table. Changing the default would change
    every existing GCP sink's ``iceberg.catalog.type`` (and ``bigquery`` needs
    Iceberg >= 1.10, newer than the published Kafka Connect sink), so the
    BigQuery paths read only an EXPLICIT ``location.catalog`` instead.
    """
    from ..iac.provider_match import canonical_cloud

    cloud = canonical_cloud(binding.get("platform"))
    if cloud in ("aws", "confluent"):
        return "glue"
    if cloud == "snowflake":
        return "snowflake-managed"
    return "rest"


def binding_catalog_kind(binding: Mapping[str, Any]) -> str:
    """The canonical kind of an expose binding: ``location.catalog``, else the
    platform default. What dbt and the IaC emitters read."""
    loc = binding.get("location") or {}
    return canonical_catalog_kind(loc.get("catalog")) or default_catalog_kind(binding)


def iceberg_catalog_kind(binding: Mapping[str, Any], sink: Any = None) -> str:
    """The canonical kind a streaming sink writes through: ``sink.catalog``,
    then ``location.catalog``, then the platform default.

    ``sink`` may be a ``SinkSpec`` or the raw ``sink`` mapping. ``fluid
    validate`` refuses a ``sink.catalog`` that disagrees with the expose,
    because dbt and the IaC emitters read only the expose.
    """
    if isinstance(sink, Mapping):
        explicit = sink.get("catalog")
    else:
        explicit = getattr(sink, "catalog", None) if sink is not None else None
    return canonical_catalog_kind(explicit) or binding_catalog_kind(binding)


#: ``binding.format`` spellings that mean Iceberg (mirrors the sink validator).
_ICEBERG_FORMATS = frozenset({"iceberg", "iceberg-table"})


def is_iceberg_format(fmt: Any) -> bool:
    return _fold(fmt) in _ICEBERG_FORMATS


def is_glue_cataloged(binding: Mapping[str, Any]) -> bool:
    """Is this binding's table registered in AWS Glue?

    Any non-Iceberg format Glue catalogs (parquet, csv, ...) is; an Iceberg
    table is only when its catalog is Glue. A Lakekeeper or other REST-catalog
    table lives in that catalog, so a static Glue table for it would be a
    second, metadata-less claim on the same name.
    """
    if not is_iceberg_format(binding.get("format")):
        return True
    return binding_catalog_kind(binding) == "glue"


#: ``location.catalog`` values that mean "a catalog EXTERNAL to Snowflake"
#: (every spelling, aliases included). Derived from the table: the dbt
#: ``catalogs.yml`` emitter maps these to ``catalog_type: iceberg_rest`` and
#: the Snowflake IaC emitter skips the EXTERNAL VOLUME for them, so both read
#: :func:`catalog_kind_info` rather than hand-maintaining a list.
EXTERNAL_ICEBERG_CATALOGS = frozenset(
    {n for n, k in _CATALOG_KINDS.items() if k.snowflake_catalog_type == "iceberg_rest"}
    | {
        a
        for a, n in _CATALOG_ALIASES.items()
        if _CATALOG_KINDS[n].snowflake_catalog_type == "iceberg_rest"
    }
    | {"iceberg_rest"}
)


def iceberg_sink_exposes(contract: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    """The exposes a self-managed streaming sink can write to, in order.

    An Iceberg-format expose that is not on ``platform: confluent`` (a
    Confluent Tableflow expose is a MANAGED output its own plugin owns). The
    runners and ``fluid validate`` both select through this, so the expose a
    sink writes to is the one the validator checks.
    """
    return tuple(
        e
        for e in (contract.get("exposes") or [])
        if isinstance(e, Mapping)
        and isinstance(e.get("binding") or {}, Mapping)
        and is_iceberg_format((e.get("binding") or {}).get("format"))
        and _fold((e.get("binding") or {}).get("platform")) != "confluent"
    )


def find_iceberg_expose_binding(contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The binding of the first of :func:`iceberg_sink_exposes`, or ``None``.

    A sink config that names its own tables (hand-written, or an override that
    sets ``iceberg.tables`` / ``table-namespace``) is checked against this
    expose's catalog. A sink config forge-cli derives reads the exposes
    :func:`resolve_iceberg_sink_exposes` joins from the build's outputs; the
    streaming runners select between the two through
    ``iceberg_sink_validation.iceberg_sink_target``.
    """
    exposes = iceberg_sink_exposes(contract)
    return dict(exposes[0].get("binding") or {}) if exposes else None


def _expose_id(expose: Mapping[str, Any]) -> Any:
    return expose.get("exposeId") or expose.get("id")


def _expose_database(expose: Mapping[str, Any]) -> Any:
    """The database leg of an expose's table, read as :func:`resolve_iceberg_catalog` reads it."""
    binding = expose.get("binding") or {}
    return (binding.get("location") or {}).get("database") or binding.get("database")


def _described(exposes: Tuple[Mapping[str, Any], ...]) -> list[str]:
    """``["orders (sales.orders)", ...]``: each expose's id and table, for the messages."""
    return [
        f"{_expose_id(e)} ({resolve_iceberg_catalog(e.get('binding') or {}).fq_table})"
        for e in exposes
    ]


def resolve_iceberg_sink_exposes(
    contract: Mapping[str, Any],
    build: Mapping[str, Any],
    *,
    namespace: bool = False,
) -> Tuple[Tuple[Mapping[str, Any], ...], Optional[str]]:
    """The exposes a sink config forge-cli DERIVES for ``build`` writes, or why it cannot.

    The candidates are :func:`iceberg_sink_exposes`; the build's ``outputs``
    pick among them, and with no outputs the candidates are the contract's.

    * ``namespace=False`` (Kafka Connect): the derived config carries one
      ``iceberg.tables`` entry, and the Apache Iceberg sink writes each record
      to every table that lists (``SinkWriter.routeRecordStatically``), so it
      writes one expose. Exactly one candidate in outputs, or no outputs and
      exactly one in the contract; anything else is an error.
    * ``namespace=True`` (embedded Debezium Server): the derived config carries
      one ``table-namespace`` and one catalog, and the server writes each
      captured table under that namespace (``DefaultIcebergTableMapper``).
      The candidates selected must share one database and resolve to one
      catalog; outputs that name none of them are an error.

    Returns ``(exposes, None)`` with at least one expose, ``((), error)``, or
    ``((), None)`` when the contract has no Iceberg sink expose at all (the
    validator reports that).
    """
    candidates = iceberg_sink_exposes(contract)
    if not candidates:
        return (), None
    head = f"iceberg sink (build {build.get('id', '?')!r})"
    raw_outputs = build.get("outputs") or []
    outputs = [raw_outputs] if isinstance(raw_outputs, str) else list(raw_outputs)
    selected = tuple(e for e in candidates if _expose_id(e) in outputs) if outputs else candidates
    if outputs:
        picked = f"its outputs {outputs} name {len(selected) or 'none'} of the"
    else:
        picked = f"it declares no outputs, and the contract has {len(candidates)}"
    picked += f" Iceberg sink exposes {_described(candidates)}"
    if outputs:
        writes = f"the exposes its outputs {outputs} name, {_described(selected)},"
    else:
        writes = f"the contract's Iceberg sink exposes, {_described(selected)},"

    if not namespace:
        if len(selected) == 1:
            return selected, None
        return (), (
            f"{head}: {picked}; a derived Kafka Connect sink writes one expose (one "
            "iceberg.tables entry). List exactly one of them in the build's outputs, split "
            "the build into one build per expose, or hand-write the sink config "
            "(properties.kafka-connect.sink_connector_config)"
        )

    remedy = "hand-write the sink config (properties.debezium.server.sink.config)"
    if not selected:
        return (), (
            f"{head}: {picked}; a derived Debezium Server sink writes the exposes the "
            f"build's outputs name. List them in the build's outputs, or {remedy}"
        )
    databases = sorted({str(_expose_database(e)) for e in selected})
    if len(databases) > 1:
        return (), (
            f"{head}: a derived Debezium Server sink writes every captured table under one "
            f"table-namespace, but {writes} sit in the databases {databases}. Give them one "
            f"binding.location.database, split the build into one build per database, or {remedy}"
        )
    differs = _catalog_differences(selected)
    if differs:
        return (), (
            f"{head}: a derived Debezium Server sink writes through one catalog, but {writes} "
            f"resolve to different catalogs (their {', '.join(differs)} differ). Give them one "
            f"catalog, split the build into one build per catalog, or {remedy}"
        )
    return selected, None


#: The binding setting each :class:`ResolvedIcebergCatalog` catalog field is
#: resolved from, for the messages.
_CATALOG_FIELD_SOURCES: Mapping[str, str] = MappingProxyType(
    {
        "kind": "catalog",
        "catalog_type": "catalog",
        "catalog_impl": "catalog",
        "uri": "uri",
        "warehouse": "warehouse",
        "io_impl": "warehouse",
        "region": "region",
        "extra_catalog_props": "catalog properties",
    }
)


def _catalog_differences(exposes: Tuple[Mapping[str, Any], ...]) -> list[str]:
    """The catalog settings on which ``exposes`` resolve differently, or ``[]``.

    Compares every :class:`ResolvedIcebergCatalog` field except the table
    identity and the per-table write defaults. A Glue warehouse is per table
    too (``get_iceberg_warehouse`` derives ``s3://{bucket}/{database}/{table}/``),
    while a REST catalog's warehouse is the catalog's name.
    """
    from dataclasses import fields as dataclass_fields

    resolved = [resolve_iceberg_catalog(e.get("binding") or {}) for e in exposes]
    if len(resolved) < 2:
        return []
    per_table = {"fq_table", "id_columns", "partition_by"}
    if all(r.catalog_impl == GLUE_CATALOG_IMPL for r in resolved):
        per_table.add("warehouse")
    differs: list[str] = []
    for f in dataclass_fields(ResolvedIcebergCatalog):
        if f.name in per_table:
            continue
        if any(getattr(r, f.name) != getattr(resolved[0], f.name) for r in resolved[1:]):
            source = _CATALOG_FIELD_SOURCES.get(f.name, f.name)
            if source not in differs:
                differs.append(source)
    return differs


#: Object-store URI schemes, and the Iceberg ``FileIO`` each one needs. THE one
#: scheme list: the resolver picks a FileIO from it and the sink validator
#: refuses a name-only warehouse that carries one.
_OBJECT_STORE_FILE_IO = (
    (("s3://", "s3a://", "s3n://"), S3_FILE_IO),
    (("gs://", "gcs://"), GCS_FILE_IO),
    (("abfs://", "abfss://"), ADLS_FILE_IO),
)


def is_object_store_uri(value: Any) -> bool:
    """Does ``value`` carry an object-store scheme (s3://, gs://, abfss://...)?"""
    return _io_impl_for_warehouse(str(value or "")) is not None


def _io_impl_for_warehouse(warehouse: str) -> Optional[str]:
    """Pick the Iceberg ``FileIO`` from the warehouse URI scheme.

    An object-store warehouse REQUIRES an ``io-impl`` (the connector's #1
    works-in-REST-demo-fails-on-cloud trap). REST / Nessie / Hive catalogs can
    front any cloud, so the FileIO follows the WAREHOUSE scheme, not the catalog
    kind: ``s3://`` -> S3FileIO, ``gs://`` -> GCSFileIO, ``abfss://`` -> ADLSFileIO.
    A warehouse NAME (Lakekeeper, Polaris) gets none: the catalog vends the
    table's FileIO configuration with its metadata.
    """
    w = (warehouse or "").lower()
    for schemes, file_io in _OBJECT_STORE_FILE_IO:
        if w.startswith(schemes):
            return file_io
    return None


@dataclass(frozen=True)
class ResolvedIcebergCatalog:
    """Canonical, provider-neutral Iceberg-table identity for a binding."""

    catalog_type: str  # wire type: "glue" | "rest" | "nessie" | "hive" | ...
    warehouse: str  # s3://|gs://|abfss:// path (glue/object-store) or catalog name (rest)
    fq_table: str  # "<database>.<table>"
    catalog_impl: Optional[str] = None  # GlueCatalog for glue; XOR with catalog_type on the wire
    io_impl: Optional[str] = None  # S3 / GCS / ADLS FileIO per warehouse scheme
    region: Optional[str] = None
    uri: Optional[str] = None  # REST catalog endpoint
    id_columns: Tuple[str, ...] = ()  # -> iceberg.tables.default-id-columns
    partition_by: Tuple[str, ...] = ()  # -> iceberg.tables.default-partition-by
    extra_catalog_props: Mapping[str, str] = field(default_factory=dict)
    kind: str = ""  # canonical catalog kind ("lakekeeper", "glue", ...)


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
    ``uri`` + ``warehouse`` (catalog name) and don't use it. DynamoDB and JDBC
    take ``location.warehouse``, else derive one from an explicit
    ``location.bucket`` (:data:`BUCKET_WAREHOUSE_KINDS`); BigQuery takes the
    ``gs://`` URI :func:`iceberg_storage_uri` gives for the bucket with its
    ``{{ env.* }}`` templates rendered, plus ``location.project``
    and ``location.region`` as its own catalog properties.
    """
    loc = binding.get("location") or {}
    database = loc.get("database") or binding.get("database")
    table = loc.get("table") or binding.get("table")
    fq_table = f"{database}.{table}"
    kind = iceberg_catalog_kind(binding, sink)
    info = catalog_kind_info(kind)

    partition_by = tuple(getattr(sink, "partition_by", None) or loc.get("partitionBy") or ())
    id_columns = _id_columns(contract)

    if info.family == FAMILY_GLUE:
        return ResolvedIcebergCatalog(
            catalog_type="glue",
            warehouse=get_iceberg_warehouse(loc, account_ref=account_ref),
            fq_table=fq_table,
            catalog_impl=GLUE_CATALOG_IMPL,
            io_impl=S3_FILE_IO,
            region=loc.get("region"),
            id_columns=id_columns,
            partition_by=partition_by,
            kind=kind,
        )

    # Non-Glue catalog over any cloud storage. The wire type comes from the
    # table (Lakekeeper / Polaris / Unity / Snowflake are all ``rest``; an
    # unknown kind keeps the historic REST fallback, and ``fluid validate``
    # refuses it). The FileIO follows the WAREHOUSE scheme so GCS (gs://) and
    # ADLS (abfss://) work, not just S3 (RFC §6.3 — PR7's REST + GCP profiles).
    warehouse = str(loc.get("warehouse") or "").strip()
    region = loc.get("region")
    extra: Dict[str, str] = {}
    if kind == "bigquery":
        # The gs:// storage dbt-bigquery's ``external_volume`` and the GCP IaC
        # use, or none, with the bucket's ``{{ env.* }}`` templates rendered:
        # a bucket that does not resolve here derives none. ``client.region``
        # is an AWS client property, so the region goes to the catalog's own
        # location key instead.
        rendered = {**loc, "bucket": _rendered_bucket(loc)}
        warehouse = iceberg_storage_uri({"location": rendered}, scheme="gs")
        project = str(loc.get("project") or "").strip()
        if project:
            extra[BIGQUERY_PROJECT_ID] = project
        location = str(region or "").strip()
        if location:
            extra[BIGQUERY_LOCATION] = location
        region = None
    elif not warehouse and kind in BUCKET_WAREHOUSE_KINDS:
        warehouse = _bucket_warehouse(binding)
    return ResolvedIcebergCatalog(
        catalog_type=info.runtime_type or kind,
        warehouse=warehouse,
        fq_table=fq_table,
        catalog_impl=info.catalog_impl,
        uri=loc.get("uri"),
        io_impl=_io_impl_for_warehouse(warehouse),
        region=region,
        id_columns=id_columns,
        partition_by=partition_by,
        extra_catalog_props=extra,
        kind=kind,
    )


def _bucket_warehouse(binding: Mapping[str, Any]) -> str:
    """The object-store warehouse an explicit ``location.bucket`` names, or ``""``.

    ``<scheme>://<bucket>/<path>``, with the bucket and path normalised the way
    the Glue row's :func:`get_iceberg_warehouse` normalises them (``path``
    defaults to ``<database>/<table>/``). The scheme follows the platform:
    ``s3`` on AWS, ``gs`` on GCP; any other platform derives nothing. A
    missing bucket, or one :func:`_rendered_bucket` cannot render, derives
    nothing either, rather than the Glue row's account-derived fallback bucket.
    """
    from ..iac.provider_match import canonical_cloud

    loc = binding.get("location") or {}
    scheme = _BUCKET_WAREHOUSE_SCHEMES.get(canonical_cloud(binding.get("platform")))
    bucket = _rendered_bucket(loc)
    if not scheme or not bucket:
        return ""
    _bucket, path = normalize_location(loc, account_ref="")
    return f"{scheme}://{bucket}/{path}"


def _rendered_bucket(loc: Mapping[str, Any]) -> str:
    """``location.bucket`` with its ``{{ env.* }}`` templates rendered, or ``""``.

    ``""`` when the bucket is absent, names a variable that is unset or empty
    in this process's environment, or holds a template that is not
    ``{{ env.* }}``. An empty variable counts as unset: rendering it would
    turn ``acme-{{ env.LAKE_ENV }}-lake`` into ``acme--lake``, a bucket the
    contract does not name.
    """
    raw = str(loc.get("bucket") or "").strip()
    if not raw or _unset_env_vars(raw) or "{{" in _ENV_TEMPLATE_RE.sub("", raw):
        return ""
    return str(resolve_env_templates(raw)).strip()


def _unset_env_vars(raw: str) -> Tuple[str, ...]:
    """The ``{{ env.* }}`` variables ``raw`` names that are unset or empty here."""
    found = (name.strip() for name in _ENV_TEMPLATE_RE.findall(raw))
    return tuple(dict.fromkeys(name for name in found if not os.environ.get(name)))


def unset_bucket_env_vars(binding: Mapping[str, Any], kind: str) -> Tuple[str, ...]:
    """The unset or empty variables the ``kind`` sink's warehouse waits on.

    Non-empty when the sink resolver derives ``kind``'s warehouse from
    ``location.bucket`` and that bucket names ``{{ env.* }}`` variables that
    are unset or empty in this process's environment: DynamoDB and JDBC on
    aws or gcp with no ``location.warehouse``, and BigQuery with no
    scheme-qualified ``location.warehouse``. The bucket is explicit, so the
    warehouse derives wherever those variables are set, but not here. ``()``
    when every variable the bucket names has a value, for an absent bucket,
    another kind or platform, a warehouse the binding sets, or a bucket
    holding a template that is not ``{{ env.* }}`` (nothing resolves that one).
    """
    from ..iac.provider_match import canonical_cloud

    loc = binding.get("location") or {}
    raw = str(loc.get("bucket") or "")
    warehouse = str(loc.get("warehouse") or "").strip()
    if kind in BUCKET_WAREHOUSE_KINDS:
        reads_bucket = not warehouse and (
            canonical_cloud(binding.get("platform")) in _BUCKET_WAREHOUSE_SCHEMES
        )
    else:
        # :func:`iceberg_storage_uri` reads the bucket unless the warehouse
        # carries a scheme.
        reads_bucket = kind == "bigquery" and not warehouse.startswith(_WAREHOUSE_SCHEMES)
    if not raw or not reads_bucket or "{{" in _ENV_TEMPLATE_RE.sub("", raw):
        return ()
    return _unset_env_vars(raw)


# ---------------------------------------------------------------------------
# Object-store warehouse URI
# ---------------------------------------------------------------------------

#: ``location.warehouse`` schemes that are already a full object-store URI.
_WAREHOUSE_SCHEMES = ("s3://", "gs://", "abfs://", "abfss://")

#: ``location.warehouse`` scheme to the Snowflake EXTERNAL VOLUME
#: ``storage_provider``, in precedence order. THE single source of truth for
#: this mapping: the Snowflake IaC emitter creates the volume from it and the
#: validate-time gate decides what a binding needs from it. Hand-rolling a
#: second shape of it in either place desyncs them the moment a scheme is
#: added. Azure is deliberately absent: the volume needs an ``azure_tenant_id``
#: the contract schema has no slot for, so guessing would emit a volume
#: Snowflake rejects at CREATE time.
STORAGE_PROVIDERS = (("s3://", "S3"), ("gs://", "GCS"))


def iceberg_storage_provider(location: Optional[Mapping[str, Any]]) -> str:
    """``"S3"`` / ``"GCS"`` / ``""`` for a binding's storage, by precedence.

    A scheme in ``location.warehouse`` wins outright; ``location.bucket`` is
    consulted ONLY when the warehouse carries no scheme, and implies S3. That
    ordering is load-bearing: a ``gs://`` warehouse alongside a ``bucket`` is
    a GCS volume, so treating the bucket as evidence of S3 would demand an
    ``iam_role_arn`` the emitter never uses.
    """
    loc = location or {}
    warehouse = str(loc.get("warehouse") or "")
    for scheme, provider in STORAGE_PROVIDERS:
        if warehouse.startswith(scheme):
            return provider
    return "S3" if loc.get("bucket") else ""


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
