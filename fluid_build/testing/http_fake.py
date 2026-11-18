# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A loopback HTTP server that stands in for a remote service in tests.

``FakeHttpServer`` listens on ``127.0.0.1`` on a free port, answers from a table of
routes, and keeps what it saw. It is the generic part of the in-tree fakes under
``tests/iac``: it knows nothing about one service, so a plugin package registers its
own routes, or subclasses it and registers them in ``__init__``.

::

    with FakeHttpServer() as fake:
        fake.add_route("GET", "/items/1", lambda request, match: (200, {"id": 1}))
        fake.fail_route("POST", "/items", status=503)
        # ... point the client under test at fake.endpoint ...
    assert fake.unknown == []  # no request hit a route that is not registered

Prior art: pytest-httpserver (MIT) serves the same need with werkzeug and pytest. This
module borrows its ``ssl_context`` argument and the idea of keeping the requests no
handler matched and the errors handlers raised, and keeps to the standard library
(``http.server``, ``ssl``, ``threading``) so it can ship inside the wheel.
"""

from __future__ import annotations

import re
import ssl
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import dumps, loads
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple, Union, cast
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ["INJECTED_FAILURE", "FakeHttpServer", "Failure", "Request", "Route"]

#: The body ``fail_route`` sends when the caller gives none.
INJECTED_FAILURE: Dict[str, str] = {"message": "injected failure"}

_HOST = "127.0.0.1"
_METHODS = ("DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT")


def _method(name: str) -> str:
    """The upper-case method, or a refusal: a route for a method the server never reads would sit unused."""
    method = name.upper()
    if method not in _METHODS:
        raise ValueError(f"{name!r} is not a method the server answers ({', '.join(_METHODS)})")
    return method


@dataclass(frozen=True)
class Request:
    """One request as the server received it."""

    method: str
    path: str  # percent-decoded, without the query string
    query: Dict[str, List[str]]  # ``urllib.parse.parse_qs`` of the query string
    headers: Dict[str, str]  # lower-cased names; a repeated header keeps its last value
    body: bytes = b""

    def json(self) -> Any:
        """The body parsed as JSON, or ``None`` when the request has no body."""
        return loads(self.body) if self.body else None


#: What a handler returns: ``(status, payload)`` or ``(status, payload, headers)``.
#: ``payload`` is ``None`` (empty body), ``bytes``, ``str``, or anything ``json.dumps``
#: takes. A ``Content-Type`` is added unless ``headers`` names one. ``headers`` holds one
#: value per name, in Latin-1 and without line breaks; the ``Content-Length`` is always
#: the length of the body, so one given here is ignored. A 1xx, 204 or 304 answer has no body.
Result = Tuple[Any, ...]
Handler = Callable[[Request, "re.Match[str]"], Result]


class Route(NamedTuple):
    method: str
    pattern: "re.Pattern[str]"  # must match the whole path
    handler: Handler


class Failure(NamedTuple):
    method: str
    pattern: "re.Pattern[str]"  # searched anywhere in the path
    status: int
    body: Any


class FakeHttpServer:
    """A threaded HTTP server on ``127.0.0.1``, a free port, driven by a route table.

    Requests are answered in this order: a :meth:`fail_route` rule, then the first
    :meth:`add_route` route whose method and whole path match, then a 404. Handlers
    run one at a time, under :attr:`lock`, so they may change shared state freely; take
    the lock yourself when a test changes state a handler reads. It answers DELETE, GET,
    HEAD (the headers of the GET answer, no body), OPTIONS, PATCH, POST and PUT, and reads
    a request body by its Content-Length or as chunked.

    Attributes (all public, all safe to read after the client call returned):

    * ``routes``: the route table, ``Route(method, compiled regex, handler)``, in
      match order. The regex must match the whole path.
    * ``failing_routes``: the :meth:`fail_route` rules; ``clear()`` it to heal.
    * ``requests``: every request received, in order, as :class:`Request`.
    * ``unknown``: the requests no route matched; each was answered with a 404.
    * ``errors``: exceptions a handler raised; each was answered with a 500.

    ``ssl_context`` serves HTTPS instead of HTTP. It must be a server-side context
    that already holds a certificate chain
    (``ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)`` and ``load_cert_chain``). The kit makes
    no certificates; a test makes its own and trusts it in the client under test.
    """

    def __init__(self, *, ssl_context: Optional[ssl.SSLContext] = None) -> None:
        if ssl_context is not None and ssl_context.check_hostname:
            raise ValueError(
                "ssl_context must be a server-side context (ssl.PROTOCOL_TLS_SERVER with a "
                "certificate chain); this one checks host names, as a client context does"
            )
        self.routes: List[Route] = []
        self.failing_routes: List[Failure] = []
        self.requests: List[Request] = []
        self.unknown: List[Request] = []
        self.errors: List[Exception] = []
        self.lock = threading.RLock()
        self._ssl_context = ssl_context
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ routes
    def add_route(
        self, method: str, pattern: Union[str, "re.Pattern[str]"], handler: Handler
    ) -> None:
        """Answer ``method`` requests whose whole path matches ``pattern`` with ``handler``.

        ``handler(request, match)`` gets the :class:`Request` and the ``re.Match`` (use
        named groups for path parameters) and returns :data:`Result`. Routes are tried in
        the order added, and the first match wins.
        """
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        with self.lock:
            self.routes.append(Route(_method(method), compiled, handler))

    def fail_route(
        self,
        method: str,
        path_part: Union[str, "re.Pattern[str]"],
        status: int = 400,
        body: Any = INJECTED_FAILURE,
    ) -> None:
        """Answer every later ``method`` request whose path contains ``path_part`` with an error.

        ``path_part`` is a regular expression searched in the path (``re.escape`` it for
        a literal). The rule is checked before the routes and holds until removed from
        :attr:`failing_routes`. A client that retries on 5xx does not retry a 4xx, which
        is why the default is 400; pass a 5xx to exercise the retry. ``body`` is encoded
        as a handler payload is.
        """
        if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
            raise ValueError(f"fail_route: {status!r} is not an HTTP status code")
        compiled = re.compile(path_part) if isinstance(path_part, str) else path_part
        with self.lock:
            self.failing_routes.append(Failure(_method(method), compiled, status, body))

    # --------------------------------------------------------------- lifecycle
    def start(self) -> "FakeHttpServer":
        """Bind and serve in a background thread; returns ``self``."""
        if self._server is not None:
            raise RuntimeError("FakeHttpServer is already running")
        server = _Server(self, self._ssl_context)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="fake-http-server",
            daemon=True,
        )
        thread.start()
        self._server, self._thread = server, thread
        return self

    def stop(self) -> None:
        """Stop serving and release the port. Calling it twice is harmless."""
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def __enter__(self) -> "FakeHttpServer":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    @property
    def endpoint(self) -> str:
        """``http://127.0.0.1:<port>``, or ``https://...`` with an ``ssl_context``."""
        if self._server is None:
            raise RuntimeError("FakeHttpServer is not running: use start() or a with block")
        scheme = "https" if self._ssl_context is not None else "http"
        return f"{scheme}://{_HOST}:{self._server.server_address[1]}"

    # ----------------------------------------------------------------- serving
    def _answer(self, request: Request) -> Tuple[int, bytes, Dict[str, str]]:
        with self.lock:
            self.requests.append(request)
            try:
                return _render(self._decide(request))
            except Exception as exc:  # a bug in a handler: keep it, and say so in the answer
                self.errors.append(exc)
                return _render((500, {"message": f"{type(exc).__name__}: {exc}"}))

    def _decide(self, request: Request) -> Result:
        for failure in self.failing_routes:
            if failure.method == request.method and failure.pattern.search(request.path):
                return failure.status, failure.body
        for route in self.routes:
            if route.method == request.method:
                match = route.pattern.fullmatch(request.path)
                if match is not None:
                    return route.handler(request, match)
        self.unknown.append(request)
        return 404, {"message": f"no route for {request.method} {request.path}"}


def _render(result: Any) -> Tuple[int, bytes, Dict[str, str]]:
    """Turn a handler result into ``(status, body bytes, headers)``."""
    status, payload, headers = _unpack(result)
    data, content_type = _encode(payload)
    if content_type and not any(name.lower() == "content-type" for name in headers):
        headers["Content-Type"] = content_type
    return status, data, headers


def _unpack(result: Any) -> Tuple[int, Any, Dict[str, str]]:
    """The status, payload and headers of a handler result, each one checked."""
    if not isinstance(result, tuple) or len(result) not in (2, 3):
        raise TypeError(
            f"a handler returns (status, payload) or (status, payload, headers), not {result!r:.200}"
        )
    status, payload = result[0], result[1]
    if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
        raise ValueError(f"{status!r} is not an HTTP status code")
    headers = dict(result[2]) if len(result) == 3 else {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise TypeError(f"header {name!r}: the name and the value must be str")
        if any(ch in text for text in (name, value) for ch in "\r\n"):
            raise ValueError(f"header {name!r} holds a line break")
        (name + value).encode("latin-1")  # http.server writes headers as Latin-1
    # The Content-Length is always the body's, which ``_Handler._serve`` sets.
    return status, payload, {k: v for k, v in headers.items() if k.lower() != "content-length"}


def _encode(payload: Any) -> Tuple[bytes, Optional[str]]:
    """The body bytes of a handler payload, and the Content-Type that goes with them."""
    if payload is None:
        return b"", None
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload), "application/octet-stream"
    if isinstance(payload, str):
        return payload.encode("utf-8"), "text/plain; charset=utf-8"
    return dumps(payload).encode("utf-8"), "application/json"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        """Keep test output free of the stdlib's per-request access log."""

    def _read_body(self) -> bytes:
        """The request body, by Content-Length or as chunked; ``ValueError`` if it is malformed."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            body = bytearray()
            while True:
                size = int(self.rfile.readline(65537).split(b";", 1)[0].strip(), 16)
                if size == 0:  # the trailer, if any, ends at a blank line
                    while self.rfile.readline(65537) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(body)
                body += self.rfile.read(size)
                self.rfile.readline(65537)  # the line break after the chunk
        length = max(0, int(self.headers.get("Content-Length") or 0))
        return self.rfile.read(length) if length else b""

    def _serve(self, method: str) -> None:
        fake = cast(_Server, self.server).fake
        parts = urlsplit(self.path)
        try:
            body = self._read_body()
        except ValueError:  # a Content-Length that is not a number, or a broken chunk
            self.close_connection = True
            self.send_error(400, "Malformed request body")
            return
        request = Request(
            method=method,
            path=unquote(parts.path),
            query=parse_qs(parts.query, keep_blank_values=True),
            headers={name.lower(): value for name, value in self.headers.items()},
            body=body,
        )
        status, payload, headers = fake._answer(request)
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        bodyless = status in (204, 304) or 100 <= status < 200  # no body, no Content-Length
        if not bodyless:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and method != "HEAD" and not bodyless:
            self.wfile.write(payload)

    def do_GET(self) -> None:
        self._serve("GET")

    def do_HEAD(self) -> None:
        self._serve("HEAD")

    def do_OPTIONS(self) -> None:
        self._serve("OPTIONS")

    def do_POST(self) -> None:
        self._serve("POST")

    def do_PUT(self) -> None:
        self._serve("PUT")

    def do_PATCH(self) -> None:
        self._serve("PATCH")

    def do_DELETE(self) -> None:
        self._serve("DELETE")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # The stdlib backlog of 5 overflows when a client opens about ten connections at
    # once, as a Terraform refresh does. Linux drops the SYN and the client retries;
    # macOS answers with a reset, which the client reports as a broken pipe.
    request_queue_size = 128

    def __init__(self, fake: FakeHttpServer, ssl_context: Optional[ssl.SSLContext]) -> None:
        super().__init__((_HOST, 0), _Handler)
        self.fake = fake
        self.ssl_context = ssl_context

    def get_request(self) -> Tuple[Any, Any]:
        conn, address = super().get_request()
        if self.ssl_context is not None:
            # The handshake runs on the first read, in the connection's own thread, so
            # a client that stalls or hangs up cannot block the accept loop.
            conn = self.ssl_context.wrap_socket(
                conn, server_side=True, do_handshake_on_connect=False
            )
        return conn, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that hung up, or refused our certificate, is not a server error.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)
