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

"""Schema surface of ``sink.catalogAuth`` (0.7.6 preview only).

The ``*Env`` fields hold an environment variable NAME, so a secret pasted in
their place fails the schema, and the schema error does not quote it
(jsonschema's own messages quote the failing value). 0.7.5 is GA and frozen,
so it keeps refusing the key. The schema and the Python parser must agree on
the variable-name rule.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import fluid_build
from fluid_build.providers._iceberg_catalog_auth import (
    ENV_NAME_RE,
    EXPRESSION_RE,
    FORBIDDEN_TEXT_PATTERN,
    SECRET_SHAPED_NAME_RE,
    parse_catalog_auth,
)
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = [pytest.mark.unit]

_SCHEMA_DIR = Path(fluid_build.__file__).parent / "schemas"
_KEYCLOAK = "http://keycloak:8080/realms/iceberg/protocol/openid-connect/token"
_SECRET = "spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret
_PAT = "da" + "pi" + "0123456789abcdef" * 2
_USERINFO_URI = "https://spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52@keycloak/realms/x/token"  # pragma: allowlist secret
_JWT = "eyJhbGciOiJIUzI1NiJ9.e30.sig"  # pragma: allowlist secret
_OAUTH2 = {
    "type": "oauth2",
    "credentialEnv": "LAKEKEEPER_CREDENTIAL",
    "oauth2ServerUri": _KEYCLOAK,
    "scope": "lakekeeper",
}


def _catalog_auth_schema() -> Dict[str, Any]:
    schema = json.loads((_SCHEMA_DIR / "fluid-schema-0.7.6.json").read_text(encoding="utf-8"))
    return schema["$defs"]["acquisitionSink"]["properties"]["catalogAuth"]


def _contract(version: str, catalog_auth: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "compat.ops.catalog_auth",
        "name": "Catalog Auth",
        "description": "Schema coverage for sink.catalogAuth.",
        "domain": "ops",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "platform-ops", "email": "ops@example.com"},
        },
        "builds": [
            {
                "id": "ingest_events",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "mode": "incremental_append",
                        "streams": ["public.events"],
                    },
                    "sink": {"format": "iceberg", "catalogAuth": catalog_auth},
                    "kafka-connect": {"iceberg_sink_enabled": True},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "version": "1.0.0",
                "binding": {
                    "platform": "local",
                    "format": "iceberg",
                    "location": {
                        "catalog": "lakekeeper",
                        "uri": "http://lakekeeper:8181/catalog",
                        "warehouse": "demo",
                        "database": "bronze",
                        "table": "events",
                    },
                },
                "contract": {"schema": [{"name": "id", "type": "integer", "required": True}]},
            }
        ],
    }


def _errors(version: str, catalog_auth: Any) -> list:
    result = FluidSchemaManager().validate_contract(_contract(version, catalog_auth), version)
    return [] if result.is_valid else list(result.errors)


@pytest.mark.parametrize(
    "catalog_auth",
    [
        dict(_OAUTH2),
        {**_OAUTH2, "audience": "lakekeeper", "resource": "urn:lakekeeper"},
        {
            "type": "oauth2",
            "credentialEnv": "POLARIS_CREDENTIAL",
            "oauth2ServerUri": "http://polaris:8181/api/catalog/v1/oauth/tokens",
            "scope": "PRINCIPAL_ROLE:ALL",
        },
        {"type": "bearer", "tokenEnv": "UC_TOKEN"},
    ],
    ids=["lakekeeper", "audience-resource", "polaris", "unity-bearer"],
)
def test_preview_accepts_catalog_auth(catalog_auth):
    assert _errors("0.7.6", catalog_auth) == []


def test_ga_075_refuses_the_key():
    """0.7.5 is released: an additive field lands in the open preview only."""
    errors = _errors("0.7.5", dict(_OAUTH2))
    assert any("catalogAuth" in e and "was unexpected" in e for e in errors), errors


@pytest.mark.parametrize(
    "catalog_auth, expected",
    [
        ({**_OAUTH2, "credentialEnv": _SECRET}, "catalogAuth.credentialEnv: does not match"),
        (
            {
                "type": "bearer",
                "tokenEnv": _JWT,
            },
            "tokenEnv: does not match",
        ),
        ({**_OAUTH2, "oauth2ServerUri": "keycloak/token"}, "oauth2ServerUri: does not match"),
        ({k: v for k, v in _OAUTH2.items() if k != "scope"}, "'scope' is a required property"),
        (
            {k: v for k, v in _OAUTH2.items() if k != "oauth2ServerUri"},
            "'oauth2ServerUri' is a required property",
        ),
        ({"type": "bearer"}, "'tokenEnv' is a required property"),
        ({"type": "bearer", "tokenEnv": "T", "scope": "x"}, "scope is not allowed here"),
        ({**_OAUTH2, "tokenEnv": "T"}, "tokenEnv is not allowed here"),
        ({**_OAUTH2, "clientSecret": "x"}, "'clientSecret' was unexpected"),
        ({"type": "basic"}, "catalogAuth.type: fails enum"),
        ({**_OAUTH2, "credentialEnv": "LK_CRED\n"}, "credentialEnv: does not match"),
        ({"type": "bearer", "tokenEnv": "UC_TOKEN\n"}, "tokenEnv: does not match"),
        ({"type": "bearer", "tokenEnv": _PAT}, "tokenEnv: must not match"),
        (
            {**_OAUTH2, "credentialEnv": "AKIAIOSFODNN7EXAMPLE"},  # pragma: allowlist secret
            "credentialEnv: must not match",
        ),
        ({**_OAUTH2, "oauth2ServerUri": _USERINFO_URI}, "oauth2ServerUri: does not match"),
        ({**_OAUTH2, "oauth2ServerUri": "https://spark@kc/token"}, "oauth2ServerUri: does not"),
        (
            {**_OAUTH2, "oauth2ServerUri": "https://${KC_HOST}/realms/x/token"},
            "oauth2ServerUri: must not match",
        ),
        ({**_OAUTH2, "scope": "${SCOPE}"}, "scope: must not match"),
        ({**_OAUTH2, "audience": "${AUD}"}, "audience: must not match"),
        ({**_OAUTH2, "resource": "urn:${RES}"}, "resource: must not match"),
    ],
)
def test_preview_refuses_a_malformed_catalog_auth(catalog_auth, expected):
    errors = _errors("0.7.6", catalog_auth)
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize(
    "catalog_auth, secret",
    [
        ({**_OAUTH2, "credentialEnv": _SECRET}, _SECRET),
        ({"type": "bearer", "tokenEnv": _JWT}, _JWT.split(".")[0]),
        # A field the type does not take is refused without quoting it either.
        ({"type": "bearer", "tokenEnv": "T", "credentialEnv": _SECRET}, _SECRET),
        ({"type": _SECRET}, _SECRET),
        ({"type": "bearer", "tokenEnv": _PAT}, _PAT),
        ({**_OAUTH2, "oauth2ServerUri": _USERINFO_URI}, "2OR3eRvYfSZ"),
    ],
    ids=["credentialEnv", "tokenEnv", "wrong-type-field", "type", "pat", "userinfo"],
)
def test_a_schema_error_never_quotes_the_value(catalog_auth, secret):
    errors = _errors("0.7.6", catalog_auth)
    assert errors
    assert not any(secret in e or "2OR3eRvYfSZ" in e for e in errors), errors


def test_schema_and_parser_share_the_variable_name_rule():
    schema = _catalog_auth_schema()
    for field in ("credentialEnv", "tokenEnv"):
        assert schema["properties"][field]["pattern"] == ENV_NAME_RE.pattern
        assert schema["properties"][field]["not"] == {"pattern": SECRET_SHAPED_NAME_RE.pattern}
    for field in ("oauth2ServerUri", "scope", "audience", "resource"):
        assert schema["properties"][field]["not"] == {"pattern": FORBIDDEN_TEXT_PATTERN}


@pytest.mark.parametrize(
    "catalog_auth",
    [
        dict(_OAUTH2),
        {**_OAUTH2, "credentialEnv": _SECRET},
        {**_OAUTH2, "oauth2ServerUri": "keycloak/token"},
        {"type": "bearer", "tokenEnv": "UC_TOKEN"},
        {"type": "bearer", "tokenEnv": "T", "scope": "x"},
        {k: v for k, v in _OAUTH2.items() if k != "scope"},
        {**_OAUTH2, "credentialEnv": "LK_CRED\n"},
        {"type": "bearer", "tokenEnv": "UC_TOKEN\n"},
        {"type": "bearer", "tokenEnv": _PAT},
        {"type": "bearer", "tokenEnv": "S3_CREDENTIAL_V2"},
        {"type": "bearer", "tokenEnv": "LAKEKEEPERCATALOGCREDENTIALPROD"},
        {**_OAUTH2, "oauth2ServerUri": _USERINFO_URI},
        {**_OAUTH2, "oauth2ServerUri": "https://kc@idp/token"},
        {**_OAUTH2, "oauth2ServerUri": "https://kc/token?login_hint=a@b"},
        {**_OAUTH2, "oauth2ServerUri": "https://kc:8443"},
        {**_OAUTH2, "oauth2ServerUri": "https://${KC_HOST}/token"},
        {**_OAUTH2, "scope": "${SCOPE}"},
        {**_OAUTH2, "audience": "${AUD}"},
        {**_OAUTH2, "resource": "urn:${RES}"},
        {**_OAUTH2, "scope": "a$b{c}"},
    ],
)
def test_schema_and_parser_agree(catalog_auth):
    """The runner preflight parses what `fluid validate`'s schema pass checked;
    a contract one accepts and the other refuses would pass one gate only."""
    _auth, problems = parse_catalog_auth(catalog_auth)
    assert (_errors("0.7.6", catalog_auth) == []) == (problems == [])


# ── a scheme Iceberg reads as a path, and control characters ────────────────


@pytest.mark.parametrize(
    "uri",
    [
        "HTTPS://keycloak:8443/realms/x/protocol/openid-connect/token",
        "Https://keycloak/realms/x/protocol/openid-connect/token",
        "hTtP://keycloak/realms/x/protocol/openid-connect/token",
    ],
)
def test_a_non_lowercase_scheme_is_refused_by_schema_and_parser(uri):
    # Iceberg's HTTPRequest.hasAbsolutePath compares the prefix case-sensitively,
    # so this URL would be resolved against the catalog uri.
    auth = {**_OAUTH2, "oauth2ServerUri": uri}
    assert parse_catalog_auth(auth)[0] is None
    assert any("lowercase http://" in p for p in parse_catalog_auth(auth)[1])
    assert any("oauth2ServerUri" in e for e in _errors("0.7.6", auth))


@pytest.mark.parametrize("field", ["scope", "audience", "resource", "oauth2ServerUri"])
@pytest.mark.parametrize(
    "char", ["\n", "\r", "\t", "\x00", "\x7f"], ids=["lf", "cr", "tab", "nul", "del"]
)
def test_a_control_character_is_refused_by_schema_and_parser(field, char):
    base = _KEYCLOAK if field == "oauth2ServerUri" else "lakekeeper"
    value = base[:4] + char + base[4:]
    auth = {**_OAUTH2, field: value}
    parsed, problems = parse_catalog_auth(auth)
    assert parsed is None, problems
    assert any(field in p for p in problems), problems
    assert not any(value in p for p in problems), problems
    errors = _errors("0.7.6", auth)
    assert any(field in e for e in errors), errors
    assert not any(value in e for e in errors), errors


@pytest.mark.parametrize("uri", [_KEYCLOAK, "https://idp.example/oauth2/token?x=1"])
def test_a_lowercase_scheme_is_accepted_by_schema_and_parser(uri):
    auth = {**_OAUTH2, "oauth2ServerUri": uri}
    assert parse_catalog_auth(auth)[1] == []
    assert not any("catalogAuth" in e for e in _errors("0.7.6", auth))


def test_a_bundle_validation_does_not_quote_a_pasted_secret():
    # `fluid validate <bundle>.tgz` reports schema errors through
    # forge.core.validators, not FluidSchemaManager.validate_contract.
    from fluid_build.forge.core.validators import validate_json_schema

    auth = {**_OAUTH2, "credentialEnv": _SECRET, "oauth2ServerUri": _USERINFO_URI}
    issues = validate_json_schema(
        "contract.resolved.yaml", _contract("0.7.6", auth), schema_manager=FluidSchemaManager()
    )
    messages = [i.message for i in issues]
    assert any("credentialEnv" in m for m in messages), messages
    assert not any("2OR3eRvYfSZ" in m for m in messages), messages


@pytest.mark.parametrize("catalog_auth", ["LK_CREDENTIAL", ["oauth2"], 3])
def test_a_non_mapping_catalog_auth_draws_one_error(catalog_auth):
    # Both per-type `if` blocks were vacuously true for a non-object, so their
    # `then` branches added six false "is not allowed here" errors.
    errors = [e for e in _errors("0.7.6", catalog_auth) if "catalogAuth" in e]
    assert len(errors) == 1, errors
    assert "fails type: 'object'" in errors[0]
