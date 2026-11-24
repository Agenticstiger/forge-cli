# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid publish`` sends nothing to the Command Center unless it was asked to.

Every case runs against a real loopback listener that records each request, so
a request that leaves the process cannot go unseen:

* ``FLUID_COMMAND_CENTER_ENABLED`` off: the Command Center target is refused.
* No ``--target`` and no ``--catalog``: an error, not a publish to the built-in
  endpoint.
* No credential: the missing-credential error comes before the health check.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

_KEY = "runner-key-not-a-secret"  # pragma: allowlist secret
_ORG = "0f5e3c1a-telco"

_CLEARED = (
    "FLUID_COMMAND_CENTER_ENABLED",
    "FLUID_CC_ENDPOINT",
    "FLUID_CATALOG_FLUID_CC_URL",
    "FLUID_API_KEY",
    "FLUID_CATALOG_FLUID_CC_TOKEN",
    "FLUID_BEARER_TOKEN",
    "FLUID_CC_ORG_ID",
    "FLUID_SECRETS_FILE",
)


@pytest.fixture
def listener() -> Iterator[Tuple[str, List[Dict[str, Any]]]]:
    """Any-method HTTP listener on loopback: ``(base_url, every request seen)``."""
    seen: List[Dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append({"method": self.command, "path": self.path})
            if self.path.startswith("/api/v1/organizations"):
                payload: Any = [{"id": _ORG, "slug": "telco", "name": "Telco"}]
            elif self.command == "GET" and self.path.startswith("/api/v1/assets"):
                payload = []
            else:
                payload = {"id": "asset-1", "ok": True}
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = _answer  # noqa: N815

        def log_message(self, *_a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    for var in _CLEARED:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def contract(checkout: Path) -> Path:
    path = checkout / "contract.fluid.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "fluidVersion": "0.7.5",
                "kind": "DataProduct",
                "id": "bronze.no_unasked_requests",
                "name": "No Unasked Requests",
                "version": "1.0.0",
                "description": "fixture",
                "domain": "Customer",
                "metadata": {
                    "layer": "Bronze",
                    "owner": {"team": "dp", "email": "dp@example.com"},
                },
                "exposes": [
                    {
                        "exposeId": "orders_raw",
                        "kind": "table",
                        "binding": {
                            "platform": "local",
                            "format": "parquet",
                            "location": {"path": "data/orders.parquet"},
                        },
                        "contract": {"schema": [{"name": "id", "type": "STRING"}]},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _publish(*argv: str) -> int:
    from fluid_build.cli import build_parser

    args = build_parser().parse_args(["publish", *argv, "--format", "json"])
    return args.func(args, logging.getLogger("test.publish"))


def _operator_endpoint(monkeypatch, url: str, *, key: bool) -> None:
    """The operator's own setup: the listener as the endpoint, and a key if asked."""
    monkeypatch.setenv("FLUID_CC_ENDPOINT", url)
    if key:
        monkeypatch.setenv("FLUID_API_KEY", _KEY)


def _reported_errors(capsys) -> str:
    out = capsys.readouterr().out
    return " ".join(str(r.get("error") or "") for r in json.loads(out))


# ── FLUID_COMMAND_CENTER_ENABLED off ─────────────────────────────────────


@pytest.mark.parametrize("value", ["false", "0", "no", "off", " False "])
@pytest.mark.parametrize(
    "argv",
    [
        ("--target", "fluid-command-center"),
        ("--target", "command-center"),
        ("--target", "fluid_cc"),
        ("--catalog", "fluid-command-center"),
    ],
)
def test_switch_off_refuses_every_command_center_name(
    checkout, contract, listener, monkeypatch, capsys, value, argv
):
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=True)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_ENABLED", value)

    rc = _publish(str(contract), *argv)

    assert rc != 0
    assert seen == []
    assert "FLUID_COMMAND_CENTER_ENABLED is off" in _reported_errors(capsys)


@pytest.mark.parametrize("mode", [(), ("--dry-run",), ("--verify-only",), ("--skip-health-check",)])
def test_switch_off_refuses_in_every_mode(checkout, contract, listener, monkeypatch, mode):
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=True)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_ENABLED", "false")

    assert _publish(str(contract), "--target", "fluid-command-center", *mode) != 0
    assert seen == []


def test_switch_off_refuses_the_command_center_provider_under_another_name(
    checkout, contract, listener, monkeypatch
):
    """A backend registered under another name that IS the Command Center provider."""
    import asyncio

    from fluid_build.cli import publish
    from fluid_build.providers.catalogs import CATALOG_PROVIDERS, FluidCommandCenterProvider

    url, seen = listener
    monkeypatch.setitem(CATALOG_PROVIDERS, "cc-alias", FluidCommandCenterProvider)
    monkeypatch.setattr(
        publish, "_publish_catalog_config", lambda *_a, **_k: ({"endpoint": url}, None)
    )
    monkeypatch.setenv("FLUID_API_KEY", _KEY)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_ENABLED", "false")

    result = asyncio.run(publish.publish_contract(contract, "cc-alias", config=None))

    assert result.success is False
    assert result.details == {"error_code": "cc_disabled"}
    assert seen == []


def test_switch_off_leaves_other_targets_alone(checkout, contract, listener, monkeypatch):
    """The switch is the Command Center's: another target is not refused by it."""
    from fluid_build.cli import publish

    monkeypatch.setenv("FLUID_COMMAND_CENTER_ENABLED", "false")
    assert publish._command_center_disabled("fluid-command-center") is True
    assert publish._command_center_disabled("datahub") is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "", "anything"])
def test_switch_not_off_publishes(checkout, contract, listener, monkeypatch, value):
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=True)
    monkeypatch.setenv("FLUID_COMMAND_CENTER_ENABLED", value)

    assert _publish(str(contract), "--target", "fluid-command-center") == 0
    assert any(r["method"] == "POST" for r in seen)


# ── no target ────────────────────────────────────────────────────────────


def test_no_target_sends_nothing_and_names_the_choices(
    checkout, contract, listener, monkeypatch, caplog
):
    url, seen = listener
    # The operator's endpoint and key are both set: the old default would have used them.
    _operator_endpoint(monkeypatch, url, key=True)

    with caplog.at_level(logging.ERROR, logger="test.publish"):
        rc = _publish(str(contract))

    assert rc == 1
    assert seen == []
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "--target" in message
    assert "fluid-command-center" in message
    assert "datahub" in message


def test_the_no_target_hints_are_commands_that_run(
    checkout, contract, listener, monkeypatch, caplog
):
    """The message names the user's own contract, and its --list-catalogs hint runs."""
    import shlex

    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=True)
    with caplog.at_level(logging.ERROR, logger="test.publish"):
        assert _publish(str(contract)) == 1
    message = " ".join(r.getMessage() for r in caplog.records)
    quoted = shlex.quote(str(contract))
    assert f"'fluid publish {quoted} --target fluid-command-center'" in message
    assert f"'fluid publish {quoted} --list-catalogs'" in message

    assert _publish(str(contract), "--list-catalogs") == 0
    assert seen == []


def test_no_target_sends_nothing_to_the_built_in_endpoint(checkout, contract, monkeypatch):
    """Nothing is opened toward ``http://localhost:8000``, the built-in endpoint."""
    import httpx

    sent: List[Any] = []

    def _record(self, request, **_kw):  # noqa: ANN001
        sent.append(request)
        raise AssertionError("a request was sent")

    monkeypatch.setattr(httpx.AsyncClient, "send", _record)
    monkeypatch.setattr(httpx.Client, "send", _record)
    monkeypatch.setenv("FLUID_API_KEY", _KEY)

    assert _publish(str(contract)) == 1
    assert sent == []


def test_project_config_does_not_name_a_target(checkout, contract, listener, monkeypatch):
    """No setting names a default target: a checkout's config cannot pick one."""
    url, seen = listener
    (checkout / "fluid.config.yaml").write_text(
        yaml.safe_dump({"catalogs": {"fluid-command-center": {"endpoint": url}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("FLUID_API_KEY", _KEY)

    assert _publish(str(contract)) == 1
    assert seen == []


def test_catalog_flag_is_an_explicit_target(checkout, contract, listener, monkeypatch):
    """Command Center's bulk publish passes ``--catalog <name>``; it keeps working."""
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=True)

    assert _publish(str(contract), "--catalog", "fluid-command-center") == 0
    assert any(r["method"] == "POST" for r in seen)


# ── no credential ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode", [(), ("--skip-health-check",), ("--verify-only",)], ids=["publish", "skip", "verify"]
)
def test_no_key_names_the_missing_credential_before_any_request(
    checkout, contract, listener, monkeypatch, capsys, mode
):
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=False)

    rc = _publish(str(contract), "--target", "fluid-command-center", *mode)

    assert rc != 0
    assert seen == []
    assert "No Command Center credential is configured" in _reported_errors(capsys)


def test_no_key_with_a_configured_organization_still_sends_nothing(
    checkout, contract, listener, monkeypatch
):
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=False)
    monkeypatch.setenv("FLUID_CC_ORG_ID", _ORG)

    assert _publish(str(contract), "--target", "fluid-command-center", "--verify-only") != 0
    assert seen == []


def test_key_and_endpoint_still_publish(checkout, contract, listener, monkeypatch):
    url, seen = listener
    _operator_endpoint(monkeypatch, url, key=True)

    assert _publish(str(contract), "--target", "fluid-command-center") == 0
    assert seen[0]["path"].startswith("/api/v1/assets")  # the health check, now credentialed
    assert any(r["method"] == "POST" for r in seen)


# ── the provider on its own ──────────────────────────────────────────────


def test_provider_health_check_and_publish_send_nothing_without_a_credential(listener):
    import asyncio

    from fluid_build.providers.catalogs import CatalogAsset, FluidCommandCenterProvider

    url, seen = listener
    provider = FluidCommandCenterProvider({"endpoint": url, "auth": {"type": "api_key"}})

    assert asyncio.run(provider.health_check()) is False
    asset = CatalogAsset(
        id="x",
        name="x",
        description="x",
        type="table",
        domain="d",
        owner="o",
        owner_email="o@example.com",
        layer="Bronze",
        tags=[],
        version="1.0.0",
        location={},
        platform="local",
    )
    result = asyncio.run(provider.publish(asset))

    assert result.success is False
    assert result.details["error_code"] == "cc_credential_missing"
    assert seen == []
