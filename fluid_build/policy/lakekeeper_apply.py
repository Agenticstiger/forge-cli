# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid policy apply`` onto Lakekeeper: read each table's grants, apply the difference.

``cli/policy_apply.py`` hands this module the ``provider: lakekeeper`` bindings
that ``policy/lakekeeper.py`` compiles. Per table it reads the direct
assignments Lakekeeper holds, plans the grants and revokes that make the data
relations (describe, select, modify) equal what the contract declares, and in
``enforce`` mode sends them in one request per table. ``check`` mode makes the
same reads and the same plan and sends nothing.

Lakekeeper's HTTP API, v0.14.0 (docs/docs/api/management-open-api.yaml and
rest-catalog-open-api.yaml at that tag):

* ``GET  /management/v1/warehouse`` -- the warehouse id for its name;
* ``GET  /catalog/v1/config?warehouse=<name>`` -- the catalog prefix;
* ``GET  /catalog/v1/{prefix}/namespaces/{ns}/tables?returnUuids=true`` -- the
  table id (paginated; ``table-uuids`` runs parallel to ``identifiers``);
* ``GET|POST /management/v1/permissions/warehouse/{wid}/table/{tid}/assignments``
  -- the table's direct assignments, and ``{"writes": [...], "deletes": [...]}``;
* ``POST /management/v1/search/user`` / ``search/role``, ``GET /user/{id}`` /
  ``/role/{id}`` -- a principal's id from its name, or that its id exists:
  Lakekeeper does not check a user id it is given a grant for, so a mistyped id
  would be granted silently and help no one;
* ``GET  /management/v1/whoami`` -- who forge signed in as, for the report.

The OpenFGA assignments API (tag ``permissions-openfga``) is the one used: v0.14.0
marks the authorizer-independent ``/grants`` API Preview ("may change in a
backward-incompatible way"), and the fluid-fabric-lab bootstrap proved these
assignment calls live on v0.14.0. OpenFGA tuples are never written directly;
Lakekeeper writes them, with the structural edges it keeps beside them. The
diff-then-one-request shape is the one Lakekeeper's own Terraform provider
documents for its assignment resources (lakekeeper/terraform-provider-lakekeeper,
MPL-2.0: the idea is borrowed, no code).

Credentials are the operator's, never the contract's: the names below are
operator-only (``FLUID_LAKEKEEPER_`` in ``credentials/protected_env.py``), and
each holds a secret REFERENCE (``env://``, ``file://``, ``vault://``,
``aws-sm://``), resolved through :func:`fluid_build.secrets.resolve_credential`.
The contract does name where the token goes (``binding.location.uri``), so the
reference must be allowed for that host by the destination policy
(``FLUID_ACQUISITION_SECRET_REF_ALLOWLIST``, ``secrets/policy.py``) before it is
read; a loopback host needs no entry. Both URLs must be https, a loopback one
too, as for ``sink.catalogAuth``. Redirects are not followed. The resolved
secret and the access token are registered with the exact-value redactor.

* ``FLUID_LAKEKEEPER_CREDENTIAL_REF`` -- ``client_id:client_secret``, the shape of
  Iceberg's ``credential`` catalog property, for an OAuth2 client-credentials
  token from ``FLUID_LAKEKEEPER_OAUTH2_SERVER_URI`` with ``FLUID_LAKEKEEPER_SCOPE``
  (a Keycloak-fronted Lakekeeper: the scope whose audience mapper names
  Lakekeeper, ``lakekeeper`` in its examples);
* or ``FLUID_LAKEKEEPER_TOKEN_REF`` -- a bearer token, used as it is.

The identity needs ``manage_grants`` on the tables (Lakekeeper gives it to a
warehouse or namespace owner, and to a project's ``security_admin``), and the
right to list the namespaces' tables and search users and roles.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import SplitResult, quote, urlsplit, urlunsplit

from .lakekeeper import MANAGED_RELATIONS, PROVIDER, Assignment, plan_changes

LOG = logging.getLogger("fluid.policy.lakekeeper")

CREDENTIAL_REF_ENV = "FLUID_LAKEKEEPER_CREDENTIAL_REF"
TOKEN_URI_ENV = "FLUID_LAKEKEEPER_OAUTH2_SERVER_URI"
SCOPE_ENV = "FLUID_LAKEKEEPER_SCOPE"
TOKEN_REF_ENV = "FLUID_LAKEKEEPER_TOKEN_REF"

#: Lakekeeper serves the Iceberg REST catalog under ``/catalog`` and its
#: management API under ``/management`` of the same base URL.
_CATALOG_SUFFIX = "/catalog"

#: Lakekeeper (OpenFGA) writes at most 100 tuples per request
#: (authorization-openfga.md: ``OPENFGA_MAX_TUPLES_PER_WRITE`` >= 100).
_MAX_CHANGES_PER_REQUEST = 100

#: A 409 is a concurrent change (retryable): re-read and re-plan this often.
_CONFLICT_ATTEMPTS = 3

#: Pages of a namespace's tables, at most: a loop guard, not a limit anyone meets.
_MAX_PAGES = 1000

_TIMEOUT_SECONDS = 30.0
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class LakekeeperError(Exception):
    """What went wrong, said for the operator; ``fix`` is the next step."""

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix

    def __str__(self) -> str:
        text = super().__str__()
        return f"{text} {self.fix}" if self.fix else text


class _Conflict(Exception):
    """Lakekeeper answered 409: another change landed first; re-read and re-plan."""


@dataclass(frozen=True)
class Settings:
    """The operator's Lakekeeper credential settings (references, never values)."""

    credential_ref: str = ""
    token_uri: str = ""
    scope: str = ""
    token_ref: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Settings":
        def get(name: str) -> str:
            return str(environ.get(name) or "").strip()

        return cls(
            credential_ref=get(CREDENTIAL_REF_ENV),
            token_uri=get(TOKEN_URI_ENV),
            scope=get(SCOPE_ENV),
            token_ref=get(TOKEN_REF_ENV),
        )

    @property
    def configured(self) -> bool:
        return bool(self.credential_ref or self.token_ref)

    @property
    def ref(self) -> str:
        return self.token_ref or self.credential_ref

    def problem(self) -> Optional[str]:
        """Why these settings cannot sign in, or ``None``."""
        if self.credential_ref and self.token_ref:
            return f"set {CREDENTIAL_REF_ENV} or {TOKEN_REF_ENV}, not both"
        if self.credential_ref:
            missing = [
                n for n, v in ((TOKEN_URI_ENV, self.token_uri), (SCOPE_ENV, self.scope)) if not v
            ]
            if missing:
                return f"{CREDENTIAL_REF_ENV} needs {' and '.join(missing)} as well"
        return None


@dataclass
class _Report:
    """What one run did or would do, for the console and the result event."""

    mode: str
    lines: List[str] = field(default_factory=list)
    grants: List[Dict[str, str]] = field(default_factory=list)
    revokes: List[Dict[str, str]] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    unchanged: int = 0
    tables: int = 0

    def result(self) -> Dict[str, Any]:
        status = "error" if self.errors else "ok"
        return {
            "status": status,
            "mode": self.mode,
            "target": PROVIDER,
            "tables": self.tables,
            "grants": self.grants,
            "revokes": self.revokes,
            "unchanged": self.unchanged,
            "pending": self.pending,
            "errors": self.errors,
            "applied": self.mode == "enforce" and not self.errors,
        }


def _clean(text: Any, limit: int = 300) -> str:
    """A server's text, safe to show: control characters spelled as ``?``, bounded."""
    shown = _CONTROL_CHARS.sub("?", str(text or "")).strip()
    return shown if len(shown) <= limit else shown[: limit - 3] + "..."


def _error_text(resp: Any) -> str:
    """``HTTP <status> <type>: <message>`` from an Iceberg or OAuth2 error body.

    Only the error's type and message are shown, never the raw body or a stack.
    """
    status = f"HTTP {resp.status_code}"
    try:
        doc = resp.json()
    except Exception:  # not JSON: the status says enough
        return status
    if not isinstance(doc, dict):
        return status
    err = doc.get("error")
    if isinstance(err, dict):  # Iceberg / Lakekeeper: {"error": {"type", "message", "code"}}
        detail = ": ".join(_clean(err.get(k)) for k in ("type", "message") if err.get(k))
    elif isinstance(err, str):  # OAuth2 (RFC 6749 section 5.2)
        detail = _clean(err)
        if doc.get("error_description"):
            detail += f" ({_clean(doc['error_description'])})"
    else:
        detail = ""
    return f"{status} {detail}".strip()


def _https(url: str, source: str) -> SplitResult:
    """``urlsplit(url)`` for an https URL with a plain host; else :class:`LakekeeperError`."""
    if "{{" in url or "${" in url:
        raise LakekeeperError(
            f"{source} holds an unrendered template (the value is not shown), so the host "
            "that would receive the token cannot be checked.",
            "Write the URL itself, in the environment's overlay.",
        )
    parts: Optional[SplitResult]
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        parts, host = None, ""
    if parts is None or parts.scheme != "https":
        raise LakekeeperError(
            f"{source} must be an https:// URL: policy-apply sends a bearer token there, and "
            "http would carry it in clear text (a loopback host too).",
            "Serve Lakekeeper and the token endpoint over TLS (a private CA works: set "
            "REQUESTS_CA_BUNDLE or SSL_CERT_FILE to its bundle).",
        )
    if "@" in parts.netloc:
        raise LakekeeperError(
            f"{source} carries a user:password@ part (not shown).",
            "Remove it: the credential belongs in the operator's secret reference.",
        )
    if not host:
        raise LakekeeperError(f"{source} names no host.")
    return parts


def service_base(catalog_uri: str) -> str:
    """Lakekeeper's base URL from the catalog uri a binding names (``.../catalog``)."""
    parts = _https(catalog_uri, "binding.location.uri")
    path = parts.path.rstrip("/")
    if path.endswith(_CATALOG_SUFFIX):
        path = path[: -len(_CATALOG_SUFFIX)]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _new_client() -> Any:
    """The HTTP client: TLS verified (certifi, or REQUESTS_CA_BUNDLE / SSL_CERT_FILE),
    environment proxies, no redirects followed, GETs retried on 429/502/503/504.

    A test seam. ``util.http_client`` imports httpx, so it is imported here,
    never on the ``fluid --help`` path.
    """
    from fluid_build.util.http_client import new_client

    return new_client(
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=False,
        retry={"total": 3, "backoff_factor": 0.5, "status_forcelist": (429, 502, 503, 504)},
        headers={"Accept": "application/json", "User-Agent": "fluid-policy-apply"},
    )


def _console_say(line: str) -> None:
    """The console, literally: a name or id never reads as Rich markup."""
    from fluid_build._console import cprint_literal

    cprint_literal(line)


def _register(value: str) -> None:
    from fluid_build.observability.secret_redactor import register_secret

    register_secret(value)


def _send(client: Any, method: str, url: str, *, what: str, **kw: Any) -> Any:
    """``client.request``, with a transport failure said as a :class:`LakekeeperError`."""
    import httpx

    try:
        return client.request(method, url, **kw)
    except httpx.HTTPError as exc:
        host = urlsplit(url).hostname or "?"
        raise LakekeeperError(
            f"{what}: could not reach {host} ({type(exc).__name__}: {_clean(exc, 200)}).",
            "Check the URL and the network; for a private CA set REQUESTS_CA_BUNDLE or "
            "SSL_CERT_FILE to its bundle.",
        ) from None


def _resolve_ref(ref: str, name: str, host: str, environ: Mapping[str, str]) -> str:
    """The value behind the operator's reference, once ``host`` may receive it.

    The destination policy runs first, so a refused reference is never read.
    """
    from fluid_build.secrets import SecretError, refuse_unless_allowed, resolve_credential

    try:
        refuse_unless_allowed(ref, host=host, environ=environ)
        value = resolve_credential(ref, environ=environ).get_secret_value()
    except SecretError as exc:
        raise LakekeeperError(f"{name}: {exc}") from None
    value = str(value or "")
    if not value:
        raise LakekeeperError(f"{name} resolved to an empty value.")
    _register(value)
    return value


def _sign_in(client: Any, settings: Settings, catalog_host: str, environ: Mapping[str, str]) -> str:
    """A bearer token for Lakekeeper: the operator's token, or a client-credentials one."""
    if settings.token_ref:
        return _resolve_ref(settings.token_ref, TOKEN_REF_ENV, catalog_host, environ)
    token_parts = _https(settings.token_uri, TOKEN_URI_ENV)
    credential = _resolve_ref(settings.credential_ref, CREDENTIAL_REF_ENV, catalog_host, environ)
    client_id, sep, secret = credential.partition(":")
    if not sep or not client_id or not secret:
        raise LakekeeperError(
            f"{CREDENTIAL_REF_ENV} must resolve to client_id:client_secret (Iceberg's "
            "credential shape); the value is not shown.",
        )
    _register(secret)
    resp = _send(
        client,
        "POST",
        urlunsplit(token_parts),
        what="token request",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": settings.scope,
        },
    )
    if resp.status_code != 200:
        raise LakekeeperError(
            f"the token endpoint ({token_parts.hostname}) refused client {client_id!r}: "
            f"{_error_text(resp)}.",
            f"Check the client's secret and that it may request scope {settings.scope!r}.",
        )
    try:
        token = resp.json().get("access_token")
    except Exception:
        token = None
    if not isinstance(token, str) or not token:
        raise LakekeeperError(
            f"the token endpoint ({token_parts.hostname}) answered without an access_token."
        )
    _register(token)
    return token


class _Lakekeeper:
    """One Lakekeeper server, signed in: lookups, cached for the run."""

    def __init__(self, client: Any, base: str, token: str) -> None:
        self._client = client
        self.base = base
        self._auth = {"Authorization": f"Bearer {token}"}
        self._warehouses: Dict[str, Tuple[str, str]] = {}
        self._principals: Dict[str, Tuple[str, str, str]] = {}
        self._names: Dict[Tuple[str, str], str] = {}

    # ── transport ────────────────────────────────────────────────────────

    def _call(self, method: str, path: str, *, what: str, **kw: Any) -> Any:
        resp = _send(self._client, method, self.base + path, what=what, headers=self._auth, **kw)
        if 300 <= resp.status_code < 400:
            raise LakekeeperError(
                f"{what}: Lakekeeper answered with a redirect (HTTP {resp.status_code}), which "
                "policy-apply does not follow with a token.",
                "Point binding.location.uri at the catalog itself.",
            )
        if resp.status_code == 401:
            raise LakekeeperError(
                f"{what}: Lakekeeper refused the token ({_error_text(resp)}).",
                f"Check {SCOPE_ENV}: the token must carry the audience Lakekeeper expects "
                "(LAKEKEEPER__OPENID_AUDIENCE), from the issuer it trusts.",
            )
        return resp

    def _json(self, resp: Any, what: str) -> Any:
        try:
            return resp.json()
        except Exception:
            raise LakekeeperError(
                f"{what}: Lakekeeper answered HTTP {resp.status_code} "
                "with a body that is not JSON."
            ) from None

    def _refused(self, resp: Any, what: str, needs: str) -> LakekeeperError:
        if resp.status_code == 403:
            return LakekeeperError(
                f"{what}: Lakekeeper refused the identity forge signed in as "
                f"({_error_text(resp)}).",
                f"It needs {needs}.",
            )
        return LakekeeperError(f"{what}: {_error_text(resp)}.")

    # ── who ──────────────────────────────────────────────────────────────

    def whoami(self) -> str:
        """``name (id)`` of the identity forge signed in as; ``""`` if Lakekeeper will not say."""
        resp = self._call("GET", "/management/v1/whoami", what="whoami")
        if resp.status_code != 200:
            return ""
        doc = self._json(resp, "whoami")
        if not isinstance(doc, dict) or not doc.get("id"):
            return ""
        return f"{_clean(doc.get('name') or '?', 80)} ({_clean(doc['id'], 120)})"

    # ── where ────────────────────────────────────────────────────────────

    def warehouse(self, name: str) -> Tuple[str, str]:
        """``(warehouse id, catalog prefix)`` for a warehouse name."""
        if name in self._warehouses:
            return self._warehouses[name]
        what = f"warehouse {name!r}"
        resp = self._call("GET", "/management/v1/warehouse", what=what)
        if resp.status_code != 200:
            raise self._refused(resp, what, "permission to list the project's warehouses")
        listed = (self._json(resp, what) or {}).get("warehouses") or []
        found = [w for w in listed if isinstance(w, dict) and w.get("name") == name]
        if not found:
            raise LakekeeperError(
                f"Lakekeeper has no warehouse {name!r} that the identity forge signed in as "
                "can see (Lakekeeper hides one a caller may not see).",
                "Check binding.location.warehouse, and the identity's access to the warehouse.",
            )
        warehouse_id = str(found[0].get("warehouse-id") or found[0].get("id") or "")
        resp = self._call("GET", "/catalog/v1/config", what=what, params={"warehouse": name})
        if resp.status_code != 200:
            raise self._refused(resp, f"{what} catalog config", "access to the warehouse")
        doc = self._json(resp, what) or {}
        prefix = (doc.get("overrides") or {}).get("prefix") or (doc.get("defaults") or {}).get(
            "prefix"
        )
        if not warehouse_id or not prefix:
            raise LakekeeperError(f"{what}: Lakekeeper returned no warehouse id or prefix.")
        self._warehouses[name] = (warehouse_id, str(prefix))
        return self._warehouses[name]

    def table_id(self, prefix: str, namespace: List[str], table: str) -> Optional[str]:
        """The table's Lakekeeper id, or ``None`` when the table (or namespace) is not there."""
        ns = quote("\x1f".join(namespace), safe="")
        path = f"/catalog/v1/{quote(prefix, safe='')}/namespaces/{ns}/tables"
        what = f"tables of namespace {'.'.join(namespace)!r}"
        params: Dict[str, str] = {"returnUuids": "true"}
        for _ in range(_MAX_PAGES):
            resp = self._call("GET", path, what=what, params=params)
            if resp.status_code == 404:
                return None
            if resp.status_code != 200:
                raise self._refused(resp, what, "permission to list the namespace's tables")
            doc = self._json(resp, what) or {}
            idents = doc.get("identifiers") or []
            raw_uuids = doc.get("table-uuids")
            uuids: List[Any] = raw_uuids if isinstance(raw_uuids, list) else []
            if len(uuids) != len(idents):
                raise LakekeeperError(
                    f"{what}: Lakekeeper returned no table ids (returnUuids); this needs a "
                    "Lakekeeper that answers it (v0.14.0 does)."
                )
            for ident, table_uuid in zip(idents, uuids, strict=True):
                if isinstance(ident, dict) and ident.get("name") == table:
                    return str(table_uuid)
            token = doc.get("next-page-token")
            if not token:
                return None
            params = {"returnUuids": "true", "pageToken": str(token)}
        raise LakekeeperError(f"{what}: more than {_MAX_PAGES} pages of tables.")

    # ── grants ───────────────────────────────────────────────────────────

    def _assignments_path(self, warehouse_id: str, table_id: str) -> str:
        return (
            f"/management/v1/permissions/warehouse/{quote(warehouse_id, safe='')}"
            f"/table/{quote(table_id, safe='')}/assignments"
        )

    def assignments(self, warehouse_id: str, table_id: str, what: str) -> List[Assignment]:
        """Every direct assignment on the table, as ``(relation, kind, id)``."""
        resp = self._call("GET", self._assignments_path(warehouse_id, table_id), what=what)
        if resp.status_code != 200:
            raise self._refused(resp, what, _GRANT_RIGHTS)
        out: List[Assignment] = []
        for item in (self._json(resp, what) or {}).get("assignments") or []:
            if not isinstance(item, dict) or not item.get("type"):
                continue
            for kind in ("user", "role"):
                if item.get(kind):
                    out.append((str(item["type"]), kind, str(item[kind])))
        return out

    def update(
        self,
        warehouse_id: str,
        table_id: str,
        writes: List[Assignment],
        deletes: List[Assignment],
        what: str,
    ) -> None:
        """Send the changes: grants before revokes, at most 100 per request.

        A table's changes fit one request (atomic in Lakekeeper) unless they pass
        100; then the grants go first, so a principal whose relation changes is
        never left with none.
        """
        changes = [("writes", a) for a in writes] + [("deletes", a) for a in deletes]
        for start in range(0, len(changes), _MAX_CHANGES_PER_REQUEST):
            body: Dict[str, List[Dict[str, str]]] = {"writes": [], "deletes": []}
            for key, (relation, kind, principal_id) in changes[
                start : start + _MAX_CHANGES_PER_REQUEST
            ]:
                body[key].append({"type": relation, kind: principal_id})
            resp = self._call(
                "POST", self._assignments_path(warehouse_id, table_id), what=what, json=body
            )
            if resp.status_code == 409:
                raise _Conflict()
            if resp.status_code not in (200, 204):
                raise self._refused(resp, what, _GRANT_RIGHTS)

    # ── principals ───────────────────────────────────────────────────────

    def principal(self, identity: str) -> Tuple[str, str, str]:
        """``(kind, id, note)`` for a Lakekeeper identity (``user:``/``role:``...)."""
        if identity not in self._principals:
            prefix, _, value = identity.partition(":")
            if prefix in ("user", "serviceAccount"):
                self._principals[identity] = self._user(value)
            elif prefix in ("role", "group"):
                self._principals[identity] = self._role(value)
            else:
                raise LakekeeperError(f"{identity!r} is not a Lakekeeper principal.")
        return self._principals[identity]

    def _user(self, value: str) -> Tuple[str, str, str]:
        if "~" in value:  # a Lakekeeper user id: oidc~<subject>, kubernetes~<uid>, ...
            what = f"user {value!r}"
            resp = self._call("GET", f"/management/v1/user/{quote(value, safe='')}", what=what)
            if resp.status_code == 200:
                doc = self._json(resp, what) or {}
                self._names[("user", value)] = _clean(doc.get("name") or "", 80)
                return "user", value, ""
            if resp.status_code == 404:
                raise LakekeeperError(
                    f"Lakekeeper has no user {value!r}: a user exists there once it has signed "
                    "in, and a grant to an id it does not know would reach no one.",
                    "Sign in once with that identity, or map the principal to the right id.",
                )
            return "user", value, f"not verified: {_error_text(resp)}"
        what = f"user {value!r}"
        resp = self._call(
            "POST", "/management/v1/search/user", what=what, json={"search": value[:64]}
        )
        if resp.status_code != 200:
            raise self._refused(resp, what, "permission to search users (or map it to user:<id>)")
        users = (self._json(resp, what) or {}).get("users") or []
        exact = [
            u
            for u in users
            if isinstance(u, dict)
            and u.get("id")
            and (u.get("name") == value or str(u.get("email") or "").casefold() == value.casefold())
        ]
        if len(exact) > 1:
            ids = sorted(str(u["id"]) for u in exact)
            raise LakekeeperError(
                f"{len(exact)} Lakekeeper users are named {value!r} or have that e-mail: {ids}.",
                "Map the principal to one of them by id (user:<id>) in binding.principals.",
            )
        if not exact:
            raise LakekeeperError(
                f"no Lakekeeper user is named {value!r} or has that e-mail. A user appears in "
                "Lakekeeper once it has signed in; a Keycloak client's service account is "
                "named service-account-<client-id>.",
                "Map the principal in binding.principals to the user's name or id "
                "(user:oidc~<subject>).",
            )
        user_id = str(exact[0]["id"])
        self._names[("user", user_id)] = _clean(exact[0].get("name") or "", 80)
        return "user", user_id, ""

    def _role(self, value: str) -> Tuple[str, str, str]:
        what = f"role {value!r}"
        if _is_uuid(value):
            resp = self._call("GET", f"/management/v1/role/{value}", what=what)
            if resp.status_code == 200:
                self._names[("role", value)] = _clean(
                    (self._json(resp, what) or {}).get("name"), 80
                )
                return "role", value, ""
            if resp.status_code == 404:
                raise LakekeeperError(f"Lakekeeper has no role with id {value!r}.")
            raise self._refused(resp, what, "permission to read roles")
        resp = self._call(
            "POST", "/management/v1/search/role", what=what, json={"search": value[:64]}
        )
        if resp.status_code != 200:
            raise self._refused(resp, what, "permission to search roles (or map it to role:<id>)")
        roles = (self._json(resp, what) or {}).get("roles") or []
        exact = [r for r in roles if isinstance(r, dict) and r.get("id") and r.get("name") == value]
        if len(exact) > 1:
            ids = sorted(str(r["id"]) for r in exact)
            raise LakekeeperError(
                f"{len(exact)} Lakekeeper roles are named {value!r}: {ids}.",
                "Map the principal to one of them by id (role:<id>) in binding.principals.",
            )
        if not exact:
            raise LakekeeperError(
                f"Lakekeeper has no role named {value!r}. With OpenFGA, Lakekeeper keeps its "
                "own roles and reads no group from the identity provider.",
                "Create the role in Lakekeeper and add its members, or map the principal in "
                "binding.principals.",
            )
        role_id = str(exact[0]["id"])
        self._names[("role", role_id)] = value
        return "role", role_id, ""

    def name_of(self, kind: str, principal_id: str) -> str:
        """The principal's Lakekeeper name, for a revoke the contract does not name."""
        key = (kind, principal_id)
        if key not in self._names:
            path = f"/management/v1/{kind}/{quote(principal_id, safe='')}"
            try:
                resp = self._call("GET", path, what=f"{kind} {principal_id}")
                doc = self._json(resp, kind) if resp.status_code == 200 else {}
            except LakekeeperError:
                doc = {}
            self._names[key] = _clean((doc or {}).get("name") or "", 80)
        return self._names[key]


_GRANT_RIGHTS = (
    "manage_grants on the table, its namespace or its warehouse (an owner has it unless "
    "managed access is on), or security_admin on the project"
)


def _shown_host(uri: str) -> str:
    """The host of a contract's catalog uri, for a message: never its userinfo or path."""
    try:
        host = urlsplit(uri).hostname
    except ValueError:
        host = None
    return _clean(host, 120) if host else "(binding.location.uri)"


def _malformed(bindings: List[Mapping[str, Any]]) -> Optional[str]:
    """Why these bindings cannot be a table's desired grants, or ``None``.

    A binding with no identity or no relation would read as "nobody may read this
    table", and the plan would revoke every grant on it, so it is refused instead.
    """
    for binding in bindings:
        identities = binding.get("identities")
        relations = binding.get("relations")
        if not isinstance(identities, list) or not identities:
            return f"the binding for {binding.get('principal')!r} names no identities"
        if not isinstance(relations, list) or not relations:
            return f"the binding for {binding.get('principal')!r} names no relations"
        unknown = [r for r in relations if r not in MANAGED_RELATIONS]
        if unknown:
            return f"the binding for {binding.get('principal')!r} names relations {unknown}"
    return None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _table_key(binding: Mapping[str, Any]) -> Tuple[str, str, Tuple[str, ...], str]:
    return (
        str(binding.get("catalog_uri") or ""),
        str(binding.get("warehouse") or ""),
        tuple(str(n) for n in binding.get("namespace") or ()),
        str(binding.get("table") or ""),
    )


def _table_name(key: Tuple[str, str, Tuple[str, ...], str]) -> str:
    return f"{key[1]}/{'.'.join([*key[2], key[3]])}"


def apply_lakekeeper_bindings(
    bindings: Iterable[Mapping[str, Any]],
    *,
    mode: str,
    environ: Optional[Mapping[str, str]] = None,
    client: Any = None,
    say: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Apply (``enforce``) or plan (``check``) the Lakekeeper grants; the result event.

    ``status`` is ``ok``, ``error`` (a table could not be checked or changed; the
    others still were), or ``noop`` (check mode with no credential configured).
    """
    import os

    emit: Callable[[str], None] = say if say is not None else _console_say
    environ = os.environ if environ is None else environ
    report = _Report(mode=mode)

    tables: "OrderedDict[Tuple[str, str, Tuple[str, ...], str], List[Mapping[str, Any]]]"
    tables = OrderedDict()
    for binding in bindings:
        tables.setdefault(_table_key(binding), []).append(binding)
    report.tables = len(tables)

    settings = Settings.from_env(environ)
    if not settings.configured:
        reason = (
            f"no Lakekeeper credential is configured: set {CREDENTIAL_REF_ENV} (with "
            f"{TOKEN_URI_ENV} and {SCOPE_ENV}) or {TOKEN_REF_ENV}"
        )
        if mode == "check":
            emit(f"Lakekeeper: {len(tables)} table(s) not checked, {reason}.")
            result = report.result()
            result.update(status="noop", note=reason)
            return result
        report.errors.append(f"{reason}; no grant was applied.")
        emit(f"Lakekeeper: {report.errors[-1]}")
        return report.result()
    problem = settings.problem()
    if problem:
        report.errors.append(f"Lakekeeper credential settings: {problem}.")
        emit(report.errors[-1])
        return report.result()

    by_catalog: "OrderedDict[str, List[Any]]" = OrderedDict()
    for key in tables:
        by_catalog.setdefault(key[0], []).append(key)

    own_client = client is None
    try:
        http = _new_client() if own_client else client
    except OSError as exc:  # util.http_client.CaBundleError: a CA bundle that does not load
        report.errors.append(f"Lakekeeper: {_clean(exc)}; no grant was applied.")
        emit(report.errors[-1])
        return report.result()
    try:
        for catalog_uri, keys in by_catalog.items():
            try:
                base = service_base(catalog_uri)
                host = urlsplit(base).hostname or ""
                server = _Lakekeeper(http, base, _sign_in(http, settings, host, environ))
                who = server.whoami()
            except LakekeeperError as exc:
                for key in keys:
                    report.errors.append(f"{_table_name(key)}: {exc}")
                emit(f"Lakekeeper {_shown_host(catalog_uri)}: {exc}")
                continue
            emit(f"Lakekeeper {base}" + (f": signed in as {who}" if who else ""))
            for key in keys:
                _apply_table(server, key, tables[key], report, emit)
    finally:
        if own_client:
            http.close()

    _summarise(report, emit)
    return report.result()


def _apply_table(
    server: _Lakekeeper,
    key: Tuple[str, str, Tuple[str, ...], str],
    bindings: List[Mapping[str, Any]],
    report: _Report,
    say: Callable[[str], None],
) -> None:
    name = _table_name(key)
    enforce = report.mode == "enforce"
    try:
        problem = _malformed(bindings)
        if problem:
            raise LakekeeperError(
                f"nothing was changed on this table: {problem}.",
                "Recompile the bindings with fluid policy compile (or fluid generate artifacts).",
            )
        warehouse_id, prefix = server.warehouse(key[1])
        table_id = server.table_id(prefix, list(key[2]), key[3])
        if table_id is None:
            count = sum(len(b.get("relations") or ()) for b in bindings)
            if not enforce:
                report.pending.append(name)
                say(
                    f"  ? {name} does not exist in Lakekeeper yet: its {count} grant(s) are "
                    "applied once it does (fluid apply creates it)."
                )
                return
            raise LakekeeperError(
                f"the table does not exist in Lakekeeper, so its {count} grant(s) cannot be "
                "applied.",
                "Run fluid apply first (it creates the table), then policy-apply again.",
            )
        desired: List[Assignment] = []
        labels: Dict[Tuple[str, str], str] = {}
        unresolved: List[str] = []
        for binding in bindings:
            for identity in binding.get("identities") or ():
                try:
                    kind, principal_id, note = server.principal(str(identity))
                except LakekeeperError as exc:
                    unresolved.append(f"{binding.get('principal')}: {exc}")
                    continue
                label = f"{binding.get('principal')} ({kind} {principal_id})"
                labels[(kind, principal_id)] = label + (f", {note}" if note else "")
                for relation in binding.get("relations") or ():
                    desired.append((str(relation), kind, principal_id))
        if unresolved:
            # The plan would revoke whatever the unresolved principal holds: change nothing.
            raise LakekeeperError(
                "nothing was changed on this table, because a principal could not be "
                "resolved: " + "; ".join(unresolved)
            )
        what = f"grants on {name}"
        for _ in range(_CONFLICT_ATTEMPTS):
            current = server.assignments(warehouse_id, table_id, what)
            writes, deletes = plan_changes(desired, current)
            if not enforce or not (writes or deletes):
                break
            try:
                server.update(warehouse_id, table_id, writes, deletes, what)
                break
            except _Conflict:
                continue
        else:
            raise LakekeeperError(
                f"{what}: Lakekeeper kept answering 409 (a concurrent change).",
                "Run policy-apply again.",
            )
    except LakekeeperError as exc:
        report.errors.append(f"{name}: {exc}")
        say(f"  x {name}: {exc}")
        return

    verb_grant, verb_revoke = ("grant", "revoke") if enforce else ("would grant", "would revoke")
    for relation, kind, principal_id in writes:
        label = labels.get((kind, principal_id), f"{kind} {principal_id}")
        report.grants.append(
            {"table": name, "relation": relation, "kind": kind, "id": principal_id}
        )
        say(f"  + {name}: {verb_grant} {relation} to {label}")
    for relation, kind, principal_id in deletes:
        known = labels.get((kind, principal_id))
        if known is None:
            shown = server.name_of(kind, principal_id)
            known = f"{kind} {principal_id}" + (f" ({shown})" if shown else "")
        label = known
        report.revokes.append(
            {"table": name, "relation": relation, "kind": kind, "id": principal_id}
        )
        say(f"  - {name}: {verb_revoke} {relation} from {label}")
    kept = len(set(desired)) - len(writes)
    report.unchanged += kept
    if not writes and not deletes:
        say(f"  = {name}: {kept} grant(s) already in place")


def _summarise(report: _Report, say: Callable[[str], None]) -> None:
    grants, revokes = len(report.grants), len(report.revokes)
    tail = (
        f"{grants} grant(s) and {revokes} revoke(s) applied, {report.unchanged} unchanged"
        if report.mode == "enforce"
        else f"{grants} grant(s) and {revokes} revoke(s) to apply, {report.unchanged} unchanged; "
        "check mode changed nothing (--mode enforce applies them)"
    )
    extra = ""
    if report.pending:
        extra += f", {len(report.pending)} table(s) not created yet"
    if report.errors:
        extra += f", {len(report.errors)} table(s) failed"
    say(f"Lakekeeper: {report.tables} table(s): {tail}{extra}.")


__all__ = [
    "CREDENTIAL_REF_ENV",
    "LakekeeperError",
    "SCOPE_ENV",
    "Settings",
    "TOKEN_REF_ENV",
    "TOKEN_URI_ENV",
    "apply_lakekeeper_bindings",
    "service_base",
]
