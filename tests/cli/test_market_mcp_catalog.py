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

"""Keyless tests for the MCP-based catalog discovery connector.

These exercise the FULL client path — connect → list_tools → call_tool →
parse → map → filter — against an **in-memory MCP server** stood up with the
``fluid_build._mcp_compat.open_inmemory_session`` harness (version-neutral
over SDK 1.x/2.x). Real MCP protocol, zero network, zero Docker: the
connector's ``_open_session`` seam is overridden to yield the in-memory
session, so production transport code (``ClientSessionGroup``) is bypassed
here and validated separately by the Stage-3 live tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import mcp.types as mcp_types
import pytest
from mcp.server.lowlevel import Server
from mcp.types import Implementation

# In-memory client<->server harness + version-neutral lowlevel Server factory
# via the SDK version-compat seam (the v1 memory helper and the v1 decorator
# registration API are both gone in mcp 2.x).
from fluid_build._mcp_compat import build_lowlevel_server, open_inmemory_session
from fluid_build.cli.market import DataProductLayer, DataProductStatus, SearchFilters
from fluid_build.cli.market_catalogs.mcp_catalog import (
    McpCatalogConnector,
    _extract_owner,
    _get_path,
    _normalize_quality,
    _parse_layer,
    _parse_status,
)

LOG = logging.getLogger("test.market.mcp")

_ROWS = [
    {
        "urn": "urn:li:dataset:finance.revenue",
        "name": "Revenue",
        "description": "Daily revenue by region",
        "domain": "finance",
        "owners": [{"name": "fin-team"}],
        "tags": ["gold", "kpi"],
        "qualityScore": 95,  # 0..100 scale → must normalize to 0.95
        "layer": "gold",
        "status": "published",  # synonym → ACTIVE
        "version": "2.1.0",
        "createdAt": "2026-01-02T00:00:00Z",
        "updatedAt": "2026-05-01T00:00:00Z",
        "popularity": 42,
    },
    {
        "id": "marketing.campaigns",
        "displayName": "Campaigns",
        "summary": "Campaign performance",
        "domain": "marketing",
        "team": "growth",
        "labels": ["silver"],  # this is tags, NOT layer
        "trustScore": 0.8,
    },
]


@contextlib.asynccontextmanager
async def _noop_lifespan(_server):
    """No-op server lifespan (compat seam passes ``lifespan`` through verbatim)."""
    yield {}


def _make_catalog_server(rows, *, tool_name: str = "search") -> Server:
    """A minimal in-memory MCP server exposing one search tool returning rows
    as a JSON text block (the universal CallToolResult shape)."""

    async def _list_tools():  # noqa: D401
        return [
            mcp_types.Tool(
                name=tool_name,
                description="Search data products",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                },
            )
        ]

    async def _call_tool(ctx, name, arguments):  # noqa: D401
        if name != tool_name:
            raise ValueError(f"unknown tool: {name}")
        return [mcp_types.TextContent(type="text", text=json.dumps(rows))]

    return build_lowlevel_server(
        "fake-catalog",
        version="0.0.0",
        lifespan=_noop_lifespan,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def _wire(connector: McpCatalogConnector, server: Server) -> None:
    """Point the connector's session seam at an in-memory server session."""

    @contextlib.asynccontextmanager
    async def _session():
        async with open_inmemory_session(
            server, client_info=Implementation(name="fluid-mcp-test", version="0.0.0")
        ) as session:
            yield session

    connector._open_session = _session


def _connector(profile: str = "auto", **cfg) -> McpCatalogConnector:
    return McpCatalogConnector({"profile": profile, **cfg}, LOG)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# connect() — search-tool resolution                                          #
# --------------------------------------------------------------------------- #
def test_connect_resolves_search_tool():
    c = _connector(profile="datahub")
    _wire(c, _make_catalog_server(_ROWS, tool_name="search"))
    assert _run(c.connect()) is True
    assert c._search_tool == "search"


def test_connect_auto_profile_resolves_by_name_heuristic():
    c = _connector(profile="auto")
    _wire(c, _make_catalog_server(_ROWS, tool_name="find_assets"))
    assert _run(c.connect()) is True
    assert c._search_tool == "find_assets"


def test_connect_returns_false_when_no_search_tool():
    c = _connector(profile="auto")
    # 'ping' carries none of the search-name hints.
    _wire(c, _make_catalog_server(_ROWS, tool_name="ping"))
    assert _run(c.connect()) is False
    assert c.is_connected is False


# --------------------------------------------------------------------------- #
# search() — real protocol round-trip + mapping                               #
# --------------------------------------------------------------------------- #
def test_search_maps_rows_to_metadata():
    c = _connector(profile="datahub")
    _wire(c, _make_catalog_server(_ROWS))
    assert _run(c.connect()) is True
    products = _run(c.search_data_products(SearchFilters()))
    assert len(products) == 2

    by_id = {p.id: p for p in products}
    rev = by_id["urn:li:dataset:finance.revenue"]
    assert rev.name == "Revenue"
    assert rev.domain == "finance"
    assert rev.owner == "fin-team"  # list[dict] → name
    assert rev.tags == ["gold", "kpi"]
    assert rev.quality_score == pytest.approx(0.95)  # 95 → 0.95
    assert rev.layer is DataProductLayer.GOLD
    assert rev.status is DataProductStatus.ACTIVE  # 'published' synonym
    assert rev.version == "2.1.0"
    assert rev.catalog_type == "mcp"
    assert rev.catalog_source == "MCP (datahub)"
    assert rev.usage_stats.get("popularity") == 42

    camp = by_id["marketing.campaigns"]
    assert camp.name == "Campaigns"
    assert camp.owner == "growth"
    assert camp.quality_score == pytest.approx(0.8)  # trustScore
    assert camp.layer is DataProductLayer.SILVER  # no layer field → neutral default
    assert camp.version == "1.0.0"  # absent → default


def test_search_applies_filters_client_side():
    c = _connector(profile="datahub")
    _wire(c, _make_catalog_server(_ROWS))
    assert _run(c.connect()) is True
    products = _run(c.search_data_products(SearchFilters(domain="finance")))
    assert [p.id for p in products] == ["urn:li:dataset:finance.revenue"]


def test_search_returns_empty_on_no_rows():
    c = _connector(profile="auto")
    _wire(c, _make_catalog_server([]))
    assert _run(c.connect()) is True
    assert _run(c.search_data_products(SearchFilters())) == []


def test_get_catalog_stats_derives_from_search():
    c = _connector(profile="datahub")
    _wire(c, _make_catalog_server(_ROWS))
    assert _run(c.connect()) is True
    stats = _run(c.get_catalog_stats())
    assert stats["total_products"] == 2
    assert stats["source"] == "mcp:datahub"
    # avg of 0.95 and 0.8
    assert stats["avg_quality"] == pytest.approx(0.875)


# --------------------------------------------------------------------------- #
# Real-world result shapes (pinned from live DataHub verification)             #
# --------------------------------------------------------------------------- #
# DataHub's `search` tool returns rows under "searchResults", each wrapped in
# an {"entity": {...}} envelope with the name nested at properties.name. This
# exact shape was captured against a live DataHub MCP server.
_DATAHUB_SHAPE = {
    "start": 0,
    "count": 2,
    "total": 2,
    "searchResults": [
        {
            "entity": {
                "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,commerce.sdp.orders,PROD)",
                "properties": {"name": "orders"},
            }
        },
        {
            "entity": {
                "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,finance.adp.revenue,PROD)",
                "properties": {"name": "revenue_daily"},
            }
        },
    ],
    "facets": [],
}


def _make_wrapping_server(payload, *, tool_name: str = "search") -> Server:
    """In-memory server returning a single JSON object payload (vs a bare list)."""

    async def _list_tools():
        return [
            mcp_types.Tool(
                name=tool_name,
                description="search",
                inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
            )
        ]

    async def _call_tool(ctx, name, arguments):
        return [mcp_types.TextContent(type="text", text=json.dumps(payload))]

    return build_lowlevel_server(
        "fake-catalog-wrap",
        version="0.0.0",
        lifespan=_noop_lifespan,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def test_search_parses_datahub_searchresults_envelope():
    c = _connector(profile="datahub")
    _wire(c, _make_wrapping_server(_DATAHUB_SHAPE))
    assert _run(c.connect()) is True
    products = _run(c.search_data_products(SearchFilters()))
    assert {p.name for p in products} == {"orders", "revenue_daily"}
    assert all(p.catalog_type == "mcp" for p in products)
    assert any("commerce.sdp.orders" in p.id for p in products)


# OpenMetadata's `search_metadata` shape, captured live from OM 1.12's built-in
# MCP server: rows under "results", each with fullyQualifiedName / name /
# description / owners (a list of {name, displayName}).
_OM_SHAPE = {
    "totalFound": 1,
    "results": [
        {
            "entityType": "table",
            "fullyQualifiedName": "svc.db.schema.dim_address",
            "name": "dim_address",
            "displayName": "Address Dimension",
            "description": "address dimension table",
            "owners": [{"name": "data-team", "displayName": "Data Team"}],
        }
    ],
}


def test_openmetadata_search_metadata_shape_maps():
    c = _connector(profile="openmetadata")
    _wire(c, _make_wrapping_server(_OM_SHAPE, tool_name="search_metadata"))
    assert _run(c.connect()) is True
    assert c._search_tool == "search_metadata"
    products = _run(c.search_data_products(SearchFilters()))
    assert len(products) == 1
    p = products[0]
    assert p.id == "svc.db.schema.dim_address"  # fullyQualifiedName
    assert p.name == "dim_address"
    assert p.description == "address dimension table"
    assert p.owner == "data-team"  # owners[].name
    assert p.catalog_source == "MCP (openmetadata)"


def test_limit_arg_is_per_catalog():
    # Verified live: OpenMetadata's search-limit param is `size`, DataHub's is
    # `num_results` — NOT `limit`. _build_search_args keeps only schema-declared
    # args, so the per-profile limit_arg must match the server's real param.
    om = _connector(profile="openmetadata")
    om._search_tool_schema = {"properties": {"query": {}, "size": {}}}
    om_args = om._build_search_args(SearchFilters(limit=7))
    assert om_args.get("size") == 7 and "limit" not in om_args

    dh = _connector(profile="datahub")
    dh._search_tool_schema = {"properties": {"query": {}, "num_results": {}}}
    dh_args = dh._build_search_args(SearchFilters(limit=7))
    assert dh_args.get("num_results") == 7 and "limit" not in dh_args


# --------------------------------------------------------------------------- #
# Pure mapping helpers                                                         #
# --------------------------------------------------------------------------- #
def test_get_path_resolves_flat_and_dotted():
    assert _get_path({"a": 1}, "a") == 1
    assert _get_path({"properties": {"name": "x"}}, "properties.name") == "x"
    assert _get_path({}, "a.b") is None
    assert _get_path({"a": {"b": None}}, "a.b") is None


def test_unwrap_envelope():
    assert McpCatalogConnector._unwrap_envelope({"entity": {"urn": "x"}}) == {"urn": "x"}
    assert McpCatalogConnector._unwrap_envelope({"node": {"id": "y"}}) == {"id": "y"}
    assert McpCatalogConnector._unwrap_envelope({"id": "z"}) == {"id": "z"}


@pytest.mark.parametrize(
    "raw,expected",
    [(95, 0.95), (0.93, 0.93), (100, 1.0), ("0.5", 0.5), (None, None), ("x", None)],
)
def test_normalize_quality(raw, expected):
    got = _normalize_quality(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("gold", DataProductLayer.GOLD),
        ("certified", DataProductLayer.GOLD),
        ("streaming", DataProductLayer.REAL_TIME),
        ("landing", DataProductLayer.RAW),
        (None, DataProductLayer.SILVER),
        ("nonsense", DataProductLayer.SILVER),
    ],
)
def test_parse_layer(raw, expected):
    assert _parse_layer(raw) is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("active", DataProductStatus.ACTIVE),
        ("published", DataProductStatus.ACTIVE),
        ("draft", DataProductStatus.DEVELOPMENT),
        ("deprecated", DataProductStatus.DEPRECATED),
        (None, DataProductStatus.ACTIVE),
    ],
)
def test_parse_status(raw, expected):
    assert _parse_status(raw) is expected


# --------------------------------------------------------------------------- #
# Authentication — secrets sourced from env, never the config file            #
# --------------------------------------------------------------------------- #
def test_headers_bearer_from_token_env(monkeypatch):
    monkeypatch.setenv("OM_JWT", "tok-123")
    c = _connector(
        profile="openmetadata",
        transport="streamable_http",
        url="https://om.example.com/mcp",
        token_env="OM_JWT",
    )
    assert c._headers()["Authorization"] == "Bearer tok-123"


def test_headers_literal_token():
    c = _connector(url="https://h/mcp", token="lit")
    assert c._headers()["Authorization"] == "Bearer lit"


def test_headers_custom_scheme_for_x_api_key():
    c = _connector(url="https://h/mcp", token="abc", auth_header="x-api-key", auth_scheme="")
    h = c._headers()
    assert h["x-api-key"] == "abc"
    assert "Authorization" not in h


def test_headers_no_token_means_no_auth_header():
    assert "Authorization" not in _connector(url="https://h/mcp")._headers()


def test_headers_missing_token_env_yields_no_auth(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert "Authorization" not in _connector(url="https://h/mcp", token_env="NOPE")._headers()


def test_insecure_http_token_warns(caplog):
    c = _connector(url="http://remote.example.com/mcp", token="t")
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        c._headers()
    assert any("plaintext HTTP" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("url", ["http://localhost:8080/mcp", "https://remote.example.com/mcp"])
def test_secure_or_local_transport_does_not_warn(url, caplog):
    c = _connector(url=url, token="t")
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        c._headers()
    assert not any("plaintext HTTP" in r.getMessage() for r in caplog.records)


def test_subprocess_env_inherits_and_layers(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_DMM_KEY", "dmm_live_secret")
    c = _connector(
        profile="datamesh_manager",
        command="uvx",
        args=["dataproduct-mcp"],
        env={"DATAMESH_MANAGER_HOST": "https://app.datamesh-manager.com"},
        env_from={"DATAMESH_MANAGER_API_KEY": "MY_DMM_KEY"},
    )
    env = c._subprocess_env()
    assert env["PATH"] == "/usr/bin"  # inherited from os.environ
    # Full-equality (not endswith) so this stays a clean assertion and doesn't
    # trip CodeQL's url-substring-sanitization pattern.
    assert env["DATAMESH_MANAGER_HOST"] == "https://app.datamesh-manager.com"  # literal config
    assert env["DATAMESH_MANAGER_API_KEY"] == "dmm_live_secret"  # env_from rename


def test_secret_never_appears_in_logs(caplog):
    c = _connector(profile="datahub", url="https://h/mcp", token="super-secret-token")
    _wire(c, _make_catalog_server(_ROWS))
    with caplog.at_level(logging.DEBUG):
        assert _run(c.connect()) is True
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "super-secret-token" not in blob


# --------------------------------------------------------------------------- #
# Two-phase enrichment (detail retrieval — "all metadata")                    #
# --------------------------------------------------------------------------- #
# Shapes mirror what was captured live from mcp-server-datahub:
#   get_entities(urns=…)     -> {"result": {entity}}
#   list_schema_fields(urn=…) -> {"fields": [{fieldPath, nativeDataType, ...}]}
_ENTITY = {
    "urn": "urn:li:dataset:x",
    "name": "orders",
    "properties": {"description": "daily orders by region"},
    "ownership": {"owners": [{"owner": {"properties": {"displayName": "Jane Doe"}}}]},
}
_FIELDS = [
    {
        "fieldPath": "id",
        "nativeDataType": "bigint",
        "description": "primary key",
        "nullable": False,
    },
    {"fieldPath": "amount", "nativeDataType": "double", "description": "order total"},
]
_SEARCH_HIT = {
    "searchResults": [{"entity": {"urn": "urn:li:dataset:x", "properties": {"name": "orders"}}}]
}


def _make_enriching_server() -> Server:
    """In-memory server exposing search + get_entities + list_schema_fields."""

    async def _list_tools():
        def tool(name):
            return mcp_types.Tool(
                name=name,
                description=name,
                inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
            )

        return [tool("search"), tool("get_entities"), tool("list_schema_fields")]

    async def _call_tool(ctx, name, arguments):
        if name == "search":
            payload = _SEARCH_HIT
        elif name == "get_entities":
            payload = {"result": _ENTITY}
        elif name == "list_schema_fields":
            payload = {"fields": _FIELDS, "totalFields": len(_FIELDS)}
        else:
            raise ValueError(f"unknown tool: {name}")
        return [mcp_types.TextContent(type="text", text=json.dumps(payload))]

    return build_lowlevel_server(
        "fake-catalog-enrich",
        version="0.0.0",
        lifespan=_noop_lifespan,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def test_get_data_product_enriches_with_detail_and_schema():
    c = _connector(profile="datahub")
    _wire(c, _make_enriching_server())
    assert _run(c.connect()) is True
    d = _run(c.get_data_product("urn:li:dataset:x"))
    assert d is not None
    # Phase 1: rich entity detail.
    assert d.description == "daily orders by region"
    assert d.owner == "Jane Doe"  # deep ownership.owners[].owner.properties.displayName
    # Phase 2: the data asset's column schema.
    assert [f["name"] for f in d.schema_fields] == ["id", "amount"]
    assert d.schema_fields[0]["type"] == "bigint"
    assert d.schema_fields[0]["description"] == "primary key"
    assert d.schema_fields[0]["nullable"] is False


def test_get_data_product_falls_back_to_shallow_without_detail_tool():
    # auto profile has no detail_tool → falls back to shallow search match.
    c = _connector(profile="auto")
    _wire(c, _make_catalog_server(_ROWS))
    assert _run(c.connect()) is True
    d = _run(c.get_data_product("urn:li:dataset:finance.revenue"))
    assert d is not None
    assert d.name == "Revenue"
    assert d.schema_fields == []  # no schema tool → no enrichment


def _make_superset_server() -> Server:
    """search returns a RICH row (trust fields); get_entities returns a SPARSE
    entity (description/owner only). The merged detail must keep the rich trust
    fields AND gain the enriched description/owner — i.e. `--detailed` ⊇ listing.
    """
    rich_row = {
        "urn": "urn:li:dataset:finance.revenue",
        "name": "Revenue",
        "description": "short summary",
        "domain": "finance",
        "owners": [{"name": "fin-team"}],
        "tags": ["gold", "kpi"],
        "qualityScore": 95,
        "layer": "gold",
        "version": "2.1.0",
        "popularity": 42,
    }
    sparse_entity = {
        "urn": "urn:li:dataset:finance.revenue",
        "name": "Revenue",
        "properties": {"description": "Daily revenue by region (enriched)"},
        "ownership": {"owners": [{"owner": {"properties": {"displayName": "Finance Team"}}}]},
    }

    async def _list_tools():
        def tool(name):
            return mcp_types.Tool(
                name=name,
                description=name,
                inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
            )

        return [tool("search"), tool("get_entities"), tool("list_schema_fields")]

    async def _call_tool(ctx, name, arguments):
        if name == "search":
            payload = [rich_row]
        elif name == "get_entities":
            payload = {"result": sparse_entity}
        elif name == "list_schema_fields":
            payload = {"fields": _FIELDS}
        else:
            raise ValueError(f"unknown tool: {name}")
        return [mcp_types.TextContent(type="text", text=json.dumps(payload))]

    return build_lowlevel_server(
        "fake-catalog-superset",
        version="0.0.0",
        lifespan=_noop_lifespan,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def test_get_data_product_detail_is_superset_of_listing():
    # Regression: enrichment must MERGE (not REPLACE) — the detail entity is
    # sparser than the search row on trust, so a naive replace would drop
    # quality/layer/tags/version/usage that the listing showed.
    c = _connector(profile="datahub")
    _wire(c, _make_superset_server())
    assert _run(c.connect()) is True
    d = _run(c.get_data_product("urn:li:dataset:finance.revenue"))
    assert d is not None
    # Detail's reason to exist — enriched description + resolved owner — wins.
    assert d.description == "Daily revenue by region (enriched)"
    assert d.owner == "Finance Team"
    # Trust fields from the shallow listing are KEPT, not dropped.
    assert d.quality_score == pytest.approx(0.95)
    assert d.layer is DataProductLayer.GOLD
    assert d.version == "2.1.0"
    assert d.tags == ["gold", "kpi"]
    assert d.usage_stats.get("popularity") == 42
    # And the column schema is attached (from the shared _FIELDS fixture).
    assert [f["name"] for f in d.schema_fields] == ["id", "amount"]


def test_extract_owner_handles_nested_ownership():
    assert _extract_owner("team-a") == "team-a"
    assert _extract_owner({"owners": [{"owner": {"properties": {"displayName": "A"}}}]}) == "A"
    assert _extract_owner({"owners": [{"owner": {"name": "B"}}, {"owner": {"urn": "C"}}]}) == "B, C"
    assert _extract_owner(["x", "y"]) == "x, y"


# --------------------------------------------------------------------------- #
# Engine registration + graceful degradation                                  #
# --------------------------------------------------------------------------- #
def test_engine_registers_mcp_and_degrades_without_config():
    """`mcp` is a known catalog_type, and with no server configured it fails to
    connect rather than crashing — and serves nothing (no fabricated data)."""
    from fluid_build.cli.market import MarketDiscoveryEngine

    engine = MarketDiscoveryEngine(
        {"catalogs": ["mcp"], "cache": {"enabled": False}, "defaults": {"timeout_seconds": 5}},
        LOG,
    )
    # No 'mcp' config block → no command/url → connect fails gracefully.
    _run(engine.initialize_connectors(["mcp"]))
    assert "mcp" not in engine.connectors
    assert engine.connectors == {}
