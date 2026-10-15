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

"""``fluid market`` with a ``market.yaml`` from the checkout, against a real
loopback listener.

With no ``~/.fluid/market.yaml``, ``fluid market`` reads ``./market.yaml``:
the checkout's file, not the operator's. It gets the project-config trust
rule. Its ``mcp`` block may not name an environment variable to send
(``token_env``) or copy into a server (``env_from``), may not start a stdio
server (which would inherit the whole environment), and its URL must pass the
Command Center host gate. A token the file carries itself is its own to send.
The operator's ``~/.fluid/market.yaml`` behaves as before.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

_OPERATOR_KEY = "operator-dmm-key-not-a-secret"  # pragma: allowlist secret
_CHECKOUT_TOKEN = "checkout-own-token-not-a-secret"  # pragma: allowlist secret
_PRIV = "fluid_build.observability.reporter._hostname_is_private"


@pytest.fixture
def listener() -> Iterator[Tuple[str, List[Dict[str, Any]]]]:
    """An HTTP listener on loopback that records every request and answers 500."""
    recorded: List[Dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def _answer(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            recorded.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = do_DELETE = _answer  # noqa: N815

        def log_message(self, *_a: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp", recorded
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch) -> Path:
    """A checkout as cwd, an empty HOME (no ~/.fluid/market.yaml) and the
    operator's key in the environment."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FLUID_COMMAND_CENTER_HOST_ALLOWLIST", raising=False)
    monkeypatch.setenv("DMM_API_KEY", _OPERATOR_KEY)
    project = tmp_path / "checkout"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def _mcp_block(**block: Any) -> Dict[str, Any]:
    return {
        "catalogs": ["mcp"],
        "mcp": {"profile": "datamesh_manager", "max_retries": 1, "retry_delay": 0.01, **block},
    }


def _write(path: Path, data: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _fluid_market(*argv: str) -> int:
    from fluid_build.cli import build_parser
    from fluid_build.cli._common import CLIError

    args = build_parser().parse_args(["market", *argv])
    # ``market.run`` asks for the current loop before ``asyncio.run``; give it
    # one, as a fresh process would have.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return args.func(args, logging.getLogger("test.market"))
    except CLIError as exc:
        return exc.exit_code
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _carrying(recorded: List[Dict[str, Any]], needle: str) -> List[Dict[str, Any]]:
    return [r for r in recorded if needle in str(r["headers"])]


@pytest.mark.parametrize("name", ["market.yaml", "market.yml"])
def test_a_checkout_market_file_cannot_send_the_operators_env_token(
    checkout, listener, caplog, name
):
    url, recorded = listener
    _write(
        checkout / name,
        _mcp_block(
            transport="streamable_http",
            url=url,
            token_env="DMM_API_KEY",
            auth_header="x-api-key",
            auth_scheme="",
        ),
    )

    with caplog.at_level(logging.WARNING):
        _fluid_market("--search", "orders")

    assert _carrying(recorded, _OPERATOR_KEY) == []
    assert recorded == []
    assert "token_env" in caplog.text
    assert "~/.fluid/market.yaml" in caplog.text
    assert _OPERATOR_KEY not in caplog.text


def test_a_checkout_market_file_cannot_start_a_stdio_server(checkout, tmp_path):
    marker = tmp_path / "stdio-ran"
    _write(
        checkout / "market.yaml",
        _mcp_block(
            transport="stdio",
            command=sys.executable,
            args=[
                "-c",
                f"import os, pathlib; pathlib.Path({str(marker)!r}).write_text("
                "os.environ.get('LEAK', 'none'))",
            ],
            env_from={"LEAK": "DMM_API_KEY"},
        ),
    )

    _fluid_market("--search", "orders")

    assert not marker.exists(), marker.read_text()


def test_a_checkout_market_file_sends_the_token_it_carries_itself(checkout, listener):
    url, recorded = listener
    _write(
        checkout / "market.yaml",
        _mcp_block(
            transport="streamable_http",
            url=url,
            token=_CHECKOUT_TOKEN,
            auth_header="x-api-key",
            auth_scheme="",
        ),
    )

    _fluid_market("--search", "orders")

    assert _carrying(recorded, _CHECKOUT_TOKEN), "the checkout's own token is its to send"
    assert _carrying(recorded, _OPERATOR_KEY) == []


def test_the_operators_market_file_still_reads_the_env_token(checkout, listener):
    url, recorded = listener
    _write(
        Path.home() / ".fluid" / "market.yaml",
        _mcp_block(
            transport="streamable_http",
            url=url,
            token_env="DMM_API_KEY",
            auth_header="x-api-key",
            auth_scheme="",
        ),
    )
    _write(checkout / "market.yaml", _mcp_block(url="http://127.0.0.1:9/ignored"))

    _fluid_market("--search", "orders")

    assert _carrying(recorded, _OPERATOR_KEY), "the operator's own config keeps its token"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://catalog.example.com/mcp", "https"),
        ("https://169.254.169.254/mcp", "private or metadata"),
    ],
)
def test_a_checkout_market_url_must_pass_the_host_gate(checkout, monkeypatch, url, reason):
    from fluid_build.cli.market import load_market_config
    from fluid_build.cli.market_catalogs.mcp_catalog import McpCatalogConnector

    monkeypatch.setattr(_PRIV, lambda host: host.startswith("169.254."))
    _write(
        checkout / "market.yaml",
        _mcp_block(transport="streamable_http", url=url, token=_CHECKOUT_TOKEN),
    )
    logger = logging.getLogger("test.market")
    config = load_market_config(SimpleNamespace(catalogs=None), logger)

    connector = McpCatalogConnector(config["mcp"], logger)

    with pytest.raises(ValueError, match=reason):
        connector._build_server_params()


def test_a_checkout_atlas_block_gets_no_env_credentials(checkout, monkeypatch):
    from fluid_build.cli.market import load_market_config
    from fluid_build.cli.market_catalogs.apache_atlas import ApacheAtlasConnector

    monkeypatch.setenv("ATLAS_USERNAME", "operator-user")
    monkeypatch.setenv("ATLAS_PASSWORD", _OPERATOR_KEY)
    _write(
        checkout / "market.yaml",
        {"catalogs": ["apache_atlas"], "apache_atlas": {"base_url": "https://atlas.example"}},
    )
    logger = logging.getLogger("test.market")
    config = load_market_config(SimpleNamespace(catalogs=None), logger)

    connector = ApacheAtlasConnector(config["apache_atlas"], logger)

    assert asyncio.run(connector._connect_impl()) is False


def test_an_env_configured_catalog_is_the_operators_even_with_a_checkout_file(
    checkout, monkeypatch
):
    # DATAHUB_SERVER_URL brings the endpoint and its token together.
    from fluid_build.cli.market import CheckoutMarketBlock, load_market_config

    monkeypatch.setenv("DATAHUB_SERVER_URL", "https://datahub.operator.example")
    _write(
        checkout / "market.yaml",
        {"catalogs": ["datahub", "mcp"], "datahub": {}, "mcp": {"url": "https://x.example"}},
    )

    config = load_market_config(SimpleNamespace(catalogs=None), logging.getLogger("t"))

    assert not isinstance(config["datahub"], CheckoutMarketBlock)
    assert isinstance(config["mcp"], CheckoutMarketBlock)
    assert config["mcp"].checkout_file == "market.yaml"
