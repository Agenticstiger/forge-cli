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

This is a SKELETON — the stubs raise ``NotImplementedError`` for
the actual fetch/auth paths. The schema + dispatcher + cache layer
are real and tested. Wiring in a concrete fetcher (gitpython for
``git_registry``, a REST client for ``catalog`` / ``http_registry``)
is a follow-up PR per integration.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

LOG = logging.getLogger("fluid.forge.federation")

FEDERATION_MANIFEST_FILENAME = "federation/upstreams.yaml"
FEDERATION_CACHE_DIRNAME = "federation"

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
            doc = yaml.safe_load(f) or {}
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
        LOG.warning("federation_cache_write_failed: path=%s error=%s", path, exc)


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
    import os

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
    """
    try:
        import urllib.error
        import urllib.request
    except ImportError:  # pragma: no cover — stdlib
        return None

    base = workspace.endpoint.rstrip("/")
    url = f"{base}/{product_id}/{version}/digest"
    req = urllib.request.Request(url, method="GET")

    secret = _read_secret_value(workspace.auth_secret_ref)
    mode = (workspace.auth_mode or "none").lower()
    if secret:
        if mode == "basic":
            req.add_header("Authorization", f"Basic {secret}")
        elif mode in ("oidc", "bearer", "http_token", "token"):
            req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("Accept", "text/plain")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        LOG.warning(
            "federation_http_fetch_http_error: workspace=%s url=%s status=%s",
            workspace.id,
            url,
            exc.code,
        )
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        LOG.warning(
            "federation_http_fetch_network_error: workspace=%s url=%s err=%s",
            workspace.id,
            url,
            exc,
        )
        return None

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
    """
    try:
        import urllib.error
        import urllib.request
    except ImportError:  # pragma: no cover
        return None

    base = workspace.endpoint.rstrip("/")
    url = f"{base}/products/{product_id}/versions/{version}"
    req = urllib.request.Request(url, method="GET")

    secret = _read_secret_value(workspace.auth_secret_ref)
    mode = (workspace.auth_mode or "none").lower()
    if secret and mode in ("oidc", "bearer", "http_token", "token", "catalog_token"):
        req.add_header("Authorization", f"Bearer {secret}")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        LOG.warning(
            "federation_catalog_fetch_http_error: workspace=%s status=%s",
            workspace.id,
            exc.code,
        )
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        LOG.warning(
            "federation_catalog_fetch_error: workspace=%s err=%s",
            workspace.id,
            exc,
        )
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
        contract = yaml.safe_load(contract_text) or {}
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

    The cache is keyed by ``workspace.id`` (manifest-stable), not by
    endpoint URL, so rewriting the endpoint without renaming the
    workspace re-clones into the same dir — operators get a clean
    sync without manual rm-rf.
    """
    import os

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
            "federation_git_gitpython_unavailable: workspace=%s — falling " "back to shell-out",
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
                LOG.debug(
                    "federation_git_gitpython_refresh_skipped: workspace=%s "
                    "err=%s — using stale cache",
                    workspace_id,
                    exc,
                )
        return True
    except GitCommandError as exc:
        LOG.warning(
            "federation_git_gitpython_failed: workspace=%s err=%s",
            workspace_id,
            exc,
        )
        return False
    except Exception as exc:  # pragma: no cover — defensive
        LOG.warning(
            "federation_git_gitpython_unexpected: workspace=%s err=%s",
            workspace_id,
            exc,
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
            LOG.warning(
                "federation_git_clone_failed: workspace=%s err=%s",
                workspace_id,
                exc,
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
            LOG.debug("federation_git_refresh_failed: err=%s", exc)
    return True


def _read_first_existing_contract(
    cache_dir: Path, workspace: FederatedWorkspace, product_id: str
) -> Optional[str]:
    """Resolve the product path inside a cloned repo. Convention:
    ``<product_id>/contract.fluid.yaml``. Tolerates dot-paths that
    map to nested directories."""
    candidate_paths = [
        cache_dir / product_id / "contract.fluid.yaml",
        cache_dir / product_id.replace(".", "/") / "contract.fluid.yaml",
        cache_dir / f"{product_id}.fluid.yaml",
    ]
    for path in candidate_paths:
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
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
                    reason=(f"Federation fetcher for kind={ws.kind!r} not " f"yet wired ({exc})."),
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
    "fetch_federated_digest",
    "get_cached_digest",
    "load_federation_manifest",
    "store_cached_digest",
    "validate_federated_consumes",
]
