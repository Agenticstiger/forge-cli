# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Debezium engine — full matrix (Slice G).

Covers Kafka-Connect mode (against ``kafka_connect_mock``) for all five
CDC source kinds × all five snapshot modes, plus Debezium Server (embedded)
config emission, plus capability declarations and dispatcher integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunnerCapability
from fluid_build.build_runners.debezium.runner import (
    SOURCE_CLASS_BY_KIND,
    DebeziumRunner,
    build_connector_config,
    execute_debezium_build,
    resolve_debezium_class,
    resolve_snapshot_mode,
)


def _base_contract(
    *,
    source: Dict[str, Any],
    dbz_props: Dict[str, Any] = None,
) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.dbz_test",
        "name": "Debezium Test",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "debezium",
                "capabilities": ["cdc", "streaming"],
                "properties": {
                    "source": source,
                    "sink": {"format": "iceberg"},
                    "debezium": dbz_props or {},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {"platform": "local", "format": "iceberg"},
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


# ── Capability declarations ────────────────────────────────────────────


class TestCapabilities:
    def test_class_attributes(self):
        r = DebeziumRunner()
        assert r.name == "debezium"
        assert r.declared_modes == frozenset({"embedded", "bring-your-own", "managed"})

    def test_capabilities_streaming_cdc(self):
        r = DebeziumRunner()
        for cap in (
            RunnerCapability.CDC,
            RunnerCapability.STREAMING,
            RunnerCapability.AT_LEAST_ONCE,
            RunnerCapability.SCHEMA_DISCOVERY,
        ):
            assert cap in r.declared_capabilities


# ── Connector class resolution ─────────────────────────────────────────


class TestDebeziumConnectorClass:
    @pytest.mark.parametrize(
        "kind",
        [
            "postgres",
            "postgres-cdc",
            "mysql",
            "mysql-cdc",
            "mongodb",
            "mongodb-cdc",
            "sqlserver",
            "sqlserver-cdc",
            "oracle",
            "oracle-cdc",
        ],
    )
    def test_known_kinds(self, kind: str):
        assert resolve_debezium_class(kind) == SOURCE_CLASS_BY_KIND[kind]

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            resolve_debezium_class("snowflake-cdc")

    def test_override_wins(self):
        assert (
            resolve_debezium_class("postgres", override="my.custom.Connector")
            == "my.custom.Connector"
        )


# ── Snapshot modes ─────────────────────────────────────────────────────


class TestSnapshotModes:
    @pytest.mark.parametrize("mode", ["initial", "schema_only", "never", "when_needed", "always"])
    def test_supported_modes(self, mode: str):
        assert resolve_snapshot_mode(mode) == mode

    def test_default_is_initial(self):
        assert resolve_snapshot_mode(None) == "initial"
        assert resolve_snapshot_mode("") == "initial"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            resolve_snapshot_mode("yolo")


# ── Connector config builder ──────────────────────────────────────────


class TestConnectorConfigBuilder:
    def test_postgres_config_keys(self):
        cfg = build_connector_config(
            "postgres",
            connection={
                "host": "db",
                "port": 5432,
                "database": "mydb",
                "user": "u",
                "password": "p",
            },
            streams=["public.orders"],
            server_name="bronze_orders",
            snapshot_mode="initial",
        )
        assert cfg["connector.class"] == "io.debezium.connector.postgresql.PostgresConnector"
        assert cfg["database.hostname"] == "db"
        assert cfg["database.port"] == "5432"
        assert cfg["database.dbname"] == "mydb"
        assert cfg["plugin.name"] == "pgoutput"
        assert cfg["snapshot.mode"] == "initial"
        assert cfg["table.include.list"] == "public.orders"

    def test_mysql_config_keys(self):
        cfg = build_connector_config(
            "mysql",
            connection={
                "host": "db",
                "port": 3306,
                "database": "mydb",
                "user": "u",
                "password": "p",
                "server_id": 42,
                "kafka_bootstrap_servers": "kafka:9092",
            },
            streams=["mydb.orders"],
            server_name="bronze_orders",
            snapshot_mode="schema_only",
        )
        assert cfg["connector.class"] == "io.debezium.connector.mysql.MySqlConnector"
        assert cfg["database.server.id"] == "42"
        assert cfg["snapshot.mode"] == "schema_only"
        assert cfg["schema.history.internal.kafka.topic"].startswith("bronze_orders.history")

    def test_mongo_uses_connection_string(self):
        cfg = build_connector_config(
            "mongodb",
            connection={"connection_string": "mongodb://m1:27017,m2:27017/?replicaSet=rs0"},
            streams=["mydb.collection"],
            server_name="bronze_mongo",
            snapshot_mode="never",
        )
        assert cfg["connector.class"] == "io.debezium.connector.mongodb.MongoDbConnector"
        assert cfg["mongodb.connection.string"].startswith("mongodb://")
        assert cfg["snapshot.mode"] == "never"

    def test_sqlserver_keys(self):
        cfg = build_connector_config(
            "sqlserver",
            connection={
                "host": "sql",
                "port": 1433,
                "database": "mydb",
                "user": "u",
                "password": "p",
                "kafka_bootstrap_servers": "kafka:9092",
            },
            streams=["dbo.orders"],
            server_name="bronze_sql",
            snapshot_mode="when_needed",
        )
        assert cfg["connector.class"] == "io.debezium.connector.sqlserver.SqlServerConnector"
        assert cfg["database.names"] == "mydb"
        assert cfg["snapshot.mode"] == "when_needed"

    def test_oracle_keys(self):
        cfg = build_connector_config(
            "oracle",
            connection={
                "host": "ora",
                "port": 1521,
                "database": "ORCL",
                "user": "u",
                "password": "p",
            },
            streams=["MYUSER.ORDERS"],
            server_name="bronze_ora",
            snapshot_mode="always",
        )
        assert cfg["connector.class"] == "io.debezium.connector.oracle.OracleConnector"
        assert cfg["database.dbname"] == "ORCL"
        assert cfg["snapshot.mode"] == "always"

    def test_extra_config_merged(self):
        cfg = build_connector_config(
            "postgres",
            connection={
                "host": "db",
                "port": 5432,
                "database": "mydb",
                "user": "u",
                "password": "p",
            },
            streams=["public.orders"],
            server_name="x",
            snapshot_mode="initial",
            extra={"heartbeat.interval.ms": "30000", "max.batch.size": "2048"},
        )
        assert cfg["heartbeat.interval.ms"] == "30000"
        assert cfg["max.batch.size"] == "2048"


# ── Kafka-Connect mode end-to-end ─────────────────────────────────────


class TestKafkaConnectMode:
    @pytest.mark.parametrize(
        "kind, expected_class",
        [
            ("postgres", "PostgresConnector"),
            ("mysql", "MySqlConnector"),
            ("mongodb", "MongoDbConnector"),
            ("sqlserver", "SqlServerConnector"),
            ("oracle", "OracleConnector"),
        ],
    )
    def test_create_connector_per_kind(
        self, kafka_connect_mock, tmp_path: Path, kind: str, expected_class: str
    ):
        contract = _base_contract(
            source={
                "kind": kind,
                "connection": {
                    "host": "db",
                    "port": 5432,
                    "database": "mydb",
                    "user": "u",
                    "password": "p",
                    "connection_string": "mongodb://localhost:27017",
                },
                "mode": "cdc",
                "streams": [f"public.{kind}_orders"],
            },
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
                "snapshot_mode": "initial",
                "connector_name": f"forge-dbz-{kind}",
            },
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        # The mock has the connector and its class is correct.
        cn = kafka_connect_mock.connectors[f"forge-dbz-{kind}"]
        assert expected_class in cn["config"]["connector.class"]

    def test_update_existing_connector(self, kafka_connect_mock, tmp_path: Path):
        kafka_connect_mock.connectors["forge-existing"] = {
            "name": "forge-existing",
            "config": {"connector.class": "old"},
            "type": "source",
        }
        contract = _base_contract(
            source={
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
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
                "connector_name": "forge-existing",
            },
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert "update_config" in kafka_connect_mock.calls
        assert (
            "PostgresConnector"
            in kafka_connect_mock.connectors["forge-existing"]["config"]["connector.class"]
        )

    def test_dry_run_no_rest_calls(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
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
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
            },
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=True)
        assert rc == 0
        assert kafka_connect_mock.calls == []

    def test_run_record_persisted(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={
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
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
                "snapshot_mode": "initial",
            },
        )
        execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        runs = list(
            (tmp_path / ".fluid" / "runs" / contract["id"] / "ingest" / "runs").glob("*.json")
        )
        assert len(runs) == 1
        rec = json.loads(runs[0].read_text())
        assert rec["facets"]["engine"] == "debezium"
        assert rec["facets"]["mode"] == "kafka-connect"
        assert rec["facets"]["snapshot_mode"] == "initial"


# ── Debezium Server (embedded) ─────────────────────────────────────────


class TestDebeziumServerEmbedded:
    def test_config_generated_when_binary_missing(self, tmp_path: Path):
        contract = _base_contract(
            source={
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
            dbz_props={
                "deployment": {"mode": "embedded"},
                "server": {
                    "sink": {
                        "type": "iceberg",
                        "config": {
                            "catalog.name": "rest",
                            "table.namespace": "bronze",
                        },
                    }
                },
            },
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        # Without the binary on PATH the runner returns failure, but the
        # config file must still have been generated.
        assert rc != 0
        config_path = (
            tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"
        )
        assert config_path.exists()
        text = config_path.read_text()
        assert (
            "debezium.source.connector.class=io.debezium.connector.postgresql.PostgresConnector"
            in text
        )
        assert "debezium.sink.type=iceberg" in text
        assert "debezium.sink.iceberg.catalog.name=rest" in text


# ── Failure modes ──────────────────────────────────────────────────────


class TestFailureModes:
    def test_kafka_connect_missing_server_url(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {"host": "db"},
                "mode": "cdc",
                "streams": ["x"],
            },
            dbz_props={"deployment": {"mode": "bring-your-own"}},
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_unknown_source_kind(self, kafka_connect_mock, tmp_path: Path):
        contract = _base_contract(
            source={"kind": "redis-cdc", "connection": {}, "mode": "cdc", "streams": ["x"]},
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
            },
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_unknown_deployment_mode(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {"host": "db"},
                "mode": "cdc",
                "streams": ["x"],
            },
            dbz_props={"deployment": {"mode": "novel-mode"}},
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_invalid_snapshot_mode(self, kafka_connect_mock, tmp_path: Path):
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
                "mode": "cdc",
                "streams": ["public.orders"],
            },
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
                "snapshot_mode": "yolo",
            },
        )
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_missing_source_block(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {"host": "db"},
                "mode": "cdc",
                "streams": ["x"],
            },
            dbz_props={"deployment": {"mode": "bring-your-own", "server_url": "http://x"}},
        )
        del contract["builds"][0]["properties"]["source"]
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0


# ── Dispatcher integration ─────────────────────────────────────────────


class TestDispatcher:
    def test_base_dispatches_to_debezium(self, kafka_connect_mock, tmp_path: Path):
        from fluid_build.build_runners.base import (
            ACQUISITION_ENGINES,
            _execute_acquisition_build,
            is_acquisition_build,
        )

        assert "debezium" in ACQUISITION_ENGINES
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
                "mode": "cdc",
                "streams": ["public.orders"],
            },
            dbz_props={
                "deployment": {
                    "mode": "bring-your-own",
                    "server_url": "http://kafka-connect.test:8083",
                },
            },
        )
        build = contract["builds"][0]
        assert is_acquisition_build(build)
        rc = _execute_acquisition_build(build, contract, tmp_path, dry_run=False, sample_rows=None)
        assert rc == 0
