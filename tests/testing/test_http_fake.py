# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid_build.testing.http_fake``: routing, logging, failure injection, TLS, lifecycle."""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import pytest

from fluid_build.testing.http_fake import INJECTED_FAILURE, FakeHttpServer, Request
from tests._loopback_tls_support import self_signed

pytestmark = pytest.mark.unit


def _call(
    fake: FakeHttpServer,
    method: str,
    path: str,
    *,
    body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    context: Optional[ssl.SSLContext] = None,
) -> Tuple[int, Dict[str, str], bytes]:
    """One request to ``fake``: ``(status, lower-cased response headers, body)``."""
    parts = urlsplit(fake.endpoint)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=10, context=context
        )
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return (
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        conn.close()


def _ok(payload: Any = None):
    return lambda request, match: (200, payload)


# ── routing ──────────────────────────────────────────────────────────────────


def test_it_listens_on_loopback_on_a_free_port():
    with FakeHttpServer() as first, FakeHttpServer() as second:
        for fake in (first, second):
            parts = urlsplit(fake.endpoint)
            assert parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port
        assert urlsplit(first.endpoint).port != urlsplit(second.endpoint).port


def test_the_handler_gets_the_request_and_the_match():
    seen = []

    def handler(request: Request, match: "re.Match[str]"):
        seen.append((request, match))
        return 201, {"id": match["id"], "echo": request.json()}

    with FakeHttpServer() as fake:
        fake.add_route("POST", r"/things/(?P<id>\d+)", handler)
        status, headers, body = _call(
            fake,
            "POST",
            "/things/42?x=1&x=2&flag=",
            body=b'{"name": "n"}',
            headers={"X-Token": "abc", "Content-Type": "application/json"},
        )
    assert status == 201 and headers["content-type"] == "application/json"
    assert body == b'{"id": "42", "echo": {"name": "n"}}'
    ((request, match),) = seen
    assert (request.method, request.path) == ("POST", "/things/42")
    assert request.query == {"x": ["1", "2"], "flag": [""]}
    assert request.headers["x-token"] == "abc" and request.body == b'{"name": "n"}'
    assert match.group("id") == "42"


def test_a_negative_content_length_reads_no_body_instead_of_waiting_for_the_client():
    seen = []
    with FakeHttpServer() as fake:
        fake.add_route(
            "POST", "/x", lambda request, match: seen.append(request.body) or (204, None)
        )
        assert _call(fake, "POST", "/x", headers={"Content-Length": "-1"})[0] == 204
    assert seen == [b""]


def test_a_path_is_percent_decoded_and_a_missing_body_reads_as_none():
    seen = []
    with FakeHttpServer() as fake:
        fake.add_route(
            "GET", r"/files/.*", lambda request, match: seen.append(request) or (204, None)
        )
        _call(fake, "GET", "/files/a%20b/c")
    assert seen[0].path == "/files/a b/c" and seen[0].body == b"" and seen[0].json() is None


def test_the_method_is_part_of_the_match_and_case_does_not_matter():
    with FakeHttpServer() as fake:
        fake.add_route("get", "/x", _ok({"by": "get"}))
        fake.add_route("POST", "/x", _ok({"by": "post"}))
        assert _call(fake, "GET", "/x")[2] == b'{"by": "get"}'
        assert _call(fake, "POST", "/x")[2] == b'{"by": "post"}'
        assert _call(fake, "DELETE", "/x")[0] == 404


def test_the_first_matching_route_wins():
    with FakeHttpServer() as fake:
        fake.add_route("GET", r"/a/.*", _ok("first"))
        fake.add_route("GET", r"/a/b", _ok("second"))
        assert _call(fake, "GET", "/a/b")[2] == b"first"


def test_a_route_matches_the_whole_path_and_not_the_query():
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/a", _ok("a"))
        assert _call(fake, "GET", "/a?x=1")[0] == 200
        assert _call(fake, "GET", "/a/b")[0] == 404
        assert _call(fake, "GET", "/xa")[0] == 404


def test_a_compiled_pattern_is_accepted():
    with FakeHttpServer() as fake:
        fake.add_route("GET", re.compile(r"/n/\d+"), _ok("n"))
        assert _call(fake, "GET", "/n/7")[0] == 200
        assert _call(fake, "GET", "/n/x")[0] == 404


def test_a_route_added_while_running_is_served():
    with FakeHttpServer() as fake:
        assert _call(fake, "GET", "/late")[0] == 404
        fake.add_route("GET", "/late", _ok("now"))
        assert _call(fake, "GET", "/late")[0] == 200


# ── responses ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload, content_type, expected",
    [
        ({"a": 1}, "application/json", b'{"a": 1}'),
        (["x"], "application/json", b'["x"]'),
        ("plain", "text/plain; charset=utf-8", b"plain"),
        (b"\x00\x01", "application/octet-stream", b"\x00\x01"),
        (None, None, b""),
    ],
)
def test_the_payload_decides_the_body_and_the_default_content_type(payload, content_type, expected):
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/p", _ok(payload))
        status, headers, body = _call(fake, "GET", "/p")
    assert status == 200 and body == expected
    assert headers.get("content-type") == content_type
    assert headers["content-length"] == str(len(expected))


def test_a_handler_may_set_headers_and_override_the_content_type():
    with FakeHttpServer() as fake:
        fake.add_route(
            "GET", "/h", lambda r, m: (200, "a,b", {"content-type": "text/csv", "X-Extra": "1"})
        )
        _, headers, body = _call(fake, "GET", "/h")
    assert body == b"a,b" and headers["content-type"] == "text/csv" and headers["x-extra"] == "1"


# ── what the server saw ──────────────────────────────────────────────────────


def test_a_request_no_route_matches_is_a_404_and_is_listed():
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/known", _ok())
        _call(fake, "GET", "/known")
        status, _, body = _call(fake, "PUT", "/nope?q=1", body=b"{}")
    assert status == 404 and b"PUT /nope" in body
    assert [(r.method, r.path) for r in fake.requests] == [("GET", "/known"), ("PUT", "/nope")]
    assert [(r.method, r.path) for r in fake.unknown] == [("PUT", "/nope")]
    assert fake.unknown[0].body == b"{}" and fake.unknown[0].query == {"q": ["1"]}


def test_nothing_unknown_leaves_the_list_empty():
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/known", _ok())
        _call(fake, "GET", "/known")
    assert fake.unknown == [] and len(fake.requests) == 1


def test_a_header_the_handler_gives_cannot_split_the_response_or_break_it():
    handlers = {
        "/split": lambda r, m: (200, "x", {"X-A": "ok\r\nSet-Cookie: stolen=1"}),
        "/latin": lambda r, m: (200, "x", {"X-A": "caf\u20ac"}),
        "/type": lambda r, m: (200, "x", {"X-A": 1}),
    }
    with FakeHttpServer() as fake:
        for path, handler in handlers.items():
            fake.add_route("GET", path, handler)
        answers = [_call(fake, "GET", path) for path in handlers]
    assert [status for status, _, _ in answers] == [500, 500, 500]
    assert [type(error) for error in fake.errors] == [ValueError, UnicodeEncodeError, TypeError]
    assert all("set-cookie" not in headers for _, headers, _ in answers)


def test_the_content_length_is_the_bodys_whatever_the_handler_says():
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/x", lambda r, m: (200, "four", {"Content-Length": "999"}))
        answer = _raw(fake, b"GET /x HTTP/1.1\r\nHost: x\r\n\r\n")
    head, _, body = answer.partition(b"\r\n\r\n")
    assert head.lower().count(b"content-length:") == 1 and b"content-length: 4" in head.lower()
    assert body == b"four"


@pytest.mark.parametrize("status", [204, 304, 101])
def test_an_answer_that_may_not_carry_a_body_has_none_and_no_content_length(status):
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/x", lambda r, m: (status, {"ignored": True}))
        answer = _raw(fake, b"GET /x HTTP/1.1\r\nHost: x\r\n\r\n")
    head, _, body = answer.partition(b"\r\n\r\n")
    assert head.startswith(f"HTTP/1.0 {status}".encode())
    assert b"content-length" not in head.lower() and body == b""


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_head_and_options_are_served_and_head_has_no_body(method):
    answer = {"method": method}
    with FakeHttpServer() as fake:
        fake.add_route(method, "/x", lambda request, match: (200, answer, {"Allow": "GET"}))
        status, headers, body = _call(fake, method, "/x")
    assert status == 200 and headers["allow"] == "GET"
    assert headers["content-length"] == str(len(json.dumps(answer)))
    assert body == (b"" if method == "HEAD" else json.dumps(answer).encode())
    assert [r.method for r in fake.requests] == [method] and fake.unknown == []


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "get;"])
def test_a_method_the_server_never_reads_is_refused_when_the_route_is_added(method):
    fake = FakeHttpServer()
    with pytest.raises(ValueError, match="not a method"):
        fake.add_route(method, "/x", _ok())
    with pytest.raises(ValueError, match="not a method"):
        fake.fail_route(method, "/x")
    assert fake.routes == [] and fake.failing_routes == []


def _raw(fake: FakeHttpServer, payload: bytes) -> bytes:
    """Send ``payload`` as it is and return everything the server answers."""
    parts = urlsplit(fake.endpoint)
    with socket.create_connection((parts.hostname, parts.port), timeout=10) as sock:
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := sock.recv(4096):
            answer += chunk
    return answer


def test_a_chunked_body_is_decoded():
    seen = []
    with FakeHttpServer() as fake:
        fake.add_route("POST", "/up", lambda r, m: seen.append(r.body) or (204, None))
        parts = urlsplit(fake.endpoint)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        conn.request("POST", "/up", body=iter([b"abc", b"defgh"]), encode_chunked=True)
        assert conn.getresponse().status == 204
        conn.close()
    assert seen == [b"abcdefgh"]


def test_chunk_extensions_and_trailers_are_skipped():
    seen = []
    request = (
        b"POST /up HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"3;name=value\r\nabc\r\n2\r\nde\r\n0\r\nX-Trailer: 1\r\n\r\n"
    )
    with FakeHttpServer() as fake:
        fake.add_route("POST", "/up", lambda r, m: seen.append(r.body) or (204, None))
        answer = _raw(fake, request)
    assert answer.startswith(b"HTTP/1.0 204") and seen == [b"abcde"]


@pytest.mark.parametrize(
    "head",
    [
        b"Transfer-Encoding: chunked\r\n\r\nzz\r\nabc\r\n0\r\n\r\n",  # not a hex size
        b"Transfer-Encoding: chunked\r\n\r\n",  # the client hung up before a size
        b"Content-Length: many\r\n\r\n",  # not a number
        b"Content-Length: 99999999999999999999999\r\n\r\n",  # too big to read
        b"Transfer-Encoding: chunked\r\n\r\nffffffffffffffffffff\r\n",  # too big to read
        b"Transfer-Encoding: chunked\r\n\r\n5\r\nHELLOWORLD\r\n0\r\n\r\n",  # longer than its size
    ],
)
def test_a_body_that_cannot_be_read_is_a_400_and_is_not_logged_as_a_request(head):
    with FakeHttpServer() as fake:
        fake.add_route("POST", "/up", _ok())
        answer = _raw(fake, b"POST /up HTTP/1.1\r\nHost: x\r\n" + head)
    assert answer.startswith(b"HTTP/1.0 400") and fake.requests == []


# ── failure injection ────────────────────────────────────────────────────────


def test_fail_route_answers_before_the_routes_with_the_status_and_body_given():
    with FakeHttpServer() as fake:
        fake.add_route("POST", "/items", _ok({"ok": True}))
        fake.fail_route("POST", "/items", 503, {"error": "down"})
        status, headers, body = _call(fake, "POST", "/items")
    assert (status, body) == (503, b'{"error": "down"}')
    assert headers["content-type"] == "application/json"
    assert [r.path for r in fake.requests] == ["/items"] and fake.unknown == []


def test_fail_route_defaults_to_a_400_with_a_message():
    with FakeHttpServer() as fake:
        fake.fail_route("GET", "/x")
        status, _, body = _call(fake, "GET", "/x")
    assert status == 400 and body == b'{"message": "injected failure"}'
    assert INJECTED_FAILURE == {"message": "injected failure"}


def test_fail_route_takes_a_regex_searched_in_the_path_for_one_method_only():
    with FakeHttpServer() as fake:
        fake.add_route("GET", r"/api/datasets/\d+", _ok("read"))
        fake.add_route("POST", r"/api/datasets/\d+", _ok("write"))
        fake.fail_route("POST", r"datasets/\d+$", 500, "boom")
        assert _call(fake, "GET", "/api/datasets/1")[2] == b"read"
        status, _, body = _call(fake, "POST", "/api/datasets/1")
    assert (status, body) == (500, b"boom")


def test_clearing_failing_routes_heals_the_server():
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/x", _ok("fine"))
        fake.fail_route("GET", "/x", 500)
        assert _call(fake, "GET", "/x")[0] == 500
        fake.failing_routes.clear()
        assert _call(fake, "GET", "/x")[0] == 200


@pytest.mark.parametrize("status", [99, 600, "500", 500.0, True, None])
def test_fail_route_refuses_something_that_is_not_a_status_code(status):
    with pytest.raises(ValueError, match="not an HTTP status code"):
        FakeHttpServer().fail_route("GET", "/x", status)


# ── a handler that breaks ────────────────────────────────────────────────────


def test_a_handler_that_raises_is_a_500_with_the_reason_and_the_exception_is_kept():
    def broken(request, match):
        raise KeyError("missing")

    with FakeHttpServer() as fake:
        fake.add_route("GET", "/broken", broken)
        fake.add_route("GET", "/fine", _ok("fine"))
        status, _, body = _call(fake, "GET", "/broken")
        assert _call(fake, "GET", "/fine")[0] == 200  # the server carried on
    assert status == 500 and b"KeyError" in body
    assert [type(e) for e in fake.errors] == [KeyError]
    assert len(fake.requests) == 2 and fake.unknown == []


@pytest.mark.parametrize(
    "result, reason",
    [
        ("just a string", "(status, payload)"),
        ((200,), "(status, payload)"),
        ((99, None), "not an HTTP status code"),
        ((200, object()), "not JSON serializable"),
    ],
)
def test_a_result_that_cannot_be_sent_is_a_500_naming_the_problem(result, reason):
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/bad", lambda request, match: result)
        status, _, body = _call(fake, "GET", "/bad")
    assert status == 500 and reason.encode() in body and len(fake.errors) == 1


# ── threads ──────────────────────────────────────────────────────────────────


def test_many_clients_at_once_are_all_answered():
    with FakeHttpServer() as fake:
        fake.add_route("GET", "/n", _ok({"ok": True}))
        with ThreadPoolExecutor(max_workers=32) as pool:
            statuses = list(pool.map(lambda _: _call(fake, "GET", "/n")[0], range(96)))
    assert statuses == [200] * 96 and len(fake.requests) == 96


def test_the_listen_backlog_is_wide_enough_for_a_burst_of_connections():
    # The stdlib default (5) resets a client that opens ten connections at once on macOS.
    from fluid_build.testing.http_fake import _Server

    assert _Server.request_queue_size >= 128


def test_handlers_run_one_at_a_time():
    running, peak = [], []

    def handler(request, match):
        running.append(1)
        peak.append(len(running))
        time.sleep(0.005)
        running.pop()
        return 200, None

    with FakeHttpServer() as fake:
        fake.add_route("GET", "/slow", handler)
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: _call(fake, "GET", "/slow"), range(32)))
    assert max(peak) == 1


def test_a_subclass_keeps_state_the_handlers_share():
    class Items(FakeHttpServer):
        def __init__(self) -> None:
            super().__init__()
            self.items: Dict[str, Any] = {}
            self.add_route("PUT", r"/items/(?P<key>\w+)", self._put)
            self.add_route("GET", r"/items/(?P<key>\w+)", self._get)

        def _put(self, request, match):
            self.items[match["key"]] = request.json()
            return 201, None

        def _get(self, request, match):
            if match["key"] not in self.items:
                return 404, {"message": "gone"}
            return 200, self.items[match["key"]]

    with Items() as fake:
        assert _call(fake, "GET", "/items/a")[0] == 404
        assert _call(fake, "PUT", "/items/a", body=b'{"v": 1}')[0] == 201
        assert _call(fake, "GET", "/items/a")[2] == b'{"v": 1}'
        with fake.lock:  # a test that changes what the handlers read takes the lock
            fake.items["b"] = {"v": 2}
        assert _call(fake, "GET", "/items/b")[2] == b'{"v": 2}'
    assert fake.unknown == []  # the 404 for a missing key was the handler's, not the router's


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_the_endpoint_is_only_known_while_running():
    fake = FakeHttpServer()
    with pytest.raises(RuntimeError, match="not running"):
        _ = fake.endpoint
    with fake:
        assert fake.endpoint.startswith("http://127.0.0.1:")
    with pytest.raises(RuntimeError, match="not running"):
        _ = fake.endpoint


def test_leaving_the_with_block_releases_the_port():
    with FakeHttpServer() as fake:
        parts = urlsplit(fake.endpoint)
    with pytest.raises(OSError):
        http.client.HTTPConnection(parts.hostname, parts.port, timeout=2).request("GET", "/")


def test_starting_twice_is_refused_and_stopping_twice_is_harmless():
    fake = FakeHttpServer().start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            fake.start()
    finally:
        fake.stop()
    fake.stop()


def test_a_stopped_server_can_start_again_and_keeps_its_log():
    fake = FakeHttpServer()
    fake.add_route("GET", "/x", _ok("x"))
    with fake:
        _call(fake, "GET", "/x")
    with fake:
        assert _call(fake, "GET", "/x")[0] == 200
    assert len(fake.requests) == 2


# ── TLS from a context the caller made ───────────────────────────────────────


@pytest.fixture
def certificate(tmp_path: Path) -> Tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1, made now and kept out of the repository."""
    return self_signed(tmp_path)


def _server_context(certificate: Tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*map(str, certificate))
    return context


def _client_context(certificate: Tuple[Path, Path]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies the certificate and the host
    context.load_verify_locations(cafile=str(certificate[0]))
    return context


def test_a_client_side_context_is_refused_when_the_server_is_built():
    unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    unverified.check_hostname = False  # still a client context
    unverified.verify_mode = ssl.CERT_NONE
    for context in (ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT), unverified):
        with pytest.raises(ValueError, match="server-side"):
            FakeHttpServer(ssl_context=context)


def test_a_caller_supplied_context_serves_https(certificate):
    with FakeHttpServer(ssl_context=_server_context(certificate)) as fake:
        fake.add_route("POST", "/secure", lambda request, match: (200, {"got": request.json()}))
        assert fake.endpoint.startswith("https://127.0.0.1:")
        status, _, body = _call(
            fake, "POST", "/secure", body=b'{"a": 1}', context=_client_context(certificate)
        )
    assert (status, body) == (200, b'{"got": {"a": 1}}')


def test_a_client_that_distrusts_the_certificate_is_refused_and_the_server_stays_quiet(
    certificate, capfd
):
    with FakeHttpServer(ssl_context=_server_context(certificate)) as fake:
        fake.add_route("GET", "/secure", _ok("ok"))
        with pytest.raises(ssl.SSLError):
            _call(fake, "GET", "/secure", context=ssl.create_default_context())
        # a client that never completes a handshake does not stop the next one
        assert _call(fake, "GET", "/secure", context=_client_context(certificate))[0] == 200
    assert capfd.readouterr().err == ""
    assert len(fake.requests) == 1


def test_plain_http_to_a_tls_server_does_not_crash_it(certificate, capfd):
    with FakeHttpServer(ssl_context=_server_context(certificate)) as fake:
        fake.add_route("GET", "/secure", _ok("ok"))
        parts = urlsplit(fake.endpoint)
        plain = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
        plain.request("GET", "/")  # sends fine; the server cannot read it as TLS
        with pytest.raises((OSError, http.client.HTTPException)):
            plain.getresponse()
        assert _call(fake, "GET", "/secure", context=_client_context(certificate))[0] == 200
    assert capfd.readouterr().err == ""


def test_a_stalled_tls_client_does_not_block_other_clients(certificate):
    with FakeHttpServer(ssl_context=_server_context(certificate)) as fake:
        fake.add_route("GET", "/secure", _ok("ok"))
        parts = urlsplit(fake.endpoint)
        with socket.create_connection((parts.hostname, parts.port)):  # connects, says nothing
            done = threading.Event()
            result = []

            def client() -> None:
                result.append(_call(fake, "GET", "/secure", context=_client_context(certificate)))
                done.set()

            threading.Thread(target=client, daemon=True).start()
            assert done.wait(timeout=10), "a silent client blocked the accept loop"
    assert result[0][0] == 200
