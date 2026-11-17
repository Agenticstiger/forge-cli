# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Lineage that Entropy Data (Data Mesh Manager) actually draws, on both publish paths.

What Entropy reads, observed against Entropy Data Community Edition by publishing
probe products and reading the edges its ``/{org}/graph`` page embeds:

* an ODPS ``inputPorts[].contractId`` alone draws a product -> product edge
  (status ``declared``), merged with an approved Access agreement for the same
  pair, so keeping the port never duplicates an edge;
* an ODPS input port links to a SourceSystem only through
  ``customProperties[sourceSystemId]``, and the edge label comes from
  ``customProperties[type]``; a native ``sourceSystemId`` field is dropped;
* an Access agreement across two teams stays ``requested``, and a requested
  agreement draws no edge. Lineage that rides only on agreements is missing for
  every consume that crosses a team, which is most of them in a mesh.

Entropy's own data-product builder writes the same shape: one ``inputPorts``
entry per upstream data product with ``name``, ``version`` and ``contractId``
(entropy-data/entropydata-dbt-demo-builder, skills/dataproduct-bootstrap).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import httpx
import respx

from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners.catalog_registrars.datamesh_manager import (
    DataMeshManagerRegistrar,
)
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

API = "https://api.datamesh-manager.com"


def _cdp() -> Dict[str, Any]:
    """A Gold product consuming two upstream products."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "gold.sales.kpis",
        "name": "KPIs",
        "metadata": {"layer": "Gold", "owner": {"team": "analytics"}},
        "consumes": [
            {"productId": "silver.sales.sessions", "exposeId": "sessions"},
            {"productId": "bronze.web.events", "exposeId": "events"},
        ],
        "exposes": [
            {
                "exposeId": "kpis",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/kpis.parquet"},
                },
                "contract": {"schema": [{"name": "kpi", "type": "VARCHAR"}]},
            }
        ],
    }


def _sdp() -> Dict[str, Any]:
    """A Bronze product landed from Kafka by an acquisition build."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "bronze.web.events",
        "name": "Events",
        "metadata": {"layer": "Bronze", "owner": {"team": "platform"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "kafka",
                        "connection": {"database": "clickstream"},
                        "mode": "incremental",
                        "streams": ["page_views"],
                    }
                },
                "outputs": ["events"],
            }
        ],
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/events.parquet"},
                },
                "contract": {"schema": [{"name": "event_id", "type": "VARCHAR"}]},
            }
        ],
    }


def _custom(port: Dict[str, Any]) -> Dict[str, Any]:
    return {
        p.get("property"): p.get("value")
        for p in port.get("customProperties") or []
        if isinstance(p, dict)
    }


def _access(access_id: str, consumer: str, *, managed: bool = True, **info: Any) -> Dict:
    agreement: Dict[str, Any] = {
        "id": access_id,
        "info": {"status": "approved", "active": True, **info},
        "provider": {"dataProductId": "up", "outputPortId": "port"},
        "consumer": {"dataProductId": consumer},
    }
    if managed:
        agreement["custom"] = {"managedBy": "forge-cli", "source": "fluid.consumes"}
    return agreement


# ── fluid publish --target datamesh-manager (the registrar) ──────────────


def _registrar_publish(contract: Dict[str, Any], existing_access: List[Dict] | None = None):
    respx.put(url__regex=rf"{API}/api/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/api/dataproducts").mock(
        return_value=httpx.Response(
            200, json=[{"id": "silver.sales.sessions"}, {"id": "bronze.web.events"}]
        )
    )
    respx.get(url__regex=rf"{API}/api/teams/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=rf"{API}/api/access\?.*").mock(
        return_value=httpx.Response(200, json=existing_access or [])
    )
    cancel = respx.post(url__regex=rf"{API}/api/access/.*/cancel").mock(
        return_value=httpx.Response(200, json={})
    )
    payload = CatalogPublicationPayload.from_contract(contract, {})
    result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
    product = next(
        json.loads(c.request.content)
        for c in respx.calls
        if c.request.method == "PUT" and "/api/dataproducts/" in str(c.request.url)
    )
    return result, product, cancel


class TestRegistrarLineage:
    @respx.mock
    def test_every_consume_is_an_input_port_naming_the_upstream_contract(self):
        result, product, _ = _registrar_publish(_cdp())
        assert result.succeeded, result.error
        ports = {p["name"]: p["contractId"] for p in product.get("inputPorts") or []}
        assert ports == {
            "sessions": "silver.sales.sessions.sessions",
            "events": "bronze.web.events.events",
        }
        lineage = [entry["id"] for entry in product["outputPorts"][0].get("inputContracts") or []]
        assert sorted(lineage) == ["bronze.web.events.events", "silver.sales.sessions.sessions"]

    @respx.mock
    def test_a_build_source_port_links_to_its_source_system_the_way_entropy_reads_it(self):
        result, product, _ = _registrar_publish(_sdp())
        assert result.succeeded, result.error
        (port,) = product["inputPorts"]
        assert _custom(port)["sourceSystemId"] == "kafka-clickstream"
        assert _custom(port)["type"] == "Kafka"
        # Native fields are not ODPS (the v1.0.0 InputPort is closed) and Entropy drops them.
        assert "sourceSystemId" not in port and "type" not in port

    @respx.mock
    def test_an_agreement_for_a_consume_the_contract_dropped_is_cancelled(self):
        kept = "gold.sales.kpis__uses__silver.sales.sessions__sessions"
        existing = [
            _access(kept, "gold.sales.kpis"),
            _access("gold.sales.kpis__uses__bronze.old__x", "gold.sales.kpis"),
            # Someone else's agreement for this consumer: not forge's to cancel.
            _access("manual-grant", "gold.sales.kpis", managed=False),
            # Already ended: nothing left to cancel.
            _access("gold.sales.kpis__uses__bronze.gone__y", "gold.sales.kpis", active=False),
        ]
        contract = _cdp()
        contract["consumes"] = contract["consumes"][:1]
        result, _, cancel = _registrar_publish(contract, existing)
        assert result.succeeded, result.error
        cancelled = [str(c.request.url) for c in cancel.calls]
        assert cancelled == [f"{API}/api/access/gold.sales.kpis__uses__bronze.old__x/cancel"]
        # An endTimestamp of now ends it immediately; without one Entropy keeps
        # the edge until the end of the day.
        body = json.loads(cancel.calls[0].request.content)
        assert body["reason"]
        # Backdated, so a server clock behind ours still reads it as the past.
        from datetime import datetime, timedelta, timezone

        end = datetime.strptime(body["endTimestamp"], "%Y-%m-%dT%H:%M:%SZ")
        assert end.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) - timedelta(minutes=5)
        assert "dmm://access/gold.sales.kpis__uses__bronze.old__x?cancelled=stale" in (
            result.metadata["access_urns"]
        )

    @respx.mock
    def test_a_failed_reconcile_does_not_fail_the_publish(self):
        respx.get(url__regex=rf"{API}/api/access\?.*").mock(return_value=httpx.Response(500))
        respx.put(url__regex=rf"{API}/api/.*").mock(return_value=httpx.Response(200, json={}))
        respx.get(f"{API}/api/dataproducts").mock(return_value=httpx.Response(200, json=[]))
        respx.get(url__regex=rf"{API}/api/teams/.*").mock(return_value=httpx.Response(200))
        payload = CatalogPublicationPayload.from_contract(_cdp(), {})
        assert DataMeshManagerRegistrar(api_token="t").register_payload(payload).succeeded


# ── fluid datamesh-manager publish (the native provider) ─────────────────


def _native_publish(contract: Dict[str, Any], existing_access: List[Dict] | None = None):
    calls: List[tuple] = []

    def fake_request(method: str, path: str, *, json_body: Any = None):
        calls.append((method, path, json_body))
        resp = MagicMock(status_code=200)
        if method == "GET" and path.startswith("/api/dataproducts?p="):
            resp.json.return_value = [{"id": "silver.sales.sessions"}, {"id": "bronze.web.events"}]
        elif method == "GET" and path.startswith("/api/access?"):
            resp.json.return_value = existing_access or []
        else:
            resp.json.return_value = {}
        return resp

    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    with (
        patch.object(DataMeshManagerProvider, "_request", side_effect=fake_request),
        patch.object(DataMeshManagerProvider, "_ensure_team"),
    ):
        result = provider._publish_one(
            contract,
            publish_contract=True,
            data_product_specification="odps",
        )
    product = next(
        body for m, p, body in calls if m == "PUT" and p.startswith("/api/dataproducts/")
    )
    return result, product, calls


class TestNativeProviderLineage:
    def test_every_consume_is_an_input_port_naming_the_upstream_contract(self):
        _, product, _ = _native_publish(_cdp())
        ports = {p["name"]: p["contractId"] for p in product.get("inputPorts") or []}
        assert ports == {
            "sessions": "silver.sales.sessions.sessions",
            "events": "bronze.web.events.events",
        }

    def test_a_build_source_port_links_to_its_source_system_the_way_entropy_reads_it(self):
        _, product, _ = _native_publish(_sdp())
        (port,) = product["inputPorts"]
        assert _custom(port)["sourceSystemId"] == "kafka-clickstream"
        assert _custom(port)["type"] == "Kafka"
        assert "sourceSystemId" not in port and "type" not in port

    def test_an_agreement_for_a_consume_the_contract_dropped_is_cancelled(self):
        existing = [
            _access("gold.sales.kpis__uses__bronze.old__x", "gold.sales.kpis"),
            _access("manual-grant", "gold.sales.kpis", managed=False),
        ]
        result, _, calls = _native_publish(_cdp(), existing)
        posted = [p for m, p, _ in calls if m == "POST"]
        assert posted == ["/api/access/gold.sales.kpis__uses__bronze.old__x/cancel"]
        assert result["cancelled_access_agreements"] == ["gold.sales.kpis__uses__bronze.old__x"]

    def test_the_lineage_mode_does_not_turn_an_upstream_product_into_a_source_system(self):
        """``source-system`` mode labels a consume port with its upstream product
        id for DMM deployments that want a ``sourceSystem`` on every port. That
        label must not become ``sourceSystemId``: no SourceSystem by that id
        exists, and the port's ``contractId`` already draws the edge."""
        provider = DataMeshManagerProvider(api_key="k", api_url=API)
        result = provider._publish_one(
            _cdp(),
            dry_run=True,
            publish_contract=True,
            data_product_specification="odps",
            odps_lineage_mode="source-system",
        )
        for port in result["payload"]["inputPorts"]:
            assert "sourceSystemId" not in _custom(port), port


def test_both_paths_send_the_same_input_ports():
    """One contract, one lineage: the two publish doors must not disagree."""
    with respx.mock:
        _, registrar_product, _ = _registrar_publish(_cdp())
    _, native_product, _ = _native_publish(_cdp())
    assert registrar_product.get("inputPorts") == native_product.get("inputPorts")


def test_two_upstreams_sharing_an_expose_id_keep_their_own_contracts():
    """The ODPS export renames the second port (``<tail>__customers``); the
    native publish's contract-id backfill matched ports by bare exposeId, so the
    first port took the second upstream's pinned contract and the first
    upstream vanished from the lineage."""
    contract = _cdp()
    contract["consumes"] = [
        {"productId": "silver.b1", "exposeId": "customers", "contractId": "c.b1"},
        {"productId": "silver.b2", "exposeId": "customers", "contractId": "c.b2"},
    ]
    _, product, _ = _native_publish(contract)
    assert sorted(p["contractId"] for p in product["inputPorts"]) == ["c.b1", "c.b2"]


def test_both_paths_mint_the_same_access_id():
    """The reconcile cancels any forge agreement whose id the current publish
    did not mint, so the two doors must spell one edge the same way."""
    from fluid_build.build_runners.catalog_registrars.datamesh_manager import _slug_access_id

    for args in (("gold.x", "silver.a", "cust-1"), ("gold.x", "silver/a", "c  d~e")):
        assert _slug_access_id(*args) == DataMeshManagerProvider._access_agreement_id(*args)


def test_both_paths_link_a_legacy_consume_source_system_the_same_way():
    """A ``consumes[].sourceSystem`` (outside the 0.7.7 schema, still accepted)
    gets its SourceSystem upserted on both paths, so both must link it."""
    contract = _sdp()
    contract["consumes"] = [
        {"productId": "silver.crm.core", "exposeId": "accounts", "sourceSystem": "salesforce"}
    ]
    with respx.mock:
        _, registrar_product, _ = _registrar_publish(contract)
    _, native_product, _ = _native_publish(contract)
    assert registrar_product["inputPorts"] == native_product["inputPorts"]
    linked = {p["name"]: _custom(p).get("sourceSystemId") for p in registrar_product["inputPorts"]}
    assert linked == {"accounts": "salesforce", "page_views": "kafka-clickstream"}


def test_a_rejected_or_cancelled_agreement_is_not_cancelled_again():
    from fluid_build.providers.datamesh_manager._publish_flow import stale_access_agreement_ids

    existing = [
        _access("a", "p", status="rejected", active=None),
        _access("b", "p", status="cancelled"),
        _access("c", "p", status="requested"),
    ]
    assert stale_access_agreement_ids(existing, "p", set()) == ["c"]
