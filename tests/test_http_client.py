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

"""fluid_build.util.http_client: the redirect and retry semantics ``requests`` had.

A real threaded loopback server, not a mock: the point is what goes over the wire.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from fluid_build.observability.reporter import CredentialRedirectRefused, refuse_foreign_redirect
from fluid_build.util.http_client import (
    MAX_REDIRECTS,
    RetryError,
    RetryTransport,
    new_async_client,
    new_client,
    tls_verify,
)


class _Handler(BaseHTTPRequestHandler):
    hits: dict = {}
    script: dict = {}

    def log_message(self, *a):  # silence
        pass

    def _serve(self):
        type(self).hits[self.path] = type(self).hits.get(self.path, 0) + 1
        n = type(self).hits[self.path]
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        steps = type(self).script.get(self.path, [(200, {}, b"ok")])
        status, headers, payload = steps[min(n, len(steps)) - 1]
        if status == "reset":
            self.connection.close()
            return
        if self.path == "/echo":
            payload = json.dumps({"method": self.command, "body": body.decode()}).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_HEAD = do_DELETE = _serve


@pytest.fixture()
def server():
    _Handler.hits = {}
    _Handler.script = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()
    srv.server_close()


def test_get_with_timeout_and_post_json(server):
    with new_client(timeout=5) as c:
        assert c.get(server.base + "/x").text == "ok"
        r = c.post(server.base + "/echo", json={"a": 1})
    assert json.loads(r.json()["body"]) == {"a": 1}


def test_redirects_are_followed_by_default_and_can_be_turned_off(server):
    _Handler.script = {"/a": [(302, {"location": "/b"}, b"")], "/b": [(200, {}, b"landed")]}
    with new_client() as c:
        assert c.get(server.base + "/a").text == "landed"
    with new_client(follow_redirects=False) as c:
        assert c.get(server.base + "/a").status_code == 302


def test_redirect_limit_is_requests_30_not_httpx_20(server):
    assert MAX_REDIRECTS == 30
    _Handler.script = {"/loop": [(302, {"location": "/loop"}, b"")]}
    with new_client() as c, pytest.raises(httpx.TooManyRedirects):
        c.get(server.base + "/loop")
    assert _Handler.hits["/loop"] == 31


def test_post_302_becomes_get_like_requests(server):
    _Handler.script = {"/p": [(302, {"location": "/echo"}, b"")]}
    with new_client() as c:
        assert c.post(server.base + "/p", json={"k": 1}).json()["method"] == "GET"


def test_response_hook_runs_on_every_hop_and_can_refuse(server):
    other = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        target = f"http://127.0.0.1:{other.server_address[1]}/secret"
        _Handler.script = {"/go": [(302, {"location": target}, b"")]}
        with new_client(response_hooks=[refuse_foreign_redirect]) as c:
            with pytest.raises(CredentialRedirectRefused, match="another origin"):
                c.get(server.base + "/go")
        assert "/secret" not in _Handler.hits
    finally:
        other.shutdown()
        other.server_close()


def test_same_origin_redirect_passes_the_hook(server):
    _Handler.script = {"/a": [(302, {"location": "/b"}, b"")]}
    with new_client(response_hooks=[refuse_foreign_redirect]) as c:
        assert c.get(server.base + "/a").status_code == 200


def test_async_client_refuses_with_the_real_exception(server):
    import asyncio

    other = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        target = f"http://127.0.0.1:{other.server_address[1]}/secret"
        _Handler.script = {"/go": [(302, {"location": target}, b"")]}

        async def run():
            async with new_async_client(response_hooks=[refuse_foreign_redirect]) as c:
                await c.get(server.base + "/go")

        with pytest.raises(CredentialRedirectRefused):
            asyncio.run(run())
        assert "/secret" not in _Handler.hits
    finally:
        other.shutdown()
        other.server_close()


def _retrying(**kw):
    sleeps: list = []
    opts = {"total": 3, "backoff_factor": 0.5, "status_forcelist": [429, 500, 502, 503, 504]}
    opts.update(kw)
    return new_client(retry={**opts, "sleep": sleeps.append}), sleeps


def test_get_retries_a_503_then_succeeds_with_urllib3_backoff(server):
    _Handler.script = {"/f": [(503, {}, b""), (503, {}, b""), (503, {}, b""), (200, {}, b"up")]}
    c, sleeps = _retrying()
    with c:
        assert c.get(server.base + "/f").text == "up"
    # urllib3: no sleep before the first retry, then factor * 2 ** (n - 1)
    assert sleeps == [1.0, 2.0]
    assert _Handler.hits["/f"] == 4


def test_exhausted_status_retries_raise_like_requests_retryerror(server):
    _Handler.script = {"/f": [(500, {}, b"")]}
    c, _ = _retrying()
    with c, pytest.raises(RetryError, match="500"):
        c.get(server.base + "/f")
    assert _Handler.hits["/f"] == 4  # 1 + total retries


def test_post_is_not_retried_when_not_an_allowed_method(server):
    _Handler.script = {"/f": [(503, {}, b""), (200, {}, b"up")]}
    c, _ = _retrying(allowed_methods=["GET", "PUT", "DELETE"])
    with c:
        assert c.post(server.base + "/f", json={}).status_code == 503
    assert _Handler.hits["/f"] == 1


def test_retry_after_is_honoured_on_429_and_503_only(server):
    _Handler.script = {
        "/a": [(429, {"retry-after": "7"}, b""), (200, {}, b"")],
        "/b": [(500, {"retry-after": "7"}, b""), (200, {}, b"")],
    }
    c, sleeps = _retrying()
    with c:
        c.get(server.base + "/a")
        c.get(server.base + "/b")
    assert sleeps == [7.0]  # /b: first retry, no backoff, header ignored on a 500


def test_connection_reset_on_get_is_retried(server):
    _Handler.script = {"/r": [("reset", {}, b""), (200, {}, b"back")]}
    c, _ = _retrying()
    with c:
        assert c.get(server.base + "/r").text == "back"
    assert _Handler.hits["/r"] == 2


def test_connect_failure_is_retried_for_every_method_then_raises():
    attempts = []

    class Refuse(httpx.BaseTransport):
        def handle_request(self, request):
            attempts.append(request.method)
            raise httpx.ConnectError("refused", request=request)

    t = RetryTransport(total=2, transport=Refuse(), sleep=lambda s: None)
    with httpx.Client(transport=t) as c, pytest.raises(httpx.ConnectError):
        c.post("http://example.invalid/")
    assert attempts == ["POST"] * 3


def test_read_failure_on_a_non_allowed_method_is_not_retried():
    attempts = []

    class Drop(httpx.BaseTransport):
        def handle_request(self, request):
            attempts.append(1)
            raise httpx.ReadError("reset", request=request)

    t = RetryTransport(total=3, allowed_methods=["GET"], transport=Drop(), sleep=lambda s: None)
    with httpx.Client(transport=t) as c, pytest.raises(httpx.ReadError):
        c.post("http://example.invalid/")
    assert len(attempts) == 1


def test_retry_wraps_proxy_transports_too(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    with new_client(retry={"total": 1}) as c:
        mounts = [v for v in c._mounts.values() if v is not None]
        assert mounts and all(isinstance(m, RetryTransport) for m in mounts)
        assert isinstance(c._transport, RetryTransport)


def test_tls_verify_defaults_on_and_honours_requests_ca_bundle_env(monkeypatch, tmp_path):
    import ssl

    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    assert tls_verify() is True
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path))
    ctx = tls_verify()
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
