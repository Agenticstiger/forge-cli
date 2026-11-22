# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``vault://<mount>/<path>#<field>[?version=<n>]`` (alias ``openbao``): KV v2 over the Vault API.

OpenBao, the reference store, and HashiCorp Vault speak the same HTTP API, so
one client serves both. It is a few calls on httpx, a core dependency, rather
than hvac: hvac has had no release since 2.4.0 (2025-10-30), OpenBao is not in
its test matrix (hvac#1162), and forge needs five endpoints. The calls and the
response handling follow hvac's ``secrets.kv.v2.read_secret_version`` (a
soft-deleted version answers 404 with ``data.metadata.deletion_time``),
``read_secret_metadata``, ``auth.jwt.jwt_login`` and ``sys.unwrap``.

Everything below comes from the operator's environment. Each name but the two
token variables is protected (``credentials/protected_env.py``), so a project
dotenv file cannot set it. A project file's ``BAO_TOKEN`` or ``VAULT_TOKEN``
loads only while the operator has no token of their own (neither variable, no
``~/.vault-token``: ``protected_env.refused_project_vault_tokens``), so it
never replaces the operator's.

* Address: ``BAO_ADDR``, else ``VAULT_ADDR`` (OpenBao prefers ``BAO_`` and
  falls back to ``VAULT_``). ``https``, or ``http`` to a loopback address only:
  a token never crosses a network in clear text.
* Namespace: ``BAO_NAMESPACE`` / ``VAULT_NAMESPACE`` (``X-Vault-Namespace``).
* CA bundle: ``BAO_CACERT`` / ``VAULT_CACERT``. TLS verification is never
  switched off; ``*_SKIP_VERIFY`` is not read.
* The token, the first that applies:

  1. The token ``fluid ship`` unwrapped from ``FLUID_VAULT_WRAPPED_TOKEN`` for
     its stages: a stage reads it from the pipe ``FLUID_VAULT_SHIP_TOKEN_FD``
     names (``FLUID_VAULT_SHIP_TOKEN`` on Windows) when it starts
     (:mod:`._ship_token`), so it is in no process's environment.
  2. ``FLUID_VAULT_WRAPPED_TOKEN``: a response-wrapped token, Command Center's
     per-run handoff. Unwrapped once; a wrapping token works once, so a
     refused unwrap means it expired or someone else used it.
  3. ``FLUID_VAULT_JWT_ROLE``: a JWT login for CI, with the JWT from
     ``FLUID_VAULT_JWT`` or the file ``FLUID_VAULT_JWT_FILE``, at the auth mount
     ``FLUID_VAULT_JWT_MOUNT`` (default ``jwt``).
  4. ``BAO_TOKEN``, else ``VAULT_TOKEN``.
  5. The default token helper's file, ``~/.vault-token`` (``bao login`` and
     ``vault login`` write it).

  A token forge obtained (2 or 3) is revoked when the run ends
  (``auth/token/revoke-self``); a token the operator supplied (4 or 5) is
  theirs, and is left alone. So is ship's (1): ship revokes it when its own
  run ends.

Requests carry ``X-Vault-Request: true``, go through ``util.safe_http`` (the
address is resolved once and the connection pinned to it) and never follow a
redirect, which could carry ``X-Vault-Token`` to another host. The first path
segment of a reference is the KV v2 mount; nested mounts are not supported.
"""

from __future__ import annotations

import hashlib
import logging
import os
import ssl
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlsplit

from fluid_build._net import is_loopback_host

from . import _ship_token
from .errors import SecretResolveError, resolve_failure
from .ref import SecretRef
from .registry import Existence
from .run import RunState

LOG = logging.getLogger("fluid.secrets")

WRAPPED_TOKEN_ENV = "FLUID_VAULT_WRAPPED_TOKEN"
JWT_ROLE_ENV = "FLUID_VAULT_JWT_ROLE"
JWT_ENV = "FLUID_VAULT_JWT"
JWT_FILE_ENV = "FLUID_VAULT_JWT_FILE"
JWT_MOUNT_ENV = "FLUID_VAULT_JWT_MOUNT"

_TIMEOUT_S = 10.0
_MAX_JWT_BYTES = 64 * 1024
_CLIENTS = "vault.clients"
_TOKENS = "vault.tokens"

#: Digests of wrapping tokens this process already unwrapped: a second run in
#: the same process gets a precise message instead of "someone else used it".
_UNWRAPPED: Set[str] = set()
_UNWRAPPED_LOCK = threading.Lock()


@dataclass(frozen=True)
class VaultConfig:
    """Where the store is, from the operator's environment."""

    address: str
    namespace: Optional[str]
    ca_bundle: Optional[str]

    @property
    def key(self) -> str:
        return f"{self.address}|{self.namespace or ''}"


def _env(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _fail(ref: Optional[SecretRef], why: str, fix: str, reason: str) -> SecretResolveError:
    what = f"{ref.canonical} did not resolve" if ref is not None else "the Vault store refused"
    return resolve_failure(what, why, fix, reason)


def load_config(ref: Optional[SecretRef] = None) -> VaultConfig:
    """The store's address, namespace and CA bundle, or the remedy for a bad address."""
    address = _env("BAO_ADDR", "VAULT_ADDR")
    if address is None:
        raise _fail(
            ref,
            "no store address is set.",
            "Set BAO_ADDR or VAULT_ADDR (https://<host>:8200) in the shell or CI job.",
            "invalid",
        )
    try:
        parts = urlsplit(address)
        host = parts.hostname
        port = parts.port
    except ValueError:
        host, port, parts = None, None, urlsplit("")
    if parts.scheme not in ("http", "https") or not host:
        raise _fail(
            ref,
            "the store address is not an http(s) URL.",
            "Set it to https://<host>:8200.",
            "invalid",
        )
    if parts.username or parts.password or parts.path not in ("", "/") or parts.query:
        raise _fail(
            ref,
            "the store address carries a path, a query or credentials.",
            "Set it to scheme://host[:port] only; the token goes in BAO_TOKEN or a login.",
            "invalid",
        )
    if parts.scheme == "http" and not is_loopback_host(host):
        raise _fail(
            ref,
            "the store address is plain http to a host that is not this machine, so the "
            "token would cross the network in clear text.",
            "Use https://, or a loopback address for a local development server.",
            "invalid",
        )
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return VaultConfig(
        address=f"{parts.scheme}://{netloc}",
        namespace=_env("BAO_NAMESPACE", "VAULT_NAMESPACE"),
        ca_bundle=_env("BAO_CACERT", "VAULT_CACERT"),
    )


def _client(config: VaultConfig, run: RunState) -> Any:
    """One client per store per run, closed when the run ends."""
    clients: Dict[str, Any] = run.scratch.setdefault(_CLIENTS, {})
    existing = clients.get(config.key)
    if existing is not None:
        return existing
    from fluid_build.util.safe_http import safe_httpx_client

    verify: Any = True
    if config.ca_bundle:
        try:
            verify = ssl.create_default_context(cafile=config.ca_bundle)
        except (OSError, ssl.SSLError):
            raise _fail(
                None,
                "the CA bundle in BAO_CACERT / VAULT_CACERT could not be read.",
                "Point it at a readable PEM file.",
                "invalid",
            ) from None
    headers = {"X-Vault-Request": "true"}
    if config.namespace:
        headers["X-Vault-Namespace"] = config.namespace
    try:
        client = safe_httpx_client(
            config.address,
            allow_private=True,
            follow_redirects=False,
            timeout=_TIMEOUT_S,
            verify=verify,
            headers=headers,
        )
    except Exception as exc:
        raise _fail(
            None,
            f"the store address could not be used ({type(exc).__name__}).",
            "Check BAO_ADDR / VAULT_ADDR: the host must resolve, and not to a metadata address.",
            "unreachable",
        ) from None
    clients[config.key] = client
    run.on_close(client.close)
    return client


def _mapping(value: Any) -> Dict[str, Any]:
    """``value`` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _errors_text(body: Dict[str, Any]) -> str:
    from fluid_build.observability.secret_redactor import redact_secret_text

    errors = body.get("errors")
    if not isinstance(errors, list):
        return ""
    text = "; ".join(str(item) for item in errors if item)[:300]
    return redact_secret_text(text)


@dataclass(frozen=True)
class _Request:
    """What one store request sends: the verb, the path and its options.

    The token and the body (a JWT login carries the JWT) stay out of the repr.
    """

    method: str
    path: str
    token: Optional[str] = field(default=None, repr=False)
    json_body: Optional[Dict[str, Any]] = field(default=None, repr=False)
    params: Optional[Dict[str, str]] = None


def _call(
    ref: Optional[SecretRef], config: VaultConfig, run: RunState, request: _Request
) -> Tuple[int, Dict[str, Any]]:
    """One request; transport failures become a :class:`SecretResolveError`."""
    import httpx

    client = _client(config, run)
    headers = {"X-Vault-Token": request.token} if request.token else {}
    try:
        response = client.request(
            request.method,
            request.path,
            headers=headers,
            json=request.json_body,
            params=request.params,
        )
    except httpx.TimeoutException:
        raise _fail(
            ref,
            f"the store at {config.address} did not answer within {_TIMEOUT_S:.0f} s.",
            "Check that it is reachable from here.",
            "unreachable",
        ) from None
    except httpx.HTTPError as exc:
        cause = exc.__cause__ or exc.__context__
        if isinstance(cause, ssl.SSLError) or "CERTIFICATE" in str(exc).upper():
            raise _fail(
                ref,
                f"TLS verification of {config.address} failed.",
                "Set BAO_CACERT / VAULT_CACERT to the CA bundle that signed its certificate.",
                "unreachable",
            ) from None
        raise _fail(
            ref,
            f"the store at {config.address} could not be reached ({type(exc).__name__}).",
            "Check BAO_ADDR / VAULT_ADDR and the network path to it.",
            "unreachable",
        ) from None
    except Exception as exc:  # the safe_http pin hook refuses an address
        raise _fail(
            ref,
            f"the request to {config.address} was refused ({type(exc).__name__}).",
            "Check BAO_ADDR / VAULT_ADDR.",
            "unreachable",
        ) from None
    if 300 <= response.status_code < 400:
        raise _fail(
            ref,
            f"the store answered with a redirect ({response.status_code}), which forge "
            "does not follow because it would carry the token elsewhere.",
            "Point BAO_ADDR / VAULT_ADDR at the active node.",
            "unreachable",
        )
    body: Dict[str, Any] = {}
    if response.content:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            body = parsed
    return response.status_code, body


def _status_error(
    ref: Optional[SecretRef], status: int, body: Dict[str, Any], what: str
) -> SecretResolveError:
    detail = _errors_text(body)
    suffix = f" The store said: {detail}" if detail else ""
    if status == 403:
        return _fail(
            ref,
            f"permission denied: this token's policy does not allow {what}.{suffix}",
            "Grant the run's identity read on that path, or reference a path it may read.",
            "denied",
        )
    if status in (429, 472, 473, 503):
        return _fail(
            ref,
            f"the store is sealed, a standby, or unavailable (HTTP {status}).{suffix}",
            "Unseal it, or point BAO_ADDR / VAULT_ADDR at the active node.",
            "unreachable",
        )
    return _fail(
        ref,
        f"the store answered HTTP {status} for {what}.{suffix}",
        "Check the reference and the store's logs.",
        "error",
    )


# ── tokens ───────────────────────────────────────────────────────────────────


def _register(value: str) -> None:
    from fluid_build.observability.secret_redactor import register_secret

    register_secret(value)


def _token_file() -> Optional[str]:
    try:
        path = Path.home() / ".vault-token"
        info = path.lstat()
    except (OSError, RuntimeError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _client_token(body: Dict[str, Any]) -> Optional[str]:
    auth = body.get("auth")
    if isinstance(auth, dict):
        token = auth.get("client_token")
        if isinstance(token, str) and token:
            return token
    return None


def _unwrap(ref: Optional[SecretRef], config: VaultConfig, run: RunState, wrapped: str) -> str:
    digest = hashlib.sha256(wrapped.encode("utf-8")).hexdigest()
    with _UNWRAPPED_LOCK:
        if digest in _UNWRAPPED:
            raise _fail(
                ref,
                f"this process already unwrapped {WRAPPED_TOKEN_ENV} for an earlier run, and "
                "a wrapping token works once.",
                "The runner hands each run its own wrapped token; start a new process per run.",
                "denied",
            )
    _register(wrapped)
    status, body = _call(
        ref, config, run, _Request("POST", "/v1/sys/wrapping/unwrap", token=wrapped)
    )
    with _UNWRAPPED_LOCK:
        _UNWRAPPED.add(digest)
    token = _client_token(body) if status == 200 else None
    if token is None:
        raise _fail(
            ref,
            f"the store refused to unwrap {WRAPPED_TOKEN_ENV} (HTTP {status}): it expired, it "
            "is not a wrapped token, or it was already unwrapped. If this run did not "
            "unwrap it, something else did: treat the run's credentials as exposed.",
            "Ask the runner for a fresh wrapped token, and check the store's audit log.",
            "denied",
        )
    return token


def _jwt(ref: Optional[SecretRef]) -> str:
    inline = _env(JWT_ENV)
    if inline:
        return inline
    path = _env(JWT_FILE_ENV)
    if path is None:
        raise _fail(
            ref,
            f"{JWT_ROLE_ENV} asks for a JWT login, but neither {JWT_ENV} nor {JWT_FILE_ENV} "
            "is set.",
            f"Set {JWT_FILE_ENV} to the file holding the CI job's OIDC token.",
            "invalid",
        )
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_JWT_BYTES:
            raise OSError("not a small regular file")
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise _fail(
            ref,
            f"the JWT file in {JWT_FILE_ENV} could not be read.",
            "Point it at the file holding the CI job's OIDC token.",
            "invalid",
        ) from None
    if not text:
        raise _fail(
            ref, f"the JWT file in {JWT_FILE_ENV} is empty.", "Write the token first.", "invalid"
        )
    return text


def _jwt_login(ref: Optional[SecretRef], config: VaultConfig, run: RunState, role: str) -> str:
    mount = _env(JWT_MOUNT_ENV) or "jwt"
    if not all(part and not part.startswith(".") for part in mount.split("/")):
        raise _fail(
            ref, f"{JWT_MOUNT_ENV} is not an auth mount path.", "Set it to e.g. jwt.", "invalid"
        )
    jwt = _jwt(ref)
    _register(jwt)
    path = "/v1/auth/" + "/".join(quote(part, safe="") for part in mount.split("/")) + "/login"
    status, body = _call(
        ref, config, run, _Request("POST", path, json_body={"role": role, "jwt": jwt})
    )
    token = _client_token(body) if status == 200 else None
    if token is None:
        detail = _errors_text(body)
        raise _fail(
            ref,
            f"the JWT login for role {role!r} at auth/{mount} was refused (HTTP {status})."
            + (f" The store said: {detail}" if detail else ""),
            "Check the role's bound claims and audience against the CI job's token.",
            "denied",
        )
    return token


def _revoke(config: VaultConfig, run: RunState, token: str) -> None:
    """Revoke a token this run obtained; a failure is a warning, never an error."""
    try:
        status, _ = _call(
            None, config, run, _Request("POST", "/v1/auth/token/revoke-self", token=token)
        )
    except SecretResolveError:
        status = 0
    if status not in (200, 204):
        LOG.warning(
            "could not revoke the store token this run obtained (HTTP %s); it lasts "
            "until its TTL ends",
            status,
        )


def _revoke_later(config: VaultConfig, run: RunState, token: str) -> None:
    run.on_close(lambda: _revoke(config, run, token))


def auth_method() -> str:
    """Which token source applies, for messages and ``fluid doctor``. No secrets."""
    if _ship_token.take():
        return "ship-token"
    if _env(WRAPPED_TOKEN_ENV):
        return "wrapped-token"
    if _env(JWT_ROLE_ENV):
        return "jwt"
    if _env("BAO_TOKEN"):
        return "token:BAO_TOKEN"
    if _env("VAULT_TOKEN"):
        return "token:VAULT_TOKEN"
    if _token_file():
        return "token-helper-file"
    return "none"


def token_for(ref: Optional[SecretRef], config: VaultConfig, run: RunState) -> str:
    """The run's token for this store, obtaining it once per run."""
    tokens: Dict[str, str] = run.scratch.setdefault(_TOKENS, {})
    cached = tokens.get(config.key)
    if cached is not None:
        return cached
    shipped = _ship_token.take()
    wrapped = _env(WRAPPED_TOKEN_ENV)
    role = _env(JWT_ROLE_ENV)
    if shipped is not None:
        token = shipped  # fluid ship unwrapped it, and revokes it when its run ends
    elif wrapped is not None:
        token = _unwrap(ref, config, run, wrapped)
        _revoke_later(config, run, token)
    elif role is not None:
        token = _jwt_login(ref, config, run, role)
        _revoke_later(config, run, token)
    else:
        found = _env("BAO_TOKEN", "VAULT_TOKEN") or _token_file()
        if found is None:
            raise _fail(
                ref,
                "no store token was found.",
                f"Sign in (bao login or vault login), or set BAO_TOKEN / VAULT_TOKEN; in CI set "
                f"{JWT_ROLE_ENV}; a runner hands over {WRAPPED_TOKEN_ENV}.",
                "denied",
            )
        token = found
    _register(token)
    tokens[config.key] = token
    return token


# ── KV v2 ────────────────────────────────────────────────────────────────────


def _kv_path(ref: SecretRef, kind: str) -> str:
    mount, _, path = ref.locator.partition("/")
    return f"/v1/{quote(mount, safe='')}/{kind}/{quote(path, safe='/@=+')}"


class VaultResolver:
    """KV v2 read and metadata read against OpenBao or Vault."""

    scheme = "vault"

    def resolve(self, ref: SecretRef, run: RunState) -> str:
        config = load_config(ref)
        token = token_for(ref, config, run)
        params = {"version": ref.version} if ref.version is not None else None
        status, body = _call(
            ref, config, run, _Request("GET", _kv_path(ref, "data"), token=token, params=params)
        )
        mount, _, path = ref.locator.partition("/")
        if status == 404:
            metadata = _mapping(_mapping(body.get("data")).get("metadata"))
            if metadata.get("destroyed"):
                why = "that version was destroyed."
            elif metadata.get("deletion_time"):
                why = "that version was deleted (it can be undeleted)."
            else:
                why = (
                    f"no secret exists at {path} in the KV v2 mount {mount!r} "
                    "(or the mount is not KV v2)."
                )
            raise _fail(
                ref, why, "Check the path and mount, or write the secret first.", "not_found"
            )
        if status != 200:
            raise _status_error(ref, status, body, f"read on {mount}/data/{path}")
        data = body.get("data")
        secret = data.get("data") if isinstance(data, dict) else None
        if not isinstance(secret, dict):
            raise _fail(
                ref,
                f"the store's answer has no KV v2 data (is {mount!r} a KV v2 mount?).",
                "Reference a KV v2 mount; this resolver reads KV v2 only.",
                "invalid",
            )
        key = ref.field or ""
        if key not in secret:
            raise _fail(
                ref,
                f"the secret has no key {key!r}.",
                "Correct the #field, or add the key to the secret.",
                "not_found",
            )
        value = secret[key]
        if not isinstance(value, str):
            raise _fail(
                ref,
                f"key {key!r} holds a {type(value).__name__}, not a string.",
                "Store the credential as a string.",
                "invalid",
            )
        return value

    def exists(self, ref: SecretRef, run: RunState) -> Existence:
        config = load_config(ref)
        token = token_for(ref, config, run)
        status, body = _call(
            ref, config, run, _Request("GET", _kv_path(ref, "metadata"), token=token)
        )
        mount, _, path = ref.locator.partition("/")
        if status == 404:
            return Existence(False, "no secret at that path")
        if status == 403:
            return Existence(
                None,
                f"this token may not read the metadata (it needs read on {mount}/metadata/{path})",
            )
        if status != 200:
            return Existence(None, _status_error(ref, status, body, "the metadata read").why)
        data = _mapping(body.get("data"))
        versions = _mapping(data.get("versions"))
        target = ref.version or str(data.get("current_version") or "")
        version = versions.get(target) if target else None
        if not isinstance(version, dict):
            return Existence(False, f"version {target or '?'} does not exist")
        if version.get("destroyed"):
            return Existence(False, f"version {target} was destroyed")
        if version.get("deletion_time"):
            return Existence(False, f"version {target} was deleted")
        return Existence(True, f"version {target} exists")


# ── fluid doctor ─────────────────────────────────────────────────────────────


def describe() -> Dict[str, Any]:
    """The store as ``fluid doctor`` reports it: never a token, never a value.

    A wrapped token is never unwrapped and no JWT login is made here: either
    would spend a credential meant for a run. Only an operator-supplied token
    is looked up, for its TTL.
    """
    if _env("BAO_ADDR", "VAULT_ADDR") is None:
        return {"configured": False}
    out: Dict[str, Any] = {"configured": True, "auth_method": auth_method()}
    try:
        config = load_config()
    except SecretResolveError as exc:
        out["error"] = exc.why
        return out
    out.update({"address": config.address, "namespace": config.namespace})
    run = RunState()
    try:
        status, body = _call(
            None,
            config,
            run,
            _Request(
                "GET", "/v1/sys/health", params={"standbyok": "true", "perfstandbyok": "true"}
            ),
        )
        out["reachable"] = True
        out["initialized"] = body.get("initialized")
        out["sealed"] = body.get("sealed")
        out["version"] = body.get("version")
        if out["auth_method"].startswith("token") or out["auth_method"] == "token-helper-file":
            token = _env("BAO_TOKEN", "VAULT_TOKEN") or _token_file()
            if token:
                _register(token)
                status, body = _call(
                    None, config, run, _Request("GET", "/v1/auth/token/lookup-self", token=token)
                )
                data = _mapping(body.get("data"))
                if status == 200:
                    out["token_ttl_seconds"] = data.get("ttl")
                    policies: List[str] = [str(p) for p in data.get("policies") or []]
                    out["token_policies"] = policies
                else:
                    out["token_error"] = f"lookup-self answered HTTP {status}"
    except SecretResolveError as exc:
        out["reachable"] = False
        out["error"] = exc.why
    finally:
        run.close()
    return out
