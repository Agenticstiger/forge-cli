# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An in-process Lakekeeper (v0.14.0 shapes) and token endpoint, for ``httpx.MockTransport``.

It answers only what ``fluid policy apply`` calls, with the request and response
shapes of Lakekeeper's management and REST catalog OpenAPI documents at v0.14.0,
and keeps every request it saw so a test can say what was sent, and what was not.
It is strict where Lakekeeper is: a write of an assignment that is already held
and a delete of one that is not are refused, so a plan that is not a diff fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote

import httpx

BASE = "https://lakekeeper.test"
CATALOG_URI = f"{BASE}/catalog"
TOKEN_URL = "https://keycloak.test/realms/fluid/protocol/openid-connect/token"
CLIENT_ID = "fluid-apply-sales"
CLIENT_SECRET = "fake-client-secret-not-a-secret"  # pragma: allowlist secret
TOKEN = "fake-lakekeeper-access-token-not-a-secret"  # pragma: allowlist secret
WAREHOUSE = "sales-wh"
WAREHOUSE_ID = "0190c5b4-0000-7000-8000-00000000a001"


@dataclass
class FakeLakekeeper:
    """The state of one fake server; ``transport()`` serves it."""

    tables: Dict[Tuple[Tuple[str, ...], str], str] = field(default_factory=dict)
    namespaces: Set[Tuple[str, ...]] = field(default_factory=set)
    users: List[Dict[str, Any]] = field(default_factory=list)
    roles: List[Dict[str, Any]] = field(default_factory=list)
    #: table id -> set of (type, kind, id)
    assignments: Dict[str, Set[Tuple[str, str, str]]] = field(default_factory=dict)
    requests: List[Tuple[str, str, Any]] = field(default_factory=list)
    page_size: int = 100
    conflicts: int = 0
    assignments_status: Optional[int] = None
    redirect_whoami: bool = False
    #: Lakekeeper refuses every token (an audience or issuer it does not trust).
    refuse_tokens: bool = False

    # ── setup ────────────────────────────────────────────────────────────

    def add_table(self, namespace: Tuple[str, ...], name: str, table_id: str) -> str:
        self.namespaces.add(namespace)
        self.tables[(namespace, name)] = table_id
        self.assignments.setdefault(table_id, set())
        return table_id

    def add_user(self, user_id: str, name: str, email: Optional[str] = None) -> None:
        self.users.append({"id": user_id, "name": name, "email": email, "user-type": "human"})

    def add_role(self, role_id: str, name: str) -> None:
        self.roles.append({"id": role_id, "name": name, "ident": f"lakekeeper~{role_id}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), follow_redirects=False)

    # ── what was sent ────────────────────────────────────────────────────

    def posts_to_assignments(self) -> List[Any]:
        return [
            body
            for method, path, body in self.requests
            if method == "POST" and path.endswith("/assignments")
        ]

    def held(self, table_id: str) -> Set[Tuple[str, str, str]]:
        return set(self.assignments.get(table_id, set()))

    # ── the server ───────────────────────────────────────────────────────

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = unquote(url.raw_path.decode().split("?", 1)[0])
        body: Any = None
        if request.content:
            ctype = request.headers.get("content-type", "")
            if "json" in ctype:
                body = json.loads(request.content)
            else:
                body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((request.method, f"{url.scheme}://{url.host}{path}", body))

        if str(url).startswith(TOKEN_URL):
            return self._token(body or {})
        if self.refuse_tokens or request.headers.get("authorization") != f"Bearer {TOKEN}":
            return _error(401, "UnauthorizedError", "invalid token")
        if url.host != "lakekeeper.test":
            return _error(404, "NotFound", "no such host")

        query = {k: v[0] for k, v in parse_qs(url.query.decode()).items()}
        if path == "/management/v1/whoami":
            if self.redirect_whoami:
                return httpx.Response(302, headers={"location": "https://elsewhere.test/"})
            return httpx.Response(200, json={"id": "oidc~admin", "name": "lab-admin"})
        if path == "/management/v1/warehouse":
            return httpx.Response(
                200,
                json={
                    "warehouses": [
                        {"id": WAREHOUSE_ID, "warehouse-id": WAREHOUSE_ID, "name": WAREHOUSE}
                    ]
                },
            )
        if path == "/catalog/v1/config":
            if query.get("warehouse") != WAREHOUSE:
                return _error(404, "WarehouseNotFound", "no warehouse")
            # v0.14.0 sends the prefix in ``defaults`` (seen live on the lab, 2026-11-18).
            return httpx.Response(
                200,
                json={
                    "defaults": {"prefix": WAREHOUSE_ID, "rest-page-size": "100"},
                    "overrides": {"uri": CATALOG_URI},
                },
            )
        prefix = f"/catalog/v1/{WAREHOUSE_ID}/namespaces/"
        if path.startswith(prefix) and path.endswith("/tables"):
            return self._list_tables(path[len(prefix) : -len("/tables")], query)
        if path.startswith("/management/v1/permissions/warehouse/") and path.endswith(
            "/assignments"
        ):
            table_id = path.split("/table/", 1)[1].rsplit("/assignments", 1)[0]
            return self._assignments(request.method, table_id, body)
        if path == "/management/v1/search/user":
            # Fuzzy: on the lab, searching "service-account-trino" returned every
            # service account and no trino at all. Every user is the worst case.
            return httpx.Response(200, json={"users": list(self.users)})
        if path.startswith("/management/v1/user/"):
            user_id = path[len("/management/v1/user/") :]
            for user in self.users:
                if user["id"] == user_id:
                    return httpx.Response(200, json=user)
            return _error(404, "UserNotFound", "no such user")
        if path == "/management/v1/search/role":
            return httpx.Response(200, json={"roles": list(self.roles)})
        if path.startswith("/management/v1/role/"):
            role_id = path[len("/management/v1/role/") :]
            for role in self.roles:
                if role["id"] == role_id:
                    return httpx.Response(200, json=role)
            return _error(404, "RoleNotFound", "no such role")
        return _error(404, "NotFound", f"no route {path}")

    def _token(self, form: Dict[str, str]) -> httpx.Response:
        if (
            form.get("grant_type") != "client_credentials"
            or form.get("client_id") != CLIENT_ID
            or form.get("client_secret") != CLIENT_SECRET
        ):
            return httpx.Response(
                401,
                json={
                    "error": "unauthorized_client",
                    "error_description": "Invalid client or Invalid client credentials",
                },
            )
        if form.get("scope") != "lakekeeper":
            return httpx.Response(400, json={"error": "invalid_scope"})
        return httpx.Response(
            200, json={"access_token": TOKEN, "token_type": "Bearer", "expires_in": 900}
        )

    def _list_tables(self, raw_ns: str, query: Dict[str, str]) -> httpx.Response:
        namespace = tuple(raw_ns.split("\x1f"))
        if namespace not in self.namespaces:
            return _error(404, "NoSuchNamespaceException", "namespace does not exist")
        names = sorted((n, tid) for (ns, n), tid in self.tables.items() if ns == namespace)
        start = int(query.get("pageToken") or 0)
        page = names[start : start + self.page_size]
        doc: Dict[str, Any] = {
            "identifiers": [{"namespace": list(namespace), "name": n} for n, _ in page],
        }
        if query.get("returnUuids") == "true":
            doc["table-uuids"] = [tid for _, tid in page]
        if start + self.page_size < len(names):
            doc["next-page-token"] = str(start + self.page_size)
        return httpx.Response(200, json=doc)

    def _assignments(self, method: str, table_id: str, body: Any) -> httpx.Response:
        if self.assignments_status is not None:
            return _error(self.assignments_status, "Forbidden", "not allowed")
        held = self.assignments.setdefault(table_id, set())
        if method == "GET":
            return httpx.Response(
                200, json={"assignments": [{"type": t, k: i} for t, k, i in sorted(held)]}
            )
        if self.conflicts:
            self.conflicts -= 1
            return _error(409, "ConcurrentModification", "retry")
        if set(body) - {"writes", "deletes"}:
            return _error(400, "BadRequest", "unknown field")
        writes = [_entry(e) for e in body.get("writes") or []]
        deletes = [_entry(e) for e in body.get("deletes") or []]
        if any(e is None for e in writes + deletes):
            return _error(400, "BadRequest", "exactly one of user and role")
        if any(e in held for e in writes) or any(e not in held for e in deletes):
            return _error(400, "TupleConflict", "write of a held tuple or delete of a missing one")
        held.update(writes)
        held.difference_update(deletes)
        return httpx.Response(204)


def _entry(item: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    kinds = [k for k in ("user", "role") if item.get(k)]
    if len(kinds) != 1 or not item.get("type"):
        return None
    return (item["type"], kinds[0], item[kinds[0]])


def _error(status: int, kind: str, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": status, "type": kind, "message": message}}
    )
