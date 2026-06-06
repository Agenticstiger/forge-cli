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
          secret_ref: GITHUB_TOKEN
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
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlparse

import yaml

# Reuse the canonical SSRF post-DNS-resolution gate rather than
# re-deriving a private-range list here. ``_hostname_is_private``
# resolves the host and returns True for RFC1918, link-local
# (169.254.0.0/16 — AWS/GCP metadata), loopback, unspecified, and
# reserved ranges (and fails closed on DNS errors).
from fluid_build._net import _hostname_is_private
from fluid_build.util.safe_http import MAX_REMOTE_BYTES
from fluid_build.util.safe_yaml import MAX_YAML_BYTES, load_yaml_safe

LOG = logging.getLogger("fluid.forge.federation")

FEDERATION_MANIFEST_FILENAME = "federation/upstreams.yaml"
FEDERATION_CACHE_DIRNAME = "federation"

# HTTP-client hardening defaults for every outbound federation fetch.
# ``follow_redirects=False`` is the SSRF-safe default — a public
# endpoint that 30x-redirects to http://169.254.169.254/ must NOT be
# chased silently. Where a redirect is genuinely expected the fetcher
# uses an explicit small cap and re-runs the host gate on each hop.
_FEDERATION_HTTP_TIMEOUT = 15.0
_FEDERATION_MAX_REDIRECTS = 3

# Opt-in allow-list for operators whose federation endpoint genuinely
# lives on an internal/private address (a corporate registry behind a
# VPN, a self-hosted catalog on an RFC1918 host). Comma-separated host
# suffixes; empty/unset → default-deny private destinations.
_FEDERATION_HOST_ALLOWLIST_ENV = "FLUID_FEDERATION_HOST_ALLOWLIST"

# ``workspace.id`` is interpolated into a filesystem path
# (``~/.cache/fluid/federation-git/<id>`` and the digest-cache file).
# Constrain it to a conservative slug so a tampered manifest row can't
# smuggle ``../`` traversal or absolute-path segments into those sinks.
_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class FederationSsrfError(ValueError):
    """Raised when a federation endpoint host fails the SSRF guard."""


def _federation_host_on_allowlist(hostname: str) -> bool:
    """Return True when ``FLUID_FEDERATION_HOST_ALLOWLIST`` is set and
    ``hostname`` matches one of its comma-separated suffixes exactly or
    as a dotted suffix. Empty/unset → False (no allow-list configured).
    """
    suffixes = [
        s.strip()
        for s in os.environ.get(_FEDERATION_HOST_ALLOWLIST_ENV, "").split(",")
        if s.strip()
    ]
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
        with path.open() as f:
            doc = load_yaml_safe(f) or {}
    except (OSError, yaml.YAMLError) as exc:
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
        with path.open() as f:
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
        raise NotImplementedError(
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

    Security: the endpoint host is run through the SSRF guard
    (:func:`_guard_federation_url`) before the request, and the
    request itself uses :func:`_federation_http_get` which disables
    redirect-following (``follow_redirects=False``) so a public
    endpoint cannot 30x-bounce the auth-bearing request to a
    cloud-metadata address. On HTTP error only the *host* is logged —
    pre-auth tokens can sit in the URL path.
    """
    base = workspace.endpoint.rstrip("/")
    url = f"{base}/{product_id}/{version}/digest"

    headers = {"Accept": "text/plain"}
    secret = _read_secret_value(workspace.auth_secret_ref)
    mode = (workspace.auth_mode or "none").lower()
    if secret:
        if mode == "basic":
            headers["Authorization"] = f"Basic {secret}"
        elif mode in ("oidc", "bearer", "http_token", "token"):
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

    Security: same SSRF guard + redirect-disabled fetch as
    :func:`_fetch_digest_via_http` (see :func:`_federation_http_get`).
    """
    base = workspace.endpoint.rstrip("/")
    url = f"{base}/products/{product_id}/versions/{version}"

    headers = {"Accept": "application/json"}
    secret = _read_secret_value(workspace.auth_secret_ref)
    mode = (workspace.auth_mode or "none").lower()
    if secret and mode in ("oidc", "bearer", "http_token", "token", "catalog_token"):
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


def _federation_http_get(
    url: str,
    *,
    headers: Mapping[str, str],
    workspace: FederatedWorkspace,
    expect_json: bool,
) -> Optional[Any]:
    """Shared SSRF-guarded HTTP GET for the federation fetchers.

    Migrated off ``urllib.request.urlopen`` (which followed up to 10
    redirects by default and re-sent ``Authorization`` cross-host).
    This uses :mod:`httpx` with ``follow_redirects=False`` and a small
    explicit ``max_redirects`` cap; every redirect hop's ``Location``
    host is re-run through :func:`_guard_federation_url` before the
    request follows it.

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
                while True:
                    with client.stream("GET", current, headers=dict(headers)) as resp:
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
                            next_url = str(httpx.URL(current).join(location))
                            try:
                                current = _guard_federation_url(next_url, workspace_id=workspace.id)
                            except FederationSsrfError as exc:
                                LOG.warning(
                                    "federation_http_fetch_ssrf_blocked_on_redirect: "
                                    "workspace=%s err=%s",
                                    workspace.id,
                                    exc,
                                )
                                return None
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
       resolved product path, and recompute its bundle digest via
       :func:`fluid_build.forge.core.plan_digest.compute_contract_digest`.
    2. **shell-out to git** — for environments where gitpython isn't
       available; same logic, ``subprocess.run(["git", "clone", ...])``.

    The product path resolves from ``workspace.endpoint`` +
    ``product_id`` + the conventional file ``contract.fluid.yaml``.
    """
    contract_text = _git_read_contract(workspace, product_id)
    if not contract_text:
        return None

    # Compute the canonical digest of the fetched contract.
    try:
        from fluid_build.forge.core.plan_digest import compute_contract_digest
    except Exception:  # pragma: no cover — defensive
        # Fallback: hash the raw text. Operators paying attention will
        # see the digest doesn't match local-bundle digests; the path
        # is intentionally narrow (gitpython missing AND core helper
        # unimportable both at once).
        import hashlib

        return "sha256:" + hashlib.sha256(contract_text.encode("utf-8")).hexdigest()

    try:
        contract = load_yaml_safe(contract_text) or {}
        return compute_contract_digest(contract)
    except Exception as exc:  # pragma: no cover — defensive
        LOG.warning(
            "federation_git_digest_failed: workspace=%s product=%s err=%s",
            workspace.id,
            product_id,
            exc,
        )
        return None


def _git_read_contract(workspace: FederatedWorkspace, product_id: str) -> Optional[str]:
    """Read the contract file text from the federated git repo.

    Tries gitpython first (in-process — no fork, surfaces detailed
    errors), then shells out to ``git`` as a fallback. Caches the
    cloned repo under ``~/.cache/fluid/federation-git/<workspace_id>``
    so repeated fetches don't re-clone.

    The cache is keyed by ``workspace.id`` (manifest-stable, and
    validated against :data:`_WORKSPACE_ID_RE` at construction so it is
    a safe path component), not by endpoint URL, so rewriting the
    endpoint without renaming the workspace re-clones into the same
    dir — operators get a clean sync without manual rm-rf.
    """
    cache_dir = Path(os.path.expanduser("~/.cache/fluid/federation-git")) / workspace.id
    cache_dir.parent.mkdir(parents=True, exist_ok=True)

    auth_url = _build_auth_url(workspace)

    # Try gitpython first; fall back to shell-out if it isn't installed
    # OR if the import-level operation fails (e.g. git binary missing).
    used_gitpython = _git_clone_or_pull_via_gitpython(
        auth_url=auth_url, cache_dir=cache_dir, workspace_id=workspace.id
    )
    if used_gitpython is None:
        # gitpython unavailable or failed — fall through to shell-out.
        ok = _git_clone_or_pull_via_shellout(
            auth_url=auth_url, cache_dir=cache_dir, workspace_id=workspace.id
        )
        if not ok:
            return None
    elif used_gitpython is False:
        # gitpython hit a real error, not just unavailability — abort.
        return None

    return _read_first_existing_contract(cache_dir, workspace, product_id)


def _build_auth_url(workspace: FederatedWorkspace) -> str:
    """Inject the auth token into an HTTPS URL when the workspace's
    auth mode requires it. Returns the URL unchanged when no token is
    needed or already embedded."""
    auth_url = workspace.endpoint
    secret = _read_secret_value(workspace.auth_secret_ref)
    if secret and (workspace.auth_mode or "").lower() in (
        "github_token",
        "http_token",
        "basic",
    ):
        if auth_url.startswith("https://") and "@" not in auth_url[8:].split("/", 1)[0]:
            auth_url = auth_url.replace("https://", f"https://x-access-token:{secret}@", 1)
    return auth_url


def _git_clone_or_pull_via_gitpython(
    *, auth_url: str, cache_dir: Path, workspace_id: str
) -> Optional[bool]:
    """Try the in-process gitpython path.

    Return values:

    * ``None`` — gitpython unavailable; caller falls back to shell-out.
    * ``True`` — clone or pull succeeded.
    * ``False`` — gitpython is installed but the operation failed (auth
      reject, dead remote, etc.); caller should abort rather than
      retry via shell-out (the failure mode is the same).
    """
    try:
        from git import GitCommandError, Repo
    except ImportError:
        LOG.debug(
            "federation_git_gitpython_unavailable: workspace=%s — falling back to shell-out",
            workspace_id,
        )
        return None

    try:
        if not cache_dir.exists():
            Repo.clone_from(auth_url, str(cache_dir), depth=1)
            LOG.debug(
                "federation_git_gitpython_cloned: workspace=%s dir=%s",
                workspace_id,
                cache_dir,
            )
        else:
            repo = Repo(str(cache_dir))
            # ``origin`` may not exist on a freshly initialised cache;
            # tolerate that (the contract file already exists from a
            # previous clone, which is fine — pull is best-effort).
            try:
                repo.remotes.origin.fetch(depth=1)
                repo.git.reset("--hard", "origin/HEAD")
                LOG.debug(
                    "federation_git_gitpython_refreshed: workspace=%s",
                    workspace_id,
                )
            except (GitCommandError, AttributeError, ValueError) as exc:
                # ``GitCommandError.__str__`` echoes the full command
                # line, which in HTTPS mode embeds the auth token from
                # the manifest's secret_ref
                # (``https://x-access-token:<TOKEN>@host``). Log only
                # the exception class — never the message body. The
                # shell-out fallback already does this; mirror it here.
                LOG.debug(
                    "federation_git_gitpython_refresh_skipped: workspace=%s "
                    "err=%s — using stale cache",
                    workspace_id,
                    type(exc).__name__,
                )
        return True
    except GitCommandError as exc:
        # See the note above — ``GitCommandError`` stringifies to the
        # token-bearing clone URL. Surface only the class plus a static
        # message; refuse to echo the command line.
        LOG.warning(
            "federation_git_gitpython_failed: workspace=%s err=%s — "
            "git operation failed (authentication or remote error); "
            "refusing to echo the command line",
            workspace_id,
            type(exc).__name__,
        )
        return False
    except Exception as exc:  # pragma: no cover — defensive
        # Non-git exceptions can still wrap the URL in their repr;
        # stay class-only here too for consistency.
        LOG.warning(
            "federation_git_gitpython_unexpected: workspace=%s err=%s",
            workspace_id,
            type(exc).__name__,
        )
        return False


def _git_clone_or_pull_via_shellout(*, auth_url: str, cache_dir: Path, workspace_id: str) -> bool:
    """Shell-out fallback. Returns True on success, False on failure.

    Security: ``auth_url`` is also validated upstream by
    :func:`_validate_federation_endpoint` before reaching this
    function, but we still pass ``--`` before the positional URL so
    git's argv parser is forced to treat it as a URL even if a future
    code path forgets the scheme allow-list. This is defence-in-depth
    against the well-known argument-injection pattern (CVE-2017-1000117
    et al.).
    """
    import subprocess

    if not cache_dir.exists():
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--",
                    auth_url,
                    str(cache_dir),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ) as exc:
            # ``CalledProcessError.__str__`` echoes the full git argv,
            # which embeds the token-bearing clone URL in HTTPS mode.
            # Log only the exception class — never the body — to match
            # the refresh branch below and the gitpython path.
            LOG.warning(
                "federation_git_clone_failed: workspace=%s err=%s — "
                "git clone failed (authentication or remote error); "
                "refusing to echo the command line",
                workspace_id,
                type(exc).__name__,
            )
            return False
    else:
        # ``fetch`` and ``reset --hard`` here read from the cached
        # remote name (``origin``) rather than ``auth_url``, so the
        # argument-injection vector closes with the upstream
        # endpoint validation alone. Adding ``--`` is still cheap and
        # makes the argv shape audit-friendly.
        try:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(cache_dir),
                    "fetch",
                    "--depth",
                    "1",
                    "origin",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(cache_dir),
                    "reset",
                    "--hard",
                    "origin/HEAD",
                ],
                check=False,
                capture_output=True,
                timeout=15,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:  # pragma: no cover
            # Don't include `exc` body — git error messages can echo the
            # remote URL which (in HTTPS mode) embeds the auth token from
            # the manifest's secret_ref. Log only the class.
            LOG.debug("federation_git_refresh_failed: err=%s", type(exc).__name__)
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
    """One drift finding for a federated consumes[] entry."""

    consume_index: int
    upstream_workspace_id: str
    upstream_product_id: str
    expected_digest: str
    actual_digest: str
    reason: str


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
        except NotImplementedError as exc:
            # Skeleton mode — surface as a violation so apply doesn't
            # silently accept an unverified federated digest. Wiring
            # the real fetcher converts this to an actual comparison.
            violations.append(
                FederatedConsumeViolation(
                    consume_index=idx,
                    upstream_workspace_id=str(ws_id),
                    upstream_product_id=str(product_id or "?"),
                    expected_digest=str(expected),
                    actual_digest="",
                    reason=(f"Federation fetcher for kind={ws.kind!r} not yet wired ({exc})."),
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
                )
            )
    return violations


__all__ = [
    "FEDERATION_CACHE_DIRNAME",
    "FEDERATION_MANIFEST_FILENAME",
    "FederatedConsumeViolation",
    "FederatedWorkspace",
    "FederationManifest",
    "FederationSsrfError",
    "fetch_federated_digest",
    "get_cached_digest",
    "load_federation_manifest",
    "store_cached_digest",
    "validate_federated_consumes",
]
