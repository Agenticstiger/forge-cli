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

"""Cross-mesh federation — digest pinning across separate workspaces.

In a single workspace, ``consumes[].upstreamDigest`` is captured and
verified at apply-time against the live upstream contract — same
process, same filesystem, same registry. When teams cross workspace
boundaries (one team owns SDPs, another consumes them in their own
mesh), the upstream contract lives in another git repo / another
catalog and the local digest check has nothing to verify against.

This module ships the federation primitives:

1. **Federated digest manifest** — a top-level
   ``federation/upstreams.yaml`` that declares known external
   workspaces and their reachability (registry URL, auth method).
2. **Digest fetcher** — :func:`fetch_federated_digest` resolves the
   external workspace's contract for ``productId@version`` and
   returns the canonical bundle digest (cached under
   ``.fluid/federation/<workspace>.digest-cache.json``).
3. **Federated drift validator** — :func:`validate_federated_consumes`
   walks ``consumes[]``, splits into local + federated buckets, and
   delegates each to the right validator. Federated entries get a
   live fetch + comparison; local entries fall through to the
   existing in-workspace validator.

The data model is **declarative**:

.. code-block:: yaml

    # federation/upstreams.yaml
    workspaces:
      - id: telco-billing
        kind: git_registry           # | catalog | http_registry
        endpoint: https://github.com/acme/telco-billing-mesh
        auth:
          mode: github_token         # | basic | oidc | none
          secret_ref: GITHUB_TOKEN   # sent only to GitHub hosts, only over https
        product_path_template: "{product_id}/contract.fluid.yaml"
      - id: marketing-cdp
        kind: catalog
        endpoint: https://catalog.acme.com/api
        auth:
          mode: oidc

The ``git_registry`` / ``catalog`` / ``http_registry`` fetch backends are
implemented (``_fetch_digest_via_git`` — gitpython with a shell-out
fallback; ``_fetch_digest_via_catalog`` / ``_fetch_digest_via_http`` — REST
clients) and tested, alongside the schema + dispatcher + cache layer.
``NotImplementedError`` is now raised only for an unrecognised ``kind`` or
when a live fetch returns no digest (endpoint unreachable / wrong path).

Credentials: ``auth.secret_ref`` names an environment variable. GITHUB_TOKEN
and GH_TOKEN may be sent to GitHub hosts; any other variable (or host) must
be listed in ``FLUID_FEDERATION_SECRET_REF_ALLOWLIST`` as ``NAME@host``
(recommended) or ``NAME``. A credential travels only over https -- git gets
it through its per-process environment, never the URL. Anything outside
that policy raises :class:`FederationCredentialPolicyError` and nothing is
sent. Both policy variables are read from the operator's environment only:
a value set by a project dotenv file is ignored (see
:func:`_operator_policy_env`). The user-facing policy, with examples and
upgrade notes, is ``docs/FEDERATION.md``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

import yaml

# Reuse the canonical SSRF post-DNS-resolution gate rather than
# re-deriving a private-range list here. ``_hostname_is_private``
# resolves the host and returns True for RFC1918, link-local
# (169.254.0.0/16 — AWS/GCP metadata), loopback, unspecified, and
# reserved ranges (and fails closed on DNS errors).
from fluid_build._net import _hostname_is_private

# Imported at module scope, NOT behind a try/except: the canonical
# contract digest is the git backend's whole contract with its peers.
# A guarded import that degraded to a raw-text hash would let two peers
# formatting the same contract differently disagree *silently*, which is
# exactly the drift ``upstreamDigest`` pinning exists to catch. Safe for
# the light-CLI cold path — ``plan_digest`` imports stdlib only.
from fluid_build.forge.core.plan_digest import compute_contract_digest
from fluid_build.util.safe_http import MAX_REMOTE_BYTES
from fluid_build.util.safe_yaml import MAX_YAML_BYTES, UnsafeYamlError, load_yaml_safe

LOG = logging.getLogger("fluid.forge.federation")

FEDERATION_MANIFEST_FILENAME = "federation/upstreams.yaml"
FEDERATION_CACHE_DIRNAME = "federation"

# HTTP-client hardening defaults for every outbound federation fetch.
# ``follow_redirects=False`` is the SSRF-safe default — a public
# endpoint that 30x-redirects to http://169.254.169.254/ must NOT be
# chased silently. Where a redirect is genuinely expected the fetcher
# uses an explicit small cap and re-runs the host gate on each hop,
# drops credential headers on a cross-origin hop and refuses an
# https -> http downgrade (see ``_federation_http_get``).
_FEDERATION_HTTP_TIMEOUT = 15.0

#: Default wall-clock cap on each federated fetch subprocess
#: (git clone/fetch/rev-parse).
_FEDERATION_GIT_TIMEOUT_DEFAULT = 30.0


def _federation_git_timeout() -> float:
    """Per-git-operation timeout for a federated digest fetch.

    ``fluid apply`` reaches another mesh to verify an upstream digest, so
    an unresponsive registry must not be able to hang the apply. The
    clone default used to be 60s per upstream, which across several
    upstreams is minutes of an apply spent waiting on someone else's git
    server.

    Read per call rather than bound at import: a module-level constant
    freezes whatever the environment held when the module first loaded,
    so a value exported later in the process (or by a test) silently
    does nothing while ``fluid doctor --env`` cheerfully reports it as
    active. Same shape as ``iac/runner.py``'s tofu timeout.

    Falls back to the default on a non-numeric or non-positive value --
    a typo in an env var must not disable the bound it configures.
    """
    raw = os.environ.get("FLUID_FEDERATION_TIMEOUT_SECONDS")
    if raw is None:
        return _FEDERATION_GIT_TIMEOUT_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        LOG.warning(
            "federation_timeout_invalid: FLUID_FEDERATION_TIMEOUT_SECONDS=%r "
            "is not a number; using the %ss default",
            raw,
            _FEDERATION_GIT_TIMEOUT_DEFAULT,
        )
        return _FEDERATION_GIT_TIMEOUT_DEFAULT
    if not math.isfinite(value) or value <= 0:
        # ``float()`` happily accepts "inf" and "nan". Both defeat the
        # bound in opposite ways: ``timeout=inf`` never fires, and
        # ``timeout=nan`` makes subprocess raise before git even starts.
        # ``value <= 0`` alone catches neither (nan compares False to
        # everything, inf is positive).
        LOG.warning(
            "federation_timeout_invalid: FLUID_FEDERATION_TIMEOUT_SECONDS=%r "
            "must be a positive, finite number of seconds; using the %ss default",
            raw,
            _FEDERATION_GIT_TIMEOUT_DEFAULT,
        )
        return _FEDERATION_GIT_TIMEOUT_DEFAULT
    return value


_FEDERATION_MAX_REDIRECTS = 3

# Opt-in allow-list for operators whose federation endpoint genuinely
# lives on an internal/private address (a corporate registry behind a
# VPN, a self-hosted catalog on an RFC1918 host). Comma-separated host
# suffixes; empty/unset → default-deny private destinations.
_FEDERATION_HOST_ALLOWLIST_ENV = "FLUID_FEDERATION_HOST_ALLOWLIST"

# Which environment variables a manifest ``auth.secret_ref`` may name, and
# where each may be sent. ``federation/upstreams.yaml`` is a committed file,
# so the variable it names and the host it sends it to are both whatever the
# last merged change says. GITHUB_TOKEN / GH_TOKEN stay allowed by default
# but only toward GitHub hosts; every other variable must be listed by the
# operator in this env var -- ``NAME`` (any host) or, recommended,
# ``NAME@host`` (that hostname only). Comma-separated.
_FEDERATION_CREDENTIAL_REF_ALLOWLIST_ENV = "FLUID_FEDERATION_SECRET_REF_ALLOWLIST"
_GITHUB_HOSTS: frozenset[str] = frozenset(
    {"github.com", "api.github.com", "raw.githubusercontent.com"}
)
_DEFAULT_SECRET_REF_HOSTS: Mapping[str, frozenset[str]] = {
    "GITHUB_TOKEN": _GITHUB_HOSTS,
    "GH_TOKEN": _GITHUB_HOSTS,
}
# An allow-list host written with a port: ``name:port``, ``v4:port`` or
# ``[v6]:port`` (bracketed, as in a URL), with or without the ``user@`` part
# of a netloc it was copied from. Hosts are compared with
# ``urlparse(url).hostname``, which carries no port, no brackets and no
# userinfo, so such an entry never matches. It stays that way (fail closed)
# and is warned about once. A bare IPv6 address (``fd00::5``) has colons but
# no port, and the unbracketed branch, which admits no colon, never matches it.
_ALLOWLIST_HOST_WITH_PORT_RE = re.compile(r"^(?:.*@)?(?:\[[^\]]+\]|[^:\[\]]+):\d+$")
#: SHA-256 digests of the allow-list entries already warned about for
#: carrying a port. A digest, not the entry: an entry can hold a password.
_ALLOWLIST_PORT_WARNED: Set[str] = set()
# A credential only travels over TLS. ssh endpoints never carry the token
# (deploy keys authenticate there), so they are neither refused nor sent it.
_CREDENTIAL_SCHEMES: frozenset[str] = frozenset({"https", "git+https"})
_SSH_SCHEMES: frozenset[str] = frozenset({"ssh", "git+ssh"})

# Both policy variables above belong to the operator. ``fluid apply`` (and
# ``publish`` / ``verify``) first hydrate ``os.environ`` from the project's
# dotenv files -- ``.env``, ``.env.<env>``, ``.env.local`` -- and from
# ``FLUID_SECRETS_FILE``, and those files are committed next to
# ``federation/upstreams.yaml``. A change to the checkout could otherwise set
# the policy that is meant to constrain it. See :func:`_operator_policy_env`.
_OPERATOR_ENV_FILE_VAR = "FLUID_SECRETS_FILE"
#: Directories whose dotenv files may have been hydrated, besides the
#: working directory: the ``workspace_root`` of the fetch in progress.
_POLICY_PROJECT_ROOTS: ContextVar[Tuple[Path, ...]] = ContextVar(
    "fluid_federation_policy_project_roots", default=()
)
#: ``(variable, files)`` pairs already warned about, so one ignored value is
#: logged once per process rather than once per request.
_POLICY_WARNED: Set[Tuple[str, Tuple[str, ...]]] = set()

# ``workspace.id`` is interpolated into a filesystem path
# (``~/.cache/fluid/federation-git/<id>`` and the digest-cache file).
# Constrain it to a conservative slug so a tampered manifest row can't
# smuggle ``../`` traversal or absolute-path segments into those sinks.
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class FederationFetchError(NotImplementedError):
    """A federated digest fetch ran but came back with nothing.

    Distinct from a plain :class:`NotImplementedError`, which means the
    workspace ``kind`` has no fetcher at all. The two were previously the
    same exception, so a partner mesh being *down* was reported to
    operators as "fetcher for kind='http_registry' not yet wired" -- a
    statement about missing FLUID code, sending them to read our source
    instead of checking their own registry.

    All three shipped backends return ``None`` rather than raising on a
    timeout, 404, TLS error, auth rejection, SSRF block or failed clone,
    so this is the exception a real outage actually produces -- the
    common case, not the edge case.

    Subclasses ``NotImplementedError`` deliberately: callers written
    while the two were conflated still catch it.
    """


class FederationSsrfError(ValueError):
    """Raised when a federation endpoint host fails the SSRF guard."""


class FederationCredentialPolicyError(ValueError):
    """A workspace's ``auth.secret_ref`` would be sent somewhere it may not go.

    Raised before any request or git process starts, so the credential is
    never sent. Two causes: the variable is not permitted for the endpoint
    host (see ``FLUID_FEDERATION_SECRET_REF_ALLOWLIST``), or the endpoint is
    not https and the credential would travel in cleartext. The message
    names the variable and the fix; it never contains the value.
    """


@contextlib.contextmanager
def _policy_project_root(root: Optional[Path]) -> Iterator[None]:
    """Also treat ``root``'s dotenv files as project files while the block runs."""
    if root is None:
        yield
        return
    token = _POLICY_PROJECT_ROOTS.set(_POLICY_PROJECT_ROOTS.get() + (Path(root),))
    try:
        yield
    finally:
        _POLICY_PROJECT_ROOTS.reset(token)


def _dotenv_keys(path: Path) -> frozenset[str]:
    """Keys ``path`` gives a value, parsed as the dotenv store parses them.

    A key with no value or a blank one (``KEY=``, as a template lists it)
    cannot have put a usable value into ``os.environ``, so it is left out.
    The raw text is read without interpolation, so ``KEY=${OTHER}`` counts.
    Unreadable or unparsable files define nothing: the store skips them the
    same way, so nothing from them reached ``os.environ``.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - python-dotenv is a core dependency
        return frozenset()
    try:
        values = dotenv_values(path, interpolate=False)
    except Exception:  # noqa: BLE001 - mirror the store: a bad file is skipped
        return frozenset()
    return frozenset(key for key, raw in values.items() if raw and raw.strip())


def _dotenv_store_paths(root: Path, environment: str) -> Tuple[Path, Path, Path]:
    """The paths ``DotEnvCredentialStore(root, environment).load()`` opens, in order.

    Kept identical to the store's list (a test pins the two together). Which
    file is "a project dotenv file" is decided by these paths and
    :meth:`Path.is_file`, i.e. by what ``open()`` resolves them to, never by
    matching directory-entry names: on a case-insensitive filesystem the store
    reads ``.Env`` when it opens ``.env``.
    """
    return (root / ".env", root / f".env.{environment}", root / ".env.local")


def _project_dotenv_files(root: Path) -> List[Path]:
    """Every existing file the dotenv store could open under ``root``.

    The environment name of the hydrating command is not known here, so every
    name a ``.env.<name>`` entry in ``root`` spells (compared without regard to
    case) is tried, plus ``FLUID_ENV`` and the store's default. A file reached
    by more than one spelling is listed once.
    """
    environments: List[str] = [os.environ.get("FLUID_ENV") or "dev"]
    with contextlib.suppress(OSError):
        for entry in sorted(os.listdir(root)):
            if entry.casefold().startswith(".env.") and len(entry) > len(".env."):
                environments.append(entry[len(".env.") :])
    files: List[Path] = []
    identities: Set[Tuple[int, int]] = set()
    for environment in environments:
        for path in _dotenv_store_paths(root, environment):
            try:
                if not path.is_file():
                    continue
                st = path.stat()
            except (OSError, ValueError):
                continue
            identity = (st.st_dev, st.st_ino)
            if identity in identities:
                continue
            identities.add(identity)
            files.append(path)
    return files


def _project_files_setting(name: str) -> List[Path]:
    """Project files that could have put ``name`` into ``os.environ``.

    The project roots are the working directory (``fluid apply`` and
    ``publish`` hydrate ``Path.cwd()``) and the ``workspace_root`` of the
    fetch in progress. Every file the dotenv store could open there counts
    (:func:`_project_dotenv_files`), not just the ones a given ``--env``
    loads: the environment name is not known here, and a file that is never
    loaded only costs a warning. When a project file sets
    ``FLUID_SECRETS_FILE``, the secrets file it may have selected counts too,
    because hydration loads it with override.
    """
    roots: List[Path] = list(_POLICY_PROJECT_ROOTS.get())
    with contextlib.suppress(OSError):
        roots.append(Path.cwd())
    seen: Set[Path] = set()
    hits: List[Path] = []
    project_picks_secrets_file = False
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        for path in _project_dotenv_files(resolved):
            keys = _dotenv_keys(path)
            if name in keys:
                hits.append(path)
            if _OPERATOR_ENV_FILE_VAR in keys:
                project_picks_secrets_file = True
    secrets_file = os.environ.get(_OPERATOR_ENV_FILE_VAR)
    if project_picks_secrets_file and secrets_file:
        path = Path(secrets_file).expanduser()
        if path.is_file() and name in _dotenv_keys(path):
            hits.append(path)
    return hits


def _operator_policy_env(name: str) -> Tuple[str, List[Path]]:
    """Read a federation policy variable the operator set; ``("", files)``
    when a project file may have set it instead.

    Returns ``(value, ignored_from)``. The value is honoured unless a project
    dotenv file defines the same variable (or picks the ``FLUID_SECRETS_FILE``
    that does), in which case it is ignored -- fail closed, whatever the
    value -- and a warning names the files. Matching on the key rather than
    the value keeps dotenv interpolation from making the two differ. The
    operator sets these in the shell or CI environment, or in a
    ``FLUID_SECRETS_FILE`` the operator chose.
    """
    value = os.environ.get(name, "")
    if not value.strip():
        return "", []
    files = _project_files_setting(name)
    if not files:
        return value, []
    key = (name, tuple(str(f) for f in files))
    if key not in _POLICY_WARNED:
        _POLICY_WARNED.add(key)
        LOG.warning(
            "federation_policy_env_ignored: %s is set by project file(s) %s, which are "
            "part of the checkout it constrains. It is ignored; set it in the operator "
            "environment (shell, CI secret, or a FLUID_SECRETS_FILE outside the project) "
            "and remove it from those files.",
            name,
            ", ".join(str(f) for f in files),
        )
    return "", files


def _policy_ignored_note(name: str) -> str:
    """Refusal-message suffix naming the project files that set ``name``, or ``""``.

    Decided from the files themselves, not from ``os.environ``: hydration
    that protects operator variables never copies a project value of ``name``
    into the environment, and the operator still needs to learn that the
    project file was not honoured. The value is never read into the message.
    """
    files = _project_files_setting(name)
    if not files:
        return ""
    return (
        f" {name} in project file(s) {', '.join(f.name for f in files)} is ignored; "
        "it must come from the operator environment (shell, CI, or a "
        f"{_OPERATOR_ENV_FILE_VAR} outside the project), not the checkout."
    )


def _federation_host_on_allowlist(hostname: str) -> bool:
    """Return True when ``FLUID_FEDERATION_HOST_ALLOWLIST`` is set and
    ``hostname`` matches one of its comma-separated suffixes exactly or
    as a dotted suffix. Empty/unset → False (no allow-list configured).
    A value set by a project dotenv file counts as unset.
    """
    raw, _ignored = _operator_policy_env(_FEDERATION_HOST_ALLOWLIST_ENV)
    suffixes = [s.strip() for s in raw.split(",") if s.strip()]
    if not suffixes:
        return False
    return any(hostname == s or hostname.endswith("." + s) for s in suffixes)


def _guard_federation_url(url: str, *, workspace_id: str) -> str:
    """Raise :class:`FederationSsrfError` when ``url``'s host resolves to
    a private/link-local/loopback/metadata address; return ``url`` on
    success.

    The allow-list (``FLUID_FEDERATION_HOST_ALLOWLIST``) trumps the IP
    check so operators with a genuine internal endpoint can opt in.
    DNS resolution failures fail closed (``_hostname_is_private``
    returns True for unresolvable hosts).
    """
    host = urlparse(url).hostname
    if not host:
        raise FederationSsrfError(
            f"federation workspace {workspace_id!r}: endpoint has no resolvable host"
        )
    if _federation_host_on_allowlist(host):
        return url
    if _hostname_is_private(host):
        raise FederationSsrfError(
            f"federation workspace {workspace_id!r}: endpoint host {host!r} "
            "resolves to a private/loopback/link-local/cloud-metadata address. "
            "Refusing the outbound request to prevent SSRF (metadata exfil, "
            f"internal-service abuse). Set {_FEDERATION_HOST_ALLOWLIST_ENV}="
            "<host-suffix> to allow a genuinely-internal federation endpoint."
            + _policy_ignored_note(_FEDERATION_HOST_ALLOWLIST_ENV)
        )
    return url


# Allow-list of URL schemes accepted on ``workspaces[].endpoint``.
# This is the security boundary that prevents argument-injection at the
# ``git clone`` / ``Repo.clone_from`` sink. An attacker-authored manifest
# entry like ``endpoint: "--upload-pack=touch /tmp/pwn"`` would otherwise
# be passed verbatim as a positional URL to git, which interprets ``--``-
# prefixed values as options (the well-known argument-injection pattern
# behind CVE-2017-1000117 / CVE-2018-17456 / gitpython CVE-2022-24439).
# We reject anything that doesn't have one of these schemes here, BEFORE
# the value reaches the subprocess argv.
_FEDERATION_ALLOWED_SCHEMES: frozenset = frozenset(
    {
        "https",
        "http",
        "ssh",
        "git",
        "git+ssh",
        "git+https",
    }
)


def _validate_federation_endpoint(endpoint: str, *, workspace_id: str) -> str:
    """Validate ``endpoint`` is a safe URL before storing on a workspace.

    Returns the trimmed value unchanged on success; raises ``ValueError``
    otherwise. Two checks:

    1. The value must NOT start with ``-``. Even if a future scheme
       check missed something, the leading-dash check forces git's
       argv parser to treat the value as a positional URL, not an
       option, when we forget to pass ``--`` at the call site.
    2. The URL scheme must be in :data:`_FEDERATION_ALLOWED_SCHEMES`.
       The ``ssh://`` and ``git+ssh://`` variants are accepted because
       deploy-key auth still flows through ``ssh://git@host:org/repo``.
       The ``scp``-style ``user@host:path`` form (no scheme prefix) is
       NOT accepted by this validator — operators on that path should
       rewrite to explicit ``ssh://`` for clarity and for this safety
       check to engage.

    Note that this is a complement to, not a substitute for, the
    ``--`` separator in the subprocess invocation — defence-in-depth.
    """
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError(f"federation workspace {workspace_id!r}: endpoint is required")
    value = endpoint.strip()
    if value.startswith("-"):
        # Explicit reject: no URL scheme begins with ``-``. This catches
        # the argument-injection payloads (``--upload-pack=...``,
        # ``--config=...``) before they reach git's argv.
        raise ValueError(
            f"federation workspace {workspace_id!r}: endpoint must not start "
            f"with '-' (got {value!r})"
        )
    # Parse the scheme manually rather than via urllib.parse.urlsplit
    # because we want a strict ``scheme://`` check, not a forgiving one.
    scheme_match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)://", value)
    if not scheme_match:
        raise ValueError(
            f"federation workspace {workspace_id!r}: endpoint must use an "
            f"explicit URL scheme (got {value!r}); allowed schemes: "
            f"{sorted(_FEDERATION_ALLOWED_SCHEMES)}"
        )
    scheme = scheme_match.group(1).lower()
    if scheme not in _FEDERATION_ALLOWED_SCHEMES:
        raise ValueError(
            f"federation workspace {workspace_id!r}: endpoint scheme "
            f"{scheme!r} not allowed (got {value!r}); allowed schemes: "
            f"{sorted(_FEDERATION_ALLOWED_SCHEMES)}"
        )
    return value


@dataclass(frozen=True)
class FederatedWorkspace:
    """One declared external workspace.

    ``kind`` is the integration type:

    * ``git_registry`` — workspace lives in a git repo; we ``git
      ls-tree`` for the contract path and read the manifest blob.
    * ``catalog`` — workspace publishes contracts to a Datamesh
      Manager / Collibra / DataHub catalog; we hit the API.
    * ``http_registry`` — bare HTTP endpoint that returns the YAML.

    Security: ``endpoint`` is validated by
    :func:`_validate_federation_endpoint` at construction time so
    attacker-authored manifest entries (the file is committed to the
    repo and merged via PR) cannot smuggle ``--upload-pack=...`` or
    similar option-shaped values into the ``git clone`` argv.
    """

    id: str
    kind: str
    endpoint: str
    auth_mode: str = "none"
    auth_secret_ref: Optional[str] = None
    product_path_template: str = "{product_id}/contract.fluid.yaml"

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FederatedWorkspace":
        auth = d.get("auth") or {}
        ws_id = str(d["id"])
        # ``id`` is interpolated into filesystem paths (the git clone
        # cache dir and the per-workspace digest-cache filename). Reject
        # anything outside a conservative slug so a tampered manifest
        # row cannot smuggle ``../`` traversal or absolute segments into
        # those path sinks.
        if not _WORKSPACE_ID_RE.match(ws_id):
            raise ValueError(
                f"federation workspace id {ws_id!r} is invalid: must match "
                f"{_WORKSPACE_ID_RE.pattern} (alphanumerics, dot, dash, "
                "underscore; 1-64 chars). It is used as a filesystem path "
                "component, so traversal/absolute segments are refused."
            )
        endpoint = _validate_federation_endpoint(str(d["endpoint"]), workspace_id=ws_id)
        return cls(
            id=ws_id,
            kind=str(d["kind"]),
            endpoint=endpoint,
            auth_mode=str(auth.get("mode", "none")),
            auth_secret_ref=auth.get("secret_ref"),
            product_path_template=str(
                d.get("product_path_template", "{product_id}/contract.fluid.yaml")
            ),
        )


@dataclass
class FederationManifest:
    """Parsed ``federation/upstreams.yaml`` payload."""

    workspaces: List[FederatedWorkspace] = field(default_factory=list)

    def get(self, workspace_id: str) -> Optional[FederatedWorkspace]:
        for ws in self.workspaces:
            if ws.id == workspace_id:
                return ws
        return None


def load_federation_manifest(workspace_root: Path) -> FederationManifest:
    """Read + parse ``federation/upstreams.yaml`` from a workspace.

    Returns an empty manifest when the file is absent — federation is
    opt-in, not required for single-workspace operation.
    """
    path = workspace_root / FEDERATION_MANIFEST_FILENAME
    if not path.is_file():
        return FederationManifest()
    try:
        with path.open(encoding="utf-8") as f:
            doc = load_yaml_safe(f) or {}
    except (OSError, yaml.YAMLError, UnsafeYamlError) as exc:
        LOG.warning("federation_manifest_unreadable: path=%s error=%s", path, exc)
        return FederationManifest()
    rows = doc.get("workspaces", [])
    if not isinstance(rows, list):
        return FederationManifest()
    workspaces: List[FederatedWorkspace] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            workspaces.append(FederatedWorkspace.from_dict(row))
        except (KeyError, ValueError) as exc:
            # Per-workspace parse / validation error — skip the entry
            # but keep loading the rest. ``ValueError`` here is
            # specifically the security-boundary endpoint check
            # (:func:`_validate_federation_endpoint`); a single
            # tampered row must not silently disable federation for
            # legitimate workspaces declared alongside it.
            LOG.warning(
                "federation_workspace_rejected: id=%s error=%s",
                (row.get("id") if isinstance(row, Mapping) else "?"),
                exc,
            )
    return FederationManifest(workspaces=workspaces)


def _cache_path(workspace_root: Path, workspace_id: str) -> Path:
    return (
        workspace_root / ".fluid" / FEDERATION_CACHE_DIRNAME / f"{workspace_id}.digest-cache.json"
    )


def _read_cache(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover
        # CodeQL py/clear-text-logging-sensitive-data: federation cache
        # payloads contain upstream contract digests + workspace IDs, but
        # the surrounding manifest carries `auth.secret_ref` which points
        # at credentials. Don't include `exc` content — log only the class.
        LOG.warning(
            "federation_cache_write_failed: path=%s error=%s",
            path,
            type(exc).__name__,
        )


def get_cached_digest(
    workspace_root: Path,
    workspace_id: str,
    product_id: str,
    version: str,
) -> Optional[str]:
    """Return the cached digest for ``product_id@version`` from
    ``workspace_id``, or ``None`` when the cache is empty / stale."""
    cache = _read_cache(_cache_path(workspace_root, workspace_id))
    return cache.get(f"{product_id}@{version}")


def store_cached_digest(
    workspace_root: Path,
    workspace_id: str,
    product_id: str,
    version: str,
    digest: str,
) -> None:
    """Persist a freshly-fetched digest into the per-workspace cache."""
    path = _cache_path(workspace_root, workspace_id)
    cache = _read_cache(path)
    cache[f"{product_id}@{version}"] = digest
    cache["__updated_at"] = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    _write_cache(path, cache)


def fetch_federated_digest(
    workspace: FederatedWorkspace,
    product_id: str,
    version: str = "1",
    *,
    use_cache: bool = True,
    workspace_root: Optional[Path] = None,
) -> str:
    """Fetch the canonical bundle digest for ``product_id`` from a
    federated upstream workspace.

    Resolution order:

    1. **Cache hit** (when ``use_cache=True`` and a workspace_root is
       supplied) — return the cached digest immediately.
    2. **Live fetch** via the workspace's declared ``kind``:

       * ``git_registry`` — clone-and-read or ls-tree.
       * ``catalog`` — REST GET against the catalog API.
       * ``http_registry`` — plain HTTP GET.

    3. **Persist to cache** so subsequent applies in the same
       workspace short-circuit.

    .. note::

        The live-fetch backends are intentionally stubbed in this
        skeleton — wiring in concrete clients (gitpython,
        ``requests`` / ``httpx``) is a per-integration follow-up.
        The function shape, error contract, and cache layer are
        production-ready.
    """
    if use_cache and workspace_root is not None:
        cached = get_cached_digest(workspace_root, workspace.id, product_id, version)
        if cached:
            LOG.debug(
                "federation_cache_hit: workspace=%s product=%s@%s",
                workspace.id,
                product_id,
                version,
            )
            return cached

    # Dispatch by ``kind`` to the right live-fetch backend.
    kind = (workspace.kind or "").lower()
    fetched: Optional[str] = None
    # The workspace root's dotenv files were hydrated too, so they count as
    # project files when the policy variables are read.
    with _policy_project_root(workspace_root):
        if kind == "git_registry":
            fetched = _fetch_digest_via_git(workspace, product_id, version)
        elif kind == "catalog":
            fetched = _fetch_digest_via_catalog(workspace, product_id, version)
        elif kind == "http_registry":
            fetched = _fetch_digest_via_http(workspace, product_id, version)
        else:
            raise NotImplementedError(
                f"Federated digest fetch for kind={workspace.kind!r} is not "
                f"a recognised backend. Supported: git_registry, catalog, "
                f"http_registry. (workspace={workspace.id}, product={product_id})"
            )

    if fetched is None:
        raise FederationFetchError(
            f"Live fetch returned no digest for kind={kind} "
            f"(workspace={workspace.id}, product={product_id}@{version}). "
            f"Check endpoint reachability and product path."
        )

    # Persist to cache so subsequent applies short-circuit.
    if workspace_root is not None:
        try:
            store_cached_digest(workspace_root, workspace.id, product_id, version, fetched)
        except Exception as exc:  # pragma: no cover — defensive
            LOG.debug(
                "federation_cache_write_failed: workspace=%s error=%s",
                workspace.id,
                exc,
            )
    return fetched


# ── live-fetch backends ────────────────────────────────────────────────


def _read_secret_value(secret_ref: Optional[str]) -> Optional[str]:
    """Resolve a manifest ``secret_ref`` against the environment.

    Token storage follows the existing pattern used by build runners:
    the manifest names an env var (``GH_TOKEN``, ``CATALOG_TOKEN``)
    and the value is read from ``os.environ`` at fetch time. Empty /
    missing env vars return None so the auth header is omitted.
    """
    if not secret_ref:
        return None
    val = os.environ.get(secret_ref)
    return val.strip() if val else None


def _secret_ref_allowlist() -> List[Tuple[str, Optional[str]]]:
    """Parse ``FLUID_FEDERATION_SECRET_REF_ALLOWLIST`` into ``(name, host)``
    pairs; ``host`` is ``None`` for a bare ``NAME`` entry (any host).

    Read per call, like the other federation env knobs, so a value exported
    after import takes effect. A value set by a project dotenv file counts
    as unset (:func:`_operator_policy_env`).

    A host written with a port (``NAME@host:port``) is kept as written, so it
    never matches, and a warning gives the form that does
    (:func:`_warn_allowlist_entry_has_port`).
    """
    value, _ignored = _operator_policy_env(_FEDERATION_CREDENTIAL_REF_ALLOWLIST_ENV)
    entries: List[Tuple[str, Optional[str]]] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        name, sep, host = item.partition("@")
        name = name.strip()
        if not name:
            continue
        host = host.strip()
        if _ALLOWLIST_HOST_WITH_PORT_RE.match(host):
            # surrogatepass: os.environ decodes undecodable bytes to lone
            # surrogates (PEP 383), which strict UTF-8 cannot encode.
            digest = hashlib.sha256(item.encode("utf-8", "surrogatepass")).hexdigest()
            if digest not in _ALLOWLIST_PORT_WARNED:
                _ALLOWLIST_PORT_WARNED.add(digest)
                _warn_allowlist_entry_has_port(host)
        entries.append((name, host.lower() if sep else None))
    return entries


def _warn_allowlist_entry_has_port(host: str) -> None:
    """Warn that an allow-list entry, whose text after its first ``@`` is
    ``host``, never matches because ``host`` has a port, and suggest the form
    ``NAME@<host>`` that does.

    No part of the entry is repeated, not even the variable's name. An entry
    copied from a URL can hold a user name, a password or a token, before or
    after the variable's name, inside brackets or split by a ``,`` into two
    entries, and no parse of a malformed entry can tell which of its parts is
    secret. The one thing taken from it is the suggested host
    (:func:`_allowlist_suggested_host`), which is a hostname or the
    placeholder ``<host>``.
    """
    LOG.warning(
        "federation_secret_ref_allowlist_entry_has_port: %s has an entry with a port, "
        "and it never matches, because hosts are compared without a port. The entry is "
        "not shown: copied from a URL, it can hold a password. To match, write it as %r, "
        "with the variable's name for NAME and the endpoint's hostname after the '@'.",
        _FEDERATION_CREDENTIAL_REF_ALLOWLIST_ENV,
        f"NAME@{_allowlist_suggested_host(host)}",
    )


def _allowlist_suggested_host(host: str) -> str:
    """The host to suggest for an allow-list ``host`` written with a port, or the
    placeholder ``<host>``.

    Parsed the way the endpoint side is (``urlparse(url).hostname``, in
    :func:`_resolve_federation_credential`), so the suggestion is exactly what
    an endpoint on that host is compared by: no port, no IPv6 brackets, and no
    ``user@`` when the entry was copied from a netloc that carries one.

    Only the text after the last ``@`` is parsed, as CPython's own
    ``urllib.parse`` takes a netloc's host (``netloc.rpartition('@')`` in
    ``_NetlocResultMixinStr._hostinfo``). Parsing the whole of
    ``user/rest:password@host:port`` would not do: an authority ends at the
    first ``/``, ``?`` or ``#`` (RFC 3986, section 3.2), so the hostname would
    be the start of the userinfo.

    The hostname is used only when it holds no ``@``, and no ``:`` or
    brackets around it unless it is an IPv6 address, the one host a URL
    writes in brackets (RFC 3986, section 3.2.2). Anything else, such as
    userinfo inside hand-written brackets (``[user:password@host]:port``,
    which ``urlparse`` rejects), or a bracketed name that is no IPv6 address
    (which Python 3.10's ``urlparse`` returns as the hostname and 3.11+
    rejects), gives ``<host>``.
    """
    bare = host.rpartition("@")[2]
    try:
        hostname = urlparse(f"//{bare}").hostname
    except ValueError:  # unbalanced brackets, or a bracketed host that is not IPv6
        return "<host>"
    if not hostname or "@" in hostname:
        return "<host>"
    if ":" in hostname or "[" in bare:
        try:
            # https://docs.python.org/3/library/ipaddress.html#ipaddress.IPv6Address
            ipaddress.IPv6Address(hostname)
        except ValueError:
            return "<host>"
    return hostname


def _secret_ref_permitted(name: str, host: str) -> bool:
    """True when env var ``name`` may be sent to ``host`` (lower-cased)."""
    if host in _DEFAULT_SECRET_REF_HOSTS.get(name, frozenset()):
        return True
    return any(
        entry_name == name and (entry_host is None or entry_host == host)
        for entry_name, entry_host in _secret_ref_allowlist()
    )


def _resolve_federation_credential(
    workspace: FederatedWorkspace, *, url: str, modes: frozenset[str]
) -> Optional[str]:
    """Return the credential to attach to a request for ``url``, or ``None``.

    ``None`` when the workspace's auth mode attaches nothing for this backend
    (``modes``), no ``secret_ref`` is set, the endpoint is ssh, or the named
    variable is empty. Raises :class:`FederationCredentialPolicyError` --
    before anything is sent -- when ``url`` is not https, or the variable may
    not go to ``url``'s host (checked in that order). Both checks run on the
    *configuration*, whether or not the variable is set here, so a manifest
    that would send a credential somewhere it may not go fails the same way
    on a laptop and in CI.

    A resolved value is registered with the global log redactor so no later
    log line, traceback or published artifact can carry it.
    """
    mode = (workspace.auth_mode or "none").lower()
    ref = workspace.auth_secret_ref
    if not ref or mode not in modes:
        return None
    name = str(ref)
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in _SSH_SCHEMES:
        return None
    host = (parsed.hostname or "").lower()
    # Scheme first: no allow-list entry can make a cleartext endpoint carry a
    # credential, so the refusal must not suggest adding one.
    if scheme not in _CREDENTIAL_SCHEMES:
        raise FederationCredentialPolicyError(
            f"federation workspace {workspace.id!r}: endpoint scheme {scheme!r} would send "
            f"the {name!r} credential in cleartext. Use an https:// endpoint (ssh:// for "
            "git), or remove auth.secret_ref to fetch anonymously. The credential was "
            "not sent."
        )
    if not _secret_ref_permitted(name, host):
        raise FederationCredentialPolicyError(
            f"federation workspace {workspace.id!r}: auth.secret_ref {name!r} may not be "
            f"sent to host {host!r}. GITHUB_TOKEN and GH_TOKEN are sent only to "
            f"{', '.join(sorted(_GITHUB_HOSTS))}; any other variable or host must be "
            f"listed in {_FEDERATION_CREDENTIAL_REF_ALLOWLIST_ENV} (recommended form: "
            f"{name}@{host or '<host>'}). The credential was not sent."
            + _policy_ignored_note(_FEDERATION_CREDENTIAL_REF_ALLOWLIST_ENV)
        )
    secret = _read_secret_value(name)
    if secret:
        from fluid_build.observability.secret_redactor import register_secret

        register_secret(secret)
    return secret


#: Auth modes that attach the secret, per backend (unchanged from before the
#: credential policy; it only decides *whether* the value may go out).
_HTTP_CREDENTIAL_MODES: frozenset[str] = frozenset(
    {"basic", "oidc", "bearer", "http_token", "token"}
)
_CATALOG_CREDENTIAL_MODES: frozenset[str] = frozenset(
    {"oidc", "bearer", "http_token", "token", "catalog_token"}
)
_GIT_CREDENTIAL_MODES: frozenset[str] = frozenset({"github_token", "http_token", "basic"})


def _fetch_digest_via_http(
    workspace: FederatedWorkspace, product_id: str, version: str
) -> Optional[str]:
    """HTTP-registry backend.

    Convention: GET ``<endpoint>/<product_id>/<version>/digest`` returns
    the canonical bundle digest as plaintext (``sha256:...``). The
    auth header is built from ``workspace.auth_mode``:

    * ``basic`` — ``Authorization: Basic <secret>`` (secret already
      base64-encoded by the operator).
    * ``oidc`` / ``bearer`` / ``http_token`` — ``Authorization:
      Bearer <secret>``.
    * ``none`` / unset — no Authorization header.

    Security: the credential goes through
    :func:`_resolve_federation_credential` (secret_ref policy + https only;
    raises :class:`FederationCredentialPolicyError` before anything is
    sent). The request uses :func:`_federation_http_get`, which follows at
    most a few redirects by hand, re-runs the SSRF guard on every hop, drops
    credential headers on a cross-origin hop and refuses an https -> http
    downgrade. On HTTP error only the *host* is logged — pre-auth tokens can
    sit in the URL path.
    """
    base = workspace.endpoint.rstrip("/")
    url = f"{base}/{product_id}/{version}/digest"

    headers = {"Accept": "text/plain"}
    secret = _resolve_federation_credential(workspace, url=url, modes=_HTTP_CREDENTIAL_MODES)
    mode = (workspace.auth_mode or "none").lower()
    if secret:
        if mode == "basic":
            headers["Authorization"] = f"Basic {secret}"
        else:
            headers["Authorization"] = f"Bearer {secret}"

    body = _federation_http_get(url, headers=headers, workspace=workspace, expect_json=False)
    if body is None:
        return None
    body = str(body).strip()

    if not body or not body.startswith("sha"):
        LOG.warning(
            "federation_http_fetch_unexpected_body: workspace=%s body_prefix=%r",
            workspace.id,
            body[:64],
        )
        return None
    return body


def _fetch_digest_via_catalog(
    workspace: FederatedWorkspace, product_id: str, version: str
) -> Optional[str]:
    """Catalog-API backend.

    Convention: GET ``<endpoint>/products/<product_id>/versions/<version>``
    returns JSON ``{"digest": "sha256:..."}``. Same auth shape as
    ``_fetch_digest_via_http``.

    Security: same credential policy, SSRF guard and redirect handling as
    :func:`_fetch_digest_via_http` (see :func:`_federation_http_get`).
    """
    base = workspace.endpoint.rstrip("/")
    url = f"{base}/products/{product_id}/versions/{version}"

    headers = {"Accept": "application/json"}
    secret = _resolve_federation_credential(workspace, url=url, modes=_CATALOG_CREDENTIAL_MODES)
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    payload = _federation_http_get(url, headers=headers, workspace=workspace, expect_json=True)
    if payload is None:
        return None

    digest = payload.get("digest") if isinstance(payload, dict) else None
    if not digest or not isinstance(digest, str):
        LOG.warning(
            "federation_catalog_fetch_no_digest: workspace=%s product=%s",
            workspace.id,
            product_id,
        )
        return None
    return digest


_DEFAULT_PORTS: Mapping[str, int] = {"http": 80, "https": 443}
#: Headers that carry a credential and must not follow a redirect to
#: another origin (requests strips Authorization; httpx also re-derives
#: Cookie; Proxy-Authorization is the same class of value).
_CREDENTIAL_HEADERS: frozenset[str] = frozenset({"authorization", "proxy-authorization", "cookie"})


def _url_origin(url: str) -> Tuple[str, str, Optional[int]]:
    """``(scheme, host, effective port)`` -- the RFC 6454 origin tuple, with
    the scheme's default port filled in so ``https://h`` == ``https://h:443``.
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    return scheme, (parsed.hostname or "").lower(), parsed.port or _DEFAULT_PORTS.get(scheme)


def _next_redirect_hop(
    current: str, next_url: str, headers: Mapping[str, str], *, workspace_id: str
) -> Optional[Tuple[str, Dict[str, str]]]:
    """``(url, headers)`` for following a redirect from ``current`` to
    ``next_url``, or ``None`` to refuse the hop.

    Refused: an https -> http downgrade (credential or not), and a target
    that fails the SSRF guard. A hop to another origin returns ``headers``
    minus every :data:`_CREDENTIAL_HEADERS` entry; the caller carries the
    result forward, so a later hop back to the first origin does not get
    them back.
    """
    here, there = _url_origin(current), _url_origin(next_url)
    if here[0] == "https" and there[0] != "https":
        LOG.warning(
            "federation_http_fetch_redirect_downgrade_refused: workspace=%s host=%s — "
            "an https endpoint redirected to %s://; refusing to leave TLS",
            workspace_id,
            here[1],
            there[0] or "?",
        )
        return None
    try:
        next_url = _guard_federation_url(next_url, workspace_id=workspace_id)
    except FederationSsrfError as exc:
        LOG.warning(
            "federation_http_fetch_ssrf_blocked_on_redirect: workspace=%s err=%s",
            workspace_id,
            exc,
        )
        return None
    if here == there:
        return next_url, dict(headers)
    LOG.debug(
        "federation_http_fetch_redirect_cross_origin: workspace=%s from=%s to=%s — "
        "credential headers dropped",
        workspace_id,
        here[1],
        there[1],
    )
    return next_url, {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}


def _federation_http_get(
    url: str,
    *,
    headers: Mapping[str, str],
    workspace: FederatedWorkspace,
    expect_json: bool,
) -> Optional[Any]:
    """Shared SSRF-guarded HTTP GET for the federation fetchers.

    Redirects are followed by hand, not by httpx (``follow_redirects=False``
    on the client), up to :data:`_FEDERATION_MAX_REDIRECTS` hops. On every
    hop:

    * the ``Location`` host is re-run through :func:`_guard_federation_url`;
    * an https -> http downgrade is refused (``None``), credential or not;
    * a hop to another origin (scheme, host or effective port differs)
      drops ``Authorization``, ``Proxy-Authorization`` and ``Cookie`` for
      the rest of the chain -- they are not restored if a later hop comes
      back. Same rule as requests' ``Session.should_strip_auth`` and
      httpx's ``Client._redirect_headers``, minus their http -> https
      exception (a credentialed request never starts on http here).

    Returns the decoded body (``str`` when ``expect_json`` is False,
    the parsed object when True), or ``None`` on any network / HTTP /
    SSRF-guard / decode failure. Only the *host* is logged on error —
    never the full URL, which can carry pre-auth tokens in the path.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover — httpx is a project dep
        LOG.warning("federation_http_fetch_httpx_unavailable: workspace=%s", workspace.id)
        return None

    host = urlparse(url).hostname or "?"
    try:
        current = _guard_federation_url(url, workspace_id=workspace.id)
    except FederationSsrfError as exc:
        LOG.warning("federation_http_fetch_ssrf_blocked: workspace=%s err=%s", workspace.id, exc)
        return None

    try:
        with httpx.Client(
            timeout=_FEDERATION_HTTP_TIMEOUT,
            follow_redirects=False,
            verify=True,
        ) as client:
            # SECURITY (unbounded-read OOM): a malicious or misconfigured
            # federation endpoint could return a multi-GB body and OOM the
            # stage-7 digest gate of ``fluid apply``. ``client.get`` buffers
            # the WHOLE body eagerly, so we must stream and cap — mirroring
            # the streamed per-chunk ceiling in ``safe_http.fetch_bytes``
            # (shared :data:`MAX_REMOTE_BYTES`). Redirects are detected from
            # the response status WITHOUT consuming the body, so the SSRF-
            # safe manual-redirect re-validation below stays intact.
            def _stream_capped() -> Optional[httpx.Response]:
                """GET ``current`` with a streamed body cap; re-validate each
                redirect hop's Location host before following it. Returns the
                final (non-redirect) response, or ``None`` on too-many-hops /
                SSRF-blocked redirect / oversized body."""
                nonlocal current
                hops = 0
                hop_headers = dict(headers)
                while True:
                    with client.stream("GET", current, headers=hop_headers) as resp:
                        if resp.is_redirect:
                            location = resp.headers.get("location")
                            if not location or hops >= _FEDERATION_MAX_REDIRECTS:
                                LOG.warning(
                                    "federation_http_fetch_too_many_redirects: "
                                    "workspace=%s host=%s",
                                    workspace.id,
                                    host,
                                )
                                return None
                            hop = _next_redirect_hop(
                                current,
                                str(httpx.URL(current).join(location)),
                                hop_headers,
                                workspace_id=workspace.id,
                            )
                            if hop is None:
                                return None
                            current, hop_headers = hop
                            hops += 1
                            continue
                        resp.raise_for_status()
                        chunks = []
                        total = 0
                        for chunk in resp.iter_bytes():
                            total += len(chunk)
                            if total > MAX_REMOTE_BYTES:
                                LOG.warning(
                                    "federation_http_fetch_body_too_large: "
                                    "workspace=%s host=%s cap=%s",
                                    workspace.id,
                                    host,
                                    MAX_REMOTE_BYTES,
                                )
                                return None
                            chunks.append(chunk)
                        # Hydrate ``resp._content`` from the capped read so
                        # ``.json()`` / ``.text`` work after the stream closes
                        # (httpx reads ``.text``/``.json`` off ``_content``).
                        resp._content = b"".join(chunks)
                        return resp

            resp = _stream_capped()
            if resp is None:
                return None
            if expect_json:
                return resp.json()
            return resp.text
    except httpx.HTTPStatusError as exc:
        LOG.warning(
            "federation_http_fetch_http_error: workspace=%s host=%s status=%s",
            workspace.id,
            host,
            exc.response.status_code,
        )
        return None
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        # Log only the exception class — httpx error messages can echo
        # the request URL, which may carry an auth token in the path.
        LOG.warning(
            "federation_http_fetch_network_error: workspace=%s host=%s err=%s",
            workspace.id,
            host,
            type(exc).__name__,
        )
        return None


def _fetch_digest_via_git(
    workspace: FederatedWorkspace, product_id: str, version: str
) -> Optional[str]:
    """Git-registry backend.

    Two paths, in order:

    1. **gitpython** (if installed) — clone (or open existing cache)
       the repo at ``workspace.endpoint``, read the file at the
       resolved product path, and recompute its digest via
       :func:`fluid_build.forge.core.plan_digest.compute_contract_digest`.
    2. **shell-out to git** — for environments where gitpython isn't
       available; same logic, ``subprocess.run(["git", "clone", ...])``.

    The product path resolves from ``workspace.endpoint`` +
    ``product_id`` + the conventional file ``contract.fluid.yaml``.

    The contract is **parsed and canonicalised** before hashing — never
    hashed as raw text. Peers format the same contract differently (key
    order, indentation, quoting, comments, CRLF), and a text hash would
    make every one of those look like upstream drift. Returns ``None``
    on an unreadable or malformed upstream contract, which the caller
    escalates to a violation — federation fails closed, never on a
    weaker digest.
    """
    contract_text = _git_read_contract(workspace, product_id)
    if not contract_text:
        return None

    try:
        contract = load_yaml_safe(contract_text)
    except (UnsafeYamlError, yaml.YAMLError) as exc:
        LOG.warning(
            "federation_git_contract_unparseable: workspace=%s product=%s err=%s",
            workspace.id,
            product_id,
            type(exc).__name__,
        )
        return None

    if not isinstance(contract, Mapping):
        LOG.warning(
            "federation_git_contract_not_a_mapping: workspace=%s product=%s got=%s",
            workspace.id,
            product_id,
            type(contract).__name__,
        )
        return None

    return compute_contract_digest(contract)


def _git_read_contract(workspace: FederatedWorkspace, product_id: str) -> Optional[str]:
    """Read the contract file text from the federated git repo.

    Tries gitpython first (in-process — no fork, surfaces detailed
    errors), then shells out to ``git`` as a fallback. Caches the
    cloned repo under ``~/.cache/fluid/federation-git/<workspace_id>``
    so repeated fetches don't re-clone.

    The cache is keyed by ``workspace.id`` (manifest-stable, and
    validated against :data:`_WORKSPACE_ID_RE` at construction so it is
    a safe path component), not by endpoint URL. Every refresh points
    ``origin`` at the current endpoint before fetching, so rewriting the
    endpoint without renaming the workspace fetches into the same dir, and
    a cache an older version cloned with the token in its remote URL is
    scrubbed on the next run. A refresh whose fetch fails returns None
    (unreachable), never the contract left in the cache.

    Raises :class:`FederationCredentialPolicyError` before git starts when
    the workspace's credential may not be sent to its endpoint.
    """
    cache_dir = Path(os.path.expanduser("~/.cache/fluid/federation-git")) / workspace.id
    cache_dir.parent.mkdir(parents=True, exist_ok=True)

    remote_url = _git_remote_url(workspace)
    auth_env = _git_auth_env(workspace)

    # Shell-out, always. There used to be a gitpython-first path here, on
    # the theory that in-process was tidier than a fork. It cannot be
    # bounded, and `fluid apply` now depends on this call finishing.
    #
    # Measured against a TCP listener that accepts and never speaks (a
    # dead git daemon), GitPython 3.1.62:
    #
    #   Repo.clone_from(url, dst, kill_after_timeout=3)  -> still running at 90s
    #   subprocess.run([...], timeout=3)                 -> TimeoutExpired at 3.0s
    #
    # `kill_after_timeout` is *accepted* by clone_from -- it is in
    # GitPython's execute_kwargs, so it is not rejected the way an unknown
    # kwarg is -- but it does not kill the clone. Accepting a timeout it
    # does not honour is worse than not offering one, because
    # `fluid doctor` then advertises a cap that does not exist.
    #
    # This was the live path, not a latent one: GitPython arrives
    # transitively via `dlt` (a declared ingestion engine), so a normal
    # install has it and took the unbounded branch first.
    #
    # Nothing is lost by dropping it: GitPython shells out to the same
    # `git` binary, so it was never a fallback for git being absent.
    if not _git_clone_or_pull_via_shellout(
        remote_url=remote_url, cache_dir=cache_dir, workspace_id=workspace.id, env=auth_env
    ):
        return None

    return _read_first_existing_contract(cache_dir, workspace, product_id)


def _git_remote_url(workspace: FederatedWorkspace) -> str:
    """The URL git clones / fetches from: the endpoint, never a credential.

    A separate seam so the real-git tests can point it at a local fixture
    (the endpoint allow-list refuses ``file://`` on purpose).
    """
    return workspace.endpoint


def _git_auth_env(workspace: FederatedWorkspace) -> Dict[str, str]:
    """Environment entries that hand git the workspace's token, or ``{}``.

    The token reaches git only through the process environment, so it is
    never in argv (which ps(1) shows), in the clone URL, or in
    ``.git/config``, and a rotated token is used on the next fetch. git gets
    two settings, both keyed to the endpoint URL (:func:`_git_http_scopes`):

    * ``http.<url>.extraHeader`` = ``Authorization: Basic
      base64(x-access-token:<token>)``, the header actions/checkout's
      ``git-auth-helper.ts`` configures, minus its on-disk config file.
      git sends every extra header that applies, so an Authorization header
      the operator already configured (a CI step's ``git config --global
      http.https://github.com/.extraheader ...``) would go out as a second
      one, which GitHub rejects with 400. An empty ``extraHeader`` comes
      first: git documents it as resetting the list (git-config(1),
      ``http.extraHeader``). The operator's other headers for the endpoint
      (an access gateway's ``Proxy-Authorization`` or ``cf-access-token``)
      follow it, then ours, so the reset removes only Authorization headers
      (:func:`_git_operator_settings`). Each scope carries that whole list,
      so whichever of them git applies, it sends the same headers.
    * ``http.<url>.followRedirects`` = ``false``. curl drops a custom header
      on the redirected request itself, but git's default (``initial``) then
      rebases the remote onto the redirect target and sends every later
      request there, with the header attached, whatever its origin or
      scheme. With redirects off, a redirect fails the fetch and the
      upstream is reported unreachable. This is the git side of the policy
      the HTTP backend applies per hop (:func:`_next_redirect_hop`); git has
      no same-origin-only setting, so an authenticated endpoint must name
      the final location.

    Both must hold whatever git configuration the operator already has. For
    each ``http.<url>.<key>``, git applies an entry only when it matches the
    remote URL at least as specifically as every earlier entry for that key
    -- host first, then the length of the matched path, then whether it names
    the URL's user -- so a later entry wins only a tie (git-config(1),
    ``http.<url>.*``; urlmatch.c ``urlmatch_config_entry``, ``cmp_matches``).
    Keyed to the origin, the settings would lose to an operator's
    ``followRedirects = true`` for the repository path, from any config file
    or environment layer. Keyed to the endpoint URL, no key that can match
    ranks higher, and the entries are read last: git reads
    ``GIT_CONFIG_COUNT`` pairs and then ``GIT_CONFIG_PARAMETERS`` after every
    config file (config.c ``git_config_from_parameters``), and the entries
    are appended to the inherited ``GIT_CONFIG_PARAMETERS`` the way ``git -c``
    appends them (:func:`_git_config_parameters`). Inherited entries are kept.
    When the operator's ``url.<base>.insteadOf`` adds a user name to the
    endpoint, the settings are keyed to that ``user@`` URL as well, which a
    key naming the user cannot outrank.

    An endpoint that already carries userinfo gets no header, as before, and
    neither does one the operator's configuration rewrites to another
    scheme, host, port or path, one it gives an extra header holding a line
    break, or one whose git configuration cannot be read. Anonymous fetches
    keep git's default redirect handling.
    """
    parsed = urlparse(workspace.endpoint)
    if "@" in parsed.netloc:
        return {}
    secret = _resolve_federation_credential(
        workspace, url=workspace.endpoint, modes=_GIT_CREDENTIAL_MODES
    )
    if not secret:
        return {}
    operator = _git_operator_settings(workspace)
    if operator is None:
        return {}
    user, operator_headers = operator
    credential = base64.b64encode(f"x-access-token:{secret}".encode("utf-8")).decode("ascii")
    from fluid_build.observability.secret_redactor import register_secret

    register_secret(credential)
    headers = [*operator_headers, f"Authorization: Basic {credential}"]
    urls = [workspace.endpoint]
    if user is not None:
        urls.append(_git_url_with_user(workspace.endpoint, user))
    entries = [
        entry
        for url in urls
        for scope in _git_http_scopes(url)
        for entry in (
            (f"http.{scope}.extraHeader", ""),
            *((f"http.{scope}.extraHeader", header) for header in headers),
            (f"http.{scope}.followRedirects", "false"),
        )
    ]
    return {
        "GIT_CONFIG_PARAMETERS": _git_config_parameters(entries),
        # Fail instead of waiting on a credential prompt the bound would kill.
        "GIT_TERMINAL_PROMPT": "0",
    }


#: Beyond this many ``http.extraHeader`` entries in the operator's git
#: configuration, federation does not work out which apply to the endpoint
#: (the probe is quadratic), and the fetch carries no credential.
_GIT_EXTRA_HEADERS_MAX = 64


def _git_operator_settings(
    workspace: FederatedWorkspace,
) -> Optional[Tuple[Optional[str], List[str]]]:
    """What the operator's git configuration does to a fetch of the endpoint.

    Returns ``(user, headers)``, or ``None`` when the fetch must carry no
    credential:

    * ``url.<base>.insteadOf`` makes git fetch another URL. One with another
      scheme, host, port or path is not the endpoint: ``None``. One that only
      adds userinfo (``https://ci-token:...@host/``, set for Go modules or
      submodules) is: ``user`` is the user name it adds (``None`` when there is
      no rewrite), which git ranks ``http.<url>`` keys by.
    * ``headers`` are the ``http.extraHeader`` values git would send to that
      URL, minus ``Authorization`` headers, which federation's replaces. When
      one of the values git would send holds a CR, LF or NUL: ``None``. curl
      writes a header as it is given (CURLOPT_HTTPHEADER(3)), and a server
      reads CR LF, or a bare LF, as the end of a header line (RFC 9112 section
      2.2), so the text after the break is a header federation cannot see,
      such as a second ``Authorization``, and a trailing break ends the header
      block before federation's header.

    Read the way ``git clone`` reads them: system, global and included
    files, ``GIT_CONFIG_COUNT`` pairs and ``GIT_CONFIG_PARAMETERS``, and no
    repository's own config (``GIT_DIR`` names no repository). A
    configuration git cannot read gives ``None`` as well.
    """
    endpoint = workspace.endpoint
    entries = _git_config_entries(r"^(url\..*\.insteadof|http\..*extraheader)$")
    rewrites: List[Tuple[str, str]] = []
    for key, value in entries or ():
        if key.startswith("url.") and key.endswith(".insteadof") and key != "url.insteadof":
            if value is None:  # git refuses to run with this entry
                entries = None
                break
            rewrites.append((key[len("url.") : -len(".insteadof")], value))
    if entries is None:
        LOG.warning(
            "federation_git_config_unreadable: workspace=%s — git's configuration could "
            "not be read, so the fetch carries no credential",
            workspace.id,
        )
        return None

    fetched = _git_rewritten_url(endpoint, rewrites)
    user: Optional[str] = None
    if fetched != endpoint:
        without_user, user = _git_url_split_user(fetched)
        if without_user != endpoint or user is None:
            # Never log the rewritten URL: its userinfo may be a credential.
            LOG.info(
                "federation_git_endpoint_rewritten: workspace=%s — url.<base>.insteadOf in "
                "your git configuration sends the fetch to another scheme, host, port or "
                "path, so it carries no credential",
                workspace.id,
            )
            return None

    url = endpoint if user is None else _git_url_with_user(endpoint, user)
    applied = _git_extra_headers_for(url, entries)
    if applied is None:
        LOG.warning(
            "federation_git_config_unreadable: workspace=%s — the http.extraHeader "
            "entries in your git configuration could not be matched to the endpoint, "
            "so the fetch carries no credential",
            workspace.id,
        )
        return None
    broken = sorted(
        {_git_extra_header_setting(key) for key, header in applied if _breaks_header_line(header)}
    )
    if broken:
        # Never log the value: it is a header, and can be a credential.
        LOG.warning(
            "federation_git_extra_header_line_break: workspace=%s — %s in your git "
            "configuration holds a line break or NUL, which curl sends as it is, so the "
            "fetch carries no credential",
            workspace.id,
            ", ".join(broken),
        )
        return None
    return user, [header for _key, header in applied if not _is_authorization_header(header)]


def _git_config_entries(pattern: str) -> Optional[List[Tuple[str, Optional[str]]]]:
    """``(key, value)`` for each entry ``git config --get-regexp`` lists, in the
    order git reads them, or ``None`` when git cannot read its configuration.

    ``GIT_DIR`` names no repository, so no repository's config is read, as for
    ``git clone``; ``GIT_CONFIG`` (read by ``git config`` alone) is dropped.
    """
    import subprocess

    env = {name: value for name, value in os.environ.items() if name != "GIT_CONFIG"}
    env["GIT_DIR"] = os.devnull
    try:
        done = subprocess.run(
            ["git", "config", "-z", "--get-regexp", pattern],
            check=False,
            capture_output=True,
            timeout=_federation_git_timeout(),
            env=env,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    # 1: no entry matched.
    if done.returncode not in (0, 1) or not isinstance(done.stdout, bytes):
        return None
    return _git_config_z_records(done.stdout)


def _git_config_z_records(raw: bytes) -> List[Tuple[str, Optional[str]]]:
    """Parse ``git config -z`` output: ``key\\nvalue\\0`` per entry, or
    ``key\\0`` for an entry with no ``=`` (value ``None``)."""
    records: List[Tuple[str, Optional[str]]] = []
    for record in raw.decode("utf-8", "surrogateescape").split("\0")[:-1]:
        key, sep, value = record.partition("\n")
        records.append((key, value if sep else None))
    return records


def _git_rewritten_url(url: str, rewrites: Sequence[Tuple[str, str]]) -> str:
    """``url`` after ``url.<base>.insteadOf``, as git applies it (remote.c
    ``alias_url``): the longest ``insteadOf`` prefix wins, the first one read
    among equals, with bases taken in the order they first appear."""
    prefixes: Dict[str, List[str]] = {}
    for base, prefix in rewrites:
        prefixes.setdefault(base, []).append(prefix)
    best: Optional[Tuple[str, str]] = None
    for base, candidates in prefixes.items():
        for prefix in candidates:
            if url.startswith(prefix) and (best is None or len(best[1]) < len(prefix)):
                best = (base, prefix)
    return url if best is None else best[0] + url[len(best[1]) :]


def _git_url_split_user(url: str) -> Tuple[str, Optional[str]]:
    """``(url without its userinfo, user name)``; the user is ``None`` when
    the URL has no userinfo. The authority ends at the first ``/``, ``?`` or
    ``#`` and the userinfo at its first ``@``, as git reads a URL (urlmatch.c
    ``url_normalize_1``)."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url, None
    end = min([i for i in (rest.find(mark) for mark in "/?#") if i != -1], default=len(rest))
    userinfo, at, hostport = rest[:end].partition("@")
    if not at:
        return url, None
    return f"{scheme}://{hostport}{rest[end:]}", userinfo.partition(":")[0]


def _git_url_with_user(url: str, user: str) -> str:
    """``url`` (which has no userinfo) with ``user@`` added, no password."""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{user}@{rest}"


def _git_extra_headers_for(
    url: str, entries: Sequence[Tuple[str, Optional[str]]]
) -> Optional[List[Tuple[str, str]]]:
    """The ``http.extraHeader`` entries git would send to ``url``, in order, as
    ``(key, value)`` from the operator's ``entries``; ``None`` when that cannot
    be worked out.

    git's own URL matching decides, not a copy of it: git applies an entry
    when it matches the URL at least as well as every earlier one that
    matched (urlmatch.c ``urlmatch_config_entry``), and an empty value clears
    the list. ``git config --get-urlmatch`` reports only the last entry it
    applied for a key, so entry ``k`` is replayed, under its own ``http.<url>``
    scope, as the last of entries ``0..k`` for a probe key of its own: it was
    applied when that probe key resolves to ``k``. The probe reads nothing but
    those ``GIT_CONFIG_COUNT`` pairs. remote-curl matches the remote URL with a
    ``/`` appended (``end_url_with_slash``), so the probe does too. ``url`` is
    on the probe's command line: the endpoint, or the endpoint with the user
    name an ``insteadOf`` rewrite adds, which git puts on its remote helper's
    command line anyway (transport-helper.c ``get_helper``); never a password.
    """
    import subprocess

    found = [
        (key, value)
        for key, value in entries
        if key.startswith("http.") and key.endswith(".extraheader")
    ]
    if not found:
        return []
    if len(found) > _GIT_EXTRA_HEADERS_MAX:
        return None

    def probe_key(index: int, probe: int) -> str:
        key = found[index][0]
        if key == "http.extraheader":
            return f"http.fluidprobe{probe}"
        return f"http.{key[len('http.') : -len('.extraheader')]}.fluidprobe{probe}"

    pairs = [(probe_key(j, k), str(j)) for k in range(len(found)) for j in range(k + 1)]
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_CONFIG")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_DIR": os.devnull,
            "GIT_CONFIG_COUNT": str(len(pairs)),
        }
    )
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    target = url if url.endswith("/") else url + "/"
    try:
        done = subprocess.run(
            ["git", "config", "-z", "--get-urlmatch", "http", target],
            check=False,
            capture_output=True,
            timeout=_federation_git_timeout(),
            env=env,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    # 1: no entry matched the URL.
    if done.returncode not in (0, 1) or not isinstance(done.stdout, bytes):
        return None
    resolved = dict(_git_config_z_records(done.stdout))
    sent: List[Tuple[str, str]] = []
    for k, (key, value) in enumerate(found):
        if resolved.get(f"http.fluidprobe{k}") != str(k):
            continue
        if value is None:  # git refuses to run with this entry
            return None
        if value:
            sent.append((key, value))
        else:
            sent.clear()
    return sent


def _breaks_header_line(header: str) -> bool:
    """True when ``header`` holds a CR, LF or NUL: curl would not send it as
    one header line (see :func:`_git_operator_settings`)."""
    return any(mark in header for mark in "\r\n\0")


def _git_extra_header_setting(key: str) -> str:
    """The setting ``key`` (``http[.<url>].extraheader``, as ``git config``
    lists it) names, for a log line: ``http[.<url>].extraHeader`` with the
    userinfo of ``<url>`` left out, since a user name or password there can be
    a credential."""
    url = key[len("http.") : -len(".extraheader")]
    if not url:
        return "http.extraHeader"
    return f"http.{_git_url_split_user(url)[0]}.extraHeader"


def _is_authorization_header(header: str) -> bool:
    """True for an ``Authorization`` header line (``Name: value``, or curl's
    ``Name;`` for an empty one), in any case."""
    return re.split(r"[:;]", header, maxsplit=1)[0].strip().lower() == "authorization"


def _git_http_scopes(url: str) -> Tuple[str, str]:
    """The ``<url>`` of the ``http.<url>.*`` keys that rank highest for ``url``.

    git matches ``http.<url>.*`` keys against the remote URL with a ``/``
    appended (remote-curl.c ``end_url_with_slash``) and scores a key's path
    by the length it matches, counting one trailing ``/`` (urlmatch.c
    ``url_match_prefix``). So ``<url>//`` scores one more than ``<url>``, and
    nothing longer matches (measured on git 2.47). Both are returned: the
    doubled slash ties the most specific key an operator can write, and
    ``url`` itself keeps the settings in force on a git that stops matching
    the doubled slash.
    """
    with_slash = url if url.endswith("/") else url + "/"
    return url, with_slash + "/"


def _git_config_parameters(entries: Sequence[Tuple[str, str]]) -> str:
    """The inherited ``GIT_CONFIG_PARAMETERS`` with ``entries`` appended.

    The layout ``git -c`` itself produces (config.c
    ``git_config_push_parameter``): the inherited value, a space, then each
    entry as ``'key'='value'`` (the form git 2.31 and later write), with
    ``'`` written as ``'\\''`` (quote.c ``sq_quote_buf``).
    """

    def quoted(text: str) -> str:
        return "'" + text.replace("'", "'\\''") + "'"

    ours = " ".join(f"{quoted(key)}={quoted(value)}" for key, value in entries)
    inherited = os.environ.get("GIT_CONFIG_PARAMETERS", "")
    return f"{inherited} {ours}" if inherited else ours


def _git_clone_or_pull_via_shellout(
    *,
    remote_url: str,
    cache_dir: Path,
    workspace_id: str,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Shell-out fallback. Returns True on success, False on failure.

    ``env``: extra entries for git's environment (:func:`_git_auth_env`).

    Security: ``remote_url`` is also validated upstream by
    :func:`_validate_federation_endpoint` before reaching this
    function, but we still pass ``--`` before the positional URL so
    git's argv parser is forced to treat it as a URL even if a future
    code path forgets the scheme allow-list. This is defence-in-depth
    against the well-known argument-injection pattern (CVE-2017-1000117
    et al.).
    """
    import subprocess

    run_env = {**os.environ, **env} if env else None

    if cache_dir.exists() and not _git_repair_origin(cache_dir, remote_url, run_env, workspace_id):
        return False

    if not cache_dir.exists():
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--", remote_url, str(cache_dir)],
                check=True,
                capture_output=True,
                timeout=_federation_git_timeout(),
                env=run_env,
            )
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as exc:
            # Log only the exception class — never the body. The URL no
            # longer carries a credential, but git's stderr is upstream-
            # controlled text and the argv is not this log's business.
            LOG.warning(
                "federation_git_clone_failed: workspace=%s err=%s — "
                "git clone failed (authentication or remote error); "
                "refusing to echo the command line",
                workspace_id,
                type(exc).__name__,
            )
            return False
    else:
        # ``fetch`` and ``reset --hard`` read from the remote name
        # (``origin``), which :func:`_git_repair_origin` just pointed at
        # the validated endpoint. A failed step fails the read: the files
        # left in the cache are the last upstream state that was fetched,
        # and returning them would report a stale contract as live.
        steps: Tuple[Tuple[str, List[str], Optional[Mapping[str, str]]], ...] = (
            ("fetch", _git_in_cache(cache_dir, "fetch", "--depth", "1", "origin"), run_env),
            # No credential needed: reset is local.
            ("reset", _git_in_cache(cache_dir, "reset", "--hard", "origin/HEAD"), None),
        )
        for step, argv, step_env in steps:
            try:
                done = subprocess.run(
                    argv,
                    check=False,
                    capture_output=True,
                    timeout=_federation_git_timeout(),
                    env=step_env,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                # Log only the class — git error text is upstream-controlled.
                LOG.warning(
                    "federation_git_refresh_failed: workspace=%s step=%s err=%s",
                    workspace_id,
                    step,
                    type(exc).__name__,
                )
                return False
            if done.returncode != 0:
                LOG.warning(
                    "federation_git_refresh_failed: workspace=%s step=%s exit=%s — "
                    "the cached clone was not refreshed, so it is not read",
                    workspace_id,
                    step,
                    done.returncode,
                )
                return False
    return True


def _git_in_cache(cache_dir: Path, *args: str) -> List[str]:
    """argv for a git command on the cached clone, and on nothing else.

    ``--git-dir`` / ``--work-tree`` pin git to ``cache_dir`` itself. With
    ``-C cache_dir`` alone git searches upward for a repository, so a cache
    dir that is not a clone (an interrupted clone, a leftover empty dir)
    inside another repo -- a home directory kept in git -- would have the
    command applied to that enclosing repo. They also override any
    ``GIT_DIR`` / ``GIT_WORK_TREE`` in the caller's environment.
    """
    return ["git", f"--git-dir={cache_dir / '.git'}", f"--work-tree={cache_dir}", *args]


def _git_repair_origin(
    cache_dir: Path,
    remote_url: str,
    run_env: Optional[Mapping[str, str]],
    workspace_id: str,
) -> bool:
    """Point the cached clone's ``origin`` at ``remote_url`` before a refresh.

    This makes an endpoint change take effect, and scrubs
    ``remote.origin.url`` in caches that older versions cloned with the
    token embedded in the URL (``https://x-access-token:<t>@...``). A cache
    dir with no ``.git`` directory, or a clone that cannot be re-pointed
    (corrupt), may still hold such a URL, so it is removed and the caller
    clones fresh. Returns False only when that removal fails. git only ever
    runs on the cache itself (:func:`_git_in_cache`).
    """
    import shutil
    import subprocess

    if (cache_dir / ".git").is_dir():
        try:
            done = subprocess.run(
                _git_in_cache(cache_dir, "remote", "set-url", "--", "origin", remote_url),
                check=False,
                capture_output=True,
                timeout=_federation_git_timeout(),
                env=run_env,
            )
            if done.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
    LOG.warning(
        "federation_git_cache_reset: workspace=%s — the cache dir is not a clone, or "
        "could not be re-pointed at the endpoint; removing it and cloning fresh",
        workspace_id,
    )
    try:
        shutil.rmtree(cache_dir)
    except OSError as exc:
        LOG.warning(
            "federation_git_cache_reset_failed: workspace=%s err=%s",
            workspace_id,
            type(exc).__name__,
        )
        return False
    return True


# SECURITY (G2): product_id is interpolated into filesystem paths under
# the git-clone cache. It originates from upstream contracts' consumes[]
# and the federation manifest, so it is attacker-influenced — restrict it
# to a data-product identifier shape (must start alphanumeric, no path
# separators) so a value like "../../etc" cannot traverse out of the clone.
_PRODUCT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _read_first_existing_contract(
    cache_dir: Path, workspace: FederatedWorkspace, product_id: str
) -> Optional[str]:
    """Resolve the product path inside a cloned repo. Convention:
    ``<product_id>/contract.fluid.yaml``. Tolerates dot-paths that
    map to nested directories."""
    if not _PRODUCT_ID_RE.fullmatch(product_id or ""):
        LOG.warning(
            "federation_git_contract_rejected: workspace=%s — product_id is not "
            "a valid data-product identifier (path-traversal guard)",
            workspace.id,
        )
        return None
    cache_root = cache_dir.resolve()
    candidate_paths = [
        cache_dir / product_id / "contract.fluid.yaml",
        cache_dir / product_id.replace(".", "/") / "contract.fluid.yaml",
        cache_dir / f"{product_id}.fluid.yaml",
    ]
    for path in candidate_paths:
        resolved = path.resolve()
        # Defence-in-depth: confirm the resolved path stays in the clone.
        if not resolved.is_relative_to(cache_root):
            continue
        if resolved.is_file():
            try:
                # SECURITY (unbounded-read OOM): stat-before-read so a
                # hostile contract pulled from a federated git repo can't
                # exhaust memory before ``load_yaml_safe``'s post-hoc cap
                # ever runs. Mirrors the stat-before-read pattern in
                # ``forge_copilot_runtime._confine_to_workspace`` and reuses
                # the same :data:`MAX_YAML_BYTES` ceiling as ``load_yaml_safe``.
                if resolved.stat().st_size > MAX_YAML_BYTES:
                    LOG.warning(
                        "federation_git_contract_too_large: path=%s size=%s cap=%s",
                        path,
                        resolved.stat().st_size,
                        MAX_YAML_BYTES,
                    )
                    return None
                return resolved.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover
                LOG.warning(
                    "federation_git_contract_read_failed: path=%s err=%s",
                    path,
                    exc,
                )
                return None

    LOG.warning(
        "federation_git_contract_not_found: workspace=%s product=%s tried=%s",
        workspace.id,
        product_id,
        [str(p.relative_to(cache_dir)) for p in candidate_paths],
    )
    return None


@dataclass
class FederatedConsumeViolation:
    """One finding for a federated consumes[] entry.

    ``kind`` separates *the upstream really did change* from *we could
    not check it*, which callers must treat differently: drift is a
    statement about the data, unreachability is a statement about the
    network. Collapsing them would let a dead registry read as a clean
    upstream (or a clean upstream read as drift), and the apply gate
    words its warning off this field.

    Values: ``"drift"`` (pinned and live digests differ), ``"unpinned"``
    (federated upstream declared with no ``upstreamDigest``),
    ``"unknown-workspace"`` (not in ``federation/upstreams.yaml``),
    ``"unreachable"`` (the fetch itself failed), ``"not-wired"`` (no
    fetcher for that workspace kind yet).
    """

    consume_index: int
    upstream_workspace_id: str
    upstream_product_id: str
    expected_digest: str
    actual_digest: str
    reason: str
    kind: str = "drift"


def _unreachable_reason(ws_id: str, exc: BaseException) -> Tuple[str, str]:
    """``(violation reason, log detail)`` for a fetch that produced no digest.

    Only the exception class is surfaced, since fetch errors can echo URLs --
    except for :class:`FederationCredentialPolicyError`, whose message names
    the variable and the fix and never the value. An operator needs that
    text: "could not reach" alone sends them to a registry never contacted.
    """
    name = type(exc).__name__
    if isinstance(exc, FederationCredentialPolicyError):
        return f"{exc} The pinned digest was NOT checked.", f"{name}: {exc}"
    return (
        f"Could not reach federated upstream {ws_id!r} to verify the pin ({name}). "
        "The pinned digest was NOT checked -- this is not a statement that the "
        "upstream is unchanged.",
        name,
    )


def validate_federated_consumes(
    contract: Mapping[str, Any],
    *,
    workspace_root: Path,
    manifest: Optional[FederationManifest] = None,
) -> List[FederatedConsumeViolation]:
    """Walk ``consumes[]`` and verify every federated entry's digest.

    Returns one :class:`FederatedConsumeViolation` per drift. Empty
    list when every entry is in sync. Local consumes (no
    ``upstreamWorkspace`` field) are passed through unchanged — they
    are validated by the existing in-workspace validator.

    The federation-aware row shape::

        consumes:
          - productId: orders_v1
            exposeId: orders
            upstreamWorkspace: telco-billing
            upstreamDigest: sha256:1c2d3e...

    When ``upstreamWorkspace`` is set, this validator looks up the
    workspace in the manifest, fetches the live digest, and compares
    against ``upstreamDigest``. Drift produces a violation; absence of
    ``upstreamDigest`` produces a violation too (the contract author
    declared a federated upstream but didn't pin it).
    """
    if manifest is None:
        manifest = load_federation_manifest(workspace_root)

    violations: List[FederatedConsumeViolation] = []
    consumes = contract.get("consumes")
    if not isinstance(consumes, list):
        return violations

    for idx, consume in enumerate(consumes):
        if not isinstance(consume, Mapping):
            continue
        ws_id = consume.get("upstreamWorkspace")
        if not ws_id:
            continue  # local consume — handled by the in-workspace validator
        product_id = consume.get("productId")
        version = str(consume.get("version", "1"))
        expected = consume.get("upstreamDigest")
        if not expected:
            violations.append(
                FederatedConsumeViolation(
                    consume_index=idx,
                    upstream_workspace_id=str(ws_id),
                    upstream_product_id=str(product_id or "?"),
                    expected_digest="",
                    actual_digest="",
                    reason=(
                        "Federated upstream missing required "
                        "``upstreamDigest`` pin. Add a digest captured "
                        "via ``fluid forge`` against the upstream "
                        "workspace's contract."
                    ),
                    kind="unpinned",
                )
            )
            continue
        ws = manifest.get(str(ws_id))
        if ws is None:
            violations.append(
                FederatedConsumeViolation(
                    consume_index=idx,
                    upstream_workspace_id=str(ws_id),
                    upstream_product_id=str(product_id or "?"),
                    expected_digest=str(expected),
                    actual_digest="",
                    reason=(
                        f"Federated workspace {ws_id!r} not declared in "
                        f"federation/upstreams.yaml. Add it to the "
                        f"manifest before referencing in consumes[]."
                    ),
                    kind="unknown-workspace",
                )
            )
            continue
        try:
            actual = fetch_federated_digest(
                ws,
                str(product_id or ""),
                version,
                workspace_root=workspace_root,
            )
        except Exception as exc:  # noqa: BLE001 - every failure becomes a row
            if isinstance(exc, NotImplementedError) and not isinstance(exc, FederationFetchError):
                # No fetcher for this workspace kind -- genuinely missing
                # FLUID code, which is what "not wired" should mean.
                violations.append(
                    FederatedConsumeViolation(
                        consume_index=idx,
                        upstream_workspace_id=str(ws_id),
                        upstream_product_id=str(product_id or "?"),
                        expected_digest=str(expected),
                        actual_digest="",
                        reason=(f"Federation fetcher for kind={ws.kind!r} not yet wired ({exc})."),
                        kind="not-wired",
                    )
                )
                continue
            # Everything else is a reachability failure: the fetch ran and
            # could not produce a digest. It must not abort the walk --
            # raising here would discard violations already found for
            # *other* upstreams, so one dead registry would mask real
            # drift elsewhere.
            reason, detail = _unreachable_reason(str(ws_id), exc)
            LOG.warning(
                "federation_fetch_failed: workspace=%s product=%s err=%s",
                ws_id,
                product_id,
                detail,
            )
            violations.append(
                FederatedConsumeViolation(
                    consume_index=idx,
                    upstream_workspace_id=str(ws_id),
                    upstream_product_id=str(product_id or "?"),
                    expected_digest=str(expected),
                    actual_digest="",
                    reason=reason,
                    kind="unreachable",
                )
            )
            continue
        if actual != expected:
            violations.append(
                FederatedConsumeViolation(
                    consume_index=idx,
                    upstream_workspace_id=str(ws_id),
                    upstream_product_id=str(product_id or "?"),
                    expected_digest=str(expected),
                    actual_digest=str(actual),
                    reason=(
                        f"Federated upstream digest drift: pinned "
                        f"{expected!r}, live {actual!r}. Re-run "
                        f"``fluid forge`` to refresh the pin or "
                        f"investigate why the upstream changed."
                    ),
                    kind="drift",
                )
            )
    return violations


__all__ = [
    "FEDERATION_CACHE_DIRNAME",
    "FEDERATION_MANIFEST_FILENAME",
    "FederatedConsumeViolation",
    "FederatedWorkspace",
    "FederationCredentialPolicyError",
    "FederationManifest",
    "FederationSsrfError",
    "fetch_federated_digest",
    "get_cached_digest",
    "load_federation_manifest",
    "store_cached_digest",
    "validate_federated_consumes",
]
