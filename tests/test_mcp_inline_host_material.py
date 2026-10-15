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

"""``--allow-inline-credentials`` alone never lets a caller use what this host holds.

An inline credential block whose credential is a path makes the SDK read that
file from this host's disk: gcloud ADC in ``~/.config/gcloud``, a
service-account key mounted under ``/var/run/secrets``, a Snowflake private key
in ``~/.snowflake``. The identity in the file is the host's, so such a block
needs the operator's ``--allow-metadata-service`` exactly like a block that
selects the instance role. The same holds for a login the host keeps for an
HTTP endpoint the caller names (``~/.netrc``) and for DataHub settings the SDK
reads from this host's environment.

Every test here drives the real MCP tool path with a fake ``HOME`` that holds
fake credential files, and watches those files with an audit hook
(:mod:`tests._infrastructure.file_read_sink`) or watches a loopback endpoint
(:mod:`tests._infrastructure.http_auth_logger`). Positive controls show the
sinks do see the SDK once the operator opts in; they need the catalog SDK and
skip without it.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest

from fluid_build.cli.mcp import _build_source_adapter, _call_tool
from fluid_build.cli.mcp.dispatch import McpToolRefusal
from tests._infrastructure.file_read_sink import child_env, read_child_log, watch_opens
from tests._infrastructure.http_auth_logger import HttpAuthLogger

_ENV = "FLUID_ALLOW_METADATA_SERVICE"
_REFUSED = "refused inline"

_CATALOG_TOOLS = (
    "list_source_tables",
    "inspect_source_table",
    "list_source_lineage",
    "list_source_glossary",
    "forge_from_source",
)

# Test values only: none of these is a credential anywhere.
_CALLER_TOKEN = "caller-test-token"  # pragma: allowlist secret
_NETRC_USER = "host-netrc-user"
_NETRC_VALUE = "host-netrc-test-value"  # pragma: allowlist secret
_NETRC_BASIC = "Basic " + base64.b64encode(f"{_NETRC_USER}:{_NETRC_VALUE}".encode()).decode()
_SYSTEM_ID = "host-system-client"
_SYSTEM_VALUE = "host-system-test-value"  # pragma: allowlist secret

_HOST_ENV_PREFIXES = ("DATAHUB_", "DATABRICKS_", "SNOWFLAKE_", "GOOGLE_", "CLOUDSDK_", "AWS_")


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def _needs(*modules: str) -> None:
    for module in modules:
        if not _has(module):
            pytest.skip(f"{module} is not installed")


@dataclass
class FakeHost:
    """A host's credential files, all fake. HOME is ``home``."""

    home: Path
    adc: Path  # gcloud ADC: ~/.config/gcloud/application_default_credentials.json
    mounted_key: Path  # a service-account key mounted outside HOME, like /var/run/secrets/...
    pem: Path  # a Snowflake private key: ~/.snowflake/rsa_key.p8
    link: Path  # ~/key.json, a symlink to the mounted key
    # The workspace a Unity block names: a loopback endpoint, so a call that
    # gets past the gate stays on this machine.
    workspace_url: str = ""

    @property
    def files(self) -> Tuple[Path, ...]:
        return (self.adc, self.mounted_key, self.pem)


def _make_host(root: Path) -> FakeHost:
    home = root / "home"
    adc = home / ".config" / "gcloud" / "application_default_credentials.json"
    adc.parent.mkdir(parents=True)
    adc.write_text('{"type": "authorized_user"}', encoding="utf-8")
    mounted_key = root / "run" / "secrets" / "sa.json"
    mounted_key.parent.mkdir(parents=True)
    mounted_key.write_text('{"type": "service_account"}', encoding="utf-8")
    pem = home / ".snowflake" / "rsa_key.p8"
    pem.parent.mkdir()
    pem.write_text("not-a-key\n", encoding="utf-8")
    link = home / "key.json"
    link.symlink_to(mounted_key)
    return FakeHost(home=home, adc=adc, mounted_key=mounted_key, pem=pem, link=link)


def _write_netrc(path: Path, machine: str) -> None:
    path.write_text(
        f"machine {machine} login {_NETRC_USER} password {_NETRC_VALUE}\n", encoding="utf-8"
    )
    path.chmod(0o600)


def _isolate(monkeypatch: pytest.MonkeyPatch, host: FakeHost) -> None:
    """Give this process ``host`` as its only HOME and no catalog settings."""
    for key in list(os.environ):
        if key.startswith(_HOST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.delenv("NETRC", raising=False)
    monkeypatch.setenv("HOME", str(host.home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.null.Keyring")
    monkeypatch.chdir(host.home)


@pytest.fixture
def host(tmp_path, monkeypatch) -> Iterator[FakeHost]:
    fake = _make_host(tmp_path)
    _isolate(monkeypatch, fake)
    monkeypatch.setattr(
        "fluid_build.copilot.catalog.credentials.SOURCES_CONFIG_DEFAULT",
        fake.home / ".fluid" / "sources.yaml",
    )
    workspace = HttpAuthLogger()
    fake.workspace_url = workspace.url
    yield fake
    workspace.close()


@pytest.fixture
def endpoint() -> Iterator[HttpAuthLogger]:
    logger = HttpAuthLogger()
    yield logger
    logger.close()


@pytest.fixture
def redirecting_endpoint() -> Iterator[HttpAuthLogger]:
    """Redirects every request on ``127.0.0.1`` to the same server as ``localhost``."""
    logger = HttpAuthLogger(redirect=True)
    yield logger
    logger.close()


# A scope every adapter accepts far enough to reach its SDK.
_SCOPE = {"database": "analytics", "schema_name": "sales", "catalog": "main", "tables": ["orders"]}


def _args(source: str, inline: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    return {"source": source, "credentials": {"inline": inline}, "scope": dict(_SCOPE), **extra}


def _bigquery(path: str) -> Dict[str, Any]:
    return {"project": "proj", "auth_method": "service_account_json", "service_account_path": path}


def _dataplex(path: str) -> Dict[str, Any]:
    return {
        "project": "proj",
        "location": "eu",
        "auth_method": "service_account_json",
        "service_account_path": path,
    }


def _snowflake(path: str) -> Dict[str, Any]:
    return {
        "account": "acct",
        "user": "analyst",
        "auth_method": "private_key",
        "private_key_path": path,
    }


def _unity_google_id(path: str, host_url: str) -> Dict[str, Any]:
    return {"host": host_url, "auth_method": "google_id", "google_service_account_path": path}


@dataclass
class PathCase:
    source: str
    field: str
    build: Callable[[FakeHost], Dict[str, Any]]


# Each way an inline block can name a credential file this host holds.
_PATH_CASES: Dict[str, PathCase] = {
    "bigquery-mounted-key-absolute": PathCase(
        "bigquery", "service_account_path", lambda h: _bigquery(str(h.mounted_key))
    ),
    "bigquery-adc-tilde": PathCase(
        "bigquery",
        "service_account_path",
        lambda h: _bigquery("~/.config/gcloud/application_default_credentials.json"),
    ),
    "bigquery-adc-env-var": PathCase(
        "bigquery",
        "service_account_path",
        lambda h: _bigquery("$HOME/.config/gcloud/application_default_credentials.json"),
    ),
    "bigquery-adc-relative": PathCase(
        "bigquery",
        "service_account_path",
        lambda h: _bigquery(".config/gcloud/application_default_credentials.json"),
    ),
    "bigquery-mounted-key-dot-dot": PathCase(
        "bigquery", "service_account_path", lambda h: _bigquery("../run/secrets/sa.json")
    ),
    "bigquery-symlink": PathCase(
        "bigquery", "service_account_path", lambda h: _bigquery(str(h.link))
    ),
    "dataplex-mounted-key-absolute": PathCase(
        "dataplex", "service_account_path", lambda h: _dataplex(str(h.mounted_key))
    ),
    "dataplex-adc-tilde": PathCase(
        "dataplex",
        "service_account_path",
        lambda h: _dataplex("~/.config/gcloud/application_default_credentials.json"),
    ),
    "snowflake-key-tilde": PathCase(
        "snowflake", "private_key_path", lambda h: _snowflake("~/.snowflake/rsa_key.p8")
    ),
    "snowflake-key-absolute": PathCase(
        "snowflake", "private_key_path", lambda h: _snowflake(str(h.pem))
    ),
    "snowflake-key-relative": PathCase(
        "snowflake", "private_key_path", lambda h: _snowflake(".snowflake/rsa_key.p8")
    ),
    "unity-google_id": PathCase(
        "unity",
        "google_service_account_path",
        lambda h: _unity_google_id(str(h.mounted_key), h.workspace_url),
    ),
    # A path beside material of the caller's own still names a host file.
    "unity-pat-plus-path": PathCase(
        "unity",
        "google_service_account_path",
        lambda h: {
            "host": h.workspace_url,
            "auth_method": "pat",
            "token": _CALLER_TOKEN,
            "google_service_account_path": str(h.adc),
        },
    ),
}


def _refusal(exc: BaseException) -> str:
    text = str(exc)
    assert "--allow-metadata-service" in text and _ENV in text, text
    assert "credential_id" in text, text
    return text


class TestInlinePathIsRefused:
    @pytest.mark.parametrize("case", list(_PATH_CASES))
    def test_refused_and_the_file_is_never_opened(self, host, case):
        spec = _PATH_CASES[case]
        with watch_opens(*host.files) as opened:
            with pytest.raises(McpToolRefusal) as exc:
                _call_tool(
                    "list_source_tables",
                    _args(spec.source, spec.build(host)),
                    read_only=True,
                    allow_inline_credentials=True,
                )
        assert opened == []
        assert spec.field in _refusal(exc.value)

    @pytest.mark.parametrize("case", ["bigquery-adc-tilde", "snowflake-key-tilde"])
    def test_the_clients_own_request_does_not_stand_in_for_the_operator(self, host, case):
        spec = _PATH_CASES[case]
        with watch_opens(*host.files) as opened:
            with pytest.raises(McpToolRefusal):
                _call_tool(
                    "list_source_tables",
                    _args(spec.source, spec.build(host), allow_metadata_service=True),
                    read_only=True,
                    allow_inline_credentials=True,
                )
        assert opened == []

    @pytest.mark.parametrize("tool", _CATALOG_TOOLS)
    def test_every_catalog_tool(self, host, tool):
        args = _args("bigquery", _bigquery(str(host.mounted_key)))
        args.update(fqn="proj.sales.orders", output_path=str(host.home / "out.fluid.yaml"))
        with watch_opens(*host.files) as opened:
            with pytest.raises(McpToolRefusal):
                _call_tool(tool, args, read_only=False, allow_inline_credentials=True)
        assert opened == []

    @pytest.mark.parametrize("source", ["bigquery", "dataplex"])
    def test_google_refusal_says_there_is_no_inline_form(self, host, source):
        block = _bigquery(str(host.adc)) if source == "bigquery" else _dataplex(str(host.adc))
        with pytest.raises(McpToolRefusal) as exc:
            _build_source_adapter(_args(source, block), allow_inline_credentials=True)
        assert "file path" in _refusal(exc.value)

    def test_snowflake_refusal_names_the_value_forms(self, host):
        with pytest.raises(McpToolRefusal) as exc:
            _build_source_adapter(
                _args("snowflake", _snowflake(str(host.pem))), allow_inline_credentials=True
            )
        text = _refusal(exc.value)
        assert "'password'" in text and "oauth_token" in text


# (case, the file the SDK opens, the SDK modules the read needs)
_OPT_IN_READS = [
    pytest.param(
        "bigquery-mounted-key-absolute", "mounted_key", ("google.cloud.bigquery",), id="bq"
    ),
    pytest.param("bigquery-adc-tilde", "adc", ("google.cloud.bigquery",), id="bq-adc-tilde"),
    pytest.param("bigquery-symlink", "mounted_key", ("google.cloud.bigquery",), id="bq-symlink"),
    pytest.param(
        "dataplex-mounted-key-absolute", "mounted_key", ("google.cloud.dataplex_v1",), id="dataplex"
    ),
    pytest.param("snowflake-key-tilde", "pem", ("snowflake.connector",), id="snowflake"),
]


class TestOperatorOptInKeepsTodaysBehaviour:
    """With ``--allow-metadata-service`` the same blocks pass the gate as before."""

    @pytest.mark.parametrize("case", list(_PATH_CASES))
    def test_the_gate_lets_it_through(self, host, case):
        spec = _PATH_CASES[case]
        try:
            _build_source_adapter(
                _args(spec.source, spec.build(host)),
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        except McpToolRefusal as exc:  # pragma: no cover - the failure being tested for
            pytest.fail(f"opt-in still refused: {exc}")

    def test_the_operator_env_var_counts(self, host, monkeypatch):
        monkeypatch.setenv(_ENV, "1")
        adapter = _build_source_adapter(
            _args("bigquery", _bigquery(str(host.mounted_key))), allow_inline_credentials=True
        )
        assert adapter is not None

    @pytest.mark.parametrize(("case", "attr", "modules"), _OPT_IN_READS)
    def test_the_sdk_then_reads_the_hosts_file(self, host, case, attr, modules):
        """Positive control for the sink: it does see the SDK's read."""
        _needs(*modules)
        spec = _PATH_CASES[case]
        with watch_opens(*host.files) as opened:
            with pytest.raises(Exception):
                _call_tool(
                    "list_source_tables",
                    _args(spec.source, spec.build(host)),
                    read_only=True,
                    allow_inline_credentials=True,
                    allow_metadata_service=True,
                )
        assert os.path.realpath(getattr(host, attr)) in opened

    def test_unity_google_id_reads_the_hosts_gcloud_adc(self, host, endpoint):
        """``google_id`` impersonates through this host's ADC, as classified."""
        _needs("databricks.sdk", "google.auth")
        block = _unity_google_id("sa@proj.iam.gserviceaccount.com", host_url=endpoint.url)
        with watch_opens(*host.files) as opened:
            with pytest.raises(Exception):
                _call_tool(
                    "list_source_tables",
                    _args("unity", block),
                    read_only=True,
                    allow_inline_credentials=True,
                    allow_metadata_service=True,
                )
        assert os.path.realpath(host.adc) in opened


class TestValueMaterialAndSavedSources:
    def test_inline_value_material_is_accepted(self, host):
        """Snowflake takes its credential as a value, so that form needs no opt-in."""
        adapter = _build_source_adapter(
            _args(
                "snowflake",
                {
                    "account": "acct",
                    "user": "analyst",
                    "auth_method": "password",
                    "password": "test-password",  # pragma: allowlist secret
                },
            ),
            allow_inline_credentials=True,
        )
        assert adapter is not None

    def _save_bigquery_source(self, host: FakeHost) -> None:
        sources = host.home / ".fluid" / "sources.yaml"
        sources.parent.mkdir(exist_ok=True)
        sources.write_text(
            "sources:\n"
            "  bq-saved:\n"
            "    source_type: bigquery\n"
            "    config:\n"
            "      project: proj\n"
            "      auth_method: service_account_json\n"
            f"      service_account_path: {host.mounted_key}\n",
            encoding="utf-8",
        )
        sources.chmod(0o600)

    def test_a_saved_source_may_name_a_host_file(self, host):
        """The operator configured it, so its path is the operator's choice."""
        self._save_bigquery_source(host)
        adapter = _build_source_adapter(
            {"source": "bigquery", "credentials": {"credential_id": "bq-saved"}}
        )
        assert adapter._credentials.service_account_path == host.mounted_key

    def test_a_saved_source_reaches_its_file(self, host):
        _needs("google.cloud.bigquery")
        self._save_bigquery_source(host)
        with watch_opens(*host.files) as opened:
            with pytest.raises(Exception):
                _call_tool(
                    "list_source_tables",
                    {
                        "source": "bigquery",
                        "credentials": {"credential_id": "bq-saved"},
                        "scope": _SCOPE,
                    },
                    read_only=True,
                )
        assert os.path.realpath(host.mounted_key) in opened


def _datahub(server: str, **extra: Any) -> Dict[str, Any]:
    return {"server": server, "auth_method": "pat", "token": _CALLER_TOKEN, **extra}


def _unity_pat(host_url: str) -> Dict[str, Any]:
    return {"host": host_url, "auth_method": "pat", "token": _CALLER_TOKEN}


def _unity_m2m(host_url: str) -> Dict[str, Any]:
    return {
        "host": host_url,
        "auth_method": "oauth_m2m",
        "oauth_client_id": "test-client",
        "oauth_client_secret": "test-client-value",  # pragma: allowlist secret
    }


_NAMED_ENDPOINTS = [
    pytest.param("datahub", _datahub, id="datahub-pat"),
    pytest.param("unity", _unity_pat, id="unity-pat"),
    pytest.param("unity", _unity_m2m, id="unity-oauth_m2m"),
]


class TestHostNetrcLogin:
    """``requests`` attaches this host's netrc login to a server the caller names."""

    @pytest.mark.parametrize(("source", "block"), _NAMED_ENDPOINTS)
    @pytest.mark.parametrize("where", ["home-.netrc", "home-_netrc", "NETRC"])
    def test_refused_and_nothing_reaches_the_server(
        self, host, endpoint, monkeypatch, source, block, where
    ):
        if where == "NETRC":
            path = host.home.parent / "netrc-elsewhere"
            monkeypatch.setenv("NETRC", str(path))
        else:
            path = host.home / where.split("-", 1)[1]
        _write_netrc(path, "127.0.0.1")
        with pytest.raises(McpToolRefusal) as exc:
            _call_tool(
                "list_source_tables",
                _args(source, block(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert "netrc" in _refusal(exc.value)
        assert str(host.home) not in str(exc.value)
        assert endpoint.requests == []

    def test_a_redirect_target_is_covered_too(self, host, redirecting_endpoint):
        """The login for the host a server redirects to is the case a URL check misses."""
        _write_netrc(host.home / ".netrc", "localhost")
        with pytest.raises(McpToolRefusal):
            _call_tool(
                "list_source_tables",
                _args("unity", _unity_pat(redirecting_endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert redirecting_endpoint.requests == []

    def test_without_a_netrc_the_callers_token_is_sent(self, host, endpoint):
        """Positive control: the server sees the SDK, carrying only the caller's token."""
        _needs("datahub")
        with pytest.raises(Exception):
            _call_tool(
                "list_source_tables",
                _args("datahub", _datahub(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert endpoint.authorizations
        assert set(endpoint.authorizations) == {f"Bearer {_CALLER_TOKEN}"}

    def test_operator_opt_in_keeps_todays_behaviour(self, host, endpoint):
        """Positive control: what the refusal prevents is real. Opted in, the host's
        netrc login replaces the caller's token on the wire."""
        _needs("datahub")
        _write_netrc(host.home / ".netrc", "127.0.0.1")
        with pytest.raises(Exception):
            _call_tool(
                "list_source_tables",
                _args("datahub", _datahub(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert _NETRC_BASIC in endpoint.authorizations

    def test_operator_opt_in_redirect_carries_the_hosts_login(self, host, redirecting_endpoint):
        """Positive control for the redirect channel, through the Databricks SDK."""
        _needs("databricks.sdk")
        _write_netrc(host.home / ".netrc", "localhost")
        try:
            _call_tool(
                "list_source_tables",
                _args("unity", _unity_pat(redirecting_endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        except Exception:
            pass  # the loopback answers {} to every request; only the headers matter
        redirected = [
            seen for seen in redirecting_endpoint.requests if seen[1].startswith("localhost")
        ]
        assert redirected and all(seen[3] == _NETRC_BASIC for seen in redirected)

    @pytest.mark.parametrize(
        ("block", "azure", "direct_below"),
        [
            pytest.param(_unity_pat, False, None, id="pat"),
            pytest.param(_unity_m2m, False, (0, 36), id="oauth_m2m"),
            pytest.param(_unity_m2m, True, (0, 87), id="oauth_m2m-azure-client-id"),
        ],
    )
    def test_operator_opt_in_named_host_login_follows_the_documented_sdk_range(
        self, host, endpoint, monkeypatch, block, azure, direct_below
    ):
        """Positive control for the databricks-sdk ranges docs/MCP.md item 5 gives.

        The SDK sends its API calls through a session whose ``auth`` it sets, so
        a ``pat`` block never carries the named host's netrc login there. An
        ``oauth_m2m`` block's endpoint lookup goes to the named host with no
        authentication of its own, so ``requests`` adds that login, in
        databricks-sdk 0.20 to 0.35 (a plain ``requests.get`` in
        ``Config.oidc_endpoints``) and in 0.36 to 0.86 on a host the SDK treats as
        Azure while it has an Azure client id
        (``get_azure_entra_id_workspace_endpoints``). Skips without the SDK; run
        it once per installed version.
        """
        _needs("databricks.sdk")
        from databricks.sdk.version import __version__ as sdk_version

        monkeypatch.delenv("ARM_CLIENT_ID", raising=False)
        if azure:
            # Fake values. The resource id makes the SDK treat any host as Azure.
            monkeypatch.setenv("ARM_CLIENT_ID", "host-azure-client-id")
            monkeypatch.setenv(
                "DATABRICKS_AZURE_RESOURCE_ID",
                "/subscriptions/s/resourceGroups/g/providers/Microsoft.Databricks/workspaces/w",
            )
        _write_netrc(host.home / ".netrc", "127.0.0.1")
        try:
            _call_tool(
                "list_source_tables",
                _args("unity", block(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        except Exception:
            pass  # the loopback answers {} to every request; only the headers matter
        assert endpoint.requests, f"databricks-sdk {sdk_version} never reached the named host"
        major, minor = (int(part) for part in sdk_version.split(".")[:2])
        direct = direct_below is not None and (major, minor) < direct_below
        assert (_NETRC_BASIC in endpoint.authorizations) is direct, (
            sdk_version,
            endpoint.requests,
        )

    def test_docs_give_the_sdk_range_where_unity_gets_the_login_directly(self):
        """The Unity sentence holds for every databricks-sdk pyproject allows (>= 0.20):
        it names the versions whose lookup carries the login to the named host, not
        only a redirect target (the test above pins those ranges)."""
        from fluid_build.copilot.catalog.credentials import host_identity_reason

        docs = (Path(__file__).resolve().parents[1] / "docs" / "MCP.md").read_text("utf-8")
        for text in (docs, host_identity_reason.__doc__ or ""):
            flat = " ".join(text.split())
            assert "only when a request is redirected" not in flat
            assert "databricks-sdk 0.20 to 0.35" in flat
            assert "0.36 to 0.86" in flat
            assert "ARM_CLIENT_ID" in flat

    def test_data_mesh_manager_never_sends_the_hosts_login(self, host, endpoint):
        """Its ``httpx`` client applies netrc only when asked to, so it is not refused."""
        _write_netrc(host.home / ".netrc", "127.0.0.1")
        out = _call_tool(
            "list_source_tables",
            _args("datamesh_manager", {"server": endpoint.url, "api_key": _CALLER_TOKEN}),
            read_only=True,
            allow_inline_credentials=True,
        )
        assert out == {"tables": []}
        assert endpoint.authorizations == [f"Bearer {_CALLER_TOKEN}"]


class TestDataHubHostConfiguration:
    def test_from_env_server_is_refused(self, host, endpoint, monkeypatch):
        """``__from_env__`` makes the SDK use this host's DATAHUB_GMS_* settings."""
        monkeypatch.setenv("DATAHUB_GMS_URL", endpoint.url)
        monkeypatch.setenv("DATAHUB_GMS_TOKEN", "host-gms-test-token")
        with pytest.raises(McpToolRefusal) as exc:
            _call_tool(
                "list_source_tables",
                _args("datahub", _datahub("__from_env__")),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert "server" in _refusal(exc.value)
        assert endpoint.requests == []

    def _oauth_block(self, server: str) -> Dict[str, Any]:
        return {
            "server": server,
            "auth_method": "oauth",
            "oauth_client_id": "caller-client",
            "oauth_client_secret": "caller-client-test-value",  # pragma: allowlist secret
        }

    def test_oauth_is_refused(self, host, endpoint, monkeypatch):
        monkeypatch.setenv("DATAHUB_SYSTEM_CLIENT_ID", _SYSTEM_ID)
        monkeypatch.setenv("DATAHUB_SYSTEM_CLIENT_SECRET", _SYSTEM_VALUE)
        with pytest.raises(McpToolRefusal) as exc:
            _call_tool(
                "list_source_tables",
                _args("datahub", self._oauth_block(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert "oauth" in _refusal(exc.value)
        assert endpoint.requests == []

    def test_oauth_with_opt_in_sends_the_hosts_system_login(self, host, endpoint, monkeypatch):
        """Positive control: why ``oauth`` is the host's. The caller's client
        credentials never reach the wire; this host's system login does."""
        _needs("datahub")
        monkeypatch.setenv("DATAHUB_SYSTEM_CLIENT_ID", _SYSTEM_ID)
        monkeypatch.setenv("DATAHUB_SYSTEM_CLIENT_SECRET", _SYSTEM_VALUE)
        with pytest.raises(Exception):
            _call_tool(
                "list_source_tables",
                _args("datahub", self._oauth_block(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
                allow_metadata_service=True,
            )
        assert endpoint.authorizations
        assert set(endpoint.authorizations) == {f"Basic {_SYSTEM_ID}:{_SYSTEM_VALUE}"}

    def test_the_hosts_client_certificate_is_refused(self, host, endpoint, monkeypatch):
        cert = host.home / ".datahub" / "client.pem"
        monkeypatch.setenv("DATAHUB_CLIENT_CERT_PATH", str(cert))
        with pytest.raises(McpToolRefusal) as exc:
            _call_tool(
                "list_source_tables",
                _args("datahub", _datahub(endpoint.url)),
                read_only=True,
                allow_inline_credentials=True,
            )
        assert "DATAHUB_CLIENT_CERT_PATH" in _refusal(exc.value)
        assert endpoint.requests == []


# ---------------------------------------------------------------------------
# A real ``fluid mcp serve`` over stdio, one call per provider.
# ---------------------------------------------------------------------------


@dataclass
class StdioRun:
    host: FakeHost
    sources: Path
    # case -> (tool result, files the server opened, requests the endpoint saw)
    seen: Dict[str, Tuple[Any, List[str], List[Any]]]


def _stdio_cases(host: FakeHost, url: str) -> Dict[str, Dict[str, Any]]:
    cases: Dict[str, Dict[str, Any]] = {
        name: _args(spec.source, spec.build(host)) for name, spec in _PATH_CASES.items()
    }
    cases.update(
        {
            "datahub-netrc": _args("datahub", _datahub(url)),
            "unity-netrc": _args("unity", _unity_pat(url)),
            "datahub-from-env": _args("datahub", _datahub("__from_env__")),
            "datahub-oauth": _args(
                "datahub",
                {
                    "server": url,
                    "auth_method": "oauth",
                    "oauth_client_id": "caller-client",
                    "oauth_client_secret": "caller-client-test-value",  # pragma: allowlist secret
                },
            ),
            # Controls: neither is refused, in either session.
            "control-dmm": _args("datamesh_manager", {"server": url, "api_key": _CALLER_TOKEN}),
            "control-saved-source": {
                "source": "bigquery",
                "credentials": {"credential_id": "bq-saved"},
                "scope": dict(_SCOPE),
            },
        }
    )
    return cases


_STDIO_REFUSED = [*_PATH_CASES, "datahub-netrc", "unity-netrc", "datahub-from-env", "datahub-oauth"]


def _stdio_run(server_flags: List[str], root: Path) -> StdioRun:
    """Make every :func:`_stdio_cases` call on one real ``fluid mcp serve``.

    The server's HOME holds the fake credential files, a netrc login for the
    loopback endpoint, and a saved BigQuery source naming the mounted key; its
    environment carries DataHub settings pointing at the endpoint.
    """
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    host = _make_host(root)
    endpoint = HttpAuthLogger()
    host.workspace_url = endpoint.url
    _write_netrc(host.home / ".netrc", "127.0.0.1")
    sources = host.home / ".fluid" / "sources.yaml"
    sources.parent.mkdir()
    sources.write_text(
        "sources:\n  bq-saved:\n    source_type: bigquery\n    config:\n"
        "      project: proj\n      auth_method: service_account_json\n"
        f"      service_account_path: {host.mounted_key}\n",
        encoding="utf-8",
    )
    sources.chmod(0o600)
    log = root / "opened.log"
    try:
        with pytest.MonkeyPatch.context() as mp:
            _isolate(mp, host)
            mp.setenv("DATAHUB_GMS_URL", endpoint.url)
            mp.setenv("DATAHUB_GMS_TOKEN", "host-gms-test-token")
            mp.setenv("DATAHUB_SYSTEM_CLIENT_ID", _SYSTEM_ID)
            mp.setenv("DATAHUB_SYSTEM_CLIENT_SECRET", _SYSTEM_VALUE)
            # sources.yaml too: the saved-source control reads it without any
            # catalog SDK, which shows the child's sink works in every lane.
            env = child_env(dict(os.environ), root / "hook", log, *host.files, sources)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "fluid_build.cli", "mcp", "serve", *server_flags],
                env=env,
                cwd=str(host.home),
            )

            async def _run() -> Dict[str, Tuple[Any, List[str], List[Any]]]:
                seen: Dict[str, Tuple[Any, List[str], List[Any]]] = {}
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        for case, arguments in _stdio_cases(host, endpoint.url).items():
                            log.write_text("", encoding="utf-8")
                            endpoint.clear()
                            result = await client.call_tool("list_source_tables", arguments)
                            seen[case] = (result, read_child_log(log), list(endpoint.requests))
                return seen

            return StdioRun(host=host, sources=sources, seen=asyncio.run(_run()))
    finally:
        endpoint.close()


@pytest.fixture(scope="module")
def stdio_inline_only(tmp_path_factory) -> StdioRun:
    return _stdio_run(["--allow-inline-credentials"], tmp_path_factory.mktemp("inline-only"))


@pytest.fixture(scope="module")
def stdio_opted_in(tmp_path_factory) -> StdioRun:
    return _stdio_run(
        ["--allow-inline-credentials", "--allow-metadata-service"],
        tmp_path_factory.mktemp("opted-in"),
    )


def _is_error(result: Any) -> bool:
    from fluid_build._mcp_compat import attr

    return attr(result, "is_error", "isError") is True


def _text(result: Any) -> str:
    return " ".join(getattr(block, "text", "") for block in result.content)


class TestOverStdio:
    """The same holds on the real server: refused, no file opened, nothing on the wire."""

    @pytest.mark.parametrize("case", _STDIO_REFUSED)
    def test_inline_only_refuses(self, stdio_inline_only, case):
        result, opened, requests = stdio_inline_only.seen[case]
        assert _is_error(result)
        assert _REFUSED in _text(result) and "--allow-metadata-service" in _text(result)
        assert opened == []
        assert requests == []

    @pytest.mark.parametrize("session", ["stdio_inline_only", "stdio_opted_in"])
    def test_controls_are_not_refused(self, request, session):
        run: StdioRun = request.getfixturevalue(session)
        result, _opened, requests = run.seen["control-dmm"]
        assert not _is_error(result), _text(result)
        assert [seen[3] for seen in requests] == [f"Bearer {_CALLER_TOKEN}"]
        saved, opened, _requests = run.seen["control-saved-source"]
        assert _REFUSED not in _text(saved)
        assert os.path.realpath(run.sources) in opened  # the child's sink is live

    @pytest.mark.parametrize("case", _STDIO_REFUSED)
    def test_opted_in_passes_the_gate(self, stdio_opted_in, case):
        result, _opened, _requests = stdio_opted_in.seen[case]
        assert _REFUSED not in _text(result)

    @pytest.mark.parametrize(
        ("case", "attr", "module"),
        [
            ("bigquery-mounted-key-absolute", "mounted_key", "google.cloud.bigquery"),
            ("bigquery-adc-tilde", "adc", "google.cloud.bigquery"),
            ("dataplex-mounted-key-absolute", "mounted_key", "google.cloud.dataplex_v1"),
            ("snowflake-key-tilde", "pem", "snowflake.connector"),
            ("control-saved-source", "mounted_key", "google.cloud.bigquery"),
        ],
    )
    def test_opted_in_sdk_reads_the_file(self, stdio_opted_in, case, attr, module):
        """Positive control for the child-process sink."""
        _needs(module)
        _result, opened, _requests = stdio_opted_in.seen[case]
        assert os.path.realpath(getattr(stdio_opted_in.host, attr)) in opened

    def test_opted_in_datahub_sends_the_hosts_netrc_login(self, stdio_opted_in):
        """Positive control for the endpoint sink, on the real server."""
        _needs("datahub")
        _result, _opened, requests = stdio_opted_in.seen["datahub-netrc"]
        assert _NETRC_BASIC in [seen[3] for seen in requests]
