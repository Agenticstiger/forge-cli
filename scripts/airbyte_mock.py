"""Minimal Airbyte API mock — matches the request contract the airbyte
runner uses. Sufficient for runner correctness verification; not a
substitute for a real Airbyte deployment in production tests.

Endpoints implemented:
    POST /api/v1/workspaces/list
    POST /api/v1/sources/create
    POST /api/v1/destinations/create
    POST /api/v1/sources/discover_schema
    POST /api/v1/connections/create
    POST /api/v1/connections/sync
    POST /api/v1/jobs/get

The mock issues deterministic UUIDs and reports every sync as `succeeded`
so the runner's happy-path can be exercised end-to-end.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORKSPACE_ID = "00000000-0000-0000-0000-000000000001"
SOURCE_ID = "00000000-0000-0000-0000-000000000002"
DEST_ID = "00000000-0000-0000-0000-000000000003"
CONN_ID = "00000000-0000-0000-0000-000000000004"
JOB_ID = 1


class Handler(BaseHTTPRequestHandler):
    def _ok(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/health"):
            self._ok({"status": "ok"})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        _ = self.rfile.read(length) if length else b""

        path = self.path.rstrip("/")
        if path.endswith("/workspaces/list"):
            self._ok({"workspaces": [{"workspaceId": WORKSPACE_ID, "name": "default"}]})
        elif path.endswith("/sources/create"):
            self._ok({"sourceId": SOURCE_ID})
        elif path.endswith("/destinations/create"):
            self._ok({"destinationId": DEST_ID})
        elif path.endswith("/sources/discover_schema"):
            # Real Airbyte returns the connector's discovered catalog
            # with full jsonSchema + supportedSyncModes per stream. The
            # runner needs both to build a valid /connections/create
            # body — this mirrors the canonical 1.x response shape.
            self._ok(
                {
                    "catalog": {
                        "streams": [
                            {
                                "stream": {
                                    "name": "orders",
                                    "namespace": "public",
                                    "jsonSchema": {
                                        "type": "object",
                                        "properties": {"id": {"type": "integer"}},
                                    },
                                    "supportedSyncModes": [
                                        "full_refresh",
                                        "incremental",
                                    ],
                                    "defaultCursorField": [],
                                    "sourceDefinedPrimaryKey": [["id"]],
                                }
                            }
                        ]
                    },
                    "jobInfo": {"succeeded": True},
                }
            )
        elif path.endswith("/connections/create"):
            self._ok({"connectionId": CONN_ID})
        elif path.endswith("/connections/sync"):
            # Real Airbyte returns "running" — the runner polls
            # /jobs/get until terminal. After the runner fix this is
            # the realistic shape, not "succeeded".
            self._ok({"job": {"id": JOB_ID, "status": "running"}})
        elif path.endswith("/jobs/get"):
            self._ok(
                {
                    "job": {"id": JOB_ID, "status": "succeeded"},
                    "attempts": [{"attempt": {"recordsSynced": 42}}],
                }
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):  # quiet
        pass


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    print("airbyte-mock listening on :8000", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
