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

"""Every FLUID_* variable is classified for project dotenv files.

A denylist that silently misses a new field is the classic way this kind of
guard fails, so a new ``FLUID_*`` name anywhere in the package must be placed
in ``fluid_build/credentials/protected_env.py`` (protected, loadable, or not a
process variable) before it can ship.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fluid_build.credentials.protected_env import (
    DOTENV_LOADABLE,
    ENDPOINT_CREDENTIAL_GROUPS,
    NOT_PROCESS_ENV,
    PROTECTED_ENV_KEYS,
    is_protected_env_key,
    unpaired_endpoints,
)

PACKAGE = Path(__file__).resolve().parents[1] / "fluid_build"
# A quoted literal (or f-string head) that starts with FLUID_.
_FLUID_LITERAL = re.compile(r"""["']((?:FLUID_)[A-Z0-9_]*)""")


def _fluid_names_in_package() -> set:
    names = set()
    for path in PACKAGE.rglob("*.py"):
        names.update(_FLUID_LITERAL.findall(path.read_text(encoding="utf-8")))
    return names


def test_every_fluid_variable_in_the_package_is_classified() -> None:
    names = _fluid_names_in_package()
    assert len(names) > 100, "the scan found too few names; is PACKAGE right?"
    unclassified = sorted(
        n
        for n in names
        if not is_protected_env_key(n) and n not in DOTENV_LOADABLE and n not in NOT_PROCESS_ENV
    )
    assert unclassified == [], (
        "Classify these in fluid_build/credentials/protected_env.py: may a project "
        f".env set them? {unclassified}"
    )


def test_the_classes_do_not_overlap() -> None:
    assert not [n for n in DOTENV_LOADABLE if is_protected_env_key(n)]
    assert not (DOTENV_LOADABLE & NOT_PROCESS_ENV)


def test_listed_names_are_upper_case() -> None:
    for name in PROTECTED_ENV_KEYS | DOTENV_LOADABLE | NOT_PROCESS_ENV:
        assert name == name.upper(), name


def test_the_names_agree_with_the_modules_that_read_them() -> None:
    pytest.importorskip("duckdb")
    from fluid_build.providers._duckdb_sandbox import OPERATOR_DIRS_ENV
    from fluid_build.util.ref_confinement import REF_ROOT_ENV

    assert is_protected_env_key(OPERATOR_DIRS_ENV)
    assert is_protected_env_key(REF_ROOT_ENV)


@pytest.mark.parametrize(
    "name",
    [
        "FLUID_DUCKDB_ALLOWED_DIRS",
        "FLUID_REF_ROOT",
        "FLUID_SECRETS_FILE",
        "FLUID_OPENLINEAGE_ALLOW_PRIVATE",
        "FLUID_ALLOW_METADATA_SERVICE",
        "FLUID_DBT_FORWARD_ENV",
        "FLUID_OVERLAY_STRICT",
        "FLUID_OVERLAY_PUBLIC_KEYS",
        "FLUID_PLUGIN_STRICT_COMPAT",
        "FLUID_RUNNER_HOST_OVERRIDE",
        "FLUID_COMMAND_CENTER_URL",
        "FLUID_COMMAND_CENTER_ENABLED",
        "FLUID_API_URL",
        "FLUID_CC_ENDPOINT",
        "FLUID_COMMAND_CENTER_HOST_ALLOWLIST",
        "FLUID_MCP_AUTH_MODE",
        "FLUID_MCP_JWT_JWKS_URL",
        "FLUID_MCP_JWT_ISSUER",
        "FLUID_LLM_ENDPOINT",
        "FLUID_MCP_TRUSTED_PROXIES",
        "FLUID_FEDERATION_SECRET_REF_ALLOWLIST",
        "fluid_ref_root",
        "https_proxy",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "AWS_ENDPOINT_URL_S3",
        "GIT_CONFIG_COUNT",
        # The key for the operator's own ~/.fluid/credentials.enc.
        "FLUID_ENCRYPTION_KEY",
        "FLUID_ENCRYPTION_PASSPHRASE",
        # SDK config files that can name a credential helper program or an
        # endpoint, and the switch that lets google-auth run one.
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "KUBECONFIG",
        "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
        "CLOUDSDK_CONFIG",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        # A knob nobody has listed yet is protected by its shape.
        "FLUID_SOMETHING_NEW_URL",
        "FLUID_SOMETHING_TRUSTED_NETWORKS",
        # Catalog endpoints the registrars fall back to, next to a token the
        # operator's environment supplies.
        "DATAHUB_GMS_URL",
        "OPENMETADATA_SERVER_URL",
        "OPENMETADATA_HOST",
        "SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL",
        "BIGQUERY_EMULATOR_HOST",
        # Proxy and CA settings, in either case: curl and httpx read the
        # lower-case proxy names too.
        "http_proxy",
        "all_proxy",
        "no_proxy",
        "ssl_cert_file",
        "ssl_cert_dir",
        "requests_ca_bundle",
        "curl_ca_bundle",
        "SSLKEYLOGFILE",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "NODE_EXTRA_CA_CERTS",
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
        # git: TLS verification and client certificates (http.ssl*) ...
        "GIT_SSL_NO_VERIFY",
        "GIT_SSL_CAINFO",
        "GIT_SSL_CAPATH",
        "GIT_SSL_CERT",
        "GIT_SSL_KEY",
        "GIT_SSL_CERT_PASSWORD_PROTECTED",
        "GIT_SSL_VERSION",
        "GIT_SSL_CIPHER_LIST",
        # ... the proxy command and proxy TLS (core.gitProxy, http.proxySSL*) ...
        "GIT_PROXY_COMMAND",
        "GIT_PROXY_SSL_CAINFO",
        # ... traces that write request headers or chosen env values to a file
        "GIT_TRACE",
        "GIT_TRACE_CURL",
        "GIT_TRACE_PACKET",
        "GIT_TRACE_REDACT",
        "GIT_TRACE2",
        "GIT_TRACE2_EVENT",
        "GIT_TRACE2_ENV_VARS",
        "GIT_CURL_VERBOSE",
        # ... which repository, config and templates git uses, and the
        # programs and protocols it runs
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_TEMPLATE_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_ALLOW_PROTOCOL",
        "GIT_EXTERNAL_DIFF",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
        "PAGER",
        "git_ssl_no_verify",
        # the home and per-user config directories that ``~`` and the
        # tools the CLI runs (git, SDKs) resolve their own config from
        "HOME",
        "home",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
        "XDG_DATA_HOME",
        "XDG_DATA_DIRS",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        # the coding-agent CLIs' own config directories (endpoint, key name,
        # Claude Code's apiKeyHelper program)
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        # litellm's proxy gets whichever provider key FLUID resolved
        "USE_LITELLM_PROXY",
        "LITELLM_PROXY_API_BASE",
        # litellm's github_copilot/ route sends the Copilot key it stored,
        # never the key FLUID passes, and GITHUB_COPILOT_API_BASE is where
        # it sends it when that key names no endpoint
        "GITHUB_COPILOT_API_BASE",
        "github_copilot_api_base",
        "GITHUB_COPILOT_API_KEY_URL",
        "GITHUB_COPILOT_ACCESS_TOKEN_URL",
        "GITHUB_COPILOT_DEVICE_CODE_URL",
        "GITHUB_COPILOT_TOKEN_DIR",
        "GITHUB_COPILOT_ACCESS_TOKEN_FILE",
        "GITHUB_COPILOT_API_KEY_FILE",
        # libpq's CA and revocation lists
        "PGSSLROOTCERT",
        "PGSSLCRL",
        "PGSSLCRLDIR",
        # Windows' profile, system-wide data, temp and shell locations, in
        # the spellings Windows itself uses
        "APPDATA",
        "LocalAppData",
        "ProgramData",
        "ALLUSERSPROFILE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "ComSpec",
        "SystemRoot",
        "windir",
        # python-dotenv's off switch and litellm's import-time .env loading
        "PYTHON_DOTENV_DISABLED",
        "LITELLM_MODE",
        # litellm's ollama/ route
        "OLLAMA_API_BASE",
        # the Debezium runner passes Debezium Server the credentials its
        # application.properties refers to as ${FLUID_DBZ_<PROPERTY>}
        "FLUID_DBZ_SOURCE_DATABASE_PASSWORD",
        "fluid_dbz_sink_iceberg_s3_secret_access_key",
    ],
)
def test_protected(name: str) -> None:
    assert is_protected_env_key(name)


@pytest.mark.parametrize(
    "name",
    [
        "FLUID_COMMAND_CENTER_API_KEY",
        "FLUID_LLM_API_KEY",
        "DMM_API_KEY",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_ACCOUNT",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_PROFILE",
        "FLUID_ENV",
        "FLUID_LOG_LEVEL",
        "DATABASE_URL",
        # git settings that neither weaken transport trust nor run anything
        "GIT_TERMINAL_PROMPT",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_HTTP_LOW_SPEED_LIMIT",
        "",
    ],
)
def test_loadable(name: str) -> None:
    assert not is_protected_env_key(name)


def test_every_catalog_endpoint_variable_is_protected() -> None:
    # A catalog backend pairs the endpoint it reads from the environment with
    # the token it reads from there too. A token may come from a project .env;
    # the endpoint may not, or the operator's token goes to the project's host.
    import fluid_build.build_runners.catalog_registrars  # noqa: F401  (registers)
    from fluid_build.api.catalog_backend import all_catalog_backend_specs

    specs = all_catalog_backend_specs()
    assert specs, "no catalog backend registered; is the import right?"
    loadable = {"api_token", "token", "password", "username", "api_key"}
    unprotected = sorted(
        f"{spec.name}.{field}: {name}"
        for spec in specs
        for field, names in spec.env_vars.items()
        if field not in loadable
        for name in names
        if not is_protected_env_key(name)
    )
    assert unprotected == [], unprotected


# ── third-party endpoint groups (the pairing rule) ───────────────────────


def test_endpoint_groups_are_well_formed() -> None:
    services = [g.service for g in ENDPOINT_CREDENTIAL_GROUPS]
    assert len(services) == len(set(services))
    for group in ENDPOINT_CREDENTIAL_GROUPS:
        assert group.endpoints and group.credentials and group.read_by, group.service
        for name in group.endpoints + group.credentials:
            assert name == name.upper(), name
        # An endpoint that is protected outright never reaches the pairing
        # rule, and a protected credential could never make a pair.
        for name in group.endpoints + group.credentials:
            if "*" not in name:
                assert not is_protected_env_key(name), name
        assert not set(group.endpoints) & set(group.credentials), group.service


def _refused(name: str, operator_credential: str) -> bool:
    return name in unpaired_endpoints({name: "h.test"}, {operator_credential: "k"})


def test_the_groups_agree_with_the_modules_that_read_them() -> None:
    # The Postgres settings FLUID's own readers pair with a password.
    pytest.importorskip("pydantic_settings")
    from fluid_build.build_runners._credentials import (
        PostgresCredentials,
        RedshiftCredentials,
    )

    def aliases(model: type, field: str) -> list:
        alias = model.model_fields[field].validation_alias  # type: ignore[attr-defined]
        return [str(a).upper() for a in getattr(alias, "choices", [field])]

    for endpoint in aliases(PostgresCredentials, "host") + aliases(PostgresCredentials, "port"):
        for password in aliases(PostgresCredentials, "password"):
            assert _refused(endpoint, password), (endpoint, password)
    assert RedshiftCredentials.model_config.get("env_prefix") == "REDSHIFT_"
    assert _refused("REDSHIFT_HOST", "REDSHIFT_PASSWORD")
    assert _refused("REDSHIFT_PORT", "REDSHIFT_PASSWORD")
    # libpq / dbt-postgres, Snowflake (account and the resolver's bare key),
    # Databricks, and the LLM SDKs and routes litellm reads.
    for endpoint, credential in [
        ("PGHOST", "PGPASSWORD"),
        ("PGPORT", "PGPASSWORD"),
        ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PASSWORD"),
        ("SF_ACCOUNT", "SF_PASSWORD"),
        ("ACCOUNT", "SNOWFLAKE_PRIVATE_KEY_PATH"),
        ("SNOWFLAKE_HOST", "SNOWFLAKE_TOKEN"),
        ("DATABRICKS_HOST", "DATABRICKS_TOKEN"),
        ("DATABRICKS_HOST", "DATABRICKS_CLIENT_SECRET"),
        ("OPENAI_BASE_URL", "OPENAI_API_KEY"),
        ("OPENAI_API_BASE", "FLUID_LLM_API_KEY"),
        ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
        ("GEMINI_API_BASE", "GOOGLE_API_KEY"),
        ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY"),
        ("AZURE_API_BASE", "AZURE_API_KEY"),
        ("GITHUB_API_BASE", "GITHUB_API_KEY"),
        ("MYSQL_HOST", "MYSQL_PWD"),
        ("DESTINATION__POSTGRES__CREDENTIALS__HOST", "POSTGRES_PASSWORD"),
        ("AZURE_AUTHORITY_HOST", "AZURE_CLIENT_SECRET"),
    ]:
        assert _refused(endpoint, credential), (endpoint, credential)
    from fluid_build.llm.providers import PROVIDER_ENV_VARS

    assert PROVIDER_ENV_VARS["gemini"] == "GOOGLE_API_KEY"
    assert PROVIDER_ENV_VARS["github"] == "GITHUB_API_KEY"
