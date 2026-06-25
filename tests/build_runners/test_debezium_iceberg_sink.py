# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""PR8 — embedded Debezium-Server Iceberg sink config derivation.

The embedded sink (memiiso/debezium-server-iceberg) speaks a DIFFERENT key
vocabulary than the Kafka-Connect sink, so this is a TRANSLATION, not a re-key:
bare ``warehouse`` / ``table-namespace`` / ``catalog-impl`` XOR ``type`` /
``io-impl`` / ``client.region`` / ``upsert`` — and crucially NONE of KC's
``iceberg.tables`` / ``iceberg.control.topic`` / ``default-id-columns`` may leak.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import (
    emit_debezium_iceberg_sink_config,
)
from fluid_build.build_runners.debezium.runner import execute_debezium_build
from fluid_build.providers._iceberg_catalog import (
    GLUE_CATALOG_IMPL,
    S3_FILE_IO,
    ResolvedIcebergCatalog,
)

pytestmark = [pytest.mark.unit]


def _glue(**kw: Any) -> ResolvedIcebergCatalog:
    base: Dict[str, Any] = dict(
        catalog_type="glue",
        warehouse="s3://b/wh",
        fq_table="db.tbl",
        catalog_impl=GLUE_CATALOG_IMPL,
        io_impl=S3_FILE_IO,
        region="us-east-1",
    )
    base.update(kw)
    return ResolvedIcebergCatalog(**base)


# ── core mapping (every key pinned verbatim to the memiiso surface) ─────────


def test_emits_warehouse_and_namespace():
    cfg = emit_debezium_iceberg_sink_config(_glue())
    assert cfg["warehouse"] == "s3://b/wh"
    assert cfg["table-namespace"] == "db"  # database leg of fq_table only


def test_glue_emits_catalog_impl_not_type():
    # catalog-impl XOR type — emitting BOTH crashes the consumer (CatalogUtil).
    cfg = emit_debezium_iceberg_sink_config(_glue())
    assert cfg["catalog-impl"] == GLUE_CATALOG_IMPL
    assert "type" not in cfg


def test_builtin_emits_type_not_catalog_impl():
    resolved = ResolvedIcebergCatalog(
        catalog_type="hive", warehouse="s3://b/wh", fq_table="db.t", catalog_impl=None
    )
    cfg = emit_debezium_iceberg_sink_config(resolved)
    assert cfg["type"] == "hive"
    assert "catalog-impl" not in cfg


def test_no_kc_keys_leak():
    cfg = emit_debezium_iceberg_sink_config(_glue(id_columns=("id",), partition_by=("ts",)))
    # None of the Kafka-Connect-only concepts may appear, in any spelling.
    assert not any(k.startswith("iceberg.") for k in cfg)
    for forbidden in (
        "iceberg.tables",
        "iceberg.control.topic",
        "iceberg.coordinator.transactional.prefix",
        "iceberg.tables.default-id-columns",
        "connector.class",
        "control.topic",
        "catalog.type",
    ):
        assert forbidden not in cfg


def test_id_columns_to_upsert():
    cfg = emit_debezium_iceberg_sink_config(_glue(id_columns=("id", "tenant")))
    assert cfg["upsert"] == "true"
    assert cfg["create-identifier-fields"] == "true"
    assert "default-id-columns" not in cfg
    assert not any("id-columns" in k for k in cfg)


def test_no_id_columns_omits_upsert():
    cfg = emit_debezium_iceberg_sink_config(_glue(id_columns=()))
    assert "upsert" not in cfg
    assert "create-identifier-fields" not in cfg


def test_region_is_passthrough_spelling():
    cfg = emit_debezium_iceberg_sink_config(_glue(region="us-east-1"))
    assert cfg["client.region"] == "us-east-1"


def test_partition_by_spelling():
    cfg = emit_debezium_iceberg_sink_config(_glue(partition_by=("event_year", "level")))
    assert cfg["partition-by"] == "event_year,level"
    assert "tables.default-partition-by" not in cfg


def test_extra_props_passthrough():
    cfg = emit_debezium_iceberg_sink_config(
        _glue(extra_catalog_props={"s3.path-style-access": "true"})
    )
    assert cfg["s3.path-style-access"] == "true"


def test_uri_emitted_for_rest():
    resolved = ResolvedIcebergCatalog(
        catalog_type="rest",
        warehouse="s3://b/wh",
        fq_table="db.t",
        uri="http://iceberg:8181",
        io_impl=S3_FILE_IO,
    )
    cfg = emit_debezium_iceberg_sink_config(resolved)
    assert cfg["uri"] == "http://iceberg:8181"
    assert cfg["type"] == "rest"


def test_operator_override_wins():
    cfg = emit_debezium_iceberg_sink_config(_glue(), overrides={"warehouse": "s3://override/wh"})
    assert cfg["warehouse"] == "s3://override/wh"


def test_degenerate_namespace_omitted():
    # a binding with no location resolves to "None.None" -> no namespace leaks.
    resolved = ResolvedIcebergCatalog(catalog_type="rest", warehouse="", fq_table="None.None")
    cfg = emit_debezium_iceberg_sink_config(resolved)
    assert "table-namespace" not in cfg


def test_pure_deterministic():
    assert emit_debezium_iceberg_sink_config(_glue()) == emit_debezium_iceberg_sink_config(_glue())


# ── wire-in through the real embedded-server runner ─────────────────────────


def _contract(*, sink: Dict[str, Any], binding: Dict[str, Any] = None) -> Dict[str, Any]:
    binding = binding or {
        "platform": "aws",
        "format": "iceberg",
        "location": {
            "database": "bronze",
            "table": "orders",
            "bucket": "lake",
            "region": "us-east-1",
        },
    }
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.dbz_ice",
        "name": "X",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "debezium",
                "capabilities": ["cdc", "streaming"],
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {
                            "host": "db",
                            "port": 5432,
                            "database": "mydb",
                            "user": "u",
                            "password": "p",
                        },
                        "mode": "cdc",
                        "streams": ["public.orders"],
                    },
                    "sink": {"format": "iceberg"},
                    "debezium": {"deployment": {"mode": "embedded"}, "server": {"sink": sink}},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": binding,
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def _props_text(contract: Dict[str, Any], tmp_path: Path) -> str:
    # binary is absent on PATH -> the runner fails, but writes the config first.
    execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    path = tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"
    assert path.exists()
    return path.read_text()


def test_wire_in_derives_when_no_handwritten_config(tmp_path: Path):
    text = _props_text(_contract(sink={"type": "iceberg"}), tmp_path)
    assert "debezium.sink.type=iceberg" in text
    assert "debezium.sink.iceberg.table-namespace=bronze" in text
    assert "debezium.sink.iceberg.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog" in text
    assert "debezium.sink.iceberg.warehouse=s3://" in text
    # mutual exclusion: glue emits catalog-impl, never type
    assert "debezium.sink.iceberg.type=" not in text
    # no KC-only keys leak through the prefix loop
    assert "iceberg.control.topic" not in text
    assert "debezium.sink.iceberg.iceberg.tables" not in text


def test_wire_in_default_off_when_handwritten(tmp_path: Path):
    # hand-written config present, no enable flag -> NO derivation (byte-for-byte
    # like before PR8); only the operator's own keys appear.
    text = _props_text(
        _contract(sink={"type": "iceberg", "config": {"catalog.name": "rest"}}), tmp_path
    )
    assert "debezium.sink.iceberg.catalog.name=rest" in text
    assert "debezium.sink.iceberg.table-namespace=" not in text
    assert "debezium.sink.iceberg.catalog-impl=" not in text


def test_wire_in_opt_in_merges_operator_wins(tmp_path: Path):
    text = _props_text(
        _contract(
            sink={
                "type": "iceberg",
                "iceberg_sink_enabled": True,
                "config": {"warehouse": "s3://override/wh"},
            }
        ),
        tmp_path,
    )
    # derived keys appear AND the operator's warehouse wins
    assert "debezium.sink.iceberg.table-namespace=bronze" in text
    assert "debezium.sink.iceberg.warehouse=s3://override/wh" in text
    assert "debezium.sink.iceberg.warehouse=s3://lake" not in text


def test_wire_in_non_iceberg_sink_skips_derivation(tmp_path: Path):
    text = _props_text(_contract(sink={"type": "s3", "config": {"bucket.name": "b"}}), tmp_path)
    assert "debezium.sink.type=s3" in text
    assert "debezium.sink.s3.bucket.name=b" in text
    assert "table-namespace" not in text
    assert "catalog-impl" not in text


def test_wire_in_gate_off_for_explicit_empty_config(tmp_path: Path):
    # an explicit `config: {}` counts as "hand-written present" -> NO derivation
    # (mirrors the KC sink_connector_config default), so only the bare type line.
    text = _props_text(_contract(sink={"type": "iceberg", "config": {}}), tmp_path)
    assert "debezium.sink.type=iceberg" in text
    assert "debezium.sink.iceberg.table-namespace=" not in text
    assert "debezium.sink.iceberg.warehouse=" not in text


def test_config_file_is_chmod_600(tmp_path: Path):
    # the file carries the source DB password + any sink creds -> must be 0o600
    import stat as _stat

    contract = _contract(sink={"type": "iceberg"})
    execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    path = tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"
    assert path.exists()
    assert _stat.S_IMODE(path.stat().st_mode) == 0o600


def test_control_char_value_is_rejected_no_injection(tmp_path: Path):
    # a newline smuggled into a sink value must NOT inject a new directive into
    # the line-based .properties file — the build fails closed before any write.
    contract = _contract(
        sink={"type": "iceberg", "config": {"warehouse": "s3://x\ndebezium.evil=pwned"}}
    )
    rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc != 0
    path = tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"
    assert not path.exists() or "debezium.evil=pwned" not in path.read_text()


def test_properties_value_escapes_backslash():
    # Java Properties.load treats `\` as an escape introducer, so a literal
    # backslash in a value MUST be doubled or it is silently corrupted on read.
    from fluid_build.build_runners.debezium.runner import (
        _escape_properties_value,
        _properties_line,
    )

    assert _escape_properties_value("C:\\data") == "C:\\\\data"
    assert _properties_line("k", "a\\b") == "k=a\\\\b"
    assert _escape_properties_value(" leading") == "\\ leading"  # leading ws escaped
    assert _escape_properties_value("s3://lake/wh") == "s3://lake/wh"  # ordinary value untouched


def test_properties_key_escapes_separators():
    from fluid_build.build_runners.debezium.runner import _escape_properties_key

    assert _escape_properties_key("a=b") == "a\\=b"
    assert _escape_properties_key("a:b") == "a\\:b"
    # an ordinary dotted Debezium key is unchanged
    assert _escape_properties_key("debezium.sink.iceberg.warehouse") == (
        "debezium.sink.iceberg.warehouse"
    )
