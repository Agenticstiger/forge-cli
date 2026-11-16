# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``tofu`` child's environment: an allowlist, never a copy of ``os.environ``.

A ``tofu`` run gets the process basics (``PATH``, ``HOME``, user, locale, the
temporary directory), proxies and CA bundles, OpenTofu's own settings, and the
variables of the clouds the run talks to: the plugin's, the state backend's and
the state-encryption key's. Nothing else. An LLM API key, a GitHub token or the
Command Center's own secrets in the operator's environment never reach ``tofu``
or the provider processes it starts.

Each plugin declares what its provider reads: exact names in
``credential_env_vars`` and whole families in ``credential_env_prefixes``
(``AWS_``, ``SNOWFLAKE_``, ...). A family keeps every way a provider can
authenticate working: a named profile whose ``credential_process`` needs
``PATH`` and ``HOME``, a web identity token file, container credentials, GCP
application default credentials and impersonation, ``SNOWFLAKE_*``,
``DATABRICKS_*``. The state backend and the encryption key provider need their
own cloud's family whatever the plugin is (Snowflake state in S3), so
:func:`build_tofu_env` takes those clouds too.

OpenTofu's settings pass by name, plus ``TF_TOKEN_*`` (registry credentials).
Some never pass, after hashicorp/terraform-exec's ``prohibitedEnvVars``
(``tfexec/cmd.go``): ``TF_CLI_ARGS`` and ``TF_CLI_ARGS_<command>`` would add
flags to every command forge runs (``-auto-approve``, ``-target``,
``-lock=false``); ``TF_VAR_*`` would set the module's variables, which forge
sets itself; ``TF_ENCRYPTION`` would override the module's state encryption
(OpenTofu lets the environment win); ``TF_INPUT`` and ``TF_REATTACH_PROVIDERS``
(a provider process started elsewhere). A dropped ``TF_*`` variable is named
in a warning, never its value.

What a plugin derives for the child (``credential_env``) is laid over the
filtered environment by the caller, and the emitted ``.tf.json`` still never
carries a credential.

Borrowed: the prefix-plus-name allowlist of ``schedulers/airflow/fluid_apply.py``
and terraform-exec's prohibited list.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from .base import IacProviderPlugin

_LOG = logging.getLogger(__name__)

#: The process basics every ``tofu`` run keeps.
BASE_ENV_NAMES: FrozenSet[str] = frozenset(
    {
        # the process: where programs are, whose home and session it is (a
        # credential_process helper reads the user's keychain), the locale and
        # the temporary directory
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        # where tofu and the providers keep configuration and caches
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        # Windows: the system root (Go's network stack needs it), the profile
        # and application data directories (tofu reads %APPDATA%\tofu.rc)
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERNAME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        # proxies and CA bundles (Go reads SSL_CERT_FILE / SSL_CERT_DIR)
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        # go-plugin: where a provider's socket goes (a long macOS TMPDIR
        # overflows the socket path)
        "PLUGIN_UNIX_SOCKET_DIR",
    }
)

#: OpenTofu's own settings (opentofu.org/docs/cli/config/environment-variables/).
TOFU_ENV_NAMES: FrozenSet[str] = frozenset(
    {
        "TF_LOG",
        "TF_LOG_CORE",
        "TF_LOG_PROVIDER",
        "TF_LOG_PATH",
        "TF_DATA_DIR",
        "TF_WORKSPACE",
        "TF_IN_AUTOMATION",
        "TF_CLI_CONFIG_FILE",
        "TF_PLUGIN_CACHE_DIR",
        "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE",
        "TF_REGISTRY_DISCOVERY_RETRY",
        "TF_REGISTRY_CLIENT_TIMEOUT",
        "TF_PROVIDER_DOWNLOAD_RETRY",
        "TF_STATE_PERSIST_INTERVAL",
        "TF_IGNORE",
        "TF_APPEND_USER_AGENT",
    }
)

#: Registry and module-host credentials (``TF_TOKEN_<host>``) for a private
#: registry or provider mirror.
TOFU_ENV_PREFIXES: Tuple[str, ...] = ("TF_TOKEN_",)

#: The cloud families, declared once: the plugins name them, and so do the state
#: backend and the encryption key provider of the same cloud.
AWS_ENV_PREFIXES: Tuple[str, ...] = ("AWS_",)
#: ``GCE_METADATA_*`` are the metadata server overrides the Google auth library
#: reads (``GCE_METADATA_HOST``, ``GCE_METADATA_IP``, ``GCE_METADATA_ROOT``).
GCP_ENV_PREFIXES: Tuple[str, ...] = ("GOOGLE_", "GCLOUD_", "CLOUDSDK_", "GCE_METADATA_")

#: Names inside a family that no provider reads and that hold a key for
#: something else: model API keys (Gemini, Amazon Bedrock).
EXCLUDED_ENV_NAMES: FrozenSet[str] = frozenset({"GOOGLE_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"})

#: The clouds a state backend block (``terraform.backend``) authenticates with.
_BACKEND_CLOUDS: Dict[str, str] = {"s3": "aws", "gcs": "gcp"}

#: Families by cloud, for the state backend and the encryption key provider.
_CLOUD_ENV: Dict[str, Tuple[str, ...]] = {"aws": AWS_ENV_PREFIXES, "gcp": GCP_ENV_PREFIXES}


def backend_clouds(backend: Optional[Mapping[str, Any]]) -> Tuple[str, ...]:
    """The clouds a ``terraform.backend`` block authenticates with (``()`` for local state)."""
    return tuple(_BACKEND_CLOUDS[kind] for kind in (backend or {}) if kind in _BACKEND_CLOUDS)


def _allowance(
    plugin: Optional[IacProviderPlugin], clouds: Iterable[str]
) -> Tuple[FrozenSet[str], Tuple[str, ...]]:
    names = set(BASE_ENV_NAMES) | set(TOFU_ENV_NAMES)
    prefixes = list(TOFU_ENV_PREFIXES)
    if plugin is not None:
        names.update(getattr(plugin, "credential_env_vars", ()) or ())
        prefixes.extend(getattr(plugin, "credential_env_prefixes", ()) or ())
    for cloud in clouds:
        prefixes.extend(_CLOUD_ENV.get(cloud, ()))
    return frozenset(names), tuple(dict.fromkeys(prefixes))


def allowed_env_name(
    name: str, plugin: Optional[IacProviderPlugin] = None, *, clouds: Iterable[str] = ()
) -> bool:
    """Whether ``name`` reaches a ``tofu`` run of ``plugin`` that also uses ``clouds``."""
    names, prefixes = _allowance(plugin, clouds)
    return _allowed(name, names, prefixes)


def _allowed(name: str, names: FrozenSet[str], prefixes: Tuple[str, ...]) -> bool:
    if name in EXCLUDED_ENV_NAMES:
        return False
    return name in names or any(name.startswith(prefix) for prefix in prefixes)


def build_tofu_env(
    plugin: Optional[IacProviderPlugin] = None,
    *,
    clouds: Iterable[str] = (),
    base: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The environment for a ``tofu`` child process: ``base`` (``os.environ``) filtered.

    ``plugin`` is the run's IaC plugin, ``clouds`` the other clouds the run
    authenticates with (:func:`backend_clouds` of the state backend, the
    encryption key provider's). Callers lay the plugin's ``credential_env``
    overlay, and any value forge resolved for the run, over the result.
    """
    source = os.environ if base is None else base
    names, prefixes = _allowance(plugin, tuple(clouds))
    env = {key: value for key, value in source.items() if _allowed(key, names, prefixes)}
    dropped = sorted(key for key in source if key.startswith("TF_") and key not in env)
    if dropped:
        _LOG.warning(
            json.dumps(
                {
                    "event": "tofu_env_dropped",
                    "names": dropped,
                    "message": (
                        "These OpenTofu variables in the environment do not reach tofu: "
                        "TF_CLI_ARGS* would change every command forge runs, TF_VAR_* "
                        "would set the module's variables, TF_ENCRYPTION would override "
                        "its state encryption (set FLUID_STATE_ENCRYPTION instead)."
                    ),
                }
            )
        )
    return env


def credential_report(
    plugin: IacProviderPlugin, env: Mapping[str, str]
) -> Tuple[List[str], List[str]]:
    """Split a plugin's credential env vars into ``(present, absent)``.

    Informational only: providers also accept file-based credentials
    (``~/.aws/credentials``, GCP ADC), so an absent env var does not by
    itself mean credentials are unavailable.
    """
    present: List[str] = []
    absent: List[str] = []
    for var in getattr(plugin, "credential_env_vars", ()):
        (present if env.get(var) else absent).append(var)
    return present, absent
