# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A runner that puts connection fields into a connection URL keeps each field in its part.

dlt hands SQLAlchemy a rendered URL string, which is parsed again; the Kafka
Connect JDBC source builds ``jdbc:<protocol>://host:port/database``; Debezium
builds the same JDBC URL from ``database.hostname``, ``database.port`` and
``database.dbname``. A field that holds URL syntax would move the connection
to another host, so it is refused before the URL is built.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.build_runners.debezium.runner import build_connector_config
from fluid_build.build_runners.kafka_connect.runner import _debezium_config, _jdbc_config

ELSEWHERE = "elsewhere.example"
BASE: Dict[str, Any] = {"host": "localhost", "port": 5432, "user": "u", "password": "pw"}

DATABASES_WITH_URL_SYNTAX = [
    f"db?host={ELSEWHERE}",
    f"db;serverName={ELSEWHERE}",
    "db&x",
    "db#x",
    "db@x",
    "db\\x",
    "db\nx",
]
HOSTS_WITH_URL_SYNTAX = [
    f"localhost/db?host={ELSEWHERE}",
    f"localhost?host={ELSEWHERE}",
    f"localhost;serverName={ELSEWHERE}",
    "local host",
    "localhost\n",
]
PORTS_THAT_ARE_NOT_NUMBERS = [f"5432/db?host={ELSEWHERE}", f"5432,{ELSEWHERE}:5432", "54x"]


def _cases():
    for database in DATABASES_WITH_URL_SYNTAX:
        yield {"database": database}
    for host in HOSTS_WITH_URL_SYNTAX:
        yield {"database": "orders", "host": host}
    for port in PORTS_THAT_ARE_NOT_NUMBERS:
        yield {"database": "orders", "port": port}


CASES = list(_cases())


# ── Kafka Connect JDBC source ────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("fields", CASES)
def test_kafka_connect_jdbc_url_refuses_url_syntax_in_a_field(fields):
    with pytest.raises(ValueError) as info:
        _jdbc_config({**BASE, **fields}, "postgres")
    assert ELSEWHERE not in str(info.value)


@pytest.mark.unit
def test_kafka_connect_jdbc_url_keeps_plain_fields():
    cfg = _jdbc_config({**BASE, "host": "db.internal", "database": "orders"}, "postgres")
    assert cfg["connection.url"] == "jdbc:postgresql://db.internal:5432/orders"


@pytest.mark.unit
@pytest.mark.parametrize("fields", CASES)
def test_kafka_connect_debezium_config_refuses_url_syntax_in_a_field(fields):
    with pytest.raises(ValueError):
        _debezium_config({**BASE, **fields}, "postgres", topic_prefix="t", streams=[])


# ── Debezium ─────────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["postgres", "mysql", "sqlserver", "oracle"])
@pytest.mark.parametrize("fields", CASES)
def test_debezium_config_refuses_url_syntax_in_a_field(kind, fields):
    with pytest.raises(ValueError):
        build_connector_config(
            kind,
            connection={**BASE, **fields},
            streams=[],
            server_name="s",
            snapshot_mode="initial",
        )


@pytest.mark.unit
def test_debezium_config_keeps_plain_fields():
    cfg = build_connector_config(
        "postgres",
        connection={**BASE, "host": "db.internal", "database": "orders"},
        streams=[],
        server_name="s",
        snapshot_mode="initial",
    )
    assert cfg["database.hostname"] == "db.internal"
    assert cfg["database.dbname"] == "orders"


# ── dlt sql_database ─────────────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch):
    pytest.importorskip("sqlalchemy")
    sql_database_module = pytest.importorskip("dlt.sources.sql_database")
    seen: Dict[str, Any] = {}

    class _Source:
        def with_resources(self, *names):
            return self

    def fake_sql_database(**kwargs):
        seen.update(kwargs)
        return _Source()

    monkeypatch.setattr(sql_database_module, "sql_database", fake_sql_database)
    return seen


def _dlt_source(connection):
    from fluid_build.build_runners.dlt.runner import _make_sql_database_source

    return _make_sql_database_source(connection, [])


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields",
    [
        {"database": f"db?host={ELSEWHERE}"},
        {"database": f"db?options=x&host={ELSEWHERE}"},
        {"host": f"localhost/db?host={ELSEWHERE}"},
        {"drivername": f"postgresql+psycopg://x@{ELSEWHERE}/db?options="},
        {"drivername": "postgresql+psycopg+extra"},
    ],
)
def test_dlt_refuses_a_field_that_would_change_the_url(captured, fields):
    with pytest.raises(ValueError) as info:
        _dlt_source({**BASE, "database": "orders", **fields})
    assert captured == {}
    assert ELSEWHERE not in str(info.value)
    assert "pw" not in str(info.value)


@pytest.mark.unit
def test_dlt_url_parses_back_to_exactly_the_connection(captured):
    from sqlalchemy.engine import make_url

    password = "p@ss/w?rd#;&%"  # pragma: allowlist secret
    _dlt_source({**BASE, "password": password, "database": "orders", "host": "db.internal"})
    url = make_url(captured["credentials"])
    assert (url.drivername, url.username, url.password) == ("postgresql+psycopg", "u", password)
    assert (url.host, url.port, url.database, dict(url.query)) == (
        "db.internal",
        5432,
        "orders",
        {},
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields",
    [{"password": 12345}, {"password": True}, {"host": ""}],
    ids=["numeric-password", "boolean-password", "empty-host"],
)
def test_dlt_keeps_a_field_the_url_reads_back_alike(captured, fields):
    """A YAML number is rendered as its text, and an empty host is the local socket."""
    _dlt_source({**BASE, "database": "orders", **fields})
    assert captured["credentials"].startswith("postgresql+psycopg://u:")


# ── a refused field is a failed build, not an unexpected error ───────────────


def _kc_contract(database: str) -> Dict[str, Any]:
    from tests.build_runners.test_kafka_connect_full_matrix import _base_contract

    return _base_contract(
        source={
            "kind": "postgres",
            "connection": {**BASE, "database": database},
            "mode": "full_refresh",
            "streams": ["public.orders"],
        },
        kc_props={
            "deployment": {"server_url": "http://kafka-connect.test:8083"},
            "connector_name": "url-syntax",
        },
    )


@pytest.mark.unit
def test_kafka_connect_reports_url_syntax_as_a_failed_build(kafka_connect_mock, tmp_path):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _kc_contract(f"orders?host={ELSEWHERE}")
    rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc != 0
    assert "url-syntax" not in kafka_connect_mock.connectors


@pytest.mark.unit
def test_debezium_reports_url_syntax_as_a_failed_build(kafka_connect_mock, tmp_path):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build
    from tests.build_runners.test_debezium_full_matrix import _base_contract

    contract = _base_contract(
        source={
            "kind": "postgres",
            "connection": {**BASE, "database": f"orders?host={ELSEWHERE}"},
            "mode": "cdc",
            "streams": ["public.orders"],
        },
        dbz_props={
            "deployment": {
                "mode": "bring-your-own",
                "server_url": "http://kafka-connect.test:8083",
            },
            "snapshot_mode": "initial",
            "connector_name": "url-syntax",
        },
    )
    rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc != 0
    assert "url-syntax" not in kafka_connect_mock.connectors


@pytest.mark.unit
def test_a_sql_server_instance_name_in_the_host_is_kept():
    connection = {**BASE, "host": "db.internal\\SQLEXPRESS", "database": "orders"}
    assert _jdbc_config(connection, "sqlserver")["connection.url"].startswith(
        "jdbc:sqlserver://db.internal\\SQLEXPRESS:5432/"
    )
