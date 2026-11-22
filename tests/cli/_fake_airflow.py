# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A fake Airflow REST API on 127.0.0.1, in the shape of Airflow 2.10 or Airflow 3.

Real HTTP on a loopback port, so the client under test runs its whole transport. The
responses copy what real servers answered (Airflow 2.10.5 with the ``basic_auth`` API
backend, Airflow 3.0.6 and 3.4.0 with the simple auth manager, recorded on localhost):

* Airflow 2: ``/api/v1`` (``version`` and ``health`` answer without credentials), HTTP
  Basic, ``application/problem+json`` errors, absolute ``fileloc`` and import error
  ``filename``; ``/api/v2`` and ``/auth/token`` are an HTML 404.
* Airflow 3: ``/api/v2`` with a JWT from ``POST /auth/token`` (201 ``{"access_token"}``);
  no credentials and Basic get 401 ``{"detail": "Not authenticated"}``, an unknown token
  403 ``{"detail": "Invalid JWT token"}``; every ``/api/v1`` path is a 404 JSON saying it
  was removed. ``bundle_name`` / ``bundle_version`` / ``relative_fileloc`` on Dags. The
  import error ``filename`` is absolute on 3.0 and relative to its bundle on 3.4;
  ``/api/v2/dagBundles`` exists on 3.4, not on 3.0 (404).

Time is simulated: :attr:`FakeAirflow.now` is the server clock, sent in every ``Date``
header and stamped on parses, and moves only when a test calls :meth:`advance`. A test
plays the Dag processor with :meth:`parse_ok` and :meth:`parse_fail`.
"""

from __future__ import annotations

import base64
import email.utils
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

_V1_ERROR_TYPE = "https://airflow.apache.org/docs/apache-airflow/2.10.5/stable-rest-api-ref.html"
_V1_REMOVED = (
    "/api/v1 has been removed in Airflow 3, please use its upgraded version /api/v2 instead."
)
_HTML_404 = '<!DOCTYPE html>\n<html lang="en"><head><title>Airflow 404</title></head></html>'


@dataclass
class _Dag:
    dag_id: str
    rel: str
    bundle: str
    version: Optional[str] = None
    last_parsed: Optional[datetime] = None
    import_errors: bool = False
    stale: bool = False


@dataclass
class _Error:
    error_id: int
    rel: str
    bundle: str
    timestamp: datetime
    stack: str


@dataclass
class _Bundle:
    name: str
    version: Optional[str]
    last_refreshed: datetime


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]]
    authorization: str
    body: Any = None


@dataclass
class FakeAirflow:
    """One fake Airflow; use it as a context manager to serve it."""

    major: int
    version: str = ""
    users: Dict[str, str] = field(default_factory=lambda: {"admin": "admin"})
    #: Bearer tokens the server accepts without a login (a token issued elsewhere).
    static_tokens: Tuple[str, ...] = ()
    #: Airflow 3.4 serves ``/api/v2/dagBundles``; 3.0 does not. ``None``: by version.
    bundles_endpoint: Optional[bool] = None
    #: ``False`` plays a token without ``IMPORT_ERRORS_ALL`` on Airflow 3: an import
    #: error of a file that has no registered Dag is left out of ``/importErrors``.
    sees_unregistered_errors: bool = True
    #: ``True`` plays a token that may read DAGs but no import error at all.
    hide_errors: bool = False
    #: Paths (prefix match) answered 403.
    forbidden: Tuple[str, ...] = ()
    dags_folder: str = "/opt/airflow/dags"
    page_cap: int = 100

    def __post_init__(self) -> None:
        self.version = self.version or ("2.10.5" if self.major == 2 else "3.4.0")
        if self.bundles_endpoint is None:
            self.bundles_endpoint = not self.version.startswith("3.0.")
        self.now = datetime(2026, 11, 21, 12, 0, 0, 250000, tzinfo=timezone.utc)
        self.dags: Dict[str, _Dag] = {}
        self.errors: Dict[Tuple[str, str], _Error] = {}
        self.bundles: Dict[str, _Bundle] = {}
        self.requests: List[Request] = []
        self.issued: List[str] = []
        self.answer_next: List[Tuple[int, Dict[str, str], str]] = []
        self.redirect_to: Optional[str] = None
        self._next_error_id = 1
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── test controls ────────────────────────────────────────────────────

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def parse_ok(
        self, dag_id: str, rel: str, *, bundle: str = "dags-folder", version: Optional[str] = None
    ) -> None:
        """A clean parse of the file ``rel`` that defines ``dag_id``."""
        with self._lock:
            dag = self.dags.get(dag_id) or _Dag(dag_id=dag_id, rel=rel, bundle=bundle)
            dag.rel, dag.bundle, dag.version = rel, bundle, version
            dag.last_parsed, dag.import_errors, dag.stale = self.now, False, False
            self.dags[dag_id] = dag
            self.errors.pop((bundle, rel), None)

    def parse_fail(
        self, rel: str, *, bundle: str = "dags-folder", stack: str = "ModuleNotFoundError: x"
    ) -> None:
        """A failed parse of ``rel``: its import error is stamped now, its Dags flagged."""
        with self._lock:
            key = (bundle, rel)
            error = self.errors.get(key)
            if error is None:
                error = _Error(self._next_error_id, rel, bundle, self.now, stack)
                self._next_error_id += 1
            error.timestamp, error.stack = self.now, stack
            self.errors[key] = error
            for dag in self.dags.values():
                if dag.rel == rel and dag.bundle == bundle:
                    dag.import_errors = True
                    dag.stale = self.major >= 3

    def set_bundle(self, name: str, version: Optional[str]) -> None:
        with self._lock:
            self.bundles[name] = _Bundle(name, version, self.now)

    def expire_tokens(self) -> None:
        with self._lock:
            self.issued.clear()

    @property
    def url(self) -> str:
        assert self._server is not None, "use FakeAirflow as a context manager"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def paths(self) -> List[str]:
        return [f"{r.method} {r.path}" for r in self.requests]

    # ── serving ──────────────────────────────────────────────────────────

    def __enter__(self) -> "FakeAirflow":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def do_GET(self) -> None:
                fake._handle(self, "GET")

            def do_POST(self) -> None:
                fake._handle(self, "POST")

            def do_PATCH(self) -> None:
                fake._handle(self, "PATCH")

            def date_time_string(self, timestamp: Optional[float] = None) -> str:
                return email.utils.format_datetime(fake.now, usegmt=True)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()

    # ── request handling ─────────────────────────────────────────────────

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(handler.path)
        body: Any = None
        length = int(handler.headers.get("Content-Length") or 0)
        if length:
            raw = handler.rfile.read(length)
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw.decode("utf-8", "replace")
        request = Request(
            method,
            parts.path,
            parse_qs(parts.query),
            handler.headers.get("Authorization", ""),
            body,
        )
        with self._lock:
            self.requests.append(request)
            canned = self.answer_next.pop(0) if self.answer_next else None
        if canned is not None:
            status, headers, text = canned
            return self._send(handler, status, text, headers.get("Content-Type", "text/plain"))
        if self.redirect_to is not None:
            return self._send(handler, 307, "", "text/plain", {"Location": self.redirect_to})
        status, payload, content_type = (
            self._route_v2(request) if self.major >= 3 else self._route_v1(request)
        )
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self._send(handler, status, text, content_type)

    @staticmethod
    def _send(
        handler: BaseHTTPRequestHandler,
        status: int,
        text: str,
        content_type: str,
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        data = text.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        for key, value in (extra or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(data)

    # ── Airflow 2: /api/v1 ───────────────────────────────────────────────

    def _problem(self, status: int, title: str, detail: Any = None) -> Tuple[int, Any, str]:
        kind = {401: "Unauthenticated", 403: "PermissionDenied", 404: "NotFound"}.get(status, "")
        doc = {
            "detail": detail,
            "status": status,
            "title": title,
            "type": f"{_V1_ERROR_TYPE}#{kind}",
        }
        return status, doc, "application/problem+json"

    def _route_v1(self, request: Request) -> Tuple[int, Any, str]:
        path = request.path
        if not path.startswith("/api/v1/"):
            return 404, _HTML_404, "text/html; charset=utf-8"
        if path == "/api/v1/version":
            return (
                200,
                {"git_version": ".release:b93c3db6", "version": self.version},
                "application/json",
            )
        if path == "/api/v1/health":
            return 200, {"metadatabase": {"status": "healthy"}}, "application/json"
        if not self._v1_authorized(request.authorization):
            return self._problem(401, "Unauthorized")
        if any(path.startswith(p) for p in self.forbidden):
            return self._problem(403, "Forbidden", "You do not have permission")
        if path.startswith("/api/v1/dags/"):
            dag = self.dags.get(path[len("/api/v1/dags/") :])
            if dag is None:
                return self._problem(404, "DAG not found", "The DAG with dag_id: x was not found")
            return 200, self._v1_dag(dag), "application/json"
        if path == "/api/v1/dags":
            items = [self._v1_dag(d) for d in self.dags.values()]
            return 200, self._page(request, items, "dags"), "application/json"
        if path == "/api/v1/importErrors":
            items = [
                {
                    "filename": f"{self.dags_folder}/{e.rel}",
                    "import_error_id": e.error_id,
                    "stack_trace": e.stack,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in self.errors.values()
            ]
            return 200, self._page(request, items, "import_errors"), "application/json"
        return self._problem(404, "Not Found")

    def _v1_authorized(self, header: str) -> bool:
        if header.startswith("Basic "):
            user, _, password = base64.b64decode(header[6:]).decode().partition(":")
            return self.users.get(user) == password
        if header.startswith("Bearer "):
            return header[7:] in self.static_tokens
        return False

    def _v1_dag(self, dag: _Dag) -> Dict[str, Any]:
        return {
            "dag_id": dag.dag_id,
            "dag_display_name": dag.dag_id,
            "fileloc": f"{self.dags_folder}/{dag.rel}",
            "file_token": "Ii4uLiI.token",
            "has_import_errors": dag.import_errors,
            "is_active": True,
            "is_paused": True,
            "last_parsed_time": dag.last_parsed.isoformat() if dag.last_parsed else None,
            "schedule_interval": {"__type": "CronExpression", "value": "0 2 * * *"},
            "tags": [],
        }

    # ── Airflow 3: /api/v2 ───────────────────────────────────────────────

    def _route_v2(self, request: Request) -> Tuple[int, Any, str]:
        path = request.path
        if path.startswith("/api/v1/"):
            return 404, {"error": _V1_REMOVED}, "application/json"
        if path == "/auth/token" and request.method == "POST":
            return self._login(request.body)
        if path == "/auth/token":
            return 403, {"detail": "This method is only allowed if all admins"}, "application/json"
        if not path.startswith("/api/v2/"):
            return 404, {"detail": "Not Found"}, "application/json"
        if path == "/api/v2/version":
            return (
                200,
                {"version": self.version, "git_version": ".release:619a787a"},
                "application/json",
            )
        if path == "/api/v2/monitor/health":
            return 200, {"metadatabase": {"status": "healthy"}}, "application/json"
        auth = request.authorization
        if not auth.startswith("Bearer "):
            return 401, {"detail": "Not authenticated"}, "application/json"
        token = auth[7:]
        if token not in self.issued and token not in self.static_tokens:
            return 403, {"detail": "Invalid JWT token"}, "application/json"
        if any(path.startswith(p) for p in self.forbidden):
            return 403, {"detail": "Forbidden"}, "application/json"
        if path.startswith("/api/v2/dags/"):
            dag_id = path[len("/api/v2/dags/") :]
            dag = self.dags.get(dag_id)
            if dag is None:
                return (
                    404,
                    {"detail": f"The Dag with ID: `{dag_id}` was not found"},
                    "application/json",
                )
            return 200, self._v2_dag(dag), "application/json"
        if path == "/api/v2/dags":
            # exclude_stale defaults to true: a DAG whose file failed to parse drops out.
            keep_stale = (request.query.get("exclude_stale") or ["true"])[0].lower() == "false"
            items = [self._v2_dag(d) for d in self.dags.values() if keep_stale or not d.stale]
            return 200, self._page(request, items, "dags"), "application/json"
        if path == "/api/v2/importErrors":
            registered = {(d.bundle, d.rel) for d in self.dags.values()}
            absolute = self.version.startswith("3.0.")
            items = [
                {
                    "import_error_id": e.error_id,
                    "timestamp": e.timestamp.isoformat().replace("+00:00", "Z"),
                    "filename": f"{self._root(e.bundle)}/{e.rel}" if absolute else e.rel,
                    "bundle_name": e.bundle,
                    "stack_trace": e.stack,
                }
                for key, e in self.errors.items()
                if not self.hide_errors and (self.sees_unregistered_errors or key in registered)
            ]
            return 200, self._page(request, items, "import_errors"), "application/json"
        if path == "/api/v2/dagBundles" and self.bundles_endpoint:
            items = [
                {
                    "name": b.name,
                    "active": True,
                    "version": b.version,
                    "last_refreshed": b.last_refreshed.isoformat().replace("+00:00", "Z"),
                    "bundle_url": None,
                    "team_name": None,
                    "import_error_count": 0,
                }
                for b in self.bundles.values()
            ]
            return 200, self._page(request, items, "dag_bundles"), "application/json"
        return 404, {"detail": "Not Found"}, "application/json"

    def _login(self, body: Any) -> Tuple[int, Any, str]:
        if not isinstance(body, dict) or "password" not in body or "username" not in body:
            return 422, {"detail": "1 validation error for LoginBody"}, "application/json"
        if self.users.get(body["username"]) != body["password"]:
            return 401, {"detail": "Invalid credentials"}, "application/json"
        token = f"jwt-{len(self.issued) + 1}-{body['username']}"
        self.issued.append(token)
        return 201, {"access_token": token}, "application/json"

    def _v2_dag(self, dag: _Dag) -> Dict[str, Any]:
        return {
            "dag_id": dag.dag_id,
            "dag_display_name": dag.dag_id,
            "is_paused": True,
            "is_stale": dag.stale,
            "last_parsed_time": (
                dag.last_parsed.isoformat().replace("+00:00", "Z") if dag.last_parsed else None
            ),
            "bundle_name": dag.bundle,
            "bundle_version": dag.version,
            "relative_fileloc": dag.rel,
            "fileloc": f"{self._root(dag.bundle)}/{dag.rel}",
            "has_import_errors": dag.import_errors,
            "timetable_summary": "0 2 * * *",
            "tags": [],
            "file_token": "Ii4uLiI.token",
        }

    def _root(self, bundle: str) -> str:
        """Where a bundle's files are on the server: one name per file, for its Dags and
        its import errors alike (as on real 2.10.5, 3.0.6 and 3.4.0)."""
        return self.dags_folder if bundle == "dags-folder" else f"/opt/airflow/bundles/{bundle}"

    # ── shared ───────────────────────────────────────────────────────────

    def _page(self, request: Request, items: List[Any], key: str) -> Dict[str, Any]:
        limit = min(int((request.query.get("limit") or ["100"])[0]), self.page_cap)
        offset = int((request.query.get("offset") or ["0"])[0])
        return {key: items[offset : offset + limit], "total_entries": len(items)}
