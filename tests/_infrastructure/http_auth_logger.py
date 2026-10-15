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

"""A loopback HTTP endpoint that records the ``Authorization`` header of every request.

Stands in for a catalog server an MCP client names (a DataHub GMS, a Databricks
workspace, a Data Mesh Manager API), so a test can see which login an SDK
attached. Built with ``redirect=True`` it answers every request addressed to
``127.0.0.1`` with a redirect to the same path on ``localhost``, a second host
name for the same server, so a test can also see what an SDK attaches when it
follows a redirect to another host. Built with ``answers`` it serves the JSON
that function returns for a path (a Databricks workspace's OIDC discovery and
token endpoints, say), so an SDK's OAuth flow runs to completion against it.
Every request body is kept in :attr:`HttpAuthLogger.bodies`, so a client
secret sent in a form body is seen as surely as one in a header.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any, Callable, List, Optional, Tuple

# (method, host header, path, Authorization header or None)
Seen = Tuple[str, str, str, Optional[str]]

# (request path, this endpoint's base URL) -> JSON body to answer with, or None
# for the default ``{}``.
Answers = Callable[[str, str], Optional[Any]]


class HttpAuthLogger:
    def __init__(self, *, redirect: bool = False, answers: Optional[Answers] = None) -> None:
        self.requests: List[Seen] = []
        # The body of each request, in the order of :attr:`requests`.
        self.bodies: List[str] = []
        requests, bodies = self.requests, self.bodies

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                host = self.headers.get("Host") or ""
                requests.append((self.command, host, self.path, self.headers.get("Authorization")))
                bodies.append(raw.decode("utf-8", "replace"))
                if redirect and host.startswith("127.0.0.1"):
                    port = self.server.server_address[1]
                    self.send_response(302)
                    self.send_header("Location", f"http://localhost:{port}{self.path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                port = self.server.server_address[1]
                answer = answers(self.path, f"http://127.0.0.1:{port}") if answers else None
                body = json.dumps({} if answer is None else answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *_args: Any) -> None:
                return None

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def authorizations(self) -> List[Optional[str]]:
        return [seen[3] for seen in self.requests]

    def clear(self) -> None:
        self.requests.clear()
        self.bodies.clear()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


__all__ = ["Answers", "HttpAuthLogger", "Seen"]
