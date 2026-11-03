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

"""Hardening of the shared HTTP client (security review of the httpx port).

* a redirect ``Location`` with a scheme but no host never escapes the origin gate;
* ``Retry-After`` is capped and a huge value does not crash;
* the Data Mesh Manager ``x-api-key`` does not follow a redirect off its origin;
* an unusable CA bundle does not crash reporter construction, nor relax TLS;
* the catalog connector keeps trusting the system CA store.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx
import pytest

from fluid_build.observability.reporter import CredentialRedirectRefused, refuse_foreign_redirect
from fluid_build.util.http_client import (
    new_async_client,
    new_client,
    parse_retry_after,
)

pytestmark = pytest.mark.unit

_KEY = "dmm-hardening-key-not-a-secret"  # pragma: allowlist secret


class _Server:
    def __init__(self) -> None:
        self.log: List[Dict[str, Any]] = []
        self.routes: Dict[str, Any] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                outer.log.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                status, headers = outer.routes.get(self.path.split("?")[0], (200, {}))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", "2")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(b"{}")

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _do

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def two_servers():
    a, b = _Server(), _Server()
    try:
        yield a, b
    finally:
        a.close()
        b.close()


_HOSTLESS = ["https:/stolen", "http:", "http:///stolen", "HTTP:/stolen", "https:stolen"]


# ── 1. hostless redirect ──────────────────────────────────────────────────


@pytest.mark.parametrize("location", _HOSTLESS)
def test_hostless_location_is_refused_on_a_mock_transport_sync(location):
    seen: List[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.port == 8443:
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(200)

    with new_client(
        headers={"X-API-Key": "k"},
        response_hooks=[refuse_foreign_redirect],
        transport=httpx.MockTransport(handler),
    ) as c:
        with pytest.raises(CredentialRedirectRefused):
            c.post("https://cc.example.com:8443/api/events", json={"r": 1})
    assert [r.url.port for r in seen] == [8443]


@pytest.mark.parametrize("location", _HOSTLESS)
def test_hostless_location_is_refused_on_a_mock_transport_async(location):
    seen: List[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(307, headers={"Location": location})

    async def run():
        async with new_async_client(
            response_hooks=[refuse_foreign_redirect], transport=httpx.MockTransport(handler)
        ) as c:
            await c.post("https://cc.example.com:8443/api/events", json={"r": 1})

    with pytest.raises(CredentialRedirectRefused):
        asyncio.run(run())
    assert len(seen) == 1


@pytest.mark.parametrize("location", _HOSTLESS)
def test_hostless_location_is_refused_against_a_real_server_sync_and_async(two_servers, location):
    a, b = two_servers
    a.routes["/go"] = (307, {"Location": location})
    with new_client(response_hooks=[refuse_foreign_redirect]) as c:
        with pytest.raises(CredentialRedirectRefused):
            c.post(a.base + "/go", json={"r": 1}, headers={"X-API-Key": "k"})

    async def run():
        async with new_async_client(response_hooks=[refuse_foreign_redirect]) as c:
            await c.get(a.base + "/go", headers={"X-API-Key": "k"})

    with pytest.raises(CredentialRedirectRefused):
        asyncio.run(run())
    assert [r["path"] for r in a.log] == ["/go", "/go"]
    assert b.log == []


def test_legitimate_redirects_are_still_followed(two_servers):
    a, _ = two_servers
    a.routes["/rel"] = (302, {"Location": "/landed"})
    a.routes["/abs"] = (302, {"Location": a.base + "/landed"})
    a.routes["/dotrel"] = (302, {"Location": "../landed"})
    with new_client(response_hooks=[refuse_foreign_redirect]) as c:
        for p in ("/rel", "/abs", "/dotrel"):
            assert c.get(a.base + p).status_code == 200


def test_default_port_is_filled_in_when_comparing_origins():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://cc.example.com:443/next"})
        return httpx.Response(200)

    with new_client(
        response_hooks=[refuse_foreign_redirect], transport=httpx.MockTransport(handler)
    ) as c:
        assert c.get("https://cc.example.com/start").status_code == 200


# ── 2. Retry-After ────────────────────────────────────────────────────────


def _retrying_mock(retry_after: str):
    sleeps: List[float] = []
    calls: List[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        return httpx.Response(429, headers={"Retry-After": retry_after})

    c = new_client()
    # ``transport=`` would bypass the retry wrapper; install it as the factory does.
    from fluid_build.util.http_client import RetryTransport

    c._transport = RetryTransport(
        transport=httpx.MockTransport(handler),
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429],
        allowed_methods=["GET"],
        sleep=sleeps.append,
    )
    return c, sleeps, calls


def test_a_huge_retry_after_is_capped_at_the_backoff_ceiling():
    c, sleeps, calls = _retrying_mock("86400000")
    with c, pytest.raises(httpx.TransportError):
        c.get("http://dmm.example/api/x")
    assert sleeps and max(sleeps) == 120.0
    assert len(calls) == 4


def test_a_small_retry_after_is_honoured_exactly():
    c, sleeps, _ = _retrying_mock("7")
    with c, pytest.raises(httpx.TransportError):
        c.get("http://dmm.example/api/x")
    assert sleeps == [7.0, 7.0, 7.0]


def test_a_400_digit_retry_after_does_not_raise():
    assert parse_retry_after("9" * 400) is None
    c, sleeps, _ = _retrying_mock("9" * 400)
    with c, pytest.raises(httpx.TransportError):
        c.get("http://dmm.example/api/x")
    assert sleeps == [1.0, 2.0]  # unparseable: plain backoff


def test_an_http_date_retry_after_still_works():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    got = parse_retry_after(format_datetime(when, usegmt=True))
    assert got is not None and 20 <= got <= 31
    assert parse_retry_after("Fri, 31 Dec 9999 23:59:59 GMT") is not None


def test_the_catalog_registrar_retry_cap_still_holds():
    from fluid_build.build_runners.catalog_registrars._http_retry import (
        DEFAULT_MAX_DELAY,
        RetryPolicy,
        compute_delay,
    )

    assert compute_delay(1, RetryPolicy(), 86400000.0) == DEFAULT_MAX_DELAY


# ── 3. Data Mesh Manager redirect ─────────────────────────────────────────


@pytest.fixture()
def dmm(two_servers, monkeypatch):
    a, b = two_servers
    monkeypatch.setenv("DMM_API_URL", a.base)
    monkeypatch.setenv("DMM_ALLOW_INSECURE_HTTP", "1")
    from fluid_build.providers.datamesh_manager.datamesh_manager import DataMeshManagerProvider

    return DataMeshManagerProvider(api_key=_KEY), a, b


@pytest.mark.parametrize(
    "method,status,body",
    [("GET", 302, None), ("PUT", 302, {"a": 1}), ("PUT", 307, {"payload": "body"})],
)
def test_dmm_does_not_send_its_key_or_body_to_another_origin(dmm, method, status, body):
    from fluid_build.providers.base import ProviderError

    prov, a, b = dmm
    a.routes["/api/dataproducts/x"] = (status, {"Location": f"{b.base}/landed"})
    with pytest.raises(ProviderError) as ei:
        prov._request(method, "/api/dataproducts/x", json_body=body)
    msg = str(ei.value)
    assert b.base.replace("http://", "") in msg and "DMM_API_URL" in msg
    assert b.log == []
    assert len([r for r in a.log if r["path"] == "/api/dataproducts/x"]) == 1  # not retried
    assert _KEY not in msg


def test_dmm_still_follows_a_same_origin_redirect(dmm):
    prov, a, b = dmm
    a.routes["/api/dataproducts/x"] = (307, {"Location": "/api/moved"})
    assert prov._request("GET", "/api/dataproducts/x").status_code == 200
    assert b.log == []


# ── 4. missing CA bundle ──────────────────────────────────────────────────


@pytest.mark.parametrize("var", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_a_missing_ca_bundle_does_not_crash_reporter_construction(
    monkeypatch, tmp_path, caplog, var
):
    from fluid_build.observability.config import CommandCenterConfig
    from fluid_build.observability.reporter import CommandCenterReporter

    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setenv(var, str(tmp_path / "does-not-exist.pem"))
    cfg = CommandCenterConfig(url="https://cc.example.com", api_key=_KEY)
    with caplog.at_level(logging.WARNING):
        reporter = CommandCenterReporter(cfg)
    assert reporter.enabled is False and reporter.session is None
    reporter.start()
    assert not reporter.running
    assert var in caplog.text


def test_a_missing_ca_bundle_never_falls_back_to_unverified_tls(monkeypatch, tmp_path):
    from fluid_build.util.http_client import CaBundleError

    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "nope.pem"))
    with pytest.raises(CaBundleError, match="REQUESTS_CA_BUNDLE"):
        new_client()


# ── 5. system trust store for the catalog connector ──────────────────────


def test_system_trust_client_uses_the_default_verify_paths(monkeypatch):
    real = ssl.create_default_context
    calls: List[Any] = []

    def spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr("fluid_build.util.http_client.ssl.create_default_context", spy)
    client = new_async_client(trust_env=False, system_trust=True)
    assert calls == [((), {})]  # no cafile / capath: the system defaults
    ctx = client._transport._pool._ssl_context
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    asyncio.run(client.aclose())


def test_the_catalog_connector_asks_for_system_trust(monkeypatch):
    from fluid_build.cli.market_catalogs.command_center import CommandCenterConnector

    cc = SimpleNamespace(
        available=True,
        features=SimpleNamespace(catalog=True),
        get_catalog_url=lambda: "https://cc.example.com/api/catalog",
    )
    monkeypatch.setattr(
        "fluid_build.cli._command_center.get_command_center_client", lambda **kw: cc
    )
    seen: Dict[str, Any] = {}
    real = new_async_client

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr("fluid_build.util.http_client.new_async_client", spy)

    async def run():
        conn = CommandCenterConnector({}, logging.getLogger("t"))
        assert await conn._connect_impl() is True
        await conn.session.aclose()

    asyncio.run(run())
    assert seen["system_trust"] is True


# ── 6. a non-JSON 200 from the marketplace is an error, not a crash ───────

_HTML = "<html>not json</html>"
_BP = "not-a-bundled-blueprint"


def _args(**kw):
    base = dict(
        query="zzz-matches-no-bundled-blueprint",
        category=None,
        tags=None,
        maturity=None,
        state="published",
        sort="downloads",
        limit=20,
        format="json",
        blueprint_id=_BP,
        version=None,
        params='{"a": 1}',
        interactive=False,
        output=None,
        submit=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_search_with_a_non_json_200_reports_instead_of_crashing(capsys):
    import respx

    from fluid_build.cli.marketplace import search_blueprints

    with respx.mock(assert_all_called=False) as router:
        router.get("http://api").respond(200, text=_HTML)
        rc = search_blueprints(_args(), logging.getLogger("t"), "http://api")
    assert rc == 1
    assert capsys.readouterr().out.strip() == "[]"


def test_info_with_a_non_json_200_reports_instead_of_crashing():
    import respx

    from fluid_build.cli.marketplace import show_blueprint_info

    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://api/{_BP}").respond(200, text=_HTML)
        assert show_blueprint_info(_args(), logging.getLogger("t"), "http://api") == 1


def test_instantiate_with_a_non_json_200_reports_instead_of_crashing():
    import respx

    from fluid_build.cli.marketplace import instantiate_blueprint

    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://api/{_BP}").respond(200, text=_HTML)
        assert instantiate_blueprint(_args(), logging.getLogger("t"), "http://api") == 1
    with respx.mock(assert_all_called=False) as router:
        router.get(f"http://api/{_BP}").respond(200, json={"id": _BP, "name": "X"})
        router.post(f"http://api/{_BP}/instantiate").respond(200, text=_HTML)
        assert instantiate_blueprint(_args(), logging.getLogger("t"), "http://api") == 1


# ── 7. params= merge into the URL's own query string ──────────────────────


def test_merge_query_keeps_the_urls_own_pairs_and_empty_params_keep_them_all():
    from fluid_build.util.http_client import merge_query

    out = httpx.URL(merge_query("https://reg.example/api?tenant=acme", {"limit": 1}))
    assert dict(out.params) == {"tenant": "acme", "limit": "1"}
    assert merge_query("https://reg.example/api?tenant=acme", {}) == (
        "https://reg.example/api?tenant=acme"
    )


def test_marketplace_search_keeps_the_registry_urls_query():
    import respx

    from fluid_build.cli.marketplace import NamedRegistryURL, search_blueprints

    url = NamedRegistryURL("https://reg.example/api/v1/blueprints?tenant=acme")
    with respx.mock(assert_all_called=False) as router:
        route = router.get(host="reg.example").respond(200, json={"items": [], "total": 0})
        search_blueprints(_args(format=None, query="q"), logging.getLogger("t"), url)
    params = route.calls.last.request.url.params
    assert params["tenant"] == "acme" and params["limit"] == "20"


def test_command_center_endpoint_probe_keeps_the_endpoints_query(monkeypatch):
    import respx

    from fluid_build.cli._command_center import CommandCenterClient

    monkeypatch.setenv("FLUID_DISABLE_CC_DETECTION", "true")
    client = CommandCenterClient()
    client.url = "https://cc.example.com"
    with respx.mock(assert_all_called=False) as router:
        router.head(host="cc.example.com").respond(500)
        get = router.get(host="cc.example.com").respond(200)
        assert client._check_endpoint("/api/v1/x?tenant=acme") is True
    params = get.calls.last.request.url.params
    assert params["tenant"] == "acme" and params["limit"] == "1"


# ── 8. safe_request: errors are 4xx/5xx only; requests-only kwargs ────────


def _safe(status, headers=None, **kw):
    from fluid_build.util.network import safe_request

    def handler(req):
        return httpx.Response(status, headers=headers or {})

    session = httpx.Client(transport=httpx.MockTransport(handler))
    return safe_request("GET", "http://svc.example/x", session=session, **kw)


@pytest.mark.parametrize("status", [304, 301])
def test_safe_request_does_not_raise_on_a_non_error_status(status):
    assert _safe(status).status_code == status


def test_safe_request_still_raises_on_4xx_and_5xx():
    from fluid_build.errors import NetworkError

    for status in (404, 503):
        with pytest.raises(NetworkError):
            _safe(status)


def test_safe_request_translates_allow_redirects():
    from fluid_build.util.network import safe_request

    def handler(req):
        if req.url.path == "/a":
            return httpx.Response(302, headers={"Location": "/b"})
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as s:
        assert (
            safe_request("GET", "http://s/a", session=s, allow_redirects=False).status_code == 302
        )
        assert safe_request("GET", "http://s/a", session=s, allow_redirects=True).status_code == 200


@pytest.mark.parametrize("name", ["verify", "cert", "stream"])
def test_safe_request_refuses_requests_only_kwargs_loudly(name):
    with pytest.raises(TypeError, match=name):
        _safe(200, **{name: True})


# ── 9. the catalog connector reads the real SearchFilters ─────────────────


def test_catalog_connector_search_uses_the_real_search_filters_and_keeps_the_query():
    from fluid_build.cli.market import (
        DataProductLayer,
        DataProductStatus,
        SearchFilters,
    )
    from fluid_build.cli.market_catalogs.command_center import CommandCenterConnector

    seen: List[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"items": []})

    async def run():
        conn = CommandCenterConnector({}, logging.getLogger("t"))
        conn.base_url = "https://cc.example.com/api/catalog?tenant=acme"
        conn.session = new_async_client(transport=httpx.MockTransport(handler))
        filters = SearchFilters(
            text_query="orders",
            domain="sales",
            owner="bob",
            layer=DataProductLayer.GOLD,
            status=DataProductStatus.ACTIVE,
            tags=["a", "b"],
        )
        try:
            return await conn._search_data_products_impl(filters)
        finally:
            await conn.session.aclose()

    assert asyncio.run(run()) == []
    params = seen[0].url.params
    assert dict(params) == {
        "tenant": "acme",
        "query": "orders",
        "domains": "sales",
        "owners": "bob",
        "tags": "a,b",
        "layers": "gold",
        "statuses": "active",
        "limit": "50",
    }
