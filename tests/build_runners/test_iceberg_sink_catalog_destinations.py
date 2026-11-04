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

"""Where a ``sink.catalogAuth`` credential may go, and what location text may say.

A contract names the variable (``tokenEnv`` / ``credentialEnv``) and every host
it is sent to (``binding.location.uri``, ``oauth2ServerUri``). The Kafka Connect
worker or the embedded Debezium Server resolves ``env:<NAME>`` from its own
environment, which for Debezium is this runner's whole environment, and sends
the value there. So the operator pairs the variable with each host
(``NAME@host``), a hostname needs a host-list entry (the remote JVM resolves it
itself, later), an IP literal keeps the deployment host rules, loopback is
exempt, and the credential travels over https only. Text from
``binding.location`` that reaches the derived sink config may not reference a
variable itself (``env:`` / ``${``) or hold a control character.

Every refusal happens before any Connect REST call, ``application.properties``
write or read of the variable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.build_runners._deployment_host_policy import ALLOWLIST_ENV as HOST_ALLOWLIST
from fluid_build.build_runners._secret_policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)

pytestmark = [pytest.mark.unit]

_SECRET = "OPERATOR_SYNTH_SECRET"  # pragma: allowlist secret - a variable NAME
_SECRET_VALUE = "operator-synth-value-7Hq2"  # pragma: allowlist secret
_CATALOG_HOST = "catalog.attacker.example"
_IDP_HOST = "idp.attacker.example"
_CATALOG = f"https://{_CATALOG_HOST}/catalog"
_IDP = f"https://{_IDP_HOST}/realms/iceberg/protocol/openid-connect/token"

_AUTHS: Dict[str, Dict[str, str]] = {
    "bearer": {"type": "bearer", "tokenEnv": _SECRET},
    "oauth2": {
        "type": "oauth2",
        "credentialEnv": _SECRET,
        "oauth2ServerUri": _IDP,
        "scope": "lakekeeper",
    },
}
#: The property each type's secret reference lands in.
_SECRET_PROPERTY = {"bearer": "token", "oauth2": "credential"}


def _hosts(auth: str) -> List[str]:
    """Every host the ``auth`` credential reaches: the catalog, and an OAuth2 IdP."""
    return [_CATALOG_HOST, _IDP_HOST] if auth == "oauth2" else [_CATALOG_HOST]


@pytest.fixture(autouse=True)
def _operator_environment(monkeypatch):
    """No ``NAME@host`` entry unless a test adds one. The variable is set, so
    the embedded Debezium preflight's own "set?" check passes."""
    monkeypatch.delenv(SECRET_REF_ALLOWLIST, raising=False)
    monkeypatch.setenv(_SECRET, _SECRET_VALUE)


def _operator_allows(monkeypatch, *, pairs=(), hosts=()) -> None:
    """Set the operator's lists. Host entries are ADDED: a fixture may list its
    own synthetic Connect host there."""
    if pairs:
        monkeypatch.setenv(SECRET_REF_ALLOWLIST, ",".join(pairs))
    if hosts:
        current = [h for h in os.environ.get(HOST_ALLOWLIST, "").split(",") if h.strip()]
        monkeypatch.setenv(HOST_ALLOWLIST, ",".join([*current, *hosts]))


def _allow_everything(monkeypatch, auth: str) -> None:
    hosts = _hosts(auth)
    _operator_allows(monkeypatch, pairs=[f"{_SECRET}@{h}" for h in hosts], hosts=hosts)


def _location(uri: str = _CATALOG, **extra: Any) -> Dict[str, Any]:
    return {
        "catalog": "lakekeeper",
        "uri": uri,
        "warehouse": "demo",
        "database": "bronze",
        "table": "orders",
        **extra,
    }


def _kc_contract(
    catalog_auth: Optional[Dict[str, Any]] = None,
    *,
    location: Optional[Dict[str, Any]] = None,
    **kc: Any,
) -> Dict[str, Any]:
    sink: Dict[str, Any] = {"format": "iceberg"}
    if catalog_auth is not None:
        sink["catalogAuth"] = catalog_auth
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "bronze.sink_destinations",
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
                    "location": dict(location if location is not None else _location()),
                },
                "contract": {"schema": []},
            }
        ],
    }


def _dbz_contract(
    catalog_auth: Optional[Dict[str, Any]] = None,
    *,
    location: Optional[Dict[str, Any]] = None,
    server_sink: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    contract = _kc_contract(catalog_auth, location=location)
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


class _Engine:
    """One streaming runner: builds its contract, runs it, reads what it delivered."""

    def __init__(self, name: str, kc_mock: Any, server: _RecordingRun, tmp_path: Path) -> None:
        self.name = name
        self._kc_mock = kc_mock
        self._server = server
        self._tmp_path = tmp_path

    def contract(self, catalog_auth=None, **kwargs: Any) -> Dict[str, Any]:
        if self.name == "kafka-connect":
            return _kc_contract(catalog_auth, **kwargs)
        return _dbz_contract(catalog_auth, **kwargs)

    def run(self, contract: Dict[str, Any]) -> int:
        if self.name == "kafka-connect":
            from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

            return execute_kafka_connect_build(contract["builds"][0], contract, self._tmp_path)
        from fluid_build.build_runners.debezium.runner import execute_debezium_build

        return execute_debezium_build(contract["builds"][0], contract, self._tmp_path)

    def _properties(self, contract: Dict[str, Any]) -> Path:
        return (
            self._tmp_path
            / ".fluid"
            / "debezium"
            / contract["id"]
            / "ingest"
            / "application.properties"
        )

    def touched(self, contract: Dict[str, Any]) -> bool:
        """Did the run reach Connect, write application.properties or start the server?"""
        if self.name == "kafka-connect":
            return bool(self._kc_mock.calls)
        return self._properties(contract).exists() or bool(self._server.calls)

    def delivered(self, contract: Dict[str, Any]) -> Dict[str, str]:
        """The catalog properties the run pushed or wrote, as bare keys."""
        if self.name == "kafka-connect":
            cfg = self._kc_mock.connectors["src-sink"]["config"]
            prefix = "iceberg.catalog."
        else:
            lines = self._properties(contract).read_text().splitlines()
            cfg = dict(line.split("=", 1) for line in lines if "=" in line)
            prefix = "debezium.sink.iceberg."
        return {k[len(prefix) :]: v for k, v in cfg.items() if k.startswith(prefix)}


@pytest.fixture(params=["kafka-connect", "debezium"])
def engine(request, kafka_connect_mock, fake_server, tmp_path) -> _Engine:
    return _Engine(request.param, kafka_connect_mock, fake_server, tmp_path)


# ── the confirmed defect: a contract-chosen variable to a contract-chosen host ──


@pytest.mark.parametrize("auth", sorted(_AUTHS))
def test_an_unpaired_variable_is_refused_before_anything_is_sent(engine, auth):
    contract = engine.contract(dict(_AUTHS[auth]))
    assert engine.run(contract) == 1
    assert not engine.touched(contract)


@pytest.mark.parametrize("auth", sorted(_AUTHS))
def test_listed_pairs_and_hosts_reach_the_sink(engine, auth, monkeypatch):
    _allow_everything(monkeypatch, auth)
    contract = engine.contract(dict(_AUTHS[auth]))
    assert engine.run(contract) == 0
    delivered = engine.delivered(contract)
    assert delivered[_SECRET_PROPERTY[auth]] == f"env:{_SECRET}"
    assert delivered["uri"] == _CATALOG


@pytest.mark.parametrize("engine_name", ["kafka-connect", "debezium"])
def test_oauth2_pairs_the_variable_with_the_token_endpoint_too(engine_name, monkeypatch):
    """The IdP receives the credential itself, the catalog a token minted from it."""
    _operator_allows(
        monkeypatch, pairs=[f"{_SECRET}@{_CATALOG_HOST}"], hosts=[_CATALOG_HOST, _IDP_HOST]
    )
    build = _kc_contract if engine_name == "kafka-connect" else _dbz_contract
    error = iceberg_sink_preflight(build(dict(_AUTHS["oauth2"])), "ingest")
    assert error is not None
    assert f'{SECRET_REF_ALLOWLIST}="{_SECRET}@{_CATALOG_HOST},{_SECRET}@{_IDP_HOST}"' in error
    assert "OAuth2 token endpoint" in error


@pytest.mark.parametrize("auth", sorted(_AUTHS))
def test_a_paired_hostname_still_needs_a_host_entry(auth, monkeypatch):
    """The remote JVM resolves the name itself, later: only the operator can vouch for it."""
    _operator_allows(monkeypatch, pairs=[f"{_SECRET}@{h}" for h in _hosts(auth)])
    error = iceberg_sink_preflight(_kc_contract(dict(_AUTHS[auth])), "ingest")
    assert error is not None
    assert f'{HOST_ALLOWLIST}="' in error
    assert _CATALOG_HOST in error


def test_a_bare_name_entry_does_not_open_every_host(monkeypatch):
    """``NAME`` (any host) still leaves the destination to the host list."""
    _operator_allows(monkeypatch, pairs=[_SECRET])
    assert iceberg_sink_preflight(_kc_contract(dict(_AUTHS["bearer"])), "ingest") is not None
    _operator_allows(monkeypatch, hosts=[_CATALOG_HOST])
    assert iceberg_sink_preflight(_kc_contract(dict(_AUTHS["bearer"])), "ingest") is None


def test_a_parent_domain_entry_covers_the_catalog_host(monkeypatch):
    _operator_allows(monkeypatch, pairs=[f"{_SECRET}@{_CATALOG_HOST}"], hosts=["attacker.example"])
    assert iceberg_sink_preflight(_kc_contract(dict(_AUTHS["bearer"])), "ingest") is None


@pytest.mark.parametrize("auth", sorted(_AUTHS))
def test_a_loopback_catalog_needs_no_entry(engine, auth):
    raw = dict(_AUTHS[auth])
    if auth == "oauth2":
        raw["oauth2ServerUri"] = (
            "https://127.0.0.1:8443/realms/iceberg/protocol/openid-connect/token"
        )
    contract = engine.contract(raw, location=_location("https://localhost:8443/catalog"))
    assert engine.run(contract) == 0
    assert engine.delivered(contract)[_SECRET_PROPERTY[auth]] == f"env:{_SECRET}"


def test_validate_warns_where_the_run_refuses():
    """``fluid validate`` runs where the operator's lists may not be; the run is where."""
    contract = _kc_contract(dict(_AUTHS["bearer"]))
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert any(f"{_SECRET}@{_CATALOG_HOST}" in w for w in warnings)
    error = iceberg_sink_preflight(contract, "ingest")
    assert error is not None
    assert error.startswith("iceberg sink preflight failed: ")  # run-only, no "see validate"
    # One export line sets both, ready to paste.
    assert (
        f'export {SECRET_REF_ALLOWLIST}="{_SECRET}@{_CATALOG_HOST}" ' f'{HOST_ALLOWLIST}="' in error
    )
    assert _SECRET_VALUE not in error


def test_the_suggestion_keeps_the_operators_entries(monkeypatch):
    _operator_allows(monkeypatch, pairs=["OTHER@db.example.com"], hosts=["db.example.com"])
    error = iceberg_sink_preflight(_kc_contract(dict(_AUTHS["bearer"])), "ingest")
    assert error is not None
    assert f'{SECRET_REF_ALLOWLIST}="OTHER@db.example.com,{_SECRET}@{_CATALOG_HOST}"' in error


def test_a_refused_variable_is_never_read(monkeypatch, fake_server, tmp_path):
    """The embedded preflight reads the variable to test it is set; not when refused."""
    from fluid_build.build_runners.debezium.runner import execute_debezium_build

    registered: List[Any] = []
    monkeypatch.setattr(
        "fluid_build.observability.secret_redactor.register_secret", registered.append
    )
    contract = _dbz_contract(dict(_AUTHS["bearer"]))
    assert execute_debezium_build(contract["builds"][0], contract, tmp_path) == 1
    assert registered == []


def test_the_pairing_follows_an_override_uri(monkeypatch):
    """The credential goes to the uri that reaches the sink, not to the binding's."""
    _allow_everything(monkeypatch, "bearer")
    kc = _kc_contract(
        dict(_AUTHS["bearer"]),
        iceberg_catalog_overrides={"iceberg.catalog.uri": "https://other.attacker.example/c"},
    )
    error = iceberg_sink_preflight(kc, "ingest")
    assert error is not None and "other.attacker.example" in error
    dbz = _dbz_contract(
        dict(_AUTHS["bearer"]),
        server_sink={
            "type": "iceberg",
            "iceberg_sink_enabled": True,
            "config": {"uri": "https://other.attacker.example/c"},
        },
    )
    error = iceberg_sink_preflight(dbz, "ingest")
    assert error is not None and "other.attacker.example" in error


# ── https ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "auth, field",
    [("bearer", "uri"), ("oauth2", "uri"), ("oauth2", "oauth2ServerUri")],
)
@pytest.mark.parametrize("engine_name", ["kafka-connect", "debezium"])
def test_a_credential_travels_over_https_only(auth, field, engine_name):
    """Loopback included, as on the managed Flink lane: refused by ``fluid validate``."""
    raw = dict(_AUTHS[auth])
    location = _location("https://localhost:8443/catalog")
    if field == "uri":
        location["uri"] = "http://localhost:8181/catalog"
    else:
        raw["oauth2ServerUri"] = "http://127.0.0.1:8080/realms/x/protocol/openid-connect/token"
    build = _kc_contract if engine_name == "kafka-connect" else _dbz_contract
    errors, _warnings = validate_iceberg_sink(build(raw, location=location))
    label = "sink.catalogAuth.oauth2ServerUri" if field != "uri" else "binding.location.uri"
    assert any(label in e and "https://" in e for e in errors), errors


def test_an_unauthenticated_http_catalog_is_unchanged():
    contract = _kc_contract(None, location=_location("http://lakekeeper:8181/catalog"))
    assert validate_iceberg_sink(contract) == ([], [])
    assert iceberg_sink_preflight(contract, "ingest") is None


# ── IP literals keep the deployment host rules ─────────────────────────────


@pytest.mark.parametrize(
    "host", ["169.254.169.254", "[fe80::1]", "0.0.0.0", "[::ffff:169.254.169.254]"]
)
def test_a_link_local_catalog_is_refused_even_when_listed(host, monkeypatch):
    bare = host.strip("[]")
    _operator_allows(monkeypatch, pairs=[_SECRET, f"{_SECRET}@{bare}"], hosts=[bare])
    contract = _kc_contract(dict(_AUTHS["bearer"]), location=_location(f"https://{host}/catalog"))
    errors, _warnings = validate_iceberg_sink(contract)
    assert any("link-local" in e for e in errors), errors


def test_a_public_ip_needs_a_host_entry(monkeypatch):
    contract = _kc_contract(
        dict(_AUTHS["bearer"]), location=_location("https://93.184.216.34/catalog")
    )
    _operator_allows(monkeypatch, pairs=[f"{_SECRET}@93.184.216.34"])
    error = iceberg_sink_preflight(contract, "ingest")
    assert error is not None and f'{HOST_ALLOWLIST}="' in error
    _operator_allows(monkeypatch, hosts=["93.184.216.34"])
    assert iceberg_sink_preflight(contract, "ingest") is None


def test_a_private_ip_needs_only_the_pairing(monkeypatch):
    contract = _kc_contract(dict(_AUTHS["bearer"]), location=_location("https://10.0.0.5/catalog"))
    assert iceberg_sink_preflight(contract, "ingest") is not None
    _operator_allows(monkeypatch, pairs=[f"{_SECRET}@10.0.0.5"])
    assert iceberg_sink_preflight(contract, "ingest") is None


# ── binding.location text in the derived sink config ───────────────────────


_REFERENCE_TEXT = [
    ("region", f"env:{_SECRET}"),
    ("warehouse", f"env:{_SECRET}"),
    ("uri", f"env:{_SECRET}"),
    ("warehouse", f"${{{_SECRET}}}"),
    ("uri", f"https://${{{_SECRET}}}.attacker.example/catalog"),
    ("database", f"${{env:{_SECRET}}}"),
    ("warehouse", "demo\nx"),
]


@pytest.mark.parametrize("field, value", _REFERENCE_TEXT)
def test_location_text_may_not_reference_a_variable(engine, field, value):
    contract = engine.contract(None, location=_location(**{field: value}))
    errors, _warnings = validate_iceberg_sink(contract)
    mine = [e for e in errors if f"binding.location.{field}" in e and "env:" in e]
    assert mine, errors
    assert not any(_SECRET in e for e in mine)  # the value is not echoed
    assert engine.run(contract) == 1
    assert not engine.touched(contract)


def test_kafka_connect_table_and_partition_text_is_checked_too():
    contract = _kc_contract(None, location=_location(table=f"${{env:{_SECRET}}}"))
    assert any("binding.location.table" in e for e in validate_iceberg_sink(contract)[0])
    contract = _kc_contract(None, location=_location(partitionBy=[f"env:{_SECRET}"]))
    assert any("binding.location.partitionBy" in e for e in validate_iceberg_sink(contract)[0])


def test_bigquery_catalog_properties_are_checked():
    location = {
        "catalog": "bigquery",
        "project": f"env:{_SECRET}",
        "dataset": "bronze",
        "table": "orders",
        "bucket": "lake",
    }
    contract = _kc_contract(None, location=location)
    contract["exposes"][0]["binding"]["platform"] = "gcp"
    errors, _warnings = validate_iceberg_sink(contract)
    assert any("binding.location.project" in e for e in errors), errors


@pytest.mark.parametrize(
    "region, accepted",
    [
        ("eu-central-1", True),
        ("us-gov-west-1", True),
        ("westeurope", True),
        ("EU", True),  # a host label ignores case
        ("attacker.example", False),
        ("us-east-1/", False),
        ("us-east-1#", False),
        ("-eu", False),
        ("a" * 64, False),
    ],
)
@pytest.mark.parametrize("engine_name", ["kafka-connect", "debezium"])
def test_client_region_is_a_host_label(region, accepted, engine_name):
    """``client.region`` becomes part of the S3 and Glue endpoint hosts."""
    build = _kc_contract if engine_name == "kafka-connect" else _dbz_contract
    errors, _warnings = validate_iceberg_sink(build(None, location=_location(region=region)))
    refused = any("binding.location.region" in e for e in errors)
    assert refused is not accepted, errors


def _rendered(contract: Dict[str, Any]) -> Dict[str, Any]:
    """The contract as the acquisition dispatcher hands it to a runner."""
    from fluid_build.build_runners.base import _resolve_env_placeholders

    return _resolve_env_placeholders(contract)


@pytest.mark.parametrize(
    "rendered_to, refused",
    [("eu-central-1", False), ("attacker.example", True)],
)
def test_a_templated_region_is_checked_where_it_is_rendered(monkeypatch, rendered_to, refused):
    """``fluid validate`` sees the template; the run's preflight sees the value."""
    contract = _kc_contract(None, location=_location(region="{{ env.LAKE_REGION }}"))
    assert not any("binding.location.region" in e for e in validate_iceberg_sink(contract)[0])
    monkeypatch.setenv("LAKE_REGION", rendered_to)
    error = iceberg_sink_preflight(_rendered(contract), "ingest")
    assert (error is not None and "binding.location.region" in error) is refused


def test_a_templated_catalog_uri_is_checked_where_it_is_rendered(monkeypatch):
    contract = _kc_contract(dict(_AUTHS["bearer"]), location=_location("{{ env.CATALOG_URI }}"))
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert not any(_SECRET in w for w in warnings)
    monkeypatch.setenv("CATALOG_URI", _CATALOG)
    error = iceberg_sink_preflight(_rendered(contract), "ingest")
    assert error is not None and f"{_SECRET}@{_CATALOG_HOST}" in error
    _allow_everything(monkeypatch, "bearer")
    assert iceberg_sink_preflight(_rendered(contract), "ingest") is None


def test_a_handwritten_sink_config_reads_no_location_text():
    """Nothing is derived, so the binding's text never reaches the sink config."""
    contract = _kc_contract(
        None,
        location=_location(region="attacker.example"),
        sink_connector_config={"connector.class": "x"},
    )
    assert not any("binding.location.region" in e for e in validate_iceberg_sink(contract)[0])


# ── a host the refusal can repeat safely ───────────────────────────────────


@pytest.mark.parametrize(
    "uri",
    [
        'https://x"$(id)".attacker.example/catalog',
        "https://x,OTHER_VAR.attacker.example/catalog",
        "https://faß.example/catalog",
        "https://[fd00::1%25eth0]/catalog",
    ],
    ids=["shell", "comma", "non-ascii", "zone-id"],
)
def test_a_host_that_is_not_plain_is_refused_and_never_echoed(uri, monkeypatch):
    """The refusal's export line repeats the host, so it must be safe to paste."""
    _operator_allows(monkeypatch, pairs=[_SECRET], hosts=["attacker.example", "example"])
    contract = _kc_contract(dict(_AUTHS["bearer"]), location=_location(uri))
    errors, _warnings = validate_iceberg_sink(contract)
    mine = [e for e in errors if "not a plain DNS name or IP address" in e]
    assert mine, errors
    error = iceberg_sink_preflight(contract, "ingest")
    assert error is not None
    for text in (*mine, error):
        assert "export" not in text
        assert "$(id)" not in text and "other_var" not in text.lower()
        assert "faß" not in text and "eth0" not in text


@pytest.mark.parametrize(
    "uri",
    ["https://evil.example\\@127.0.0.1/catalog", "https://u:p4ss@catalog.attacker.example/c"],
    ids=["backslash", "userinfo"],
)
def test_a_user_password_part_is_refused(uri):
    """Python and the JVM read such an authority differently; it never reaches a check."""
    contract = _kc_contract(dict(_AUTHS["bearer"]), location=_location(uri))
    errors, _warnings = validate_iceberg_sink(contract)
    mine = [e for e in errors if "user:password@ part" in e]
    assert mine, errors
    assert not any("p4ss" in e for e in mine)


def test_an_engine_that_derives_no_sink_config_skips_the_location_text():
    """Only the Kafka Connect and Debezium runners derive the config from the binding."""
    contract = _kc_contract(None, location=_location(warehouse=f"env:{_SECRET}"))
    contract["builds"][0]["engine"] = "airbyte"
    errors, _warnings = validate_iceberg_sink(contract)
    assert not any("binding.location.warehouse" in e for e in errors), errors


def test_a_rendered_region_is_not_echoed(monkeypatch):
    """At run a region may hold whatever a {{ env.* }} template rendered."""
    contract = _kc_contract(None, location=_location(region="{{ env.LAKE_REGION }}"))
    monkeypatch.setenv("LAKE_REGION", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    error = iceberg_sink_preflight(_rendered(contract), "ingest")
    assert error is not None and "binding.location.region" in error
    assert "wJalrXUtnFEMI" not in error


def test_validate_defers_a_template_only_where_it_decides_the_host():
    """A template in the path leaves the scheme and host to check now."""
    raw = dict(_AUTHS["bearer"])
    contract = _kc_contract(raw, location=_location("http://169.254.169.254/{{ env.X }}"))
    errors, _warnings = validate_iceberg_sink(contract)
    assert any("must be an https:// URL" in e for e in errors), errors


def test_a_catalog_not_reached_over_rest_never_reads_the_variable(monkeypatch):
    """catalogAuth on Glue is already refused; the embedded preflight reads nothing."""
    registered: List[Any] = []
    monkeypatch.setattr(
        "fluid_build.observability.secret_redactor.register_secret", registered.append
    )
    location = {"database": "bronze", "table": "orders", "bucket": "lake", "region": "us-east-1"}
    contract = _dbz_contract(dict(_AUTHS["bearer"]), location=location)
    contract["exposes"][0]["binding"]["platform"] = "aws"
    error = iceberg_sink_preflight(contract, "ingest")
    assert error is not None and "not reached over REST" in error
    assert registered == []
