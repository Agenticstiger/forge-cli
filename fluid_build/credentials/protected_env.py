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

"""Environment variables a project dotenv file may never set.

``fluid apply`` / ``verify`` / ``publish`` (through ``cli._common.hydrate_dotenv``)
and every credential lookup that reads dotenv files (``credentials.resolver``)
load the project's ``.env``, ``.env.{environment}`` and ``.env.local`` into the
process environment. Those files travel with the project, and the project may
be written by someone other than the person running the CLI. They may carry
credentials, which is what they are for. They may not carry a setting that:

* widens a sandbox or a confinement root (``FLUID_DUCKDB_ALLOWED_DIRS``,
  ``FLUID_REF_ROOT``, ``FLUID_UPSTREAM_CONTRACTS``, ``FLUID_WORKSPACE_ROOT``,
  ``FLUID_FORGE_AGENT_CWD``, ``FLUID_LOCAL_DUCKDB_PATH``);
* moves where the CLI reads trusted configuration or writes state, audit and
  logs, or picks the key of the operator's own encrypted credential store
  (the home and XDG config directories ``HOME``, ``USERPROFILE``,
  ``XDG_CONFIG_HOME`` ..., their Windows counterparts ``APPDATA``,
  ``LOCALAPPDATA``, ``ProgramData``, ``ALLUSERSPROFILE``, the temporary
  directory ``TMPDIR``/``TEMP``/``TMP``, ``FLUID_SECRETS_FILE``, ``FLUID_ENCRYPTION_KEY``,
  ``FLUID_ENCRYPTION_PASSPHRASE``, ``FLUID_USER_HOME``, ``FLUID_HOME``,
  ``FLUID_CONFIG``, ``FLUID_CACHE_DIR``, ``FLUID_LOG_FILE``,
  ``FLUID_PRICES_JSON``, ``FLUID_STATE_BACKEND``, ``FLUID_STORE_*`` locations,
  ``FLUID_AUDIT_*``, ``FLUID_ATHENA_OUTPUT_LOCATION``);
* changes an authentication or trust mode (``FLUID_SAFE_MODE``,
  ``FLUID_AI_READY``, ``FLUID_ALLOW_*``, ``FLUID_OPENLINEAGE_ALLOW_PRIVATE``,
  ``FLUID_DBT_FORWARD_ENV``, ``FLUID_OVERLAY_*``, ``FLUID_PROMPT_OVERLAYS``,
  ``FLUID_PLUGIN_STRICT_COMPAT``, ``FLUID_PLUGINS_*``, ``FLUID_PROVIDERS``,
  ``FLUID_SECRETS_INMEMORY``, ``FLUID_TELEMETRY``, ``FLUID_MCP_AUTH_MODE``,
  ``FLUID_MCP_JWT_*``, ``FLUID_MCP_TRUSTED_PROXIES``, the MCP rate limits,
  every ``*_ALLOWLIST``, TLS, CA and proxy settings in either case
  (``HTTPS_PROXY``/``https_proxy``, ``SSL_CERT_FILE``, ``REQUESTS_CA_BUNDLE``,
  ``SSLKEYLOGFILE`` ...), git's (``GIT_SSL_*``, ``GIT_PROXY_*``,
  ``GIT_TRACE*``, ``GIT_CURL_*``), ``PGSSLMODE``,
  ``DMM_ALLOW_INSECURE_HTTP``, ``DMM_AUTO_APPROVE_ACCESS``,
  ``CUSTOM_CATALOG_AUTH_TYPE``);
* changes where data or credentials are sent: the Command Center, catalog,
  lineage, telemetry, webhook, registry, LLM and agent-tool endpoints and
  selectors (``FLUID_COMMAND_CENTER_URL``/``_ENABLED``, ``FLUID_API_URL``,
  ``FLUID_CC_ENDPOINT``, ``FLUID_CATALOG_*_URL``, ``FLUID_OPENLINEAGE_URL``,
  ``FLUID_LLM_ENDPOINT``, ``FLUID_LLM_PROVIDER``/``_MODEL``,
  ``FLUID_RUNNER_HOST_OVERRIDE``, ``FLUID_PUBLIC_REGISTRY``,
  ``FLUID_*_MCP`` opt-ins, ``DMM_API_URL``, ``DATAHUB_GMS_HOST``,
  ``OPENLINEAGE_URL``, ``OTEL_EXPORTER_OTLP_*ENDPOINT``, ``AWS_ENDPOINT_URL*``,
  ``VAULT_ADDR``, ``OLLAMA_HOST``, every endpoint variable a catalog backend
  reads (``DATAHUB_GMS_URL``, ``OPENMETADATA_SERVER_URL`` ...), and
  ``SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL``, ``BIGQUERY_EMULATOR_HOST`` ...);
* chooses what program or library runs in this process or its subprocesses
  (``FLUID_DBT_MCP_COMMAND``/``_ARGS``/``_ENV``, ``FLUID_OPENTOFU_VERSION``,
  ``DBT_EXECUTABLE``, ``PATH``, ``ComSpec``, ``SystemRoot``, ``PYTHONPATH``,
  ``LD_*``, ``DYLD_*``,
  ``NODE_OPTIONS``, ``GIT_SSH_COMMAND``, ``GIT_CONFIG_*``, ``GIT_DIR``,
  ``GIT_TEMPLATE_DIR``, ``GIT_ALLOW_PROTOCOL``, ``GIT_EXTERNAL_DIFF``,
  ``TF_CLI_CONFIG_FILE``, and the SDK settings that name a credential helper
  program or an endpoint: ``AWS_CONFIG_FILE``,
  ``AWS_SHARED_CREDENTIALS_FILE``, ``KUBECONFIG``, ``CLOUDSDK_CONFIG``,
  ``DOCKER_CONFIG``, ``DOCKER_HOST``,
  ``GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES`` ...);
* lets a tool re-read the project's dotenv files without this filter
  (``PYTHON_DOTENV_DISABLED``, ``LITELLM_MODE``): dbt loads the nearest
  ``.env`` when it starts and litellm loads one when it is imported, so once
  the CLI has read the project's files it switches python-dotenv off.

Credentials (API keys, tokens, passwords) stay loadable: an API key from
``.env`` can never be paired with a FLUID endpoint, proxy or SDK config file
from ``.env``, because those are protected.

Third-party endpoints (an LLM SDK's base URL, a warehouse host or account, a
driver's port) follow a pairing rule instead of a ban, because a project that
brings its own service legitimately names it. A project file may set such an
endpoint only for a credential it supplies itself: the variable is refused
when a credential of the same service is set in the real environment (the
SDK would send the operator's credential to the project's host), and, for a
service that also finds the operator's credential outside the environment
(keyring, a stored CLI login, an SDK config file, cloud default credentials),
whenever the project files do not supply one that every reader sends in its
place. A project credential a reader consults after the keyring (FLUID passes
the key it resolved, FLUID_LLM_API_KEY else the keyring, as litellm's
``api_key``, which beats ``GROQ_API_KEY``), or never (``CLAUDE_API_KEY`` for
litellm), is a decoy and pairs nothing; :attr:`EndpointGroup.paired_by` lists,
per code path, the credentials that do. :data:`ENDPOINT_CREDENTIAL_GROUPS`
lists the services and where each variable is read; the names litellm reads
to choose a provider's host that no group lists (``<PROVIDER>_API_BASE``,
``DATAROBOT_ENDPOINT``, ``SNOWFLAKE_ACCOUNT_ID`` ...) pair like the LLM groups
(:data:`UNLISTED_LLM_ENDPOINTS`); for any other name no group lists,
:func:`unpaired_endpoints` applies the same rule by name shape: an endpoint
ending in one of :data:`ENDPOINT_NAME_SUFFIXES` is refused while a credential
``<PREFIX>_*`` ending in one of :data:`CREDENTIAL_NAME_SUFFIXES` is set in the
real environment. This is how npm binds an auth token to the registry host it
was issued for, so a project ``.npmrc`` that changes ``registry=`` does not
receive the user's token, and how git's credential helpers match a stored
credential to the URL's host.

Values for protected names come only from the real environment (shell, CI) or
from a ``FLUID_SECRETS_FILE`` the operator set there; both are trusted.

Names are compared upper-cased, so ``https_proxy`` is ``HTTPS_PROXY``. The
filter is by variable name, never by file name: on a case-insensitive
filesystem ``.Env`` is the ``.env`` the store opens, and it is filtered alike.
Project values are also read literally (``interpolate=False`` in
``dotenv_store``): ``${VAR}`` is never expanded from the operator's
environment, so a project file cannot copy a secret into a loadable name.

Classification. Every ``FLUID_*`` name the package mentions is classified here
and ``tests/test_protected_env_classification.py`` fails on an unclassified
one: it is protected (:data:`PROTECTED_ENV_KEYS`, a protected prefix, or the
name-shape rule below), deliberately loadable (:data:`DOTENV_LOADABLE`), or not
read from the CLI's environment at all (:data:`NOT_PROCESS_ENV`). A new
``FLUID_*`` name that ends in ``_URL``, ``_ENDPOINT``, ``_HOST``, ``_ALLOWLIST``,
``_BLOCKLIST``, ``_ROOT``, ``_DIR(S)``, ``_PATH``, ``_FILE``, ``_DSN``,
``_HOME``, ``_BACKEND``, ``_PROXIES``, ``_MCP``, ``_COMMAND``, ``_ARGS``,
``_MCP_ENV`` or ``_EXECUTABLE``, or contains ``_ALLOW_``,
``_TRUSTED_``, ``_AUTH_MODE``, ``_JWT_`` or ``_INSECURE``, is protected
without being listed, so a forgotten entry fails closed. The completeness test
exists because a name that decides what a project may do must never come from
the project. mise's GHSA-436v-8fw5-4mj8 (CVE-2026-35533) is that bug: a
project ``.mise.toml`` set trust-control settings that mise read before its
trust check (``trusted_config_paths``, or ``yes`` / ``ci``, which auto-approve
the trust prompt), and so made itself trusted.

Prior art: Git honours ``safe.directory`` only from "protected configuration"
(system, global, command line), never from a repository's own config; the
mise advisory above suggests not honouring trust-control settings from
non-global project config files; the GitHub Actions runner refuses
``NODE_OPTIONS`` written to ``$GITHUB_ENV``, comparing names
case-insensitively; sudo's ``initial_badenv_table`` removes loader and
interpreter injection variables (``LD_*``, ``DYLD_*``, ``PYTHONPATH``,
``PERL5OPT``, ``NODE_OPTIONS``, ``GIT_SSH_COMMAND``, ``GIT_CONFIG_*`` ...)
before running a command for someone else.

This module is a stdlib-only leaf: the modules that read these variables keep
their own constants, and the classification test pins that the names agree.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlsplit

from .litellm_endpoints import LITELLM_ENDPOINT_NAMES

__all__ = [
    "CREDENTIAL_NAME_SUFFIXES",
    "DOTENV_LOADABLE",
    "ENDPOINT_CREDENTIAL_GROUPS",
    "ENDPOINT_NAME_SUFFIXES",
    "EndpointGroup",
    "EndpointRefusal",
    "NOT_PROCESS_ENV",
    "PROTECTED_ENV_KEYS",
    "PROTECTED_ENV_PREFIXES",
    "UNLISTED_LLM_ENDPOINTS",
    "is_protected_env_key",
    "unpaired_endpoints",
]

#: Names a project dotenv file may never set. Compared upper-cased, because
#: Windows environment names are case-insensitive.
PROTECTED_ENV_KEYS: FrozenSet[str] = frozenset(
    {
        # ── sandbox and confinement roots ────────────────────────────────
        "FLUID_DUCKDB_ALLOWED_DIRS",  # providers/_duckdb_sandbox.OPERATOR_DIRS_ENV
        "FLUID_REF_ROOT",  # util/ref_confinement.REF_ROOT_ENV
        "FLUID_UPSTREAM_CONTRACTS",  # extra roots the DuckDB sandbox allows
        "FLUID_WORKSPACE_ROOT",
        "FLUID_FORGE_AGENT_CWD",
        "FLUID_LOCAL_DUCKDB_PATH",
        # ── where trusted config is read and state, audit, logs written ──
        "FLUID_SECRETS_FILE",
        # the key for the operator's own ~/.fluid/credentials.enc
        # (credentials/encrypted_store.py prefers it over ~/.fluid/.key)
        "FLUID_ENCRYPTION_KEY",
        "FLUID_ENCRYPTION_PASSPHRASE",
        "FLUID_USER_HOME",
        "FLUID_HOME",
        "FLUID_CONFIG",
        "FLUID_CONFIG_PATH",
        "FLUID_CACHE_DIR",
        "FLUID_LOG_FILE",
        "FLUID_DIAG_OUT_DIR",
        "FLUID_PRICES_JSON",
        "FLUID_STATE_BACKEND",
        "FLUID_STORE_BACKEND",
        "FLUID_STORE_DSN",
        "FLUID_STORE_PATH",
        "FLUID_STORE_ROOT",
        "FLUID_STORE_VECTOR_BACKING",
        "FLUID_AUDIT_ROOT",
        "FLUID_AUDIT_MAX_AGE_DAYS",
        "FLUID_AUDIT_MAX_TOTAL_MB",
        "FLUID_ATHENA_OUTPUT_LOCATION",
        # the home and per-user config directories: ``~`` (including the
        # operator's ``FLUID_SECRETS_FILE=~/...``) and the config, credentials
        # and hooks that git and the cloud SDKs read from there. The shell
        # normally sets HOME, which a dotenv never overrides, but a stripped
        # CI or service environment may not.
        "HOME",
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
        # the Windows equivalents, which Windows readers consult instead:
        # the system config (config_manager reads %ProgramData%\fluid), the
        # keyring's keyringrc.cfg (%LOCALAPPDATA%, else %ProgramData%; its
        # ``keyring-path`` is put on sys.path and its backend imported),
        # Python's user site-packages (%APPDATA%\Python, whose .pth files run
        # in every interpreter the CLI starts), and Git for Windows'
        # %ProgramData%\Git\config
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "ALLUSERSPROFILE",
        # the temporary directory (tempfile reads TMPDIR, TEMP and TMP on
        # every platform): the coding agents run there and Claude Code loads
        # the .claude/settings.json hooks it finds, and the CLI writes its
        # scratch files there
        "TMPDIR",
        "TEMP",
        "TMP",
        # the coding-agent CLIs the forge runs with this environment read
        # their settings from here: the endpoint, the key's env name, and
        # Claude Code's ``apiKeyHelper`` program
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        # ── authentication and trust modes ───────────────────────────────
        "FLUID_SAFE_MODE",
        "FLUID_AI_READY",
        "FLUID_ALLOW_METADATA_SERVICE",
        "FLUID_ALLOW_PLAINTEXT_AI_SECRETS",
        "FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS",
        "FLUID_OPENLINEAGE_ALLOW_PRIVATE",
        "FLUID_DBT_FORWARD_ENV",
        "FLUID_SECRETS_INMEMORY",
        "FLUID_PROMPT_OVERLAYS",
        "FLUID_PLUGIN_STRICT_COMPAT",
        "FLUID_PLUGINS_ALLOWLIST",
        "FLUID_PLUGINS_BLOCKLIST",
        "FLUID_PROVIDERS",
        "FLUID_TELEMETRY",
        "FLUID_MCP_AUTH_MODE",
        "FLUID_MCP_TRUSTED_PROXIES",
        "FLUID_MCP_RATE_LIMIT",
        "FLUID_MCP_RATE_WINDOW_SECONDS",
        "FLUID_MCP_MAX_CONCURRENCY",
        "FLUID_COMMAND_CENTER_HOST_ALLOWLIST",
        "FLUID_FEDERATION_HOST_ALLOWLIST",
        "FLUID_FEDERATION_SECRET_REF_ALLOWLIST",
        "FLUID_WEBHOOK_HOST_ALLOWLIST",
        "DMM_ALLOW_INSECURE_HTTP",
        "DMM_AUTO_APPROVE_ACCESS",
        "CUSTOM_CATALOG_AUTH_TYPE",
        "PGSSLMODE",
        "POSTGRES_SSLMODE",
        # the CA and revocation lists libpq's ``sslmode=verify-*`` trusts
        "PGSSLROOTCERT",
        "PGSSLCRL",
        "PGSSLCRLDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "AWS_CA_BUNDLE",
        "PYTHONHTTPSVERIFY",
        # Python's ssl module and httpx write TLS session keys to this file
        "SSLKEYLOGFILE",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "NODE_EXTRA_CA_CERTS",
        "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
        # ── where data and credentials are sent ──────────────────────────
        "FLUID_COMMAND_CENTER_URL",
        "FLUID_COMMAND_CENTER_ENABLED",
        "FLUID_API_URL",
        "FLUID_CC_ENDPOINT",
        "FLUID_CATALOG_FLUID_CC_URL",
        "FLUID_CATALOG_DMM_URL",
        "FLUID_CATALOG_DATAHUB_URL",
        "FLUID_CATALOG_DATAHUB_SPEC_BASE_URL",
        "FLUID_CATALOG_OPENMETADATA_URL",
        "FLUID_OPENLINEAGE_URL",
        "FLUID_OPENLINEAGE_ENDPOINT",
        "FLUID_MCP_AUDIT_WEBHOOK_URL",
        "FLUID_RUNNER_HOST_OVERRIDE",
        "FLUID_IMPORT_AIRBYTE_URL",
        "FLUID_PUBLIC_REGISTRY",
        "FLUID_WEB_SEARCH_PROVIDER",
        "FLUID_AGENT_WEB_TOOLS",
        "FLUID_FORGE_DB_TOOLS",
        "FLUID_GITHUB_MCP",
        "FLUID_SNOWFLAKE_MCP",
        "FLUID_DBT_MCP",
        "FLUID_LLM_ENDPOINT",
        "FLUID_LLM_ROUTING_ENDPOINT",
        "FLUID_LLM_PROVIDER",
        "FLUID_LLM_BACKEND",
        "FLUID_LLM_MODEL",
        "FLUID_LLM_ROUTING_MODEL",
        "FLUID_LLM_FALLBACK_CHAIN",
        "FLUID_LITELLM_MODEL_PREFIX",
        "FLUID_FORGE_AGENT",
        "DMM_API_URL",
        "DATAMESH_MANAGER_SERVER",
        "DATAHUB_GMS_HOST",
        "DATAHUB_GMS_URL",
        "DATAHUB_SERVER",
        "DATAHUB_SERVER_URL",
        "ALATION_BASE_URL",
        "ATLAS_BASE_URL",
        "COLLIBRA_BASE_URL",
        "CUSTOM_CATALOG_URL",
        "OPENMETADATA_SERVER_URL",
        "OPENMETADATA_HOST",
        "AZURE_PURVIEW_ACCOUNT",
        "CONFLUENT_SCHEMA_REGISTRY_URL",
        "OPENLINEAGE_URL",
        "OPENLINEAGE_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "AWS_ENDPOINT_URL",
        "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS",
        "OLLAMA_HOST",
        # litellm's ollama/ route reads this one, and sends it the key FLUID
        # resolved (FLUID_LLM_API_KEY or the keyring) as a bearer token
        "OLLAMA_API_BASE",
        "VAULT_ADDR",
        # litellm routes every model call to this proxy when the switch is
        # on, and sends it the key FLUID resolved for whichever provider
        # (``api_key`` wins over LITELLM_PROXY_API_KEY), so no project file
        # can make that pairing self-contained
        "USE_LITELLM_PROXY",
        "LITELLM_PROXY_API_BASE",
        # litellm's github_copilot/ route sends the Copilot key it stored or
        # mints, never the key FLUID passes, so no project file can pair any
        # of these. It sends that key to GITHUB_COPILOT_API_BASE when neither
        # an api_base argument nor its stored key file names an endpoint, as
        # on a first call: it picks the host before it reads or mints the key
        # (llms/github_copilot/chat/transformation.py, litellm 1.83.14 to
        # 1.105.0). It fetches the key from the next URL with the GitHub login
        # it stored, its login flow sends the operator to the two after, and
        # it keeps that login and the key at the last three
        # (llms/github_copilot/authenticator.py)
        "GITHUB_COPILOT_API_BASE",
        "GITHUB_COPILOT_API_KEY_URL",
        "GITHUB_COPILOT_ACCESS_TOKEN_URL",
        "GITHUB_COPILOT_DEVICE_CODE_URL",
        "GITHUB_COPILOT_TOKEN_DIR",
        "GITHUB_COPILOT_ACCESS_TOKEN_FILE",
        "GITHUB_COPILOT_API_KEY_FILE",
        # where the Snowflake provider posts the OAuth client secret
        "SNOWFLAKE_OAUTH_TOKEN_REQUEST_URL",
        # python-bigquery sends queries, rows and the bearer token here
        "BIGQUERY_EMULATOR_HOST",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        # botocore's container and instance-metadata providers fetch the
        # operator's credentials from these locations (sending the container
        # authorization token there), and its web-identity provider exchanges
        # the operator's token for the role named here
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_ROLE_ARN",
        "AWS_ROLE_SESSION_NAME",
        # libpq service definitions can carry a host, a password and a
        # passfile: a project may neither pick one of the operator's
        # (PGSERVICE) nor supply its own (PGSERVICEFILE, PGSYSCONFDIR)
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGSYSCONFDIR",
        # libpq connects to this address whatever host it was given, and keeps
        # using that host for the rest: the password the caller passed for it
        # and the operator's ~/.pgpass entry for it go to the address
        # (https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNECT-HOSTADDR,
        # https://www.postgresql.org/docs/current/libpq-pgpass.html). It reads
        # the variable wherever the connection string leaves hostaddr out,
        # which nearly every caller does: dlt's postgres, redshift, sqlalchemy
        # and ducklake destinations, dbt-postgres, psycopg itself. So no
        # pairing rule can make it safe, and only the operator may set it.
        "PGHOSTADDR",
        # ── what program or library runs (after sudo's badenv table) ─────
        "FLUID_DBT_MCP_COMMAND",
        "FLUID_DBT_MCP_ARGS",
        "FLUID_DBT_MCP_ENV",
        "FLUID_OPENTOFU_VERSION",
        "FLUID_EXTRA_PIP_SPECS",
        "DBT_EXECUTABLE",
        "DBT_DOCKER_IMAGE",
        "DBT_BOOTSTRAP_IMAGE",
        "DBT_ADAPTER_PACKAGE",
        "TF_CLI_CONFIG_FILE",
        # SDK config files that can name a credential helper program
        # (AWS ``credential_process``, which botocore also reads from the
        # shared credentials file; kubeconfig ``exec``; Docker ``credsStore``)
        # or an endpoint, and google-auth's switch for executable-sourced
        # credentials
        "AWS_CONFIG_FILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "KUBECONFIG",
        "CLOUDSDK_CONFIG",
        "DOCKER_CONFIG",
        "DOCKER_HOST",
        "GOOGLE_EXTERNAL_ACCOUNT_ALLOW_EXECUTABLES",
        "PATH",
        # Windows: the shell ``subprocess`` runs for shell=True (%ComSpec%,
        # else %SystemRoot%\System32\cmd.exe); %windir% is the same directory
        "COMSPEC",
        "SYSTEMROOT",
        "WINDIR",
        # python-dotenv's off switch: once the CLI has read the project's
        # dotenv files through this filter it sets PYTHON_DOTENV_DISABLED, so
        # dbt (which loads the nearest .env at start-up) and litellm (which
        # loads one at import) do not read them again unfiltered. LITELLM_MODE
        # decides whether litellm does.
        "PYTHON_DOTENV_DISABLED",
        "LITELLM_MODE",
        "IFS",
        "CDPATH",
        "ENV",
        "BASH_ENV",
        "BASHOPTS",
        "SHELLOPTS",
        "PS4",
        "GLOBIGNORE",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "PERLLIB",
        "PERL5LIB",
        "PERL5OPT",
        "PERL5DB",
        "RUBYLIB",
        "RUBYOPT",
        "NODE_OPTIONS",
        "NODE_PATH",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "CLASSPATH",
        "GCONV_PATH",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_EXEC_PATH",
        # git (git(1) "ENVIRONMENT VARIABLES"; the CLI runs git in the project
        # directory with its own environment): the repository, config and
        # templates whose hooks and ``core.*`` commands git runs, the transport
        # protocols it may use (``ext::`` runs a command), and the programs it
        # starts. GIT_SSL_*, GIT_PROXY_*, GIT_TRACE* and GIT_CURL_* are
        # prefixes below. GIT_TERMINAL_PROMPT, GIT_SSH_VARIANT, identity and
        # timeout settings stay loadable: they only narrow or label.
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_TEMPLATE_DIR",
        "GIT_CONFIG",
        "GIT_ALLOW_PROTOCOL",
        "GIT_EXTERNAL_DIFF",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_PAGER",
        "PAGER",
        "EDITOR",
        "VISUAL",
    }
)

#: Every name starting with one of these is protected.
PROTECTED_ENV_PREFIXES: Tuple[str, ...] = (
    "FLUID_OVERLAY_",
    "FLUID_MCP_JWT_",
    "FLUID_LLM_DEFAULT_MODEL_",
    # credentials the Debezium runner itself sets: it writes each credential
    # property of Debezium Server's application.properties as
    # ``${FLUID_DBZ_<PROPERTY>}`` and passes the values in the server's
    # environment (build_runners/debezium/runner.py), so a project file has
    # nothing to supply here
    "FLUID_DBZ_",
    "AWS_ENDPOINT_URL_",
    "GIT_CONFIG_",
    # git's TLS verification, CA and client certificate (http.ssl*)
    "GIT_SSL_",
    # core.gitProxy runs a command; proxy TLS settings (http.proxySSL*)
    "GIT_PROXY_",
    # GIT_TRACE_CURL / _PACKET / GIT_TRACE_REDACT=0 write request headers,
    # and GIT_TRACE2_ENV_VARS chosen variables, to a file the value names
    "GIT_TRACE",
    # GIT_CURL_VERBOSE dumps request headers
    "GIT_CURL_",
    "TF_CLI_ARGS",
    "LD_",
    "DYLD_",
    "_RLD",
)

# Name-shape rule for FLUID_* names: fails closed for a knob nobody listed yet.
_FLUID_PROTECTED_SUFFIXES: Tuple[str, ...] = (
    "_URL",
    "_ENDPOINT",
    "_HOST",
    "_ALLOWLIST",
    "_BLOCKLIST",
    "_ROOT",
    "_DIR",
    "_DIRS",
    "_PATH",
    "_FILE",
    "_DSN",
    "_HOME",
    "_BACKEND",
    "_PROXIES",
    # hosted MCP servers: ``FLUID_<NAME>_MCP`` opts in, ``_MCP_COMMAND`` /
    # ``_MCP_ARGS`` / ``_MCP_ENV`` choose what runs (``cli/hosted_mcp.py``)
    "_MCP",
    "_COMMAND",
    "_ARGS",
    "_MCP_ENV",  # not bare ``_ENV``: FLUID_ENV is the environment's name
    "_EXECUTABLE",
)
_FLUID_PROTECTED_FRAGMENTS: Tuple[str, ...] = (
    "_ALLOW_",
    "_TRUSTED_",
    "_AUTH_MODE",
    "_JWT_",
    "_INSECURE",
)

#: FLUID_* names (and name prefixes ending in ``_``) a project dotenv file may
#: set: credentials, warehouse targets the contract can name anyway, budgets
#: whose default is "no limit" (a project file can only add one), and UX,
#: feature and tuning switches.
DOTENV_LOADABLE: FrozenSet[str] = frozenset(
    {
        # credentials
        "FLUID_API_KEY",
        "FLUID_BEARER_TOKEN",
        "FLUID_CATALOG_DATAHUB_TOKEN",
        "FLUID_CATALOG_DMM_TOKEN",
        "FLUID_CATALOG_FLUID_CC_TOKEN",
        "FLUID_CATALOG_OPENMETADATA_TOKEN",
        "FLUID_COMMAND_CENTER_API_KEY",
        "FLUID_FORGE_DB_URI",
        "FLUID_FORGE_DB_URI_",
        "FLUID_LLM_API_KEY",
        "FLUID_MCP_AUDIT_WEBHOOK_HEADER_AUTH",
        "FLUID_MCP_AUTH_TOKEN",
        "FLUID_OPENLINEAGE_API_KEY",
        "FLUID_PII_ENCRYPTION_SECRET_KEY",
        "FLUID_PII_HASH_SECRET",
        "FLUID_PII_TOKENIZATION_KEY",
        # targets the contract or the operator's credentials already decide
        # (FLUID_SOURCE_*: env_var() defaults in generated dbt sources.yml)
        "FLUID_SOURCE_DATABASE",
        "FLUID_SOURCE_SCHEMA",
        "FLUID_ATHENA_WORKGROUP",
        "FLUID_CC_ORG_ID",
        "FLUID_DEFAULT_REGION",
        "FLUID_ENV",
        "FLUID_GCP_PROJECT",
        "FLUID_PROJECT",
        "FLUID_REGION",
        # budgets, timeouts, retries, limits (defaults are unlimited or loose)
        "FLUID_AGENT_COMPACT_AFTER",
        "FLUID_ATHENA_TIMEOUT_SECONDS",
        "FLUID_CATALOG_DATAHUB_MAX_RETRIES",
        "FLUID_COMMAND_CENTER_TIMEOUT",
        "FLUID_COST_LIMIT_USD",
        "FLUID_COST_LIMIT_USD_PER_PRODUCT",
        "FLUID_COST_LIMIT_USD_PER_RUN",
        "FLUID_FEDERATION_TIMEOUT_SECONDS",
        "FLUID_FORGE_AGENT_TIMEOUT_SECONDS",
        "FLUID_HOSTED_MCP_TIMEOUT_SECONDS",
        "FLUID_LLM_TIMEOUT_SECONDS",
        "FLUID_MARKET_CACHE_TTL",
        "FLUID_MARKET_DEFAULT_LIMIT",
        "FLUID_MARKET_MIN_QUALITY",
        "FLUID_MARKET_TIMEOUT",
        "FLUID_MAX_FILE_SIZE_MB",
        "FLUID_MAX_RETRIES",
        "FLUID_MCP_AUDIT_WEBHOOK_TIMEOUT_SECONDS",
        "FLUID_MCP_CIRCUIT_COOLDOWN_SECONDS",
        "FLUID_MCP_CIRCUIT_THRESHOLD",
        "FLUID_MCP_CIRCUIT_WINDOW_SECONDS",
        "FLUID_MISSION_TIMEOUT_SECONDS",
        "FLUID_OPENLINEAGE_TIMEOUT_SECONDS",
        "FLUID_PARALLEL_OPERATIONS",
        "FLUID_RATE_LIMIT",
        "FLUID_ROLLBACK_KEEP_LAST_N",
        "FLUID_STAGE_BUDGET_",
        "FLUID_TIMEOUT",
        "FLUID_TIMEOUT_SECONDS",
        "FLUID_TOFU_TIMEOUT_SECONDS",
        # output, logging verbosity, UX
        "FLUID_AUTO_CONFIRM",
        "FLUID_BANNER",
        "FLUID_BANNER_TODAY",
        "FLUID_DEBUG",
        "FLUID_DRY_RUN",
        "FLUID_FORGE_NO_",
        "FLUID_FORGE_NO_ARROW_KEYS",
        "FLUID_FORGE_NO_PICKER",
        "FLUID_FORGE_NO_PREVIEW",
        "FLUID_FORGE_NO_PRUNE_HINT",
        "FLUID_FORGE_NO_STREAMING_PREVIEW",
        "FLUID_FORGE_NO_TEAM_MEMORY_SCAFFOLD",
        "FLUID_FORGE_NO_WELCOME",
        "FLUID_FORGE_PICKER_ALWAYS",
        "FLUID_FORGE_WATCH_DEBOUNCE",
        "FLUID_FORGE_WATCH_INTERVAL",
        "FLUID_LOG_FORMAT",
        "FLUID_LOG_LEVEL",
        "FLUID_NONINTERACTIVE",
        "FLUID_NO_ARROW_KEYS",
        "FLUID_NO_TUI",
        "FLUID_OUTPUT_FORMAT",
        "FLUID_PROFILING",
        "FLUID_QUIET",
        "FLUID_RICH_OUTPUT",
        "FLUID_SHOW_REGISTRATION",
        "FLUID_TRACE",
        "FLUID_VERBOSE",
        # feature and behaviour switches that neither widen nor redirect
        "FLUID_BUILD_PROFILE",
        "FLUID_CACHE_ENABLED",
        "FLUID_CACHE_TTL_SECONDS",
        "FLUID_COMPACTION_STRATEGY",
        "FLUID_COPILOT_AGENT_LOOP",
        "FLUID_COPILOT_CHECKPOINT",
        "FLUID_COPILOT_ENRICHMENT",
        "FLUID_COPILOT_EPISODIC_MEMORY",
        "FLUID_COPILOT_FORCE_INTERVIEW",
        "FLUID_COPILOT_JUDGE",
        "FLUID_COPILOT_PARALLEL_PHYSICAL",
        "FLUID_COPILOT_PII_CLASSIFIER",
        "FLUID_COPILOT_POST_SYNTHESIS",
        "FLUID_COPILOT_SELF_EVAL",
        "FLUID_COPILOT_SEMANTIC_MEMORY",
        "FLUID_DBT_TESTS_KEY",
        "FLUID_DISABLE_CC_DETECTION",
        "FLUID_DISCOVERY_CACHE",
        "FLUID_EXPORT_RESOLVE_ENV",
        "FLUID_FORGE_AGENT_MODE",
        "FLUID_FORGE_AUTO_CI",
        "FLUID_FORGE_AUTO_RESUME",
        "FLUID_FORGE_DRIFT_GUARD",
        "FLUID_FORGE_LEGACY_COPILOT",
        "FLUID_FORGE_OFFLINE",
        "FLUID_FORGE_STAGED_COPILOT",
        "FLUID_FORGE_STAGED_TOOL_LOOP",
        "FLUID_FORGE_TOOL_SEARCH",
        "FLUID_FORGE_TOOL_SEARCH_CORE",
        "FLUID_GEMINI_RESPONSE_SCHEMA",
        "FLUID_INTERVIEW_LEGACY",
        "FLUID_JUDGE_SELF_CRITIQUE",
        "FLUID_LLM_MODEL_PREFLIGHT",
        "FLUID_LLM_STREAMING",
        "FLUID_LLM_STRUCTURED_OUTPUTS",
        "FLUID_MARKETPLACE_FALLBACK",
        "FLUID_OPENAI_STRICT_SCHEMA",
        "FLUID_PROMPT_PROFILE",
        "FLUID_PROVIDER",
        "FLUID_RUN_ID",
        "FLUID_TIERED",
        "FLUID_TOKEN_COUNTER",
    }
)

#: Names that look like FLUID_* variables but are not read from the CLI's own
#: environment: object tags it writes, variables of generated pipelines, DAGs
#: and Lambdas, a model reply token, and placeholders in prose.
NOT_PROCESS_ENV: FrozenSet[str] = frozenset(
    {
        # Snowflake object tags
        "FLUID_CONTRACT_ID",
        "FLUID_CONTRACT_NAME",
        "FLUID_DOMAIN",
        "FLUID_LAYER",
        "FLUID_PRODUCT_TYPE",
        "FLUID_TEAM",
        # generated Airflow DAG / CI pipeline / Lambda environment
        "FLUID_DAG_BUILD_ID",
        "FLUID_DAG_CONTRACT",
        "FLUID_DAG_CONTRACT_ENV",
        "FLUID_DAG_ENV",
        "FLUID_ENV_NAME",
        "FLUID_OIDC_TOKEN",
        "FLUID_PACKAGE_SPEC",
        "FLUID_REDSHIFT_DATABASE",
        "FLUID_REDSHIFT_SQL",
        "FLUID_REDSHIFT_WORKGROUP",
        # a model reply token, and placeholders in prose and messages
        "FLUID_OK",
        "FLUID_X",
        "FLUID_STORE_",
        # name prefixes for other things: Iceberg volumes, IAM roles, and the
        # head of the f-strings that build ``FLUID_<NAME>_MCP_*``
        "FLUID_",
        "FLUID_MODEL_",
    }
)


def is_protected_env_key(key: str) -> bool:
    """Whether a project dotenv file is refused the variable ``key``.

    Compared upper-cased: on Windows ``fluid_ref_root`` and ``FLUID_REF_ROOT``
    are the same variable (the GitHub runner compares its blocklist the same
    way).
    """
    name = key.strip().upper()
    if not name:
        return False
    if name in PROTECTED_ENV_KEYS or name.startswith(PROTECTED_ENV_PREFIXES):
        return True
    if name.startswith("FLUID_"):
        return name.endswith(_FLUID_PROTECTED_SUFFIXES) or any(
            fragment in name for fragment in _FLUID_PROTECTED_FRAGMENTS
        )
    return False


# ── third-party endpoints: the pairing rule ─────────────────────────────────


@dataclass(frozen=True)
class EndpointGroup:
    """One service as one set of readers sees it: the variables that choose
    where it connects, the credentials it sends there, and which credentials
    a project may pair an endpoint with.

    ``endpoints`` and ``credentials`` are upper-case names, or patterns with
    ``*`` (matched by :func:`fnmatch.fnmatchcase`). A credential of the group
    that the operator holds (real environment or ``FLUID_SECRETS_FILE``)
    refuses every project endpoint of the group.

    ``ambient`` marks readers that also find the operator's credential
    outside the environment (keyring, a stored CLI login, an SDK config file,
    cloud default credentials). Their endpoints load from a project file only
    when, for every entry of ``paired_by``, the project files supply one of
    its names. Each entry is one code path's credentials that it sends in
    preference to the ambient one, so a project credential a reader consults
    after the keyring, or never, pairs nothing: it is a decoy, and the
    operator's credential is what goes out. ``{section}`` in a name stands for
    the endpoint's own dlt section (``DESTINATION__POSTGRES__CREDENTIALS__``).
    An ambient group with no ``paired_by`` never takes a project endpoint.

    ``switches`` are settings that make a reader send the ambient credential
    whatever the project supplies (Redshift's IAM auth); while one is set, by
    the operator or the project, the group's endpoints pair with nothing.

    ``authenticators`` are the settings that choose how a reader signs in
    (Snowflake's authenticator), and ``project_authenticators`` the values
    (lower case) that sign in with nothing but the credential that pairs the
    endpoint. Other values can sign in with the operator's identity or
    material: a browser session, an identity provider login, an OAuth or
    workload identity, a key. So one the operator sets counts as a
    credential of theirs, whatever its value (the project file's own value
    of it excepted only when it is one of ``project_authenticators``), and a
    project file may give one only one of those values next to an endpoint of
    its own (:func:`refused_project_authenticators`).

    ``blank_reads_project`` are the credentials of ``paired_by`` that the
    readers which also see a project endpoint behind a blank variable read
    from the project's files when the operator's variable of that name is
    blank (the credential resolver reads a blank variable as unset and falls
    back to the project's ``.env``). A project credential of one of these
    names behind the operator's blank variable still pairs an endpoint that
    is itself behind the operator's blank variable: only those readers see
    either. Every other project credential behind a blank variable pairs
    nothing, since an SDK then falls back to the operator's stored login.

    ``connection_strings`` are the endpoints whose value is a whole connection
    string, a URL naming the host and the database. One that carries its own
    password, in the URL's user info or under one of
    ``connection_string_passwords`` in its query, pairs itself: the password
    is the project's own, in the same file as the host.

    ``read_by`` says where the variables are read, so a reviewer can check
    the group against the code.
    """

    service: str
    endpoints: Tuple[str, ...]
    credentials: Tuple[str, ...]
    ambient: bool
    read_by: str
    paired_by: Tuple[Tuple[str, ...], ...] = ()
    switches: Tuple[str, ...] = ()
    authenticators: Tuple[str, ...] = ()
    project_authenticators: Tuple[str, ...] = ()
    blank_reads_project: Tuple[str, ...] = ()
    connection_strings: Tuple[str, ...] = ()
    connection_string_passwords: Tuple[str, ...] = ()


# ``llm/provider_resolve._resolve_api_key`` prefers it for every provider and
# passes the result to litellm as ``api_key``, which beats the provider's own
# key variable.
_LLM_KEY = "FLUID_LLM_API_KEY"
# ``_resolve_api_key`` then reads the provider's PROVIDER_ENV_VARS name (only
# openai, anthropic, gemini and github have one) and falls back to the key
# ``fluid ai setup`` saved in the keyring, which also beats a provider key
# variable that litellm would read.
_LLM_KEYRING = (
    "the key comes from FLUID_LLM_API_KEY, the provider's PROVIDER_ENV_VARS name, then the "
    "keyring (llm/provider_resolve.py)"
)
_GOOGLE_CREDENTIALS: Tuple[str, ...] = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CREDENTIALS",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "GOOGLE_GENAI_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "GCP_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GCP_KEYFILE",
    "BIGQUERY_KEYFILE_PATH",
    "VERTEXAI_CREDENTIALS",
)

#: Services whose endpoint a project dotenv file may set only for a credential
#: it supplies itself. Every name here stays loadable otherwise; endpoints FLUID
#: itself owns (catalogs, Command Center, OpenLineage, Vault, Ollama,
#: ``AWS_ENDPOINT_URL*``, ``BIGQUERY_EMULATOR_HOST`` ...) are protected outright.
ENDPOINT_CREDENTIAL_GROUPS: Tuple[EndpointGroup, ...] = (
    # ── LLM providers (``fluid forge``; litellm when FLUID_LLM_BACKEND=litellm,
    #    and the coding-agent CLIs llm/coding_agent.py starts with os.environ)
    EndpointGroup(
        service="OpenAI",
        endpoints=("OPENAI_BASE_URL", "OPENAI_API_BASE"),
        credentials=("OPENAI_API_KEY", "CODEX_API_KEY", _LLM_KEY),
        ambient=True,
        read_by=(
            "openai SDK OpenAI() (OPENAI_BASE_URL); litellm's openai/ route "
            "(OPENAI_BASE_URL, OPENAI_API_BASE) called by llm/litellm_backend.py; "
            f"{_LLM_KEYRING}. The codex CLI also reads OPENAI_BASE_URL but sends its "
            "stored login unless CODEX_API_KEY is set, so llm/coding_agent.py drops a "
            "project OPENAI_BASE_URL from the codex environment unless the project "
            "supplies CODEX_API_KEY too"
        ),
        paired_by=(("OPENAI_API_KEY", _LLM_KEY),),
    ),
    EndpointGroup(
        service="Anthropic",
        endpoints=("ANTHROPIC_BASE_URL", "ANTHROPIC_API_BASE"),
        credentials=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_API_KEY",
            _LLM_KEY,
        ),
        ambient=True,
        read_by=(
            "litellm's anthropic/ route (ANTHROPIC_API_BASE, ANTHROPIC_BASE_URL); "
            f"{_LLM_KEYRING}. The claude CLI (llm/coding_agent.py) sends "
            "ANTHROPIC_AUTH_TOKEN, else ANTHROPIC_API_KEY, else its apiKeyHelper or "
            "stored subscription login to ANTHROPIC_BASE_URL; only ANTHROPIC_API_KEY "
            "comes before the ambient credential on both paths"
        ),
        paired_by=(("ANTHROPIC_API_KEY", _LLM_KEY), ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")),
    ),
    EndpointGroup(
        service="Azure OpenAI",
        endpoints=("AZURE_OPENAI_ENDPOINT", "AZURE_API_BASE", "AZURE_AI_API_BASE"),
        credentials=(
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_AD_TOKEN",
            "AZURE_API_KEY",
            "AZURE_AD_TOKEN",
            "AZURE_AI_API_KEY",
            _LLM_KEY,
        ),
        ambient=True,
        read_by=(
            "openai SDK AzureOpenAI() (AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY); "
            "litellm's azure/ route (AZURE_API_BASE; AZURE_OPENAI_API_KEY, "
            f"AZURE_API_KEY, which the key FLUID resolves beats); {_LLM_KEYRING}"
        ),
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="Google Gemini",
        endpoints=("GEMINI_API_BASE", "GOOGLE_GEMINI_BASE_URL"),
        credentials=("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_API_KEY", _LLM_KEY),
        ambient=True,
        read_by=(
            "litellm's gemini/ route (GEMINI_API_BASE; GOOGLE_API_KEY, "
            "GEMINI_API_KEY); google-genai (GOOGLE_GEMINI_BASE_URL); "
            f"{_LLM_KEYRING}, and for gemini also GEMINI_API_KEY and GOOGLE_API_KEY"
        ),
        paired_by=((_LLM_KEY, "GOOGLE_API_KEY", "GEMINI_API_KEY"),),
    ),
    EndpointGroup(
        service="Groq",
        endpoints=("GROQ_API_BASE",),
        credentials=("GROQ_API_KEY", _LLM_KEY),
        ambient=True,
        read_by=f"litellm's groq/ route (GROQ_API_BASE); {_LLM_KEYRING}",
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="Mistral",
        endpoints=("MISTRAL_API_BASE", "MISTRAL_AZURE_API_BASE"),
        credentials=("MISTRAL_API_KEY", "MISTRAL_AZURE_API_KEY", _LLM_KEY),
        ambient=True,
        read_by=f"litellm's mistral/ route (MISTRAL_API_BASE); {_LLM_KEYRING}",
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="Cohere",
        endpoints=("COHERE_API_BASE", "CO_API_URL"),
        credentials=("COHERE_API_KEY", "CO_API_KEY", _LLM_KEY),
        ambient=True,
        read_by=(
            "litellm's cohere/ route (COHERE_API_BASE); the cohere SDK "
            f"(CO_API_URL, CO_API_KEY); {_LLM_KEYRING}"
        ),
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="GitHub Models",
        endpoints=("GITHUB_API_BASE",),
        credentials=("GITHUB_API_KEY", "GITHUB_TOKEN", _LLM_KEY),
        ambient=True,
        read_by=(
            "litellm's github/ route (GITHUB_API_BASE; the key is GITHUB_API_KEY, "
            f"llm/providers.py PROVIDER_ENV_VARS); {_LLM_KEYRING}"
        ),
        paired_by=((_LLM_KEY, "GITHUB_API_KEY"),),
    ),
    EndpointGroup(
        service="Amazon Bedrock",
        endpoints=(
            "AWS_BEDROCK_RUNTIME_ENDPOINT",
            "BEDROCK_API_BASE",
            "ANTHROPIC_BEDROCK_BASE_URL",
        ),
        credentials=(
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            _LLM_KEY,
        ),
        ambient=True,
        read_by=(
            "litellm's bedrock/ route (AWS_BEDROCK_RUNTIME_ENDPOINT, BEDROCK_API_BASE; "
            "the api_key FLUID resolves, else AWS_BEARER_TOKEN_BEDROCK, is sent as a "
            f"bearer token, a session token in a header); {_LLM_KEYRING}; the claude "
            "CLI with CLAUDE_CODE_USE_BEDROCK (ANTHROPIC_BEDROCK_BASE_URL). "
            "AWS_ENDPOINT_URL* is protected outright"
        ),
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="Vertex AI",
        endpoints=("VERTEXAI_API_BASE", "VERTEX_API_BASE"),
        credentials=_GOOGLE_CREDENTIALS + (_LLM_KEY,),
        ambient=True,
        read_by=(
            "litellm's vertex_ai/ route (VERTEXAI_API_BASE, VERTEX_API_BASE): the "
            f"api_key FLUID resolves ({_LLM_KEYRING}), else VERTEXAI_CREDENTIALS, else "
            "application default credentials"
        ),
        paired_by=((_LLM_KEY,), ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEXAI_CREDENTIALS")),
    ),
    EndpointGroup(
        service="Google Cloud",
        endpoints=(
            "GOOGLE_VERTEX_BASE_URL",
            "ANTHROPIC_VERTEX_BASE_URL",
            "GOOGLE_*_CUSTOM_ENDPOINT",
            # every google-cloud client builds its host from this domain
            "GOOGLE_CLOUD_UNIVERSE_DOMAIN",
            # where google-auth asks for the access token on GCE / GKE / Cloud Run
            "GCE_METADATA_HOST",
            "GCE_METADATA_IP",
            "GCE_METADATA_ROOT",
        ),
        credentials=_GOOGLE_CREDENTIALS,
        ambient=True,
        read_by=(
            "google-genai (GOOGLE_VERTEX_BASE_URL); the claude CLI with "
            "CLAUDE_CODE_USE_VERTEX (ANTHROPIC_VERTEX_BASE_URL); the OpenTofu "
            "google provider iac/providers/gcp.py runs "
            "(GOOGLE_<SERVICE>_CUSTOM_ENDPOINT); google-auth and the google-cloud "
            "clients (GCE_METADATA_*, GOOGLE_CLOUD_UNIVERSE_DOMAIN). Each sends an "
            "access token minted from application default credentials, which also "
            "live outside the environment (gcloud login, the metadata server); "
            "GOOGLE_APPLICATION_CREDENTIALS comes first on every path"
        ),
        paired_by=(("GOOGLE_APPLICATION_CREDENTIALS",),),
    ),
    EndpointGroup(
        service="Google Cloud CLI",
        endpoints=("CLOUDSDK_API_ENDPOINT_OVERRIDES_*",),
        credentials=_GOOGLE_CREDENTIALS,
        ambient=True,
        read_by=(
            "gcloud (CLOUDSDK_API_ENDPOINT_OVERRIDES_<API>), which sends the token of "
            "its own stored login unless CLOUDSDK_AUTH_ACCESS_TOKEN_FILE or "
            "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE names another"
        ),
        paired_by=(("CLOUDSDK_AUTH_ACCESS_TOKEN_FILE", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"),),
    ),
    EndpointGroup(
        service="Hugging Face Hub",
        endpoints=("HF_ENDPOINT", "HF_INFERENCE_ENDPOINT"),
        credentials=("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_API_KEY", "HF_API_KEY"),
        ambient=True,
        read_by=(
            "huggingface_hub (constants.ENDPOINT from HF_ENDPOINT), which sends HF_TOKEN, "
            "else the token `hf auth login` stored under HF_HOME"
        ),
        paired_by=(("HF_TOKEN",),),
    ),
    # ── warehouses and databases ─────────────────────────────────────────
    EndpointGroup(
        service="PostgreSQL",
        # PGHOSTADDR is not here: it is protected outright (PROTECTED_ENV_KEYS)
        endpoints=(
            "PGHOST",
            "PGPORT",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "PG_HOST",
            "PG_PORT",
        ),
        credentials=("PGPASSWORD", "PGPASSFILE", "PGSSLKEY", "POSTGRES_PASSWORD", "PG_PASSWORD"),
        ambient=False,
        read_by=(
            "build_runners/dbt/profiles.py (the dbt-postgres profile: PGHOST, "
            "PGPORT, PGPASSWORD); build_runners/_credentials.py PostgresCredentials "
            "(POSTGRES_* and PG_*, "
            "passed on by the dlt destination bridge); "
            "output_ports/mcp/drivers/postgres.py (POSTGRES_HOST, POSTGRES_PORT, "
            "POSTGRES_PASSWORD into psycopg.connect); libpq itself (PG* in psycopg "
            "and DuckDB's postgres extension). A server may ask libpq for the "
            "password in clear text. libpq's ~/.pgpass matches the host name, so "
            "a project host receives only an entry stored for it or for '*'; "
            "while the operator's file has a '*' entry, unpaired_endpoints "
            "pairs these endpoints only with the password their readers send"
        ),
    ),
    EndpointGroup(
        service="MySQL",
        endpoints=("MYSQL_HOST", "MYSQL_TCP_PORT", "MYSQL_UNIX_PORT"),
        credentials=("MYSQL_PWD", "MYSQL_PASSWORD"),
        ambient=False,
        read_by=(
            "DuckDB's mysql extension (``fluid forge data-model from-source "
            "--source mysql``, cli/discover/_jdbc_introspect.py) through "
            "libmysqlclient (MYSQL_HOST, MYSQL_TCP_PORT, MYSQL_PWD); "
            "providers/odcs/mappers/servers.py emits ${MYSQL_HOST}"
        ),
    ),
    EndpointGroup(
        service="Snowflake",
        endpoints=(
            "SNOWFLAKE_ACCOUNT",
            "SF_ACCOUNT",
            # credentials/resolver.py also tries SNOWFLAKE__<KEY> and the bare key
            "SNOWFLAKE__ACCOUNT",
            "ACCOUNT",
            "SNOWFLAKE_ACCOUNT_NAME",
            "SNOWFLAKE_ORGANIZATION_NAME",
            "SNOWFLAKE_HOST",
            "SNOWFLAKE_PORT",
            "SNOWFLAKE_PROTOCOL",
            "SNOWFLAKE_PROD_ACCOUNT",
        ),
        credentials=(
            "SNOWFLAKE_PASSWORD",
            "SF_PASSWORD",
            "SNOWFLAKE_PASSCODE",
            "SNOWFLAKE_PRIVATE_KEY",
            "SNOWFLAKE_PRIVATE_KEY_PATH",
            "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE",
            "SNOWFLAKE_TOKEN",
            "SNOWFLAKE_PAT",
            "SNOWFLAKE_OAUTH_TOKEN",
            "SNOWFLAKE_OAUTH_CLIENT_SECRET",
            "SNOWFLAKE_PROD_PASSWORD",
            "SNOWFLAKE__PASSWORD",
            "SNOWFLAKE__PRIVATE_KEY_PATH",
            "SNOWFLAKE__OAUTH_TOKEN",
            "PASSWORD",
            "PRIVATE_KEY_PATH",
            "OAUTH_TOKEN",
        ),
        ambient=True,
        read_by=(
            "providers/snowflake/util/config.py (SNOWFLAKE_ACCOUNT, SF_ACCOUNT "
            "through credentials/resolver.py, whose chain also reads the keyring, "
            "~/.fluid/credentials.enc, ~/.fluidrc.yaml and a secret manager for "
            "each setting on its own, and get_connection_params prefers a key "
            "path, then an OAuth token, then an authenticator, then a password); "
            "build_runners/dbt/profiles.py and engines/dbt/profiles.py (the dbt "
            "profile, from the environment); copilot/catalog/credentials.py "
            "(from the environment, or account and credentials together from a "
            "saved source); the OpenTofu Snowflake provider iac/providers/"
            "snowflake.py runs (SNOWFLAKE_ORGANIZATION_NAME, SNOWFLAKE_ACCOUNT_NAME, "
            "SNOWFLAKE_HOST; it merges ~/.snowflake/config under the environment); "
            "the Snowflake MCP server cli/hosted_mcp.py starts (SNOWFLAKE_PAT; the "
            "connector's connections.toml under SNOWFLAKE_HOME); the dlt Snowflake "
            "destination build_runners/dlt/runner.py (dlt fills unset fields from "
            "~/.dlt/secrets.toml); `fluid auth` cli/_auth_provider_impls.py, which "
            "runs snowsql (~/.snowsql/config) only when the resolver found no "
            "connector credential. While an endpoint from the project files is in "
            "effect (dotenv_store.project_endpoint_in_effect) the readers that "
            "reach outside the environment are pinned to it: the resolver reads "
            "only the environment and the project files, the provider gets an "
            "empty config file, the MCP server an empty SNOWFLAKE_HOME and dlt "
            "credentials built from the project's own values. So every credential "
            "form the resolver reads from there pairs, and `fluid auth` never reaches "
            "snowsql. get_connection_params ranks an authenticator above the password, "
            "and the resolver (SNOWFLAKE_AUTHENTICATOR, SNOWFLAKE__AUTHENTICATOR, "
            "AUTHENTICATOR), resolve_snowflake_settings (SF_AUTHENTICATOR), the dbt "
            "profile, the OpenTofu provider, the MCP server and dlt "
            "(SNOWFLAKE_AUTHENTICATOR in any case through pydantic-settings, and "
            "<SECTION>CREDENTIALS__AUTHENTICATOR under every section path dlt tries) "
            "read it from the environment"
        ),
        paired_by=(
            (
                # the names a refusal warning shows first
                "SNOWFLAKE_PASSWORD",
                "SNOWFLAKE_OAUTH_TOKEN",
                "SNOWFLAKE_PRIVATE_KEY_PATH",
                # the resolver's other spellings of them
                "SF_PASSWORD",
                "SNOWFLAKE__PASSWORD",
                "PASSWORD",
                "SNOWFLAKE__OAUTH_TOKEN",
                "OAUTH_TOKEN",
                "SNOWFLAKE__PRIVATE_KEY_PATH",
                "PRIVATE_KEY_PATH",
            ),
        ),
        authenticators=(
            "SNOWFLAKE_AUTHENTICATOR",
            "SF_AUTHENTICATOR",
            "SNOWFLAKE__AUTHENTICATOR",
            "AUTHENTICATOR",
            # dlt's, under every section path it tries for the destination
            # (DESTINATION__SNOWFLAKE__CREDENTIALS__, DESTINATION__CREDENTIALS__,
            # CREDENTIALS__, each also behind a pipeline name)
            "*CREDENTIALS__AUTHENTICATOR",
        ),
        # the connector's default: the password the project supplies
        project_authenticators=("snowflake",),
        # the names the resolver's .env step reads (SNOWFLAKE_<KEY>, SF_PASSWORD,
        # <KEY>); with the account behind a blank variable too, no other reader
        # sees the project's account (dbt, OpenTofu, the MCP server and the dlt
        # bridge read the environment, the pinned dlt runner the same values)
        blank_reads_project=(
            "SNOWFLAKE_PASSWORD",
            "SF_PASSWORD",
            "PASSWORD",
            "SNOWFLAKE_OAUTH_TOKEN",
            "OAUTH_TOKEN",
            "SNOWFLAKE_PRIVATE_KEY_PATH",
            "PRIVATE_KEY_PATH",
        ),
    ),
    EndpointGroup(
        service="Amazon Redshift",
        endpoints=("REDSHIFT_HOST", "REDSHIFT_PORT"),
        credentials=(
            "REDSHIFT_PASSWORD",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
        ),
        ambient=True,
        read_by=(
            "build_runners/dbt/profiles.py and engines/dbt/profiles.py (the "
            "dbt-redshift profile); build_runners/_credentials.py "
            "RedshiftCredentials. With IAM auth (REDSHIFT_USE_IAM, "
            "REDSHIFT_IAM_PROFILE, AWS_PROFILE) the driver gets a temporary "
            "database password with the operator's AWS credentials and sends it "
            "to the host, whatever REDSHIFT_PASSWORD says"
        ),
        paired_by=(("REDSHIFT_PASSWORD",),),
        switches=("REDSHIFT_USE_IAM", "REDSHIFT_IAM_PROFILE", "AWS_PROFILE"),
    ),
    EndpointGroup(
        service="Databricks",
        endpoints=(
            "DATABRICKS_HOST",
            "DATABRICKS_SERVER_HOSTNAME",
            # databricks-sdk reads host and token from this file's profile
            "DATABRICKS_CONFIG_FILE",
        ),
        credentials=(
            "DATABRICKS_TOKEN",
            "DATABRICKS_CLIENT_SECRET",
            "DATABRICKS_API_KEY",
            "DATABRICKS_PASSWORD",
        ),
        ambient=True,
        read_by=(
            "build_runners/dbt/profiles.py (the dbt-databricks profile); "
            "copilot/catalog/credentials.py (DATABRICKS_HOST with DATABRICKS_TOKEN "
            "or DATABRICKS_CLIENT_SECRET); cli/_auth_provider_impls.py check_auth "
            "(DATABRICKS_HOST from the environment, the token from DATABRICKS_TOKEN, "
            "else the keyring); databricks-sdk, which also reads ~/.databrickscfg"
        ),
        paired_by=(("DATABRICKS_TOKEN",),),
    ),
    EndpointGroup(
        service="Databricks model serving",
        endpoints=("DATABRICKS_API_BASE",),
        credentials=("DATABRICKS_API_KEY", "DATABRICKS_TOKEN", _LLM_KEY),
        ambient=True,
        read_by=f"litellm's databricks/ route (DATABRICKS_API_BASE); {_LLM_KEYRING}",
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="IBM watsonx",
        endpoints=(
            "WATSONX_API_BASE",
            "WATSONX_URL",
            "WX_URL",
            "WML_URL",
            # where litellm posts the key FLUID passes for an IAM token
            "WATSONX_IAM_URL",
        ),
        # litellm sends WATSONX_TOKEN, else WATSONX_ZENAPIKEY, from the
        # environment ahead of an IAM token for the key FLUID passes, and reads
        # the API key names only when FLUID passes none
        credentials=(
            "WATSONX_APIKEY",
            "WATSONX_API_KEY",
            "WX_API_KEY",
            "WATSONX_ZENAPIKEY",
            "WATSONX_TOKEN",
            _LLM_KEY,
        ),
        ambient=True,
        read_by=(
            "litellm's watsonx/ route (llms/watsonx/common_utils.py): the host from "
            "WATSONX_API_BASE, WATSONX_URL, WX_URL or WML_URL, the IAM token from "
            f"WATSONX_IAM_URL; {_LLM_KEYRING}"
        ),
        paired_by=((_LLM_KEY,),),
    ),
    EndpointGroup(
        service="dlt",
        endpoints=(
            "*CREDENTIALS__HOST",
            "*CREDENTIALS__PORT",
            "*CREDENTIALS__SERVER_HOSTNAME",
            # Snowflake's: the host the connector connects to in place of the
            # account's (dlt's SnowflakeCredentials._snowflake_host)
            "*CREDENTIALS___SNOWFLAKE_HOST",
            # the whole credentials as a connection string, which dlt reads
            # under every section path it tries: DESTINATION__<NAME>__,
            # DESTINATION__ and none, each also behind a pipeline name
            "*__CREDENTIALS",
            "CREDENTIALS",
        ),
        credentials=(
            "*CREDENTIALS__PASSWORD",
            "*CREDENTIALS__PRIVATE_KEY*",
            "*CREDENTIALS__ACCESS_TOKEN",
            "*CREDENTIALS__TOKEN",
            "*CREDENTIALS__CLIENT_SECRET",
            "POSTGRES_PASSWORD",
            "PG_PASSWORD",
            "SNOWFLAKE_PASSWORD",
            "SNOWFLAKE_PRIVATE_KEY",
            "REDSHIFT_PASSWORD",
            "DATABRICKS_TOKEN",
        ),
        ambient=True,
        read_by=(
            "dlt (DESTINATION__<NAME>__CREDENTIALS__<FIELD>, and the shorter "
            "section paths it falls back to, then .dlt/secrets.toml and the global "
            "~/.dlt one); build_runners/dlt/destinations.py copies FLUID's "
            "credentials (POSTGRES_PASSWORD, SNOWFLAKE_PASSWORD ...) into those "
            "names after the project files load, keeping one already set, so only "
            "a password in the endpoint's own section is the one dlt sends. dlt "
            "fills each field the environment leaves unset from its other config "
            "providers, so while a project endpoint reaches a destination "
            "build_runners/dlt/runner.py builds its credentials from the project's "
            "own values only (dotenv_store.project_values_in_effect, the contract); "
            "Snowflake's authenticator is read as above"
        ),
        paired_by=(("{section}PASSWORD",),),
        authenticators=("*CREDENTIALS__AUTHENTICATOR",),
        project_authenticators=("snowflake",),
        # dlt's connection-string credentials take the password from the user
        # info (dlt/common/configuration/specs/connection_string_credentials.py,
        # parse_native_representation), MotherDuck's from the ``token`` or
        # ``motherduck_token`` query parameter
        # (dlt/destinations/impl/motherduck/configuration.py, _token_to_password)
        connection_strings=("*__CREDENTIALS", "CREDENTIALS"),
        connection_string_passwords=("token", "motherduck_token"),
    ),
    # ── identity providers and other SDKs ────────────────────────────────
    EndpointGroup(
        service="Azure identity",
        endpoints=(
            "AZURE_AUTHORITY_HOST",
            "AZURE_POD_IDENTITY_AUTHORITY_HOST",
            "IDENTITY_ENDPOINT",
            "MSI_ENDPOINT",
            "IMDS_ENDPOINT",
        ),
        credentials=(
            "AZURE_CLIENT_SECRET",
            "AZURE_CLIENT_CERTIFICATE_PATH",
            "AZURE_CLIENT_CERTIFICATE_PASSWORD",
            "AZURE_PASSWORD",
            "AZURE_FEDERATED_TOKEN_FILE",
            "IDENTITY_HEADER",
            "MSI_SECRET",
        ),
        ambient=True,
        read_by=(
            "azure-identity (DefaultAzureCredential in secrets.py, "
            "ClientSecretCredential in cli/_auth_provider_azure.py): the client "
            "secret is posted to AZURE_AUTHORITY_HOST, the managed-identity header "
            "sent to IDENTITY_ENDPOINT / MSI_ENDPOINT, and SharedTokenCacheCredential "
            "sends the refresh token it finds in the operator's token cache to "
            "AZURE_AUTHORITY_HOST. EnvironmentCredential comes first only when "
            "AZURE_TENANT_ID, AZURE_CLIENT_ID and a secret or certificate are all set"
        ),
        paired_by=(
            ("AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH"),
            ("AZURE_CLIENT_ID",),
            ("AZURE_TENANT_ID",),
        ),
    ),
    EndpointGroup(
        service="GitHub",
        endpoints=("GITHUB_HOST", "GH_HOST", "GITHUB_API_URL"),
        credentials=(
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
        ),
        ambient=False,
        read_by=(
            "the GitHub MCP server cli/hosted_mcp.py starts with this environment "
            "(GITHUB_HOST, GITHUB_PERSONAL_ACCESS_TOKEN); gh's GH_HOST / GH_TOKEN "
            "(gh keys its stored logins by host)"
        ),
    ),
)

#: Endpoints no group lists that litellm reads all the same: ``<PROVIDER>_API_BASE``
#: for its providers (DEEPSEEK_API_BASE, XAI_API_BASE, OPENROUTER_API_BASE,
#: LM_STUDIO_API_BASE, HOSTED_VLLM_API_BASE ...), the names its registry of
#: OpenAI-compatible providers gives, and the other hosts, URLs, endpoints and
#: account ids its provider code chooses a host from (DATAROBOT_ENDPOINT,
#: SNOWFLAKE_ACCOUNT_ID, GIGACHAT_AUTH_URL ...); FLUID routes any provider name
#: litellm knows (``llm/providers.py`` falls back to litellm's catch-all),
#: handing it the key ``_resolve_api_key`` found. Only the names litellm reads
#: (``litellm_endpoints.py``): any other ``<NAME>_API_BASE`` is the project's own
#: and follows the name-shape rule.
UNLISTED_LLM_ENDPOINTS = EndpointGroup(
    service="LLM provider",
    endpoints=tuple(sorted(LITELLM_ENDPOINT_NAMES)),
    credentials=(_LLM_KEY,),
    ambient=True,
    read_by=(
        "litellm's <provider>/ routes (<PROVIDER>_API_BASE, and the hosts, URLs, "
        "endpoints and account ids credentials/litellm_endpoints.py lists); "
        f"{_LLM_KEYRING}"
    ),
    paired_by=((_LLM_KEY,),),
)
_UNLISTED_LLM_NAMES = frozenset(UNLISTED_LLM_ENDPOINTS.endpoints)

#: Name-shape rule for endpoints no group lists, longest suffix first: the part
#: before the suffix is the service prefix (``ACME_API_BASE`` -> ``ACME``).
ENDPOINT_NAME_SUFFIXES: Tuple[str, ...] = (
    "_ENDPOINT_URL",
    "_API_BASE",
    "_BASE_URL",
    "_API_URL",
    "_HOSTNAME",
    "_ENDPOINT",
    "_SERVER",
    "_HOST",
    "_ADDR",
    "_URL",
    "_URI",
)

#: A real-environment variable ``<PREFIX>_...`` ending in one of these counts as
#: a credential of the endpoint with that prefix (``ACME_API_KEY``,
#: ``ACME_CLIENT_SECRET``, ``ACME_DB_PASSWORD``).
CREDENTIAL_NAME_SUFFIXES: Tuple[str, ...] = (
    "_KEY",
    "_TOKEN",
    "_PASSWORD",
    "_PASSWD",
    "_PWD",
    "_PASS",
    "_SECRET",
    "_PASSPHRASE",
    "_CREDENTIALS",
    "_PAT",
)


@dataclass(frozen=True)
class EndpointRefusal:
    """Why a project dotenv file may not set the endpoint ``name``.

    ``operator_credentials`` are the real-environment credentials the SDK would
    send to the project's host. When it is empty, ``services`` find the
    operator's credential outside the environment and the project files supply
    none that those readers send in its place; ``pairs_with`` names what would
    (one per code path), empty when nothing a project file sets can.
    ``pairing`` is each of those code paths whole: any one of its names pairs
    that path. ``switches`` are the settings that are set (by the operator or
    the project) and make a reader send the ambient credential whatever the
    project supplies. ``pairable`` is whether adding a credential to the
    project files would load the endpoint: no variable of the operator's holds
    one of the service's credentials, no switch is set, and something pairs it.
    ``pairs_inside`` is whether the endpoint is a connection string that a
    password of its own, inside it, would also pair
    (:attr:`EndpointGroup.connection_strings`).
    """

    name: str
    services: Tuple[str, ...]
    operator_credentials: Tuple[str, ...]
    pairs_with: Tuple[str, ...] = ()
    pairing: Tuple[Tuple[str, ...], ...] = ()
    switches: Tuple[str, ...] = ()
    pairable: bool = False
    pairs_inside: bool = False


#: While the operator's libpq password file has an entry for any host
#: (``*``), a project PostgreSQL endpoint pairs only with the password its
#: readers send ahead of that file: libpq reads PGPASSWORD; PostgresCredentials
#: and the output-port driver read POSTGRES_PASSWORD (PostgresCredentials also
#: takes PG_PASSWORD for the PG_* names).
_PGPASS_PAIRED_BY: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], ...] = (
    (("PGHOST", "PGPORT"), ("PGPASSWORD",)),
    (("POSTGRES_HOST", "POSTGRES_PORT"), ("POSTGRES_PASSWORD",)),
    (("PG_HOST", "PG_PORT"), ("PG_PASSWORD", "POSTGRES_PASSWORD")),
)


def operator_pgpass_for_any_host(operator: Mapping[str, Optional[str]]) -> Optional[str]:
    """The operator's libpq password file, when it has an entry for any host.

    Located as libpq does: ``PGPASSFILE``, else ``%APPDATA%\\postgresql\\pgpass.conf``
    on Windows, else ``$HOME/.pgpass``, all from the operator's environment.
    libpq matches the host field ``*`` only as the line's first two
    characters, skips ``#`` comment lines, and on POSIX ignores a file that
    is not a regular file or that its group or others can read; so does this.
    """
    values = {k.strip().upper(): v for k, v in operator.items()}
    if values.get("PGPASSFILE"):
        path = Path(str(values["PGPASSFILE"])).expanduser()
    elif os.name == "nt":
        if not values.get("APPDATA"):
            return None
        path = Path(str(values["APPDATA"])) / "postgresql" / "pgpass.conf"
    else:
        if not values.get("HOME"):
            return None
        path = Path(str(values["HOME"])) / ".pgpass"
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            return None
        if os.name != "nt" and mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if any(line.startswith("*:") for line in lines):
        return str(path)
    return None


def _pgpass_pairing(name: str) -> Tuple[str, ...]:
    for endpoints, passwords in _PGPASS_PAIRED_BY:
        if name in endpoints:
            return passwords
    return ()


def _matches(name: str, patterns: Tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, p) if "*" in p else name == p for p in patterns)


def _behind_blank(name: str, operator_values: Mapping[str, Optional[str]]) -> bool:
    """Whether the operator set ``name`` (upper case) to a blank value."""
    return name in operator_values and not (operator_values[name] or "").strip()


def _shape_prefix(name: str) -> Optional[str]:
    for suffix in ENDPOINT_NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return None


def _dlt_section(name: str) -> str:
    """``DESTINATION__POSTGRES__CREDENTIALS__HOST`` -> ``DESTINATION__POSTGRES__CREDENTIALS__``."""
    at = name.rfind("CREDENTIALS")
    return name[: at + len("CREDENTIALS")] + "__" if at >= 0 else ""


def _pairing(group: EndpointGroup, name: str) -> Tuple[Tuple[str, ...], ...]:
    section = _dlt_section(name)
    return tuple(tuple(p.replace("{section}", section) for p in path) for path in group.paired_by)


def _carries_its_password(group: EndpointGroup, value: str) -> bool:
    """Whether ``value``, a connection string of ``group``, holds its own
    password: after the user name in its user info, or under one of
    ``group.connection_string_passwords`` in its query, and not blank. Parsed
    as a URL (RFC 3986 section 3.2.1, :func:`urllib.parse.urlsplit`); a value
    that does not parse holds none."""
    try:
        parts = urlsplit(value.strip())
        password = parts.password
        query = parse_qsl(parts.query)
    except ValueError:
        return False
    if password and password.strip():
        return True
    wanted = {name.lower() for name in group.connection_string_passwords}
    return any(key.lower() in wanted and item.strip() for key, item in query)


def endpoint_group(service: str) -> EndpointGroup:
    """The group of ``ENDPOINT_CREDENTIAL_GROUPS`` named ``service``."""
    for group in ENDPOINT_CREDENTIAL_GROUPS:
        if group.service == service:
            return group
    raise KeyError(service)


def is_endpoint_of(group: EndpointGroup, name: str) -> bool:
    """Whether ``name``, in any case, is one of ``group``'s endpoint variables
    (patterns included), as :func:`unpaired_endpoints` compares names."""
    return _matches(name.strip().upper(), group.endpoints)


def environment_is_case_insensitive() -> bool:
    """Whether variable names are case-insensitive here, as ``os.environ`` keys
    them on Windows (``pghost`` and ``PGHOST`` are one variable)."""
    return os.name == "nt"


def unpaired_endpoints(
    project: Mapping[str, Optional[str]],
    operator: Mapping[str, Optional[str]],
    *,
    case_insensitive_env: Optional[bool] = None,
) -> Dict[str, EndpointRefusal]:
    """The endpoint variables in ``project`` that would carry an operator credential.

    ``project`` is what the project's dotenv files set (merged); ``operator``
    is the real environment, plus an operator ``FLUID_SECRETS_FILE``. An
    operator variable counts as a credential only when it is non-empty and
    differs from the project's own value for that name: the same value means
    the project file already holds that secret, as on a second load in one run.
    Empty endpoint values redirect nothing and are never refused. Names are
    compared upper-cased, as everywhere in this module.

    A project credential pairs an endpoint only if it is the value readers
    will see: non-blank, not replaced by an operator variable of that name
    (even an empty one: the operator's value wins the export, and a reader
    then falls back to the keyring), and spelled as readers look it up. On
    POSIX that is upper case (``openai_api_key`` is another variable, which no
    SDK reads); ``case_insensitive_env`` (default: Windows) lifts that. The
    exception is a group's :attr:`EndpointGroup.blank_reads_project`, whose
    readers read the project's value for a blank operator variable: behind
    one it still pairs an endpoint that is behind one too.
    """
    if case_insensitive_env is None:
        case_insensitive_env = environment_is_case_insensitive()
    operator_values = {k.strip().upper(): v for k, v in operator.items()}
    supplied = {k.strip().upper(): v for k, v in project.items() if v}
    held = {
        name for name, value in operator_values.items() if value and supplied.get(name) != value
    }
    in_effect: Set[str] = set()
    # project credentials an operator's blank variable of the same name
    # replaces in the environment (see EndpointGroup.blank_reads_project)
    behind_blank: Set[str] = set()
    for key, value in project.items():
        name = key.strip().upper()
        if not value or not value.strip():
            continue
        if not case_insensitive_env and key.strip() != name:
            continue
        if name in operator_values and operator_values[name] != value:
            if not (operator_values[name] or "").strip():
                behind_blank.add(name)
            continue
        in_effect.add(name)
    # a switch counts wherever it is set, by the operator or the project
    set_anywhere = {name for name, value in operator_values.items() if value} | set(supplied)
    pgpass_checked = False
    pgpass_any_host: Optional[str] = None

    refused: Dict[str, EndpointRefusal] = {}
    for key, value in project.items():
        name = key.strip().upper()
        if not value or not name:
            continue
        groups = [g for g in ENDPOINT_CREDENTIAL_GROUPS if _matches(name, g.endpoints)]
        if not groups and name in _UNLISTED_LLM_NAMES:
            groups = [UNLISTED_LLM_ENDPOINTS]
        services: List[str] = []
        creds: Set[str] = set()
        # the operator's own variables among ``creds``; a project file pairs
        # nothing while one is set
        holds: Set[str] = set()
        hints: List[str] = []
        pairing: List[Tuple[str, ...]] = []
        switches: List[str] = []
        pairs_inside = False
        for group in groups:
            theirs = {c for c in held if _matches(c, group.credentials)}
            theirs |= _operator_authenticators(group, operator_values, supplied)
            creds |= theirs
            holds |= theirs
            passwords = _pgpass_pairing(name) if group.service == "PostgreSQL" else ()
            if passwords and not theirs and not any(p in in_effect for p in passwords):
                if not pgpass_checked:
                    pgpass_any_host = operator_pgpass_for_any_host(operator)
                    pgpass_checked = True
                if pgpass_any_host:
                    creds.add(f"the '*' entry in {pgpass_any_host} (set {passwords[0]})")
                    if passwords not in pairing:
                        pairing.append(passwords)
            if theirs or not group.ambient:
                continue
            paths = _pairing(group, name)
            active = [s for s in group.switches if s in set_anywhere]
            if active:
                switches.extend(s for s in active if s not in switches)
                paths = ()
            seen = in_effect
            if group.blank_reads_project and _behind_blank(name, operator_values):
                seen = in_effect | {c for c in behind_blank if c in group.blank_reads_project}
            if paths and all(any(_matches(c, path) for c in seen) for path in paths):
                continue
            connection_string = _matches(name, group.connection_strings)
            if connection_string and not active and _carries_its_password(group, value):
                continue
            services.append(group.service)
            pairs_inside = pairs_inside or (connection_string and not active)
            hints.extend(path[0] for path in paths if path[0] not in hints)
            pairing.extend(path for path in paths if path not in pairing)
        prefix = _shape_prefix(name)
        if prefix:
            head = prefix + "_"
            theirs = {
                c for c in held if c.startswith(head) and c.endswith(CREDENTIAL_NAME_SUFFIXES)
            }
            creds |= theirs
            holds |= theirs
        if creds or services:
            refused[key] = EndpointRefusal(
                name=key,
                services=tuple(services),
                operator_credentials=tuple(sorted(creds)),
                pairs_with=tuple(hints),
                pairing=tuple(pairing),
                switches=tuple(switches),
                pairable=bool(pairing) and not holds and not switches,
                pairs_inside=pairs_inside and not holds,
            )
    return refused


def _project_authenticator(group: EndpointGroup, value: str) -> bool:
    """Whether ``value`` of one of ``group.authenticators`` signs in with the
    project's own credential only."""
    return value.strip().lower() in group.project_authenticators


def _operator_authenticators(
    group: EndpointGroup,
    operator_values: Mapping[str, Optional[str]],
    supplied: Mapping[str, Optional[str]],
) -> Set[str]:
    """The authenticators of ``group`` the operator sets: credentials of theirs.

    Even with the project file's value, readers see the operator's variable.
    Only a value a project file may set itself is let through when the two
    match: that is what a second load in one run sees of the project's own
    export.
    """
    return {
        name
        for name, value in operator_values.items()
        if value
        and value.strip()
        and _matches(name, group.authenticators)
        and (supplied.get(name) != value or not _project_authenticator(group, value))
    }


def _project_endpoint_in_effect(
    group: EndpointGroup,
    key: str,
    value: Optional[str],
    operator_values: Mapping[str, Optional[str]],
) -> bool:
    """Whether the project's ``key`` is an endpoint of ``group`` that readers see.

    It is unless the operator set the variable to another value: a blank one
    makes the credential resolver fall back to the project's files, and the
    same one is what a second load in one run sees of the project's export.
    """
    name = key.strip().upper()
    if not value or not value.strip() or not _matches(name, group.endpoints):
        return False
    theirs = operator_values.get(name)
    return not (theirs and theirs.strip()) or theirs == value


def refused_project_authenticators(
    project: Mapping[str, Optional[str]],
    operator: Mapping[str, Optional[str]],
) -> Dict[str, EndpointGroup]:
    """The authenticator settings in ``project`` that may not load.

    ``project`` is what loads from the project's dotenv files once
    :func:`unpaired_endpoints` has dropped what it refuses; ``operator`` is
    as there. While an endpoint of a group with ``authenticators`` comes from
    the project files, a project value of one of them other than the group's
    ``project_authenticators`` would sign in to the host the project chose
    with the operator's identity or material. Returns each such name, as the
    project file spells it, with its group. Names compare in any case: the
    pydantic-settings readers (the dlt bridge's ``SnowflakeCredentials``) do.
    """
    operator_values = {k.strip().upper(): v for k, v in operator.items()}
    refused: Dict[str, EndpointGroup] = {}
    for group in ENDPOINT_CREDENTIAL_GROUPS:
        if not group.authenticators or not any(
            _project_endpoint_in_effect(group, key, value, operator_values)
            for key, value in project.items()
        ):
            continue
        for key, value in project.items():
            if (
                value
                and value.strip()
                and _matches(key.strip().upper(), group.authenticators)
                and not _project_authenticator(group, value)
            ):
                refused[key] = group
    return refused
