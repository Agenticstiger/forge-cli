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

"""A Snowflake account from the project's dotenv files gets the project's credentials only.

The quickstart ``.env`` (the repo's ``.env.example`` block, and what the forge
scaffold writes and its setup script copies to ``.env``) holds the account,
the user and a password. The Snowflake readers that also find a credential of
the operator's outside the environment are pinned while such an account (or
host) is in effect: the credential resolver reads only the CLI argument, the
environment and the project's files (never the keyring, ``credentials.enc``,
``~/.fluidrc.yaml`` or a secret manager), the OpenTofu provider gets an empty
config file, and the hosted Snowflake MCP server an empty ``SNOWFLAKE_HOME``.
So the project's own password pairs the account again, and nothing the
operator stored goes to the host the project chose. Nor does the operator's
authenticator, and the project's own may only be the password one.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import (  # noqa: E402
    DotEnvCredentialStore,
    reset_dotenv_load_log,
)
from fluid_build.credentials.protected_env import unpaired_endpoints  # noqa: E402
from tests._dotenv_pairing_support import (  # noqa: E402
    OPERATOR_KEY,
    PROJECT_KEY,
    dlt_destination_credentials,
    drop_new_names,
    hermetic_env,
    refuse_outbound_dns,
)

REPO = Path(__file__).resolve().parents[1]
_OPERATOR_KEY_PATH = "/operator/snowflake/rsa_key.p8"
QUICKSTART_ENV = (
    "SNOWFLAKE_ACCOUNT=xy12345\n"
    "SNOWFLAKE_USER=project_user\n"  # pragma: allowlist secret
    f"SNOWFLAKE_PASSWORD={PROJECT_KEY}\n"
    "SNOWFLAKE_WAREHOUSE=COMPUTE_WH\n"
    "SNOWFLAKE_DATABASE=DEV_ANALYTICS\n"
    "SNOWFLAKE_SCHEMA=COMMUNITY_SMOKE\n"
    "SNOWFLAKE_ROLE=TRANSFORMER\n"
)
# What the operator keeps outside the environment, by resolver key.
_OPERATOR_STORE = {
    "oauth_token": OPERATOR_KEY,
    "private_key_path": _OPERATOR_KEY_PATH,
    "password": OPERATOR_KEY,
    "authenticator": "externalbrowser",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import fluid_build.credentials as credentials_pkg

    monkeypatch.setattr(credentials_pkg, "_adapters", {})
    before = hermetic_env(monkeypatch, tmp_path / "home")
    refuse_outbound_dns(monkeypatch)
    reset_dotenv_load_log()
    yield
    drop_new_names(before)
    reset_dotenv_load_log()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _operator_stores(
    home: Path,
    monkeypatch: Optional[pytest.MonkeyPatch] = None,
    held: Mapping[str, str] = _OPERATOR_STORE,
) -> None:
    """Put the operator's Snowflake material where the resolver looks after the
    environment: ``~/.fluid/credentials.enc``, ``~/.fluidrc.yaml`` and, in
    process, the keyring and the secret manager."""
    from fluid_build.credentials.encrypted_store import EncryptedCredentialStore

    store = EncryptedCredentialStore(
        store_path=home / ".fluid" / "credentials.enc", key_path=home / ".fluid" / ".key"
    )
    for key, value in held.items():
        store.set_credential(f"snowflake.{key}", value)
    rc = home / ".fluidrc.yaml"
    rc.write_text("snowflake:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in held.items()))
    rc.chmod(0o600)
    if monkeypatch is None:
        return
    from fluid_build.credentials.keyring_store import KeyringCredentialStore
    from fluid_build.credentials.resolver import BaseCredentialResolver

    def keyring_get(key: str) -> Optional[str]:
        provider, _, name = key.partition(".")
        return held.get(name) if provider == "snowflake" else None

    monkeypatch.setattr(KeyringCredentialStore, "get_credential", staticmethod(keyring_get))
    monkeypatch.setattr(
        BaseCredentialResolver,
        "_get_from_secret_manager",
        lambda self, key: held.get(key) if self.provider == "snowflake" else None,
    )


def _no_operator_material(params: Mapping[str, Any]) -> None:
    flat = json.dumps({k: str(v) for k, v in params.items()})
    assert OPERATOR_KEY not in flat, sorted(params)
    assert _OPERATOR_KEY_PATH not in flat, sorted(params)
    assert params.get("authenticator") in (None, "oauth"), params.get("authenticator")


# ── the quickstart .env loads whole ───────────────────────────────────────


def _repo_env_example_snowflake_block() -> str:
    lines = (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
    block = [line[2:] for line in lines if line.startswith("# SNOWFLAKE_") and "=" in line]
    assert block, "the repo .env.example has no Snowflake block"
    return "\n".join(block) + "\n"


def _scaffold_env_example() -> str:
    from types import SimpleNamespace

    from fluid_build.forge.generators.config_generator import ConfigGenerator

    generator = ConfigGenerator.__new__(ConfigGenerator)
    context = SimpleNamespace(project_config={"provider": "snowflake", "name": "p"})
    return ConfigGenerator._generate_env_example(generator, context)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [_repo_env_example_snowflake_block, _scaffold_env_example, lambda: QUICKSTART_ENV],
    ids=["repo-env-example", "forge-scaffold", "quickstart"],
)
def test_the_quickstart_snowflake_dotenv_loads_whole(project: Path, text: Any) -> None:
    content = text()
    (project / ".env").write_text(content)

    loaded = DotEnvCredentialStore(project_root=project).load()

    wanted = [
        line.split("=", 1)[0] for line in content.splitlines() if line.startswith("SNOWFLAKE_")
    ]
    assert "SNOWFLAKE_ACCOUNT" in wanted
    assert [name for name in wanted if name not in loaded] == []


@pytest.mark.parametrize(
    "credential",
    [
        "SNOWFLAKE_PASSWORD",
        "SF_PASSWORD",
        "PASSWORD",
        "SNOWFLAKE_OAUTH_TOKEN",
        "SNOWFLAKE_PRIVATE_KEY_PATH",
        "PRIVATE_KEY_PATH",
    ],
)
def test_a_project_credential_the_pinned_resolver_reads_pairs_the_account(
    credential: str,
) -> None:
    project = {"SNOWFLAKE_ACCOUNT": "xy12345", credential: PROJECT_KEY}

    assert unpaired_endpoints(project, {}, case_insensitive_env=False) == {}


@pytest.mark.parametrize(
    "decoy", ["SNOWFLAKE_PAT", "SNOWFLAKE_TOKEN", "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
)
def test_a_credential_no_pinned_reader_sends_still_does_not_pair(decoy: str) -> None:
    project = {"SNOWFLAKE_ACCOUNT": "xy12345", decoy: PROJECT_KEY}

    assert "SNOWFLAKE_ACCOUNT" in unpaired_endpoints(project, {}, case_insensitive_env=False)


def test_an_operator_credential_in_the_environment_still_refuses_the_account() -> None:
    project = {"SNOWFLAKE_ACCOUNT": "xy12345", "SNOWFLAKE_PASSWORD": PROJECT_KEY}

    refused = unpaired_endpoints(project, {"SNOWFLAKE_OAUTH_TOKEN": OPERATOR_KEY})

    assert refused["SNOWFLAKE_ACCOUNT"].operator_credentials == ("SNOWFLAKE_OAUTH_TOKEN",)


# ── the resolver is pinned to the project's material ───────────────────────


def test_the_project_account_gets_the_project_password_not_the_operator_s_stores(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    (project / ".env").write_text(QUICKSTART_ENV)

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert (params["account"], params["user"]) == ("xy12345", "project_user")
    assert params["password"] == PROJECT_KEY
    _no_operator_material(params)


@pytest.mark.parametrize("credential", ["SNOWFLAKE_OAUTH_TOKEN", "SNOWFLAKE_PRIVATE_KEY_PATH"])
def test_every_credential_form_the_resolver_uses_comes_from_the_project(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential: str
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    (project / "key.p8").write_text("not a key\n")
    value = str(project / "key.p8") if credential.endswith("PATH") else PROJECT_KEY
    (project / ".env").write_text(
        f"SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n{credential}={value}\n"
    )

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert params["account"] == "xy12345"
    assert value in params.values()
    _no_operator_material(params)


def test_a_value_cached_before_the_project_loaded_is_not_used(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A long-lived process resolved the operator's own setup first, through
    # the process-wide adapter (get_snowflake_adapter), which cached what the
    # operator's stores hold. get_connection_params prefers the key path.
    from fluid_build.credentials import get_snowflake_adapter
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operator-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "operator_user")
    assert get_connection_params(project_root=project)["private_key_path"] == _OPERATOR_KEY_PATH
    adapter = get_snowflake_adapter()
    cached = {value for value, _expiry in adapter._cache.values()}
    assert {OPERATOR_KEY, _OPERATOR_KEY_PATH} <= cached
    monkeypatch.delenv("SNOWFLAKE_ACCOUNT")
    monkeypatch.delenv("SNOWFLAKE_USER")
    (project / ".env").write_text(QUICKSTART_ENV)

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert get_snowflake_adapter() is adapter
    assert params["account"] == "xy12345"
    assert params["password"] == PROJECT_KEY
    _no_operator_material(params)


def test_an_operator_s_blank_account_lets_the_project_account_through_pinned(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The blank value wins the export, so the resolver's .env step supplies
    # the project's account; that account is the project's all the same.
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "")
    (project / ".env").write_text(QUICKSTART_ENV)

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert params["account"] == "xy12345"
    _no_operator_material(params)


# docker compose ``environment: [SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER,
# SNOWFLAKE_PASSWORD]``, and CI secrets that are not defined, pass the host's
# unset variables as blank ones; the mounted project .env holds the values.
_BLANK_PASSTHROUGH = {"SNOWFLAKE_ACCOUNT": "", "SNOWFLAKE_USER": "", "SNOWFLAKE_PASSWORD": ""}


def test_a_blank_passthrough_keeps_the_quickstart_account_with_its_password_pinned(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The credential resolver reads a blank variable as unset and falls back
    # to the project's files for the account and the password alike, so the
    # blank password does not take the project's password away from it.
    from fluid_build.credentials.dotenv_store import project_endpoint_in_effect
    from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    for name, value in _BLANK_PASSTHROUGH.items():
        monkeypatch.setenv(name, value)
    (project / ".env").write_text(QUICKSTART_ENV)

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert (params["account"], params["user"]) == ("xy12345", "project_user")
    assert params["password"] == PROJECT_KEY
    _no_operator_material(params)
    # and the readers outside the resolver are pinned as for any project account
    assert project_endpoint_in_effect("Snowflake")
    env = dict(os.environ)
    assert "SNOWFLAKE_CONFIG_PATH" in SnowflakeIacPlugin().credential_env(env)
    mcp = _snowflake_mcp_env({**env, "FLUID_SNOWFLAKE_MCP": "1"})
    assert list(Path(mcp["SNOWFLAKE_HOME"]).iterdir()) == []


def test_a_blank_operator_credential_pairs_only_an_account_behind_a_blank_variable() -> None:
    project = {"SNOWFLAKE_ACCOUNT": "xy12345", "SNOWFLAKE_PASSWORD": PROJECT_KEY}

    assert unpaired_endpoints(project, _BLANK_PASSTHROUGH, case_insensitive_env=False) == {}
    # Without a variable of its own the account is exported, and the readers
    # of the environment (dbt, OpenTofu, the hosted MCP server) would see it
    # next to the blank password, so they would take theirs from elsewhere.
    refused = unpaired_endpoints(project, {"SNOWFLAKE_PASSWORD": ""}, case_insensitive_env=False)
    assert "SNOWFLAKE_ACCOUNT" in refused


def test_a_blank_operator_llm_key_still_unpairs_an_llm_endpoint() -> None:
    # FLUID reads a blank FLUID_LLM_API_KEY as unset and takes the keyring key.
    project = {"OPENAI_API_BASE": "https://llm.project.test", "FLUID_LLM_API_KEY": PROJECT_KEY}

    refused = unpaired_endpoints(project, {"OPENAI_API_BASE": "", "FLUID_LLM_API_KEY": ""})

    assert "OPENAI_API_BASE" in refused


def test_a_dropped_account_is_reported_while_the_shell_s_variable_is_blank(
    project: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The resolver would have read the project's account for the blank one.
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "")
    monkeypatch.setenv("SNOWFLAKE_OAUTH_TOKEN", OPERATOR_KEY)
    (project / ".env").write_text(QUICKSTART_ENV)

    warnings = _hydrate_warnings(project, caplog)

    assert [w for w in warnings if "SNOWFLAKE_ACCOUNT (.env, would receive" in w], warnings
    assert all(OPERATOR_KEY not in w for w in warnings)


def test_a_protected_setting_is_reported_while_the_shell_s_variable_is_blank(
    project: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL", "")
    (project / ".env").write_text("SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL=https://idp.project.test\n")

    warnings = _hydrate_warnings(project, caplog)

    assert [w for w in warnings if "SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL (.env)" in w], warnings


def test_the_operator_s_own_account_still_uses_the_operator_s_stores(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operator-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "operator_user")
    (project / ".env").write_text("SNOWFLAKE_WAREHOUSE=PROJECT_WH\n")

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert (params["account"], params["warehouse"]) == ("operator-acct", "PROJECT_WH")
    assert params["private_key_path"] == _OPERATOR_KEY_PATH


def test_an_operator_exporting_the_same_account_keeps_the_operator_s_stores(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A team .env names the account the operator also exports: their own.
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "xy12345")
    (project / ".env").write_text(QUICKSTART_ENV)

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert params["private_key_path"] == _OPERATOR_KEY_PATH


def test_the_store_leaves_a_name_the_operator_s_secrets_file_sets_to_that_file(
    project: Path,
) -> None:
    # FLUID_SECRETS_FILE loads after the project files and wins, so the
    # project's value of a name it sets is never the one in effect.
    from fluid_build.credentials.dotenv_store import project_exports

    (project / ".env").write_text("SNOWFLAKE_WAREHOUSE=PROJECT_WH\nSNOWFLAKE_ROLE=PROJECT_ROLE\n")

    loaded = DotEnvCredentialStore(
        project_root=project, operator_env={"SNOWFLAKE_WAREHOUSE": "OPERATOR_WH"}
    ).load()

    assert loaded["SNOWFLAKE_WAREHOUSE"] == "PROJECT_WH"
    assert "SNOWFLAKE_WAREHOUSE" not in os.environ
    assert os.environ["SNOWFLAKE_ROLE"] == "PROJECT_ROLE"
    assert project_exports() == {"SNOWFLAKE_ROLE": "PROJECT_ROLE"}


def test_an_account_the_operator_s_secrets_file_also_sets_is_the_operator_s(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The same account in the team .env and in the operator's secrets file:
    # the file's value wins and it is the operator's, even with the same text,
    # so the operator's stores still serve it.
    from fluid_build.credentials.dotenv_store import project_exports
    from fluid_build.providers.snowflake.util.config import get_connection_params

    _operator_stores(tmp_path / "home", monkeypatch)
    secrets = tmp_path / "operator.env"
    secrets.write_text(
        "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=operator_user\n"  # pragma: allowlist secret
    )
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    (project / ".env").write_text(QUICKSTART_ENV)

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")

    assert "SNOWFLAKE_ACCOUNT" not in project_exports()
    assert (params["account"], params["user"]) == ("xy12345", "operator_user")
    assert params["private_key_path"] == _OPERATOR_KEY_PATH


_TEAM_SETTINGS = (
    "SNOWFLAKE_ACCOUNT=corp-analytics\nSNOWFLAKE_WAREHOUSE=TEAM_WH\n"  # pragma: allowlist secret
    "SNOWFLAKE_DATABASE=TEAM_DB\nSNOWFLAKE_ROLE=TEAM_ROLE\n"
)


@pytest.mark.parametrize(
    "secrets_text",
    [
        "SNOWFLAKE_ACCOUNT=corp-analytics\nSNOWFLAKE_USER=me\n"  # pragma: allowlist secret
        "SNOWFLAKE_AUTHENTICATOR=externalbrowser\n",  # pragma: allowlist secret
        "SNOWFLAKE_ACCOUNT=corp-analytics\nSNOWFLAKE_USER=me\n"  # pragma: allowlist secret
        f"SNOWFLAKE_PASSWORD={OPERATOR_KEY}\n",
    ],
    ids=["sso", "password"],
)
def test_no_warning_for_what_the_operator_s_secrets_file_sets_itself(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    secrets_text: str,
) -> None:
    # The team .env names the account the operator's secrets file sets: the
    # file's value is in effect, as an exported one is, and nothing of the
    # project's is ignored that the operator did not set already.
    from fluid_build.providers.snowflake.util.config import get_connection_params

    secrets = tmp_path / "operator.env"
    secrets.write_text(secrets_text)
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    (project / ".env").write_text(_TEAM_SETTINGS)
    (project / ".env").chmod(0o600)

    warnings = _hydrate_warnings(project, caplog)
    params = get_connection_params(project_root=project, environment="dev")

    assert [w for w in warnings if w.startswith("Ignored")] == []
    assert (params["account"], params["warehouse"]) == ("corp-analytics", "TEAM_WH")


def test_no_warning_for_a_protected_setting_the_operator_s_secrets_file_sets(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secrets = tmp_path / "operator.env"
    secrets.write_text("SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL=https://idp.operator.test\n")
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    (project / ".env").write_text("SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL=https://idp.project.test\n")
    (project / ".env").chmod(0o600)

    warnings = _hydrate_warnings(project, caplog)

    assert os.environ["SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL"] == "https://idp.operator.test"
    assert [w for w in warnings if w.startswith("Ignored")] == []


@pytest.mark.parametrize("operator", [{}, {"SF_PASSWORD": ""}], ids=["unset", "blank"])
def test_the_adapter_sends_the_project_s_sf_password_for_its_account(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operator: Dict[str, str]
) -> None:
    # SF_PASSWORD pairs the project's account, so the adapter reads it too
    # (as resolve_snowflake_settings does), from the environment and, behind
    # a blank variable of the operator's, from the project's files.
    from fluid_build.credentials import CredentialConfig
    from fluid_build.credentials.adapters import SnowflakeCredentialAdapter

    _operator_stores(tmp_path / "home", monkeypatch)
    for name, value in operator.items():
        monkeypatch.setenv(name, value)
    if operator:
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "")
    (project / ".env").write_text(
        "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n"  # pragma: allowlist secret
        f"SF_PASSWORD={PROJECT_KEY}\n"
    )

    hydrate_dotenv(project)
    adapter = SnowflakeCredentialAdapter(CredentialConfig(project_root=project))
    params = adapter.get_connection_params()

    assert (params["account"], params["password"]) == ("xy12345", PROJECT_KEY)
    _no_operator_material(params)


def test_the_adapter_never_offers_the_browser_for_a_pinned_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Pinned, the adapter finds no credential of the project's: it leaves the
    # sign-in to fail rather than open the operator's browser session there.
    from fluid_build.credentials import CredentialConfig
    from fluid_build.credentials.adapters import SnowflakeCredentialAdapter

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "xy12345")
    monkeypatch.setenv("SNOWFLAKE_USER", "project_user")
    monkeypatch.setattr(
        SnowflakeCredentialAdapter, "_project_endpoint_in_effect", lambda self: True
    )
    adapter = SnowflakeCredentialAdapter(CredentialConfig(project_root=tmp_path))

    params = adapter.get_connection_params()

    assert params["account"] == "xy12345"
    assert "authenticator" not in params, params


def test_the_adapter_still_offers_the_browser_for_the_operator_s_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fluid_build.credentials import CredentialConfig
    from fluid_build.credentials.adapters import SnowflakeCredentialAdapter

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operator-acct")
    monkeypatch.setenv("SNOWFLAKE_USER", "operator_user")

    params = SnowflakeCredentialAdapter(
        CredentialConfig(project_root=tmp_path)
    ).get_connection_params()

    assert params["authenticator"] == "externalbrowser"


# ── readers outside the resolver ───────────────────────────────────────────


def test_opentofu_gets_an_empty_snowflake_config_for_a_project_account(project: Path) -> None:
    # The snowflakedb/snowflake provider merges ~/.snowflake/config (or
    # SNOWFLAKE_CONFIG_PATH) under the environment, field by field.
    from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

    (project / ".env").write_text(QUICKSTART_ENV.replace("xy12345", "myorg-xy12345"))
    hydrate_dotenv(project)
    env = dict(os.environ)

    overlay = SnowflakeIacPlugin().credential_env(env)

    config = Path(overlay["SNOWFLAKE_CONFIG_PATH"])
    assert config.is_file() and config.read_bytes() == b""
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert (overlay["SNOWFLAKE_ORGANIZATION_NAME"], overlay["SNOWFLAKE_ACCOUNT_NAME"]) == (
        "myorg",
        "xy12345",
    )


def test_opentofu_keeps_the_operator_s_config_for_the_operator_s_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-operator")

    overlay = SnowflakeIacPlugin().credential_env(dict(os.environ))

    assert "SNOWFLAKE_CONFIG_PATH" not in overlay


def _snowflake_mcp_env(source: Mapping[str, str]) -> Dict[str, str]:
    from fluid_build.cli import hosted_mcp

    spec = hosted_mcp.HOSTED_MCP_REGISTRY["snowflake"]
    return hosted_mcp.HostedMcpClient(spec, env=dict(source))._subprocess_env()


def test_the_hosted_snowflake_mcp_gets_an_empty_snowflake_home_for_a_project_account(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The connector reads connections.toml / config.toml from SNOWFLAKE_HOME
    # (~/.snowflake) for a named connection, and SNOWFLAKE_CONNECTIONS too.
    monkeypatch.setenv("SNOWFLAKE_CONNECTIONS", "[default]\npassword = 'x'\n")
    monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "default")
    (project / ".env").write_text(QUICKSTART_ENV)
    hydrate_dotenv(project)

    env = _snowflake_mcp_env({**os.environ, "FLUID_SNOWFLAKE_MCP": "1"})

    home = Path(env["SNOWFLAKE_HOME"])
    assert home.is_dir() and list(home.iterdir()) == []
    assert "SNOWFLAKE_CONNECTIONS" not in env
    assert "SNOWFLAKE_DEFAULT_CONNECTION_NAME" not in env
    assert (env["SNOWFLAKE_ACCOUNT"], env["SNOWFLAKE_PASSWORD"]) == ("xy12345", PROJECT_KEY)


def test_the_hosted_snowflake_mcp_keeps_the_operator_s_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operator-acct")
    monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "default")

    env = _snowflake_mcp_env({**os.environ, "FLUID_SNOWFLAKE_MCP": "1"})

    assert "SNOWFLAKE_HOME" not in env
    assert env["SNOWFLAKE_DEFAULT_CONNECTION_NAME"] == "default"


def _snowflake_pairing_names() -> List[str]:
    from fluid_build.credentials.protected_env import ENDPOINT_CREDENTIAL_GROUPS

    group = next(g for g in ENDPOINT_CREDENTIAL_GROUPS if g.service == "Snowflake")
    return [name for path in group.paired_by for name in path]


@pytest.mark.parametrize("credential", _snowflake_pairing_names())
def test_fluid_auth_never_falls_back_to_snowsql_for_a_project_account(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential: str
) -> None:
    # `fluid auth` runs snowsql, which reads the password of its connection
    # from ~/.snowsql/config, only when the resolver found no connector
    # credential. A project account loads only next to a credential that
    # pairs it, and the pinned resolver reads each of those names, so the
    # check goes through the connector with the project's credential.
    import asyncio
    import logging

    import fluid_build.providers.snowflake.connection as connection
    from fluid_build.cli.auth import SnowflakeAuthProvider

    home = tmp_path / "home"
    _operator_stores(home, monkeypatch)
    (home / ".snowsql").mkdir()
    (home / ".snowsql" / "config").write_text(f"[connections]\npassword = {OPERATOR_KEY}\n")
    (project / "key.p8").write_text("not a key\n")
    value = str(project / "key.p8") if credential.endswith("PATH") else PROJECT_KEY
    (project / ".env").write_text(
        f"SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n{credential}={value}\n"
    )
    monkeypatch.chdir(project)  # `fluid auth` reads the .env of the directory it runs in
    bin_dir = tmp_path / "bin"  # an installed snowsql, so login() may choose it
    bin_dir.mkdir()
    (bin_dir / "snowsql").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "snowsql").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    commands: List[List[str]] = []
    connected: List[Dict[str, Any]] = []

    def connect(**params: Any) -> None:
        connected.append(params)
        raise RuntimeError("recorded, not connected")

    monkeypatch.setattr(connection, "SnowflakeConnection", connect)
    monkeypatch.setattr(
        SnowflakeAuthProvider,
        "_run_command",
        lambda self, command, **kwargs: commands.append(command),
    )
    provider = SnowflakeAuthProvider({}, logging.getLogger("test.auth"))

    asyncio.run(provider.check_auth())
    asyncio.run(provider.login())

    assert commands == []
    assert len(connected) == 2
    for params in connected:
        assert (params["account"], params["user"]) == ("xy12345", "project_user")
        assert value in params.values()
        _no_operator_material(params)


def test_the_metadata_source_reader_keeps_each_account_with_its_own_credential(
    project: Path, tmp_path: Path
) -> None:
    # copilot/catalog/credentials.py takes a Snowflake source from the
    # environment (account, user and credential together), or account and
    # credential together from a source the operator saved; it never puts
    # the project's account together with the operator's saved credential.
    from fluid_build.copilot.catalog.credentials import CredentialResolver
    from fluid_build.copilot.catalog.credentials import (
        SnowflakeCredentials as SourceCredentials,
    )

    sources = tmp_path / "home" / ".fluid" / "sources.yaml"
    sources.parent.mkdir(parents=True)
    sources.write_text(
        "sources:\n  ops:\n    source_type: snowflake\n    config:\n"
        "      account: operator-acct\n      user: operator_user\n      auth_method: password\n"
    )

    class OperatorKeyring:
        @staticmethod
        def get_password(service: str, name: str) -> Optional[str]:
            return json.dumps({"password": OPERATOR_KEY}) if name == "ops" else None

    (project / ".env").write_text(QUICKSTART_ENV)
    hydrate_dotenv(project)
    resolver = CredentialResolver(sources_config_path=sources, keyring_module=OperatorKeyring)

    from_env = resolver.resolve(catalog_name="snowflake", credential_type=SourceCredentials)
    saved = resolver.resolve(
        catalog_name="snowflake", credential_type=SourceCredentials, credential_id="ops"
    )

    project_kwargs = from_env.to_connection_kwargs()
    assert (project_kwargs["account"], project_kwargs["password"]) == ("xy12345", PROJECT_KEY)
    _no_operator_material(project_kwargs)
    saved_kwargs = saved.to_connection_kwargs()
    assert (saved_kwargs["account"], saved_kwargs["password"]) == ("operator-acct", OPERATOR_KEY)


# The operator's dlt secrets (``~/.dlt/secrets.toml``) for the Snowflake
# destination, and the credential fields these tests look at.
_OPERATOR_DLT_SECRETS = {
    "private_key": "OPERATOR-FAKE-PRIVATE-KEY",  # pragma: allowlist secret
    "authenticator": "externalbrowser",
    "password": OPERATOR_KEY,
}
_DLT_SNOWFLAKE_FIELDS = ("host", "username", "password", "private_key", "authenticator")


def _dlt_snowflake_credentials(
    tmp_path: Path, dotenv: Optional[str], extra: Mapping[str, str]
) -> Dict[str, Any]:
    return dlt_destination_credentials(
        tmp_path,
        "snowflake",
        dotenv,
        _DLT_SNOWFLAKE_FIELDS,
        operator_env=extra,
        operator_secrets={
            f"destination.snowflake.credentials.{name}": value
            for name, value in _OPERATOR_DLT_SECRETS.items()
        },
    )


def test_the_dlt_snowflake_destination_takes_no_operator_dlt_secret_for_a_project_account(
    tmp_path: Path,
) -> None:
    # dlt fills each credential field the environment leaves unset from its
    # secrets.toml, the operator's global one included, and prefers a key or
    # an authenticator to the project's password.
    fields = _dlt_snowflake_credentials(tmp_path, QUICKSTART_ENV, {})

    assert (fields["host"], fields["username"]) == ("xy12345", "project_user")
    assert fields["password"] == PROJECT_KEY
    assert (fields["private_key"], fields["authenticator"]) == (None, None)


def test_the_dlt_snowflake_destination_takes_the_quickstart_behind_a_blank_passthrough(
    tmp_path: Path,
) -> None:
    # As the credential resolver does, the pinned destination reads the
    # project's value for a FLUID variable the operator passed through blank.
    fields = _dlt_snowflake_credentials(tmp_path, QUICKSTART_ENV, _BLANK_PASSTHROUGH)

    assert (fields.get("host"), fields.get("username")) == ("xy12345", "project_user"), fields
    assert fields["password"] == PROJECT_KEY
    assert (fields["private_key"], fields["authenticator"]) == (None, None)


def test_a_project_account_in_another_case_pins_the_dlt_destination_too(tmp_path: Path) -> None:
    # The pairing rule loads ``snowflake_account`` next to the project's
    # password, and the dlt bridge's SnowflakeCredentials (pydantic-settings)
    # reads it as SNOWFLAKE_ACCOUNT. Unpinned, dlt filled the operator's key
    # and authenticator from their dlt secrets next to it.
    dotenv = QUICKSTART_ENV.replace("SNOWFLAKE_ACCOUNT=", "snowflake_account=")
    assert dotenv != QUICKSTART_ENV

    fields = _dlt_snowflake_credentials(tmp_path, dotenv, {})

    assert (fields["host"], fields["username"]) == ("xy12345", "project_user")
    assert fields["password"] == PROJECT_KEY
    assert (fields["private_key"], fields["authenticator"]) == (None, None)


def test_the_dlt_snowflake_destination_keeps_the_operator_s_dlt_secrets_for_their_account(
    tmp_path: Path,
) -> None:
    fields = _dlt_snowflake_credentials(
        tmp_path,
        None,
        {
            "SNOWFLAKE_ACCOUNT": "operator-acct",
            "SNOWFLAKE_USER": "operator_user",
            "SNOWFLAKE_DATABASE": "OPERATOR_DB",
        },
    )

    assert fields["host"] == "operator-acct"
    assert fields["private_key"] == _OPERATOR_DLT_SECRETS["private_key"]


# ── end to end: fluid verify on examples/snowflake/smoke ──────────────────

_FAKE_CONNECTOR = '''"""Stand-in for snowflake-connector-python: records connect(), never connects."""
import json, os


class DictCursor:
    pass


class Error(Exception):
    pass


class ProgrammingError(Error):
    pass


class DatabaseError(Error):
    pass


def connect(**kwargs):
    with open(os.environ["FAKE_SF_RECORD"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v if isinstance(v, (str, int, bool, type(None))) else repr(v)
                             for k, v in kwargs.items()}) + "\\n")
    raise DatabaseError("stand-in connector: connect() recorded, no network")
'''
_OPERATOR_KEYRING = '''"""The operator's keyring, holding Snowflake credentials ``fluid`` may find."""
import json, os
from keyring.backend import KeyringBackend


class OperatorKeyring(KeyringBackend):
    priority = 10

    def get_password(self, service, username):
        return json.loads(os.environ["OPERATOR_KEYRING"]).get(f"{service}/{username}")

    def set_password(self, service, username, password):
        raise RuntimeError("read-only")

    def delete_password(self, service, username):
        raise RuntimeError("read-only")
'''


def _operator_key(home: Path) -> str:
    """A real key pair file of the operator's, so a leak would reach connect()."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = home / ".snowflake" / "rsa_key.p8"
    path.parent.mkdir(parents=True)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path)


def _fluid_on_smoke(
    tmp_path: Path,
    extra: Dict[str, str],
    dotenv: Optional[str],
    args: Tuple[str, ...] = ("verify", "contract.fluid.yaml"),
    connects: bool = True,
) -> Tuple[List[Dict], List[str]]:
    """``fluid <args>`` in a copy of examples/snowflake/smoke, the operator's
    keyring, ``credentials.enc`` and ``~/.fluidrc.yaml`` holding an OAuth
    token, a key pair and a password, and ``~/.snowsql/config`` a password;
    returns the connect() calls and the snowsql command lines. With
    ``connects=False`` the command may also end before any connect() call."""
    pytest.importorskip("keyring")
    pytest.importorskip("cryptography")
    smoke = tmp_path / "smoke"
    shutil.copytree(REPO / "examples" / "snowflake" / "smoke", smoke)
    if dotenv is not None:
        (smoke / ".env").write_text(dotenv)
    site = tmp_path / "site"
    (site / "snowflake" / "connector").mkdir(parents=True)
    (site / "snowflake" / "__init__.py").write_text("")
    (site / "snowflake" / "connector" / "__init__.py").write_text(_FAKE_CONNECTOR)
    (site / "snowflake" / "connector" / "errors.py").write_text(
        "from . import DatabaseError, Error, ProgrammingError  # noqa: F401\n"
    )
    (site / "operator_keyring.py").write_text(_OPERATOR_KEYRING)
    home = tmp_path / "home"
    held = {**_OPERATOR_STORE, "private_key_path": _operator_key(home)}
    _operator_stores(home, held=held)
    (home / ".snowsql").mkdir()
    (home / ".snowsql" / "config").write_text(f"[connections]\npassword = {OPERATOR_KEY}\n")
    snowsql_record = tmp_path / "snowsql.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "snowsql").write_text(f'#!/bin/sh\necho "$*" >> "{snowsql_record}"\n')
    (bin_dir / "snowsql").chmod(0o755)
    record = tmp_path / "connect.jsonl"
    keyring = {f"fluid-cli/snowflake.{k}": v for k, v in held.items()}
    env = {
        "PATH": os.pathsep.join([str(bin_dir), os.environ["PATH"]]),
        "PYTHONPATH": os.pathsep.join([str(site), os.environ.get("PYTHONPATH", "")]),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "FLUID_NONINTERACTIVE": "1",
        "PYTHON_KEYRING_BACKEND": "operator_keyring.OperatorKeyring",
        "OPERATOR_KEYRING": json.dumps(keyring),
        "FAKE_SF_RECORD": str(record),
        **extra,
    }

    done = subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=smoke,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    output = done.stdout + done.stderr
    assert record.exists() or not connects, output[-3000:]
    calls = (
        [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
    )
    snowsql = snowsql_record.read_text().splitlines() if snowsql_record.exists() else []
    return calls, snowsql


def _verify_smoke(tmp_path: Path, extra: Dict[str, str], dotenv: Optional[str]) -> List[Dict]:
    calls, _snowsql = _fluid_on_smoke(tmp_path, extra, dotenv)
    return calls


def test_fluid_verify_connects_the_quickstart_account_with_the_project_password(
    tmp_path: Path,
) -> None:
    calls = _verify_smoke(tmp_path, {}, QUICKSTART_ENV)

    assert calls
    for call in calls:
        assert (call["account"], call["user"]) == ("xy12345", "project_user")
        assert call["password"] == PROJECT_KEY
        assert "token" not in call and "private_key" not in call
        _no_operator_material(call)


def test_fluid_verify_still_uses_the_operator_s_stores_for_the_operator_s_account(
    tmp_path: Path,
) -> None:
    calls = _verify_smoke(
        tmp_path,
        {
            "SNOWFLAKE_ACCOUNT": "operator-acct",
            "SNOWFLAKE_USER": "operator_user",
            "SNOWFLAKE_DATABASE": "DEV_ANALYTICS",
            "SNOWFLAKE_SCHEMA": "COMMUNITY_SMOKE",
        },
        None,
    )

    assert calls
    assert (calls[0]["account"], calls[0]["user"]) == ("operator-acct", "operator_user")
    # the key pair the operator's stores hold (get_connection_params prefers it)
    assert "private_key" in calls[0] and "password" not in calls[0]


def test_fluid_auth_status_checks_the_quickstart_account_through_the_connector(
    tmp_path: Path,
) -> None:
    calls, snowsql = _fluid_on_smoke(tmp_path, {}, QUICKSTART_ENV, ("auth", "status", "snowflake"))

    assert snowsql == []
    (call,) = calls
    assert (call["account"], call["user"], call["password"]) == (
        "xy12345",
        "project_user",
        PROJECT_KEY,
    )
    _no_operator_material(call)


# ── the authenticator under the pin ─────────────────────────────────────────
#
# get_connection_params ranks an authenticator above a password, and the dbt
# profile, the OpenTofu provider, the hosted MCP server and dlt read
# SNOWFLAKE_AUTHENTICATOR from the environment. Values other than 'snowflake'
# can sign in with something the project files do not hold: the operator's
# browser session (externalbrowser), an identity provider login (an Okta URL),
# an OAuth or workload identity, a key. So the operator's own authenticator
# refuses the project's account like any credential of theirs, and one the
# project sets next to its account is dropped unless it is 'snowflake'.

# Every spelling a Snowflake reader takes it under: the resolver
# (SNOWFLAKE_, SNOWFLAKE__ and the bare key), resolve_snowflake_settings
# (SF_), and dlt's own section (read by the pinned dlt destination).
_AUTHENTICATOR_NAMES = [
    "SNOWFLAKE_AUTHENTICATOR",
    "SF_AUTHENTICATOR",
    "SNOWFLAKE__AUTHENTICATOR",
    "AUTHENTICATOR",
    "DESTINATION__SNOWFLAKE__CREDENTIALS__AUTHENTICATOR",
]
_FOREIGN_AUTHENTICATORS = [
    "externalbrowser",
    "oauth",
    "snowflake_jwt",
    "https://acme.okta.com",
    "WORKLOAD_IDENTITY",
    "programmatic_access_token",
]
# The quickstart .env with an authenticator that signs in with a browser.
_BROWSER = "SNOWFLAKE_AUTHENTICATOR=externalbrowser\n"  # pragma: allowlist secret
_PINNED_WITH_BROWSER = QUICKSTART_ENV + _BROWSER


def _hydrate_warnings(project: Path, caplog: pytest.LogCaptureFixture) -> List[str]:
    import logging

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(project)
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize("name", _AUTHENTICATOR_NAMES)
def test_an_operator_authenticator_under_any_name_refuses_the_project_account(name: str) -> None:
    project = {"SNOWFLAKE_ACCOUNT": "xy12345", "SNOWFLAKE_PASSWORD": PROJECT_KEY}

    refused = unpaired_endpoints(project, {name: "externalbrowser"})

    assert refused["SNOWFLAKE_ACCOUNT"].operator_credentials == (name,)
    assert not refused["SNOWFLAKE_ACCOUNT"].pairable


@pytest.mark.parametrize("value", _FOREIGN_AUTHENTICATORS + ["snowflake"])
def test_every_operator_authenticator_is_the_operator_s_material(value: str) -> None:
    project = {"SNOWFLAKE_ACCOUNT": "xy12345", "SNOWFLAKE_PASSWORD": PROJECT_KEY}

    refused = unpaired_endpoints(project, {"SNOWFLAKE_AUTHENTICATOR": value})

    assert refused["SNOWFLAKE_ACCOUNT"].operator_credentials == ("SNOWFLAKE_AUTHENTICATOR",)


def test_an_operator_authenticator_with_the_project_s_value_still_refuses_the_account() -> None:
    # The project file setting the same value does not make the operator's
    # environment variable, which readers see, the project's.
    project = {
        "SNOWFLAKE_ACCOUNT": "xy12345",
        "SNOWFLAKE_PASSWORD": PROJECT_KEY,
        "SNOWFLAKE_AUTHENTICATOR": "externalbrowser",
    }

    refused = unpaired_endpoints(project, {"SNOWFLAKE_AUTHENTICATOR": "externalbrowser"})

    assert "SNOWFLAKE_ACCOUNT" in refused


def test_the_project_s_own_password_authenticator_seen_again_still_pairs() -> None:
    # A second load in one run sees what the first exported.
    project = {
        "SNOWFLAKE_ACCOUNT": "xy12345",
        "SNOWFLAKE_PASSWORD": PROJECT_KEY,
        "SNOWFLAKE_AUTHENTICATOR": "snowflake",
    }

    assert unpaired_endpoints(project, dict(project)) == {}


@pytest.mark.parametrize("source", ["shell", "secrets-file"])
def test_an_operator_authenticator_keeps_the_project_account_out(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    source: str,
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    if source == "shell":
        monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
    else:
        secrets = tmp_path / "operator.env"
        secrets.write_text(_BROWSER)
        monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))
    (project / ".env").write_text(QUICKSTART_ENV)

    warnings = _hydrate_warnings(project, caplog)

    assert "SNOWFLAKE_ACCOUNT" not in os.environ
    (warning,) = [w for w in warnings if "SNOWFLAKE_ACCOUNT" in w]
    assert "SNOWFLAKE_AUTHENTICATOR" in warning
    assert "put the credential in the same project file" not in warning
    with pytest.raises(ValueError, match="account not specified"):
        get_connection_params(project_root=project, environment="dev")


def _assert_no_project_authenticator(project: Path, name: str) -> None:
    from fluid_build.build_runners._credentials import get_credentials_for
    from fluid_build.providers.snowflake.util.config import get_connection_params

    assert name.upper() not in {key.upper() for key in os.environ}
    assert name not in DotEnvCredentialStore(project_root=project).load()
    params = get_connection_params(project_root=project, environment="dev")
    assert (params["account"], params["user"]) == ("xy12345", "project_user")
    assert params["password"] == PROJECT_KEY
    assert "authenticator" not in params, params["authenticator"]
    assert get_credentials_for("snowflake").get("authenticator") is None


@pytest.mark.parametrize("value", _FOREIGN_AUTHENTICATORS)
def test_a_project_authenticator_other_than_snowflake_is_dropped_next_to_its_account(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    _operator_stores(tmp_path / "home", monkeypatch)
    (project / ".env").write_text(QUICKSTART_ENV + f"SNOWFLAKE_AUTHENTICATOR={value}\n")

    warnings = _hydrate_warnings(project, caplog)

    _assert_no_project_authenticator(project, "SNOWFLAKE_AUTHENTICATOR")
    (warning,) = [w for w in warnings if "SNOWFLAKE_AUTHENTICATOR" in w]
    assert "(.env" in warning and "'snowflake'" in warning
    assert value not in warning
    assert os.environ["SNOWFLAKE_ACCOUNT"] == "xy12345"


@pytest.mark.parametrize(
    "name",
    [n for n in _AUTHENTICATOR_NAMES if n != "SNOWFLAKE_AUTHENTICATOR"]
    + ["snowflake_authenticator"],
)
def test_a_project_authenticator_under_any_name_is_dropped_next_to_its_account(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    # pydantic-settings (the dlt bridge's SnowflakeCredentials) reads names in
    # any case, so a lower-case spelling is dropped too.
    _operator_stores(tmp_path / "home", monkeypatch)
    (project / ".env").write_text(QUICKSTART_ENV + f"{name}=externalbrowser\n")

    warnings = _hydrate_warnings(project, caplog)

    _assert_no_project_authenticator(project, name)
    assert [w for w in warnings if name in w]


@pytest.mark.parametrize("env_file", [".env.dev", ".env.local"])
def test_a_project_authenticator_in_another_project_file_is_dropped_too(
    project: Path, caplog: pytest.LogCaptureFixture, env_file: str
) -> None:
    (project / ".env").write_text(QUICKSTART_ENV)
    (project / env_file).write_text(_BROWSER)

    warnings = _hydrate_warnings(project, caplog)

    _assert_no_project_authenticator(project, "SNOWFLAKE_AUTHENTICATOR")
    assert [w for w in warnings if f"SNOWFLAKE_AUTHENTICATOR ({env_file})" in w]


def test_an_operator_s_blank_authenticator_does_not_let_the_project_s_through(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A blank value wins the export, and the resolver then falls back to the
    # project's files for it.
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "")
    (project / ".env").write_text(_PINNED_WITH_BROWSER)

    hydrate_dotenv(project)

    assert "SNOWFLAKE_AUTHENTICATOR" not in DotEnvCredentialStore(project_root=project).load()
    from fluid_build.providers.snowflake.util.config import get_connection_params

    params = get_connection_params(project_root=project, environment="dev")
    assert (params["account"], params["password"]) == ("xy12345", PROJECT_KEY)
    assert "authenticator" not in params


@pytest.mark.parametrize("value", ["snowflake", "SNOWFLAKE"])
def test_the_password_authenticator_loads_next_to_the_project_account(
    project: Path, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    from fluid_build.providers.snowflake.util.config import get_connection_params

    (project / ".env").write_text(QUICKSTART_ENV + f"SNOWFLAKE_AUTHENTICATOR={value}\n")

    warnings = _hydrate_warnings(project, caplog)
    hydrate_dotenv(project)  # a second load in the same run

    assert not [w for w in warnings if "AUTHENTICATOR" in w or "SNOWFLAKE_ACCOUNT" in w]
    assert os.environ["SNOWFLAKE_AUTHENTICATOR"] == value
    params = get_connection_params(project_root=project, environment="dev")
    assert (params["account"], params["authenticator"], params["password"]) == (
        "xy12345",
        value,
        PROJECT_KEY,
    )


@pytest.mark.parametrize(
    "credential, authenticator",
    [("SNOWFLAKE_OAUTH_TOKEN", "oauth"), ("SNOWFLAKE_PRIVATE_KEY_PATH", "snowflake_jwt")],
)
def test_a_project_token_or_key_path_signs_in_without_the_dropped_authenticator(
    project: Path, credential: str, authenticator: str
) -> None:
    # The docs say a project OAuth token or key path does not need 'oauth' or
    # 'snowflake_jwt': the connection settings and the dbt profile choose
    # that sign-in from the token or key path itself.
    from fluid_build.providers.snowflake.util.config import get_connection_params

    (project / "key.p8").write_text("not a key\n")
    value = str(project / "key.p8") if credential.endswith("PATH") else PROJECT_KEY
    (project / ".env").write_text(
        "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_USER=project_user\n"  # pragma: allowlist secret
        f"{credential}={value}\nSNOWFLAKE_AUTHENTICATOR={authenticator}\n"
    )

    hydrate_dotenv(project)
    params = get_connection_params(project_root=project, environment="dev")
    output = _dbt_snowflake_output()

    assert "SNOWFLAKE_AUTHENTICATOR" not in os.environ
    if credential == "SNOWFLAKE_OAUTH_TOKEN":
        assert (params["authenticator"], params["oauth_token"]) == ("oauth", PROJECT_KEY)
        assert (output["authenticator"], output["token"]) == ("oauth", PROJECT_KEY)
    else:
        assert params["private_key_path"] == value and "authenticator" not in params
        assert output["private_key_path"] == value and "authenticator" not in output


def test_a_project_authenticator_still_loads_for_the_operator_s_own_account(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No project account is in effect: the operator chose the account.
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "operator-acct")
    (project / ".env").write_text(
        "SNOWFLAKE_ACCOUNT=xy12345\nSNOWFLAKE_PASSWORD="
        f"{PROJECT_KEY}\nSNOWFLAKE_AUTHENTICATOR=externalbrowser\n"
    )

    hydrate_dotenv(project)

    assert os.environ["SNOWFLAKE_ACCOUNT"] == "operator-acct"
    assert os.environ["SNOWFLAKE_AUTHENTICATOR"] == "externalbrowser"


def test_a_project_authenticator_loads_when_the_project_sets_no_account(project: Path) -> None:
    (project / ".env").write_text("SNOWFLAKE_WAREHOUSE=PROJECT_WH\n")
    (project / ".env.local").write_text(_BROWSER)

    hydrate_dotenv(project)

    assert os.environ["SNOWFLAKE_AUTHENTICATOR"] == "externalbrowser"


# The readers outside the resolver, each with the project's authenticator.


def _dbt_snowflake_output() -> Dict[str, Any]:
    from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile

    build = {"execution": {"runtime": {"platform": "snowflake"}}, "properties": {}}
    profile = _build_generated_dbt_profile(build, {})
    assert profile is not None
    return profile["default"]["outputs"]["dev"]


def test_the_dbt_profile_gets_no_project_authenticator_for_the_project_account(
    project: Path,
) -> None:
    (project / ".env").write_text(_PINNED_WITH_BROWSER)
    hydrate_dotenv(project)

    output = _dbt_snowflake_output()

    assert (output["account"], output["password"]) == ("xy12345", PROJECT_KEY)
    assert "authenticator" not in output


def test_the_dbt_profile_never_pairs_the_project_account_with_the_operator_s_authenticator(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
    (project / ".env").write_text(QUICKSTART_ENV)
    hydrate_dotenv(project)

    output = _dbt_snowflake_output()

    assert output["account"] != "xy12345"


def test_opentofu_and_the_hosted_mcp_get_no_project_authenticator(project: Path) -> None:
    from fluid_build.iac.credentials import build_tofu_env
    from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

    (project / ".env").write_text(_PINNED_WITH_BROWSER.replace("xy12345", "myorg-xy12345"))
    hydrate_dotenv(project)

    tofu = build_tofu_env()
    tofu.update(SnowflakeIacPlugin().credential_env(tofu))
    mcp = _snowflake_mcp_env({**os.environ, "FLUID_SNOWFLAKE_MCP": "1"})

    assert tofu["SNOWFLAKE_ACCOUNT_NAME"] == "xy12345"
    assert "SNOWFLAKE_AUTHENTICATOR" not in tofu
    assert mcp["SNOWFLAKE_ACCOUNT"] == "myorg-xy12345"
    assert "SNOWFLAKE_AUTHENTICATOR" not in mcp


def test_opentofu_and_mcp_never_get_the_project_account_with_an_operator_authenticator(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.iac.credentials import build_tofu_env
    from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
    (project / ".env").write_text(QUICKSTART_ENV.replace("xy12345", "myorg-xy12345"))
    hydrate_dotenv(project)

    tofu = build_tofu_env()
    tofu.update(SnowflakeIacPlugin().credential_env(tofu))
    mcp = _snowflake_mcp_env({**os.environ, "FLUID_SNOWFLAKE_MCP": "1"})

    assert tofu.get("SNOWFLAKE_ACCOUNT_NAME") != "xy12345"
    assert mcp.get("SNOWFLAKE_ACCOUNT") != "myorg-xy12345"


@pytest.mark.parametrize(
    "name", ["SNOWFLAKE_AUTHENTICATOR", "DESTINATION__SNOWFLAKE__CREDENTIALS__AUTHENTICATOR"]
)
def test_the_dlt_snowflake_destination_gets_no_project_authenticator(
    tmp_path: Path, name: str
) -> None:
    fields = _dlt_snowflake_credentials(tmp_path, QUICKSTART_ENV + f"{name}=externalbrowser\n", {})

    assert (fields["host"], fields["password"]) == ("xy12345", PROJECT_KEY)
    assert (fields["private_key"], fields["authenticator"]) == (None, None)


def _fluid_auth_check(project: Path, monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    import asyncio
    import logging

    import fluid_build.providers.snowflake.connection as connection
    from fluid_build.cli.auth import SnowflakeAuthProvider

    monkeypatch.chdir(project)  # `fluid auth` reads the .env of the directory it runs in
    connected: List[Dict[str, Any]] = []

    def connect(**params: Any) -> None:
        connected.append(params)
        raise RuntimeError("recorded, not connected")

    monkeypatch.setattr(connection, "SnowflakeConnection", connect)
    monkeypatch.setattr(SnowflakeAuthProvider, "_run_command", lambda self, command, **kw: None)
    provider = SnowflakeAuthProvider({}, logging.getLogger("test.auth"))
    asyncio.run(provider.check_auth())
    return connected


def test_fluid_auth_checks_the_project_account_without_the_project_s_authenticator(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / ".env").write_text(_PINNED_WITH_BROWSER)

    (params,) = _fluid_auth_check(project, monkeypatch)

    assert (params["account"], params["password"]) == ("xy12345", PROJECT_KEY)
    assert "authenticator" not in params


def test_fluid_auth_never_takes_the_project_account_with_the_operator_s_authenticator(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "externalbrowser")
    (project / ".env").write_text(QUICKSTART_ENV)

    connected = _fluid_auth_check(project, monkeypatch)

    assert [p for p in connected if p.get("account") == "xy12345"] == []


# ``fluid forge data-model dump-ddl`` hands get_connection_params' result to
# snowflake.connector.connect as it is, authenticator included.
_DUMP_DDL = (
    "forge",
    "data-model",
    "dump-ddl",
    "--database",
    "DEV_ANALYTICS",
    "--schema",
    "COMMUNITY_SMOKE",
    "--output",
    "schema.sql",
)


def test_fluid_dump_ddl_signs_in_to_the_project_account_with_its_password_not_a_browser(
    tmp_path: Path,
) -> None:
    calls, _snowsql = _fluid_on_smoke(tmp_path, {}, _PINNED_WITH_BROWSER, _DUMP_DDL)

    assert calls
    for call in calls:
        assert (call["account"], call["password"]) == ("xy12345", PROJECT_KEY)
        assert "authenticator" not in call
        _no_operator_material(call)


def test_fluid_dump_ddl_never_takes_the_project_account_with_the_operator_s_authenticator(
    tmp_path: Path,
) -> None:
    calls, _snowsql = _fluid_on_smoke(
        tmp_path,
        {"SNOWFLAKE_AUTHENTICATOR": "externalbrowser"},
        QUICKSTART_ENV,
        _DUMP_DDL,
        connects=False,
    )

    assert [call for call in calls if call.get("account") == "xy12345"] == []
