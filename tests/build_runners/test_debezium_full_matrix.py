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
    resolve_server_binary,
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


# ── server_binary validation (arbitrary-binary-exec guard) ─────────────


class TestResolveServerBinary:
    """``properties.debezium.server_binary`` becomes argv[0] of a subprocess,
    so an unvalidated value is an arbitrary-binary-execution vector. The
    resolver validates it the way the meltano runner validates its tap/target
    binaries and dbt validates ``DBT_EXECUTABLE``: a bare name must resolve on
    PATH; an explicit path must be an existing executable file."""

    def test_none_falls_back_to_path_lookup(self, monkeypatch):
        import fluid_build.build_runners.debezium.runner as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/bin/{name}")
        assert resolve_server_binary(None) == "/usr/bin/debezium-server"

    def test_bare_name_on_path_is_accepted(self, monkeypatch):
        import fluid_build.build_runners.debezium.runner as mod

        monkeypatch.setattr(
            mod.shutil, "which", lambda name: "/opt/dbz/bin/run.sh" if name == "run.sh" else None
        )
        assert resolve_server_binary("run.sh") == "/opt/dbz/bin/run.sh"

    def test_bare_name_not_on_path_is_rejected(self, monkeypatch):
        import fluid_build.build_runners.debezium.runner as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        assert resolve_server_binary("definitely-not-installed") is None

    def test_explicit_path_to_nonexistent_file_is_rejected(self, tmp_path):
        # An absolute path that does not exist (or is not executable) must be
        # rejected — this is the core arbitrary-binary vector (e.g. /tmp/evil).
        assert resolve_server_binary(str(tmp_path / "evil")) is None

    def test_explicit_path_to_existing_executable_is_accepted(self, tmp_path):
        import os
        import stat

        binpath = tmp_path / "debezium-server"
        binpath.write_text("#!/bin/sh\nexit 0\n")
        binpath.chmod(binpath.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
        assert resolve_server_binary(str(binpath)) == str(binpath)
        assert os.access(binpath, os.X_OK)

    def test_explicit_path_to_directory_is_rejected(self, tmp_path):
        # A directory next to a real path must not be returned (would EACCES
        # at exec time); is_file() rejects it.
        d = tmp_path / "server_dir"
        d.mkdir()
        assert resolve_server_binary(str(d)) is None

    def test_shell_metacharacter_value_is_rejected(self, monkeypatch):
        # Defense-in-depth: even though argv (not shell) is used, a value like
        # "x; rm -rf /" has no path separator, so it routes to PATH lookup and
        # is rejected when shutil.which finds nothing.
        import fluid_build.build_runners.debezium.runner as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        assert resolve_server_binary("x; rm -rf /") is None


class TestEmbeddedBinaryNotExecuted:
    """Integration: a malicious ``server_binary`` must be rejected BEFORE any
    subprocess is spawned (fail-closed)."""

    def _embedded_contract(self, server_binary: str) -> Dict[str, Any]:
        return _base_contract(
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
                "server_binary": server_binary,
                "server": {"sink": {"type": "iceberg", "config": {}}},
            },
        )

    def test_malicious_binary_never_spawns_subprocess(self, tmp_path: Path, monkeypatch):
        import fluid_build.build_runners.debezium.runner as mod

        def _boom(*_a, **_k):
            raise AssertionError(
                "subprocess.run was reached with an unvalidated server_binary "
                "(arbitrary-binary-execution regression)"
            )

        monkeypatch.setattr(mod.subprocess, "run", _boom)
        # An absolute path that does not exist — the classic /tmp/evil vector.
        contract = self._embedded_contract(str(tmp_path / "evil"))
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0  # fail-closed
        # Config was still generated (generation precedes the binary gate).
        config_path = (
            tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"
        )
        assert config_path.exists()

    def test_injection_string_binary_never_spawns_subprocess(self, tmp_path: Path, monkeypatch):
        import fluid_build.build_runners.debezium.runner as mod

        def _boom(*_a, **_k):
            raise AssertionError("subprocess.run reached with injection-string binary")

        monkeypatch.setattr(mod.subprocess, "run", _boom)
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        contract = self._embedded_contract("x; touch /tmp/pwned")
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_valid_binary_is_executed(self, tmp_path: Path, monkeypatch):
        """Positive path: a normal binary name that resolves on PATH reaches
        subprocess.run (proves the guard does not break legitimate use)."""
        import subprocess as real_subprocess

        import fluid_build.build_runners.debezium.runner as mod

        fake_bin = tmp_path / "debezium-server"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        import stat as _stat

        fake_bin.chmod(fake_bin.stat().st_mode | _stat.S_IXUSR | _stat.S_IRUSR)

        spawned = {}

        def _fake_run(cmd, *_a, **_k):
            spawned["cmd"] = cmd
            # Mimic the long-running server hitting the timeout (the runner's
            # expected "binary booted" success path).
            raise real_subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        contract = self._embedded_contract(str(fake_bin))
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0  # TimeoutExpired → _success_embedded
        assert spawned["cmd"][0] == str(fake_bin)
        assert spawned["cmd"][1:3] == ["--config", spawned["cmd"][2]]


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
