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

"""Stage-3 LIVE tests for MCP-based catalog discovery.

These connect the :class:`McpCatalogConnector` to a *real* catalog MCP server
(DataHub / OpenMetadata / Data Mesh Manager) and assert a genuine discovery
round-trip. They are **gated + self-skipping**: nothing runs unless you opt in,
so the keyless suite (``tests/cli/test_market_mcp_catalog.py``) stays the
fast, hermetic default and CI never needs Docker.

Run against a local DataHub Docker quickstart (auth disabled by default)::

    datahub docker quickstart
    datahub docker ingest-sample-data
    export FLUID_MCP_DISCOVERY_LIVE=1
    export FLUID_MCP_LIVE_CONFIG_JSON='{
      "profile": "datahub", "transport": "stdio", "command": "uvx",
      "args": ["mcp-server-datahub"],
      "env": {"DATAHUB_GMS_URL": "http://localhost:8080"}
    }'
    pytest tests/integration/test_market_mcp_discovery_live.py -v

Against a remote OpenMetadata::

    export FLUID_MCP_DISCOVERY_LIVE=1 OPENMETADATA_JWT_TOKEN=<pat>
    export FLUID_MCP_LIVE_CONFIG_JSON='{
      "profile": "openmetadata", "transport": "streamable_http",
      "url": "https://openmetadata.example.com/mcp", "token_env": "OPENMETADATA_JWT_TOKEN"
    }'
    pytest tests/integration/test_market_mcp_discovery_live.py -v
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest

_LIVE = os.environ.get("FLUID_MCP_DISCOVERY_LIVE") == "1"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _LIVE,
        reason="set FLUID_MCP_DISCOVERY_LIVE=1 + FLUID_MCP_LIVE_CONFIG_JSON to run",
    ),
]

LOG = logging.getLogger("test.market.mcp.live")


def _config() -> dict:
    raw = os.environ.get("FLUID_MCP_LIVE_CONFIG_JSON")
    if not raw:
        pytest.skip("FLUID_MCP_LIVE_CONFIG_JSON not set")
    return json.loads(raw)


def test_live_discovery_round_trip():
    """Connect to a real catalog MCP server and run one search; the round-trip
    must succeed and every mapped product must be well-formed."""
    from fluid_build.cli.market import SearchFilters
    from fluid_build.cli.market_catalogs.mcp_catalog import McpCatalogConnector

    connector = McpCatalogConnector(_config(), LOG)
    assert asyncio.run(connector.connect()) is True, "connect failed / no search tool found"
    assert connector._search_tool, "no search tool resolved"

    products = asyncio.run(connector.search_data_products(SearchFilters(limit=10)))
    # A real catalog may legitimately be empty, so we don't assert a count —
    # but anything returned must map cleanly into the marketplace model.
    for p in products:
        assert p.catalog_type == "mcp"
        assert p.id, "discovered product missing id"
        assert p.name, "discovered product missing name"
        assert p.quality_score is None or 0.0 <= p.quality_score <= 1.0
    LOG.info("live MCP discovery returned %d product(s)", len(products))


def test_live_catalog_stats():
    from fluid_build.cli.market_catalogs.mcp_catalog import McpCatalogConnector

    connector = McpCatalogConnector(_config(), LOG)
    assert asyncio.run(connector.connect()) is True
    stats = asyncio.run(connector.get_catalog_stats())
    assert "total_products" in stats
    assert stats["source"].startswith("mcp:")
