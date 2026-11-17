# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a publish leaves behind in Data Mesh Manager / Entropy Data, on both paths.

* A contract the product's output ports used to name, and no longer do (an expose was
  removed or renamed), is retired: re-PUT with ``status: retired``, never deleted, so
  whoever still reads it sees it is gone. Only contracts in the product's own
  ``{productId}.`` namespace are forge's to retire.
* No umbrella contract at ``{productId}``: it described no port, named no schema and
  showed up as an extra contract beside the real ones. One an earlier forge published
  (``customProperties[umbrella] = true``) is retired.
* An output port names only a contract that exists: a port whose contract this publish
  does not send, and Entropy does not have, carries no ``contractId``.
* A per-expose ODCS contract carries its expose's ``version``.
* The registrar PUTs a contract at the id the contract (and its output port) names,
  so a pinned ``exposes[].contract.id`` resolves.

Entropy Data CE accepts ``status: retired`` on ``PUT /api/datacontracts/{id}`` and
returns it on read.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from fluid_build.api.catalog_publication import CatalogPublicationPayload
from fluid_build.build_runners.catalog_registrars.datamesh_manager import (
    DataMeshManagerRegistrar,
)
from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

API = "https://api.datamesh-manager.com"
PID = "gold.sales.kpis"


def _expose(expose_id: str, **extra: Any) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "parquet",
            "location": {"path": f"out/{expose_id}.parquet"},
        },
        "contract": {"schema": [{"name": "kpi", "type": "VARCHAR"}]},
        **extra,
    }


def _product(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PID,
        "name": "KPIs",
        "metadata": {"owner": {"team": "analytics"}},
        "exposes": list(exposes) or [_expose("kpis")],
    }


def _existing_product(*contract_ids: str) -> Dict[str, Any]:
    return {
        "id": PID,
        "outputPorts": [
            {"name": cid.rsplit(".", 1)[-1], "contractId": cid} for cid in contract_ids
        ],
    }


def _contract(cid: str, *, status: str = "active", umbrella: bool = False) -> Dict[str, Any]:
    body: Dict[str, Any] = {"id": cid, "apiVersion": "v3.1.0", "status": status}
    if umbrella:
        body["customProperties"] = [{"property": "umbrella", "value": True}]
    return body


# Entropy state for the stale-contract cases: the product used to expose ``kpis`` and
# ``old``, named a shared contract outside its namespace, and an earlier forge published
# an umbrella. ``gone`` was named and is no longer in Entropy at all.
_OLD = _existing_product(f"{PID}.kpis", f"{PID}.old", f"{PID}.gone", "shared.orders")
_STORE = {
    f"{PID}.kpis": _contract(f"{PID}.kpis"),
    f"{PID}.old": _contract(f"{PID}.old"),
    "shared.orders": _contract("shared.orders"),
    PID: _contract(PID, umbrella=True),
}


# ── fluid publish --target datamesh-manager (the registrar) ──────────────


def _mock_entropy(
    existing_product: Optional[Dict[str, Any]], store: Dict[str, Dict[str, Any]]
) -> None:
    respx.put(url__regex=rf"{API}/api/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{API}/api/dataproducts").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/api/dataproducts/{PID}").mock(
        return_value=(
            httpx.Response(200, json=existing_product)
            if existing_product
            else httpx.Response(404, json={})
        )
    )

    def contract(request: httpx.Request) -> httpx.Response:
        cid = request.url.path.rsplit("/", 1)[-1]
        if cid in store:
            return httpx.Response(200, json=store[cid])
        return httpx.Response(404, json={})

    respx.get(url__regex=rf"{API}/api/datacontracts/.*").mock(side_effect=contract)
    respx.get(url__regex=rf"{API}/api/teams/.*").mock(return_value=httpx.Response(200, json={}))
    respx.get(url__regex=rf"{API}/api/access\?.*").mock(return_value=httpx.Response(200, json=[]))


def _puts(prefix: str) -> Dict[str, Dict[str, Any]]:
    return {
        str(c.request.url).split(prefix, 1)[1]: json.loads(c.request.content)
        for c in respx.calls
        if c.request.method == "PUT" and prefix in str(c.request.url)
    }


def _registrar_publish(
    contract: Dict[str, Any],
    existing_product: Optional[Dict[str, Any]] = None,
    store: Optional[Dict[str, Dict[str, Any]]] = None,
):
    _mock_entropy(existing_product, store or {})
    payload = CatalogPublicationPayload.from_contract(contract, {})
    result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
    assert result.succeeded, result.error
    return result, _puts("/api/datacontracts/"), _puts("/api/dataproducts/")[PID]


class TestRegistrarContractHygiene:
    @respx.mock
    def test_a_contract_no_port_names_any_more_is_retired(self):
        result, contracts, _ = _registrar_publish(_product(), _OLD, dict(_STORE))
        retired = {cid for cid, body in contracts.items() if body.get("status") == "retired"}
        # ``old`` and the umbrella: forge's, and no longer named. ``gone`` is not in
        # Entropy; ``shared.orders`` is outside the product's namespace.
        assert retired == {f"{PID}.old", PID}
        assert contracts[f"{PID}.old"]["id"] == f"{PID}.old"
        assert contracts[f"{PID}.kpis"]["status"] == "active"
        assert not [c for c in respx.calls if c.request.method == "DELETE"]
        urns = result.metadata["contract_urns"]
        assert f"dmm://datacontracts/{PID}.old?retired=stale" in urns

    @respx.mock
    def test_a_contract_already_retired_is_left_alone(self):
        store = dict(_STORE, **{f"{PID}.old": _contract(f"{PID}.old", status="retired")})
        store[PID] = _contract(PID, status="retired", umbrella=True)
        _, contracts, _ = _registrar_publish(_product(), _OLD, store)
        assert set(contracts) == {f"{PID}.kpis"}

    @respx.mock
    def test_a_contract_at_the_product_id_that_is_no_umbrella_is_not_forges(self):
        store = dict(_STORE, **{PID: _contract(PID)})
        _, contracts, _ = _registrar_publish(_product(), _existing_product(f"{PID}.kpis"), store)
        assert set(contracts) == {f"{PID}.kpis"}

    @respx.mock
    def test_a_first_publish_retires_nothing(self):
        _, contracts, _ = _registrar_publish(_product(), None, {})
        assert set(contracts) == {f"{PID}.kpis"}

    @respx.mock
    def test_the_contract_carries_its_expose_version(self):
        contract = _product(_expose("kpis", version="2.1.0"))
        _, contracts, product = _registrar_publish(contract)
        assert contracts[f"{PID}.kpis"]["version"] == "2.1.0"
        assert product["outputPorts"][0]["version"] == "2.1.0"

    @respx.mock
    def test_a_pinned_contract_id_is_where_the_contract_is_put(self):
        expose = _expose("kpis")
        expose["contract"]["id"] = "kpis-contract"
        _, contracts, product = _registrar_publish(_product(expose))
        assert set(contracts) == {"kpis-contract"}
        assert contracts["kpis-contract"]["id"] == "kpis-contract"
        assert product["outputPorts"][0]["contractId"] == "kpis-contract"

    @respx.mock
    def test_a_port_whose_contract_was_not_sent_and_does_not_exist_names_none(self, monkeypatch):
        from fluid_build.api import catalog_publication

        # No ODCS for the asset: the registrar sends no contract for it.
        monkeypatch.setattr(catalog_publication, "_render_odcs_yaml", lambda *a, **k: None)
        _, contracts, product = _registrar_publish(_product())
        assert contracts == {}
        assert "contractId" not in product["outputPorts"][0]

    @respx.mock
    def test_a_port_whose_contract_exists_already_keeps_it(self, monkeypatch):
        from fluid_build.api import catalog_publication

        monkeypatch.setattr(catalog_publication, "_render_odcs_yaml", lambda *a, **k: None)
        store = {f"{PID}.kpis": _contract(f"{PID}.kpis")}
        _, _, product = _registrar_publish(_product(), None, store)
        assert product["outputPorts"][0]["contractId"] == f"{PID}.kpis"


# ── fluid datamesh-manager publish (the native provider) ─────────────────


def _native_publish(
    contract: Dict[str, Any],
    existing_product: Optional[Dict[str, Any]] = None,
    store: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    publish_contract: bool = True,
    spec: str = "odps",
):
    store = dict(store or {})
    calls: List[tuple] = []

    def request(method: str, path: str, *, json_body: Any = None):
        calls.append((method, path, json_body))
        resp = MagicMock(status_code=200)
        resp.json.return_value = {}
        if method == "GET" and path == f"/api/dataproducts/{PID}":
            if not existing_product:
                raise ProviderError(f"Entropy Data API error 404 on GET {path}: not found")
            resp.json.return_value = existing_product
        elif method == "GET" and path.startswith("/api/datacontracts/"):
            cid = path.rsplit("/", 1)[-1]
            if cid not in store:
                raise ProviderError(f"Entropy Data API error 404 on GET {path}: not found")
            resp.json.return_value = store[cid]
        elif method == "GET" and path.startswith("/api/access"):
            resp.json.return_value = []
        return resp

    provider = DataMeshManagerProvider(api_key="k", api_url=API)
    provider._request = request  # type: ignore[method-assign]
    provider._ensure_team = lambda *a, **k: None  # type: ignore[method-assign]
    result = provider._publish_one(
        contract, publish_contract=publish_contract, data_product_specification=spec
    )
    contracts = {
        path.rsplit("/", 1)[-1]: body
        for method, path, body in calls
        if method == "PUT" and path.startswith("/api/datacontracts/")
    }
    product = next(
        body for method, path, body in calls if method == "PUT" and path.endswith(f"/{PID}")
    )
    return result, contracts, product, calls


class TestNativeContractHygiene:
    def test_no_umbrella_contract_is_published(self):
        _, contracts, _, _ = _native_publish(_product())
        assert set(contracts) == {f"{PID}.kpis"}

    def test_a_contract_no_port_names_any_more_is_retired(self):
        result, contracts, _, calls = _native_publish(_product(), _OLD, dict(_STORE))
        retired = {cid for cid, body in contracts.items() if body.get("status") == "retired"}
        assert retired == {f"{PID}.old", PID}
        assert result["retired_contracts"] == sorted([f"{PID}.old", PID])
        assert not [c for c in calls if c[0] == "DELETE"]

    def test_without_contracts_a_port_names_only_a_contract_that_exists(self):
        contract = _product(_expose("kpis"), _expose("fresh"))
        store = {f"{PID}.kpis": _contract(f"{PID}.kpis")}
        _, contracts, product, _ = _native_publish(contract, None, store, publish_contract=False)
        ports = {p["name"]: p.get("contractId") for p in product["outputPorts"]}
        assert ports == {"kpis": f"{PID}.kpis", "fresh": None}
        assert contracts == {}

    def test_a_port_is_kept_when_existence_cannot_be_read(self):
        contract = _product()
        provider = DataMeshManagerProvider(api_key="k", api_url=API)
        calls: List[tuple] = []

        def request(method: str, path: str, *, json_body: Any = None):
            calls.append((method, path, json_body))
            if method == "GET" and path.startswith("/api/datacontracts/"):
                raise ProviderError(f"Entropy Data API error 503 on GET {path}: down")
            if method == "GET" and path == f"/api/dataproducts/{PID}":
                raise ProviderError(f"Entropy Data API error 404 on GET {path}: none")
            resp = MagicMock(status_code=200)
            resp.json.return_value = []
            return resp

        provider._request = request  # type: ignore[method-assign]
        provider._ensure_team = lambda *a, **k: None  # type: ignore[method-assign]
        provider._publish_one(contract, publish_contract=False, data_product_specification="odps")
        product = next(b for m, p, b in calls if m == "PUT" and p.endswith(f"/{PID}"))
        assert product["outputPorts"][0]["contractId"] == f"{PID}.kpis"

    def test_the_contract_carries_its_expose_version(self):
        _, contracts, _, _ = _native_publish(_product(_expose("kpis", version="2.1.0")))
        assert contracts[f"{PID}.kpis"]["version"] == "2.1.0"

    def test_dry_run_sends_nothing_and_previews_no_umbrella(self):
        provider = DataMeshManagerProvider(api_key="k", api_url=API)
        provider._request = MagicMock(side_effect=AssertionError("dry run sent a request"))
        result = provider._publish_one(
            _product(), dry_run=True, publish_contract=True, data_product_specification="odps"
        )
        assert "odcs_product_umbrella" not in result
        assert [p["url"].rsplit("/", 1)[-1] for p in result["odcs_contracts"]] == [f"{PID}.kpis"]


@pytest.mark.parametrize("version", [None, "1.0.0"])
def test_odcs_version_defaults_when_the_expose_has_none(version):
    from fluid_build.providers.odcs import OdcsProvider

    expose = _expose("kpis") if version is None else _expose("kpis", version=version)
    body = OdcsProvider().render(_product(expose), expose_id="kpis")
    assert body["version"] == "1.0.0"


def test_a_dps_port_still_naming_its_contract_keeps_it_active():
    # The DPS 0.0.1 payload spells the reference ``dataContractId``.
    store = {f"{PID}.kpis": _contract(f"{PID}.kpis")}
    result, contracts, product, _ = _native_publish(
        _product(), _existing_product(f"{PID}.kpis"), store, publish_contract=False, spec="0.0.1"
    )
    assert product["outputPorts"][0]["dataContractId"] == f"{PID}.kpis"
    assert "retired_contracts" not in result
    assert contracts == {}


def test_a_port_does_not_link_to_a_contract_a_publish_retired():
    # ``old`` was retired when its expose went; the expose is back, but this publish sends
    # no contract, so the port must not point at the retired one.
    store = {f"{PID}.old": _contract(f"{PID}.old", status="retired")}
    _, _, product, _ = _native_publish(
        _product(_expose("old")), None, store, publish_contract=False
    )
    assert "contractId" not in product["outputPorts"][0]


@respx.mock
def test_the_registrar_does_not_link_to_a_retired_contract(monkeypatch):
    from fluid_build.api import catalog_publication

    monkeypatch.setattr(catalog_publication, "_render_odcs_yaml", lambda *a, **k: None)
    store = {f"{PID}.kpis": _contract(f"{PID}.kpis", status="retired")}
    _, _, product = _registrar_publish(_product(), None, store)
    assert "contractId" not in product["outputPorts"][0]
