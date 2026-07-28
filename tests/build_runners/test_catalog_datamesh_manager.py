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

"""DataMesh Manager catalog registrar tests.

Exercises the registrar against a respx-mocked DMM API plus the
end-to-end publish_acquisition flow that the publish CLI now invokes
for ``pattern: acquisition`` contracts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import respx

from fluid_build.build_runners.catalog_registrars.datamesh_manager import (
    DataMeshManagerRegistrar,
)


def _acquisition_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.crm.salesforce_accounts",
        "name": "Salesforce Accounts (Bronze)",
        "description": "Source-aligned Bronze landing of Salesforce Accounts",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "tags": ["crm", "salesforce", "bronze"],
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "airbyte",
                "properties": {
                    "source": {
                        "kind": "salesforce",
                        "connection": {"instance_url": "x"},
                        "mode": "incremental_append",
                    },
                    "catalog": {"register": ["datamesh_manager"]},
                },
                "outputs": ["accounts_raw"],
            }
        ],
        "exposes": [
            {
                "exposeId": "accounts_raw",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "BRONZE",
                        "schema": "SALESFORCE",
                        "table": "ACCOUNTS",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "Id", "type": "string"},
                        {"name": "Name", "type": "string"},
                        {"name": "Email", "type": "string"},
                    ]
                },
            }
        ],
    }


# ── Registrar unit ───────────────────────────────────────────────────────


class TestDmmRegistrar:
    @respx.mock
    def test_register_happy_path_publishes_product_and_contract(self):
        """DMM's canonical publish is two-step: a data-product PUT
        (ODPS body) + one data-contract PUT per asset (ODCS body).
        That matches what ``DataMeshManagerProvider._publish_odcs_per_expose``
        does, and what the DMM UI surfaces as a data product with
        linked contract pages.
        """
        dp_route = respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))
        dc_route = respx.put(
            "https://api.datamesh-manager.com/api/datacontracts/"
            "bronze.crm.salesforce_accounts.accounts_raw"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))
        registrar = DataMeshManagerRegistrar(api_token="t-123")
        result = registrar.register(
            product_id="bronze.crm.salesforce_accounts",
            expose_id="accounts_raw",
            contract=_acquisition_contract(),
            classifications={"Email": ["pii", "email"]},
        )
        assert result.succeeded is True
        assert result.target == "datamesh_manager"
        assert result.urn == "dmm://bronze.crm.salesforce_accounts/accounts_raw"
        # Both PUTs landed
        assert dp_route.called, "data-product PUT must fire"
        assert dc_route.called, "datacontract PUT must fire (per-asset ODCS)"
        # Bearer auth on both calls.
        for call in (*dp_route.calls, *dc_route.calls):
            assert call.request.headers["Authorization"] == "Bearer t-123"
        # Data-product body is ODPS-shaped (carries the id at the top
        # level matching the DMM path-route).
        dp_body = json.loads(dp_route.calls[0].request.content)
        assert dp_body["id"] == "bronze.crm.salesforce_accounts"
        # Datacontract body is ODCS-shaped — pin the contract id matches
        # the path so DMM's UI link between port → contract resolves.
        dc_body = json.loads(dc_route.calls[0].request.content)
        assert dc_body["id"] == "bronze.crm.salesforce_accounts.accounts_raw"

    @respx.mock
    def test_register_4xx_on_data_product_surfaces_as_error(self):
        """4xx on the data-product PUT short-circuits the contract PUTs
        — DMM rejects orphan contracts (their lookups go through the
        owning product) so failing fast is the right move."""
        # Use the contract's actual id + an existing expose so the
        # canonical layer can derive a real payload; the 4xx still
        # fires on the data-product PUT below.
        respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(403, text="forbidden"))
        registrar = DataMeshManagerRegistrar(api_token="t-x")
        result = registrar.register(
            product_id="bronze.crm.salesforce_accounts",
            expose_id="accounts_raw",
            contract=_acquisition_contract(),
            classifications={},
        )
        assert result.succeeded is False
        assert "403" in (result.error or "")

    def test_register_without_token_refuses(self, monkeypatch):
        monkeypatch.delenv("DMM_API_KEY", raising=False)
        registrar = DataMeshManagerRegistrar(api_token=None)
        result = registrar.register(
            product_id="x",
            expose_id="r",
            contract=_acquisition_contract(),
            classifications={},
        )
        assert result.succeeded is False
        assert "DMM_API_KEY" in (result.error or "")

    def test_register_picks_up_env_vars(self, monkeypatch):
        monkeypatch.setenv("DMM_API_KEY", "env-token")
        monkeypatch.setenv("DMM_API_URL", "https://my-dmm.internal")
        registrar = DataMeshManagerRegistrar()
        assert registrar.api_token == "env-token"
        assert registrar.api_url == "https://my-dmm.internal"


# ── End-to-end via publish_acquisition ───────────────────────────────────


class TestPublishAcquisitionToDmm:
    @respx.mock
    def test_publish_acquisition_dispatches_to_dmm(self, tmp_path: Path):
        """Acquisition contract → DMM canonical publish.

        ``publish_acquisition`` calls ``register_payload`` once per
        contract — for DMM that fires two PUTs (per-asset ODCS contract,
        then the ODPS data product). The result projects to a per-expose
        PublishResult so existing CLI display code keeps working; ``urn``
        here is the DMM data-product URN (the canonical identity), not
        the per-expose URN the legacy path emitted.
        """
        from fluid_build.build_runners import _catalog as orch
        from fluid_build.cli._acquisition_stage_ext import publish_acquisition

        respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))
        respx.put(
            "https://api.datamesh-manager.com/api/datacontracts/"
            "bronze.crm.salesforce_accounts.accounts_raw"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))

        orch.register_registrar("datamesh_manager", DataMeshManagerRegistrar(api_token="t"))
        try:
            results = publish_acquisition(_acquisition_contract(), tmp_path)
        finally:
            orch._REGISTRY.pop("datamesh_manager", None)
        assert len(results) == 1
        assert results[0].target == "datamesh_manager"
        assert results[0].succeeded is True
        # Canonical URN is the DMM data-product URN — that's what an
        # operator clicks through to in the DMM UI.
        assert results[0].urn == "dmm://bronze.crm.salesforce_accounts"

    @respx.mock
    def test_publish_acquisition_dmm_5xx_surfaces_failure(self, tmp_path: Path):
        from fluid_build.build_runners import _catalog as orch
        from fluid_build.cli._acquisition_stage_ext import publish_acquisition

        # Contracts publish FIRST in the canonical order, so they need to
        # succeed before we can exercise a 502 on the data-product PUT.
        respx.put(
            "https://api.datamesh-manager.com/api/datacontracts/"
            "bronze.crm.salesforce_accounts.accounts_raw"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))
        respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(502, text="bad gateway"))

        orch.register_registrar("datamesh_manager", DataMeshManagerRegistrar(api_token="t"))
        try:
            results = publish_acquisition(_acquisition_contract(), tmp_path)
        finally:
            orch._REGISTRY.pop("datamesh_manager", None)
        assert len(results) == 1
        assert results[0].succeeded is False
        assert "502" in (results[0].error or "")


# ── Publish order (load-bearing for DMM UI contract-chip rendering) ───────


class TestDmmSourceSystemUpsert:
    """Source-aligned data products declare their ingestion source under
    ``builds[].properties.source``. The DMM registrar must upsert a
    SourceSystem entity per unique source AND inject ``sourceSystemId``
    onto the data product's matching input port so the SDP renders in
    DMM with an upstream lineage edge instead of free-floating.
    """

    @respx.mock
    def test_source_system_upserted_before_data_product(self):
        from fluid_build.api.catalog_publication import CatalogPublicationPayload

        sys_route = respx.put("https://api.datamesh-manager.com/api/sourcesystems/salesforce").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        respx.put(
            "https://api.datamesh-manager.com/api/datacontracts/"
            "bronze.crm.salesforce_accounts.accounts_raw"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))
        dp_route = respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))

        payload = CatalogPublicationPayload.from_contract(_acquisition_contract(), {})
        result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
        assert result.succeeded is True
        assert sys_route.called, "SourceSystem PUT must fire for SDP build source"

        # Body shape: id+name+owner+custom{type,kind,...}
        body = json.loads(sys_route.calls[0].request.content)
        assert body["id"] == "salesforce"
        assert body["owner"] == "data-platform"
        assert body["custom"]["kind"] == "salesforce"
        # kind_to_dmm_type maps Salesforce → "Salesforce" (TitleCase)
        assert "type" in body["custom"]

        # The data-product PUT body must reference the SourceSystem via
        # inputPorts[].sourceSystemId — otherwise DMM won't draw the
        # upstream edge on the lineage graph.
        dp_body = json.loads(dp_route.calls[0].request.content)
        ips = dp_body.get("inputPorts") or []
        assert ips, "data product must carry inputPorts wiring to the SourceSystem"
        assert any(
            ip.get("sourceSystemId") == "salesforce" for ip in ips
        ), "at least one inputPort must reference the upserted SourceSystem"

        # URN surfaces on metadata for catalog observability.
        assert "dmm://sourcesystems/salesforce" in result.metadata.get("source_system_urns", [])

    @respx.mock
    def test_source_system_failure_is_non_fatal(self):
        """If the SourceSystem PUT fails (4xx/5xx), the publish keeps
        going — at worst the lineage graph degrades, the data product
        still lands."""
        from fluid_build.api.catalog_publication import CatalogPublicationPayload

        respx.put("https://api.datamesh-manager.com/api/sourcesystems/salesforce").mock(
            return_value=httpx.Response(500, text="dmm down")
        )
        respx.put(
            "https://api.datamesh-manager.com/api/datacontracts/"
            "bronze.crm.salesforce_accounts.accounts_raw"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))
        respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))

        payload = CatalogPublicationPayload.from_contract(_acquisition_contract(), {})
        result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
        # Overall publish still succeeded.
        assert result.succeeded is True
        # Failed upsert is reflected in metadata (no URN listed).
        assert result.metadata.get("source_system_urns") == []


class TestDmmPublishOrder:
    """Entropy's ``OpenDataProductStandardUpdateService`` resolves each
    ``outputPorts[].contractId`` to an internal ``data_contract`` FK at
    the moment of the data-product PUT. If the per-asset contracts
    don't exist yet, the FK stays null and the UI shows
    "Add Data Contract…" forever. So the contracts MUST be PUT before
    the data product. This test pins the order — flipping it back would
    silently regress the contract-chip rendering.
    """

    @respx.mock
    def test_per_asset_contracts_publish_before_data_product(self):
        from fluid_build.api.catalog_publication import CatalogPublicationPayload

        ordered_paths: list[str] = []

        def _record(request):
            ordered_paths.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        respx.put(
            "https://api.datamesh-manager.com/api/datacontracts/"
            "bronze.crm.salesforce_accounts.accounts_raw"
        ).mock(side_effect=_record)
        respx.put(
            "https://api.datamesh-manager.com/api/dataproducts/bronze.crm.salesforce_accounts"
        ).mock(side_effect=_record)

        payload = CatalogPublicationPayload.from_contract(_acquisition_contract(), {})
        result = DataMeshManagerRegistrar(api_token="t").register_payload(payload)
        assert result.succeeded is True

        per_asset_path = "/api/datacontracts/bronze.crm.salesforce_accounts.accounts_raw"
        product_path = "/api/dataproducts/bronze.crm.salesforce_accounts"
        assert ordered_paths.index(per_asset_path) < ordered_paths.index(product_path)
