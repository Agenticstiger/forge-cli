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

"""Derive the Apache Iceberg Kafka-Connect sink connector config.

Pure, deterministic, credential-free — the streaming twin of
``schema_registry.avro_converter_config`` / ``_late_arrival.extract_late_arrival_policy``:
it returns a flat ``str -> str`` map the runner merges UNDER any hand-written
``sink_connector_config`` (operator keys always win). Turns the previously
hand-authored ``iceberg.catalog.*`` / ``iceberg.tables.*`` dict into a derived
artifact (RFC-streaming-extension §6.2).

The emitted key surface was validated end-to-end against a real Apache Iceberg
Connect sink (RFC §14): JSON-schemaless converters, a per-product control topic,
and the ``iceberg.catalog.*`` block. The connector itself owns exactly-once
commit coordination + compaction — forge only emits config.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Mapping, Optional

from ...providers._iceberg_catalog import ResolvedIcebergCatalog

# org.apache.iceberg.connect.* is the CURRENT Apache class (1.x); io.tabular.* is
# the retired pre-donation namespace (RFC §15.1) — never emit the legacy one.
ICEBERG_SINK_CLASS = "org.apache.iceberg.connect.IcebergSinkConnector"

_KAFKA_SAFE = re.compile(r"[^a-zA-Z0-9._-]")
_JSON_CONVERTER = "org.apache.kafka.connect.json.JsonConverter"


def sanitize_topic_segment(value: str, *, max_len: int = 200) -> str:
    """product_id -> a Kafka-topic-safe segment.

    slugify (Kafka topics allow ``[a-zA-Z0-9._-]``) -> truncate -> append a short
    stable hash of the ORIGINAL when slugification or truncation changed it, so
    two product ids that differ only in illegal characters can't collide on the
    same control topic (the locked decision in RFC §6.6).
    """
    # Legal char set + 249 cap per Apache Kafka's own Topic.java
    # (clients/.../common/internals/Topic.java: [a-zA-Z0-9._-], len 1..249).
    # The stable-hash suffix adopts Debezium DefaultTopicNamingStrategy's
    # "sanitization must stay collision-free" principle — a plain illegal->'-'
    # replace (Debezium's retired approach) collapses distinct ids together.
    slug = _KAFKA_SAFE.sub("-", value).strip("-._") or "x"
    if slug == value and len(slug) <= max_len:
        return slug
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:max_len]}-{digest}"


def control_topic(product_id: str) -> str:
    """Unique control topic per product — NEVER the shared ``control-iceberg``
    default (the Redpanda/Aiven multi-connector collision pitfall, RFC §6.6)."""
    return f"_iceberg-control-{sanitize_topic_segment(product_id)}"


def emit_iceberg_sink_config(
    resolved: ResolvedIcebergCatalog,
    *,
    product_id: str,
    topics: List[str],
    kc_props: Optional[Mapping[str, Any]] = None,
    schema_registry_url: Optional[str] = None,
    delivery_guarantee: Optional[str] = None,
) -> Dict[str, str]:
    """Build the flat Iceberg sink connector config for ``topics``.

    Pure: same inputs -> identical output (the load-bearing guarantee for the
    contract-derived config riding plan-binding later). ``kc_props`` may carry an
    ``iceberg_catalog_overrides`` map and a ``streamingSink`` tuning block; both
    are forwarded last so an operator can always override a derived key.
    """
    kc_props = kc_props or {}
    streaming = kc_props.get("streamingSink") or kc_props.get("streaming_sink") or {}

    cfg: Dict[str, str] = {
        "connector.class": ICEBERG_SINK_CLASS,
        "topics": ",".join(topics),
        "iceberg.tables": resolved.fq_table,
        "iceberg.control.topic": control_topic(product_id),
    }

    # ── catalog block (prefix-passthrough: derive a few, forward the rest) ──
    cfg["iceberg.catalog.type"] = resolved.catalog_type
    if resolved.catalog_impl:
        cfg["iceberg.catalog.catalog-impl"] = resolved.catalog_impl
    if resolved.warehouse:
        cfg["iceberg.catalog.warehouse"] = resolved.warehouse
    if resolved.io_impl:
        cfg["iceberg.catalog.io-impl"] = resolved.io_impl
    if resolved.region:
        cfg["iceberg.catalog.client.region"] = resolved.region
    if resolved.uri:
        cfg["iceberg.catalog.uri"] = resolved.uri

    # ── routing / write semantics ──
    if resolved.id_columns:
        cfg["iceberg.tables.default-id-columns"] = ",".join(resolved.id_columns)
    if resolved.partition_by:
        cfg["iceberg.tables.default-partition-by"] = ",".join(resolved.partition_by)
    if streaming.get("autoCreate") is not None:
        cfg["iceberg.tables.auto-create-enabled"] = _b(streaming["autoCreate"])
    if streaming.get("evolveSchema") is not None:
        cfg["iceberg.tables.evolve-schema-enabled"] = _b(streaming["evolveSchema"])

    # ── exactly-once / commit interval ──
    if str(delivery_guarantee or "").lower() in ("exactly_once", "exactly-once"):
        seg = sanitize_topic_segment(product_id)
        cfg["iceberg.coordinator.transactional.prefix"] = f"iceberg-coord-{seg}"
    commit_ms = streaming.get("commitIntervalMs")
    if commit_ms is not None:
        cfg["iceberg.control.commit.interval-ms"] = str(commit_ms)

    # ── converters: records must deserialize to a struct/map. Default to
    #    JSON-schemaless (validated in the §14 spike); use Avro when a Schema
    #    Registry is wired (mirrors the source connector). ──
    if schema_registry_url:
        from .schema_registry import avro_converter_config

        cfg.update(avro_converter_config(schema_registry_url))
    else:
        cfg["key.converter"] = _JSON_CONVERTER
        cfg["key.converter.schemas.enable"] = "false"
        cfg["value.converter"] = _JSON_CONVERTER
        cfg["value.converter.schemas.enable"] = "false"

    # ── operator escape hatch: forwarded LAST so it always wins ──
    overrides = kc_props.get("iceberg_catalog_overrides") or {}
    for k, v in overrides.items():
        cfg[str(k)] = str(v)

    return cfg


def _b(value: Any) -> str:
    return "true" if value in (True, "true", "True", 1) else "false"
