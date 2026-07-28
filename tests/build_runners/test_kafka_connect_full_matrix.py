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

"""Kafka Connect engine — full matrix (Slice F).

REST mode against ``kafka_connect_mock`` respx server, full connector lifecycle
(create / get / update / delete / status), source-class dispatch (jdbc / s3 /
salesforce / mongodb), sink-class dispatch (jdbc / s3 / snowflake / iceberg /
bigquery), mode mapping, capability declarations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunnerCapability
from fluid_build.build_runners.kafka_connect.runner import (
    KafkaConnectRunner,
    _jdbc_config,
    _map_acquisition_mode_to_kc,
    execute_kafka_connect_build,
    resolve_sink_connector,
    resolve_source_connector,
)


def _base_contract(
    *,
    source: Dict[str, Any],
    kc_props: Dict[str, Any] = None,
    binding: Dict[str, Any] = None,
) -> Dict[str, Any]:
    expose = {
        "exposeId": "data",
        "kind": "table",
        "binding": binding or {"platform": "local", "format": "parquet"},
        "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
    }
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.kc_test",
        "name": "KC Test",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "capabilities": ["streaming", "at_least_once"],
                "properties": {
                    "source": source,
                    "sink": {"format": "parquet"},
                    "kafka-connect": kc_props or {},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [expose],
    }


# ── Capability declarations ────────────────────────────────────────────


class TestCapabilities:
    def test_class_attributes(self):
        r = KafkaConnectRunner()
        assert r.name == "kafka-connect"
        assert "bring-your-own" in r.declared_modes
        assert "managed" in r.declared_modes
        # Embedded mode is not supported (Kafka cluster required).
        assert "embedded" not in r.declared_modes

    def test_streaming_capabilities(self):
        r = KafkaConnectRunner()
        for cap in (
            RunnerCapability.STREAMING,
            RunnerCapability.AT_LEAST_ONCE,
            RunnerCapability.EXACTLY_ONCE,
            RunnerCapability.CDC,
        ):
            assert cap in r.declared_capabilities

    def test_does_not_declare_full_refresh_only(self):
        # Kafka Connect for streaming does not declare FULL_REFRESH or
        # AT_MOST_ONCE; bulk mode is supported via the JDBC source's "bulk"
        # mode but ZK-Connect's semantics are streaming-first.
        r = KafkaConnectRunner()
        assert RunnerCapability.AT_MOST_ONCE not in r.declared_capabilities


# ── Source connector class dispatch ────────────────────────────────────


class TestSourceConnectorClass:
    @pytest.mark.parametrize(
        "kind, expected",
        [
            ("jdbc", "io.confluent.connect.jdbc.JdbcSourceConnector"),
            ("postgres", "io.confluent.connect.jdbc.JdbcSourceConnector"),
            ("mysql", "io.confluent.connect.jdbc.JdbcSourceConnector"),
            ("sqlserver", "io.confluent.connect.jdbc.JdbcSourceConnector"),
            ("oracle", "io.confluent.connect.jdbc.JdbcSourceConnector"),
            ("s3", "io.confluent.connect.s3.S3SourceConnector"),
            ("salesforce", "io.confluent.salesforce.SalesforceCdcSourceConnector"),
            ("mongodb", "com.mongodb.kafka.connect.MongoSourceConnector"),
        ],
    )
    def test_known_kinds(self, kind: str, expected: str):
        assert resolve_source_connector(kind) == expected

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            resolve_source_connector("alien")

    def test_override_wins(self):
        assert (
            resolve_source_connector("postgres", override="custom.SourceConnector")
            == "custom.SourceConnector"
        )


# ── Sink connector class dispatch ──────────────────────────────────────


class TestSinkConnectorClass:
    @pytest.mark.parametrize(
        "fmt, expected_substring",
        [
            ("snowflake_table", "snowflake"),
            ("bigquery_table", "BigQuery"),
            ("iceberg", "Iceberg"),
            ("s3_file", "S3Sink"),
            ("anything-jdbc", "JdbcSink"),
        ],
    )
    def test_dispatch(self, fmt: str, expected_substring: str):
        cls = resolve_sink_connector(fmt)
        assert expected_substring.lower() in cls.lower()

    def test_default_falls_back_to_s3(self):
        assert "S3Sink" in resolve_sink_connector("xyz_unknown")


# ── Mode mapping ───────────────────────────────────────────────────────


class TestModeMapping:
    @pytest.mark.parametrize(
        "fluid_mode, kc_mode",
        [
            ("full_refresh", "bulk"),
            ("incremental_append", "incrementing"),
            ("incremental_dedup", "timestamp+incrementing"),
            ("cdc", "timestamp"),
            ("streaming", "incrementing"),
        ],
    )
    def test_maps(self, fluid_mode: str, kc_mode: str):
        assert _map_acquisition_mode_to_kc(fluid_mode) == kc_mode


# ── JDBC config translator ─────────────────────────────────────────────


class TestJdbcConfigTranslator:
    def test_postgres_url(self):
        cfg = _jdbc_config(
            {"host": "db.example", "port": 5432, "database": "mydb", "user": "u", "password": "p"},
            "postgres",
        )
        assert cfg["connection.url"] == "jdbc:postgresql://db.example:5432/mydb"
        assert cfg["connection.user"] == "u"
        assert cfg["connection.password"] == "p"

    def test_mysql_url(self):
        cfg = _jdbc_config(
            {"host": "db", "port": 3306, "database": "mydb", "user": "u", "password": "p"},
            "mysql",
        )
        assert cfg["connection.url"] == "jdbc:mysql://db:3306/mydb"

    def test_sqlserver_url(self):
        cfg = _jdbc_config(
            {"host": "db", "port": 1433, "database": "mydb", "user": "u", "password": "p"},
            "sqlserver",
        )
        assert cfg["connection.url"] == "jdbc:sqlserver://db:1433/mydb"

    def test_non_jdbc_passthrough(self):
        cfg = _jdbc_config({"bucket": "my-bucket", "secretRef": "vault://x"}, "s3")
        assert cfg == {"bucket": "my-bucket"}


# ── REST lifecycle ─────────────────────────────────────────────────────


class TestRestLifecycle:
    def test_create_when_missing(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={
                "deployment": {"server_url": "http://kafka-connect.test:8083"},
                "connector_name": "test-pg-source",
            },
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        # Mock recorded create+status; no update because the connector didn't pre-exist.
        assert "create" in kafka_connect_mock.calls
        assert "status" in kafka_connect_mock.calls
        assert "test-pg-source" in kafka_connect_mock.connectors

    def test_avro_schema_registry_config_emitted(self, kafka_connect_mock, tmp_path: Path):
        """When ``schema_registry.url`` is set, the runner emits AvroConverter
        properties pointing at it. This is the contract → wire bridge that
        makes the Avro/Schema-Registry path actually work in production."""
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={
                "deployment": {"server_url": "http://kafka-connect.test:8083"},
                "connector_name": "avro-pg-source",
                "schema_registry": {"url": "http://schema-registry.test:8081"},
            },
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        cfg = kafka_connect_mock.connectors["avro-pg-source"]["config"]
        assert cfg["key.converter"] == "io.confluent.connect.avro.AvroConverter"
        assert cfg["value.converter"] == "io.confluent.connect.avro.AvroConverter"
        assert cfg["value.converter.schema.registry.url"] == "http://schema-registry.test:8081"

    def test_no_schema_registry_means_default_converter(self, kafka_connect_mock, tmp_path: Path):
        """Without a schema-registry block the runner does NOT inject Avro
        converters — the default Connect JSON converter remains in effect."""
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={
                "deployment": {"server_url": "http://kafka-connect.test:8083"},
                "connector_name": "json-pg-source",
            },
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        cfg = kafka_connect_mock.connectors["json-pg-source"]["config"]
        assert "key.converter" not in cfg
        assert "value.converter" not in cfg

    def test_update_when_exists(self, kafka_connect_mock, tmp_path: Path):
        # Pre-stage an existing connector.
        kafka_connect_mock.connectors["preexisting"] = {
            "name": "preexisting",
            "config": {"connector.class": "old"},
            "type": "source",
        }
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={
                "deployment": {"server_url": "http://kafka-connect.test:8083"},
                "connector_name": "preexisting",
            },
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert "update_config" in kafka_connect_mock.calls
        # New connector class replaced the old one.
        assert (
            kafka_connect_mock.connectors["preexisting"]["config"]["connector.class"]
            == "io.confluent.connect.jdbc.JdbcSourceConnector"
        )

    def test_with_sink_companion(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "incremental_append",
                "streams": ["public.orders"],
            },
            kc_props={
                "deployment": {"server_url": "http://kafka-connect.test:8083"},
                "connector_name": "src",
                "sink_connector_name": "snk",
                "sink_connector_config": {
                    "connector.class": "io.confluent.connect.s3.S3SinkConnector",
                    "topics": "public.orders",
                    "s3.bucket.name": "fluid-bronze",
                },
            },
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert "src" in kafka_connect_mock.connectors
        assert "snk" in kafka_connect_mock.connectors

    def test_dry_run_no_rest_calls(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={"deployment": {"server_url": "http://kafka-connect.test:8083"}},
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=True)
        assert rc == 0
        assert kafka_connect_mock.calls == []

    def test_run_record_persisted(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={"deployment": {"server_url": "http://kafka-connect.test:8083"}},
        )
        execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        runs = list(
            (tmp_path / ".fluid" / "runs" / contract["id"] / "ingest" / "runs").glob("*.json")
        )
        assert len(runs) == 1
        rec = json.loads(runs[0].read_text())
        assert rec["facets"]["engine"] == "kafka-connect"
        assert rec["facets"]["connector_class"].endswith("JdbcSourceConnector")
        assert rec["facets"]["connector_state"] == "RUNNING"


# ── Failure modes ──────────────────────────────────────────────────────


class TestFailureModes:
    def test_missing_server_url(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {"host": "db"},
                "mode": "full_refresh",
                "streams": ["x"],
            },
            kc_props={"deployment": {}},
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_unknown_source_kind(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "alien-system",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["x"],
            },
            kc_props={"deployment": {"server_url": "http://kafka-connect.test:8083"}},
        )
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_missing_source_block(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {"host": "db"},
                "mode": "full_refresh",
                "streams": ["x"],
            },
            kc_props={"deployment": {"server_url": "http://kafka-connect.test:8083"}},
        )
        del contract["builds"][0]["properties"]["source"]
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0


# ── Dispatcher integration ─────────────────────────────────────────────


class TestDispatcher:
    def test_base_dispatches_to_kafka_connect(self, kafka_connect_mock, tmp_path: Path):
        from fluid_build.build_runners.base import (
            ACQUISITION_ENGINES,
            _execute_acquisition_build,
            is_acquisition_build,
        )

        assert "kafka-connect" in ACQUISITION_ENGINES
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "x",
                    "user": "u",
                    "password": "p",
                },
                "mode": "full_refresh",
                "streams": ["public.orders"],
            },
            kc_props={"deployment": {"server_url": "http://kafka-connect.test:8083"}},
        )
        build = contract["builds"][0]
        assert is_acquisition_build(build)
        rc = _execute_acquisition_build(build, contract, tmp_path, dry_run=False, sample_rows=None)
        assert rc == 0
