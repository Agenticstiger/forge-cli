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


# ── runner preflight: fail closed BEFORE application.properties is written ──
#
# The embedded runner runs the same checks as `fluid validate` right before it
# derives the sink, so a sink the validator rejects never boots a server.

_LAKEKEEPER_NO_URI = {
    "platform": "local",
    "format": "iceberg",
    "location": {
        "database": "bronze",
        "table": "orders",
        "catalog": "lakekeeper",
        "warehouse": "analytics",
    },
}


def _props_path(contract: Dict[str, Any], tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"


def _run_embedded(contract: Dict[str, Any], tmp_path: Path):
    from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
    from fluid_build.build_runners.debezium.runner import DebeziumRunner

    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    return DebeziumRunner().run(ctx)


@pytest.mark.parametrize(
    "sink, binding, expected",
    [
        (
            {"type": "iceberg"},
            _LAKEKEEPER_NO_URI,
            "lakekeeper catalog requires binding.location.uri",
        ),
        (
            # a hand-written `type` merged over the derived Glue catalog-impl
            {"type": "iceberg", "iceberg_sink_enabled": True, "config": {"type": "rest"}},
            None,
            "would carry both type",
        ),
    ],
    ids=["missing-uri", "type-and-impl"],
)
def test_embedded_preflight_fails_before_writing_config(tmp_path: Path, sink, binding, expected):
    from fluid_build.api.runner import RunState

    contract = _contract(sink=sink, binding=binding)
    result = _run_embedded(contract, tmp_path)
    assert result.state == RunState.FAILED
    assert expected in (result.error or ""), result.error
    assert "iceberg sink preflight failed" in result.error
    assert not _props_path(contract, tmp_path).exists()

    assert execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False) == 1
    assert not _props_path(contract, tmp_path).exists()


def test_embedded_complete_lakekeeper_derives_rest(tmp_path: Path):
    binding = {
        **_LAKEKEEPER_NO_URI,
        "location": {**_LAKEKEEPER_NO_URI["location"], "uri": "http://lakekeeper:8181/catalog"},
    }
    text = _props_text(_contract(sink={"type": "iceberg"}, binding=binding), tmp_path)
    assert "debezium.sink.iceberg.type=rest" in text
    assert "debezium.sink.iceberg.uri=http://lakekeeper:8181/catalog" in text
    assert "debezium.sink.iceberg.warehouse=analytics" in text
    assert "debezium.sink.iceberg.catalog-impl=" not in text


@pytest.mark.parametrize(
    "sink, binding, expected",
    [
        (
            {"type": "iceberg", "config": {"catalog.name": "rest"}},
            _LAKEKEEPER_NO_URI,
            "lakekeeper catalog requires binding.location.uri",
        ),
        (
            {"type": "iceberg", "config": {"type": "rest", "catalog-impl": "x.Y"}},
            None,
            "would carry both type",
        ),
    ],
    ids=["missing-uri", "both-selectors"],
)
def test_embedded_handwritten_config_is_preflighted(tmp_path: Path, sink, binding, expected):
    # Derivation is OFF for a hand-written config, but the server still writes
    # an Iceberg sink, so the run refuses what `fluid validate` refuses. It
    # used to write the file and boot a server that never started.
    from fluid_build.api.runner import RunState

    contract = _contract(sink=sink, binding=binding)
    result = _run_embedded(contract, tmp_path)
    assert result.state == RunState.FAILED
    assert expected in (result.error or ""), result.error
    assert "iceberg sink preflight failed" in result.error
    assert not _props_path(contract, tmp_path).exists()


def test_embedded_handwritten_config_validate_accepts_is_written_verbatim(tmp_path: Path):
    # Preflighted, but nothing is derived: the file stays what the operator wrote.
    complete = {
        **_LAKEKEEPER_NO_URI,
        "location": {**_LAKEKEEPER_NO_URI["location"], "uri": "http://lakekeeper:8181/catalog"},
    }
    contract = _contract(
        sink={"type": "iceberg", "config": {"catalog.name": "rest"}}, binding=complete
    )
    text = _props_text(contract, tmp_path)
    assert "debezium.sink.iceberg.catalog.name=rest" in text
    assert "debezium.sink.iceberg.uri=" not in text


def test_multi_build_embedded_runner_uses_the_executing_builds_properties(tmp_path: Path):
    # The runner used to read builds[0]'s Debezium settings for every build:
    # here that is a hand-written config, so ingest derived nothing and wrote
    # landing's catalog name under landing's server name.
    import copy

    from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
        iceberg_sink_preflight,
        validate_iceberg_sink,
    )

    contract = _contract(sink={"type": "iceberg"})
    ingest = contract["builds"][0]
    landing = copy.deepcopy(ingest)
    landing["id"] = "landing"
    landing["outputs"] = []
    landing["properties"]["sink"] = {"format": "parquet"}
    landing["properties"]["debezium"]["server_name"] = "landing_srv"
    landing["properties"]["debezium"]["server"]["sink"] = {
        "type": "iceberg",
        "config": {"catalog.name": "landing"},
    }
    contract["builds"] = [landing, ingest]
    assert validate_iceberg_sink(contract) == ([], [])
    assert iceberg_sink_preflight(contract, "ingest") is None

    execute_debezium_build(ingest, contract, tmp_path, dry_run=False)
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.table-namespace=bronze" in text
    assert "debezium.sink.iceberg.catalog-impl=org.apache.iceberg.aws.glue.GlueCatalog" in text
    assert "landing" not in text


def test_embedded_runner_refuses_a_build_the_contract_does_not_declare(tmp_path: Path):
    import copy

    from fluid_build.api.runner import RunState
    from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
    from fluid_build.build_runners.debezium.runner import DebeziumRunner

    contract = _contract(sink={"type": "iceberg"})
    ghost = copy.deepcopy(contract["builds"][0])
    ghost["id"] = "ghost"
    result = DebeziumRunner().run(build_acquisition_run_context(ghost, contract, tmp_path))
    assert result.state == RunState.FAILED
    assert "build 'ghost' is not in the contract's builds" in result.error


def test_bring_your_own_mode_is_not_preflighted(kafka_connect_mock, tmp_path: Path):
    # bring-your-own creates only the SOURCE connector; no sink is derived, so an
    # incomplete Iceberg binding is not this build's concern.
    contract = _contract(sink={"type": "iceberg"}, binding=_LAKEKEEPER_NO_URI)
    contract["builds"][0]["properties"]["debezium"] = {
        "deployment": {"mode": "bring-your-own", "server_url": "http://kafka-connect.test:8083"},
        "status_timeout_seconds": 1,
        "poll_interval_seconds": 0.01,
    }
    rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    assert "create" in kafka_connect_mock.calls
