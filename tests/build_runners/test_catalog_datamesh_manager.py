# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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
    def test_register_happy_path_uses_bearer_auth(self):
        route = respx.put(
            "https://api.datamesh-manager.com/api/data-products/bronze.crm.salesforce_accounts"
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
        assert route.called
        # Bearer auth header sent.
        req = route.calls[0].request
        assert req.headers["Authorization"] == "Bearer t-123"
        body = json.loads(req.content)
        assert body["id"] == "bronze.crm.salesforce_accounts"
        assert body["owner"]["team"] == "data-platform"
        # Classifications round-tripped to ports.schema.
        email_field = next(f for f in body["ports"][0]["schema"] if f["name"] == "Email")
        assert email_field["classifications"] == ["pii", "email"]

    @respx.mock
    def test_register_4xx_surfaces_as_error(self):
        respx.put("https://api.datamesh-manager.com/api/data-products/bronze.x").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        registrar = DataMeshManagerRegistrar(api_token="t-x")
        result = registrar.register(
            product_id="bronze.x",
            expose_id="raw",
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
        # The user's flow: contract has ``catalog.register: [datamesh_manager]``,
        # publish_acquisition picks it up, our registrar publishes.
        from fluid_build.cli._acquisition_stage_ext import publish_acquisition

        from fluid_build.build_runners import _catalog as orch

        respx.put(
            "https://api.datamesh-manager.com/api/data-products/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(200, json={"ok": True}))

        orch.register_registrar("datamesh_manager", DataMeshManagerRegistrar(api_token="t"))
        try:
            results = publish_acquisition(_acquisition_contract(), tmp_path)
        finally:
            orch._REGISTRY.pop("datamesh_manager", None)
        assert len(results) == 1
        assert results[0].target == "datamesh_manager"
        assert results[0].succeeded is True
        assert results[0].urn == "dmm://bronze.crm.salesforce_accounts/accounts_raw"

    @respx.mock
    def test_publish_acquisition_dmm_5xx_surfaces_failure(self, tmp_path: Path):
        from fluid_build.cli._acquisition_stage_ext import publish_acquisition

        from fluid_build.build_runners import _catalog as orch

        respx.put(
            "https://api.datamesh-manager.com/api/data-products/bronze.crm.salesforce_accounts"
        ).mock(return_value=httpx.Response(502, text="bad gateway"))

        orch.register_registrar("datamesh_manager", DataMeshManagerRegistrar(api_token="t"))
        try:
            results = publish_acquisition(_acquisition_contract(), tmp_path)
        finally:
            orch._REGISTRY.pop("datamesh_manager", None)
        assert len(results) == 1
        assert results[0].succeeded is False
        assert "502" in (results[0].error or "")
