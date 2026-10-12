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

"""``fluid validate`` and the runner preflight on ``sink.catalogAuth``.

Hard errors: a malformed catalogAuth, one on a catalog not reached over REST,
one on a hand-written (non-deriving) sink config, one on a build no runner
derives an Iceberg sink for, and one next to an override map that sets an auth
property. A warning: an override or hand-written map that inlines a literal
``credential`` / ``token``. No message quotes a secret.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import catalog_kind_info, known_catalog_kinds

pytestmark = [pytest.mark.unit]

_KEYCLOAK = "http://keycloak:8080/realms/iceberg/protocol/openid-connect/token"
_OAUTH2 = {
    "type": "oauth2",
    "credentialEnv": "LAKEKEEPER_CREDENTIAL",
    "oauth2ServerUri": _KEYCLOAK,
    "scope": "lakekeeper",
}
_LAKEKEEPER_LOCATION = {
    "catalog": "lakekeeper",
    "uri": "http://lakekeeper:8181/catalog",
    "warehouse": "demo",
    "database": "bronze",
    "table": "orders",
}
_GLUE_LOCATION = {"database": "bronze", "table": "orders", "bucket": "lake", "region": "us-east-1"}


def _kc(
    catalog_auth: Optional[Dict[str, Any]] = None,
    *,
    kc: Optional[Dict[str, Any]] = None,
    location: Optional[Dict[str, Any]] = None,
    platform: str = "local",
    sink_format: str = "iceberg",
    engine: str = "kafka-connect",
) -> Dict[str, Any]:
    sink: Dict[str, Any] = {"format": sink_format}
    if catalog_auth is not None:
        sink["catalogAuth"] = catalog_auth
    return {
        "id": "b.auth",
        "builds": [
            {
                "id": "ingest",
                "engine": engine,
                "properties": {
                    "source": {"kind": "postgres"},
                    "sink": sink,
                    "kafka-connect": kc or {},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": platform,
                    "format": "iceberg",
                    "location": dict(location or _LAKEKEEPER_LOCATION),
                },
            }
        ],
    }


def _dbz(catalog_auth: Optional[Dict[str, Any]], *, server_sink=None, mode="embedded"):
    contract = _kc(catalog_auth)
    build = contract["builds"][0]
    build["engine"] = "debezium"
    props = build["properties"]
    del props["kafka-connect"]
    props["debezium"] = {
        "deployment": {"mode": mode},
        "server": {"sink": server_sink if server_sink is not None else {"type": "iceberg"}},
    }
    return contract


# ── accepted ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "catalog",
    sorted(k for k in known_catalog_kinds() if catalog_kind_info(k).speaks_rest),
)
def test_every_rest_kind_accepts_catalog_auth(catalog):
    location = {**_LAKEKEEPER_LOCATION, "catalog": catalog}
    if catalog != "lakekeeper":
        location["uri"] = "http://catalog:8181/api/catalog"
    errors, _warnings = validate_iceberg_sink(_kc(dict(_OAUTH2), location=location))
    assert errors == []


def test_derived_kafka_connect_and_embedded_debezium_accept_it():
    assert validate_iceberg_sink(_kc(dict(_OAUTH2))) == ([], [])
    assert validate_iceberg_sink(_dbz({"type": "bearer", "tokenEnv": "UC_TOKEN"})) == ([], [])


def test_validate_does_not_need_the_variable_set(monkeypatch):
    """The variable lives where the sink runs, not where `fluid validate` runs."""
    monkeypatch.delenv("UC_TOKEN", raising=False)
    assert validate_iceberg_sink(_dbz({"type": "bearer", "tokenEnv": "UC_TOKEN"}))[0] == []


# ── refused ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "location, platform, loads",
    [
        (_GLUE_LOCATION, "aws", "glue"),
        (
            {
                "catalog": "nessie",
                "uri": "http://nessie:19120/api/v2",
                "warehouse": "s3://w/",
                "database": "bronze",
                "table": "orders",
            },
            "local",
            "nessie",
        ),
    ],
    ids=["glue", "nessie"],
)
def test_a_catalog_not_reached_over_rest_is_refused(location, platform, loads):
    errors = validate_iceberg_sink(_kc(dict(_OAUTH2), location=location, platform=platform))[0]
    assert any(
        "sink.catalogAuth configures an Iceberg REST catalog's auth" in e
        and f"(the sink builds Iceberg's {loads} catalog)" in e
        for e in errors
    ), errors


def test_sink_catalog_kind_is_the_one_checked():
    """``sink.catalog`` picks the kind the sink writes through, so it is the
    kind catalogAuth is checked against (the disagreement is refused too)."""
    contract = _kc(dict(_OAUTH2), location=_GLUE_LOCATION, platform="aws")
    contract["builds"][0]["properties"]["sink"]["catalog"] = "rest"
    errors = validate_iceberg_sink(contract)[0]
    assert not any("not reached over REST" in e for e in errors)


@pytest.mark.parametrize(
    "contract, where",
    [
        (
            _kc(dict(_OAUTH2), kc={"sink_connector_config": {"iceberg.tables": "bronze.orders"}}),
            "properties.kafka-connect.sink_connector_config",
        ),
        (
            _dbz(dict(_OAUTH2), server_sink={"type": "iceberg", "config": {"type": "rest"}}),
            "properties.debezium.server.sink.config",
        ),
    ],
    ids=["kafka-connect", "debezium"],
)
def test_a_handwritten_sink_config_is_refused(contract, where):
    errors = validate_iceberg_sink(contract)[0]
    hit = [e for e in errors if "applies to a sink config forge-cli derives" in e]
    assert len(hit) == 1, errors
    assert where in hit[0]
    # It tells the user which keys to set in their own config.
    prefix = "iceberg.catalog." if "kafka-connect" in where else ""
    assert f"{prefix}credential to env:LAKEKEEPER_CREDENTIAL" in hit[0]
    assert f"{prefix}rest.auth.type to oauth2" in hit[0]
    # A contract value is named by its field, not echoed (it could hold a secret).
    assert f"{prefix}oauth2-server-uri to <oauth2ServerUri>" in hit[0]
    assert "keycloak:8080" not in hit[0]


def test_a_handwritten_config_with_a_userinfo_uri_is_refused_without_echoing_it():
    secret = "2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret
    auth = {**_OAUTH2, "oauth2ServerUri": f"https://spark:{secret}@keycloak/realms/x/token"}
    contract = _kc(auth, kc={"sink_connector_config": {"iceberg.tables": "bronze.orders"}})
    errors = validate_iceberg_sink(contract)[0]
    assert any("no user:password@ part" in e for e in errors), errors
    assert not any(secret in e for e in errors), errors


def test_opting_back_into_derivation_accepts_it():
    kc = {"iceberg_sink_enabled": True, "sink_connector_config": {"tasks.max": "1"}}
    assert validate_iceberg_sink(_kc(dict(_OAUTH2), kc=kc))[0] == []


@pytest.mark.parametrize(
    "contract",
    [
        _kc(dict(_OAUTH2), sink_format="parquet"),
        _dbz(dict(_OAUTH2), mode="bring-your-own"),
    ],
    ids=["kafka-connect-parquet", "debezium-bring-your-own"],
)
def test_a_build_with_no_derived_iceberg_sink_is_refused(contract):
    errors = validate_iceberg_sink(contract)[0]
    assert any("sink.catalogAuth has no effect" in e for e in errors), errors


def test_an_engine_that_derives_no_sink_config_is_refused():
    errors = validate_iceberg_sink(_kc(dict(_OAUTH2), engine="duckdb"))[0]
    assert any("has no effect on engine 'duckdb'" in e for e in errors), errors


@pytest.mark.parametrize(
    "key",
    [
        "iceberg.catalog.rest.auth.type",
        "iceberg.catalog.credential",
        "iceberg.catalog.token",
        "iceberg.catalog.oauth2-server-uri",
        "iceberg.catalog.scope",
    ],
)
@pytest.mark.parametrize("label", ["iceberg_catalog_overrides", "sink_connector_config"])
def test_kafka_connect_override_of_an_auth_property_is_refused(key, label):
    kc: Dict[str, Any] = {label: {key: "env:OTHER"}}
    if label == "sink_connector_config":
        kc["iceberg_sink_enabled"] = True
    errors = validate_iceberg_sink(_kc(dict(_OAUTH2), kc=kc))[0]
    assert any(
        f"sink.catalogAuth and {label} both set the catalog's auth ({key})" in e for e in errors
    ), errors


@pytest.mark.parametrize(
    "key", ["rest.auth.type", "credential", "token", "oauth2-server-uri", "scope"]
)
def test_debezium_override_of_an_auth_property_is_refused(key):
    server_sink = {"type": "iceberg", "iceberg_sink_enabled": True, "config": {key: "x"}}
    errors = validate_iceberg_sink(_dbz(dict(_OAUTH2), server_sink=server_sink))[0]
    assert any(
        f"sink.catalogAuth and debezium.server.sink.config both set the catalog's auth ({key})" in e
        for e in errors
    ), errors


def test_an_unrelated_override_key_is_not_a_conflict():
    kc = {
        "iceberg_catalog_overrides": {
            "iceberg.catalog.uri": "http://c:8181/catalog",
            "s3.region": "y",
        }
    }
    assert validate_iceberg_sink(_kc(dict(_OAUTH2), kc=kc))[0] == []


def test_a_pasted_secret_is_refused_without_being_quoted():
    secret = "spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret
    errors = validate_iceberg_sink(_kc({**_OAUTH2, "credentialEnv": secret}))[0]
    assert any("credentialEnv must NAME an environment variable" in e for e in errors), errors
    assert not any("2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52" in e for e in errors)


# ── inlined-secret warning ──────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["iceberg.catalog.credential", "iceberg.catalog.token"])
def test_a_literal_secret_in_an_override_warns(key):
    contract = _kc(kc={"iceberg_catalog_overrides": {key: "spark:s3cr3t-value"}})
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    hit = [w for w in warnings if f"sets {key} to a literal value" in w]
    assert len(hit) == 1, warnings
    assert "s3cr3t-value" not in hit[0]
    assert "pushes to the Kafka Connect REST API" in hit[0]


def test_a_literal_secret_in_a_handwritten_config_warns():
    contract = _dbz(None, server_sink={"type": "iceberg", "config": {"credential": "id:sec"}})
    warnings = validate_iceberg_sink(contract)[1]
    assert any(
        "debezium.server.sink.config sets credential to a literal value" in w
        and "writes to application.properties" in w
        and "id:sec" not in w
        for w in warnings
    ), warnings


@pytest.mark.parametrize(
    "value", ["env:LK_CRED", "${env:LK_CRED}", "${env:ID}:${env:SECRET}", "${LK_CRED}", ""]
)
def test_a_reference_is_not_a_literal_secret(value):
    contract = _kc(kc={"iceberg_catalog_overrides": {"iceberg.catalog.credential": value}})
    assert not any("literal value" in w for w in validate_iceberg_sink(contract)[1])


# ── run preflight ───────────────────────────────────────────────────────────


def test_embedded_preflight_refuses_an_unset_variable(monkeypatch):
    monkeypatch.delenv("LAKEKEEPER_CREDENTIAL", raising=False)
    error = iceberg_sink_preflight(_dbz(dict(_OAUTH2)), "ingest")
    assert error is not None
    assert "credentialEnv names LAKEKEEPER_CREDENTIAL" in error
    # Run-only: `fluid validate` does not report it, so no pointer there.
    assert error.startswith("iceberg sink preflight failed: ")


def test_embedded_preflight_passes_a_set_variable(monkeypatch):
    monkeypatch.setenv("LAKEKEEPER_CREDENTIAL", "spark:value")
    assert iceberg_sink_preflight(_dbz(dict(_OAUTH2)), "ingest") is None


def test_kafka_connect_preflight_has_no_variable_check(monkeypatch):
    """The variable lives on the Connect worker, which forge-cli cannot see."""
    monkeypatch.delenv("LAKEKEEPER_CREDENTIAL", raising=False)
    assert iceberg_sink_preflight(_kc(dict(_OAUTH2)), "ingest") is None


# ── names the env-name rule used to let through ─────────────────────────────


def test_embedded_preflight_refuses_a_name_with_a_trailing_newline(monkeypatch):
    """``LK_CRED\n`` used to pass, and the preflight then reported a SET
    variable as unset, while Kafka Connect ran without credentials."""
    monkeypatch.setenv("LK_CRED", "spark:value")
    auth = {**_OAUTH2, "credentialEnv": "LK_CRED\n"}
    for contract in (_kc(dict(auth)), _dbz(dict(auth))):
        error = iceberg_sink_preflight(contract, "ingest")
        assert error is not None
        assert "credentialEnv must NAME an environment variable" in error
        assert "unset or empty" not in error


def test_a_token_pasted_into_token_env_never_reaches_a_message():
    pat = "da" + "pi" + "0123456789abcdef" * 2
    for contract in (
        _kc({"type": "bearer", "tokenEnv": pat}),
        _dbz({"type": "bearer", "tokenEnv": pat}),
    ):
        errors = validate_iceberg_sink(contract)[0]
        assert any("tokenEnv looks like a token" in e for e in errors), errors
        error = iceberg_sink_preflight(contract, "ingest")
        assert error is not None
        assert pat not in error and pat not in " ".join(errors)


def test_an_expression_in_the_server_uri_is_refused_on_both_engines():
    auth = {**_OAUTH2, "oauth2ServerUri": "https://${KEYCLOAK_HOST}/realms/x/token"}
    for contract in (_kc(dict(auth)), _dbz(dict(auth))):
        errors = validate_iceberg_sink(contract)[0]
        assert any("oauth2ServerUri holds a ${...} expression" in e for e in errors), errors


# ── overrides that also decide auth, a bad type, an env: value ─────────────


@pytest.mark.parametrize(
    "key",
    [
        "iceberg.catalog.audience",
        "iceberg.catalog.resource",
        "iceberg.catalog.rest.sigv4-enabled",
        "iceberg.catalog.header.Authorization",
        "iceberg.catalog.header.authorization",
    ],
)
def test_an_override_that_also_decides_auth_is_refused(key):
    kc = {"iceberg_catalog_overrides": {key: "x"}}
    errors = validate_iceberg_sink(_kc(dict(_OAUTH2), kc=kc))[0]
    assert any("both set the catalog's auth" in e and key in e for e in errors), errors


def test_a_non_string_type_is_a_problem_not_a_crash():
    errors = validate_iceberg_sink(_kc({"type": ["oauth2"], "credentialEnv": "X"}))[0]
    assert any("type" in e for e in errors), errors
    assert not any("could not run" in e for e in errors), errors


@pytest.mark.parametrize("field", ["scope", "audience", "resource"])
def test_an_env_reference_in_a_written_value_is_refused(field):
    errors = validate_iceberg_sink(_kc({**_OAUTH2, field: " env:LK_SCOPE"}))[0]
    assert any(f"{field} starts with env:" in e for e in errors), errors


def test_derivation_turned_off_is_not_called_hand_written():
    kc = {"iceberg_sink_enabled": False}
    errors = validate_iceberg_sink(_kc(dict(_OAUTH2), kc=kc))[0]
    assert any("derivation is turned off" in e for e in errors), errors
    assert not any("hand-written" in e for e in errors), errors
