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

"""A project dotenv file cannot point an operator credential at its own host.

An SDK pairs the endpoint it reads from the environment (``OPENAI_BASE_URL``,
``PGHOST``, ``SNOWFLAKE_ACCOUNT`` ...) with the credential it reads from there
too. When the credential is the operator's (exported in the shell or CI, or in
their ``FLUID_SECRETS_FILE``, or held in a keyring or stored login) and the
endpoint comes from the project's ``.env``, the operator's credential goes to
the host the project chose. ``credentials.protected_env.unpaired_endpoints``
refuses the endpoint in that case, and only then: a project that supplies its
own credential for its own endpoint still works, and an endpoint the operator
exported always wins.

The SDK tests drive the real client against a loopback listener and send the
request only when the client resolved the listener as its target, so a green
run never touches the network and a red run shows the leaked header.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import (  # noqa: E402
    DotEnvCredentialStore,
    project_endpoint_in_effect,
    reset_dotenv_load_log,
)
from fluid_build.credentials.protected_env import (  # noqa: E402
    ENDPOINT_CREDENTIAL_GROUPS,
    EndpointGroup,
    unpaired_endpoints,
)

OPERATOR_KEY = "operator-test-key-0451"  # pragma: allowlist secret
PROJECT_KEY = "project-test-key-7a2c"  # pragma: allowlist secret
_SNOWFLAKE_TARGET = "SNOWFLAKE_ACCOUNT=project-acct\n"  # pragma: allowlist secret

_EXTRA_KEYS = (
    "FLUID_SECRETS_FILE",
    "FLUID_ENV",
    "FLUID_LLM_API_KEY",
    "ACME_API_BASE",
    "ACME_API_KEY",
    "ACME_BASE_URL",
    "ACME_HOST",
    "ACME_DB_PASSWORD",
    "OTHER_API_KEY",
    "USE_LITELLM_PROXY",
    "LITELLM_PROXY_API_BASE",
    "LITELLM_PROXY_API_KEY",
    "PGUSER",
    "PGDATABASE",
    "POSTGRES_USER",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "REDSHIFT_USER",
    "REDSHIFT_USE_IAM",
    "REDSHIFT_IAM_PROFILE",
    "REDSHIFT_CLUSTER_ID",
    "REDSHIFT_SERVERLESS_WORKGROUP",
    "AWS_PROFILE",
    "DATABRICKS_HTTP_PATH",
    "OPENAI_API_VERSION",
    "LITELLM_LOCAL_MODEL_COST_MAP",
)


def _group_names() -> List[str]:
    names: List[str] = []
    for group in ENDPOINT_CREDENTIAL_GROUPS:
        pairing = tuple(n for path in group.paired_by for n in path)
        names.extend(
            n
            for n in group.endpoints + group.credentials + pairing + group.switches
            if "*" not in n and "{" not in n
        )
    return names


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # ``credentials.get_snowflake_adapter`` caches one adapter per process,
    # with the project root of its first caller and every value it resolved
    # (the keyring password of the Snowflake test below included). Left in
    # place, a later test's Snowflake lookups read this test's project and
    # its cached password.
    import fluid_build.credentials as credentials_pkg

    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    for key in list(os.environ):
        upper = key.upper()
        if upper in _group_names() or upper in _EXTRA_KEYS or "CREDENTIALS__" in upper:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # litellm fetches its model price map over the network on import otherwise
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    reset_dotenv_load_log()
    before = dict(os.environ)
    yield
    # The store writes os.environ directly, which monkeypatch never saw.
    for key in set(os.environ) - set(before):
        del os.environ[key]
    reset_dotenv_load_log()


# ── a loopback HTTP listener that records what each request carried ───────


class _Listener:
    def __init__(self) -> None:
        self.requests: List[Tuple[str, str, Dict[str, str]]] = []
        seen = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                seen.append((self.command, self.path, dict(self.headers.items())))
                body = json.dumps({"error": {"message": "loopback", "type": "auth"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _record

            def log_message(self, *args: Any) -> None:  # silence stderr
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def saw(self, secret: str) -> List[str]:
        """The requests that carried ``secret`` in the URL or a header."""
        return [
            f"{method} {path}"
            for method, path, headers in self.requests
            if secret in path or any(secret in v for v in headers.values())
        ]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def listener() -> Iterator[_Listener]:
    server = _Listener()
    yield server
    server.close()


@pytest.fixture
def operator_listener() -> Iterator[_Listener]:
    server = _Listener()
    yield server
    server.close()


def _send_if_loopback(target: Optional[str], send: Callable[[], Any]) -> None:
    """Send only when the SDK resolved a loopback target; never the network."""
    if target and urlsplit(str(target)).hostname in ("127.0.0.1", "localhost"):
        try:
            send()
        except Exception:  # noqa: BLE001 — the listener answers 401
            pass


def _pairing_warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "endpoint setting" in r.getMessage()
    ]


# ── OpenAI and Azure OpenAI through the openai SDK ────────────────────────


def test_dotenv_openai_base_url_does_not_receive_the_operator_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: _Listener
) -> None:
    openai = pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    (tmp_path / ".env").write_text(f"OPENAI_BASE_URL={listener.url}/v1\n")

    hydrate_dotenv(tmp_path)
    client = openai.OpenAI(max_retries=0, timeout=5)
    _send_if_loopback(str(client.base_url), client.models.list)

    assert listener.saw(OPERATOR_KEY) == []
    assert "OPENAI_BASE_URL" not in os.environ


def test_a_self_contained_openai_dotenv_still_works(tmp_path: Path, listener: _Listener) -> None:
    # The project's own endpoint with the project's own key: nothing of the
    # operator's is involved, so the pair loads.
    openai = pytest.importorskip("openai")
    (tmp_path / ".env").write_text(
        f"OPENAI_BASE_URL={listener.url}/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)
    client = openai.OpenAI(max_retries=0, timeout=5)
    _send_if_loopback(str(client.base_url), client.models.list)

    assert os.environ["OPENAI_BASE_URL"] == f"{listener.url}/v1"
    assert listener.saw(PROJECT_KEY) == ["GET /v1/models"]


def test_an_operator_exported_openai_endpoint_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: _Listener,
    operator_listener: _Listener,
    caplog: pytest.LogCaptureFixture,
) -> None:
    openai = pytest.importorskip("openai")
    monkeypatch.setenv("OPENAI_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("OPENAI_BASE_URL", f"{operator_listener.url}/v1")
    (tmp_path / ".env").write_text(f"OPENAI_BASE_URL={listener.url}/v1\n")

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path)
    client = openai.OpenAI(max_retries=0, timeout=5)
    _send_if_loopback(str(client.base_url), client.models.list)

    assert os.environ["OPENAI_BASE_URL"] == f"{operator_listener.url}/v1"
    assert operator_listener.saw(OPERATOR_KEY) == ["GET /v1/models"]
    assert listener.requests == []
    # The operator's own value is in effect, so there is nothing to report.
    assert _pairing_warnings(caplog) == []


def test_dotenv_azure_openai_endpoint_does_not_receive_the_operator_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: _Listener
) -> None:
    openai = pytest.importorskip("openai")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("OPENAI_API_VERSION", "2024-06-01")
    (tmp_path / ".env").write_text(f"AZURE_OPENAI_ENDPOINT={listener.url}\n")

    hydrate_dotenv(tmp_path)
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if endpoint:
        client = openai.AzureOpenAI(max_retries=0, timeout=5)
        _send_if_loopback(endpoint, client.models.list)

    assert listener.saw(OPERATOR_KEY) == []
    assert "AZURE_OPENAI_ENDPOINT" not in os.environ


# ── litellm, with the key FLUID itself resolves ───────────────────────────


def _litellm() -> Any:
    return pytest.importorskip("litellm")


@pytest.mark.parametrize(
    "provider,endpoint_var,key_var",
    [
        ("openai", "OPENAI_API_BASE", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
        ("gemini", "GEMINI_API_BASE", "GEMINI_API_KEY"),
    ],
)
def test_dotenv_llm_endpoint_does_not_receive_the_key_fluid_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    listener: _Listener,
    provider: str,
    endpoint_var: str,
    key_var: str,
) -> None:
    # FLUID_LLM_BACKEND=litellm: llm/litellm_backend.py hands litellm the key
    # provider_resolve picked and leaves the endpoint to litellm's env lookup.
    litellm = _litellm()
    from fluid_build.llm.litellm_backend import _litellm_model_for
    from fluid_build.llm.provider_resolve import _resolve_api_key

    monkeypatch.setenv(key_var, OPERATOR_KEY)
    (tmp_path / ".env").write_text(f"{endpoint_var}={listener.url}\n")

    hydrate_dotenv(tmp_path)
    key = _resolve_api_key(provider, os.environ)
    _send_if_loopback(
        os.environ.get(endpoint_var),
        lambda: litellm.completion(
            model=_litellm_model_for(provider, "test-model"),
            messages=[{"role": "user", "content": "ping"}],
            api_key=key,
            num_retries=0,
            timeout=5,
        ),
    )

    assert listener.saw(OPERATOR_KEY) == []
    assert endpoint_var not in os.environ


def test_dotenv_anthropic_endpoint_does_not_receive_a_keyring_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: _Listener
) -> None:
    # No key in the environment at all: provider_resolve falls back to the key
    # ``fluid ai setup`` saved in the keyring. The project file cannot know
    # that, so an Anthropic endpoint loads only with the project's own key.
    litellm = _litellm()
    import fluid_build.llm.providers as llm_providers
    from fluid_build.llm.litellm_backend import _litellm_model_for
    from fluid_build.llm.provider_resolve import _resolve_api_key

    monkeypatch.setattr(
        llm_providers,
        "_get_api_key_from_keyring",
        lambda provider: OPERATOR_KEY if provider == "anthropic" else None,
    )
    (tmp_path / ".env").write_text(f"ANTHROPIC_BASE_URL={listener.url}\n")

    hydrate_dotenv(tmp_path)
    key = _resolve_api_key("anthropic", os.environ)
    assert key == OPERATOR_KEY
    _send_if_loopback(
        os.environ.get("ANTHROPIC_BASE_URL"),
        lambda: litellm.completion(
            model=_litellm_model_for("anthropic", "test-model"),
            messages=[{"role": "user", "content": "ping"}],
            api_key=key,
            num_retries=0,
            timeout=5,
        ),
    )

    assert listener.saw(OPERATOR_KEY) == []
    assert "ANTHROPIC_BASE_URL" not in os.environ


def test_litellm_proxy_routing_is_operator_only(tmp_path: Path) -> None:
    # litellm sends the proxy the key FLUID resolved for any provider, so even
    # a proxy key in the same file cannot make the pairing self-contained.
    (tmp_path / ".env").write_text(
        "USE_LITELLM_PROXY=true\n"
        "LITELLM_PROXY_API_BASE=http://127.0.0.1:9\n"
        f"LITELLM_PROXY_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    assert "USE_LITELLM_PROXY" not in os.environ
    assert "LITELLM_PROXY_API_BASE" not in os.environ


# ── PostgreSQL ─────────────────────────────────────────────────────────────


def _dbt_output(platform: str) -> Dict[str, Any]:
    from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile

    profile = _build_generated_dbt_profile({"execution": {"runtime": {"platform": platform}}}, {})
    assert profile is not None
    (block,) = profile.values()
    return dict(block["outputs"][block["target"]])


def test_dotenv_pghost_does_not_receive_the_operator_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # build_runners/dbt/profiles.py writes the dbt-postgres profile from
    # PGHOST / PGPORT / PGPASSWORD; libpq sends the password to that host.
    monkeypatch.setenv("PGPASSWORD", OPERATOR_KEY)
    (tmp_path / ".env").write_text("PGHOST=pg.project.test\nPGPORT=6543\n")

    hydrate_dotenv(tmp_path)
    output = _dbt_output("postgres")

    assert output["host"] != "pg.project.test"
    assert output["port"] == 5432
    assert "PGHOST" not in os.environ and "PGPORT" not in os.environ


def test_a_self_contained_postgres_dotenv_still_works(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"PGHOST=pg.project.test\nPGPASSWORD={PROJECT_KEY}\n")

    hydrate_dotenv(tmp_path)
    output = _dbt_output("postgres")

    assert output["host"] == "pg.project.test"
    assert output["password"] == PROJECT_KEY


def test_an_operator_exported_postgres_host_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGHOST", "db.operator.test")
    monkeypatch.setenv("PGPASSWORD", OPERATOR_KEY)
    (tmp_path / ".env").write_text("PGHOST=pg.project.test\n")

    hydrate_dotenv(tmp_path)

    assert _dbt_output("postgres")["host"] == "db.operator.test"


def test_the_postgres_output_port_driver_does_not_connect_to_a_dotenv_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # output_ports/mcp/drivers/postgres.py reads POSTGRES_HOST / _PORT /
    # _PASSWORD into psycopg.connect. A stub driver records the target.
    from fluid_build.output_ports.mcp.drivers.postgres import PostgresDriver

    calls: List[Dict[str, Any]] = []
    fake = types.ModuleType("psycopg")
    fake.connect = lambda **opts: calls.append(opts) or types.SimpleNamespace()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("POSTGRES_PASSWORD", OPERATOR_KEY)
    (tmp_path / ".env").write_text("POSTGRES_HOST=pg.project.test\nPOSTGRES_PORT=6543\n")

    hydrate_dotenv(tmp_path)
    expose = {
        "exposeId": "orders",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {"database": "shop", "table": "orders"},
        },
    }
    PostgresDriver(expose=expose, contract={"id": "p"})._get_connection()

    (opts,) = calls
    assert opts["password"] == OPERATOR_KEY
    assert opts["host"] != "pg.project.test"
    assert opts["port"] == 5432


# ── Snowflake, Redshift, Databricks, MySQL ────────────────────────────────


def test_dotenv_snowflake_account_does_not_receive_the_operator_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", OPERATOR_KEY)
    (tmp_path / ".env").write_text(_SNOWFLAKE_TARGET + "SNOWFLAKE_USER=svc\n")

    hydrate_dotenv(tmp_path)
    output = _dbt_output("snowflake")

    assert output["account"] != "project-acct"
    # Non-endpoint settings in the same file still load.
    assert output["user"] == "svc"


def test_dotenv_snowflake_account_does_not_receive_a_keyring_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # providers/snowflake/util/config.py resolves each setting through
    # credentials/resolver.py: environment, project .env, then the keyring.
    from fluid_build.credentials.resolver import BaseCredentialResolver
    from fluid_build.providers.snowflake.util.config import resolve_snowflake_settings

    def keyring(self: Any, key: str) -> Optional[str]:
        return OPERATOR_KEY if key == "password" else None

    monkeypatch.setattr(BaseCredentialResolver, "_get_from_keyring", keyring)
    (tmp_path / ".env").write_text(_SNOWFLAKE_TARGET + "SNOWFLAKE_USER=svc\n")

    settings = resolve_snowflake_settings(project_root=tmp_path, environment="dev")

    assert settings.get("password") == OPERATOR_KEY
    assert settings.get("account") != "project-acct"


def test_dotenv_redshift_host_does_not_receive_the_operator_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDSHIFT_PASSWORD", OPERATOR_KEY)
    (tmp_path / ".env").write_text("REDSHIFT_HOST=rs.project.test\nREDSHIFT_PORT=6543\n")

    hydrate_dotenv(tmp_path)
    output = _dbt_output("redshift")

    assert output["host"] != "rs.project.test"
    assert output["port"] == 5439


def test_dotenv_databricks_host_does_not_receive_the_operator_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABRICKS_TOKEN", OPERATOR_KEY)
    (tmp_path / ".env").write_text("DATABRICKS_HOST=dbx.project.test\n")

    hydrate_dotenv(tmp_path)
    output = _dbt_output("databricks")

    assert output["token"] == OPERATOR_KEY
    assert output["host"] != "dbx.project.test"


def test_dotenv_mysql_host_does_not_receive_the_operator_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # libmysqlclient (DuckDB's mysql extension) reads MYSQL_HOST and MYSQL_PWD.
    monkeypatch.setenv("MYSQL_PWD", OPERATOR_KEY)
    (tmp_path / ".env").write_text("MYSQL_HOST=mysql.project.test\nMYSQL_TCP_PORT=6543\n")

    hydrate_dotenv(tmp_path)

    assert "MYSQL_HOST" not in os.environ
    assert "MYSQL_TCP_PORT" not in os.environ


# ── every group, the same four cases ──────────────────────────────────────


def _concrete(name: str) -> str:
    return name.replace("*", "DESTINATION__POSTGRES__" if name.startswith("*") else "X")


def _first_endpoint(group: EndpointGroup) -> str:
    return _concrete(group.endpoints[0])


def _first_credential(group: EndpointGroup) -> str:
    return _concrete(group.credentials[0])


def _pairing_credentials(group: EndpointGroup) -> List[str]:
    """Credentials that pair the group's first endpoint: one per code path."""
    if not group.ambient:
        return [_first_credential(group)]
    endpoint = _first_endpoint(group)
    section = endpoint[: endpoint.rfind("CREDENTIALS") + len("CREDENTIALS")] + "__"
    chosen: List[str] = []
    for path in group.paired_by:
        names = [n.replace("{section}", section) for n in path]
        if not set(names) & set(chosen):
            chosen.append(names[0])
    return chosen


_GROUPS = [pytest.param(g, id=g.service) for g in ENDPOINT_CREDENTIAL_GROUPS]


@pytest.mark.parametrize("group", _GROUPS)
def test_an_endpoint_is_refused_next_to_an_operator_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    group: EndpointGroup,
) -> None:
    endpoint, credential = _first_endpoint(group), _first_credential(group)
    monkeypatch.setenv(credential, OPERATOR_KEY)
    (tmp_path / ".env").write_text(f"{endpoint}=https://project.example.test\n")

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path)

    assert endpoint not in os.environ
    (message,) = _pairing_warnings(caplog)
    assert f"{endpoint} (.env, would receive {credential})" in message
    assert OPERATOR_KEY not in message
    assert "project.example.test" not in message


@pytest.mark.parametrize("group", _GROUPS)
def test_an_endpoint_loads_with_the_project_s_own_credential(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, group: EndpointGroup
) -> None:
    endpoint, credentials = _first_endpoint(group), _pairing_credentials(group)
    lines = [f"{endpoint}=https://project.example.test"]
    lines += [f"{credential}={PROJECT_KEY}" for credential in credentials]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path)

    assert os.environ[endpoint] == "https://project.example.test"
    for credential in credentials:
        assert os.environ[credential] == PROJECT_KEY
    assert _pairing_warnings(caplog) == []


@pytest.mark.parametrize("group", _GROUPS)
def test_an_operator_exported_endpoint_is_never_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: EndpointGroup
) -> None:
    endpoint, credential = _first_endpoint(group), _first_credential(group)
    monkeypatch.setenv(endpoint, "https://operator.example.test")
    (tmp_path / ".env").write_text(
        f"{endpoint}=https://project.example.test\n{credential}={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    assert os.environ[endpoint] == "https://operator.example.test"


@pytest.mark.parametrize("group", _GROUPS)
def test_an_endpoint_alone_is_refused_only_where_the_credential_lives_elsewhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, group: EndpointGroup
) -> None:
    endpoint = _first_endpoint(group)
    (tmp_path / ".env").write_text(f"{endpoint}=https://project.example.test\n")

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path)

    if group.ambient:
        assert endpoint not in os.environ
        (message,) = _pairing_warnings(caplog)
        assert f"would receive your {group.service} login or keyring credential" in message
    else:
        assert os.environ[endpoint] == "https://project.example.test"
        assert _pairing_warnings(caplog) == []


@pytest.mark.parametrize("group", _GROUPS)
def test_a_project_endpoint_in_another_case_is_in_effect_all_the_same(
    tmp_path: Path, group: EndpointGroup
) -> None:
    # The pairing rule compares names upper-cased, so ``snowflake_account``
    # next to the project's password loads, exported as spelled. Its readers
    # compare them the same way: pydantic-settings (the dlt bridge's
    # SnowflakeCredentials, PostgresCredentials ...) reads it as
    # SNOWFLAKE_ACCOUNT, and Windows has one variable for both. So the readers
    # pinned while a project endpoint is in effect must see this one too.
    endpoint, credentials = _first_endpoint(group), _pairing_credentials(group)
    spelled = endpoint.lower()
    lines = [f"{spelled}=https://project.example.test"]
    lines += [f"{credential}={PROJECT_KEY}" for credential in credentials]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")

    hydrate_dotenv(tmp_path)

    assert os.environ.get(spelled) == "https://project.example.test"
    assert project_endpoint_in_effect(group.service)


@pytest.mark.parametrize("operator_value", ["operator-acct", ""], ids=["set", "blank"])
def test_a_project_spelling_of_an_operator_endpoint_is_not_in_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operator_value: str
) -> None:
    # The operator's SNOWFLAKE_ACCOUNT, even a blank one, keeps the project's
    # ``snowflake_account`` out of the environment, and the credential
    # resolver's .env step reads only the exact name: no reader sees it.
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", operator_value)
    (tmp_path / ".env").write_text(
        f"snowflake_account=project-acct\nSNOWFLAKE_PASSWORD={PROJECT_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    assert "snowflake_account" not in os.environ
    assert not project_endpoint_in_effect("Snowflake")


# ── the rule's edges ───────────────────────────────────────────────────────


def test_unlisted_endpoints_follow_the_name_shape_rule() -> None:
    held = {"ACME_API_KEY": OPERATOR_KEY, "ACME_DB_PASSWORD": OPERATOR_KEY}
    refused = unpaired_endpoints(
        {
            "ACME_API_BASE": "https://a.test",
            "ACME_HOST": "a.test",
            "acme_base_url": "https://a.test",
            "WIDGET_URL": "https://w.test",
        },
        held,
    )

    assert set(refused) == {"ACME_API_BASE", "ACME_HOST", "acme_base_url"}
    assert refused["ACME_API_BASE"].operator_credentials == ("ACME_API_KEY", "ACME_DB_PASSWORD")
    # A credential of another service does not count.
    assert unpaired_endpoints({"ACME_BASE_URL": "https://a.test"}, {"OTHER_API_KEY": "k"}) == {}
    # ACME is no litellm provider, so with no ACME credential of the
    # operator's its endpoint is the project's own.
    assert unpaired_endpoints({"ACME_API_BASE": "https://a.test"}, {}) == {}
    # ``<PROVIDER>_API_BASE`` is litellm's name for its providers' endpoints,
    # where FLUID sends the key it resolved (FLUID_LLM_API_KEY, else the
    # keyring), so it pairs only with a FLUID_LLM_API_KEY of the project's.
    assert "DEEPSEEK_API_BASE" in unpaired_endpoints({"DEEPSEEK_API_BASE": "https://a.test"}, {})
    paired = {"DEEPSEEK_API_BASE": "https://a.test", "FLUID_LLM_API_KEY": PROJECT_KEY}
    assert unpaired_endpoints(paired, {}) == {}


def test_only_a_real_operator_credential_counts() -> None:
    endpoint = {"OPENAI_BASE_URL": "https://p.test"}
    # An empty operator value holds nothing.
    assert "OPENAI_BASE_URL" in unpaired_endpoints(endpoint, {"OPENAI_API_KEY": ""})
    # The project's own value, already exported by an earlier load in the same
    # run, is not the operator's.
    same = {**endpoint, "OPENAI_API_KEY": PROJECT_KEY}
    assert unpaired_endpoints(same, {"OPENAI_API_KEY": PROJECT_KEY}) == {}
    # A different value is, even when the project file sets the name too.
    assert unpaired_endpoints(same, {"OPENAI_API_KEY": OPERATOR_KEY})["OPENAI_BASE_URL"]
    # An empty endpoint redirects nothing.
    assert unpaired_endpoints({"PGHOST": ""}, {"PGPASSWORD": OPERATOR_KEY}) == {}


def test_a_secrets_file_credential_is_the_operator_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listener: _Listener
) -> None:
    # The secrets file loads after the project files and wins, so the key in
    # effect is the operator's even though the project file names one too.
    secrets = tmp_path / "operator.env"
    secrets.write_text(f"OPENAI_API_KEY={OPERATOR_KEY}\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(
        f"OPENAI_BASE_URL={listener.url}/v1\nOPENAI_API_KEY={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)

    assert os.environ["OPENAI_API_KEY"] == OPERATOR_KEY
    assert "OPENAI_BASE_URL" not in os.environ


def test_later_credential_lookups_agree_with_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # credentials/resolver.py builds a new store per lookup after hydration
    # exported the project's values; the project's own key must not then look
    # like the operator's.
    (tmp_path / ".env").write_text(f"PGHOST=pg.project.test\nPGPASSWORD={PROJECT_KEY}\n")
    hydrate_dotenv(tmp_path)

    with caplog.at_level(logging.WARNING):
        store = DotEnvCredentialStore(project_root=tmp_path)
        assert store.get_credential("PGHOST") == "pg.project.test"
    assert _pairing_warnings(caplog) == []

    # Once the operator's own password is in play, lookups refuse the host too.
    monkeypatch.setenv("PGPASSWORD", OPERATOR_KEY)
    assert DotEnvCredentialStore(project_root=tmp_path).get_credential("PGHOST") is None


@pytest.mark.parametrize("filename", [".env", ".env.dev", ".env.local"])
def test_every_project_dotenv_file_is_paired_alike(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    monkeypatch.setenv("PGPASSWORD", OPERATOR_KEY)
    (tmp_path / filename).write_text("PGHOST=pg.project.test\n")

    hydrate_dotenv(tmp_path, environment="dev")

    assert "PGHOST" not in os.environ


def test_a_credential_in_another_project_file_makes_the_pair(tmp_path: Path) -> None:
    # A key path, the method the Snowflake provider prefers to any keyring
    # password or token (tests/test_dotenv_pairing_matrix.py has the decoys).
    (tmp_path / ".env").write_text(_SNOWFLAKE_TARGET)
    (tmp_path / ".env.local").write_text(f"SNOWFLAKE_PRIVATE_KEY_PATH={tmp_path / 'key.p8'}\n")

    hydrate_dotenv(tmp_path)

    assert os.environ["SNOWFLAKE_ACCOUNT"] == "project-acct"
