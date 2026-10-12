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

"""``sink.catalogAuth`` in the configs the two streaming runners derive.

Kafka Connect takes the auth properties under ``iceberg.catalog.``; embedded
Debezium Server takes them bare and the runner adds ``debezium.sink.iceberg.``.
Either way the secret is an ``env:<NAME>`` reference, never its value, and
never a KIP-887 ``${env:...}`` or Quarkus ``${...}`` expression. The embedded
runner starts the server with its own environment, so its preflight refuses a
run whose variable is unset or empty.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.providers._iceberg_catalog import ResolvedIcebergCatalog
from fluid_build.providers._iceberg_catalog_auth import parse_catalog_auth

pytestmark = [pytest.mark.unit]

_KEYCLOAK = "http://keycloak:8080/realms/iceberg/protocol/openid-connect/token"
_SECRET_VALUE = "spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret
_OAUTH2 = {
    "type": "oauth2",
    "credentialEnv": "LAKEKEEPER_CREDENTIAL",
    "oauth2ServerUri": _KEYCLOAK,
    "scope": "lakekeeper",
}
_BEARER = {"type": "bearer", "tokenEnv": "UC_TOKEN"}

_LAKEKEEPER = ResolvedIcebergCatalog(
    catalog_type="rest",
    warehouse="demo",
    fq_table="bronze.orders",
    uri="http://lakekeeper:8181/catalog",
    kind="lakekeeper",
)


def _auth(raw: Dict[str, Any]):
    auth, problems = parse_catalog_auth(raw)
    assert problems == []
    return auth


# ── emitters ────────────────────────────────────────────────────────────────


def test_kafka_connect_oauth2_properties_under_the_catalog_prefix():
    cfg = emit_iceberg_sink_config(
        _LAKEKEEPER, product_id="p", topics=["t"], catalog_auth=_auth(_OAUTH2)
    )
    assert cfg["iceberg.catalog.rest.auth.type"] == "oauth2"
    assert cfg["iceberg.catalog.credential"] == "env:LAKEKEEPER_CREDENTIAL"
    assert cfg["iceberg.catalog.oauth2-server-uri"] == _KEYCLOAK
    assert cfg["iceberg.catalog.scope"] == "lakekeeper"
    assert not any("${" in v for v in cfg.values())


def test_kafka_connect_bearer_properties():
    cfg = emit_iceberg_sink_config(
        _LAKEKEEPER, product_id="p", topics=["t"], catalog_auth=_auth(_BEARER)
    )
    assert cfg["iceberg.catalog.rest.auth.type"] == "oauth2"
    assert cfg["iceberg.catalog.token"] == "env:UC_TOKEN"
    assert "iceberg.catalog.credential" not in cfg
    assert "iceberg.catalog.scope" not in cfg


def test_kafka_connect_without_catalog_auth_is_unchanged():
    with_none = emit_iceberg_sink_config(_LAKEKEEPER, product_id="p", topics=["t"])
    assert not any(
        k.endswith((".credential", ".token", ".rest.auth.type", ".scope")) for k in with_none
    )


def test_debezium_properties_are_bare():
    cfg = emit_debezium_iceberg_sink_config(_LAKEKEEPER, catalog_auth=_auth(_OAUTH2))
    assert cfg["rest.auth.type"] == "oauth2"
    assert cfg["credential"] == "env:LAKEKEEPER_CREDENTIAL"
    assert cfg["oauth2-server-uri"] == _KEYCLOAK
    assert cfg["scope"] == "lakekeeper"
    assert not any(k.startswith(("iceberg.", "debezium.")) for k in cfg)
    bearer = emit_debezium_iceberg_sink_config(_LAKEKEEPER, catalog_auth=_auth(_BEARER))
    assert bearer["token"] == "env:UC_TOKEN"


def test_emitters_are_deterministic_with_auth():
    auth = _auth({**_OAUTH2, "audience": "lakekeeper"})
    assert emit_iceberg_sink_config(
        _LAKEKEEPER, product_id="p", topics=["t"], catalog_auth=auth
    ) == emit_iceberg_sink_config(_LAKEKEEPER, product_id="p", topics=["t"], catalog_auth=auth)
    assert emit_debezium_iceberg_sink_config(
        _LAKEKEEPER, catalog_auth=auth
    ) == emit_debezium_iceberg_sink_config(_LAKEKEEPER, catalog_auth=auth)


# ── Kafka Connect runner ────────────────────────────────────────────────────


def _kc_contract(catalog_auth: Dict[str, Any], **kc: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.kc_auth",
        "name": "X",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {"host": "db", "database": "x", "user": "u"},
                        "mode": "incremental_append",
                        "streams": ["public.orders"],
                    },
                    "sink": {"format": "iceberg", "catalogAuth": catalog_auth},
                    "kafka-connect": {
                        "deployment": {"server_url": "http://kafka-connect.test:8083"},
                        "connector_name": "src",
                        **kc,
                    },
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "iceberg",
                    "location": {
                        "catalog": "lakekeeper",
                        "uri": "http://lakekeeper:8181/catalog",
                        "warehouse": "demo",
                        "database": "bronze",
                        "table": "orders",
                    },
                },
                "contract": {"schema": []},
            }
        ],
    }


def test_kafka_connect_runner_pushes_env_references(
    kafka_connect_mock, tmp_path: Path, monkeypatch, caplog
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    # Set or not, the runner never reads the worker's variable: the config
    # carries the reference only.
    monkeypatch.setenv("LAKEKEEPER_CREDENTIAL", _SECRET_VALUE)
    contract = _kc_contract(dict(_OAUTH2))
    with caplog.at_level(logging.DEBUG):
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path)
    assert rc == 0
    cfg = kafka_connect_mock.connectors["src-sink"]["config"]
    assert cfg["iceberg.catalog.credential"] == "env:LAKEKEEPER_CREDENTIAL"
    assert cfg["iceberg.catalog.rest.auth.type"] == "oauth2"
    assert cfg["iceberg.catalog.oauth2-server-uri"] == _KEYCLOAK
    assert cfg["iceberg.catalog.scope"] == "lakekeeper"
    assert _SECRET_VALUE not in repr(kafka_connect_mock.connectors)
    assert _SECRET_VALUE not in caplog.text


def test_kafka_connect_runner_needs_no_variable_in_its_own_environment(
    kafka_connect_mock, tmp_path: Path, monkeypatch
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    monkeypatch.delenv("UC_TOKEN", raising=False)
    contract = _kc_contract(dict(_BEARER))
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    cfg = kafka_connect_mock.connectors["src-sink"]["config"]
    assert cfg["iceberg.catalog.token"] == "env:UC_TOKEN"


def test_kafka_connect_runner_refuses_an_override_that_sets_auth(
    kafka_connect_mock, tmp_path: Path
):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _kc_contract(
        dict(_OAUTH2), iceberg_catalog_overrides={"iceberg.catalog.token": "env:OTHER"}
    )
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 1
    assert kafka_connect_mock.calls == []


# ── embedded Debezium Server runner ─────────────────────────────────────────


def _dbz_contract(catalog_auth: Dict[str, Any], server_sink: Any = None) -> Dict[str, Any]:
    contract = _kc_contract(catalog_auth)
    contract["id"] = "bronze.dbz_auth"
    build = contract["builds"][0]
    build["engine"] = "debezium"
    props = build["properties"]
    del props["kafka-connect"]
    props["source"] = {
        "kind": "postgres",
        "connection": {"host": "db", "port": 5432, "database": "x", "user": "u"},
        "mode": "cdc",
        "streams": ["public.orders"],
    }
    props["debezium"] = {
        "deployment": {"mode": "embedded"},
        "server": {"sink": server_sink or {"type": "iceberg"}},
    }
    return contract


def _props_path(contract: Dict[str, Any], tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"


class _RecordingRun:
    """Stands in for ``subprocess.run`` of the Debezium Server binary."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def fake_server(monkeypatch):
    from fluid_build.build_runners.debezium import runner as dbz_runner

    recorder = _RecordingRun()
    monkeypatch.setattr(dbz_runner, "resolve_server_binary", lambda _v: "/opt/dbz/run.sh")
    monkeypatch.setattr(dbz_runner.subprocess, "run", recorder)
    return recorder


def test_embedded_writes_env_references_and_the_server_inherits_the_environment(
    tmp_path: Path, monkeypatch, fake_server, caplog
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv("LAKEKEEPER_CREDENTIAL", _SECRET_VALUE)
    contract = _dbz_contract(dict(_OAUTH2))
    with caplog.at_level(logging.DEBUG):
        rc = execute_debezium_build(contract["builds"][0], contract, tmp_path)
    assert rc == 0
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.rest.auth.type=oauth2" in text
    assert "debezium.sink.iceberg.credential=env:LAKEKEEPER_CREDENTIAL" in text
    assert f"debezium.sink.iceberg.oauth2-server-uri={_KEYCLOAK}" in text
    assert "debezium.sink.iceberg.scope=lakekeeper" in text
    assert _SECRET_VALUE not in text
    assert _SECRET_VALUE not in caplog.text
    # No ``env=``: the server process inherits this one's environment, where
    # Iceberg resolves env:LAKEKEEPER_CREDENTIAL.
    assert len(fake_server.calls) == 1
    assert "env" not in fake_server.calls[0]


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_embedded_preflight_refuses_an_unset_variable(
    tmp_path: Path, monkeypatch, fake_server, value
):
    from fluid_build.api.runner import RunState
    from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
    from fluid_build.build_runners.debezium.runner import DebeziumRunner

    if value is None:
        monkeypatch.delenv("UC_TOKEN", raising=False)
    else:
        monkeypatch.setenv("UC_TOKEN", value)
    contract = _dbz_contract(dict(_BEARER))
    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    result = DebeziumRunner().run(ctx)
    assert result.state == RunState.FAILED
    assert "iceberg sink preflight failed:" in (result.error or "")
    assert "sink.catalogAuth.tokenEnv names UC_TOKEN" in result.error
    assert "unset or empty" in result.error
    assert not _props_path(contract, tmp_path).exists()
    assert fake_server.calls == []


def test_embedded_preflight_never_reports_the_value(tmp_path: Path, monkeypatch, fake_server):
    """A set variable passes; its value appears nowhere in the run result."""
    from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
    from fluid_build.build_runners.debezium.runner import DebeziumRunner

    monkeypatch.setenv("UC_TOKEN", "eyJhbGciOiJIUzI1NiJ9.e30.sig")  # pragma: allowlist secret
    contract = _dbz_contract(dict(_BEARER))
    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    result = DebeziumRunner().run(ctx)
    assert result.error is None
    assert "eyJhbGciOiJIUzI1NiJ9" not in repr(result)
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.token=env:UC_TOKEN" in text


def test_embedded_handwritten_config_with_catalog_auth_is_refused(
    tmp_path: Path, monkeypatch, fake_server
):
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    monkeypatch.setenv("LAKEKEEPER_CREDENTIAL", _SECRET_VALUE)
    contract = _dbz_contract(
        dict(_OAUTH2), server_sink={"type": "iceberg", "config": {"type": "rest"}}
    )
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 1
    assert not _props_path(contract, tmp_path).exists()
    assert fake_server.calls == []
