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

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from .aws.util.warehouse import get_iceberg_warehouse

# Apache Iceberg runtime class names (pinned to the connector surface validated
# in the OSS spike — RFC §14). Bumping the Iceberg runtime may change these.
GLUE_CATALOG_IMPL = "org.apache.iceberg.aws.glue.GlueCatalog"
S3_FILE_IO = "org.apache.iceberg.aws.s3.S3FileIO"


@dataclass(frozen=True)
class ResolvedIcebergCatalog:
    """Canonical, provider-neutral Iceberg-table identity for a binding."""

    catalog_type: str  # "glue" | "rest"
    warehouse: str  # s3://bucket/path (glue) or catalog name/uri-warehouse (rest)
    fq_table: str  # "<database>.<table>"
    catalog_impl: Optional[str] = None  # GlueCatalog for glue
    io_impl: Optional[str] = None  # S3FileIO for object-store warehouses
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

    # REST catalog (Snowflake Open Catalog / Polaris / Nessie / the spike's
    # iceberg-rest-fixture). Warehouse is the catalog *name*, not an s3 path.
    return ResolvedIcebergCatalog(
        catalog_type="rest",
        warehouse=loc.get("warehouse") or loc.get("catalog_warehouse") or "",
        fq_table=fq_table,
        uri=loc.get("uri") or loc.get("catalogUri"),
        io_impl=S3_FILE_IO if loc.get("warehouse", "").startswith(("s3://", "s3a://")) else None,
        region=loc.get("region"),
        id_columns=id_columns,
        partition_by=partition_by,
    )
