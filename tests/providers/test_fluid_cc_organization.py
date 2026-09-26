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

"""``fluid publish --target fluid-command-center`` against a Command Center.

The Command Center creates every asset inside one organization and refuses a
create that does not name it: ``POST /api/v1/assets`` answers 400 unless the
request carries ``X-Organization-Id``, and 403 unless the caller is an active
member of that organization (``fluid_cc_backend/app/api/v1/assets.py``
``create_asset`` and ``app/api/dependencies.py`` ``require_org_scope``). The
provider never sent the header, so every first-time publish failed.

These tests run the real provider against a loopback HTTP server that answers
the way those routes do, and record every request it receives.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import pytest

from fluid_build.providers.catalogs import CATALOG_PROVIDERS, get_catalog_provider
from fluid_build.providers.catalogs.base import CatalogAsset
from fluid_build.providers.catalogs.fluid_cc import FluidCommandCenterProvider

# The wire contract, spelled out rather than imported, so these tests pin what
# the Command Center reads and not whatever the provider happens to export.
ORG_HEADER = "X-Organization-Id"
API_KEY = "fluid_test_key_not_a_secret"
ORG_A = {"id": "0b6f6c3e-org-a", "name": "Acme", "slug": "acme", "role": "owner"}
ORG_B = {"id": "5d1e2f4a-org-b", "name": "Globex", "slug": "globex", "role": "member"}

# The Command Center's own 400 detail for a header-less create.
NO_ORG_DETAIL = (
    "Creating an asset requires an organization context — set the X-Organization-Id header."
)


class _StubCommandCenter:
    """The slice of the Command Center API the provider talks to."""

    def __init__(self) -> None:
        self.organizations: List[Dict[str, Any]] = []
        self.organizations_status = 200
        self.requests: List[Dict[str, Any]] = []
        self.url = ""

    @property
    def member_ids(self) -> set:
        return {o["id"] for o in self.organizations if isinstance(o, dict)}

    def paths(self, method: Optional[str] = None) -> List[str]:
        return [r["path"] for r in self.requests if method is None or r["method"] == method]

    def posts(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]


def _handler_for(stub: _StubCommandCenter):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep test output clean
            return

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _record(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            entry = {
                "method": self.command,
                "path": urlsplit(self.path).path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(raw) if raw else None,
            }
            stub.requests.append(entry)
            return entry

        def _authenticated(self) -> bool:
            return self.headers.get("X-API-Key") == API_KEY

        def do_GET(self) -> None:  # noqa: N802 — http.server naming
            entry = self._record()
            if not self._authenticated():
                return self._send(401, {"detail": "Not authenticated"})
            if entry["path"] == "/api/v1/organizations":
                if stub.organizations_status != 200:
                    return self._send(stub.organizations_status, {"detail": "boom"})
                return self._send(200, stub.organizations)
            if entry["path"] == "/api/v1/assets":
                return self._send(200, {"items": [], "total": 0})
            return self._send(404, {"detail": "Not Found"})

        def do_POST(self) -> None:  # noqa: N802
            entry = self._record()
            if not self._authenticated():
                return self._send(401, {"detail": "Not authenticated"})
            if entry["path"] != "/api/v1/assets":
                return self._send(404, {"detail": "Not Found"})
            org = self.headers.get(ORG_HEADER)
            if not org:
                return self._send(400, {"detail": NO_ORG_DETAIL})
            if org not in stub.member_ids:
                return self._send(
                    403, {"detail": "Not an active member of the requested organization"}
                )
            body = dict(entry["body"] or {})
            body.update({"id": "asset-0001", "organization_id": org})
            return self._send(201, body)

    return Handler


@pytest.fixture
def cc_stub(monkeypatch):
    # Loopback only: no proxy may sit between the provider and the stub, and no
    # ambient Command Center settings may leak in from the developer's shell.
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "FLUID_CC_ORG_ID",
        "FLUID_CC_ENDPOINT",
        "FLUID_CATALOG_FLUID_CC_URL",
        "FLUID_API_KEY",
        "FLUID_CATALOG_FLUID_CC_TOKEN",
        "FLUID_BEARER_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)

    stub = _StubCommandCenter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(stub))
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _provider(stub: _StubCommandCenter, **extra: Any) -> FluidCommandCenterProvider:
    config: Dict[str, Any] = {
        "endpoint": stub.url,
        "auth": {"type": "api_key", "api_key": API_KEY},
        "max_retries": 3,
        "retry_delay": 0,
        "timeout": 5.0,
    }
    config.update(extra)
    provider = get_catalog_provider("fluid-command-center", config)
    assert isinstance(provider, FluidCommandCenterProvider)
    return provider


def _asset() -> CatalogAsset:
    return CatalogAsset(
        id="bronze.customer_subscriptions",
        name="Customer Subscriptions",
        description="Subscriptions from the SID source",
        type="dataproduct",
        domain="Customer",
        owner="data-platform",
        owner_email="data-platform@example.com",
        layer="Bronze",
        tags=["subscriptions"],
        version="1.0.0",
        platform="local",
        location={"path": "out/customer_subscriptions.parquet"},
    )


# ---------------------------------------------------------------------------
# Where the organization comes from
# ---------------------------------------------------------------------------


class TestOrganizationHeader:
    def test_env_org_id_goes_on_every_request_and_the_create_succeeds(self, cc_stub, monkeypatch):
        cc_stub.organizations = [ORG_A, ORG_B]
        monkeypatch.setenv("FLUID_CC_ORG_ID", ORG_B["id"])

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert result.success, result.error
        assert result.details["operation"] == "create"
        assert result.details["organization_id"] == ORG_B["id"]
        # An explicit id needs no lookup, and every request is scoped by it.
        assert "/api/v1/organizations" not in cc_stub.paths()
        assert cc_stub.requests, "the provider made no requests"
        for request in cc_stub.requests:
            assert request["headers"].get("x-organization-id") == ORG_B["id"], request
        assert len(cc_stub.posts()) == 1

    def test_config_organization_id_is_used(self, cc_stub):
        cc_stub.organizations = [ORG_A, ORG_B]

        result = asyncio.run(_provider(cc_stub, organization_id=ORG_A["id"]).publish(_asset()))

        assert result.success, result.error
        (post,) = cc_stub.posts()
        assert post["headers"]["x-organization-id"] == ORG_A["id"]
        assert "/api/v1/organizations" not in cc_stub.paths()

    def test_the_only_organization_is_picked_when_none_is_configured(self, cc_stub):
        cc_stub.organizations = [ORG_A]

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert result.success, result.error
        paths = cc_stub.paths()
        assert paths.count("/api/v1/organizations") == 1
        # Everything after the lookup carries the organization it found.
        after_lookup = cc_stub.requests[paths.index("/api/v1/organizations") + 1 :]
        assert any(r["method"] == "POST" for r in after_lookup)
        for request in after_lookup:
            assert request["headers"].get("x-organization-id") == ORG_A["id"], request

    def test_resolution_is_cached_for_the_provider(self, cc_stub):
        cc_stub.organizations = [ORG_A]
        provider = _provider(cc_stub)

        asyncio.run(provider.publish(_asset()))
        asyncio.run(provider.publish(_asset()))

        assert cc_stub.paths().count("/api/v1/organizations") == 1
        assert len(cc_stub.posts()) == 2

    def test_no_organization_is_a_typed_error_and_nothing_is_created(self, cc_stub):
        cc_stub.organizations = []
        provider = _provider(cc_stub)

        result = asyncio.run(provider.publish(_asset()))
        assert cc_stub.posts() == []
        assert not result.success
        assert "no organization" in result.error
        assert "FLUID_CC_ORG_ID" in result.error
        assert result.details["error_code"] == "cc_organization_unresolved"

        from fluid_build.providers.catalogs.fluid_cc.provider import (
            CommandCenterOrganizationError,
        )

        with pytest.raises(CommandCenterOrganizationError) as excinfo:
            asyncio.run(provider.resolve_organization_id())
        assert excinfo.value.organizations == []

    def test_several_organizations_fail_listing_each_slug_and_id(self, cc_stub):
        cc_stub.organizations = [ORG_A, ORG_B]
        provider = _provider(cc_stub)

        result = asyncio.run(provider.publish(_asset()))
        # Ambiguity is configuration, not a transient fault: no retries, no create.
        assert cc_stub.posts() == []
        assert cc_stub.paths().count("/api/v1/organizations") == 1
        assert not result.success
        for org in (ORG_A, ORG_B):
            assert org["slug"] in result.error
            assert org["id"] in result.error
        assert "FLUID_CC_ORG_ID" in result.error
        assert "organization_id" in result.error
        assert result.details["error_code"] == "cc_organization_unresolved"
        assert [o["id"] for o in result.details["organizations"]] == [ORG_A["id"], ORG_B["id"]]

        from fluid_build.providers.catalogs.fluid_cc.provider import (
            CommandCenterOrganizationError,
        )

        with pytest.raises(CommandCenterOrganizationError) as excinfo:
            asyncio.run(provider.resolve_organization_id())
        assert [o["slug"] for o in excinfo.value.organizations] == ["acme", "globex"]

    def test_an_unreadable_organization_list_is_a_typed_error(self, cc_stub):
        cc_stub.organizations = [ORG_A]
        cc_stub.organizations_status = 500

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert not result.success
        assert "HTTP 500" in result.error
        assert "FLUID_CC_ORG_ID" in result.error
        assert API_KEY not in result.error
        assert cc_stub.posts() == []

    def test_a_rejected_credential_is_reported_as_that(self, cc_stub):
        cc_stub.organizations = [ORG_A]
        cc_stub.organizations_status = 401

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert not result.success
        assert "rejected the credential (HTTP 401)" in result.error
        assert API_KEY not in result.error
        assert cc_stub.posts() == []

    def test_a_blank_env_org_id_counts_as_unset(self, cc_stub, monkeypatch):
        cc_stub.organizations = [ORG_A]
        monkeypatch.setenv("FLUID_CC_ORG_ID", "   ")

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert result.success, result.error
        assert cc_stub.paths().count("/api/v1/organizations") == 1
        (post,) = cc_stub.posts()
        assert post["headers"]["x-organization-id"] == ORG_A["id"]

    def test_server_labels_reach_the_error_without_control_characters(self, cc_stub):
        cc_stub.organizations = [
            {"id": ORG_A["id"], "slug": "acme\n[fake] published ok", "name": "Acme"},
            {"id": ORG_B["id"], "slug": "\x1b[31mglobex\x1b[0m", "name": "Globex"},
        ]

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert not result.success
        assert "\n" not in result.error
        assert "\x1b" not in result.error
        assert "acme[fake] published ok" in result.error
        assert "[31mglobex[0m" in result.error


# ---------------------------------------------------------------------------
# The header value is sent verbatim, so it has to be safe to send
# ---------------------------------------------------------------------------


class TestOrganizationIdIsHeaderSafe:
    @pytest.mark.parametrize(
        "bad",
        [f"{ORG_A['id']}\r\nX-Injected: 1", "acme corp", "org-\x00", "ü-org", "x" * 129],
    )
    def test_a_malformed_configured_id_is_refused_before_it_is_sent(self, cc_stub, bad):
        cc_stub.organizations = [ORG_A]

        result = asyncio.run(_provider(cc_stub, organization_id=bad).publish(_asset()))

        assert cc_stub.posts() == []
        for request in cc_stub.requests:
            assert "x-organization-id" not in request["headers"], request
            assert "x-injected" not in request["headers"], request
        assert not result.success
        assert result.details["error_code"] == "cc_organization_unresolved"

    def test_server_listed_ids_that_are_not_header_safe_are_ignored(self, cc_stub):
        cc_stub.organizations = [
            {"id": "evil\r\nX-Injected: 1", "slug": "evil", "name": "Evil"},
            # A trailing newline must not slip past the check either.
            {"id": "evil-trailing\n", "slug": "evil2", "name": "Evil 2"},
            ORG_A,
        ]

        result = asyncio.run(_provider(cc_stub).publish(_asset()))

        assert result.success, result.error
        (post,) = cc_stub.posts()
        assert post["headers"]["x-organization-id"] == ORG_A["id"]
        assert all("x-injected" not in r["headers"] for r in cc_stub.requests)


class TestVerifyIsScoped:
    def test_verify_only_lookup_carries_the_organization(self, cc_stub):
        cc_stub.organizations = [ORG_A]

        found = asyncio.run(_provider(cc_stub).verify("bronze.customer_subscriptions"))

        assert found is False  # the stub holds no assets
        lookups = [r for r in cc_stub.requests if r["path"] == "/api/v1/assets"]
        assert lookups
        assert all(r["headers"].get("x-organization-id") == ORG_A["id"] for r in lookups)

    def test_verify_with_an_ambiguous_organization_is_false_not_a_crash(self, cc_stub):
        cc_stub.organizations = [ORG_A, ORG_B]

        assert asyncio.run(_provider(cc_stub).verify("bronze.x")) is False
        assert "/api/v1/assets" not in cc_stub.paths()


# ---------------------------------------------------------------------------
# ``command-center`` is the name the help text and the generated pipelines use
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """A FluidConfig that reads no user or project config file."""
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(work)
    monkeypatch.delenv("FLUID_SECRETS_FILE", raising=False)
    return work


class TestCommandCenterAlias:
    def test_alias_is_registered_to_the_native_provider(self):
        assert CATALOG_PROVIDERS["command-center"] is FluidCommandCenterProvider

    def test_every_registered_name_resolves_the_command_center_config(self, isolated_config):
        """A name the registry accepts must also find its config, or ``fluid
        publish --target <name>`` answers "not configured" for a registered
        target (what ``fluid_cc`` and ``command-center`` both did)."""
        from fluid_build.config_manager import FluidConfig

        registered = sorted(
            name for name, cls in CATALOG_PROVIDERS.items() if cls is FluidCommandCenterProvider
        )
        assert "command-center" in registered
        canonical = FluidConfig().get_catalog_config("fluid-command-center")
        assert canonical.get("endpoint")
        for name in registered:
            assert FluidConfig().get_catalog_config(name) == canonical, name

    def test_alias_reads_the_canonical_config_block_and_env(self, isolated_config, monkeypatch):
        from fluid_build.config_manager import FluidConfig

        canonical = FluidConfig().get_catalog_config("fluid-command-center")
        assert FluidConfig().get_catalog_config("command-center") == canonical

        monkeypatch.setenv("FLUID_CC_ENDPOINT", "https://cc.example.test")
        monkeypatch.setenv("FLUID_API_KEY", "k")
        monkeypatch.setenv("FLUID_CC_ORG_ID", ORG_A["id"])
        for name in ("command-center", "fluid_cc", "fluid-command-center"):
            cfg = FluidConfig().get_catalog_config(name)
            assert cfg["endpoint"] == "https://cc.example.test", name
            assert cfg["auth"]["api_key"] == "k", name
            assert cfg["organization_id"] == ORG_A["id"], name
            assert cfg["enabled"] is True, name

    def test_env_org_id_overrides_the_config_file(self, isolated_config, monkeypatch):
        from fluid_build.config_manager import FluidConfig

        (isolated_config / ".fluidrc.yaml").write_text(
            "catalogs:\n  fluid-command-center:\n    organization_id: from-file\n",
            encoding="utf-8",
        )
        assert FluidConfig().get_catalog_config("command-center")["organization_id"] == (
            "from-file"
        )
        monkeypatch.setenv("FLUID_CC_ORG_ID", "from-env")
        assert FluidConfig().get_catalog_config("command-center")["organization_id"] == ("from-env")

    def test_fluid_publish_target_command_center_creates_the_asset(
        self, cc_stub, isolated_config, monkeypatch
    ):
        from fluid_build.cli import publish

        cc_stub.organizations = [ORG_A, ORG_B]
        monkeypatch.setenv("FLUID_CC_ENDPOINT", cc_stub.url)
        monkeypatch.setenv("FLUID_API_KEY", API_KEY)
        monkeypatch.setenv("FLUID_CC_ORG_ID", ORG_A["id"])

        contract = isolated_config / "contract.fluid.yaml"
        contract.write_text(
            "\n".join(
                [
                    'fluidVersion: "0.7.5"',
                    "kind: DataProduct",
                    "id: bronze.customer_subscriptions",
                    "name: Customer Subscriptions",
                    "description: Subscriptions from the SID source",
                    "domain: Customer",
                    "metadata:",
                    "  layer: Bronze",
                    "  owner:",
                    "    team: data-platform",
                    "    email: data-platform@example.com",
                    "exposes:",
                    "  - exposeId: subscriptions",
                    "    kind: table",
                    "    binding:",
                    "      platform: local",
                    "      format: parquet",
                    "      location:",
                    "        path: out/customer_subscriptions.parquet",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        parser = argparse.ArgumentParser()
        publish.register(parser.add_subparsers())
        args = parser.parse_args(
            ["publish", str(contract), "--target", "command-center", "--format", "json"]
        )

        code = asyncio.run(publish.run_async(args, logging.getLogger("test")))

        assert code == 0
        (post,) = cc_stub.posts()
        assert post["headers"]["x-organization-id"] == ORG_A["id"]
        assert post["body"]["metadata"]["fluid_contract_id"] == "bronze.customer_subscriptions"
