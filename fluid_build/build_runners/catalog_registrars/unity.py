# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Databricks Unity Catalog registrar (REST 2.1).

Translates the canonical
:class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
into Unity's ``POST /api/2.1/unity-catalog/tables`` shape.

Spec attachments use Unity's ``properties`` map — a free-form
``map<string, string>`` Unity preserves on the table entity. Same
canonical keys (``fluid_layer``, ``fluid_product_type``, ``odcs_contract``)
as DataHub's ``customProperties`` and OpenMetadata's ``extension``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import (
    AssetPayload,
    CatalogPublicationPayload,
)

LOG = logging.getLogger("fluid.acquire.catalog.unity")


@dataclass
class UnityCatalogRegistrar(CatalogRegistrar):
    target: str = "unity"
    base_url: str = "https://databricks.test"
    workspace_token: Optional[str] = None
    catalog_name: str = "forge"
    schema_name: str = "bronze"
    timeout_seconds: int = 30

    def register_payload(
        self, payload: CatalogPublicationPayload
    ) -> RegistrationResult:
        product_urn = f"unity://{self.catalog_name}.{self.schema_name}.{payload.product.product_id}"
        last_err: Optional[str] = None
        published: List[str] = []
        for asset in payload.assets:
            full_name = f"{self.catalog_name}.{self.schema_name}.{asset.asset_id}"
            urn = f"unity://{full_name}"
            body = self._build_payload(payload, asset, full_name)
            try:
                self._post(body)
                published.append(urn)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                break
        if last_err is not None:
            return RegistrationResult(
                target="unity",
                urn=product_urn,
                succeeded=False,
                error=last_err,
                metadata={"published_assets": published},
            )
        return RegistrationResult(
            target="unity",
            urn=product_urn,
            succeeded=True,
            metadata={"published_assets": published},
        )

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        payload = CatalogPublicationPayload.from_contract(contract, classifications)
        scoped = tuple(a for a in payload.assets if a.asset_id == expose_id)
        full_name = f"{self.catalog_name}.{self.schema_name}.{expose_id}"
        urn = f"unity://{full_name}"
        if not scoped:
            return RegistrationResult(
                target="unity",
                urn=urn,
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        try:
            self._post(self._build_payload(payload, scoped[0], full_name))
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="unity", urn=urn, succeeded=False, error=str(exc))
        return RegistrationResult(target="unity", urn=urn, succeeded=True)

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        import httpx

        full_name = f"{self.catalog_name}.{self.schema_name}.{expose_id}"
        urn = f"unity://{full_name}"
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as c:
                r = c.delete(f"/api/2.1/unity-catalog/tables/{full_name}")
                r.raise_for_status()
            return RegistrationResult(target="unity", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="unity", urn=urn, succeeded=False, error=str(exc))

    # ── helpers ──────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.workspace_token:
            headers["Authorization"] = f"Bearer {self.workspace_token}"
        return headers

    def _post(self, body: Dict[str, Any]) -> None:
        import httpx

        with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as c:
            r = c.post(
                "/api/2.1/unity-catalog/tables", json=body, headers=self._headers()
            )
            r.raise_for_status()

    def _build_payload(
        self,
        payload: CatalogPublicationPayload,
        asset: AssetPayload,
        full_name: str,
    ) -> Dict[str, Any]:
        product = payload.product

        # ``properties`` is Unity's free-form string→string map on
        # tables. Same canonical attachment shape as DataHub
        # customProperties / OpenMetadata extension.
        properties: Dict[str, str] = {}
        if product.layer:
            properties["fluid_layer"] = product.layer
        if product.product_type:
            properties["fluid_product_type"] = product.product_type
        if product.domain:
            properties["fluid_domain"] = product.domain
        if product.version:
            properties["fluid_version"] = product.version
        if asset.odcs_yaml:
            properties["odcs_contract"] = asset.odcs_yaml
        if payload.specs.fluid_yaml:
            properties["fluid_contract"] = payload.specs.fluid_yaml
        if payload.specs.odps_yaml:
            properties["odps_spec"] = payload.specs.odps_yaml

        return {
            "name": full_name.split(".")[-1],
            "catalog_name": self.catalog_name,
            "schema_name": self.schema_name,
            "table_type": "MANAGED",
            "data_source_format": "DELTA",
            "comment": product.description,
            "properties": properties,
            "columns": [
                {
                    "name": c.name,
                    "type_name": (c.native_type or "STRING").upper(),
                    "comment": c.description,
                    "tags": [
                        {
                            "key": "pii",
                            "value": ",".join(payload.classifications.get(c.name) or ()),
                        }
                    ],
                }
                for c in asset.schema
            ],
        }


# ── Plugin registration ─────────────────────────────────────────────────

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402


def _build_unity_registrar(config: dict) -> UnityCatalogRegistrar:
    return UnityCatalogRegistrar(
        base_url=pick_endpoint(config, default="https://databricks.test"),
        workspace_token=pick_token(config),
        catalog_name=str(config.get("catalog_name", "forge")),
        schema_name=str(config.get("schema_name", "bronze")),
        timeout_seconds=pick_int(config, "timeout", 30),
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="unity",
        aliases=("unity-catalog",),
        registrar_factory=_build_unity_registrar,
        env_vars={
            "endpoint": (
                "FLUID_CATALOG_UNITY_URL",
                "DATABRICKS_HOST",
                "UNITY_CATALOG_URL",
            ),
            "api_token": (
                "FLUID_CATALOG_UNITY_TOKEN",
                "DATABRICKS_TOKEN",
            ),
            "catalog_name": (
                "FLUID_CATALOG_UNITY_NAME",
                "UNITY_CATALOG_NAME",
            ),
            "schema_name": (
                "FLUID_CATALOG_UNITY_SCHEMA",
                "UNITY_SCHEMA_NAME",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.CUSTOM_PROPERTIES,
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
            }
        ),
        description="Databricks Unity Catalog via REST 2.1",
    )
)
