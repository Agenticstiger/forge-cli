# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Archetype, transport and settings of a Data Mesh Manager / Entropy Data publish.

* Archetype: Entropy Data CE reads an ODPS v1.0.0 product's archetype from
  ``customProperties[type]`` and recognises only ``source-aligned``, ``aggregate``
  and ``consumer-aligned`` (a camelCase ``sourceAligned`` shows as a plain "Data
  Product"). Both publish paths sent no archetype, so every product was a plain
  "Data Product". It comes from ``metadata.productType`` (SDP / ADP / CDP), else
  ``metadata.layer`` (Bronze / Silver / Gold), as the DPS payload already did.
* Transport: the registrar (``fluid publish --target datamesh-manager``) sent the
  API key over plain http to any host. Like the native provider it now refuses a
  non-local http endpoint unless ``DMM_ALLOW_INSECURE_HTTP`` is set.
* Settings: the registrar publishes ODPS only and links lineage from consumes and
  build sources; ``DMM_DATA_PRODUCT_SPECIFICATION`` / ``DMM_PROVIDER_HINT`` naming
  anything else, and ``DMM_ODPS_LINEAGE_MODE``, were silently ignored. They warn.
* Paging: Entropy pages ``GET /api/dataproducts`` at 1000 (``p`` from 0); both paths
  read every page.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners.catalog_registrars.datamesh_manager import (
    DataMeshManagerRegistrar,
    _build_dmm_registrar,
)
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider
from fluid_build.providers.datamesh_manager._odps_helpers import dmm_archetype

API = "https://api.datamesh-manager.com"
PID = "gold.sales.kpis"


def _product(**metadata: Any) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PID,
        "name": "KPIs",
        "metadata": {"owner": {"team": "analytics"}, **metadata},
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


def _custom(body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        p.get("property"): p.get("value")
        for p in body.get("customProperties") or []
        if isinstance(p, dict)
    }


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"productType": "SDP"}, "source-aligned"),
        ({"productType": "ADP"}, "aggregate"),
        ({"productType": "CDP"}, "consumer-aligned"),
        ({"layer": "Bronze"}, "source-aligned"),
        ({"layer": "Silver"}, "aggregate"),
        ({"layer": "Gold"}, "consumer-aligned"),
        ({"layer": "Gold", "productType": "CDP"}, "consumer-aligned"),
        ({"archetype": "sourceAligned"}, "source-aligned"),
        ({"archetype": "consumer_aligned"}, "consumer-aligned"),
        ({}, None),
        ({"layer": "Platinum"}, None),
    ],
)
def test_the_archetype_entropy_recognises(metadata, expected):
    assert dmm_archetype(_product(**metadata)) == expected


# ── the registrar ────────────────────────────────────────────────────────


def _mock_entropy(pages: List[List[Dict[str, Any]]] | None = None) -> respx.Route:
    respx.put(url__regex=rf"{API}/api/.*").mock(return_value=httpx.Response(200, json={}))
    pages = pages or [[]]

    def listing(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("p", "0"))
        return httpx.Response(200, json=pages[page] if page < len(pages) else [])

    route = respx.get(url__regex=rf"{API}/api/dataproducts(\?.*)?$").mock(side_effect=listing)
    respx.get(url__regex=rf"{API}/api/dataproducts/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=rf"{API}/api/datacontracts/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=rf"{API}/api/teams/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=rf"{API}/api/access\?.*").mock(return_value=httpx.Response(200, json=[]))
    return route


def _registrar_product(contract: Dict[str, Any]) -> Dict[str, Any]:
    _mock_entropy()
    payload = CatalogPublicationPayload.from_contract(contract, {})
    result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
    assert result.succeeded, result.error
    return next(
        json.loads(c.request.content)
        for c in respx.calls
        if c.request.method == "PUT" and c.request.url.path == f"/api/dataproducts/{PID}"
    )


class TestRegistrar:
    @respx.mock
    def test_the_product_carries_its_archetype(self):
        body = _registrar_product(_product(productType="CDP"))
        assert _custom(body)["type"] == "consumer-aligned"

    @respx.mock
    def test_a_product_without_one_carries_none(self):
        assert "type" not in _custom(_registrar_product(_product()))

    def test_plain_http_to_a_remote_host_is_refused(self, monkeypatch):
        monkeypatch.delenv("DMM_ALLOW_INSECURE_HTTP", raising=False)
        payload = CatalogPublicationPayload.from_contract(_product(), {})
        with respx.mock(assert_all_called=False) as mock:
            sent = mock.route().mock(return_value=httpx.Response(200, json={}))
            result = DataMeshManagerRegistrar(
                api_url="http://dmm.example.com", api_token="t"
            ).register_payload(payload)
        assert not result.succeeded
        assert "DMM_ALLOW_INSECURE_HTTP" in result.error
        assert not sent.calls  # the key went nowhere

    def test_plain_http_to_a_local_host_or_with_the_opt_in_is_allowed(self, monkeypatch):
        for url, opt_in in (("http://localhost:8081", None), ("http://dmm.example.com", "1")):
            if opt_in:
                monkeypatch.setenv("DMM_ALLOW_INSECURE_HTTP", opt_in)
            payload = CatalogPublicationPayload.from_contract(_product(), {})
            with respx.mock(assert_all_called=False) as mock:
                mock.route().mock(return_value=httpx.Response(200, json=[]))
                result = DataMeshManagerRegistrar(api_url=url, api_token="t").register_payload(
                    payload
                )
            assert result.succeeded, (url, result.error)

    @respx.mock
    def test_every_page_of_products_is_read(self):
        first = [{"id": f"p{i}"} for i in range(1000)]
        route = _mock_entropy([first, [{"id": "late"}]])
        ids = DataMeshManagerRegistrar(api_token="t")._existing_product_ids()
        assert "late" in ids and "p0" in ids and len(ids) == 1001
        assert [c.request.url.params.get("p") for c in route.calls] == ["0", "1"]

    @pytest.mark.parametrize(
        "config,env,needle",
        [
            ({"data_product_specification": "0.0.1"}, {}, "data_product_specification"),
            ({"provider_hint": "dps"}, {}, "provider_hint"),
            ({}, {"DMM_ODPS_LINEAGE_MODE": "source-system"}, "DMM_ODPS_LINEAGE_MODE"),
        ],
    )
    def test_a_setting_the_registrar_does_not_use_warns(
        self, config, env, needle, monkeypatch, caplog
    ):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with caplog.at_level(logging.WARNING):
            _build_dmm_registrar(dict(config))
        assert any(needle in r.getMessage() for r in caplog.records), caplog.text

    def test_odps_settings_do_not_warn(self, monkeypatch, caplog):
        monkeypatch.delenv("DMM_ODPS_LINEAGE_MODE", raising=False)
        with caplog.at_level(logging.WARNING):
            _build_dmm_registrar({"data_product_specification": "odps", "provider_hint": "odps"})
        assert not caplog.records


# ── the native provider ──────────────────────────────────────────────────


def test_the_native_product_carries_its_archetype():
    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    result = provider._publish_one(
        _product(layer="Bronze"), dry_run=True, data_product_specification="odps"
    )
    assert _custom(result["payload"])["type"] == "source-aligned"


def test_the_native_listing_reads_every_page():
    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    pages = [[{"id": f"p{i}"} for i in range(1000)], [{"id": "late"}]]
    asked: List[str] = []

    def request(method: str, path: str, *, json_body: Any = None):
        asked.append(path)
        page = int(path.rsplit("p=", 1)[1]) if "p=" in path else 0
        resp = MagicMock(status_code=200)
        resp.json.return_value = pages[page] if page < len(pages) else []
        return resp

    provider._request = request  # type: ignore[method-assign]
    products = provider.list_products()
    assert len(products) == 1001 and products[-1]["id"] == "late"
    assert asked == ["/api/dataproducts?p=0", "/api/dataproducts?p=1"]
