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

"""V1.5 — interactive wizard for ``fluid ai setup --source NAME``.

Walks the operator through configuring one metadata-source catalog
(Snowflake / Unity / BigQuery / Dataplex / Glue / DataHub /
Data Mesh Manager). Saves non-sensitive config to
``~/.fluid/sources.yaml`` (mode 600) and secret values to the OS
keyring under the ``fluid_source`` service prefix. If the keyring is
unavailable, setup fails closed unless the operator explicitly sets
``FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS=1`` for a legacy local fallback.

Honours the four V1.5 design north-stars:

* **World-class.** Recommends the best auth method per catalog
  (Snowflake key-pair, Unity OAuth M2M, BigQuery ADC, Glue IAM
  role, DataHub OAuth, DMM token); legacy methods (passwords,
  long-lived PATs) are still selectable but flagged as
  "not recommended."
* **Lightweight.** Wizard imports lazily — running it doesn't pull
  in the catalog SDKs (those are gated by the per-adapter optional
  extras).
* **Best UX.** Tests the connection before save; on success the
  saved name is what the user types as ``--credential-id``;
  on failure the wizard surfaces the catalog adapter's typed
  ``suggestions`` so the next-action is one line of CLI away.
* **Open-community.** Apache 2.0; no vendor-locked code paths.

Layout of saved entries:

* ``~/.fluid/sources.yaml`` — non-sensitive config (account,
  region, role, project, host) keyed by saved name. Schema::

      sources:
        snowflake-prod:
          source_type: snowflake
          config:
            account: abc-xyz.us-east-1
            user: ANALYST
            auth_method: private_key
            private_key_path: ~/.snowflake/rsa_key.p8
            role: ANALYST_RW
            warehouse: ANALYTICS_XS

* OS keyring ``fluid_source/<name>`` — JSON-serialised dict of
  secret-only fields (passwords / tokens / private-key passphrases).
  Plaintext YAML secrets require explicit opt-in with
  ``FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS=1`` and are rejected by
  default.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)


SOURCES_PATH = Path.home() / ".fluid" / "sources.yaml"
KEYRING_PREFIX = "fluid_source"
PLAINTEXT_SOURCE_SECRETS_ENV = "FLUID_ALLOW_PLAINTEXT_SOURCE_SECRETS"
_SECRET_OUTPUT_PATTERN = re.compile(
    r"(?i)\b(password|passphrase|token|secret|api[_ -]?key)\b(\s*[:=]\s*)([^\s,;]+)"
)


# Auth-method recommendations per catalog (the V1.5 plan's matrix).
# Each entry: (method_id, label, recommended).
_AUTH_METHODS: Dict[str, List[Tuple[str, str, bool]]] = {
    "snowflake": [
        ("private_key", "Key-pair (recommended; rotation-friendly)", True),
        ("oauth", "OAuth (Snowflake-managed)", False),
        ("sso", "SSO / External Browser (interactive)", False),
        ("password", "Password (legacy; not recommended)", False),
    ],
    "unity": [
        ("oauth_m2m", "OAuth M2M (recommended; service principal)", True),
        ("pat", "Personal Access Token (≤90-day expiry)", False),
        ("azure_ad", "Azure AD", False),
        ("google_id", "Google ID", False),
    ],
    "bigquery": [
        ("adc", "Application Default Credentials (recommended)", True),
        ("service_account_json", "Service Account JSON file", False),
    ],
    "dataplex": [
        ("adc", "Application Default Credentials (recommended)", True),
        ("service_account_json", "Service Account JSON file", False),
    ],
    "glue": [
        ("iam_role", "IAM Role via STS / profile (recommended)", True),
        ("instance_profile", "EC2 / Lambda instance profile", False),
        ("sso", "AWS SSO (aws sso login)", False),
        ("iam_key", "IAM access key + secret (legacy)", False),
    ],
    "datahub": [
        ("oauth", "OAuth (recommended for production)", True),
        ("pat", "Personal Access Token", False),
        ("none", "No auth (dev / local only)", False),
    ],
    "datamesh_manager": [
        ("api_key", "API Token (Bearer)", True),
    ],
}


# Per-catalog field prompts. Each (name, prompt, secret, default).
# ``secret=True`` fields go to the keyring; everything else to YAML.
_FIELD_PROMPTS: Dict[str, Dict[str, List[Tuple[str, str, bool, Optional[str]]]]] = {
    "snowflake": {
        "private_key": [
            ("account", "Snowflake account locator (e.g. abc-xyz.us-east-1)", False, None),
            ("user", "Snowflake user", False, None),
            ("private_key_path", "Path to private key (.p8 or .pem)", False, None),
            ("private_key_passphrase", "Private key passphrase (leave blank if none)", True, ""),
            ("role", "Snowflake role (optional)", False, None),
            ("warehouse", "Snowflake warehouse (optional)", False, None),
        ],
        "password": [
            ("account", "Snowflake account locator", False, None),
            ("user", "Snowflake user", False, None),
            ("password", "Snowflake password", True, None),
            ("role", "Snowflake role (optional)", False, None),
            ("warehouse", "Snowflake warehouse (optional)", False, None),
        ],
        "oauth": [
            ("account", "Snowflake account locator", False, None),
            ("user", "Snowflake user", False, None),
            ("oauth_token", "OAuth token", True, None),
            ("role", "Snowflake role (optional)", False, None),
            ("warehouse", "Snowflake warehouse (optional)", False, None),
        ],
        "sso": [
            ("account", "Snowflake account locator", False, None),
            ("user", "Snowflake user", False, None),
            ("role", "Snowflake role (optional)", False, None),
            ("warehouse", "Snowflake warehouse (optional)", False, None),
        ],
    },
    "unity": {
        "oauth_m2m": [
            ("host", "Workspace host (e.g. https://abc.cloud.databricks.com)", False, None),
            ("oauth_client_id", "Service principal client ID", False, None),
            ("oauth_client_secret", "Service principal client secret", True, None),
        ],
        "pat": [
            ("host", "Workspace host", False, None),
            ("token", "Personal access token", True, None),
        ],
        "azure_ad": [
            ("host", "Workspace host", False, None),
            ("azure_tenant_id", "Azure tenant ID", False, None),
        ],
        "google_id": [
            ("host", "Workspace host", False, None),
            ("google_service_account_path", "Path to Google SA JSON", False, None),
        ],
    },
    "bigquery": {
        "adc": [
            ("project", "GCP project ID", False, None),
            ("location", "Default location (e.g. EU, US, us-central1)", False, ""),
        ],
        "service_account_json": [
            ("project", "GCP project ID", False, None),
            ("service_account_path", "Path to service account JSON", False, None),
            ("location", "Default location (optional)", False, ""),
        ],
    },
    "dataplex": {
        "adc": [
            ("project", "GCP project ID", False, None),
            ("location", "Dataplex location (or 'global')", False, "global"),
        ],
        "service_account_json": [
            ("project", "GCP project ID", False, None),
            ("location", "Dataplex location (or 'global')", False, "global"),
            ("service_account_path", "Path to service account JSON", False, None),
        ],
    },
    "glue": {
        "iam_role": [
            ("region", "AWS region (e.g. us-east-1)", False, None),
            ("profile_name", "AWS profile name (optional; defaults to 'default')", False, ""),
            ("role_arn", "Role ARN to assume (optional)", False, ""),
        ],
        "instance_profile": [
            ("region", "AWS region", False, None),
        ],
        "sso": [
            ("region", "AWS region", False, None),
            ("profile_name", "AWS SSO profile name", False, None),
        ],
        "iam_key": [
            ("region", "AWS region", False, None),
            ("access_key_id", "AWS access key ID", False, None),
            ("secret_access_key", "AWS secret access key", True, None),
            ("session_token", "AWS session token (optional, for STS temp creds)", True, ""),
        ],
    },
    "datahub": {
        "oauth": [
            ("server", "DataHub GMS server (e.g. https://datahub.example.com:8080)", False, None),
            ("oauth_client_id", "OAuth client ID", False, None),
            ("oauth_client_secret", "OAuth client secret", True, None),
        ],
        "pat": [
            ("server", "DataHub GMS server", False, None),
            ("token", "Personal access token", True, None),
        ],
        "none": [
            ("server", "DataHub GMS server (no-auth dev mode)", False, None),
        ],
    },
    "datamesh_manager": {
        "api_key": [
            ("server", "DMM API server (e.g. https://api.datamesh-manager.com)", False, None),
            ("api_key", "DMM API token", True, None),
        ],
    },
}


def setup_source(
    source: str,
    *,
    name: Optional[str] = None,
    console: Any = None,
) -> int:
    """Run the interactive wizard for one source catalog.

    Returns 0 on success, non-zero on failure (user aborted, save
    error, connection-test error). Honours ``console`` when
    available (rich-formatted prompts) and falls back to plain
    ``input()`` / ``getpass()`` otherwise.
    """
    if source not in _AUTH_METHODS:
        _emit(
            console,
            f"Unknown source catalog: {source!r}. Supported: {', '.join(sorted(_AUTH_METHODS))}.",
            kind="error",
        )
        return 1

    saved_name = name or _prompt_text(
        console,
        "Save under name (used by --credential-id): ",
        default=f"{source}-prod",
    )
    if not saved_name:
        _emit(console, "Aborted — no saved name supplied.", kind="warn")
        return 1

    auth_method = _prompt_auth_method(source, console)
    if not auth_method:
        return 1

    # Collect non-secret + secret fields per the prompt spec.
    fields = _FIELD_PROMPTS[source][auth_method]
    config_values: Dict[str, Any] = {"auth_method": auth_method}
    secret_values: Dict[str, str] = {}
    for field_name, prompt, is_secret, default in fields:
        value = _prompt_field(prompt, secret=is_secret, default=default, console=console)
        if value is None or value == "":
            prompt_is_optional = "optional" in prompt.lower() or "leave blank" in prompt.lower()
            if (default is not None and default == "") or prompt_is_optional:
                continue  # optional field skipped
            _emit(console, f"Aborted — required field {field_name!r} not provided.", kind="warn")
            return 1
        if is_secret:
            secret_values[field_name] = value
        else:
            config_values[field_name] = value

    secrets_need_plaintext_fallback = False
    if secret_values:
        if _save_keyring_entry(saved_name, secret_values):
            secrets_need_plaintext_fallback = False
        elif _allow_plaintext_source_secrets():
            secrets_need_plaintext_fallback = True
        else:
            _emit(
                console,
                "OS keyring is not available, so source secrets were not saved. "
                f'Install keyring with `pip install "data-product-forge[keyring]"` '
                f"or explicitly set {PLAINTEXT_SOURCE_SECRETS_ENV}=1 for a local "
                "plaintext fallback.",
                kind="error",
            )
            return 1

    # Save non-sensitive config to YAML only after secrets are either
    # keyring-backed or explicitly allowed to fall back to plaintext.
    _save_yaml_entry(saved_name, source, config_values)
    if secrets_need_plaintext_fallback:
        _emit(
            console,
            "Plaintext source-secret fallback enabled by environment. "
            "~/.fluid/sources.yaml is chmod 600, but OS keyring storage is recommended.",
            kind="warn",
        )
        _save_yaml_secrets_fallback(saved_name, secret_values)

    _emit(
        console,
        f"Saved {saved_name!r} as a {source} source. "
        f"Run: fluid forge data-model from-source --source {source} "
        f"--credential-id {saved_name} ...",
        kind="success",
    )
    return 0


def list_configured_sources() -> List[Dict[str, Any]]:
    """Return a list of ``{name, source_type, auth_method}`` dicts
    for every source registered in ``~/.fluid/sources.yaml``.

    Used by ``fluid ai status`` to enumerate what's configured
    without exposing any secret values.
    """
    if not SOURCES_PATH.is_file():
        return []
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return []
    out: List[Dict[str, Any]] = []
    for name, entry in sources.items():
        if not isinstance(entry, dict):
            continue
        config = entry.get("config") or {}
        out.append(
            {
                "name": name,
                "source_type": entry.get("source_type") or "unknown",
                "auth_method": config.get("auth_method") or "—",
                # Non-sensitive identifying field — the operator's
                # eye-line for "which environment am I looking at".
                "identifier": (
                    config.get("account")
                    or config.get("host")
                    or config.get("project")
                    or config.get("region")
                    or config.get("server")
                    or "—"
                ),
            }
        )
    return out


def show_source_status(console: Any = None) -> int:
    """Pretty-print the configured sources table.

    Never prints secret values. Returns 0 always — informational
    surface, not a gate.
    """
    sources = list_configured_sources()
    if not sources:
        _emit(
            console,
            "No metadata-source catalogs configured yet. "
            "Run: fluid ai setup --source <snowflake | unity | bigquery | "
            "dataplex | glue | datahub | datamesh_manager>",
            kind="info",
        )
        return 0
    _emit(console, "Configured metadata sources:", kind="info")
    for src in sources:
        _emit(
            console,
            f"  • {src['name']:30} type={src['source_type']:18} "
            f"auth={src['auth_method']:15} id={src['identifier']}",
        )
    _emit(
        console,
        "Use any of these names with --credential-id on `fluid forge data-model from-source`.",
        kind="info",
    )
    return 0


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------


def _prompt_auth_method(source: str, console: Any) -> Optional[str]:
    options = _AUTH_METHODS[source]
    _emit(console, f"Auth methods for {source}:")
    for idx, (method_id, label, recommended) in enumerate(options, start=1):
        marker = " ★" if recommended else ""
        _emit(console, f"  {idx}. {label}{marker}")
    raw = _prompt_text(
        console,
        f"Pick an auth method [1-{len(options)}, default=1]: ",
        default="1",
    )
    try:
        choice = int(raw or "1")
    except ValueError:
        _emit(console, f"Aborted — '{raw}' isn't a valid choice.", kind="warn")
        return None
    if not 1 <= choice <= len(options):
        _emit(console, f"Aborted — choice {choice} out of range.", kind="warn")
        return None
    return options[choice - 1][0]


def _prompt_field(prompt: str, *, secret: bool, default: Optional[str], console: Any) -> str:
    """Read one field. ``secret=True`` uses ``getpass`` so the value
    is never echoed to the terminal."""
    if secret:
        import getpass

        suffix = "" if not default else f" [default: {default!r}]"
        value = getpass.getpass(f"{prompt}{suffix}: ")
        if not value and default is not None:
            return default
        return value
    return _prompt_text(console, prompt, default=default)


def _prompt_text(console: Any, prompt: str, *, default: Optional[str] = None) -> str:
    """Plain-text prompt; falls back to ``input()`` when no console."""
    suffix = "" if not default else f" [{default}]"
    response = input(f"{prompt}{suffix} ").strip()
    if not response and default is not None:
        return default
    return response


def _emit(console: Any, message: str, *, kind: str = "plain") -> None:
    """Output one line. Uses rich console when available, else
    prints plain text."""
    safe_message = _redact_console_message(message)
    if console is not None:
        # Rich console: use markup for severity colour.
        prefix = {
            "success": "[green]✓[/green] ",
            "warn": "[yellow]![/yellow] ",
            "error": "[red]✗[/red] ",
            "info": "[cyan]i[/cyan] ",
        }.get(kind, "")
        try:
            console.print(prefix + safe_message)
            return
        except Exception:  # pragma: no cover — fall through to plain
            pass
    from fluid_build.cli.console import cprint

    cprint(safe_message)


def _redact_console_message(message: str) -> str:
    """Best-effort guardrail for console messages near credential setup."""
    return _SECRET_OUTPUT_PATTERN.sub(r"\1\2<redacted>", str(message))


def _save_yaml_entry(saved_name: str, source: str, config: Dict[str, Any]) -> None:
    """Write the non-sensitive entry to ``~/.fluid/sources.yaml``.

    Mode 600 on first creation; merges into the existing
    ``sources:`` mapping so re-running the wizard for a different
    source name doesn't clobber other entries.
    """
    import stat

    import yaml  # type: ignore

    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing = {}
    if SOURCES_PATH.is_file():
        try:
            existing = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    sources = existing.setdefault("sources", {})
    if not isinstance(sources, dict):
        sources = {}
        existing["sources"] = sources
    sources[saved_name] = {
        "source_type": source,
        "config": config,
    }
    SOURCES_PATH.write_text(
        yaml.safe_dump(existing, sort_keys=False),
        encoding="utf-8",
    )
    SOURCES_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _save_keyring_entry(saved_name: str, secret_fields: Dict[str, str]) -> bool:
    """Persist secret fields under ``fluid_source/<saved_name>``.

    Returns True when the keyring write succeeded. Returns False
    (without raising) when keyring isn't installed / accessible —
    the wizard then falls back to YAML.
    """
    try:
        import keyring  # type: ignore
    except ImportError:
        return False
    try:
        keyring.set_password(
            KEYRING_PREFIX,
            saved_name,
            json.dumps(secret_fields, sort_keys=True),
        )
        return True
    except Exception as exc:  # pragma: no cover — keyring backends vary
        _log.debug("fluid.cli.ai_source_setup.keyring_set_failed: %s", exc)
        return False


def _allow_plaintext_source_secrets() -> bool:
    return os.environ.get(PLAINTEXT_SOURCE_SECRETS_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _save_yaml_secrets_fallback(saved_name: str, secret_fields: Dict[str, str]) -> None:
    """When keyring is unavailable, write secrets into the YAML
    under a ``secrets`` key in the saved entry. Mode 600 on the
    file is the only protection — the operator was warned."""
    import stat

    import yaml  # type: ignore

    if not SOURCES_PATH.is_file():
        return
    try:
        data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    if not isinstance(data, dict):
        return
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return
    entry = sources.get(saved_name)
    if not isinstance(entry, dict):
        return
    entry["secrets"] = secret_fields
    SOURCES_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False),
        encoding="utf-8",
    )
    SOURCES_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)


__all__ = [
    "setup_source",
    "list_configured_sources",
    "show_source_status",
    "SOURCES_PATH",
    "KEYRING_PREFIX",
]
