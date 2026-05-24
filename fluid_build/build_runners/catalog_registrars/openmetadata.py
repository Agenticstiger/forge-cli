# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenMetadata catalog registrar (REST).

Translates :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
into OpenMetadata's Tables API shape (``PUT /api/v1/tables`` per
asset).

Spec attachments — the FLUID-native classification, the ODPS spec,
and the per-asset ODCS contract — land in OpenMetadata's
``extension`` field (a free-form JSON object the platform preserves
on the entity). That mirrors how :mod:`datahub` attaches them to
``customProperties``: a single well-known location each backend
chooses to satisfy ``CatalogCapability.CUSTOM_PROPERTIES`` /
``PRODUCT_SPECS`` / ``PER_ASSET_CONTRACT``.

The legacy ``register(product_id, expose_id, contract, classifications)``
entry point builds a payload from its args and delegates to
:meth:`register_payload` so historical callers (test fixtures,
``register_all`` per-expose iteration) keep working.
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

LOG = logging.getLogger("fluid.acquire.catalog.openmetadata")


@dataclass
class OpenMetadataRegistrar(CatalogRegistrar):
    target: str = "openmetadata"
    base_url: str = "https://openmetadata.test"
    api_token: Optional[str] = None
    timeout_seconds: int = 30

    def register_payload(
        self, payload: CatalogPublicationPayload
    ) -> RegistrationResult:
        product_urn = f"forge://{payload.product.product_id}"
        last_err: Optional[str] = None
        published: List[str] = []
        for asset in payload.assets:
            asset_urn = f"forge://{payload.product.product_id}/{asset.asset_id}"
            payload_body = self._build_payload(payload, asset)
            try:
                self._put(payload_body)
                published.append(asset_urn)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                break
        if last_err is not None:
            return RegistrationResult(
                target="openmetadata",
                urn=product_urn,
                succeeded=False,
                error=last_err,
                metadata={"published_assets": published},
            )
        return RegistrationResult(
            target="openmetadata",
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
        """Legacy per-expose entry point — build a single-asset payload
        and delegate to :meth:`register_payload`."""
        payload = CatalogPublicationPayload.from_contract(contract, classifications)
        scoped = tuple(a for a in payload.assets if a.asset_id == expose_id)
        if not scoped:
            return RegistrationResult(
                target="openmetadata",
                urn=f"forge://{product_id}/{expose_id}",
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        # Surface the single-asset URN as the result's primary URN so
        # legacy callers (orchestrator iteration, mocks) keep getting
        # the per-expose URN they expect.
        urn = f"forge://{product_id}/{expose_id}"
        try:
            self._put(self._build_payload(payload, scoped[0]))
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="openmetadata", urn=urn, succeeded=False, error=str(exc)
            )
        return RegistrationResult(target="openmetadata", urn=urn, succeeded=True)

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        from fluid_build.util.safe_http import safe_httpx_client

        urn = f"forge://{product_id}/{expose_id}"
        try:
            with safe_httpx_client(
                base_url=self.base_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.delete(f"/api/v1/tables/name/{product_id}.{expose_id}")
                r.raise_for_status()
            return RegistrationResult(target="openmetadata", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="openmetadata", urn=urn, succeeded=False, error=str(exc)
            )

    # ── helpers ──────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _put(self, body: Dict[str, Any]) -> None:
        from fluid_build.util.safe_http import safe_httpx_client

        with safe_httpx_client(
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.put("/api/v1/tables", json=body, headers=self._headers())
            r.raise_for_status()

    @staticmethod
    def _build_payload(
        payload: CatalogPublicationPayload, asset: AssetPayload
    ) -> Dict[str, Any]:
        product = payload.product

        # ``extension`` is OpenMetadata's free-form JSON object on the
        # Table entity. We use it to carry every cross-backend
        # FLUID-native attachment so an analyst gets the same
        # ``fluid_layer`` / ``fluid_product_type`` / ``odcs_contract``
        # whether they're browsing DataHub, OpenMetadata, or Unity.
        extension: Dict[str, Any] = {}
        if product.layer:
            extension["fluid_layer"] = product.layer
        if product.product_type:
            extension["fluid_product_type"] = product.product_type
        if product.domain:
            extension["fluid_domain"] = product.domain
        if product.version:
            extension["fluid_version"] = product.version
        if asset.odcs_yaml:
            extension["odcs_contract"] = asset.odcs_yaml
        if payload.specs.fluid_yaml:
            extension["fluid_contract"] = payload.specs.fluid_yaml
        if payload.specs.odps_yaml:
            extension["odps_spec"] = payload.specs.odps_yaml

        columns = [
            {
                "name": c.name,
                "dataType": (c.native_type or "STRING").upper(),
                "description": c.description,
                "tags": [
                    {"tagFQN": f"PII.{label}"}
                    for label in (payload.classifications.get(c.name) or ())
                ],
            }
            for c in asset.schema
        ]
        body: Dict[str, Any] = {
            "name": asset.asset_id,
            "fullyQualifiedName": f"forge.{product.product_id}.{asset.asset_id}",
            "description": product.description,
            "tags": [{"tagFQN": t} for t in product.tags],
            "columns": columns,
        }
        if extension:
            body["extension"] = extension
        return body


# ── Plugin registration ─────────────────────────────────────────────────

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402


def _build_openmetadata_registrar(config: dict) -> OpenMetadataRegistrar:
    return OpenMetadataRegistrar(
        base_url=pick_endpoint(config, default="https://openmetadata.test"),
        api_token=pick_token(config),
        timeout_seconds=pick_int(config, "timeout", 30),
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="openmetadata",
        registrar_factory=_build_openmetadata_registrar,
        env_vars={
            "endpoint": (
                "FLUID_CATALOG_OPENMETADATA_URL",
                "OPENMETADATA_SERVER_URL",
                "OPENMETADATA_HOST",
            ),
            "api_token": (
                "FLUID_CATALOG_OPENMETADATA_TOKEN",
                "OPENMETADATA_JWT_TOKEN",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.CUSTOM_PROPERTIES,
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
                CatalogCapability.GLOSSARY_TERMS,
            }
        ),
        description="OpenMetadata via the v1 REST API",
    )
)
