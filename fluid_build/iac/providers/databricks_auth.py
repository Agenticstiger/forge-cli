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

"""Which Databricks credential a run uses: the environment, or one named profile.

The databricks/databricks provider, ``fluid verify`` and the doctor all
authenticate through Databricks unified auth (the Go and Python SDKs). Left to
itself it reads the ``DATABRICKS_*`` variables and, when they configure no host
or no credential, the ``DEFAULT`` profile of ``~/.databrickscfg``
(databricks-sdk-go ``config/config_file.go`` skips the file only once a host or
an auth attribute is set). So a run with ``DATABRICKS_HOST`` forgotten deployed
to whatever workspace that profile names, with its token, and a stray
``DATABRICKS_AUTH_TYPE`` or client secret could change which credential went
where.

:func:`resolve_workspace_auth` pins one answer before anything runs:

* ``DATABRICKS_HOST`` names the workspace, always (never a contract, never the
  file), over HTTPS (plain HTTP only to this machine, for a stand-in).
* With no ``DATABRICKS_CONFIG_PROFILE``, the credential is the environment's:
  ``DATABRICKS_TOKEN`` (``pat``) or ``DATABRICKS_CLIENT_ID`` +
  ``DATABRICKS_CLIENT_SECRET`` (``oauth-m2m``), or the method an explicit
  ``DATABRICKS_AUTH_TYPE`` names. The config file is blanked
  (``DATABRICKS_CONFIG_FILE`` = the null device), so no profile is read.
* With ``DATABRICKS_CONFIG_PROFILE``, that profile is the credential, and its
  ``host`` must be ``DATABRICKS_HOST``: a profile for another workspace would
  send its credential there. A laptop's ``databricks auth login`` profile works
  this way. A credential in the environment as well is refused, not merged.
* ``DATABRICKS_AUTH_TYPE`` is set to the method chosen, and the credential
  variables of other methods are blanked for the child, so the SDK cannot pick
  a second one.

Borrowed: the overlay shape of the Snowflake plugin's ``credential_env`` (an
``env.update`` can only add, so a variable is blanked, not removed) and the
pinning rationale of ``copilot/catalog/credentials.py::pin_to_own_material``.
Messages name variables and hosts, never a credential's value.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from ..base import UnsupportedBindingError

HOST_ENV = "DATABRICKS_HOST"
PROFILE_ENV = "DATABRICKS_CONFIG_PROFILE"
CONFIG_FILE_ENV = "DATABRICKS_CONFIG_FILE"
AUTH_TYPE_ENV = "DATABRICKS_AUTH_TYPE"
TOKEN_ENV = "DATABRICKS_TOKEN"
CLIENT_ID_ENV = "DATABRICKS_CLIENT_ID"
CLIENT_SECRET_ENV = "DATABRICKS_CLIENT_SECRET"

#: The credential variables unified auth reads, and the method each belongs to.
_METHOD_VARS = {
    TOKEN_ENV: "pat",
    CLIENT_ID_ENV: "oauth-m2m",
    CLIENT_SECRET_ENV: "oauth-m2m",
    "DATABRICKS_USERNAME": "basic",
    "DATABRICKS_PASSWORD": "basic",
}
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class WorkspaceAuth:
    """The workspace and credential a run uses; ``overlay`` is for the child's env."""

    host: str
    auth_type: str
    profile: Optional[str] = None
    overlay: Dict[str, str] = field(default_factory=dict)


def _refusal(kind: str, message: str, *remediation: str) -> UnsupportedBindingError:
    return UnsupportedBindingError(f"databricks-{kind}", message, tuple(remediation))


def normalize_host(host: str) -> str:
    """``scheme://host[:port]``, lower-cased, no path; a bare host is HTTPS."""
    text = host.strip()
    parts = urlsplit(text if "://" in text else f"https://{text}")
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def _host(env: Mapping[str, str]) -> str:
    raw = str(env.get(HOST_ENV) or "").strip()
    if not raw:
        raise _refusal(
            "host-unset",
            f"{HOST_ENV} is not set. The workspace comes from the environment only; without "
            "it the provider would use the DEFAULT profile of ~/.databrickscfg, whatever "
            "workspace that names.",
            f"Set {HOST_ENV} to the workspace URL, for example "
            "https://<workspace>.cloud.databricks.com.",
        )
    host = normalize_host(raw)
    parts = urlsplit(host)
    if parts.scheme != "https" and (parts.scheme != "http" or parts.hostname not in _LOOPBACK):
        raise _refusal(
            "host-insecure",
            f"{HOST_ENV} is {host}, not an https:// URL: the credential would cross the network "
            "readable by anyone on the path.",
            f"Set {HOST_ENV} to the workspace's https:// URL.",
        )
    return host


def _profile_auth(env: Mapping[str, str], host: str, profile: str) -> WorkspaceAuth:
    in_env = sorted(name for name in _METHOD_VARS if env.get(name))
    if in_env:
        raise _refusal(
            "auth-ambiguous",
            f"{PROFILE_ENV} names profile {profile!r}, and {', '.join(in_env)} is set too: two "
            "credentials for one workspace, and the SDK would merge them.",
            f"Unset {', '.join(in_env)} to use the profile, or unset {PROFILE_ENV} to use the "
            "environment's credential.",
        )
    path = Path(str(env.get(CONFIG_FILE_ENV) or "~/.databrickscfg")).expanduser()
    parser = configparser.RawConfigParser()
    try:
        read = parser.read(path, encoding="utf-8")
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise _refusal(
            "profile-unreadable",
            f"{path} could not be read as a Databricks config file ({type(exc).__name__}).",
            "Fix the file (databricks auth profiles lists what it holds).",
        ) from None
    if not read or not parser.has_section(profile):
        raise _refusal(
            "profile-missing",
            f"{PROFILE_ENV} names profile {profile!r}, which {path} does not have.",
            f"Create it (databricks auth login --host {host} --profile {profile}), or unset "
            f"{PROFILE_ENV}.",
        )
    section = parser[profile]
    named = str(section.get("host") or "").strip()
    if not named or normalize_host(named) != host:
        raise _refusal(
            "profile-host",
            f"profile {profile!r} is for {normalize_host(named) if named else 'no host'}, not "
            f"{HOST_ENV} {host}: its credential would be sent to a workspace it was not made for.",
            f"Point {PROFILE_ENV} at the profile for {host}, or set {HOST_ENV} to the profile's "
            "host.",
        )
    auth_type = str(env.get(AUTH_TYPE_ENV) or section.get("auth_type") or "").strip()
    if not auth_type:
        if section.get("token"):
            auth_type = "pat"
        elif section.get("client_id") and section.get("client_secret"):
            auth_type = "oauth-m2m"
        else:
            raise _refusal(
                "profile-auth",
                f"profile {profile!r} names no auth_type, and holds no token or client "
                "secret to tell it from.",
                "Add auth_type to the profile (pat, oauth-m2m, databricks-cli, azure-cli, ...).",
            )
    return WorkspaceAuth(host, auth_type, profile, {AUTH_TYPE_ENV: auth_type})


def _env_auth(env: Mapping[str, str], host: str) -> WorkspaceAuth:
    token = bool(env.get(TOKEN_ENV))
    client = bool(env.get(CLIENT_ID_ENV)) and bool(env.get(CLIENT_SECRET_ENV))
    explicit = str(env.get(AUTH_TYPE_ENV) or "").strip()
    if explicit:
        missing = {"pat": not token, "oauth-m2m": not client}.get(explicit, False)
        if missing:
            needs = TOKEN_ENV if explicit == "pat" else f"{CLIENT_ID_ENV} and {CLIENT_SECRET_ENV}"
            raise _refusal(
                "credentials-missing",
                f"{AUTH_TYPE_ENV} is {explicit}, and {needs} is not set.",
                f"Set {needs}, or change {AUTH_TYPE_ENV}.",
            )
        auth_type = explicit
    elif token and (env.get(CLIENT_ID_ENV) or env.get(CLIENT_SECRET_ENV)):
        raise _refusal(
            "auth-ambiguous",
            f"both {TOKEN_ENV} and an OAuth client ({CLIENT_ID_ENV} / {CLIENT_SECRET_ENV}) are "
            "set: two credentials for one workspace.",
            f"Unset one of them, or set {AUTH_TYPE_ENV} to pat or oauth-m2m to choose.",
        )
    elif token:
        auth_type = "pat"
    elif client:
        auth_type = "oauth-m2m"
    else:
        raise _refusal(
            "credentials-missing",
            "the environment holds no Databricks credential, and the config file is not read "
            f"unless {PROFILE_ENV} names a profile.",
            f"Set {HOST_ENV} and {TOKEN_ENV} (a personal access token), or {HOST_ENV} with "
            f"{CLIENT_ID_ENV} and {CLIENT_SECRET_ENV} (a service principal's OAuth secret), or "
            f"{PROFILE_ENV} to a profile for {host}, then re-run.",
        )
    overlay = {CONFIG_FILE_ENV: os.devnull, AUTH_TYPE_ENV: auth_type}
    for name, method in _METHOD_VARS.items():
        if env.get(name) and method != auth_type:
            overlay[name] = ""  # an empty value reads as unset to the SDK
    return WorkspaceAuth(host, auth_type, None, overlay)


def resolve_workspace_auth(env: Mapping[str, str]) -> WorkspaceAuth:
    """The workspace and credential for a run in ``env``; :class:`UnsupportedBindingError`
    (``databricks-<kind>``, with a fix-it) when they are missing, ambiguous or unsafe."""
    host = _host(env)
    profile = str(env.get(PROFILE_ENV) or "").strip()
    if profile:
        return _profile_auth(env, host, profile)
    return _env_auth(env, host)


#: The secret-bearing variables and profile keys, for scrubbing by value.
_SECRET_VARS = (TOKEN_ENV, CLIENT_SECRET_ENV, "DATABRICKS_PASSWORD")
_PROFILE_SECRETS = ("token", "client_secret", "password")


def credential_values(env: Mapping[str, str]) -> List[str]:
    """Every credential value a run may use, the environment's and the named
    profile's, so a message can be scrubbed of them by value."""
    values = [str(env[name]) for name in _SECRET_VARS if env.get(name)]
    profile = str(env.get(PROFILE_ENV) or "").strip()
    if not profile:
        return values
    parser = configparser.RawConfigParser()
    try:
        parser.read(
            Path(str(env.get(CONFIG_FILE_ENV) or "~/.databrickscfg")).expanduser(),
            encoding="utf-8",
        )
    except (configparser.Error, UnicodeDecodeError):
        return values
    if parser.has_section(profile):
        section = parser[profile]
        values += [str(section[key]) for key in _PROFILE_SECRETS if section.get(key)]
    return values


def tofu_env_overlay(env: Mapping[str, str]) -> Dict[str, str]:
    """What the ``tofu`` child's environment gets on top of ``env`` (``credential_env``)."""
    return dict(resolve_workspace_auth(env).overlay)


def sdk_config(env: Mapping[str, str]) -> Dict[str, Any]:
    """Keyword arguments for ``databricks.sdk.WorkspaceClient`` that authenticate
    exactly as :func:`resolve_workspace_auth` decided, as the provider does: an
    explicit ``auth_type`` (the SDK then uses that method alone), and no config
    file unless a profile was named."""
    auth = resolve_workspace_auth(env)
    kwargs: Dict[str, Any] = {"host": auth.host, "auth_type": auth.auth_type}
    if auth.profile:
        kwargs["profile"] = auth.profile
        if env.get(CONFIG_FILE_ENV):
            kwargs["config_file"] = str(env[CONFIG_FILE_ENV])
        return kwargs
    kwargs["config_file"] = os.devnull
    if auth.auth_type == "pat":
        kwargs["token"] = str(env[TOKEN_ENV])
    elif auth.auth_type == "oauth-m2m":
        kwargs["client_id"] = str(env[CLIENT_ID_ENV])
        kwargs["client_secret"] = str(env[CLIENT_SECRET_ENV])
    return kwargs


__all__ = [
    "WorkspaceAuth",
    "credential_values",
    "normalize_host",
    "resolve_workspace_auth",
    "sdk_config",
    "tofu_env_overlay",
]
