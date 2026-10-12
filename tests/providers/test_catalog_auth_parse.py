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

"""``sink.catalogAuth`` parsing and the catalog properties it becomes.

The contract names environment variables; the emitted properties carry
``env:<NAME>`` references that Iceberg's REST client resolves itself
(apache-iceberg-1.9.2 / 1.10.0 RESTSessionCatalog.java:195). No problem
message may quote a field value: a value that fails the name rule may be a
pasted secret.
"""

from __future__ import annotations

import pytest

from fluid_build.providers._iceberg_catalog_auth import (
    AUTH_OWNED_PROPERTIES,
    CatalogAuth,
    catalog_auth_of,
    is_literal_secret,
    parse_catalog_auth,
)

pytestmark = [pytest.mark.unit]

_KEYCLOAK = "http://keycloak:8080/realms/iceberg/protocol/openid-connect/token"
_SECRET = "spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52"  # pragma: allowlist secret

_OAUTH2 = {
    "type": "oauth2",
    "credentialEnv": "LAKEKEEPER_CREDENTIAL",
    "oauth2ServerUri": _KEYCLOAK,
    "scope": "lakekeeper",
}


def test_oauth2_emits_env_references_and_explicit_auth_type():
    auth, problems = parse_catalog_auth(dict(_OAUTH2))
    assert problems == []
    assert auth is not None
    assert auth.catalog_properties() == {
        "rest.auth.type": "oauth2",
        "credential": "env:LAKEKEEPER_CREDENTIAL",
        "oauth2-server-uri": _KEYCLOAK,
        "scope": "lakekeeper",
    }


def test_oauth2_audience_and_resource_only_when_set():
    auth, _ = parse_catalog_auth({**_OAUTH2, "audience": "lakekeeper", "resource": "urn:lk"})
    assert auth is not None
    props = auth.catalog_properties()
    assert props["audience"] == "lakekeeper"
    assert props["resource"] == "urn:lk"
    bare, _ = parse_catalog_auth(dict(_OAUTH2))
    assert bare is not None
    assert "audience" not in bare.catalog_properties()
    assert "resource" not in bare.catalog_properties()


def test_bearer_emits_token_reference():
    auth, problems = parse_catalog_auth({"type": "bearer", "tokenEnv": "UC_TOKEN"})
    assert problems == []
    assert auth == CatalogAuth(type="bearer", env_var="UC_TOKEN")
    assert auth.env_field == "tokenEnv"
    assert auth.catalog_properties() == {"rest.auth.type": "oauth2", "token": "env:UC_TOKEN"}


def test_emitted_keys_are_the_ones_the_conflict_check_owns():
    """A key catalogAuth emits but the override conflict check does not own
    could be set twice without an error."""
    oauth2, _ = parse_catalog_auth({**_OAUTH2, "audience": "a", "resource": "urn:r"})
    bearer, _ = parse_catalog_auth({"type": "bearer", "tokenEnv": "T"})
    assert oauth2 is not None and bearer is not None
    emitted = set(oauth2.catalog_properties()) | set(bearer.catalog_properties())
    assert emitted <= set(AUTH_OWNED_PROPERTIES), emitted - set(AUTH_OWNED_PROPERTIES)


def test_no_catalog_auth_is_not_a_problem():
    assert parse_catalog_auth(None) == (None, [])
    assert catalog_auth_of({"format": "iceberg"}) is None
    assert catalog_auth_of(None) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({**_OAUTH2, "credentialEnv": _SECRET}, "must NAME an environment variable"),
        ({**_OAUTH2, "credentialEnv": "1BAD"}, "must NAME an environment variable"),
        ({"type": "bearer", "tokenEnv": "eyJhbGciOi.payload.sig"}, "must NAME"),
        ({k: v for k, v in _OAUTH2.items() if k != "scope"}, "scope is required"),
        (
            {k: v for k, v in _OAUTH2.items() if k != "oauth2ServerUri"},
            "oauth2ServerUri is required",
        ),
        ({**_OAUTH2, "oauth2ServerUri": "keycloak/token"}, "absolute http(s) URL"),
        ({**_OAUTH2, "oauth2ServerUri": "ftp://keycloak/token"}, "absolute http(s) URL"),
        ({**_OAUTH2, "oauth2ServerUri": "http://kc/a token"}, "absolute http(s) URL"),
        ({**_OAUTH2, "scope": "  "}, "scope is required"),
        ({**_OAUTH2, "audience": ""}, "audience must be a non-empty string"),
        ({**_OAUTH2, "tokenEnv": "T"}, "does not take ['tokenEnv']"),
        ({"type": "bearer", "tokenEnv": "T", "scope": "x"}, "does not take ['scope']"),
        ({"type": "basic"}, "type must be one of: oauth2, bearer"),
        ({"credentialEnv": "X"}, "type must be one of"),
        ("LAKEKEEPER_CREDENTIAL", "must be a mapping"),
    ],
)
def test_malformed_catalog_auth_is_refused(raw, expected):
    auth, problems = parse_catalog_auth(raw)
    assert auth is None
    assert any(expected in p for p in problems), problems


@pytest.mark.parametrize("value", [_SECRET, "eyJhbGciOi.payload.sig", "client:secret=="])
def test_a_pasted_secret_is_never_quoted(value):
    _auth, problems = parse_catalog_auth({**_OAUTH2, "credentialEnv": value})
    assert problems
    assert not any(value in p or value.split(":")[-1] in p for p in problems)
    with pytest.raises(ValueError) as raised:
        catalog_auth_of({"catalogAuth": {**_OAUTH2, "credentialEnv": value}})
    assert value not in str(raised.value)


@pytest.mark.parametrize(
    "value, literal",
    [
        ("spark:secret", True),
        ("eyJhbGciOi.payload.sig", True),
        ("env:LK_CRED", False),
        ("${env:LK_CRED}", False),
        ("${env:ID}:${env:SECRET}", False),
        ("${LK_CRED}", False),
        ("", False),
        (None, False),
    ],
)
def test_is_literal_secret(value, literal):
    assert is_literal_secret(value) is literal


# ── values that pass a naive rule but must not reach the sink config ─────────

_PAT = "da" + "pi" + "0123456789abcdef" * 2


@pytest.mark.parametrize("field", ["credentialEnv", "tokenEnv"])
@pytest.mark.parametrize("value", ["LK_CRED\n", "LK_CRED\r\n", "LK_CRED "])
def test_a_name_with_trailing_whitespace_is_refused(field, value):
    """Python's ``$`` matches before a trailing newline; ``env:LK_CRED\\n`` names
    a variable no process has, so Iceberg would drop the credential."""
    raw = (
        {**_OAUTH2, "credentialEnv": value}
        if field == "credentialEnv"
        else {"type": "bearer", "tokenEnv": value}
    )
    auth, problems = parse_catalog_auth(raw)
    assert auth is None
    assert any(f"{field} must NAME an environment variable" in p for p in problems), problems


@pytest.mark.parametrize(
    "value",
    [
        _PAT,
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",  # pragma: allowlist secret
        "AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret
        "sk_" + "live" + "_51HxYzAbCdEfGhIjKlMnOpQr",  # pragma: allowlist secret
    ],
    ids=["databricks-pat", "github-pat", "aws-key-id", "stripe-key"],
)
@pytest.mark.parametrize("field", ["credentialEnv", "tokenEnv"])
def test_a_token_shaped_name_is_refused_without_being_quoted(field, value):
    raw = (
        {**_OAUTH2, "credentialEnv": value}
        if field == "credentialEnv"
        else {"type": "bearer", "tokenEnv": value}
    )
    auth, problems = parse_catalog_auth(raw)
    assert auth is None
    assert any(f"{field} looks like a token" in p for p in problems), problems
    assert not any(value in p for p in problems)


@pytest.mark.parametrize(
    "name",
    [
        "LAKEKEEPER_CREDENTIAL",
        "UC_TOKEN",
        "S3_CREDENTIAL_V2",
        "LAKEKEEPERCATALOGCREDENTIALPROD",  # long, but no digit
        "POLARIS_PRINCIPAL_CLIENT_CREDENTIAL_2026",
        "lk_cred",
    ],
)
def test_ordinary_variable_names_are_accepted(name):
    auth, problems = parse_catalog_auth({"type": "bearer", "tokenEnv": name})
    assert problems == []
    assert auth is not None and auth.env_var == name


@pytest.mark.parametrize(
    "uri",
    [
        "https://spark:2OR3eRvYfSZzzZ16MlPd95jhLnOaLM52@keycloak/realms/x/protocol/openid-connect/token",  # pragma: allowlist secret
        "https://spark@keycloak/token",
        "http://:secret@keycloak:8080/token",
    ],
)
def test_an_oauth2_server_uri_with_userinfo_is_refused_without_being_quoted(uri):
    auth, problems = parse_catalog_auth({**_OAUTH2, "oauth2ServerUri": uri})
    assert auth is None
    assert any("no user:password@ part" in p for p in problems), problems
    assert not any("2OR3eRvYfSZ" in p or "secret@" in p or "spark@" in p for p in problems)


def test_an_at_sign_after_the_host_is_not_userinfo():
    auth, problems = parse_catalog_auth(
        {**_OAUTH2, "oauth2ServerUri": "https://keycloak/token?login_hint=a@b"}
    )
    assert problems == []
    assert auth is not None


@pytest.mark.parametrize(
    "field, value",
    [
        ("oauth2ServerUri", "https://${KEYCLOAK_HOST}/realms/x/protocol/openid-connect/token"),
        ("oauth2ServerUri", "https://keycloak/realms/${REALM}/token"),
        ("scope", "${SCOPE}"),
        ("audience", "${AUDIENCE}"),
        ("resource", "urn:${RESOURCE}"),
    ],
)
def test_an_expression_in_a_plain_field_is_refused(field, value):
    """Debezium Server (SmallRye) expands ``${...}`` in application.properties;
    Kafka Connect passes it on as text. One contract, two behaviours."""
    auth, problems = parse_catalog_auth({**_OAUTH2, field: value})
    assert auth is None
    assert any(f"{field} holds a ${{...}} expression" in p for p in problems), problems


def test_example_properties_echo_no_contract_value():
    auth, _ = parse_catalog_auth({**_OAUTH2, "audience": "aud-x", "resource": "urn:res-x"})
    assert auth is not None
    example = auth.example_properties()
    assert list(example) == list(auth.catalog_properties())
    assert example == {
        "rest.auth.type": "oauth2",
        "credential": "env:LAKEKEEPER_CREDENTIAL",
        "oauth2-server-uri": "<oauth2ServerUri>",
        "scope": "<scope>",
        "audience": "<audience>",
        "resource": "<resource>",
    }
