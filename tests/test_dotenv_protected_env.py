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

"""Project dotenv files cannot set operator-only security settings.

``fluid apply`` / ``verify`` / ``publish`` load the project's ``.env``,
``.env.{env}`` and ``.env.local`` into the process environment. Those files
belong to the project, which may be written by someone other than the person
running the CLI, so they may carry credentials but never the settings that
widen a sandbox, change an authentication or trust mode, or change where data
and credentials are sent. Those come only from the real environment (shell, CI)
or from an operator-chosen ``FLUID_SECRETS_FILE``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Iterator

import pytest

pytest.importorskip("dotenv")

from fluid_build.cli._common import hydrate_dotenv  # noqa: E402
from fluid_build.credentials.dotenv_store import (  # noqa: E402
    DotEnvCredentialStore,
    reset_dotenv_load_log,
)

_KEYS = (
    "FLUID_DUCKDB_ALLOWED_DIRS",
    "FLUID_REF_ROOT",
    "FLUID_SECRETS_FILE",
    "FLUID_COMMAND_CENTER_URL",
    "FLUID_COMMAND_CENTER_API_KEY",
    "FLUID_OPENLINEAGE_ALLOW_PRIVATE",
    "FLUID_MCP_TRUSTED_PROXIES",
    "FLUID_FEDERATION_SECRET_REF_ALLOWLIST",
    "fluid_duckdb_allowed_dirs",
    "HTTPS_PROXY",
    "LD_PRELOAD",
    "FLUID_ENCRYPTION_KEY",
    "FLUID_ENCRYPTION_PASSPHRASE",
    "AWS_CONFIG_FILE",
    "KUBECONFIG",
    "PROTECTED_ENV_TEST_API_KEY",
    "PROTECTED_ENV_TEST_FROM_SECRETS",
    "PROTECTED_ENV_TEST_OPERATOR_SECRET",
    "PROTECTED_ENV_TEST_LABEL",
    "FLUID_CC_ENDPOINT",
    "FLUID_API_KEY",
    "FLUID_COMMAND_CENTER_HOST_ALLOWLIST",
    "FLUID_FEDERATION_HOST_ALLOWLIST",
    "DMM_API_URL",
    "DMM_API_KEY",
    "DATAHUB_GMS_URL",
    "DATAHUB_GMS_TOKEN",
    "OPENMETADATA_SERVER_URL",
    "OPENMETADATA_HOST",
    "OPENMETADATA_JWT_TOKEN",
)
_TEST_API_KEY = "test-key-not-a-secret"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    # Anything a test plants in either case, and no operator config file.
    for key in list(os.environ):
        if key.upper().startswith(("GIT_", "HTTP", "ALL_PROXY", "NO_PROXY", "SSL_CERT")):
            monkeypatch.delenv(key, raising=False)
        elif key.upper() in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "FLUID_CONFIG"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_dotenv_load_log()
    before = dict(os.environ)
    yield
    # The store writes os.environ directly, which monkeypatch never saw.
    for key in set(os.environ) - set(before):
        del os.environ[key]
    reset_dotenv_load_log()


def _warnings(caplog: pytest.LogCaptureFixture) -> list:
    """The skipped-settings warnings (not the unrelated file-mode warning)."""
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage().startswith("Ignored ")
    ]


def test_project_dotenv_cannot_set_protected_keys_but_credentials_still_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secrets = tmp_path / "planted.env"
    secrets.write_text("PROTECTED_ENV_TEST_FROM_SECRETS=planted\n")
    (tmp_path / ".env").write_text(
        f"FLUID_DUCKDB_ALLOWED_DIRS={tmp_path.parent}\n"
        "FLUID_REF_ROOT=/\n"
        f"FLUID_SECRETS_FILE={secrets}\n"
        "FLUID_COMMAND_CENTER_URL=http://127.0.0.1:9/collect\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path)

    assert "FLUID_DUCKDB_ALLOWED_DIRS" not in os.environ
    assert "FLUID_REF_ROOT" not in os.environ
    assert "FLUID_SECRETS_FILE" not in os.environ
    assert "FLUID_COMMAND_CENTER_URL" not in os.environ
    # The secrets file a project .env names is not trusted either.
    assert "PROTECTED_ENV_TEST_FROM_SECRETS" not in os.environ
    # Credentials are what .env is for: they still load.
    assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY

    warnings = _warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    for name in (
        "FLUID_COMMAND_CENTER_URL",
        "FLUID_DUCKDB_ALLOWED_DIRS",
        "FLUID_REF_ROOT",
        "FLUID_SECRETS_FILE",
    ):
        assert name in message
    # Names only, never values, and it says what to do instead.
    assert str(tmp_path.parent) not in message
    assert "127.0.0.1" not in message
    assert str(secrets) not in message
    assert _TEST_API_KEY not in message
    assert "export" in message.lower()


@pytest.mark.parametrize("filename", [".env.dev", ".env.local"])
def test_environment_and_local_dotenv_files_are_filtered_too(tmp_path: Path, filename: str) -> None:
    (tmp_path / filename).write_text(
        f"FLUID_DUCKDB_ALLOWED_DIRS={tmp_path.parent}\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )

    hydrate_dotenv(tmp_path, environment="dev")

    assert "FLUID_DUCKDB_ALLOWED_DIRS" not in os.environ
    assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY


def test_project_dotenv_cannot_choose_the_operator_store_key_or_sdk_config(tmp_path: Path) -> None:
    # The encryption key unlocks the operator's own ~/.fluid/credentials.enc,
    # and SDK config files can name a credential helper program to run: both
    # belong to the operator, not to the project.
    (tmp_path / ".env").write_text(
        "FLUID_ENCRYPTION_KEY=project-chosen-key\n"  # pragma: allowlist secret
        "FLUID_ENCRYPTION_PASSPHRASE=project-chosen-passphrase\n"  # pragma: allowlist secret
        f"AWS_CONFIG_FILE={tmp_path / 'aws-config'}\n"
        f"KUBECONFIG={tmp_path / 'kubeconfig'}\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    for name in (
        "FLUID_ENCRYPTION_KEY",
        "FLUID_ENCRYPTION_PASSPHRASE",
        "AWS_CONFIG_FILE",
        "KUBECONFIG",
    ):
        assert name not in os.environ, name
    assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY


def test_protected_key_matching_ignores_case(tmp_path: Path) -> None:
    # Windows environment names are case-insensitive, so a lower-case key in
    # .env would set the real variable there.
    (tmp_path / ".env").write_text(f"fluid_duckdb_allowed_dirs={tmp_path.parent}\n")

    hydrate_dotenv(tmp_path)

    assert "fluid_duckdb_allowed_dirs" not in os.environ
    assert "FLUID_DUCKDB_ALLOWED_DIRS" not in os.environ


def test_variables_decided_after_the_list_was_written_are_protected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "FLUID_MCP_TRUSTED_PROXIES=0.0.0.0/0\n"
        "FLUID_FEDERATION_SECRET_REF_ALLOWLIST=*\n"
        "FLUID_OPENLINEAGE_ALLOW_PRIVATE=1\n"
        "HTTPS_PROXY=http://127.0.0.1:9\n"
        "LD_PRELOAD=/tmp/x.so\n"
    )

    hydrate_dotenv(tmp_path)

    for name in (
        "FLUID_MCP_TRUSTED_PROXIES",
        "FLUID_FEDERATION_SECRET_REF_ALLOWLIST",
        "FLUID_OPENLINEAGE_ALLOW_PRIVATE",
        "HTTPS_PROXY",
        "LD_PRELOAD",
    ):
        assert name not in os.environ, name


def test_project_dotenv_cannot_move_the_home_or_config_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With HOME unset (a stripped CI or service environment) a project value
    # would otherwise become the directory ``~`` and the tools the CLI runs
    # read their own config from.
    monkeypatch.delenv("HOME")
    for name in ("USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    planted = tmp_path / "planted"
    (tmp_path / ".env").write_text(
        f"HOME={planted}\n"
        f"USERPROFILE={planted}\n"
        f"XDG_CONFIG_HOME={planted}\n"
        f"XDG_DATA_HOME={planted}\n"
        f"XDG_CACHE_HOME={planted}\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert name not in os.environ, name
    assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY


def test_a_dotenv_cannot_redirect_a_home_relative_secrets_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator names their secrets file relative to their home. Which
    # file that is gets decided before any project file loads, so a project
    # value for HOME (when the shell left it unset) cannot choose it.
    planted = tmp_path / "planted"
    planted.mkdir()
    name = "fluid-protected-env-test-secrets.env"
    (planted / name).write_text(f"FLUID_DUCKDB_ALLOWED_DIRS={tmp_path.parent}\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text(f"HOME={planted}\n")
    monkeypatch.delenv("HOME")
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setenv("FLUID_SECRETS_FILE", f"~/{name}")

    hydrate_dotenv(project)

    assert "HOME" not in os.environ
    assert "FLUID_DUCKDB_ALLOWED_DIRS" not in os.environ


def test_the_secrets_file_path_is_fixed_before_project_files_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even for a variable the store let through, ``~`` in FLUID_SECRETS_FILE
    # means the operator's home as it was when the command started.
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    (operator_home / "secrets.env").write_text("PROTECTED_ENV_TEST_FROM_SECRETS=operator\n")
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("FLUID_SECRETS_FILE", "~/secrets.env")
    real_load = DotEnvCredentialStore.load

    def load_then_move_home(self: DotEnvCredentialStore) -> Dict[str, str]:
        result = real_load(self)
        os.environ["HOME"] = str(tmp_path / "moved")
        return result

    monkeypatch.setattr(DotEnvCredentialStore, "load", load_then_move_home)

    hydrate_dotenv(tmp_path)

    assert os.environ["HOME"] == str(tmp_path / "moved"), "the project store ran"
    assert os.environ.get("PROTECTED_ENV_TEST_FROM_SECRETS") == "operator"


def test_the_warning_names_each_project_that_sets_a_protected_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A long-lived process (an MCP server, a test run) can hydrate several
    # projects. Each one that sets the variable is reported, by file.
    first = tmp_path / "first"
    second = tmp_path / "second"
    for project, filename in ((first, ".env"), (second, ".env.local")):
        project.mkdir()
        (project / filename).write_text(f"FLUID_DUCKDB_ALLOWED_DIRS={tmp_path.parent}\n")

    with caplog.at_level(logging.WARNING):
        DotEnvCredentialStore(project_root=first).load()
        DotEnvCredentialStore(project_root=first).load()
        DotEnvCredentialStore(project_root=second).load()

    messages = [r.getMessage() for r in _warnings(caplog)]
    assert len(messages) == 2, messages
    assert "FLUID_DUCKDB_ALLOWED_DIRS (.env)" in messages[0]
    assert "FLUID_DUCKDB_ALLOWED_DIRS (.env.local)" in messages[1]


def test_a_protected_key_set_in_the_shell_still_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    shared = tmp_path / "shared"
    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", str(shared))
    (tmp_path / ".env").write_text("FLUID_DUCKDB_ALLOWED_DIRS=/elsewhere\n")

    with caplog.at_level(logging.WARNING):
        hydrate_dotenv(tmp_path)

    assert os.environ["FLUID_DUCKDB_ALLOWED_DIRS"] == str(shared)
    # Nothing was skipped on the operator's behalf: the shell value applies.
    assert _warnings(caplog) == []


def test_an_operator_secrets_file_is_trusted_for_protected_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "fluid.local.env"
    secrets.write_text(
        "FLUID_COMMAND_CENTER_URL=http://127.0.0.1:9/cc\n"
        f"FLUID_COMMAND_CENTER_API_KEY={_TEST_API_KEY}\n"
    )
    monkeypatch.setenv("FLUID_SECRETS_FILE", str(secrets))

    hydrate_dotenv(tmp_path)

    assert os.environ.get("FLUID_COMMAND_CENTER_URL") == "http://127.0.0.1:9/cc"
    assert os.environ.get("FLUID_COMMAND_CENTER_API_KEY") == _TEST_API_KEY


def test_the_store_never_returns_a_protected_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "FLUID_COMMAND_CENTER_URL=http://127.0.0.1:9/cc\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )
    store = DotEnvCredentialStore(project_root=tmp_path)

    assert store.get_credential("FLUID_COMMAND_CENTER_URL") is None
    assert store.get_credential("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY
    assert "FLUID_COMMAND_CENTER_URL" not in store.load()


def test_the_warning_is_logged_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Several credential consumers each build their own store in one run.
    (tmp_path / ".env").write_text(f"FLUID_DUCKDB_ALLOWED_DIRS={tmp_path.parent}\n")

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            DotEnvCredentialStore(project_root=tmp_path).load()

    assert len(_warnings(caplog)) == 1


# ── the card's end-to-end case: a declared input in a parent directory ────


def test_a_dotenv_cannot_let_a_contract_read_a_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("duckdb")
    from fluid_build.build_runners.base import _execute_embedded_sql_build

    parent = tmp_path / "parent"
    contract_dir = parent / "product"
    contract_dir.mkdir(parents=True)
    (parent / "outside.csv").write_text("token\nPARENT-FILE-CONTENTS\n", encoding="utf-8")
    out = contract_dir / "out" / "result.csv"
    build = {
        "id": "read_parent",
        "engine": "sql",
        "properties": {
            "sql": "SELECT * FROM outside",
            "parameters": {"inputs": [{"name": "outside", "path": str(parent / "outside.csv")}]},
        },
    }
    contract = {
        "id": "silver.dotenv_probe",
        "builds": [build],
        "consumes": [],
        "exposes": [
            {
                "exposeId": "out",
                "binding": {"platform": "local", "format": "csv", "location": {"path": str(out)}},
            }
        ],
    }
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    (contract_dir / ".env").write_text(f"FLUID_DUCKDB_ALLOWED_DIRS={parent}\n")

    hydrate_dotenv(contract_dir)

    assert _execute_embedded_sql_build(build, contract, contract_dir) == 1
    assert not out.exists()

    # The operator can still grant it from the real environment.
    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", str(parent))
    assert _execute_embedded_sql_build(build, contract, contract_dir) == 0
    assert "PARENT-FILE-CONTENTS" in out.read_text(encoding="utf-8")


def test_a_case_variant_cannot_shadow_an_operator_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pydantic-settings reads POSTGRES_HOST case-insensitively (and Windows
    # has one variable for both spellings), so a project ``postgres_host``
    # next to the operator's POSTGRES_HOST must not load.
    pytest.importorskip("pydantic_settings")
    from fluid_build.build_runners._credentials import PostgresCredentials

    monkeypatch.setenv("POSTGRES_HOST", "db.operator.test")
    (tmp_path / ".env").write_text("postgres_host=pg.project.test\n")

    hydrate_dotenv(tmp_path)

    assert PostgresCredentials().host == "db.operator.test"
    assert "postgres_host" not in os.environ


# ── transport trust: proxies, CA bundles, git TLS, in either case ─────────


def _set_in_any_case(name: str) -> bool:
    return any(key.upper() == name.upper() for key in os.environ)


_TRANSPORT_SETTINGS = (
    "HTTPS_PROXY=http://127.0.0.1:9\n"
    "https_proxy=http://127.0.0.1:9\n"
    "HTTP_PROXY=http://127.0.0.1:9\n"
    "http_proxy=http://127.0.0.1:9\n"
    "ALL_PROXY=socks5://127.0.0.1:9\n"
    "all_proxy=socks5://127.0.0.1:9\n"
    "NO_PROXY=*\n"
    "no_proxy=*\n"
    "SSL_CERT_FILE=./ca.pem\n"
    "ssl_cert_file=./ca.pem\n"
    "SSL_CERT_DIR=./certs\n"
    "ssl_cert_dir=./certs\n"
    "REQUESTS_CA_BUNDLE=./ca.pem\n"
    "requests_ca_bundle=./ca.pem\n"
    "CURL_CA_BUNDLE=./ca.pem\n"
    "curl_ca_bundle=./ca.pem\n"
    "SSLKEYLOGFILE=./tls-keys.log\n"
)


def test_proxy_and_ca_settings_are_refused_in_either_case(tmp_path: Path) -> None:
    # httpx, requests and curl read the lower-case proxy names as well, and a
    # proxy plus a project-chosen CA lets the proxy read every request.
    import urllib.request

    (tmp_path / ".env").write_text(
        _TRANSPORT_SETTINGS + f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )

    hydrate_dotenv(tmp_path)

    for line in _TRANSPORT_SETTINGS.splitlines():
        name = line.split("=", 1)[0]
        assert not _set_in_any_case(name), name
    assert urllib.request.getproxies() == {}
    assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY


_GIT_SETTINGS = (
    "GIT_SSL_NO_VERIFY=1",
    "GIT_SSL_CAINFO=./ca.pem",
    "GIT_SSL_CAPATH=./certs",
    "GIT_SSL_CERT=./client.pem",
    "GIT_SSL_KEY=./client.key",
    "GIT_PROXY_COMMAND=./proxy.sh",
    "GIT_PROXY_SSL_CAINFO=./ca.pem",
    "GIT_TRACE=1",
    "GIT_TRACE_CURL=./trace.log",
    "GIT_TRACE_PACKET=1",
    "GIT_TRACE_REDACT=0",
    "GIT_TRACE2_EVENT=./trace2.json",
    "GIT_TRACE2_ENV_VARS=PROTECTED_ENV_TEST_API_KEY",
    "GIT_CURL_VERBOSE=1",
    "GIT_SSH=./ssh.sh",
    "GIT_SSH_COMMAND=./ssh.sh",
    "GIT_DIR=./other-repo",
    "GIT_WORK_TREE=..",
    "GIT_COMMON_DIR=./other-repo",
    "GIT_TEMPLATE_DIR=./templates",
    "GIT_CONFIG=./gitconfig",
    "GIT_CONFIG_GLOBAL=./gitconfig",
    "GIT_ALLOW_PROTOCOL=ext",
    "GIT_EXTERNAL_DIFF=./diff.sh",
    "GIT_PAGER=./pager.sh",
    "git_ssl_no_verify=1",
)


@pytest.mark.parametrize("filename", [".env", ".env.dev", ".env.local"])
def test_git_transport_trust_and_program_settings_are_refused(
    tmp_path: Path, filename: str
) -> None:
    # The CLI runs git in the project directory (provenance, attestation,
    # federation) with its own environment, so a project file must not turn
    # off TLS verification, trace request headers to a file, or pick the
    # repository, config or programs git uses.
    (tmp_path / filename).write_text(
        "\n".join(_GIT_SETTINGS) + "\nGIT_TERMINAL_PROMPT=0\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )

    hydrate_dotenv(tmp_path, environment="dev")

    for line in _GIT_SETTINGS:
        name = line.split("=", 1)[0]
        assert not _set_in_any_case(name), name
    # Harmless git settings and credentials still load.
    assert os.environ.get("GIT_TERMINAL_PROMPT") == "0"
    assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY


# ── a file name that differs only in case gets no exemption ───────────────


@pytest.mark.parametrize("filename", [".Env", ".ENV.local", ".env.LOCAL"])
def test_a_case_variant_dotenv_file_name_gets_no_exemption(tmp_path: Path, filename: str) -> None:
    # The store opens .env, .env.<env> and .env.local by exact path. On a
    # case-insensitive filesystem (macOS, Windows, a Docker Desktop bind
    # mount) that open resolves to the case variant, and the filter is by
    # variable name, not file name, so the variant is filtered the same way.
    # On a case-sensitive filesystem the variant is never opened at all.
    (tmp_path / filename).write_text(
        "FLUID_FEDERATION_HOST_ALLOWLIST=evil.example\n"
        "HTTPS_PROXY=http://127.0.0.1:9\n"
        f"PROTECTED_ENV_TEST_API_KEY={_TEST_API_KEY}\n"
    )
    exact = tmp_path / filename.lower()
    case_insensitive = exact.exists()

    hydrate_dotenv(tmp_path)

    assert not _set_in_any_case("FLUID_FEDERATION_HOST_ALLOWLIST")
    assert not _set_in_any_case("HTTPS_PROXY")
    if case_insensitive:
        # Proof that the variant really was read, and filtered by key.
        assert os.environ.get("PROTECTED_ENV_TEST_API_KEY") == _TEST_API_KEY
    else:
        assert "PROTECTED_ENV_TEST_API_KEY" not in os.environ


# ── values are literal: no ${VAR} expansion from the operator environment ─


def test_project_dotenv_values_are_not_expanded_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "operator-sentinel-value"  # pragma: allowlist secret
    monkeypatch.setenv("PROTECTED_ENV_TEST_OPERATOR_SECRET", secret)
    (tmp_path / ".env").write_text(
        "PROTECTED_ENV_TEST_LABEL=${PROTECTED_ENV_TEST_OPERATOR_SECRET}\n"
        "PROTECTED_ENV_TEST_DEFAULTED=pre-${PROTECTED_ENV_TEST_OPERATOR_SECRET:-x}\n"
    )

    hydrate_dotenv(tmp_path)

    assert os.environ["PROTECTED_ENV_TEST_LABEL"] == "${PROTECTED_ENV_TEST_OPERATOR_SECRET}"
    assert (
        os.environ["PROTECTED_ENV_TEST_DEFAULTED"] == "pre-${PROTECTED_ENV_TEST_OPERATOR_SECRET:-x}"
    )
    store = DotEnvCredentialStore(project_root=tmp_path)
    assert secret not in (store.get_credential("PROTECTED_ENV_TEST_LABEL") or "")


def test_a_dotenv_cannot_copy_a_secret_past_the_contract_placeholder_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # publish refuses to resolve a secret-shaped ``{{ env.X }}`` in a contract
    # it ships to a catalog. Expansion in a project .env would copy the
    # secret into an innocuous name that the guard then resolves.
    from fluid_build._contract_loader import resolve_contract_env_templates

    secret = "operator-sentinel-value"  # pragma: allowlist secret
    monkeypatch.setenv("PROTECTED_ENV_TEST_OPERATOR_SECRET", secret)
    (tmp_path / ".env").write_text(
        "PROTECTED_ENV_TEST_LABEL=${PROTECTED_ENV_TEST_OPERATOR_SECRET}\n"
    )

    hydrate_dotenv(tmp_path)
    body = resolve_contract_env_templates(
        {
            "direct": "{{ env.PROTECTED_ENV_TEST_OPERATOR_SECRET }}",
            "copied": "{{ env.PROTECTED_ENV_TEST_LABEL }}",
        }
    )

    assert secret not in repr(body)


# ── the cross-branch regression corpus, one test per proven vector ────────


def test_dotenv_cannot_point_the_command_center_at_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.config_manager import FluidConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FLUID_API_KEY", _TEST_API_KEY)
    monkeypatch.setenv("PROTECTED_ENV_TEST_OPERATOR_SECRET", "operator-sentinel-value")
    (tmp_path / ".env").write_text(
        "FLUID_CC_ENDPOINT=http://127.0.0.1:9/x/${PROTECTED_ENV_TEST_OPERATOR_SECRET}\n"
        "FLUID_CATALOG_FLUID_CC_URL=http://127.0.0.1:9/y\n"
        "FLUID_COMMAND_CENTER_URL=http://127.0.0.1:9/z\n"
    )

    hydrate_dotenv(tmp_path)
    config = FluidConfig().get_catalog_config("fluid-command-center")

    assert "127.0.0.1:9" not in repr(config)
    for name in ("FLUID_CC_ENDPOINT", "FLUID_CATALOG_FLUID_CC_URL", "FLUID_COMMAND_CENTER_URL"):
        assert name not in os.environ, name


def test_dotenv_cannot_name_a_secrets_file_that_replaces_the_operator_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fluid_build.config_manager import FluidConfig

    monkeypatch.chdir(tmp_path)
    operator = "https://cc.operator.example"
    monkeypatch.setenv("FLUID_CC_ENDPOINT", operator)
    monkeypatch.setenv("FLUID_API_KEY", _TEST_API_KEY)
    (tmp_path / "ci.env").write_text("FLUID_CC_ENDPOINT=http://127.0.0.1:9/x\n")
    (tmp_path / ".env").write_text("FLUID_SECRETS_FILE=ci.env\n")

    hydrate_dotenv(tmp_path)

    assert os.environ["FLUID_CC_ENDPOINT"] == operator
    assert FluidConfig().get_catalog_config("fluid-command-center")["endpoint"] == operator


@pytest.mark.parametrize(
    "target,endpoint_var,token_var",
    [
        ("datamesh-manager", "DMM_API_URL", "DMM_API_KEY"),
        ("datahub", "DATAHUB_GMS_URL", "DATAHUB_GMS_TOKEN"),
        ("datahub", "FLUID_CATALOG_DATAHUB_URL", "DATAHUB_GMS_TOKEN"),
        ("openmetadata", "OPENMETADATA_SERVER_URL", "OPENMETADATA_JWT_TOKEN"),
        ("openmetadata", "OPENMETADATA_HOST", "OPENMETADATA_JWT_TOKEN"),
    ],
)
def test_dotenv_cannot_point_a_catalog_at_another_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    endpoint_var: str,
    token_var: str,
) -> None:
    # The operator's token comes from the real environment; the endpoint it
    # is sent to must not come from the project.
    from fluid_build.config_manager import FluidConfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_CATALOG_DATAHUB_URL", raising=False)
    monkeypatch.setenv(token_var, _TEST_API_KEY)
    (tmp_path / ".env").write_text(f"{endpoint_var}=https://127.0.0.1:9/catalog\n")

    hydrate_dotenv(tmp_path)
    config = FluidConfig().get_catalog_config(target)

    assert endpoint_var not in os.environ
    assert "127.0.0.1:9" not in repr(config)


@pytest.mark.parametrize("filename", [".env", ".env.dev", ".env.local"])
def test_dotenv_cannot_widen_the_host_or_secret_ref_allowlists(
    tmp_path: Path, filename: str
) -> None:
    from fluid_build.forge.federation import _federation_host_on_allowlist

    (tmp_path / filename).write_text(
        "FLUID_FEDERATION_HOST_ALLOWLIST=evil.example\n"
        "FLUID_FEDERATION_SECRET_REF_ALLOWLIST=CI_SECRET@evil.example\n"
        "FLUID_COMMAND_CENTER_HOST_ALLOWLIST=evil.example\n"
    )

    hydrate_dotenv(tmp_path, environment="dev")

    assert not _federation_host_on_allowlist("evil.example")
    for name in (
        "FLUID_FEDERATION_HOST_ALLOWLIST",
        "FLUID_FEDERATION_SECRET_REF_ALLOWLIST",
        "FLUID_COMMAND_CENTER_HOST_ALLOWLIST",
    ):
        assert name not in os.environ, name
