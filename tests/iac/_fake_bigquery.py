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

"""A minimal in-process stand-in for the BigQuery v2 REST API, for real ``tofu`` runs.

``tofu apply`` against the goccy bigquery-emulator crashes inside
terraform-provider-google (the emulator returns a dataset without ``selfLink``,
and ``resource_bigquery_dataset.go`` asserts it is a string), so the emulator
cannot hold the state a governance test needs. This server stores exactly what
the provider sends for datasets and tables and returns it with the output-only
fields the provider reads (ids, ``selfLink``, ``etag``, timestamps), and a new
dataset gets the default access entries BigQuery gives one (the project's owners,
writers and readers, and the creator). Nothing else is emulated: no query, no
load, no IAM evaluation, no Cloud KMS and no Data Catalog. What it proves is what
the real provider and ``tofu`` plan for a live table, which is where the
data-loss gate decides; what BigQuery itself accepts is not proven here.

The provider is pointed at it with ``bigquery_custom_endpoint`` and a dummy
``access_token``, so no credential and no Google endpoint is involved.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_DATASET_RE = re.compile(r"^/bigquery/v2/projects/([^/]+)/datasets/?([^/]*)$")
_TABLE_RE = re.compile(r"^/bigquery/v2/projects/([^/]+)/datasets/([^/]+)/tables/?([^/]*)$")

#: What BigQuery puts on a dataset created without ``access`` (BigQuery
#: documentation, "Dataset access": the project's basic roles and the creator).
DEFAULT_ACCESS = [
    {"role": "OWNER", "specialGroup": "projectOwners"},
    {"role": "OWNER", "userByEmail": "creator@fake.iam.gserviceaccount.com"},
    {"role": "WRITER", "specialGroup": "projectWriters"},
    {"role": "READER", "specialGroup": "projectReaders"},
]


#: BigQuery returns the basic dataset roles by their legacy names, whatever it was
#: sent (terraform-provider-google's ``iam_bigquery_dataset.go``: "API changes
#: certain IAM roles to legacy roles").
_LEGACY_ROLES = {
    "roles/bigquery.dataOwner": "OWNER",
    "roles/bigquery.dataEditor": "WRITER",
    "roles/bigquery.dataViewer": "READER",
}


def _legacy_access(dataset: Dict[str, Any]) -> None:
    for entry in dataset.get("access") or []:
        if isinstance(entry, dict) and entry.get("role") in _LEGACY_ROLES:
            entry["role"] = _LEGACY_ROLES[entry["role"]]


class FakeBigQuery:
    """Datasets and tables by ``(project, dataset[, table])``; every request is logged."""

    def __init__(self) -> None:
        self.datasets: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.tables: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self.requests: List[Tuple[str, str]] = []
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> "FakeBigQuery":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # quiet
                return

            def _body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                return json.loads(raw) if raw else {}

            def _send(self, status: int, body: Optional[Dict[str, Any]]) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _handle(self, method: str) -> None:
                path = urlparse(self.path).path
                with fake._lock:
                    fake.requests.append((method, path))
                    status, body = fake._route(
                        method, path, self._body() if method in ("POST", "PUT", "PATCH") else {}
                    )
                self._send(status, body)

            def do_GET(self) -> None:  # noqa: N802 — http.server's naming
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_PATCH(self) -> None:  # noqa: N802
                self._handle("PATCH")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/bigquery/v2/"

    # ── routing ──────────────────────────────────────────────────────────
    @staticmethod
    def _not_found(what: str) -> Tuple[int, Dict[str, Any]]:
        return 404, {
            "error": {
                "code": 404,
                "message": f"Not found: {what}",
                "errors": [{"reason": "notFound", "message": f"Not found: {what}"}],
                "status": "NOT_FOUND",
            }
        }

    def _stamp(self, body: Dict[str, Any]) -> None:
        now = str(int(time.time() * 1000))
        body.setdefault("creationTime", now)
        body["lastModifiedTime"] = now
        body["etag"] = f"etag-{now}"

    def _route(self, method: str, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
        match = _TABLE_RE.match(path)
        if match:
            return self._table(method, *match.groups(), body)
        match = _DATASET_RE.match(path)
        if match:
            return self._dataset(method, *match.groups(), body)
        return self._not_found(path)

    def _dataset(self, method: str, project: str, dataset: str, body: Dict[str, Any]) -> Any:
        if method == "POST" and not dataset:
            ref = body.get("datasetReference") or {}
            key = (project, ref.get("datasetId") or "")
            stored = copy.deepcopy(body)
            stored.setdefault("access", copy.deepcopy(DEFAULT_ACCESS))
            _legacy_access(stored)
            stored["id"] = f"{project}:{key[1]}"
            stored["selfLink"] = f"{self.endpoint}projects/{project}/datasets/{key[1]}"
            stored["kind"] = "bigquery#dataset"
            stored.setdefault("location", "US")
            self._stamp(stored)
            self.datasets[key] = stored
            return 200, stored
        key = (project, dataset)
        if key not in self.datasets:
            return self._not_found(f"Dataset {project}:{dataset}")
        if method == "GET":
            return 200, self.datasets[key]
        if method in ("PATCH", "PUT"):
            stored = (
                self.datasets[key]
                if method == "PATCH"
                else {
                    k: v for k, v in self.datasets[key].items() if k in ("id", "selfLink", "kind")
                }
            )
            stored.update(copy.deepcopy(body))
            _legacy_access(stored)
            self._stamp(stored)
            self.datasets[key] = stored
            return 200, stored
        if method == "DELETE":
            del self.datasets[key]
            return 204, None
        return self._not_found(path_of(method, project, dataset))

    def _table(
        self, method: str, project: str, dataset: str, table: str, body: Dict[str, Any]
    ) -> Any:
        if (project, dataset) not in self.datasets:
            return self._not_found(f"Dataset {project}:{dataset}")
        if method == "POST" and not table:
            ref = body.get("tableReference") or {}
            key = (project, dataset, ref.get("tableId") or "")
            if key in self.tables:
                return 409, {"error": {"code": 409, "message": "Already Exists"}}
            stored = copy.deepcopy(body)
            stored["id"] = f"{project}:{dataset}.{key[2]}"
            stored["selfLink"] = (
                f"{self.endpoint}projects/{project}/datasets/{dataset}/tables/{key[2]}"
            )
            stored["kind"] = "bigquery#table"
            stored.setdefault("type", "TABLE")
            stored["location"] = self.datasets[(project, dataset)].get("location", "US")
            stored.setdefault("numRows", "0")
            stored.setdefault("numBytes", "0")
            self._stamp(stored)
            self.tables[key] = stored
            return 200, stored
        key = (project, dataset, table)
        if key not in self.tables:
            return self._not_found(f"Table {project}:{dataset}.{table}")
        if method == "GET":
            return 200, self.tables[key]
        if method in ("PUT", "PATCH"):
            stored = self.tables[key]
            keep = {k: stored[k] for k in ("id", "selfLink", "kind", "creationTime", "location")}
            merged = dict(stored) if method == "PATCH" else {}
            merged.update(copy.deepcopy(body))
            merged.update(keep)
            merged.setdefault("type", "TABLE")
            self._stamp(merged)
            self.tables[key] = merged
            return 200, merged
        if method == "DELETE":
            del self.tables[key]
            return 204, None
        return self._not_found(path_of(method, project, dataset, table))


def path_of(method: str, *parts: str) -> str:
    return f"{method} {'/'.join(parts)}"


__all__ = ["DEFAULT_ACCESS", "FakeBigQuery"]
