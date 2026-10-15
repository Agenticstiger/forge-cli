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

"""The Command Center client has no host gate of its own: it uses the reporter's.

``cli/_command_center.py`` used to keep a copy of the gate without the rule
that a host that is neither loopback nor allow-listed must be https, so its
detection probes (and the blueprint marketplace fetched from the URL it
detected) went over plain http to public hosts the reporter refused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Iterator, List, Tuple
from unittest.mock import patch

import pytest
import requests

from fluid_build.cli._command_center import CommandCenterClient

pytestmark = pytest.mark.unit

_PRIV = "fluid_build.observability.reporter._hostname_is_private"


def test_the_client_and_the_reporter_share_one_gate():
    from fluid_build.cli import _command_center
    from fluid_build.observability import reporter

    assert _command_center._command_center_host_allowed is reporter._command_center_host_allowed
    assert _command_center._command_center_host_refusal is reporter._command_center_host_refusal


def test_the_client_gate_refuses_plain_http_to_a_public_host():
    from fluid_build.cli._command_center import _command_center_host_allowed

    with patch(_PRIV, return_value=False):
        assert _command_center_host_allowed("http://cc.example.com") is False
        assert _command_center_host_allowed("https://cc.example.com") is True


# ── the client's probes against a real listener ───────────────────────


@pytest.fixture
def staged_public_host(monkeypatch) -> Iterator[Tuple[str, List[Tuple[str, str]]]]:
    """``(host:port, recorded)`` for ``cc.example.com`` on a loopback listener.

    DNS is staged, not the gate: the gate's lookup (``getaddrinfo(host, None)``)
    gets a public address, and the connection itself lands on the listener, so
    a probe the gate lets through is recorded.
    """
    recorded: List[Tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            recorded.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_HEAD = do_GET  # noqa: N815

        def log_message(self, *_a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    real_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, port, *args, **kwargs):
        if host != "cc.example.com":
            return real_getaddrinfo(host, port, *args, **kwargs)
        if port is None:  # the gate's lookup
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    try:
        yield f"cc.example.com:{server.server_address[1]}", recorded
    finally:
        server.shutdown()
        server.server_close()


class TestClientProbeUsesTheReporterGate:
    def test_a_public_host_over_plain_http_is_not_probed(self, staged_public_host, monkeypatch):
        hostport, recorded = staged_public_host
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", f"http://{hostport}")

        client = CommandCenterClient()

        assert recorded == []
        assert client.available is False

    def test_an_allow_listed_host_over_plain_http_is_still_probed(
        self, staged_public_host, monkeypatch
    ):
        # The control: the listener does record a probe the gate lets through.
        hostport, recorded = staged_public_host
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", f"http://{hostport}")
        monkeypatch.setenv("FLUID_COMMAND_CENTER_HOST_ALLOWLIST", "cc.example.com")

        CommandCenterClient()

        assert ("GET", "/health") in recorded


# ── where the client takes its URL from ──────────────────────────────────


class _Listener:
    """A loopback HTTP server that records every request and answers with ``answer``."""

    def __init__(self, answer) -> None:
        self.recorded: List[Tuple[str, str]] = []
        self.bodies: List[bytes] = []
        # Each request's headers, lower-cased names, in the order received.
        self.headers: List[dict] = []
        recorded, bodies, seen, respond = self.recorded, self.bodies, self.headers, answer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    bodies.append(self.rfile.read(length))
                recorded.append((self.command, self.path))
                seen.append({k.lower(): v for k, v in self.headers.items()})
                status, headers, body = respond(self.path)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_HEAD = do_POST = do_PUT = do_GET  # noqa: N815

            def log_message(self, *_a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _ok(_path: str):
    return 200, {"Content-Type": "application/json"}, b'{"version": "9.9.9"}'


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """No Command Center URL in the environment, an empty HOME, a checkout as cwd."""
    for var in (
        "FLUID_COMMAND_CENTER_URL",
        "FLUID_DISABLE_CC_DETECTION",
        "FLUID_COMMAND_CENTER_HOST_ALLOWLIST",
    ):
        monkeypatch.delenv(var, raising=False)
    # ``get_command_center_client`` keeps one client per process.
    monkeypatch.setattr("fluid_build.cli._command_center._global_client", None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    return home, checkout


@pytest.mark.parametrize("name", ["fluid.yaml", "fluid.yml"])
def test_a_checkout_file_does_not_choose_the_command_center_url(clean_env, name):
    _home, checkout = clean_env
    listener = _Listener(_ok)
    try:
        (checkout / name).write_text(f"command_center:\n  url: {listener.url}\n", encoding="utf-8")

        client = CommandCenterClient()

        assert client.url != listener.url
        assert listener.recorded == []
    finally:
        listener.close()


@pytest.mark.parametrize("name", ["config.yaml", "config.yml"])
def test_the_operators_own_config_still_chooses_it(clean_env, name):
    home, _checkout = clean_env
    listener = _Listener(_ok)
    try:
        (home / ".fluid").mkdir()
        (home / ".fluid" / name).write_text(
            f"command_center:\n  url: {listener.url}\n", encoding="utf-8"
        )

        client = CommandCenterClient()

        assert client.url == listener.url
        assert ("GET", "/health") in listener.recorded
    finally:
        listener.close()


# ── the client's probes never follow a redirect off the gated origin ─────


@pytest.fixture
def redirecting_cc(clean_env, monkeypatch):
    """``(cc, metadata)``: a Command Center on loopback that redirects every
    probe to another origin, standing in for a private / metadata address."""
    metadata = _Listener(
        lambda _p: (200, {"Content-Type": "application/json"}, b'{"version": "SECRET-META"}')
    )

    def _redirect(path: str):
        return 302, {"Location": f"{metadata.url}/latest/meta-data{path}"}, b""

    cc = _Listener(_redirect)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)
    try:
        yield cc, metadata
    finally:
        cc.close()
        metadata.close()


def test_a_probe_does_not_follow_a_redirect_to_another_origin(redirecting_cc):
    cc, metadata = redirecting_cc

    client = CommandCenterClient()

    assert ("GET", "/health") in cc.recorded
    assert metadata.recorded == []
    assert client.available is False
    assert client.features.version != "SECRET-META"


def test_every_probe_refuses_the_foreign_redirect(redirecting_cc):
    # Past the health check too: the feature probes and the version read.
    cc, metadata = redirecting_cc
    client = CommandCenterClient()
    client.available = True

    client._detect_features()

    assert metadata.recorded == []
    assert client.features.version == "unknown"
    assert not any(
        (client.features.marketplace, client.features.catalog, client.features.governance)
    )
    assert ("GET", "/api/v1/version") in cc.recorded


def test_a_redirect_within_the_origin_is_still_followed(clean_env, monkeypatch):
    def _answer(path: str):
        if path == "/health":
            return 302, {"Location": "/healthz"}, b""
        return _ok(path)

    cc = _Listener(_answer)
    try:
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)

        client = CommandCenterClient()

        assert ("GET", "/healthz") in cc.recorded
        assert client.available is True
        assert client.features.version == "9.9.9"
    finally:
        cc.close()


# ── and neither does any request made with the URL it hands on ───────────
#
# The blueprint marketplace (``fluid market --blueprints``, ``--blueprint-id``,
# ``--instantiate``) and the market Command Center catalog connector use the
# URL the client detected after its probes passed. A Command Center that
# answers those requests, not the probes, with a redirect to another origin
# is not followed there either, and the instantiate POST does not resend the
# operator's parameters.

_MARKETPLACE = "/api/v1/blueprints-marketplace"
_CATALOG = "/api/v1/data-products"
_PARAMETERS = {"warehouse_password": "operator-param-not-a-secret"}  # pragma: allowlist secret


@pytest.fixture
def cc_redirecting_its_data(clean_env, monkeypatch):
    """``(cc, other)``: a Command Center whose probes pass and whose
    marketplace and catalog requests are redirected to another origin."""
    other = _Listener(lambda _p: (200, {"Content-Type": "application/json"}, b'{"items": []}'))

    def _answer(path: str):
        if path in ("/health", "/api/v1/version", _MARKETPLACE, _CATALOG):
            return _ok(path)  # the probes
        status = 307 if path.endswith("/instantiate") else 302
        return status, {"Location": f"{other.url}/latest/meta-data{path}"}, b""

    cc = _Listener(_answer)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)
    try:
        yield cc, other
    finally:
        cc.close()
        other.close()


def _marketplace_url() -> str:
    from fluid_build.cli import marketplace

    url = marketplace.get_api_url(logging.getLogger("t"))
    assert url == f"{os.environ['FLUID_COMMAND_CENTER_URL']}{_MARKETPLACE}"
    return url


@pytest.mark.parametrize("path", ["/customer-360", "/customer-360/info"], ids=["info", "nested"])
def test_a_blueprint_get_does_not_follow_a_redirect_to_another_origin(
    cc_redirecting_its_data, path
):
    from fluid_build.cli import marketplace

    cc, other = cc_redirecting_its_data
    api_url = _marketplace_url()

    with pytest.raises(requests.exceptions.RequestException) as raised:
        marketplace._marketplace_get(f"{api_url}{path}", logging.getLogger("t"))

    assert ("GET", f"{_MARKETPLACE}{path}") in cc.recorded
    assert other.recorded == []
    assert "another origin" in str(raised.value)
    assert "/latest/meta-data" not in str(raised.value)


def test_the_blueprint_search_does_not_follow_a_redirect_to_another_origin(
    cc_redirecting_its_data, monkeypatch
):
    from fluid_build.cli import marketplace

    cc, other = cc_redirecting_its_data
    monkeypatch.setattr(marketplace, "_bundled_blueprints_matching", lambda _args: [])
    args = SimpleNamespace(
        query="x",
        category=None,
        tags=None,
        maturity=None,
        state=None,
        limit=5,
        sort="name",
        format="json",
    )

    rc = marketplace.search_blueprints(args, logging.getLogger("t"), _marketplace_url())

    assert rc == 1
    assert any(m == "GET" and p.startswith(f"{_MARKETPLACE}?") for m, p in cc.recorded)
    assert other.recorded == []


def test_the_instantiate_post_does_not_resend_the_parameters_to_another_origin(
    cc_redirecting_its_data,
):
    from fluid_build.cli import marketplace

    cc, other = cc_redirecting_its_data
    api_url = _marketplace_url()

    with pytest.raises(requests.exceptions.RequestException):
        marketplace._marketplace_post(
            f"{api_url}/customer-360/instantiate",
            logging.getLogger("t"),
            json={"parameters": _PARAMETERS},
        )

    assert ("POST", f"{_MARKETPLACE}/customer-360/instantiate") in cc.recorded
    assert other.recorded == [] and other.bodies == []


def test_a_blueprint_redirect_within_the_origin_is_still_followed(clean_env, monkeypatch):
    from fluid_build.cli import marketplace

    def _answer(path: str):
        if path == f"{_MARKETPLACE}/old-id":
            return 307, {"Location": f"{_MARKETPLACE}/new-id"}, b""
        return 200, {"Content-Type": "application/json"}, b'{"id": "new-id"}'

    cc = _Listener(_answer)
    try:
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)

        response = marketplace._marketplace_get(
            f"{_marketplace_url()}/old-id", logging.getLogger("t")
        )

        assert response.json() == {"id": "new-id"}
        assert ("GET", f"{_MARKETPLACE}/new-id") in cc.recorded
    finally:
        cc.close()


async def _connected_catalog_connector():
    from fluid_build.cli.market_catalogs.command_center import CommandCenterConnector

    connector = CommandCenterConnector({}, logging.getLogger("t"))
    assert await connector._connect_impl() is True
    assert connector.base_url == f"{os.environ['FLUID_COMMAND_CENTER_URL']}{_CATALOG}"
    return connector


def test_the_market_catalog_connector_does_not_follow_a_redirect_to_another_origin(
    cc_redirecting_its_data,
):
    cc, other = cc_redirecting_its_data

    async def _run():
        connector = await _connected_catalog_connector()
        try:
            return await connector._get_catalog_stats_impl()
        finally:
            await connector.session.close()

    stats = asyncio.run(_run())

    assert ("GET", f"{_CATALOG}/stats") in cc.recorded
    assert other.recorded == []
    assert stats["total_products"] == 0


def test_every_request_on_the_connector_session_refuses_the_foreign_redirect(
    cc_redirecting_its_data,
):
    from fluid_build.observability.reporter import CredentialRedirectRefused

    cc, other = cc_redirecting_its_data

    async def _run():
        connector = await _connected_catalog_connector()
        try:
            async with connector.session.get(f"{connector.base_url}/search") as response:
                return response.status
        finally:
            await connector.session.close()

    with pytest.raises(CredentialRedirectRefused, match="another origin"):
        asyncio.run(_run())

    assert ("GET", f"{_CATALOG}/search") in cc.recorded
    assert other.recorded == []


def test_a_catalog_redirect_within_the_origin_is_still_followed(clean_env, monkeypatch):
    def _answer(path: str):
        if path == f"{_CATALOG}/stats":
            return 302, {"Location": f"{_CATALOG}/stats/v2"}, b""
        if path == f"{_CATALOG}/stats/v2":
            return 200, {"Content-Type": "application/json"}, b'{"total_products": 7}'
        return _ok(path)

    cc = _Listener(_answer)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)

    async def _run():
        connector = await _connected_catalog_connector()
        try:
            return await connector._get_catalog_stats_impl()
        finally:
            await connector.session.close()

    try:
        assert asyncio.run(_run()) == {"total_products": 7}
    finally:
        cc.close()


# ── a refused hop is never sent: no request, not even a connection ───────
#
# The refusal is raised from the hook each library runs on a redirect response
# before it follows it: aiohttp's ``on_request_redirect`` trace signal for the
# catalog connector's session, requests' response hook for the marketplace.
# The second origin must see nothing at all, also for a 307 / 308, which
# resends the method and the body (the operator's parameters).


def _counting(listener: "_Listener") -> List[Any]:
    """Every connection ``listener`` accepts, whether or not a request follows on it."""
    accepted: List[Any] = []
    server = listener.server
    verify = server.verify_request

    def _verify(request: Any, client_address: Any) -> bool:
        accepted.append(client_address)
        return verify(request, client_address)

    server.verify_request = _verify
    return accepted


@pytest.fixture(params=[301, 302, 303, 307, 308])
def cc_redirecting_every_hop(clean_env, monkeypatch, request):
    """``(cc, other, accepted, status)``: a Command Center whose probes pass and
    that answers every other request with ``status`` to a second origin;
    ``accepted`` is every connection that second origin accepts."""
    status = request.param
    other = _Listener(lambda _p: (200, {"Content-Type": "application/json"}, b'{"items": []}'))
    accepted = _counting(other)

    def _answer(path: str):
        if path in ("/health", "/api/v1/version", _MARKETPLACE, _CATALOG):
            return _ok(path)  # the probes
        return status, {"Location": f"{other.url}/collect{path}"}, b""

    cc = _Listener(_answer)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)
    try:
        yield cc, other, accepted, status
    finally:
        cc.close()
        other.close()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT"])
def test_the_connector_session_sends_nothing_to_the_redirect_target(
    cc_redirecting_every_hop, method
):
    from fluid_build.observability.reporter import CredentialRedirectRefused

    cc, other, accepted, _status = cc_redirecting_every_hop

    async def _run():
        connector = await _connected_catalog_connector()
        try:
            kwargs = {} if method == "GET" else {"json": {"parameters": _PARAMETERS}}
            async with connector.session.request(
                method, f"{connector.base_url}/instantiate", **kwargs
            ) as response:
                return response.status
        finally:
            await connector.session.close()

    with pytest.raises(CredentialRedirectRefused, match="another origin"):
        asyncio.run(_run())

    assert (method, f"{_CATALOG}/instantiate") in cc.recorded
    assert other.recorded == [] and other.bodies == []
    assert accepted == []


@pytest.mark.parametrize("method", ["get", "post", "put"])
def test_a_marketplace_request_sends_nothing_to_the_redirect_target(
    cc_redirecting_every_hop, method
):
    from fluid_build.cli import marketplace

    cc, other, accepted, _status = cc_redirecting_every_hop
    url = f"{_marketplace_url()}/customer-360/instantiate"
    kwargs = {} if method == "get" else {"json": {"parameters": _PARAMETERS}}

    with pytest.raises(requests.exceptions.RequestException, match="another origin"):
        marketplace._marketplace_request(
            getattr(requests, method), url, logging.getLogger("t"), **kwargs
        )

    assert (method.upper(), f"{_MARKETPLACE}/customer-360/instantiate") in cc.recorded
    assert other.recorded == [] and other.bodies == []
    assert accepted == []


def test_the_second_origin_is_reachable_so_the_zero_above_is_real(cc_redirecting_every_hop):
    # The counting listener does see a connection and a request sent to it.
    _cc, other, accepted, _status = cc_redirecting_every_hop

    assert requests.post(f"{other.url}/collect", json={"x": 1}, timeout=5).status_code == 200
    assert other.recorded == [("POST", "/collect")]
    assert len(accepted) == 1


# ── a registry the operator names is not the Command Center ──────────────
#
# FLUID_API_URL, and FLUID_PUBLIC_REGISTRY with FLUID_MARKETPLACE_FALLBACK=public,
# name a blueprint registry, not the Command Center. A search or a blueprint
# lookup there carries no credential and no body, so a registry that moved
# host (a 301 from the apex to www) is followed there, as on main; f00cc845
# refused it, and the search fell back to the bundled blueprints alone. The
# instantiate POST, which carries the operator's parameters, and a request
# with a credential header still refuse a redirect to another origin.

_MOVED_BLUEPRINT = {
    "id": "moved-registry-blueprint",
    "name": "Moved Registry Blueprint",
    "description": "served by the registry's new host",
    "category": "analytics",
    "version": "1.0.0",
    "labels": {"maturity": "stable"},
    "spec": {"parameters": []},
}


def _json_answer(body: Any) -> Tuple[int, dict, bytes]:
    import json

    return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()


@pytest.fixture
def moved_registry(clean_env, monkeypatch):
    """``(old, new)``: a registry that answers every request with a redirect to
    the same path on another origin (301; 307 for the instantiate POST)."""
    for var in ("FLUID_API_URL", "FLUID_PUBLIC_REGISTRY", "FLUID_MARKETPLACE_FALLBACK"):
        monkeypatch.delenv(var, raising=False)

    def _new(path: str):
        if path.endswith("/instantiate"):
            return _json_answer({"contract": {"id": "rendered"}, "cost_estimate": "$0"})
        if path.split("?")[0] == _MARKETPLACE:
            return _json_answer({"items": [_MOVED_BLUEPRINT], "total": 1})
        return _json_answer(_MOVED_BLUEPRINT)

    new = _Listener(_new)

    def _old(path: str):
        status = 307 if path.endswith("/instantiate") else 301
        return status, {"Location": f"{new.url}{path}"}, b""

    old = _Listener(_old)
    try:
        yield old, new
    finally:
        old.close()
        new.close()


def _name_the_registry(monkeypatch, source: str, url: str) -> None:
    if source == "FLUID_API_URL":
        monkeypatch.setenv("FLUID_API_URL", url)
    else:
        monkeypatch.setenv("FLUID_DISABLE_CC_DETECTION", "true")
        monkeypatch.setenv("FLUID_MARKETPLACE_FALLBACK", "public")
        monkeypatch.setenv("FLUID_PUBLIC_REGISTRY", url)


_SOURCES = ["FLUID_API_URL", "FLUID_PUBLIC_REGISTRY"]


@pytest.mark.parametrize("source", _SOURCES)
def test_a_search_of_a_named_registry_follows_its_move_to_another_host(
    moved_registry, monkeypatch, capsys, source
):
    import json

    from fluid_build.cli import marketplace

    old, new = moved_registry
    _name_the_registry(monkeypatch, source, f"{old.url}{_MARKETPLACE}")
    args = SimpleNamespace(
        query=None,
        category=None,
        tags=None,
        maturity=None,
        state=None,
        limit=5,
        sort="name",
        format="json",
    )
    api_url = marketplace.get_api_url(logging.getLogger("t"), required=False)
    capsys.readouterr()

    rc = marketplace.search_blueprints(args, logging.getLogger("t"), api_url)

    assert rc == 0
    ids = [bp["id"] for bp in json.loads(capsys.readouterr().out)]
    assert _MOVED_BLUEPRINT["id"] in ids
    assert any(m == "GET" and p.startswith(f"{_MARKETPLACE}?") for m, p in new.recorded)


@pytest.mark.parametrize("source", _SOURCES)
def test_a_blueprint_lookup_in_a_named_registry_follows_its_move(
    moved_registry, monkeypatch, source
):
    from fluid_build.cli import marketplace

    old, new = moved_registry
    _name_the_registry(monkeypatch, source, f"{old.url}{_MARKETPLACE}")
    args = SimpleNamespace(blueprint_id=_MOVED_BLUEPRINT["id"], version=None, show_template=False)

    rc = marketplace.show_blueprint_info(
        args, logging.getLogger("t"), marketplace.get_api_url(logging.getLogger("t"))
    )

    assert rc == 0
    assert ("GET", f"{_MARKETPLACE}/{_MOVED_BLUEPRINT['id']}") in new.recorded


@pytest.mark.parametrize("source", _SOURCES)
def test_the_instantiate_post_to_a_named_registry_still_refuses_another_origin(
    moved_registry, monkeypatch, source
):
    from fluid_build.cli import marketplace

    old, new = moved_registry
    _name_the_registry(monkeypatch, source, f"{old.url}{_MARKETPLACE}")
    args = SimpleNamespace(
        blueprint_id=_MOVED_BLUEPRINT["id"],
        params='{"warehouse_password": "operator-param-not-a-secret"}',  # pragma: allowlist secret
        interactive=False,
        output=None,
        submit=False,
    )

    rc = marketplace.instantiate_blueprint(
        args, logging.getLogger("t"), marketplace.get_api_url(logging.getLogger("t"))
    )

    assert rc == 1
    # The lookup before it followed the move; the POST did not.
    assert ("GET", f"{_MARKETPLACE}/{_MOVED_BLUEPRINT['id']}") in new.recorded
    assert ("POST", f"{_MARKETPLACE}/{_MOVED_BLUEPRINT['id']}/instantiate") in old.recorded
    assert [m for m, _p in new.recorded if m == "POST"] == [] and new.bodies == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer probe-not-a-secret"},  # pragma: allowlist secret
        {"X-API-Key": "probe-not-a-secret"},  # pragma: allowlist secret
        {"Cookie": "session=probe-not-a-secret"},  # pragma: allowlist secret
        {"X-Custom": "anything"},
    ],
    ids=["authorization", "api-key", "cookie", "any-other-header"],
)
def test_a_named_registry_request_with_a_header_of_its_own_refuses_another_origin(
    moved_registry, headers
):
    from fluid_build.cli import marketplace

    old, new = moved_registry

    with pytest.raises(requests.exceptions.RequestException, match="another origin"):
        marketplace._marketplace_get(
            f"{old.url}{_MARKETPLACE}/x",
            logging.getLogger("t"),
            named_registry=True,
            headers=headers,
        )

    assert new.recorded == []


@pytest.mark.parametrize("method", ["post", "put"])
def test_a_named_registry_request_with_a_body_refuses_another_origin(moved_registry, method):
    from fluid_build.cli import marketplace

    old, new = moved_registry

    with pytest.raises(requests.exceptions.RequestException, match="another origin"):
        marketplace._marketplace_request(
            getattr(requests, method),
            f"{old.url}{_MARKETPLACE}/x/instantiate",
            logging.getLogger("t"),
            named_registry=True,
            json={"parameters": _PARAMETERS},
        )

    assert new.recorded == [] and new.bodies == []


# A registry that moves host after a hop on its own origin. On that later hop
# requests adds two headers of its own accord, and neither is the operator's:
# the Cookie its per-call jar built from a Set-Cookie in this redirect chain
# (Session.resolve_redirects), and, for an http:// URL, the Proxy-Authorization
# of the operator's proxy (Session.rebuild_proxies), which goes to the proxy.
# A named registry's move is followed through both. A request that carried
# anything of its own from its first hop still stops at the origin.


@pytest.fixture
def sticky_moved_registry(clean_env, monkeypatch):
    """``(old, new)``: the old host behind a load balancer that pins a client
    with a cookie it sets on a same-origin 302, and answers a pinned request
    with the registry's move (301) to the new host."""
    for var in ("FLUID_API_URL", "FLUID_PUBLIC_REGISTRY", "FLUID_MARKETPLACE_FALLBACK"):
        monkeypatch.delenv(var, raising=False)

    def _new(path: str):
        if path.split("?")[0] == _MARKETPLACE:
            return _json_answer({"items": [_MOVED_BLUEPRINT], "total": 1})
        return _json_answer(_MOVED_BLUEPRINT)

    new = _Listener(_new)
    holder: dict = {}

    def _old(path: str):
        old = holder["old"]
        if "lb=node-3" not in old.headers[-1].get("cookie", ""):
            return 302, {"Location": f"{old.url}{path}", "Set-Cookie": "lb=node-3; Path=/"}, b""
        return 301, {"Location": f"{new.url}{path}"}, b""

    holder["old"] = _Listener(_old)
    try:
        yield holder["old"], new
    finally:
        holder["old"].close()
        new.close()


def test_a_named_registry_that_pins_a_cookie_and_then_moves_host_is_searched(
    sticky_moved_registry, monkeypatch, capsys
):
    import json

    from fluid_build.cli import marketplace

    old, new = sticky_moved_registry
    monkeypatch.setenv("FLUID_API_URL", f"{old.url}{_MARKETPLACE}")
    args = SimpleNamespace(
        query=None,
        category=None,
        tags=None,
        maturity=None,
        state=None,
        limit=5,
        sort="name",
        format="json",
    )
    api_url = marketplace.get_api_url(logging.getLogger("t"), required=False)
    capsys.readouterr()

    rc = marketplace.search_blueprints(args, logging.getLogger("t"), api_url)

    assert rc == 0
    assert _MOVED_BLUEPRINT["id"] in [bp["id"] for bp in json.loads(capsys.readouterr().out)]
    # The pinned hop carried the load balancer's cookie, the case this covers.
    assert [("cookie" in h) for h in old.headers] == [False, True]
    assert [m for m, _p in new.recorded] == ["GET"]


def test_a_blueprint_lookup_in_a_named_registry_that_pins_a_cookie_follows_its_move(
    sticky_moved_registry, monkeypatch
):
    from fluid_build.cli import marketplace

    old, new = sticky_moved_registry
    monkeypatch.setenv("FLUID_API_URL", f"{old.url}{_MARKETPLACE}")
    args = SimpleNamespace(blueprint_id=_MOVED_BLUEPRINT["id"], version=None, show_template=False)

    rc = marketplace.show_blueprint_info(
        args, logging.getLogger("t"), marketplace.get_api_url(logging.getLogger("t"))
    )

    assert rc == 0
    assert ("GET", f"{_MARKETPLACE}/{_MOVED_BLUEPRINT['id']}") in new.recorded


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cookies": {"session": "probe-not-a-secret"}},  # pragma: allowlist secret
        {"headers": {"X-API-Key": "probe-not-a-secret"}},  # pragma: allowlist secret
        {"auth": ("svc", "probe-not-a-secret")},  # pragma: allowlist secret
    ],
    ids=["operator-cookie", "api-key", "basic-auth"],
)
def test_a_named_registry_request_of_the_operators_still_stops_after_a_same_origin_hop(
    sticky_moved_registry, kwargs
):
    from fluid_build.cli import marketplace

    old, new = sticky_moved_registry

    with pytest.raises(requests.exceptions.RequestException, match="another origin"):
        marketplace._marketplace_get(
            f"{old.url}{_MARKETPLACE}/x", logging.getLogger("t"), named_registry=True, **kwargs
        )

    assert len(old.recorded) == 2  # the same-origin hop was followed
    assert new.recorded == []


_PROXIED_OLD = "http://registry.example.test"
_PROXIED_NEW = "http://blueprints.example.test"


@pytest.fixture
def authenticated_proxy(clean_env, monkeypatch):
    """A forward proxy on loopback, its credentials in HTTP_PROXY as corporate
    networks set them, that answers for two registry hosts itself (so neither
    name has to resolve): the old one moves the API under /v2 on its own
    origin, then to the new host."""

    def _route(target: str):  # the absolute-form target a proxy receives
        if target.startswith(f"{_PROXIED_OLD}/v2/"):
            return 301, {"Location": _PROXIED_NEW + target[len(_PROXIED_OLD) :]}, b""
        if target.startswith(f"{_PROXIED_OLD}/"):
            return 301, {"Location": f"{_PROXIED_OLD}/v2{target[len(_PROXIED_OLD):]}"}, b""
        if target.startswith(f"{_PROXIED_NEW}/"):
            return _json_answer(_MOVED_BLUEPRINT)
        return 404, {}, b""

    proxy = _Listener(_route)
    port = proxy.server.server_address[1]
    url = f"http://proxyuser:proxy-pass-not-a-secret@127.0.0.1:{port}"  # pragma: allowlist secret
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTP_PROXY", url)
    monkeypatch.setenv("http_proxy", url)
    try:
        yield proxy
    finally:
        proxy.close()


def test_a_named_registry_through_an_authenticated_proxy_follows_its_move(authenticated_proxy):
    from fluid_build.cli import marketplace

    response = marketplace._marketplace_get(
        f"{_PROXIED_OLD}{_MARKETPLACE}/x", logging.getLogger("t"), named_registry=True
    )

    assert response.json()["id"] == _MOVED_BLUEPRINT["id"]
    assert [p for _m, p in authenticated_proxy.recorded] == [
        f"{_PROXIED_OLD}{_MARKETPLACE}/x",
        f"{_PROXIED_OLD}/v2{_MARKETPLACE}/x",
        f"{_PROXIED_NEW}/v2{_MARKETPLACE}/x",
    ]
    # Every hop carried the proxy's credential to the proxy, the case this covers.
    assert all("proxy-authorization" in h for h in authenticated_proxy.headers)


def test_a_named_registry_request_with_a_key_through_the_proxy_still_stops(authenticated_proxy):
    from fluid_build.cli import marketplace

    with pytest.raises(requests.exceptions.RequestException, match="another origin"):
        marketplace._marketplace_get(
            f"{_PROXIED_OLD}{_MARKETPLACE}/x",
            logging.getLogger("t"),
            named_registry=True,
            headers={"X-API-Key": "probe-not-a-secret"},  # pragma: allowlist secret
        )

    assert not [p for _m, p in authenticated_proxy.recorded if p.startswith(_PROXIED_NEW)]


def test_the_command_center_marketplace_is_not_a_named_registry(clean_env, monkeypatch):
    from fluid_build.cli import marketplace

    cc = _Listener(_ok)
    try:
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", cc.url)
        url = _marketplace_url()
    finally:
        cc.close()

    assert not isinstance(url, marketplace.NamedRegistryURL)
