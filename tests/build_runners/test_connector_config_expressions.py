# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contract text bound for a connector's config may not hold a ``${...}`` expression.

Debezium Server reads ``application.properties`` through SmallRye Config, which
expands ``${...}`` in a value from every config source, the environment
included. A Kafka Connect worker resolves ``${provider:path:key}`` with its
config providers. Either would send what it read to wherever the connector
connects, so a contract-derived value with ``${`` is refused, naming the key
and never the value. forge's own ``${FLUID_DBZ_...}`` credential lines stay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners._acquisition_common import refuse_config_expressions
from fluid_build.build_runners.debezium.runner import render_application_properties

BASE: Dict[str, Any] = {"host": "127.0.0.1", "port": 5432, "user": "app", "database": "orders"}
EXPRESSION = "${FLUID_DBZ_SOURCE_DATABASE_PASSWORD}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value, label",
    [
        ({"url": f"https://sink.example/{EXPRESSION}"}, "sink.url"),
        ({"headers": {"X-Token": "${env:BAO_TOKEN}"}}, "sink.headers.X-Token"),
        ({"topics": ["a", "${file:/etc/secrets:pw}"]}, "sink.topics[1]"),
        ({"a${b}": "x"}, "sink"),
    ],
)
def test_an_expression_anywhere_is_refused_by_its_key(value, label):
    with pytest.raises(ValueError) as caught:
        refuse_config_expressions(value, where="sink")
    message = str(caught.value)
    assert label in message
    for secret_text in ("FLUID_DBZ_SOURCE_DATABASE_PASSWORD", "BAO_TOKEN", "/etc/secrets"):
        assert secret_text not in message


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        {"url": "https://sink.example/path", "regex": "^orders$", "price": "$5"},
        {"template": "{{ env.X }}", "n": 3, "flag": True, "none": None},
    ],
)
def test_text_without_an_expression_passes(value):
    refuse_config_expressions(value, where="sink")


@pytest.mark.unit
def test_a_sink_value_with_an_expression_is_not_written_to_application_properties():
    with pytest.raises(ValueError) as caught:
        source = {"database.password": "pw"}  # pragma: allowlist secret
        render_application_properties(source, "http", {"url": f"https://sink.example/{EXPRESSION}"})
    assert "properties.debezium.server.sink.config.url" in str(caught.value)
    assert "FLUID_DBZ" not in str(caught.value)


@pytest.mark.unit
def test_a_sink_type_with_an_expression_is_refused():
    with pytest.raises(ValueError):
        render_application_properties({}, "http${x}", {})


@pytest.mark.unit
def test_forges_own_credential_line_keeps_its_expression():
    source = {"database.password": "p${x}"}  # pragma: allowlist secret
    properties, env = render_application_properties(source, "iceberg", {"catalog-name": "c"})
    assert "debezium.source.database.password=${FLUID_DBZ_SOURCE_DATABASE_PASSWORD}" in properties
    assert env["FLUID_DBZ_SOURCE_DATABASE_PASSWORD"] == "p${x}"  # pragma: allowlist secret


# ── the runners refuse before anything is built or sent ──────────────────────


def _kc_contract(connection: Dict[str, Any], **kc_extra: Any) -> Dict[str, Any]:
    from tests.build_runners.test_kafka_connect_full_matrix import _base_contract

    return _base_contract(
        source={
            "kind": "postgres",
            "connection": connection,
            "mode": "full_refresh",
            "streams": ["public.orders"],
        },
        kc_props={
            "deployment": {"server_url": "http://kafka-connect.test:8083"},
            "connector_name": "expr",
            **kc_extra,
        },
    )


def _dbz_contract(connection: Dict[str, Any], **dbz_extra: Any) -> Dict[str, Any]:
    from tests.build_runners.test_debezium_full_matrix import _base_contract

    return _base_contract(
        source={
            "kind": "postgres",
            "connection": connection,
            "mode": "cdc",
            "streams": ["public.orders"],
        },
        dbz_props={
            "deployment": {
                "mode": "bring-your-own",
                "server_url": "http://kafka-connect.test:8083",
            },
            "snapshot_mode": "initial",
            "connector_name": "expr",
            **dbz_extra,
        },
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "contract",
    [
        lambda: _kc_contract({**BASE, "database": "${env:BAO_TOKEN}"}),
        lambda: _kc_contract(
            BASE,
            sink_connector_config={
                "connector.class": "io.confluent.connect.http.HttpSinkConnector",
                "http.api.url": "https://sink.example/${env:BAO_TOKEN}",
            },
        ),
    ],
)
def test_kafka_connect_refuses_an_expression_before_any_rest_call(
    kafka_connect_mock, tmp_path, contract
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    built = contract()
    rc = execute_kafka_connect_build(built["builds"][0], built, tmp_path, dry_run=False)
    assert rc != 0
    assert "expr" not in kafka_connect_mock.connectors
    assert "expr-sink" not in kafka_connect_mock.connectors


@pytest.mark.unit
def test_debezium_on_kafka_connect_refuses_an_expression_in_extra_config(
    kafka_connect_mock, tmp_path
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    contract = _dbz_contract(BASE, extra_config={"heartbeat.action.query": "SELECT '${env:X}'"})
    rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc != 0
    assert "expr" not in kafka_connect_mock.connectors


@pytest.mark.unit
def test_embedded_debezium_writes_no_properties_for_an_expression(tmp_path: Path):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    contract = _dbz_contract(
        BASE,
        deployment={"mode": "embedded"},
        server={"sink": {"type": "http", "config": {"url": f"https://sink.example/{EXPRESSION}"}}},
    )
    rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc != 0
    assert not list(tmp_path.rglob("application.properties"))


@pytest.mark.unit
def test_a_secret_whose_value_holds_dollar_brace_still_connects(
    kafka_connect_mock, tmp_path, monkeypatch
):
    """The resolved secret is not contract text: only what the contract wrote is read."""
    from fluid_build.build_runners.debezium.runner import execute_debezium_build
    from fluid_build.secrets import secret_run

    monkeypatch.setenv("FLUID_DBZ_TEST_PW", "p${x}")
    connection = {**BASE, "secretRef": "env://FLUID_DBZ_TEST_PW"}  # pragma: allowlist secret
    contract = _dbz_contract(connection)
    with secret_run("apply"):
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    sent = kafka_connect_mock.connectors["expr"]["config"]
    assert sent["database.password"] == "p${x}"  # pragma: allowlist secret
