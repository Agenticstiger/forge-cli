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

"""An inline block's own material is all its SDK sends, whatever this host configured.

With ``--allow-inline-credentials`` alone a block passes the gate only when it
carries the caller's credential as values. A catalog SDK also reads settings
from the server's environment and config files, though, and some of those
settings choose the credential. The Databricks SDK reads ``DATABRICKS_AUTH_TYPE``
(or ``auth_type`` from the ``~/.databrickscfg`` profile ``DATABRICKS_CONFIG_PROFILE``
names) and lets it override a block's token: with ``oauth-m2m`` it posts this
host's service-principal secret to the token endpoint of whatever workspace the
block names. The Snowflake connector, building its login request, asks AWS STS
who this host is, signed with this host's AWS credentials, and reports what it
finds to the account the block names.

Each test plants such settings on a fake host, drives the real MCP tool path
against a loopback endpoint that records what arrives, and asserts that only the
caller's material is sent. The positive controls make the same call with the
operator's ``--allow-metadata-service``, which keeps today's behaviour, and show
the sink receiving the host's material, so a sink that sees nothing cannot pass.
The tests that need a catalog SDK skip without it; the ones on boto3 and httpx
run in every lane, and so do the checks on the arguments each adapter hands its
SDK.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from fluid_build.cli.mcp import _build_source_adapter, _call_tool
from fluid_build.cli.mcp.dispatch import McpToolRefusal
from fluid_build.copilot.catalog.credentials import UnityCredentials, pin_to_own_material
from tests._infrastructure.aws_endpoint_logger import AwsEndpointLogger, point_boto3_at
from tests._infrastructure.file_read_sink import watch_opens
from tests._infrastructure.http_auth_logger import HttpAuthLogger

_ENV = "FLUID_ALLOW_METADATA_SERVICE"

# Test values only: none of these is a credential anywhere.
_CALLER_TOKEN = "caller-test-token"  # pragma: allowlist secret
_CALLER_CLIENT = "caller-client"
_CALLER_CLIENT_VALUE = "caller-client-test-value"  # pragma: allowlist secret
_CALLER_KEY_ID = "CALLERTESTKEYID"
_CALLER_KEY_VALUE = "caller-test-secret-value"  # pragma: allowlist secret
_HOST_SP = "host-service-principal"
_HOST_SP_VALUE = "host-sp-test-value"  # pragma: allowlist secret
_HOST_USER = "host-user"
_HOST_PASSWORD = "host-password-test-value"  # pragma: allowlist secret
_HOST_TOKEN = "host-test-token"  # pragma: allowlist secret
_HOST_KEY_ID = "HOSTTESTKEYID"
_HOST_KEY_VALUE = "host-test-secret-value"  # pragma: allowlist secret
_HOST_SESSION_TOKEN = "host-test-session-token"  # pragma: allowlist secret
_MINTED = "minted-test-access-token"  # pragma: allowlist secret

_HOST_ENV_PREFIXES = (
    "AWS_",
    "ARM_",
    "CLOUDSDK_",
    "DATABRICKS_",
    "DATAHUB_",
    "DATAMESH_MANAGER_",
    "DMM_",
    "GOOGLE_",
    "SNOWFLAKE_",
)


def _basic(user: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()


# Every form in which this host's material could reach the wire.
_HOST_MATERIAL = (
    _HOST_SP_VALUE,
    _HOST_PASSWORD,
    _HOST_TOKEN,
    _HOST_KEY_VALUE,
    _HOST_SESSION_TOKEN,
    _basic(_HOST_SP, _HOST_SP_VALUE),
    _basic(_HOST_USER, _HOST_PASSWORD),
)


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def _needs(*modules: str) -> None:
    for module in modules:
        if not _has(module):
            pytest.skip(f"{module} is not installed")


def _host_material_seen(endpoint: HttpAuthLogger) -> List[str]:
    """Every header or body on ``endpoint`` that carries this host's material."""
    seen = [auth or "" for (_method, _host, _path, auth) in endpoint.requests]
    return [text for text in seen + endpoint.bodies if any(m in text for m in _HOST_MATERIAL)]


def _isolate(mp: pytest.MonkeyPatch, home: Path) -> None:
    """Give this process ``home`` as its only HOME and no catalog settings."""
    for key in list(os.environ):
        if key.startswith(_HOST_ENV_PREFIXES):
            mp.delenv(key, raising=False)
    for key in (_ENV, "NETRC", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        mp.delenv(key, raising=False)
        mp.delenv(key.lower(), raising=False)
    mp.setenv("HOME", str(home))
    mp.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    mp.chdir(home)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    _isolate(monkeypatch, path)
    monkeypatch.setattr(
        "fluid_build.copilot.catalog.credentials.SOURCES_CONFIG_DEFAULT",
        path / ".fluid" / "sources.yaml",
    )
    return path


def _args(source: str, inline: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    scope = {
        "database": "analytics",
        "schema_name": "sales",
        "catalog": "main",
        "tables": ["orders"],
    }
    return {"source": source, "credentials": {"inline": inline}, "scope": scope, **extra}


def _list_tables(arguments: Dict[str, Any], *, opted_in: bool = False) -> Any:
    return _call_tool(
        "list_source_tables",
        arguments,
        read_only=True,
        allow_inline_credentials=True,
        allow_metadata_service=opted_in,
    )


# ---------------------------------------------------------------------------
# Databricks Unity: the SDK lets this host's settings choose the credential.
# ---------------------------------------------------------------------------


def _workspace_answers(path: str, url: str) -> Optional[Dict[str, Any]]:
    """A Databricks workspace's discovery and token endpoints, served on loopback.

    Answers both discovery routes the SDK has used: ``/.well-known/databricks-config``
    (newer SDKs read the OIDC root from it) and the workspace's own
    ``/oidc/.well-known/oauth-authorization-server``.
    """
    if path.startswith("/.well-known/databricks-config"):
        return {"oidc_endpoint": f"{url}/oidc", "workspace_id": "1"}
    if path.startswith("/oidc/.well-known/oauth-authorization-server"):
        return {
            "authorization_endpoint": f"{url}/oidc/v1/authorize",
            "token_endpoint": f"{url}/oidc/v1/token",
        }
    if path.startswith("/oidc/v1/token"):
        return {"access_token": _MINTED, "token_type": "Bearer", "expires_in": 3600}
    return None


@pytest.fixture
def workspace() -> Iterator[HttpAuthLogger]:
    logger = HttpAuthLogger(answers=_workspace_answers)
    yield logger
    logger.close()


def _write_databrickscfg(path: Path, profile: str, **values: str) -> None:
    lines = [f"[{profile}]"] + [f"{key} = {value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _env_oauth_m2m(mp: pytest.MonkeyPatch, home: Path) -> None:
    """The reported case: the operator's own service principal, in the environment."""
    mp.setenv("DATABRICKS_AUTH_TYPE", "oauth-m2m")
    mp.setenv("DATABRICKS_CLIENT_ID", _HOST_SP)
    mp.setenv("DATABRICKS_CLIENT_SECRET", _HOST_SP_VALUE)


def _profile_oauth_m2m(mp: pytest.MonkeyPatch, home: Path) -> None:
    """The same service principal, in the ~/.databrickscfg profile the env names."""
    _write_databrickscfg(
        home / ".databrickscfg",
        "host-sp",
        auth_type="oauth-m2m",
        client_id=_HOST_SP,
        client_secret=_HOST_SP_VALUE,
    )
    mp.setenv("DATABRICKS_CONFIG_PROFILE", "host-sp")


def _config_file_elsewhere(mp: pytest.MonkeyPatch, home: Path) -> None:
    """A config file outside HOME, named by DATABRICKS_CONFIG_FILE."""
    path = home.parent / "databricks.cfg"
    _write_databrickscfg(
        path, "host-sp", auth_type="oauth-m2m", client_id=_HOST_SP, client_secret=_HOST_SP_VALUE
    )
    mp.setenv("DATABRICKS_CONFIG_FILE", str(path))
    mp.setenv("DATABRICKS_CONFIG_PROFILE", "host-sp")


def _env_basic(mp: pytest.MonkeyPatch, home: Path) -> None:
    """A username and password in the environment, sent as HTTP basic auth."""
    mp.setenv("DATABRICKS_AUTH_TYPE", "basic")
    mp.setenv("DATABRICKS_USERNAME", _HOST_USER)
    mp.setenv("DATABRICKS_PASSWORD", _HOST_PASSWORD)


_UNITY_HOST_CONFIG = [
    pytest.param(_env_oauth_m2m, id="env-oauth-m2m"),
    pytest.param(_profile_oauth_m2m, id="profile-oauth-m2m"),
    pytest.param(_config_file_elsewhere, id="config-file-oauth-m2m"),
    pytest.param(_env_basic, id="env-basic"),
]


def _unity_pat(url: str) -> Dict[str, Any]:
    return {"host": url, "auth_method": "pat", "token": _CALLER_TOKEN}


def _api_authorizations(endpoint: HttpAuthLogger) -> List[Optional[str]]:
    return [auth for (_m, _h, path, auth) in endpoint.requests if path.startswith("/api/")]


def _token_posts(endpoint: HttpAuthLogger) -> List[Optional[str]]:
    return [auth for (_m, _h, path, auth) in endpoint.requests if path.startswith("/oidc/v1/token")]


class TestUnityPatBlock:
    @pytest.mark.parametrize("plant", _UNITY_HOST_CONFIG)
    def test_only_the_callers_token_is_sent(self, home, workspace, monkeypatch, plant):
        _needs("databricks.sdk")
        plant(monkeypatch, home)
        out = _list_tables(_args("unity", _unity_pat(workspace.url)))
        assert out == {"tables": []}
        assert _host_material_seen(workspace) == []
        assert _token_posts(workspace) == []
        api = _api_authorizations(workspace)
        assert api and set(api) == {f"Bearer {_CALLER_TOKEN}"}

    @pytest.mark.parametrize("plant", _UNITY_HOST_CONFIG)
    def test_operator_opt_in_keeps_todays_behaviour(self, home, workspace, monkeypatch, plant):
        """Positive control: what the pin prevents is real. Opted in, the host's
        settings replace the block's token, and the sink sees the host's material."""
        _needs("databricks.sdk")
        plant(monkeypatch, home)
        _list_tables(_args("unity", _unity_pat(workspace.url)), opted_in=True)
        assert _host_material_seen(workspace)
        assert f"Bearer {_CALLER_TOKEN}" not in _api_authorizations(workspace)

    def test_oauth_m2m_block_posts_only_the_callers_client(self, home, workspace, monkeypatch):
        """The block's own ``oauth-m2m`` was already explicit; this host's token
        and service principal stay out of it."""
        _needs("databricks.sdk")
        _env_oauth_m2m(monkeypatch, home)
        monkeypatch.setenv("DATABRICKS_AUTH_TYPE", "pat")
        monkeypatch.setenv("DATABRICKS_TOKEN", _HOST_TOKEN)
        block = {
            "host": workspace.url,
            "auth_method": "oauth_m2m",
            "oauth_client_id": _CALLER_CLIENT,
            "oauth_client_secret": _CALLER_CLIENT_VALUE,
        }
        out = _list_tables(_args("unity", block))
        assert out == {"tables": []}
        assert _host_material_seen(workspace) == []
        assert _token_posts(workspace) == [_basic(_CALLER_CLIENT, _CALLER_CLIENT_VALUE)]
        assert set(_api_authorizations(workspace)) == {f"Bearer {_MINTED}"}


class TestUnityClientArguments:
    """What the adapter hands ``WorkspaceClient``, in every lane, SDK or not."""

    def test_inline_pat_names_its_auth_type(self, home):
        adapter = _build_source_adapter(
            _args("unity", _unity_pat("https://ws.example.invalid")),
            allow_inline_credentials=True,
        )
        kwargs = adapter._credentials.to_connection_kwargs()
        assert kwargs["auth_type"] == "pat"
        assert kwargs["token"] == _CALLER_TOKEN

    def test_operator_opt_in_is_unchanged(self, home):
        adapter = _build_source_adapter(
            _args("unity", _unity_pat("https://ws.example.invalid")),
            allow_inline_credentials=True,
            allow_metadata_service=True,
        )
        assert "auth_type" not in adapter._credentials.to_connection_kwargs()

    def test_saved_source_is_unchanged(self, home, monkeypatch):
        monkeypatch.setenv("FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS", "1")
        sources = home / ".fluid" / "sources.yaml"
        sources.parent.mkdir()
        sources.write_text(
            "sources:\n  ws-saved:\n    source_type: unity\n    config:\n"
            "      host: https://ws.example.invalid\n      auth_method: pat\n"
            f"    secrets:\n      token: {_CALLER_TOKEN}\n",
            encoding="utf-8",
        )
        sources.chmod(0o600)
        adapter = _build_source_adapter(
            {"source": "unity", "credentials": {"credential_id": "ws-saved"}}
        )
        kwargs = adapter._credentials.to_connection_kwargs()
        assert kwargs == {"host": "https://ws.example.invalid", "token": _CALLER_TOKEN}

    def test_a_caller_cannot_unpin_its_block(self, home):
        """The pin is not a field: a key in the block cannot set or clear it."""
        block = {**_unity_pat("https://ws.example.invalid"), "_own_material_only": False}
        adapter = _build_source_adapter(_args("unity", block), allow_inline_credentials=True)
        assert adapter._credentials.to_connection_kwargs()["auth_type"] == "pat"

    def test_pinning_returns_a_copy(self):
        original = UnityCredentials.model_validate(_unity_pat("https://ws.example.invalid"))
        pinned = pin_to_own_material(original)
        assert pinned.model_dump() == original.model_dump()  # the pin is not a field
        assert pinned.to_connection_kwargs()["auth_type"] == "pat"
        assert "auth_type" not in original.to_connection_kwargs()


# ---------------------------------------------------------------------------
# Snowflake: the login request asks AWS who this host is.
# ---------------------------------------------------------------------------


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()
    return port


@pytest.fixture
def sts(home, monkeypatch) -> Iterator[AwsEndpointLogger]:
    """This host's AWS identity, its STS served on loopback; the login goes nowhere.

    ``HTTPS_PROXY`` names a closed port, so the connector's login request to the
    account's Snowflake host fails on this machine instead of leaving it.
    """
    logger = AwsEndpointLogger()
    point_boto3_at(logger, home, monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _HOST_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _HOST_KEY_VALUE)
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", logger.url)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    yield logger
    logger.close()


def _snowflake_password() -> Dict[str, Any]:
    return {
        "account": "acct",
        "user": "analyst",
        "auth_method": "password",
        "password": "caller-test-password",  # pragma: allowlist secret
    }


_SNOWFLAKE_BLOCKS = [
    pytest.param(_snowflake_password(), id="password"),
    pytest.param(
        {
            "account": "acct",
            "user": "analyst",
            "auth_method": "oauth",
            "oauth_token": _CALLER_TOKEN,
        },
        id="oauth",
    ),
]


class TestSnowflakeLogin:
    @pytest.mark.parametrize("block", _SNOWFLAKE_BLOCKS)
    def test_the_login_never_uses_this_hosts_aws_identity(self, sts, block):
        _needs("snowflake.connector", "boto3")
        with pytest.raises(Exception):
            _list_tables(_args("snowflake", block))
        assert sts.api_calls == []

    def test_operator_opt_in_keeps_todays_behaviour(self, sts):
        """Positive control: opted in, the connector signs an STS call as this host."""
        _needs("snowflake.connector", "boto3")
        with pytest.raises(Exception):
            _list_tables(_args("snowflake", _snowflake_password()), opted_in=True)
        assert _HOST_KEY_ID in sts.api_calls

    def test_inline_block_turns_platform_detection_off(self, home):
        """What the adapter hands ``snowflake.connector.connect``, in every lane."""
        adapter = _build_source_adapter(
            _args("snowflake", _snowflake_password()), allow_inline_credentials=True
        )
        kwargs = adapter._credentials.to_connection_kwargs()
        assert kwargs["platform_detection_timeout_seconds"] == 0.0

    def test_operator_opt_in_is_unchanged(self, home):
        adapter = _build_source_adapter(
            _args("snowflake", _snowflake_password()),
            allow_inline_credentials=True,
            allow_metadata_service=True,
        )
        assert "platform_detection_timeout_seconds" not in (
            adapter._credentials.to_connection_kwargs()
        )


# ---------------------------------------------------------------------------
# Glue: boto3 takes explicit keys as the whole credential (runs in every lane).
# ---------------------------------------------------------------------------


@dataclass
class AwsHost:
    endpoint: AwsEndpointLogger
    marker: Path  # created by the profile's credential_process when it runs


@pytest.fixture
def aws_host(home, monkeypatch) -> Iterator[AwsHost]:
    """Every AWS credential source this host could offer, each one observable.

    Keys and a session token in the environment, a profile (``AWS_PROFILE``)
    whose ``credential_process`` leaves a marker file, the container-credentials
    endpoint with its authorization token, and the instance-metadata endpoint;
    the last two and the Glue API are the loopback logger.
    """
    endpoint = AwsEndpointLogger()
    point_boto3_at(endpoint, home, monkeypatch)
    marker = home / "credential-process-ran"
    process = home / "credential-process.sh"
    process.write_text(
        "#!/bin/sh\n"
        f"touch '{marker}'\n"
        f'echo \'{{"Version": 1, "AccessKeyId": "{_HOST_KEY_ID}", '
        f'"SecretAccessKey": "{_HOST_KEY_VALUE}"}}\'\n',
        encoding="utf-8",
    )
    process.chmod(0o700)
    config = home / "aws-config"
    config.write_text(
        f"[profile host-profile]\nregion = us-east-1\ncredential_process = {process}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_PROFILE", "host-profile")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _HOST_KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _HOST_KEY_VALUE)
    monkeypatch.setenv("AWS_SESSION_TOKEN", _HOST_SESSION_TOKEN)
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", endpoint.url + "container-credentials")
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", _HOST_TOKEN)
    yield AwsHost(endpoint=endpoint, marker=marker)
    endpoint.close()


def _glue(**fields: Any) -> Dict[str, Any]:
    return {"region": "us-east-1", **fields}


class TestGlueKeyBlock:
    def test_signs_with_the_callers_key_alone(self, aws_host):
        _needs("boto3")
        block = _glue(
            auth_method="iam_key",
            access_key_id=_CALLER_KEY_ID,
            secret_access_key=_CALLER_KEY_VALUE,
        )
        assert _list_tables(_args("glue", block)) == {"tables": []}
        assert aws_host.endpoint.api_calls == [_CALLER_KEY_ID]
        # The host's session token never rides along with the caller's keys.
        assert aws_host.endpoint.session_tokens == [None]
        assert aws_host.endpoint.requests == []  # no container or instance metadata
        assert not aws_host.marker.exists()

    def test_operator_opt_in_reaches_the_hosts_identity(self, aws_host):
        """Positive control: the sink sees this host's key once the chain runs."""
        _needs("boto3")
        _list_tables(_args("glue", _glue(auth_method="instance_profile")), opted_in=True)
        assert aws_host.endpoint.api_calls == [_HOST_KEY_ID]
        assert aws_host.endpoint.session_tokens == [_HOST_SESSION_TOKEN]

    def test_operator_opt_in_runs_the_named_profiles_process(self, aws_host):
        """Positive control for the marker: a named profile runs its process."""
        _needs("boto3")
        _list_tables(
            _args("glue", _glue(auth_method="iam_role", profile_name="host-profile")),
            opted_in=True,
        )
        assert aws_host.marker.exists()
        assert aws_host.endpoint.api_calls == [_HOST_KEY_ID]


# ---------------------------------------------------------------------------
# DataHub and Data Mesh Manager: the block's token is the request's only login.
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint() -> Iterator[HttpAuthLogger]:
    logger = HttpAuthLogger()
    yield logger
    logger.close()


def _plant_datahub_host(mp: pytest.MonkeyPatch, home: Path, url: str) -> None:
    mp.setenv("DATAHUB_GMS_URL", url)
    mp.setenv("DATAHUB_GMS_TOKEN", _HOST_TOKEN)
    mp.setenv("DATAHUB_SYSTEM_CLIENT_ID", _HOST_SP)
    mp.setenv("DATAHUB_SYSTEM_CLIENT_SECRET", _HOST_SP_VALUE)
    (home / ".datahubenv").write_text(
        f"gms:\n  server: {url}\n  token: {_HOST_TOKEN}\n", encoding="utf-8"
    )


class TestDataHubPatBlock:
    def test_only_the_callers_token_is_sent(self, home, endpoint, monkeypatch):
        _needs("datahub")
        _plant_datahub_host(monkeypatch, home, endpoint.url)
        block = {"server": endpoint.url, "auth_method": "pat", "token": _CALLER_TOKEN}
        with pytest.raises(Exception):
            _list_tables(_args("datahub", block))
        assert endpoint.authorizations
        assert set(endpoint.authorizations) == {f"Bearer {_CALLER_TOKEN}"}
        assert _host_material_seen(endpoint) == []

    def test_operator_opt_in_reaches_the_hosts_system_login(self, home, endpoint, monkeypatch):
        """Positive control: a block with no token of its own gets the host's login."""
        _needs("datahub")
        _plant_datahub_host(monkeypatch, home, endpoint.url)
        with pytest.raises(Exception):
            _list_tables(
                _args("datahub", {"server": endpoint.url, "auth_method": "none"}), opted_in=True
            )
        assert f"Basic {_HOST_SP}:{_HOST_SP_VALUE}" in endpoint.authorizations


class TestDataMeshManagerBlock:
    def test_only_the_callers_key_is_sent(self, home, endpoint, monkeypatch):
        """httpx sends the header the adapter sets; this host's settings add nothing."""
        monkeypatch.setenv("DMM_API_URL", endpoint.url)
        monkeypatch.setenv("DMM_API_KEY", _HOST_TOKEN)
        monkeypatch.setenv("DATAMESH_MANAGER_API_KEY", _HOST_TOKEN)
        (home / ".netrc").write_text(
            f"machine 127.0.0.1 login {_HOST_USER} password {_HOST_PASSWORD}\n", encoding="utf-8"
        )
        (home / ".netrc").chmod(0o600)
        out = _list_tables(
            _args("datamesh_manager", {"server": endpoint.url, "api_key": _CALLER_TOKEN})
        )
        assert out == {"tables": []}
        assert endpoint.authorizations == [f"Bearer {_CALLER_TOKEN}"]


# ---------------------------------------------------------------------------
# BigQuery and Dataplex: no inline block reaches google-auth at all.
# ---------------------------------------------------------------------------


class TestGoogleBlocks:
    @pytest.mark.parametrize("source", ["bigquery", "dataplex"])
    @pytest.mark.parametrize("auth_method", ["adc", "oidc", "service_account_json"])
    def test_refused_and_no_host_credential_file_is_opened(
        self, home, monkeypatch, source, auth_method
    ):
        key = home / "mounted" / "sa.json"
        key.parent.mkdir()
        key.write_text('{"type": "service_account"}', encoding="utf-8")
        adc = home / "gcloud" / "application_default_credentials.json"
        adc.parent.mkdir()
        adc.write_text('{"type": "authorized_user"}', encoding="utf-8")
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
        monkeypatch.setenv("CLOUDSDK_CONFIG", str(adc.parent))
        block: Dict[str, Any] = {"project": "proj", "location": "eu", "auth_method": auth_method}
        if auth_method == "service_account_json":
            block["service_account_path"] = str(key)
        with watch_opens(key, adc) as opened:
            with pytest.raises(McpToolRefusal):
                _list_tables(_args(source, block))
        assert opened == []


# ---------------------------------------------------------------------------
# A real ``fluid mcp serve`` over stdio, with this host's settings in its env.
# ---------------------------------------------------------------------------


def _is_error(result: Any) -> bool:
    from fluid_build._mcp_compat import attr

    return attr(result, "is_error", "isError") is True


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


@dataclass
class StdioRun:
    # case -> (tool result, requests the workspace saw, its bodies, STS signers)
    seen: Dict[str, Tuple[Any, List[Any], List[str], List[Optional[str]]]]


def _stdio_run(server_flags: List[str], root: Path) -> StdioRun:
    """One real server; its environment carries this host's Databricks and AWS settings."""
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = root / "home"
    home.mkdir()
    workspace = HttpAuthLogger(answers=_workspace_answers)
    sts = AwsEndpointLogger()
    try:
        with pytest.MonkeyPatch.context() as mp:
            _isolate(mp, home)
            _env_oauth_m2m(mp, home)
            point_boto3_at(sts, home, mp)
            mp.setenv("AWS_DEFAULT_REGION", "us-east-1")
            mp.setenv("AWS_ACCESS_KEY_ID", _HOST_KEY_ID)
            mp.setenv("AWS_SECRET_ACCESS_KEY", _HOST_KEY_VALUE)
            mp.setenv("AWS_ENDPOINT_URL_STS", sts.url)
            mp.setenv("HTTPS_PROXY", f"http://127.0.0.1:{_closed_port()}")
            mp.setenv("NO_PROXY", "127.0.0.1,localhost")
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "fluid_build.cli", "mcp", "serve", *server_flags],
                env=dict(os.environ),
                cwd=str(home),
            )
            cases = {
                "unity-pat": _args("unity", _unity_pat(workspace.url)),
                "snowflake-password": _args("snowflake", _snowflake_password()),
            }

            async def _run() -> Dict[str, Tuple[Any, List[Any], List[str], List[Optional[str]]]]:
                seen: Dict[str, Tuple[Any, List[Any], List[str], List[Optional[str]]]] = {}
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        for case, arguments in cases.items():
                            workspace.clear()
                            sts.clear()
                            result = await client.call_tool("list_source_tables", arguments)
                            seen[case] = (
                                result,
                                list(workspace.requests),
                                list(workspace.bodies),
                                list(sts.api_calls),
                            )
                return seen

            return StdioRun(seen=asyncio.run(_run()))
    finally:
        workspace.close()
        sts.close()


@pytest.fixture(scope="module")
def stdio_inline_only(tmp_path_factory) -> StdioRun:
    return _stdio_run(["--allow-inline-credentials"], tmp_path_factory.mktemp("inline-only"))


@pytest.fixture(scope="module")
def stdio_opted_in(tmp_path_factory) -> StdioRun:
    return _stdio_run(
        ["--allow-inline-credentials", "--allow-metadata-service"],
        tmp_path_factory.mktemp("opted-in"),
    )


def _carries_host_material(requests: List[Any], bodies: List[str]) -> bool:
    texts = [seen[3] or "" for seen in requests] + bodies
    return any(material in text for text in texts for material in _HOST_MATERIAL)


class TestOverStdio:
    def test_unity_pat_sends_only_the_callers_token(self, stdio_inline_only):
        _needs("databricks.sdk")
        result, requests, bodies, _sts = stdio_inline_only.seen["unity-pat"]
        assert not _is_error(result), _text(result)
        assert not _carries_host_material(requests, bodies)
        api = [seen[3] for seen in requests if seen[2].startswith("/api/")]
        assert api and set(api) == {f"Bearer {_CALLER_TOKEN}"}

    def test_unity_opted_in_keeps_todays_behaviour(self, stdio_opted_in):
        """Positive control on the real server: the host's service principal posts."""
        _needs("databricks.sdk")
        _result, requests, bodies, _sts = stdio_opted_in.seen["unity-pat"]
        assert _basic(_HOST_SP, _HOST_SP_VALUE) in [seen[3] for seen in requests]
        assert _carries_host_material(requests, bodies)

    def test_snowflake_login_never_signs_as_this_host(self, stdio_inline_only):
        _needs("snowflake.connector", "boto3")
        _result, _requests, _bodies, signers = stdio_inline_only.seen["snowflake-password"]
        assert signers == []

    def test_snowflake_opted_in_keeps_todays_behaviour(self, stdio_opted_in):
        _needs("snowflake.connector", "boto3")
        _result, _requests, _bodies, signers = stdio_opted_in.seen["snowflake-password"]
        assert _HOST_KEY_ID in signers
