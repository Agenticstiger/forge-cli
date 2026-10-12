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

"""LIVE proof: ``sink.catalogAuth`` against a real OAuth2-protected Lakekeeper.

``sink.catalogAuth`` makes forge emit the REST catalog's auth as Iceberg
catalog properties, the secret as ``iceberg.catalog.credential=env:<NAME>``.
Apache Iceberg's REST client resolves that reference from the environment of
the JVM that builds the catalog (apache-iceberg-1.9.2
RESTSessionCatalog.java:195 -> EnvironmentUtil), here the Kafka Connect
WORKER, and an unset variable silently drops the property. No unit test can
see either half: both live in the worker's runtime. This module runs forge's
REAL derivation chain (``iceberg_sink_preflight`` ->
``find_iceberg_expose_binding`` -> ``resolve_iceberg_catalog`` ->
``emit_iceberg_sink_config(catalog_auth=catalog_auth_of(sink))``, the calls
the Kafka Connect runner makes) and hands the result to a real worker in front
of a Lakekeeper that requires a Keycloak-issued token:

* a ``catalogAuth`` of ``type: oauth2`` naming ``LK_CREDENTIAL`` streams
  records into the auth-required catalog. The worker holds the client
  credential ONLY as that environment variable; Iceberg fetches a token from
  Keycloak with it (client credentials, scope ``lakekeeper``) and presents it
  to Lakekeeper;
* the config Kafka Connect stores (its REST API and its internal config
  topic) holds ``env:LK_CREDENTIAL``, never the secret;
* the same derived config naming a variable the worker does not have
  (``LK_MISSING``) never writes: Iceberg drops the property, sends its first
  catalog request without a token, and the task fails on Lakekeeper's 401;
* Lakekeeper itself refuses a request with no token, and a token minted for
  another audience, so the first test's records prove authentication rather
  than an open catalog.

Stack: the stack of ``test_lakekeeper_kafka_connect_live`` (its compose file is
imported, not copied, so every image pin stays in one place) plus a compose
override that adds Keycloak (pinned by a multi-arch digest, dev mode, a realm
imported from a JSON file this module writes) and turns on Lakekeeper's OpenID
authentication (``LAKEKEEPER__OPENID_PROVIDER_URI`` +
``LAKEKEEPER__OPENID_AUDIENCE``; authorization stays ``allowall``, because the
point is authentication). The realm follows Lakekeeper's own Keycloak recipe
(docs/docs/authentication.md, lakekeeper v0.13.6): a confidential client with
service accounts and Standard Token Exchange, and an optional client scope
``lakekeeper`` whose Audience mapper adds ``lakekeeper`` to ``aud``. The client
secret is generated per run; nothing here is a real credential.

Gating: ``integration`` + ``emulated_heavy``; self-skips unless
``FLUID_TEST_LAKEKEEPER=1`` and Docker is reachable. It runs in the
``lakekeeper-integration`` job of
``.github/workflows/integration-emulated-heavy.yml``. Locally:

    FLUID_TEST_LAKEKEEPER=1 \\
    FLUID_LK_PLUGIN_CACHE=~/.cache/fluid/iceberg-kafka-connect \\
      python -m pytest -v -m emulated_heavy \\
      tests/integration/test_lakekeeper_oauth_live.py

Optional environment: ``FLUID_LK_PLUGIN_CACHE`` and ``FLUID_LK_LOG_DIR`` as in
``test_lakekeeper_kafka_connect_live``; ``FLUID_LK_AUTH_PROJECT`` is this
module's compose project name (default ``fluid-lk-auth-<random>``). It is not
``FLUID_LK_PROJECT``: CI runs both modules in one job, and each needs a stack
of its own.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
)
from fluid_build.providers._iceberg_catalog import (
    find_iceberg_expose_binding,
    resolve_iceberg_catalog,
)
from fluid_build.providers._iceberg_catalog_auth import catalog_auth_of
from tests.integration._iceberg_objectstore_live import _docker_available

# The unauthenticated module's stack and its catalog-agnostic plumbing. Only
# what does not depend on auth is imported; everything that talks to the
# catalog is redefined below, because here every catalog request carries a
# token.
from tests.integration.test_lakekeeper_kafka_connect_live import (
    _BUCKET,
    _COMPOSE,
    _LK_ENCRYPTION_KEY,
    _NAMESPACE,
    _PG_PASSWORD,
    _RECORDS,
    _S3_PASSWORD,
    _S3_REGION,
    _S3_USER,
    _WAREHOUSE,
    _contract,
    _create_topic,
    _delete_connector,
    _failed_task,
    _free_ports,
    _http,
    _lakekeeper_location,
    _parse,
    _put_connector,
    _Stack,
    _status,
    _total_records,
    _wait_running,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated_heavy,
    pytest.mark.slow,
    # The first test also pays for the module stack (image pulls, Keycloak's
    # start, the plugin download, the worker's warm-up commit rounds); the
    # 600 s default from pyproject.toml is not enough for that.
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        os.environ.get("FLUID_TEST_LAKEKEEPER") != "1" or not _docker_available(),
        reason=(
            "set FLUID_TEST_LAKEKEEPER=1 and have Docker running to run the "
            "Lakekeeper + Keycloak + Kafka Connect live test"
        ),
    ),
]

_REALM = "fluid"
_CLIENT_ID = "fluid-sink"
#: The client scope whose Audience mapper adds :data:`_AUDIENCE` to ``aud``.
_SCOPE = "lakekeeper"
_AUDIENCE = "lakekeeper"
#: A second optional scope, for a token minted for some OTHER service in the
#: same realm: the token Lakekeeper's audience check exists to refuse.
_OTHER_SCOPE = "other-service"
#: Long enough that no token expires during the module (a few minutes), so the
#: run never depends on Iceberg's token refresh. Refresh is still configured
#: to work: see the Standard Token Exchange note in :func:`_realm`.
_TOKEN_LIFESPAN_S = 3600

#: The worker's environment variable holding ``<client_id>:<client_secret>``,
#: the shape Iceberg's ``credential`` takes (OAuth2Util.java parseCredential).
_CREDENTIAL_ENV = "LK_CREDENTIAL"
#: A variable the worker does not have.
_MISSING_ENV = "LK_MISSING"

#: Worker-side (compose network) addresses. Lakekeeper validates a token's
#: ``iss`` against the discovery document it fetches from here; a token the
#: worker fetches from the same host carries that issuer.
_KEYCLOAK_REALM_URI = f"http://keycloak:8080/realms/{_REALM}"
_TOKEN_ENDPOINT = f"{_KEYCLOAK_REALM_URI}/protocol/openid-connect/token"

_UNAUTHORIZED_PREFIX = "org.apache.iceberg.exceptions.NotAuthorizedException: Not authorized: "

#: Layered over the unauthenticated module's ``_COMPOSE`` through COMPOSE_FILE.
#: Compose merges ``environment`` and ``depends_on`` by key, so ``lakekeeper``
#: gains the OpenID settings and the Keycloak dependency, ``connect`` gains the
#: credential variable, and nothing else in the base stack changes.
#:
#: ``LAKEKEEPER__OPENID_ADDITIONAL_ISSUERS``: Keycloak derives ``iss`` from the
#: request's host, so a token this module fetches through the published port
#: is issued by ``http://127.0.0.1:<port>/...``, not ``http://keycloak:8080/...``.
#: Lakekeeper's own Keycloak example accepts the host-side issuer the same way
#: (examples/access-control-simple/docker-compose.yaml, v0.13.6). The worker's
#: tokens carry the provider's issuer.
_AUTH_OVERRIDE = r"""
services:
  keycloak:
    image: quay.io/keycloak/keycloak:26.7.5@sha256:37dbaf6f0722c9ec246335f36e1ef8b2e6cb960f7c27e0d8c615121a3d475a85
    command: ["start-dev", "--import-realm"]
    environment:
      JAVA_OPTS_KC_HEAP: -Xms64m -Xmx512m
    volumes:
      - ./realm.json:/opt/keycloak/data/import/realm.json:ro
    ports:
      - "127.0.0.1:${LK_KEYCLOAK_PORT}:8080"
    healthcheck:
      # No curl in the image. The realm's discovery document answers only once
      # the import is done, so this also proves the realm loaded.
      test: ["CMD-SHELL", "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /realms/fluid/.well-known/openid-configuration HTTP/1.1\\r\\nHost: localhost\\r\\nConnection: close\\r\\n\\r\\n' >&3 && grep -q jwks_uri <&3"]
      interval: 2s
      timeout: 5s
      retries: 120
      start_period: 10s

  lakekeeper:
    environment:
      LAKEKEEPER__OPENID_PROVIDER_URI: http://keycloak:8080/realms/fluid
      LAKEKEEPER__OPENID_AUDIENCE: lakekeeper
      LAKEKEEPER__OPENID_ADDITIONAL_ISSUERS: http://127.0.0.1:${LK_KEYCLOAK_PORT}/realms/fluid
    depends_on:
      keycloak:
        condition: service_healthy

  connect:
    environment:
      LK_CREDENTIAL: ${LK_SINK_CREDENTIAL}
"""


def _realm(client_secret: str) -> Dict[str, Any]:
    """The Keycloak realm: one confidential client for the sink.

    The client scopes are all defined here because a realm file that lists
    any ``clientScopes`` gets none of Keycloak's built-in ones (26.7.5 logs
    "Referenced client scope 'basic' doesn't exist"): ``basic`` carries the
    ``sub`` claim Lakekeeper takes the principal's id from, and ``lakekeeper``
    is the Audience scope from Lakekeeper's Keycloak recipe. It is OPTIONAL on
    the client, as the recipe recommends, so a token carries the audience only
    when ``scope=lakekeeper`` is requested, which is what ``catalogAuth.scope``
    is for. ``other-service`` mints a token for a different audience.

    Standard Token Exchange: Iceberg 1.9.2 refreshes a token by exchanging it
    at the token endpoint, first authenticated with the token itself, then,
    after that fails, with the client credential (OAuth2Util.java
    ``AuthSession.refresh`` -> ``refreshExpiredToken``). Keycloak 26.7.5
    refuses the first ("invalid_client") and refuses the second unless the
    client has Standard Token Exchange ("Standard token exchange is not
    enabled for the requested client"); both were checked against this image.
    """
    return {
        "realm": _REALM,
        "enabled": True,
        "sslRequired": "none",
        "accessTokenLifespan": _TOKEN_LIFESPAN_S,
        "clientScopes": [
            {
                "name": "basic",
                "protocol": "openid-connect",
                "attributes": {
                    "include.in.token.scope": "false",
                    "display.on.consent.screen": "false",
                },
                "protocolMappers": [
                    {
                        "name": "sub",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-sub-mapper",
                        "config": {
                            "access.token.claim": "true",
                            "introspection.token.claim": "true",
                        },
                    }
                ],
            },
            _audience_scope(_SCOPE, _AUDIENCE),
            _audience_scope(_OTHER_SCOPE, _OTHER_SCOPE),
        ],
        "clients": [
            {
                "clientId": _CLIENT_ID,
                "name": "forge-derived Iceberg sink",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "clientAuthenticatorType": "client-secret",
                "secret": client_secret,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "attributes": {"standard.token.exchange.enabled": "true"},
                "defaultClientScopes": ["basic"],
                "optionalClientScopes": [_SCOPE, _OTHER_SCOPE],
            }
        ],
    }


def _audience_scope(name: str, audience: str) -> Dict[str, Any]:
    """A client scope whose Audience mapper adds ``audience`` to the access
    token's ``aud`` (the mapper of Lakekeeper's Keycloak recipe)."""
    return {
        "name": name,
        "description": f"Adds the {audience} audience",
        "protocol": "openid-connect",
        "attributes": {
            "include.in.token.scope": "true",
            "display.on.consent.screen": "false",
        },
        "protocolMappers": [
            {
                "name": f"{audience}-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.custom.audience": audience,
                    "access.token.claim": "true",
                    "introspection.token.claim": "true",
                    "id.token.claim": "false",
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


@dataclass
class _AuthStack(_Stack):
    keycloak_url: str = ""
    client_secret: str = ""
    #: A ``scope=lakekeeper`` token for this module's own catalog requests.
    token: str = ""

    def auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _fetch_token(stack: _AuthStack, *, scope: Optional[str]) -> Tuple[int, Dict[str, Any]]:
    """Client credentials against Keycloak's published port: ``(status, body)``."""
    form = {
        "grant_type": "client_credentials",
        "client_id": _CLIENT_ID,
        "client_secret": stack.client_secret,
    }
    if scope:
        form["scope"] = scope
    request = urllib.request.Request(
        f"{stack.keycloak_url}/realms/{_REALM}/protocol/openid-connect/token",
        data=urllib.parse.urlencode(form).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 — loopback only
            return resp.status, _parse(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode("utf-8", "replace")}


def _token(stack: _AuthStack, *, scope: Optional[str]) -> str:
    status, body = _fetch_token(stack, scope=scope)
    assert status == 200 and body.get("access_token"), f"token (scope={scope}) -> {status}"
    return str(body["access_token"])


def _claims(token: str) -> Dict[str, Any]:
    """A JWT's payload, unverified (Lakekeeper does the verifying)."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def _audiences(token: str) -> Tuple[str, ...]:
    aud = _claims(token).get("aud")
    if aud is None:
        return ()
    return (aud,) if isinstance(aud, str) else tuple(aud)


def _bootstrap_lakekeeper(stack: _AuthStack) -> str:
    """Bootstrap Lakekeeper and create the warehouse with a Keycloak token, and
    return the REST path prefix the catalog assigned it.

    The unauthenticated module's ``_bootstrap_lakekeeper`` with an
    ``Authorization`` header on every catalog call; the warehouse body is
    copied from it unchanged.
    """
    base = stack.catalog_url
    status, body = _http("GET", f"{base}/health")
    assert status == 200, f"GET /health -> {status}: {body}"

    status, body = _http(
        "POST",
        f"{base}/management/v1/bootstrap",
        {"accept-terms-of-use": True},
        headers=stack.auth(),
    )
    assert status == 204 or (
        status == 400 and "CatalogAlreadyBootstrapped" in body
    ), f"bootstrap -> {status}: {body}"

    warehouse = {
        "warehouse-name": _WAREHOUSE,
        "storage-profile": {
            "type": "s3",
            "bucket": _BUCKET,
            "region": _S3_REGION,
            "endpoint": "http://silo:9000",
            "sts-endpoint": "http://silo:9000",
            "path-style-access": True,
            "flavor": "s3-compat",
            "sts-enabled": True,
        },
        "storage-credential": {
            "type": "s3",
            "credential-type": "access-key",
            "aws-access-key-id": _S3_USER,
            "aws-secret-access-key": _S3_PASSWORD,
        },
    }
    status, body = _http("POST", f"{base}/management/v1/warehouse", warehouse, headers=stack.auth())
    assert status in (200, 201, 409), f"create warehouse -> {status}: {body}"

    status, body = _http(
        "GET", f"{base}/catalog/v1/config?warehouse={_WAREHOUSE}", headers=stack.auth()
    )
    assert status == 200, f"GET /catalog/v1/config -> {status}: {body}"
    config = _parse(body)
    merged = {**(config.get("defaults") or {}), **(config.get("overrides") or {})}
    prefix = merged.get("prefix")
    assert prefix, f"Lakekeeper /v1/config carries no prefix: {config}"
    return str(prefix)


@pytest.fixture(scope="module")
def auth_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_AuthStack]:
    workdir = tmp_path_factory.mktemp("lakekeeper-oauth-live")
    (workdir / "docker-compose.yml").write_text(_COMPOSE)
    (workdir / "docker-compose.auth.yml").write_text(_AUTH_OVERRIDE)
    client_secret = secrets.token_urlsafe(24)
    realm_file = workdir / "realm.json"
    realm_file.write_text(json.dumps(_realm(client_secret), indent=2))
    # Bind-mounted as a single file and read by Keycloak's uid 1000; the
    # directory pytest made is 0700, the file must not be.
    realm_file.chmod(0o644)
    project = os.environ.get("FLUID_LK_AUTH_PROJECT") or f"fluid-lk-auth-{uuid.uuid4().hex[:8]}"

    cache = os.environ.get("FLUID_LK_PLUGIN_CACHE")
    if cache:
        cache_dir = Path(cache).expanduser().resolve()
        cache_dir.mkdir(parents=True, exist_ok=True)
        plugin_source = str(cache_dir)
    else:
        plugin_source = "plugins"  # the compose volume; removed by `down -v`

    catalog_port, connect_port, keycloak_port = _free_ports(3)
    stack = _AuthStack(
        project=project,
        workdir=workdir,
        env={
            "COMPOSE_FILE": os.pathsep.join(["docker-compose.yml", "docker-compose.auth.yml"]),
            "LK_PG_PASSWORD": _PG_PASSWORD,
            "LK_ENCRYPTION_KEY": _LK_ENCRYPTION_KEY,
            "LK_S3_USER": _S3_USER,
            "LK_S3_PASSWORD": _S3_PASSWORD,
            "LK_CATALOG_PORT": str(catalog_port),
            "LK_CONNECT_PORT": str(connect_port),
            "LK_KEYCLOAK_PORT": str(keycloak_port),
            "LK_PLUGIN_SOURCE": plugin_source,
            # Reaches the worker as LK_CREDENTIAL, and nothing else.
            "LK_SINK_CREDENTIAL": f"{_CLIENT_ID}:{client_secret}",
        },
        catalog_url=f"http://127.0.0.1:{catalog_port}",
        connect_url=f"http://127.0.0.1:{connect_port}",
        keycloak_url=f"http://127.0.0.1:{keycloak_port}",
        client_secret=client_secret,
    )
    try:
        pull = stack.compose("pull", "--quiet", "--policy", "missing", timeout=600)
        if pull.returncode != 0:
            pytest.fail(f"docker compose pull failed:\n{pull.stderr[-3000:]}")
        # Long-running leaves only: `--wait` on a one-shot fails when it exits.
        up = stack.compose(
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "420",
            "keycloak",
            "lakekeeper",
            "connect",
            timeout=600,
        )
        if up.returncode != 0:
            stack.dump_logs("bring-up")
            pytest.fail(
                "the Lakekeeper + Keycloak + Kafka Connect stack did not come up:\n"
                f"{up.stderr[-3000:]}"
            )
        with stack.logs_on_failure("bootstrap"):
            stack.token = _token(stack, scope=_SCOPE)
            stack.prefix = _bootstrap_lakekeeper(stack)
        yield stack
    finally:
        stack.compose("down", "-v", "--remove-orphans", timeout=300)


# ---------------------------------------------------------------------------
# Contract + derivation (forge's real code only)
# ---------------------------------------------------------------------------


def _oauth2_contract(product_id: str, table: str, credential_env: str) -> Dict[str, Any]:
    """The unauthenticated module's Lakekeeper contract plus ``sink.catalogAuth``."""
    contract = _contract(product_id, _lakekeeper_location(table))
    contract["builds"][0]["properties"]["sink"]["catalogAuth"] = {
        "type": "oauth2",
        "credentialEnv": credential_env,
        "oauth2ServerUri": _TOKEN_ENDPOINT,
        "scope": _SCOPE,
    }
    return contract


def _derive(contract: Mapping[str, Any], topic: str) -> Dict[str, str]:
    """The sink config exactly as the Kafka Connect runner derives it, with
    ``catalog_auth`` read from ``properties.sink`` the way the runner reads it
    (fluid_build/build_runners/kafka_connect/runner.py)."""
    assert iceberg_sink_preflight(contract, "stream_orders") is None
    binding = find_iceberg_expose_binding(contract)
    assert binding is not None, "the contract's Iceberg expose was not found"
    resolved = resolve_iceberg_catalog(binding, contract=contract)
    props = contract["builds"][0]["properties"]
    return emit_iceberg_sink_config(
        resolved,
        product_id=contract["id"],
        topics=[topic],
        kc_props=props["kafka-connect"],
        catalog_auth=catalog_auth_of(props.get("sink")),
    )


def _assert_auth_properties(config: Mapping[str, str], env_name: str) -> None:
    """The catalog auth forge emitted: references and contract values only."""
    assert config["iceberg.catalog.type"] == "rest"
    assert config["iceberg.catalog.rest.auth.type"] == "oauth2"
    assert config["iceberg.catalog.credential"] == f"env:{env_name}"
    assert config["iceberg.catalog.oauth2-server-uri"] == _TOKEN_ENDPOINT
    assert config["iceberg.catalog.scope"] == _SCOPE
    assert "iceberg.catalog.token" not in config


# ---------------------------------------------------------------------------
# Catalog probes (every one authenticated)
# ---------------------------------------------------------------------------


def _load_table(stack: _AuthStack, table: str) -> Optional[Dict[str, Any]]:
    status, body = _http(
        "GET",
        f"{stack.catalog_url}/catalog/v1/{stack.prefix}/namespaces/{_NAMESPACE}/tables/{table}",
        headers={"X-Iceberg-Access-Delegation": "client-managed", **stack.auth()},
    )
    if status == 404:
        return None
    assert status == 200, f"loadTable {_NAMESPACE}.{table} -> {status}: {body}"
    return _parse(body)


def _produce(stack: _AuthStack, topic: str) -> None:
    records = "".join(
        json.dumps({"id": i, "customer": f"c{i}", "amount_cents": 100 * i}) + "\n"
        for i in range(1, _RECORDS + 1)
    )
    stack.kafka(
        "kafka-console-producer.sh",
        "--bootstrap-server",
        "localhost:19092",
        "--topic",
        topic,
        stdin=records,
    )


def _wait_failed(stack: _AuthStack, name: str, *, deadline_s: float = 180) -> Dict[str, Any]:
    """The connector status once a task is FAILED; fails the test if none does."""
    deadline = time.monotonic() + deadline_s
    status: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        status = _status(stack, name) or {}
        if _failed_task(status) is not None:
            return status
        time.sleep(2)
    pytest.fail(f"no task of {name} FAILED within {deadline_s:.0f}s: {status}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_catalog_auth_oauth2_streams_into_an_auth_required_lakekeeper(
    request: pytest.FixtureRequest,
):
    """forge's derived ``catalogAuth: {type: oauth2}`` config commits every
    record into a Lakekeeper that requires a Keycloak token, with the client
    credential reaching the worker only as an environment variable."""
    suffix = uuid.uuid4().hex[:8]
    table = f"orders_{suffix}"
    topic = f"orders-auth-{suffix}"
    connector = f"fluid-lk-oauth-{suffix}"
    config = _derive(
        _oauth2_contract(f"live.lakekeeper.oauth_{suffix}", table, _CREDENTIAL_ENV), topic
    )

    # Checked before any container starts: a deriver regression fails here in
    # well under a second.
    _assert_auth_properties(config, _CREDENTIAL_ENV)
    assert config["iceberg.catalog.warehouse"] == _WAREHOUSE
    assert config["iceberg.tables"] == f"{_NAMESPACE}.{table}"

    stack: _AuthStack = request.getfixturevalue("auth_stack")
    assert stack.client_secret not in json.dumps(config)
    with stack.logs_on_failure(request.node.name):
        try:
            _create_topic(stack, topic)
            _put_connector(stack, connector, config)
            # RUNNING already proves the credential resolved: sink 1.9.2 builds
            # the catalog in IcebergSinkTask.start(), and RESTCatalog's first
            # call (GET /v1/config) carries the token Iceberg fetched with it.
            _wait_running(stack, connector)
            _produce(stack, topic)

            # The worker's first commit rounds time out while it warms up (up
            # to about 2.5 minutes observed in the unauthenticated module).
            deadline = time.monotonic() + 360
            loaded: Optional[Dict[str, Any]] = None
            total = -1
            while time.monotonic() < deadline:
                failed = _failed_task(_status(stack, connector) or {})
                if failed:
                    pytest.fail(f"sink task FAILED while committing:\n{failed.get('trace', '')}")
                loaded = _load_table(stack, table)
                total = _total_records(loaded) if loaded else -1
                if total >= _RECORDS:
                    break
                time.sleep(5)
            assert loaded is not None, f"the sink never created {_NAMESPACE}.{table} in Lakekeeper"
            assert total == _RECORDS, f"current snapshot holds {total} records, want {_RECORDS}"
            location = (loaded.get("metadata") or {}).get("location", "")
            assert location.startswith(f"s3://{_BUCKET}/"), location
        finally:
            _delete_connector(stack, connector)


def test_stored_connector_config_holds_the_reference_not_the_secret(
    auth_stack: _AuthStack, request: pytest.FixtureRequest
):
    """What Kafka Connect stores and serves for the sink is ``env:LK_CREDENTIAL``:
    the secret is nowhere in GET /connectors/<name>/config, in the worker's
    internal config topic, or in the worker's log, while the task authenticates
    with it."""
    stack = auth_stack
    suffix = uuid.uuid4().hex[:8]
    topic = f"orders-stored-{suffix}"
    connector = f"fluid-lk-stored-{suffix}"
    config = _derive(
        _oauth2_contract(f"live.lakekeeper.stored_{suffix}", f"orders_{suffix}", _CREDENTIAL_ENV),
        topic,
    )
    with stack.logs_on_failure(request.node.name):
        try:
            _create_topic(stack, topic)
            _put_connector(stack, connector, config)
            _wait_running(stack, connector)

            status_code, body = _http("GET", f"{stack.connect_url}/connectors/{connector}/config")
            assert status_code == 200, f"GET /connectors/{connector}/config -> {status_code}"
            stored = _parse(body)
            assert stored["iceberg.catalog.credential"] == f"env:{_CREDENTIAL_ENV}"
            assert stored["iceberg.catalog.rest.auth.type"] == "oauth2"
            assert stored["iceberg.catalog.oauth2-server-uri"] == _TOKEN_ENDPOINT
            assert stored["iceberg.catalog.scope"] == _SCOPE
            assert stack.client_secret not in body

            # The record Connect persisted (and a worker restart would replay).
            persisted = stack.kafka(
                "kafka-console-consumer.sh",
                "--bootstrap-server",
                "localhost:19092",
                "--topic",
                "_connect-configs",
                "--from-beginning",
                "--timeout-ms",
                "10000",
            )
            assert f"env:{_CREDENTIAL_ENV}" in persisted, "the sink's config is not in the topic"
            assert stack.client_secret not in persisted

            # Nor did the worker log the resolved value.
            assert stack.client_secret not in stack.worker_log()
        finally:
            _delete_connector(stack, connector)


def test_unset_credential_variable_fails_the_task_and_writes_nothing(
    auth_stack: _AuthStack, request: pytest.FixtureRequest
):
    """The same derived config naming a variable the worker does not have.

    Iceberg drops ``credential=env:LK_MISSING`` (EnvironmentUtil.resolveAll),
    so with ``rest.auth.type=oauth2`` and no credential the client sends its
    first catalog request with no token, and Lakekeeper's 401 kills the task in
    IcebergSinkTask.start(). No record is consumed and no table appears.
    """
    stack = auth_stack
    suffix = uuid.uuid4().hex[:8]
    table = f"orders_{suffix}"
    topic = f"orders-missing-{suffix}"
    connector = f"fluid-lk-missing-{suffix}"
    config = _derive(
        _oauth2_contract(f"live.lakekeeper.missing_{suffix}", table, _MISSING_ENV), topic
    )
    _assert_auth_properties(config, _MISSING_ENV)

    with stack.logs_on_failure(request.node.name):
        # The premise: the worker has LK_CREDENTIAL and not LK_MISSING. Only
        # presence is tested; the value is never printed.
        has = stack.compose("exec", "-T", "connect", "sh", "-c", f'test -n "${_CREDENTIAL_ENV}"')
        assert has.returncode == 0, "the worker has no LK_CREDENTIAL"
        missing = stack.compose("exec", "-T", "connect", "sh", "-c", f'test -z "${_MISSING_ENV}"')
        assert missing.returncode == 0, f"the worker unexpectedly has {_MISSING_ENV}"

        try:
            _create_topic(stack, topic)
            _put_connector(stack, connector, config)
            _produce(stack, topic)
            status = _wait_failed(stack, connector)

            # Only the task fails; the connector stays RUNNING, which is why
            # forge's runner polls the task states.
            assert status.get("connector", {}).get("state") == "RUNNING", status
            failed = _failed_task(status) or {}
            trace = str(failed.get("trace", ""))
            head = trace.splitlines()[0] if trace else ""
            print(f"\n[observed] {connector} task trace head: {head}")
            # Lakekeeper's answer to a request with no Authorization header
            # (crates/lakekeeper/src/service/authn.rs, v0.13.6), as Iceberg's
            # default error handler reports a 401.
            assert head == f"{_UNAUTHORIZED_PREFIX}Missing Authorization Header", trace
            assert "RESTSessionCatalog" in trace or "RESTCatalog" in trace, trace

            # Nothing reached the catalog.
            assert _load_table(stack, table) is None
        finally:
            _delete_connector(stack, connector)


def test_lakekeeper_refuses_requests_without_a_valid_token(auth_stack: _AuthStack):
    """Authentication is really on: Lakekeeper refuses a catalog request with no
    token, with a garbage token, and with a valid Keycloak token minted for
    another audience, and serves the same request with the ``lakekeeper``
    audience.

    The audience negative uses a token whose ``aud`` names another service, not
    a token with no ``aud`` at all: Lakekeeper v0.13.6 ACCEPTS the latter (seen
    here: 200 on GET /catalog/v1/config). With a single OpenID provider it
    calls the provider's authenticator directly (authn.rs
    ``assemble_authenticator_chain``), whose jsonwebtoken ``Validation``
    (limes 0.4.2 jwks.rs ``authenticate_jwt``) checks ``aud`` only when the
    claim is present (jsonwebtoken 10.4.0 validation.rs: "Validation only
    happens if `aud` claim is present in the token").
    """
    stack = auth_stack
    config_url = f"{stack.catalog_url}/catalog/v1/config?warehouse={_WAREHOUSE}"
    namespaces_url = f"{stack.catalog_url}/catalog/v1/{stack.prefix}/namespaces"

    for url in (config_url, namespaces_url):
        status, body = _http("GET", url)
        print(f"\n[observed] GET {url} without a token -> {status}: {body}")
        assert status == 401, f"GET {url} without a token -> {status}: {body}"
        error = _parse(body).get("error") or {}
        assert error.get("type") == "MissingAuthorizationHeader", body

    status, body = _http("GET", config_url, headers={"Authorization": "Bearer not-a-jwt"})
    print(f"\n[observed] GET {config_url} with a garbage token -> {status}: {body}")
    assert status == 401, f"garbage token -> {status}: {body}"
    assert (_parse(body).get("error") or {}).get("type") == "AuthenticationFailed", body

    # A token from the same realm and client, for another service.
    other = _token(stack, scope=_OTHER_SCOPE)
    assert _audiences(other) == (_OTHER_SCOPE,), _audiences(other)
    status, body = _http("GET", config_url, headers={"Authorization": f"Bearer {other}"})
    print(f"\n[observed] GET {config_url} with aud={_OTHER_SCOPE} -> {status}: {body}")
    assert status == 401, f"token for aud={_OTHER_SCOPE} -> {status}: {body}"
    assert (_parse(body).get("error") or {}).get("type") == "AuthenticationFailed", body

    # The positive control: the same request with the audience is served.
    assert _AUDIENCE in _audiences(stack.token), _audiences(stack.token)
    status, body = _http("GET", config_url, headers=stack.auth())
    assert status == 200, f"token with the {_AUDIENCE} audience -> {status}: {body}"
