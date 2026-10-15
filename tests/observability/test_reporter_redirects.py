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

"""A credential-bearing request never follows a redirect off its own origin.

The Command Center reporter sends ``X-API-Key`` (and, for a bearer credential,
``Authorization``) as session headers. ``requests`` follows redirects by
default and drops only ``Authorization`` when the host changes, so a Command
Center (or anything in front of it) answering ``307 Location: <elsewhere>``
used to receive the run, and the key, at the second address. Two real loopback
listeners on different ports are two origins: the first redirects, the second
records what reaches it.

The catalog publish clients (the Command Center provider, the DMM / DataHub /
OpenMetadata registrars) use httpx with redirects off; the last tests pin that.
"""

from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

pytestmark = pytest.mark.unit

_KEY = "cc-redirect-key-not-a-secret"  # pragma: allowlist secret
_TOKEN = "cc-redirect-bearer-not-a-secret"  # pragma: allowlist secret
_ORG = "0f5e3c1a-telco"


class _Listener:
    """A loopback HTTP server that records every request, and may redirect it."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        #: ``(status, location)`` for every request not under ``/moved``; None answers.
        self.redirect: Optional[Tuple[int, str]] = None
        self.url = ""

    def carrying(self, *needles: str) -> List[Dict[str, Any]]:
        return [
            r
            for r in self.requests
            if any(n in str(r["headers"]) or n in r["raw"] or n in r["path"] for n in needles)
        ]


def _serve(listener: _Listener) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            listener.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "raw": raw.decode("utf-8", "replace"),
                }
            )
            if self.command == "GET" and self.path.startswith("/api/v1/organizations"):
                self._json(200, [{"id": _ORG, "slug": "telco", "name": "Telco"}])
                return
            if listener.redirect and not self.path.startswith("/moved"):
                status, location = listener.redirect
                self.send_response(status)
                self.send_header("Location", f"{location}{self.path}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json(201 if self.command == "POST" else 200, {"id": "x1", "ok": True})

        def _json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer  # noqa: N815

        def log_message(self, *_a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    listener.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def two_origins() -> Iterator[Tuple[_Listener, _Listener]]:
    """``(origin, elsewhere)``: two loopback listeners, so two origins."""
    origin, elsewhere = _Listener(), _Listener()
    servers = [_serve(origin), _serve(elsewhere)]
    try:
        yield origin, elsewhere
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _reporter(url: str):
    from fluid_build.observability.config import CommandCenterConfig
    from fluid_build.observability.reporter import CommandCenterReporter

    config = CommandCenterConfig(
        url=url,
        api_key=_KEY,
        headers={"Authorization": f"Bearer {_TOKEN}", "X-Organization-Id": _ORG},
    )
    return CommandCenterReporter(config)


def _report_a_run(reporter) -> None:
    reporter.start()
    assert reporter.running, "the host gate lets a loopback endpoint through"
    reporter.register_execution(execution_id="run-1", command="apply")
    reporter.update_execution(execution_id="run-1", status="success")
    reporter.stop()


# ── the reporter ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_reporter_does_not_follow_a_redirect_to_another_origin(two_origins, status, caplog):
    origin, elsewhere = two_origins
    origin.redirect = (status, elsewhere.url)
    reporter = _reporter(origin.url)

    _report_a_run(reporter)

    assert elsewhere.carrying(_KEY, _TOKEN) == []
    assert elsewhere.requests == []
    assert reporter.stats == {"sent": 0, "failed": 2}
    assert elsewhere.url in caplog.text  # where it was refused to go: the origin only
    assert _KEY not in caplog.text


def test_the_reporter_still_follows_a_redirect_on_its_own_origin(two_origins):
    origin, _ = two_origins
    origin.redirect = (307, f"{origin.url}/moved")
    reporter = _reporter(origin.url)

    _report_a_run(reporter)

    moved = [r for r in origin.requests if r["path"].startswith("/moved")]
    assert [r["method"] for r in moved] == ["POST", "PATCH"]
    assert all(r["headers"]["X-API-Key"] == _KEY for r in moved)
    assert reporter.stats == {"sent": 2, "failed": 0}


class _TlsRedirect:
    """A requests transport adapter standing in for the TLS hop: answers 307 to ``location``."""

    def __init__(self, location: str) -> None:
        self.location = location

    def send(self, request: Any, **_kwargs: Any) -> Any:
        import requests

        response = requests.Response()
        response.status_code = 307
        response.reason = "Temporary Redirect"
        response.headers["Location"] = self.location
        response.url = request.url
        response.request = request
        response.raw = io.BytesIO(b"")
        response.connection = self
        return response

    def close(self) -> None:
        pass


def test_the_reporter_does_not_follow_https_down_to_http_on_the_same_host(two_origins):
    _, plain = two_origins
    port = plain.url.rsplit(":", 1)[1]
    reporter = _reporter(f"https://127.0.0.1:{port}")
    # Only the TLS answer is staged; the http hop it redirects to is the real listener.
    reporter.session.mount(f"https://127.0.0.1:{port}", _TlsRedirect(f"{plain.url}/api/v1/x"))

    _report_a_run(reporter)

    assert plain.carrying(_KEY, _TOKEN) == []
    assert plain.requests == []
    assert reporter.stats["sent"] == 0


@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        ("https://cc.example.com/a", "https://cc.example.com/b", True),
        ("https://cc.example.com/a", "https://CC.example.com:443/b", True),
        ("http://cc.example.com/a", "https://cc.example.com/b", True),  # upgrade, default ports
        ("https://cc.example.com/a", "http://cc.example.com/a", False),  # downgrade
        ("https://cc.example.com/a", "https://cc.example.com:8443/a", False),
        ("https://cc.example.com/a", "https://evil.example.com/a", False),
        ("https://cc.example.com/a", "https://cc.example.com.evil.example/a", False),
        ("http://127.0.0.1:8000/a", "http://127.0.0.1:9000/a", False),
        ("http://127.0.0.1:8000/a", "https://127.0.0.1:8000/a", False),  # not a default port
        ("https://cc.example.com/a", "ftp://cc.example.com/a", False),
    ],
)
def test_which_redirects_a_credential_may_follow(source, target, allowed):
    from fluid_build.observability.reporter import credential_redirect_refusal

    assert (credential_redirect_refusal(source, target) is None) is allowed


# ── fluid apply's run report ──────────────────────────────────────────────


from tests.cli.test_apply_reports_to_command_center import (  # noqa: E402,F401
    _apply,
    product,
    tofu,
)


def test_fluid_apply_does_not_send_its_report_through_a_cross_origin_redirect(
    product, tofu, two_origins, monkeypatch, capsys
):
    origin, elsewhere = two_origins
    origin.redirect = (307, elsewhere.url)
    monkeypatch.setenv("FLUID_CC_ENDPOINT", origin.url)
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    monkeypatch.setenv("FLUID_CC_ORG_ID", _ORG)

    assert _apply(product) == 0

    assert elsewhere.carrying(_KEY) == []
    assert elsewhere.requests == []
    out = " ".join(capsys.readouterr().out.split())
    assert "not fully reported" in out


# ── the catalog publish clients do not follow redirects at all ────────────


_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.5",
    "kind": "DataProduct",
    "id": "bronze.redirect_probe",
    "name": "Redirect Probe",
    "version": "1.0.0",
    "description": "fixture",
    "domain": "Customer",
    "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "dp@example.com"}},
    "exposes": [
        {
            "exposeId": "orders_raw",
            "kind": "table",
            "binding": {"platform": "local", "location": {"path": "data/o.parquet"}},
            "contract": {"schema": [{"name": "id", "type": "STRING"}]},
        }
    ],
}


def _publish_payload():
    from fluid_build.api.catalog_publication import CatalogPublicationPayload

    return CatalogPublicationPayload.from_contract(_CONTRACT, {})


@pytest.mark.parametrize("target", ["datamesh_manager", "datahub", "openmetadata"])
def test_a_catalog_registrar_does_not_follow_a_redirect(two_origins, target):
    from fluid_build.build_runners import _catalog

    origin, elsewhere = two_origins
    origin.redirect = (307, elsewhere.url)
    registrar = _catalog.build_registrar(target, {"endpoint": origin.url, "api_token": _KEY})
    assert registrar is not None

    registrar.register_payload(_publish_payload())

    assert origin.carrying(_KEY), "the registrar did talk to its endpoint"
    assert elsewhere.requests == []


def test_the_command_center_publisher_does_not_follow_a_redirect(two_origins, monkeypatch):
    import asyncio

    from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

    origin, elsewhere = two_origins
    origin.redirect = (307, elsewhere.url)
    monkeypatch.delenv("FLUID_CC_ORG_ID", raising=False)
    provider = FluidCommandCenterProvider(
        {
            "endpoint": origin.url,
            "organization_id": _ORG,
            "auth": {"type": "api_key", "api_key": _KEY},
        }
    )
    asset = provider.map_contract_to_asset(_CONTRACT)

    asyncio.run(provider.publish(asset))

    assert origin.carrying(_KEY), "the publisher did talk to its endpoint"
    assert elsewhere.requests == []
