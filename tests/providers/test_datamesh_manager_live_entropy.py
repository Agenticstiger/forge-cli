# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Live: the lineage Entropy Data draws, edge for edge, after a forge publish.

Self-skipping. Runs only against a disposable Entropy Data instance (Community
Edition in Docker is enough) with::

    FLUID_DMM_LIVE=1
    DMM_API_URL=http://localhost:8081      # the instance
    DMM_API_KEY=...                        # an organization-scoped API key
    FLUID_DMM_LIVE_ORG=<vanity url>        # the organization's URL segment
    FLUID_DMM_LIVE_USER=<email>            # a user of that organization, to
    FLUID_DMM_LIVE_PASSWORD=<password>     # read the graph (no API route has it)

Entropy has no lineage read API. The UI's ``/{org}/graph`` page embeds the whole
graph as JSON (``{"edges": [...], "nodes": [...]}``) and that is the ground
truth read here. Every product id carries a per-run prefix, so runs never meet;
the products and contracts a run creates are deleted afterwards (best effort).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Dict, Iterator, Set, Tuple

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.datamesh]

_ENV = (
    "DMM_API_URL",
    "DMM_API_KEY",
    "FLUID_DMM_LIVE_ORG",
    "FLUID_DMM_LIVE_USER",
    "FLUID_DMM_LIVE_PASSWORD",
)

if os.environ.get("FLUID_DMM_LIVE") != "1" or not all(os.environ.get(k) for k in _ENV):
    pytest.skip(
        "live Entropy Data test: set FLUID_DMM_LIVE=1 and " + ", ".join(_ENV),
        allow_module_level=True,
    )

import httpx  # noqa: E402

from fluid_build.api.catalog_publication import CatalogPublicationPayload  # noqa: E402
from fluid_build.build_runners.catalog_registrars.datamesh_manager import (  # noqa: E402
    DataMeshManagerRegistrar,
)
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider  # noqa: E402

API = os.environ["DMM_API_URL"].rstrip("/")
KEY = os.environ["DMM_API_KEY"]
ORG = os.environ["FLUID_DMM_LIVE_ORG"]

Edge = Tuple[str, str]


def _product(pid: str, team: str, *, consumes=(), source: bool = False) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": pid,
        "name": pid,
        "metadata": {"owner": {"team": team}},
        "exposes": [
            {
                "exposeId": "out",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": f"out/{pid}.parquet"},
                },
                "contract": {"schema": [{"name": "id", "type": "VARCHAR"}]},
            }
        ],
    }
    if consumes:
        contract["consumes"] = [{"productId": up, "exposeId": "out"} for up in consumes]
    if source:
        contract["builds"] = [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "kafka",
                        "connection": {"database": f"{pid.split('.')[0]}stream"},
                        "mode": "incremental",
                        "streams": ["events"],
                    }
                },
                "outputs": ["out"],
            }
        ]
    return contract


def _graph_edges(node_ids: Set[str]) -> Set[Edge]:
    """Edges of ``/{org}/graph`` between the nodes named in ``node_ids``."""
    with httpx.Client(base_url=API, follow_redirects=True, timeout=30) as c:
        login = c.get("/login").text
        csrf = re.search(r'name="_csrf" value="([^"]+)"', login).group(1)
        c.post(
            "/login",
            data={
                "_csrf": csrf,
                "username": os.environ["FLUID_DMM_LIVE_USER"],
                "password": os.environ["FLUID_DMM_LIVE_PASSWORD"],
            },
        )
        page = c.get(f"/{ORG}/graph").text
    start = page.find('{"edges"')
    assert start >= 0, "the graph page carried no graph JSON; is the login right?"
    graph, _ = json.JSONDecoder().raw_decode(page[start:])
    name = {
        n["id"]: str((n.get("data") or {}).get("link", "")).rstrip("/").rsplit("/", 1)[-1]
        for n in graph["nodes"]
    }
    edges = {(name.get(e["source"], ""), name.get(e["target"], "")) for e in graph["edges"]}
    return {e for e in edges if e[0] in node_ids and e[1] in node_ids}


def _cleanup(prefix: str) -> None:
    """Delete what a run made: its agreements first (Entropy refuses to delete a
    product in use), then products consumer first, contracts, source system."""
    headers = {"x-api-key": KEY}
    now = {"endTimestamp": "2000-01-01T00:00:00Z", "reason": "live test cleanup"}
    with httpx.Client(base_url=API, timeout=30) as c:
        for a in c.get("/api/access", params={"pageSize": 10000}, headers=headers).json():
            ends = (a.get("provider") or {}, a.get("consumer") or {})
            if any(str(e.get("dataProductId", "")).startswith(prefix) for e in ends):
                c.post(f"/api/access/{a['id']}/cancel", json=now, headers=headers)
                c.delete(f"/api/access/{a['id']}", headers=headers)
        mine = [
            p["id"]
            for p in c.get("/api/dataproducts", headers=headers).json()
            if str(p.get("id", "")).startswith(prefix)
        ]
        for pid in sorted(
            mine,
            key=lambda i: ("sdp", "adp", "cdp", "xteam").index(i.rsplit(".", 1)[-1]),
            reverse=True,
        ):
            c.delete(f"/api/dataproducts/{pid}", headers=headers)
            c.delete(f"/api/datacontracts/{pid}.out", headers=headers)
        c.delete(f"/api/sourcesystems/kafka-{prefix}stream", headers=headers)


@pytest.fixture
def run() -> Iterator[str]:
    prefix = f"live{uuid.uuid4().hex[:8]}"
    yield prefix
    _cleanup(prefix)


def _publish_registrar(contract: Dict[str, Any]) -> None:
    payload = CatalogPublicationPayload.from_contract(contract, {})
    result = DataMeshManagerRegistrar(api_url=API, api_token=KEY).register_payload(payload)
    assert result.succeeded, result.error


def _publish_native(contract: Dict[str, Any]) -> None:
    provider = DataMeshManagerProvider(api_key=KEY, api_url=API)
    provider._publish_one(contract, publish_contract=True, data_product_specification="odps")


@pytest.mark.parametrize(
    "publish", [_publish_registrar, _publish_native], ids=["registrar", "native"]
)
def test_the_graph_holds_exactly_the_contracts_lineage(run, publish):
    sdp, adp, cdp, xteam = (f"{run}.sdp", f"{run}.adp", f"{run}.cdp", f"{run}.xteam")
    source = f"kafka-{run}stream"
    for contract in (
        _product(sdp, "audit-team", source=True),
        _product(adp, "audit-team", consumes=[sdp]),
        _product(cdp, "audit-team", consumes=[adp, sdp]),
        # Another team: its Access agreement stays ``requested``; the input
        # port alone has to draw the edge.
        _product(xteam, "analytics-team", consumes=[sdp]),
    ):
        publish(contract)
    nodes = {sdp, adp, cdp, xteam, source}
    assert _graph_edges(nodes) == {
        (source, sdp),
        (sdp, adp),
        (adp, cdp),
        (sdp, cdp),
        (sdp, xteam),
    }

    # Drop a consume: its edge goes, nothing else moves.
    publish(_product(cdp, "audit-team", consumes=[adp]))
    assert _graph_edges(nodes) == {(source, sdp), (sdp, adp), (adp, cdp), (sdp, xteam)}
