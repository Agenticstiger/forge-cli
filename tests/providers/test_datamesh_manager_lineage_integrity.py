# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Lineage ids a Data Mesh Manager / Entropy Data publish must get exactly right.

* A consume's ``sourceSystem`` lands on the input port the ODPS export built for that
  consume, matched by the port's ``contractId`` (``{productId}.{exposeId}``), not by
  the bare exposeId: two upstreams exposing ``accounts`` share it, and the export
  renames the second port (``erp__accounts``), so one port got the other's source
  system and the renamed one none.
* One product id spelling on both ends of an edge. Entropy ids are case-sensitive
  (``CaseProbe.Mixed`` is not ``caseprobe.mixed``). The native provider published a
  product under a lower-cased, slugged id, while every consumer named it as written,
  so ``Silver.Sales.Orders`` was published as ``silver.sales.orders`` and its
  consumers' input ports and Access agreements pointed at a product that does not
  exist. Both paths now slug the product's own id and every ``consumes[].productId``
  the same way.
* With ``--with-contract`` an output port names only a contract that landed: the
  native path PUT the product before its contracts and never checked, so a contract
  that failed to render or PUT left the port naming nothing.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import respx

from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners.catalog_registrars.datamesh_manager import (
    DataMeshManagerRegistrar,
)
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.datamesh_manager._ids import dmm_product_id

API = "https://api.datamesh-manager.com"


def _expose(expose_id: str) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "parquet",
            "location": {"path": f"out/{expose_id}.parquet"},
        },
        "contract": {"schema": [{"name": "id", "type": "VARCHAR"}]},
    }


def _product(pid: str, *exposes: str, consumes: Optional[List[Dict]] = None) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": pid,
        "name": pid,
        "metadata": {"owner": {"team": "analytics"}},
        "exposes": [_expose(e) for e in exposes or ("out",)],
    }
    if consumes:
        contract["consumes"] = consumes
    return contract


def _custom(port: Dict[str, Any]) -> Dict[str, Any]:
    return {p.get("property"): p.get("value") for p in port.get("customProperties") or []}


_TWO_ACCOUNTS = [
    {"productId": "crm", "exposeId": "accounts", "sourceSystem": "crm"},
    {"productId": "erp", "exposeId": "accounts", "sourceSystem": "erp"},
]


def _native(contract: Dict[str, Any], *, publish_contract: bool = False, existing=("crm", "erp")):
    calls: List[tuple] = []

    def request(method: str, path: str, *, json_body: Any = None):
        calls.append((method, path, json_body))
        resp = MagicMock(status_code=200)
        resp.json.return_value = {}
        if method == "GET" and path.startswith("/api/dataproducts?"):
            resp.json.return_value = [{"id": pid} for pid in existing]
        elif method == "GET" and path.startswith(("/api/dataproducts/", "/api/datacontracts/")):
            raise ProviderError(f"Entropy Data API error 404 on GET {path}: none")
        elif method == "GET" and path.startswith("/api/access"):
            resp.json.return_value = []
        return resp

    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    provider._request = request  # type: ignore[method-assign]
    provider._ensure_team = lambda *a, **k: None  # type: ignore[method-assign]
    result = provider._publish_one(
        contract, publish_contract=publish_contract, data_product_specification="odps"
    )
    product = next(b for m, p, b in calls if m == "PUT" and p.startswith("/api/dataproducts/"))
    return result, product, calls


def _registrar(contract: Dict[str, Any], existing=("crm", "erp")):
    respx.put(url__regex=rf"{API}/api/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=rf"{API}/api/dataproducts(\?.*)?$").mock(
        return_value=httpx.Response(200, json=[{"id": pid} for pid in existing])
    )
    respx.get(url__regex=rf"{API}/api/(dataproducts|datacontracts)/.*").mock(
        return_value=httpx.Response(404)
    )
    respx.get(url__regex=rf"{API}/api/teams/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=rf"{API}/api/access\?.*").mock(return_value=httpx.Response(200, json=[]))
    payload = CatalogPublicationPayload.from_contract(contract, {})
    result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
    assert result.succeeded, result.error
    puts = [
        (c.request.url.path, json.loads(c.request.content))
        for c in respx.calls
        if c.request.method == "PUT"
    ]
    product = next(b for p, b in puts if p.startswith("/api/dataproducts/"))
    return result, product, puts


def _source_systems(product: Dict[str, Any]) -> Dict[str, Any]:
    return {p["contractId"]: _custom(p).get("sourceSystemId") for p in product["inputPorts"]}


# ── B: a consume's source system lands on its own port ───────────────────


def test_two_upstreams_sharing_an_expose_id_keep_their_own_source_system_native():
    _, product, _ = _native(_product("hub", consumes=_TWO_ACCOUNTS))
    assert _source_systems(product) == {"crm.accounts": "crm", "erp.accounts": "erp"}


@respx.mock
def test_two_upstreams_sharing_an_expose_id_keep_their_own_source_system_registrar():
    _, product, _ = _registrar(_product("hub", consumes=_TWO_ACCOUNTS))
    assert _source_systems(product) == {"crm.accounts": "crm", "erp.accounts": "erp"}


def test_a_consume_named_apart_from_its_expose_keeps_its_source_system():
    consumes = [
        {"productId": "crm", "exposeId": "accounts", "name": "crm_accounts", "sourceSystem": "crm"}
    ]
    _, product, _ = _native(_product("hub", consumes=consumes))
    assert _source_systems(product) == {"crm.accounts": "crm"}


# ── C: one product id spelling on both ends ──────────────────────────────


def test_the_shared_id_slug():
    assert dmm_product_id("Silver.Sales.Orders") == "silver.sales.orders"
    assert dmm_product_id(" a b/c ") == "a-b-c"
    assert dmm_product_id("already.fine") == "already.fine"


def test_a_mixed_case_upstream_is_consumed_under_the_id_it_is_published_as_native():
    consumer = _product(
        "Gold.KPIs", consumes=[{"productId": "Silver.Sales.Orders", "exposeId": "orders"}]
    )
    result, product, calls = _native(consumer, existing=("silver.sales.orders",))
    assert product["id"] == "gold.kpis"
    assert [p["contractId"] for p in product["inputPorts"]] == ["silver.sales.orders.orders"]
    access = [b for m, p, b in calls if m == "PUT" and p.startswith("/api/access/")]
    assert [a["provider"]["dataProductId"] for a in access] == ["silver.sales.orders"]
    assert access[0]["consumer"]["dataProductId"] == "gold.kpis"


def test_a_mixed_case_producer_publishes_its_contracts_under_the_slugged_id_native():
    _, product, calls = _native(_product("Silver.Sales.Orders", "orders"), publish_contract=True)
    contracts = [p for m, p, _ in calls if m == "PUT" and p.startswith("/api/datacontracts/")]
    assert contracts == ["/api/datacontracts/silver.sales.orders.orders"]
    assert product["outputPorts"][0]["contractId"] == "silver.sales.orders.orders"


@respx.mock
def test_the_registrar_uses_the_same_ids_as_the_native_path():
    consumer = _product(
        "Gold.KPIs", consumes=[{"productId": "Silver.Sales.Orders", "exposeId": "orders"}]
    )
    _, product, puts = _registrar(consumer, existing=("silver.sales.orders",))
    paths = [p for p, _ in puts]
    assert "/api/dataproducts/gold.kpis" in paths
    assert "/api/datacontracts/gold.kpis.out" in paths
    assert product["id"] == "gold.kpis"
    assert [p["contractId"] for p in product["inputPorts"]] == ["silver.sales.orders.orders"]
    access = [b for p, b in puts if p.startswith("/api/access/")]
    assert [a["provider"]["dataProductId"] for a in access] == ["silver.sales.orders"]


# ── A: with --with-contract a port names only a contract that landed ─────


def test_a_contract_that_failed_to_render_leaves_its_port_without_a_contract_id():
    from fluid_build.providers.odcs import OdcsProvider

    real = OdcsProvider.render

    def render(self, fluid, *args, expose_id=None, **kw):
        if expose_id == "broken":
            raise RuntimeError("cannot render")
        return real(self, fluid, *args, expose_id=expose_id, **kw)

    with patch.object(OdcsProvider, "render", render):
        _, product, calls = _native(_product("p", "ok", "broken"), publish_contract=True)
    ports = {p["name"]: p.get("contractId") for p in product["outputPorts"]}
    assert ports == {"ok": "p.ok", "broken": None}
    order = [p for m, p, _ in calls if m == "PUT"]
    assert order.index("/api/datacontracts/p.ok") < order.index("/api/dataproducts/p")


# ── C, the other side: an upstream Entropy holds under a mixed-case id ───


def test_an_upstream_entropy_holds_as_written_is_consumed_as_written_native():
    # Not published by forge (forge would have slugged it): Entropy has "Acme.Orders".
    consumer = _product("hub", consumes=[{"productId": "Acme.Orders", "exposeId": "orders"}])
    result, product, calls = _native(consumer, existing=("Acme.Orders",))
    assert [p["contractId"] for p in product["inputPorts"]] == ["Acme.Orders.orders"]
    access = [b for m, p, b in calls if m == "PUT" and p.startswith("/api/access/")]
    assert [a["provider"]["dataProductId"] for a in access] == ["Acme.Orders"]


@respx.mock
def test_an_upstream_entropy_holds_as_written_is_consumed_as_written_registrar():
    consumer = _product("hub", consumes=[{"productId": "Acme.Orders", "exposeId": "orders"}])
    _, product, puts = _registrar(consumer, existing=("Acme.Orders",))
    assert [p["contractId"] for p in product["inputPorts"]] == ["Acme.Orders.orders"]
    access = [b for p, b in puts if p.startswith("/api/access/")]
    assert [a["provider"]["dataProductId"] for a in access] == ["Acme.Orders"]


def test_a_dry_run_slugs_an_upstream_it_cannot_look_up():
    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    provider._request = MagicMock(side_effect=AssertionError("dry run sent a request"))
    consumer = _product(
        "hub", consumes=[{"productId": "Silver.Sales.Orders", "exposeId": "orders"}]
    )
    result = provider._publish_one(consumer, dry_run=True, data_product_specification="odps")
    assert [p["contractId"] for p in result["payload"]["inputPorts"]] == [
        "silver.sales.orders.orders"
    ]


@respx.mock
def test_the_legacy_registrar_entry_points_use_the_slugged_id():
    respx.put(url__regex=rf"{API}/api/.*").mock(return_value=httpx.Response(200, json={}))
    deleted = respx.delete(url__regex=rf"{API}/api/dataproducts/.*").mock(
        return_value=httpx.Response(200)
    )
    registrar = DataMeshManagerRegistrar(api_token="t")
    contract = _product("Silver.Sales.Orders", "orders")
    assert registrar.register("Silver.Sales.Orders", "orders", contract, {}).succeeded
    paths = [c.request.url.path for c in respx.calls if c.request.method == "PUT"]
    assert "/api/dataproducts/silver.sales.orders" in paths
    assert "/api/datacontracts/silver.sales.orders.orders" in paths
    assert registrar.unregister("Silver.Sales.Orders", "orders").succeeded
    assert [c.request.url.path for c in deleted.calls] == ["/api/dataproducts/silver.sales.orders"]


def test_the_upstream_lookup_waits_for_the_endpoint_check(monkeypatch):
    # Looking up how upstream ids are spelled reads the product list with the key; a
    # refused plain-http endpoint must see no request at all.
    monkeypatch.delenv("DMM_ALLOW_INSECURE_HTTP", raising=False)
    consumer = _product("hub", consumes=[{"productId": "Acme.Orders", "exposeId": "orders"}])
    payload = CatalogPublicationPayload.from_contract(consumer, {})
    with respx.mock(assert_all_called=False) as mock:
        sent = mock.route().mock(return_value=httpx.Response(200, json=[]))
        result = DataMeshManagerRegistrar(
            api_url="http://dmm.example.com", api_token="t"
        ).register_payload(payload)
    assert not result.succeeded and not sent.calls
