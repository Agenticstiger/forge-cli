# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DataMesh Manager catalog registrar.

Translates :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
into Data Mesh Manager's two-endpoint shape:

* ``PUT /api/data-products/{id}`` — the data product (one per contract).
  Body is the **rendered ODPS spec** from ``payload.specs.odps_yaml``;
  DMM accepts the ODPS-Bitol v1.0.0 schema directly on this endpoint,
  which is the same wire shape its UI consumes.
* ``PUT /api/datacontracts/{product_id}.{expose_id}`` — one **rendered
  ODCS contract** per asset. Body is the YAML from
  ``asset.odcs_yaml`` parsed back to a dict.

That's the exact division DMM's UI expects (the data-product page +
the linked data-contract sub-page). Same DMM payload-shape the older
async :class:`DataMeshManagerProvider` PUTs — this registrar is the
canonical-layer-driven equivalent so contracts that declare
``catalog.register: [datamesh_manager]`` get the same artifacts.

Configuration:

* ``api_url`` — DMM / Entropy Data REST endpoint. Defaults via env
  ``DMM_API_URL``; final fallback is the public datamesh-manager.com.
* ``api_token`` — bearer token. Defaults via env ``DMM_API_KEY``.

Errors are wrapped in ``RegistrationResult.error`` so a DMM outage
downgrades to "not registered" rather than crashing the run — catalog
auto-registration is observability, not correctness.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import (
    AssetPayload,
    CatalogPublicationPayload,
)

LOG = logging.getLogger("fluid.acquire.catalog.datamesh_manager")


@dataclass
class DataMeshManagerRegistrar(CatalogRegistrar):
    target: str = "datamesh_manager"
    api_url: Optional[str] = None
    api_token: Optional[str] = None
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        # Late env-var fallback so env state at register-time wins.
        self.api_url = (
            self.api_url
            or os.environ.get("DMM_API_URL")
            or "https://api.datamesh-manager.com"
        )
        self.api_token = self.api_token or os.environ.get("DMM_API_KEY")

    # ── Canonical entry point ─────────────────────────────────────────

    def register_payload(
        self, payload: CatalogPublicationPayload
    ) -> RegistrationResult:
        """Publish *payload* to Data Mesh Manager end-to-end.

        Two phases:

        1. ``PUT /api/data-products/{product_id}`` — the ODPS-shaped
           data product (single per contract).
        2. For each asset: ``PUT /api/datacontracts/{product_id}.{expose_id}``
           — the per-asset ODCS contract.

        Failure on either short-circuits with the first error. The
        returned URN is the DMM data-product URN; per-asset URNs land
        in ``metadata['contract_urns']`` so callers can navigate to
        the linked contract pages.
        """
        product_id = payload.product.product_id
        product_urn = f"dmm://{product_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=product_urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
        contract_urns: List[str] = []
        try:
            self._put_data_product(payload)
            for asset in payload.assets:
                contract_id = f"{product_id}.{asset.asset_id}"
                contract_urn = f"dmm://datacontracts/{contract_id}"
                self._put_data_contract(contract_id, asset)
                contract_urns.append(contract_urn)
        except _HttpStatusError as exc:
            return RegistrationResult(
                target=self.target,
                urn=product_urn,
                succeeded=False,
                error=str(exc),
                metadata={"contract_urns": contract_urns},
            )
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target=self.target,
                urn=product_urn,
                succeeded=False,
                error=str(exc),
                metadata={"contract_urns": contract_urns},
            )
        return RegistrationResult(
            target=self.target,
            urn=product_urn,
            succeeded=True,
            metadata={"contract_urns": contract_urns},
        )

    # ── Legacy per-expose entry point ─────────────────────────────────

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        """Backward-compatible per-expose entry point.

        Builds a canonical payload from the contract, scopes it to the
        requested expose, and delegates to :meth:`register_payload`.
        Preserves the per-expose URN shape the orchestrator's
        historical iteration relied on (``dmm://<product>/<expose>``).
        """
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
        payload = CatalogPublicationPayload.from_contract(contract, classifications)
        scoped = tuple(a for a in payload.assets if a.asset_id == expose_id)
        if not scoped:
            return RegistrationResult(
                target=self.target,
                urn=urn,
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        try:
            # PUT the data product (whole, ODPS shape) — same as canonical.
            self._put_data_product(payload)
            # PUT only the scoped asset's contract — preserves historical
            # behaviour where ``register("p", "x", ...)`` PUT only the
            # ``p.x`` contract.
            contract_id = f"{product_id}.{expose_id}"
            self._put_data_contract(contract_id, scoped[0])
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target=self.target, urn=urn, succeeded=False, error=str(exc)
            )
        return RegistrationResult(target=self.target, urn=urn, succeeded=True)

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target, urn=urn, succeeded=False, error="DMM_API_KEY not set"
            )
        try:
            import httpx

            with httpx.Client(base_url=self.api_url, timeout=self.timeout_seconds) as c:
                r = c.delete(
                    f"/api/data-products/{product_id}",
                    headers={"Authorization": f"Bearer {self.api_token}"},
                )
                if r.status_code >= 400 and r.status_code != 404:
                    return RegistrationResult(
                        target=self.target,
                        urn=urn,
                        succeeded=False,
                        error=f"DMM DELETE returned {r.status_code}",
                    )
            return RegistrationResult(target=self.target, urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target=self.target, urn=urn, succeeded=False, error=str(exc)
            )

    # ── HTTP helpers ──────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def _put_data_product(self, payload: CatalogPublicationPayload) -> None:
        """``PUT /api/data-products/{id}`` with the ODPS-shaped payload.

        We prefer the pre-rendered ODPS YAML when available (parsed
        back to a dict via PyYAML) so the wire body is exactly what
        ``fluid render --format odps`` would emit. Falls back to a
        minimal native shape — derived directly from the canonical
        payload — when ODPS rendering wasn't available.
        """
        import httpx

        body = self._render_dmm_data_product_body(payload)
        with httpx.Client(base_url=self.api_url, timeout=self.timeout_seconds) as c:
            r = c.put(
                f"/api/data-products/{payload.product.product_id}",
                json=body,
                headers=self._headers(),
            )
            if r.status_code >= 400:
                raise _HttpStatusError(
                    f"DMM PUT /data-products returned {r.status_code}: "
                    + (r.text or "")[:512]
                )

    def _put_data_contract(self, contract_id: str, asset: AssetPayload) -> None:
        """``PUT /api/datacontracts/{contract_id}`` with the ODCS body.

        ``contract_id`` is ``{product_id}.{asset_id}`` to match DMM's
        own per-port linkage convention (see
        ``DataMeshManagerProvider._publish_odcs_per_expose``).
        """
        import httpx

        body = self._render_dmm_data_contract_body(asset)
        if body is None:
            # No ODCS available — silently skip the contract PUT rather
            # than POSTing an empty body that DMM would reject. The
            # data-product PUT above still landed.
            return
        with httpx.Client(base_url=self.api_url, timeout=self.timeout_seconds) as c:
            r = c.put(
                f"/api/datacontracts/{contract_id}",
                json=body,
                headers=self._headers(),
            )
            if r.status_code >= 400:
                raise _HttpStatusError(
                    f"DMM PUT /datacontracts/{contract_id} returned "
                    f"{r.status_code}: " + (r.text or "")[:512]
                )

    # ── Body renderers ────────────────────────────────────────────────

    @staticmethod
    def _render_dmm_data_product_body(
        payload: CatalogPublicationPayload,
    ) -> Dict[str, Any]:
        """Prefer the ODPS YAML rendered at payload-build time, falling
        back to a native shape so the PUT can't silently no-op."""
        if payload.specs.odps_yaml:
            try:
                import yaml as _yaml

                parsed = _yaml.safe_load(payload.specs.odps_yaml)
                if isinstance(parsed, dict):
                    # Override id deterministically so DMM's path-route
                    # matches even if the renderer ever changes its
                    # ``id`` source.
                    parsed["id"] = payload.product.product_id
                    return parsed
            except Exception:  # noqa: BLE001 — fall through to native
                LOG.debug(
                    "ODPS YAML parse failed for %s — using native fallback",
                    payload.product.product_id,
                    exc_info=True,
                )
        # Native fallback: a minimal, DMM-readable data product shape.
        product = payload.product
        return {
            "id": product.product_id,
            "name": product.name or product.product_id,
            "description": product.description,
            "owner": {
                "team": product.owner.team if product.owner else "unknown",
                "email": product.owner.email if product.owner else "",
            },
            "ports": [
                {
                    "id": asset.asset_id,
                    "type": "table",
                    "platform": asset.platform,
                    "schema": [
                        {
                            "name": col.name,
                            "type": col.native_type,
                            "classifications": list(
                                payload.classifications.get(col.name) or ()
                            ),
                        }
                        for col in asset.schema
                    ],
                }
                for asset in payload.assets
            ],
            "tags": list(product.tags),
            "metadata": {
                "layer": product.layer,
                "productType": product.product_type,
                "domain": product.domain,
                "version": product.version,
            },
        }

    @staticmethod
    def _render_dmm_data_contract_body(asset: AssetPayload) -> Optional[Dict[str, Any]]:
        """Parse the asset's pre-rendered ODCS YAML back to a dict.
        DMM's ``/api/datacontracts/{id}`` endpoint accepts ODCS v3.1
        natively — no further translation needed."""
        if not asset.odcs_yaml:
            return None
        try:
            import yaml as _yaml

            parsed = _yaml.safe_load(asset.odcs_yaml)
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            LOG.debug("ODCS YAML parse failed for asset %s", asset.asset_id, exc_info=True)
            return None


class _HttpStatusError(Exception):
    """Lifted to a private exception so ``register_payload`` can
    distinguish HTTP-status failures (caller-actionable) from
    transport-layer failures (network down, DNS broken)."""


# ── Plugin registration ─────────────────────────────────────────────────
#
# Self-register so ``properties.catalog.register: [datamesh_manager]``
# resolves through the canonical layer (rather than only via the
# legacy native async provider that lives under
# ``fluid_build/providers/catalogs/datamesh_manager.py``). The native
# provider stays as the rich DMM-specific Surface A path with team
# management / access agreements; this registrar is the canonical-
# payload-driven Surface B equivalent.

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402


def _build_dmm_registrar(config: dict) -> DataMeshManagerRegistrar:
    return DataMeshManagerRegistrar(
        api_url=pick_endpoint(config, default=None) or None,
        api_token=pick_token(config),
        timeout_seconds=pick_int(config, "timeout", 30),
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="datamesh_manager",
        aliases=("datamesh-manager", "entropy-data", "dmm"),
        registrar_factory=_build_dmm_registrar,
        env_vars={
            "endpoint": (
                "FLUID_CATALOG_DMM_URL",
                "DMM_API_URL",
            ),
            "api_token": (
                "FLUID_CATALOG_DMM_TOKEN",
                "DMM_API_KEY",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.DATA_PRODUCT,
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
                CatalogCapability.DOMAIN,
                CatalogCapability.OWNERSHIP,
                CatalogCapability.CUSTOM_PROPERTIES,
            }
        ),
        description=(
            "Data Mesh Manager / Entropy Data via "
            "PUT /api/data-products (ODPS) + /api/datacontracts (ODCS per asset)"
        ),
    )
)
