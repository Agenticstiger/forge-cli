# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Derive the embedded Debezium-Server Iceberg sink config.

Pure, deterministic, credential-free — the Debezium-Server twin of
``kafka_connect/iceberg_sink.py``. Returns a flat ``str -> str`` map of BARE
keys (no prefix); the runner prepends ``debezium.sink.iceberg.`` exactly once.

The embedded sink (``memiiso/debezium-server-iceberg``, package
``io.debezium.server.iceberg``) speaks a DIFFERENT config vocabulary than the
Apache Kafka-Connect sink: there is no control topic, no ``iceberg.tables``
explicit list, and no ``default-id-columns``. The destination table is COMPOSED
by the consumer from the namespace + the CDC source coordinates, so only the
namespace (the database leg of the fully-qualified table) is pinnable here.
Every key below is pinned verbatim to the upstream surface
(``IcebergConfig.java`` / ``docs/iceberg.md``); the consumer owns
commit / upsert / schema-evolution — forge only emits config (RFC §6.2).
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from ...providers._iceberg_catalog import ResolvedIcebergCatalog


def emit_debezium_iceberg_sink_config(
    resolved: ResolvedIcebergCatalog,
    *,
    overrides: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build the flat BARE-key Debezium-Server Iceberg sink config.

    Keys are returned WITHOUT the ``debezium.sink.iceberg.`` prefix — the
    runner's existing ``debezium.sink.{type}.{k}={v}`` loop adds it exactly
    once. Pure: same input -> identical output (the plan-binding guarantee).
    ``overrides`` (a hand-written ``server.sink.config``) is forwarded LAST so
    an operator key always wins.
    """
    cfg: Dict[str, str] = {}

    if resolved.warehouse:
        cfg["warehouse"] = resolved.warehouse

    # table-namespace = the database leg of "<db>.<table>". The table LEAF is
    # NOT pinnable: the consumer composes the physical name from
    # namespace.prefix + <server>_<db>_<table>, so KC's iceberg.tables=<db.table>
    # has no analog here (a documented semantic divergence).
    namespace = (resolved.fq_table or "").split(".", 1)[0]
    if namespace and namespace != "None":
        cfg["table-namespace"] = namespace

    # Catalog selector: catalog-impl XOR type. Emitting BOTH crashes the
    # consumer — Iceberg CatalogUtil.buildIcebergCatalog throws
    # IllegalArgumentException when catalog-impl and type are both set. Prefer
    # the explicit impl (e.g. glue -> GlueCatalog); else the built-in type
    # (rest | hive | hadoop | jdbc).
    if resolved.catalog_impl:
        cfg["catalog-impl"] = resolved.catalog_impl
    elif resolved.catalog_type:
        cfg["type"] = resolved.catalog_type

    if resolved.io_impl:
        cfg["io-impl"] = resolved.io_impl
    if resolved.uri:
        cfg["uri"] = resolved.uri
    if resolved.region:
        # passthrough-only: reaches Iceberg's AwsClientProperties as the native
        # ``client.region`` key (there is no dedicated server key).
        cfg["client.region"] = resolved.region

    # id_columns has no column-list key on this sink. Enable upsert + identifier
    # fields; the identity columns are taken from the CDC message KEY (source
    # PK), NOT from this list — a documented translation-loss point.
    if resolved.id_columns:
        cfg["upsert"] = "true"
        cfg["create-identifier-fields"] = "true"

    if resolved.partition_by:
        # GLOBAL (applies to all derived tables), not a per-table default.
        cfg["partition-by"] = ",".join(resolved.partition_by)

    # Native Iceberg props (s3.* / gcs.* / adls.* / jdbc.* ...) pass straight
    # through with the same prefix the runner adds.
    for k, v in (resolved.extra_catalog_props or {}).items():
        cfg[str(k)] = str(v)

    # Operator escape hatch — forwarded LAST so a hand-written key always wins.
    for k, v in (overrides or {}).items():
        cfg[str(k)] = str(v)

    return cfg
