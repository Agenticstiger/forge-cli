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

# fluid_build/providers/snowflake/util/config.py
"""Snowflake configuration utilities with secure credential resolution."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_WAREHOUSE = "COMPUTE_WH"
DEFAULT_SCHEMA = "PUBLIC"
_ENV_TEMPLATE_RE = re.compile(r"\{\{\s*env\.(\S+?)\s*\}\}")
_ACCOUNT_HOST_RE = re.compile(r"\.snowflakecomputing\.com$", re.IGNORECASE)
_ACCOUNT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ACCOUNT_REGION_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$")
_ACCOUNT_CLOUD_SUFFIXES = frozenset({"aws", "azure", "gcp"})

# Keys that carry secret material. These MUST never appear in the `_sources`
# map returned to callers, nor be echoed into diagnostic reports.
SECRET_KEYS = frozenset({"password", "private_key_passphrase", "oauth_token"})


def _is_present(value: Any) -> bool:
    return value is not None and value != ""


def resolve_env_templates(value: Any) -> Any:
    """
    Resolve ``{{ env.VAR }}`` placeholders from environment variables.

    Unresolved placeholders are left intact so callers can decide whether to
    error, warn, or fall back. This is the single source of truth for env
    template resolution across the Snowflake provider and CLI — other modules
    should import this rather than redefining the regex.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1).strip()
        return os.environ.get(var_name, match.group(0))

    return _ENV_TEMPLATE_RE.sub(_replace, value).strip()


# Backwards-compatible private alias for existing internal call sites.
_resolve_env_templates = resolve_env_templates


def _normalize_account_identifier(account: Optional[str]) -> Optional[str]:
    if not _is_present(account):
        return None

    value = str(account).strip()
    if "{{" in value and "}}" in value:
        return value

    if "://" in value:
        parsed = urlsplit(value)
        value = parsed.netloc or parsed.path
    elif "/" in value.strip("/"):
        raise ValueError(
            "Invalid Snowflake account identifier. Accepted forms include "
            "'org-account', 'xy12345', 'xy12345.region', "
            "'xy12345.region.aws', and full *.snowflakecomputing.com hostnames."
        )

    value = value.split("@")[-1]
    value = value.split("/", 1)[0]
    value = value.split("?", 1)[0]
    value = value.split("#", 1)[0]
    value = value.rstrip(".")
    value = _ACCOUNT_HOST_RE.sub("", value)
    value = value.strip()
    if not value:
        return None

    if value.lower().startswith("app-"):
        value = value[4:]

    segments = value.split(".")
    if any(not segment for segment in segments):
        raise ValueError(
            "Invalid Snowflake account identifier. Accepted forms include "
            "'org-account', 'xy12345', 'xy12345.region', "
            "'xy12345.region.aws', and full *.snowflakecomputing.com hostnames."
        )

    if not all(_ACCOUNT_SEGMENT_RE.match(segment) for segment in segments):
        raise ValueError(
            "Invalid Snowflake account identifier. Accepted forms include "
            "'org-account', 'xy12345', 'xy12345.region', "
            "'xy12345.region.aws', and full *.snowflakecomputing.com hostnames."
        )

    has_privatelink = False
    if segments and segments[-1].lower() == "privatelink":
        has_privatelink = True
        segments = segments[:-1]

    if not segments:
        raise ValueError(
            "Invalid Snowflake account identifier. Accepted forms include "
            "'org-account', 'xy12345', 'xy12345.region', "
            "'xy12345.region.aws', and full *.snowflakecomputing.com hostnames."
        )

    if len(segments) > 1 and segments[-1].lower() in _ACCOUNT_CLOUD_SUFFIXES:
        segments = segments[:-1]

    normalized = ".".join(segments)
    if "-" in segments[0] and len(segments) > 2:
        raise ValueError(
            "Invalid Snowflake organization/account identifier. Expected "
            "'org-account' or 'org-account.privatelink'."
        )
    if (
        "-" not in segments[0]
        and len(segments) > 1
        and not _ACCOUNT_REGION_SEGMENT_RE.match(segments[1])
    ):
        raise ValueError(
            "Invalid Snowflake account identifier. Accepted forms include "
            "'org-account', 'xy12345', 'xy12345.region', "
            "'xy12345.region.aws', and full *.snowflakecomputing.com hostnames."
        )

    if has_privatelink:
        normalized = f"{normalized}.privatelink"

    return normalized or None


def _normalize_value(key: str, value: Any) -> Any:
    if not _is_present(value):
        return None

    if isinstance(value, str):
        value = _resolve_env_templates(value)
        value = value.strip()
        if not value:
            return None
        if key == "account":
            return _normalize_account_identifier(value)
        return value

    return value


def _first_present(values: list[tuple[Any, str]]) -> tuple[Any, Optional[str]]:
    for value, source in values:
        if _is_present(value):
            return value, source
    return None, None


def _iter_snowflake_bindings(
    contract: Mapping[str, Any],
) -> list[tuple[str, Dict[str, Any], Dict[str, Any]]]:
    bindings: list[tuple[str, Dict[str, Any], Dict[str, Any]]] = []

    top_binding = contract.get("binding", {})
    if isinstance(top_binding, Mapping) and top_binding.get("platform") == "snowflake":
        bindings.append(
            ("contract.binding", dict(top_binding), dict(top_binding.get("location", {})))
        )

    for index, expose in enumerate(contract.get("exposes", []) or []):
        if not isinstance(expose, Mapping):
            continue
        binding = expose.get("binding", {})
        if isinstance(binding, Mapping) and binding.get("platform") == "snowflake":
            legacy_location = expose.get("location", {})
            # Fall back to legacy when binding.location is missing OR empty;
            # `binding.get("location", ...)` alone doesn't handle empty dicts.
            location = binding.get("location") or legacy_location or {}
            bindings.append((f"exposes[{index}].binding", dict(binding), dict(location or {})))

    return bindings


def _extract_contract_settings(
    contract: Optional[Mapping[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, str]]:
    if not contract:
        return {}, {}

    settings: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    binding_candidates = _iter_snowflake_bindings(contract)

    def _pick(key: str, *candidates: tuple[Any, str]) -> None:
        value, source = _first_present(list(candidates))
        if _is_present(value):
            settings[key] = _normalize_value(key, value)
            if source:
                sources[key] = source

    for key in ["account", "database", "schema", "warehouse", "role", "user", "authenticator"]:
        candidates: list[tuple[Any, str]] = []
        for binding_path, binding, location in binding_candidates:
            properties = binding.get("properties", {}) if isinstance(binding, Mapping) else {}
            if key in location:
                candidates.append((location.get(key), f"{binding_path}.location.{key}"))
            if isinstance(properties, Mapping) and key in properties:
                candidates.append((properties.get(key), f"{binding_path}.properties.{key}"))
        _pick(key, *candidates)

    for index, build in enumerate(contract.get("builds", []) or []):
        if not isinstance(build, Mapping):
            continue
        execution = build.get("execution", {})
        runtime = execution.get("runtime", {}) if isinstance(execution, Mapping) else {}
        resources = runtime.get("resources", {}) if isinstance(runtime, Mapping) else {}
        if runtime.get("platform") != "snowflake" or not isinstance(resources, Mapping):
            continue

        for key in ["warehouse", "database", "schema", "role"]:
            if key not in settings and _is_present(resources.get(key)):
                settings[key] = _normalize_value(key, resources.get(key))
                sources[key] = f"builds[{index}].execution.runtime.resources.{key}"

    return settings, sources


def _resolve_with_adapter(
    key: str,
    cli_value: Any,
    *,
    project_root: Path,
    environment: Optional[str],
) -> tuple[Any, Optional[str]]:
    try:
        from fluid_build.credentials import CredentialConfig, get_snowflake_adapter

        adapter = get_snowflake_adapter(
            CredentialConfig(project_root=project_root, environment=environment or "dev")
        )
        value = adapter.get_credential(key, required=False, cli_value=cli_value)
        if _is_present(value):
            source = "explicit_or_contract" if _is_present(cli_value) else "credential_resolver"
            return value, source
        return None, None
    except Exception as exc:
        logger.debug("Snowflake credential adapter unavailable for %s: %s", key, exc)
        return None, None


def resolve_snowflake_settings(
    *,
    contract: Optional[Mapping[str, Any]] = None,
    account: Optional[str] = None,
    warehouse: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = None,
    user: Optional[str] = None,
    role: Optional[str] = None,
    authenticator: Optional[str] = None,
    password: Optional[str] = None,
    private_key_path: Optional[str] = None,
    private_key_passphrase: Optional[str] = None,
    oauth_token: Optional[str] = None,
    project_root: Optional[Path] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Resolve Snowflake connection settings with one consistent precedence order.

    Precedence:
    1. Explicit function arguments
    2. Contract binding / runtime values
    3. Credential adapter and environment variables
    4. Safe defaults (warehouse/schema only)
    """

    project_root = project_root or Path.cwd()
    contract_values, contract_sources = _extract_contract_settings(contract)

    explicit_values = {
        "account": account,
        "warehouse": warehouse,
        "database": database,
        "schema": schema,
        "user": user,
        "role": role,
        "authenticator": authenticator,
        "password": password,
        "private_key_path": private_key_path,
        "private_key_passphrase": private_key_passphrase,
        "oauth_token": oauth_token,
    }

    env_fallbacks = {
        "account": ["SNOWFLAKE_ACCOUNT", "SF_ACCOUNT"],
        "warehouse": ["SNOWFLAKE_WAREHOUSE", "SF_WAREHOUSE"],
        "database": ["SNOWFLAKE_DATABASE", "SF_DATABASE"],
        "schema": ["SNOWFLAKE_SCHEMA", "SF_SCHEMA"],
        "user": ["SNOWFLAKE_USER", "SF_USER"],
        "role": ["SNOWFLAKE_ROLE", "SF_ROLE"],
        "authenticator": ["SNOWFLAKE_AUTHENTICATOR", "SF_AUTHENTICATOR"],
        "password": ["SNOWFLAKE_PASSWORD", "SF_PASSWORD"],
        "private_key_path": ["SNOWFLAKE_PRIVATE_KEY_PATH"],
        "private_key_passphrase": ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"],
        "oauth_token": ["SNOWFLAKE_OAUTH_TOKEN"],
    }

    resolved: Dict[str, Any] = {}
    sources: Dict[str, str] = {}

    for key in explicit_values:
        explicit = _normalize_value(key, explicit_values.get(key))
        contract_value = _normalize_value(key, contract_values.get(key))

        if _is_present(explicit):
            resolved[key] = explicit
            sources[key] = "explicit"
            continue

        if _is_present(contract_value):
            resolved[key] = contract_value
            sources[key] = contract_sources.get(key, "contract")
            continue

        env_value = None
        for env_var in env_fallbacks.get(key, []):
            env_value = _normalize_value(key, os.environ.get(env_var))
            if _is_present(env_value):
                resolved[key] = env_value
                sources[key] = f"env:{env_var}"
                break
        if _is_present(env_value):
            continue

        adapter_value, adapter_source = _resolve_with_adapter(
            key,
            None,
            project_root=project_root,
            environment=environment,
        )
        adapter_value = _normalize_value(key, adapter_value)
        if _is_present(adapter_value):
            resolved[key] = adapter_value
            sources[key] = adapter_source or "credential_resolver"
            continue

    if not _is_present(resolved.get("warehouse")):
        resolved["warehouse"] = DEFAULT_WAREHOUSE
        sources["warehouse"] = "default"
    if not _is_present(resolved.get("schema")):
        resolved["schema"] = DEFAULT_SCHEMA
        sources["schema"] = "default"

    if _is_present(resolved.get("account")):
        resolved["account"] = _normalize_account_identifier(resolved["account"])

    # Never echo the provenance of secret values — avoids leaking, for
    # example, that a passphrase came from `env:SNOWFLAKE_PRIVATE_KEY_PASSPHRASE`
    # into logs or auth reports. The raw secret values are still carried
    # through `resolved` for downstream connection building.
    resolved["_sources"] = {k: v for k, v in sources.items() if k not in SECRET_KEYS}
    return resolved


def resolve_account_and_warehouse(
    account: Optional[str] = None, warehouse: Optional[str] = None
) -> Tuple[str, str]:
    """
    Resolve Snowflake account and warehouse using unified credential resolver.

    Priority order:
    1. Explicit parameters
    2. Credential resolver chain (keyring, encrypted file, env vars, Vault)
    3. Default values

    Returns:
        Tuple of (account, warehouse)

    Raises:
        ValueError: If account cannot be determined
    """
    resolved = resolve_snowflake_settings(account=account, warehouse=warehouse)
    account = resolved.get("account")
    warehouse = resolved.get("warehouse")

    if not account:
        raise ValueError(
            "Snowflake account not specified. "
            "Provide via 'account' parameter, SNOWFLAKE_ACCOUNT environment variable, "
            "or store in keyring with: fluid auth set snowflake --account YOUR_ACCOUNT"
        )

    return account, warehouse


def get_connection_params(
    account: Optional[str] = None,
    warehouse: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = DEFAULT_SCHEMA,
    user: Optional[str] = None,
    contract: Optional[Mapping[str, Any]] = None,
    **kwargs,
) -> dict:
    """
    Build connection parameters using unified credential resolver.

    This function now uses the SnowflakeCredentialAdapter which provides:
    - Secure credential resolution (keyring, .env, Vault, etc.)
    - Multiple authentication methods (password, key-pair, OAuth, SSO)
    - Backward compatibility with environment variables

    Args:
        account: Snowflake account identifier
        warehouse: Compute warehouse name
        database: Optional database name
        schema: Schema name (default: PUBLIC)
        user: Optional username (resolved via credential chain if not provided)
        **kwargs: Additional parameters and credential overrides

    Returns:
        Connection parameters dict for snowflake-connector-python
    """
    resolved = resolve_snowflake_settings(
        contract=contract,
        account=account,
        warehouse=warehouse,
        database=database,
        schema=schema,
        user=user,
        role=kwargs.get("role"),
        authenticator=kwargs.get("authenticator"),
        password=kwargs.get("password"),
        private_key_path=kwargs.get("private_key_path"),
        private_key_passphrase=kwargs.get("private_key_passphrase"),
        oauth_token=kwargs.get("oauth_token"),
        project_root=kwargs.get("project_root"),
        environment=kwargs.get("environment"),
    )

    if not resolved.get("account"):
        raise ValueError(
            "Snowflake account not specified. Set binding.location.account, "
            "SNOWFLAKE_ACCOUNT, or pass an explicit Snowflake account."
        )

    if not resolved.get("user"):
        raise ValueError(
            "Snowflake user not specified. Set SNOWFLAKE_USER or provide an explicit user."
        )

    params = {
        "account": resolved["account"],
        "user": resolved["user"],
        "warehouse": resolved["warehouse"],
    }

    if resolved.get("database"):
        params["database"] = resolved["database"]
    if resolved.get("schema"):
        params["schema"] = resolved["schema"]
    if resolved.get("role"):
        params["role"] = resolved["role"]

    # Authentication preference (highest to lowest trust for automation):
    #   1. Key-pair  — the standard for service accounts at regulated shops
    #   2. OAuth     — federated identity, short-lived tokens
    #   3. Explicit authenticator (e.g. SSO/externalbrowser, oauth, okta)
    #   4. Password  — discouraged for automation; warn when used non-interactively
    #   5. externalbrowser default — only when stdin is a TTY
    if resolved.get("private_key_path"):
        params["private_key_path"] = resolved["private_key_path"]
        if resolved.get("private_key_passphrase"):
            params["private_key_passphrase"] = resolved["private_key_passphrase"]
    elif resolved.get("oauth_token"):
        params["oauth_token"] = resolved["oauth_token"]
        params["authenticator"] = "oauth"
    elif resolved.get("authenticator"):
        params["authenticator"] = resolved["authenticator"]
        if resolved.get("password"):
            params["password"] = resolved["password"]
    elif resolved.get("password"):
        params["password"] = resolved["password"]
        if not os.isatty(0):
            logger.warning(
                "Snowflake password auth used in a non-interactive session. "
                "Prefer key-pair or OAuth for automation."
            )
    else:
        # No explicit credential material — only acceptable when a human is
        # at the terminal to complete the SSO browser flow. In CI or any
        # non-TTY context this should fail fast rather than hang.
        if not os.isatty(0):
            raise ValueError(
                "No Snowflake credentials found and stdin is not a TTY. "
                "Set SNOWFLAKE_PRIVATE_KEY_PATH, SNOWFLAKE_OAUTH_TOKEN, "
                "SNOWFLAKE_PASSWORD, or SNOWFLAKE_AUTHENTICATOR, or run "
                "interactively to use the browser SSO flow."
            )
        params["authenticator"] = "externalbrowser"

    # Attach a QUERY_TAG so every statement issued on this session is
    # attributable in Snowflake's QUERY_HISTORY view. This is the single
    # highest-leverage change for cost/ops observability — without it,
    # platform teams cannot attribute warehouse spend back to contracts.
    query_tag_parts = ["forge"]
    if contract is not None and isinstance(contract, Mapping):
        contract_id = contract.get("id") or contract.get("metadata", {}).get("name")
        if contract_id:
            query_tag_parts.append(str(contract_id))
    if kwargs.get("environment"):
        query_tag_parts.append(str(kwargs["environment"]))
    query_tag = ":".join(query_tag_parts)

    session_params = dict(kwargs.get("session_params") or {})
    session_params.setdefault("QUERY_TAG", query_tag)
    params["session_params"] = session_params

    for key in ["application", "insecure_mode", "ocsp_response_cache_filename"]:
        if key in kwargs and kwargs[key] is not None:
            params[key] = kwargs[key]

    return params


def _get_connection_params_legacy(
    account: str,
    warehouse: str,
    database: Optional[str] = None,
    schema: Optional[str] = "PUBLIC",
    user: Optional[str] = None,
    **kwargs,
) -> dict:
    """
    Legacy connection params builder (backward compatibility).

    This is the original implementation that uses environment variables directly.
    Kept for backward compatibility if credential resolver is not available.
    """
    # Resolve user
    user = user or os.environ.get("SNOWFLAKE_USER") or os.environ.get("SF_USER")

    if not user:
        raise ValueError(
            "Snowflake user not specified. "
            "Provide via 'user' parameter or SNOWFLAKE_USER environment variable."
        )

    # Base parameters
    params = {
        "account": account,
        "user": user,
        "warehouse": warehouse,
    }

    if database:
        params["database"] = database
    if schema:
        params["schema"] = schema

    # Authentication method (priority order)
    if "password" in kwargs:
        params["password"] = kwargs["password"]
    elif "SNOWFLAKE_PASSWORD" in os.environ:
        params["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    elif "private_key" in kwargs:
        params["private_key"] = kwargs["private_key"]
    elif "token" in kwargs:
        params["token"] = kwargs["token"]
    elif "authenticator" in kwargs:
        params["authenticator"] = kwargs["authenticator"]
    elif os.environ.get("SNOWFLAKE_AUTHENTICATOR"):
        params["authenticator"] = os.environ["SNOWFLAKE_AUTHENTICATOR"]
    else:
        # Default to external browser for SSO
        params["authenticator"] = "externalbrowser"

    # Optional parameters
    for key in ["role", "application", "insecure_mode", "ocsp_response_cache_filename"]:
        if key in kwargs:
            params[key] = kwargs[key]

    return params
