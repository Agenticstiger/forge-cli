# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenMetadata catalog registrar (REST).

Translates :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
into OpenMetadata's Tables API shape (``PUT /api/v1/tables`` per asset),
then registers the per-asset ODCS contract against OpenMetadata's
first-class Data Contracts entity.

Two-step publish, because OpenMetadata models these separately:

1. ``PUT /api/v1/tables`` creates or updates the table entity and carries
   the FLUID-native attachments (``fluid_layer``, ``fluid_product_type``,
   the ODPS spec) in ``extension``, a free-form JSON object the platform
   preserves. That mirrors how :mod:`datahub` uses ``customProperties``.
2. ``PUT /api/v1/dataContracts/odcs/yaml`` registers the asset's ODCS
   v3.1.0 document as a real ``DataContract``, resolved onto the table
   from step 1.

Step 2 matters because OpenMetadata made Data Contracts a first-class
entity in 1.10. Writing the contract only into ``extension`` (which is
what this registrar used to do) leaves it as an opaque blob: invisible to
the contracts UI, to contract search, and to validation runs. The blob is
still written as a fallback so the integration degrades cleanly against
pre-1.10 servers, where the Data Contracts route 404s.

The ODCS route needs OpenMetadata's internal entity UUID rather than an
FQN, so :meth:`_resolve_entity_id` looks the table up by name first. The
whole of step 2 is best-effort: a failure there is logged and leaves the
table publish intact, because losing the catalog entry entirely would be
a worse outcome than losing the first-class contract link.

Endpoints verified against ``DataContractResource.java`` on
open-metadata/OpenMetadata ``main`` (class ``@Path("/v1/dataContracts")``;
ODCS import at ``PUT /odcs/yaml`` with query params ``entityId``,
``entityType``, ``mode`` and ``objectName``).

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

    def register_payload(self, payload: CatalogPublicationPayload) -> RegistrationResult:
        product_urn = f"forge://{payload.product.product_id}"
        last_err: Optional[str] = None
        published: List[str] = []
        # Assets whose table publish landed but whose ODCS Data Contract did
        # not — a partially-applied result the caller must be able to see.
        contract_degraded: Dict[str, str] = {}
        for asset in payload.assets:
            asset_urn = f"forge://{payload.product.product_id}/{asset.asset_id}"
            payload_body = self._build_payload(payload, asset)
            try:
                self._put(payload_body)
                degraded = self._publish_odcs_contract(payload_body["fullyQualifiedName"], asset)
                if degraded:
                    contract_degraded[asset.asset_id] = degraded
                published.append(asset_urn)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                break
        metadata: Dict[str, Any] = {"published_assets": published}
        if contract_degraded:
            metadata["odcs_contract_degraded"] = contract_degraded
            metadata["partial"] = True
        if last_err is not None:
            return RegistrationResult(
                target="openmetadata",
                urn=product_urn,
                succeeded=False,
                error=last_err,
                metadata=metadata,
            )
        return RegistrationResult(
            target="openmetadata",
            urn=product_urn,
            succeeded=True,
            metadata=metadata,
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
            body = self._build_payload(payload, scoped[0])
            self._put(body)
            degraded = self._publish_odcs_contract(body["fullyQualifiedName"], scoped[0])
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="openmetadata", urn=urn, succeeded=False, error=str(exc)
            )
        metadata: Dict[str, Any] = {}
        if degraded:
            metadata = {"odcs_contract_degraded": {expose_id: degraded}, "partial": True}
        return RegistrationResult(target="openmetadata", urn=urn, succeeded=True, metadata=metadata)

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

    def _resolve_entity_id(self, fqn: str) -> Optional[str]:
        """Look up OpenMetadata's internal UUID for a table by FQN.

        The ODCS import route keys on ``entityId``, not on the FQN, so the
        contract cannot be attached without this hop. Returns ``None`` when
        the table cannot be resolved, which the caller treats as "skip the
        contract publish" rather than as a failure.
        """
        from fluid_build.util.safe_http import safe_httpx_client

        with safe_httpx_client(
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.get(f"/api/v1/tables/name/{fqn}", headers=self._headers())
            r.raise_for_status()
            entity_id = r.json().get("id")
        return str(entity_id) if entity_id else None

    def _publish_odcs_contract(self, fqn: str, asset: AssetPayload) -> Optional[str]:
        """Register the asset's ODCS document as a first-class DataContract.

        Non-fatal by design. OpenMetadata only grew the Data Contracts entity
        in 1.10, so older servers 404 this route; the contract is still present
        in the table's ``extension`` blob either way, and the table publish has
        already succeeded by the time this runs.

        But "non-fatal" is not "invisible". The stated motivation for the Data
        Contracts route is that the extension blob is invisible to the
        contracts UI, to contract search and to validation runs — so falling
        back to exactly that state is a *partially applied* publish. It was
        logged at DEBUG and the result still said ok=True / err=None, which
        reads as full success. The degrade is now a WARNING and the reason is
        returned so :meth:`register_payload` can mark the result partial.

        Returns ``None`` on success (or when there is nothing to publish), and
        a short reason string when the contract did not land.
        """
        if not asset.odcs_yaml:
            return None
        try:
            entity_id = self._resolve_entity_id(fqn)
            if not entity_id:
                reason = "no resolvable entity id"
                LOG.warning(
                    "ODCS Data Contract NOT registered for %s (%s). The contract is still "
                    "in the table's extension blob, but it is invisible to the contracts "
                    "UI, contract search and validation runs.",
                    fqn,
                    reason,
                )
                return reason
            self._put_odcs_yaml(entity_id, asset.odcs_yaml)
            LOG.debug("Registered ODCS contract for %s", fqn)
            return None
        except Exception as exc:  # noqa: BLE001
            # Class-only: OpenMetadata error bodies can echo the token-bearing URL.
            reason = type(exc).__name__
            LOG.warning(
                "ODCS Data Contract NOT registered for %s (%s — a pre-1.10 OpenMetadata "
                "server does not serve /api/v1/dataContracts). The contract is still in "
                "the table's extension blob, but it is invisible to the contracts UI, "
                "contract search and validation runs.",
                fqn,
                reason,
            )
            return reason

    def _put_odcs_yaml(self, entity_id: str, odcs_yaml: str) -> None:
        """``PUT /api/v1/dataContracts/odcs/yaml`` with the raw ODCS document.

        ``mode=merge`` is OpenMetadata's own default and preserves fields
        that exist on the server but not in the imported document, which is
        the right semantics for a registrar that owns only part of the
        contract surface.
        """
        from fluid_build.util.safe_http import safe_httpx_client

        headers = dict(self._headers())
        headers["Content-Type"] = "application/yaml"
        with safe_httpx_client(
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.put(
                "/api/v1/dataContracts/odcs/yaml",
                params={"entityId": entity_id, "entityType": "table", "mode": "merge"},
                content=odcs_yaml.encode("utf-8"),
                headers=headers,
            )
            r.raise_for_status()

    @staticmethod
    def _build_payload(payload: CatalogPublicationPayload, asset: AssetPayload) -> Dict[str, Any]:
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
    CatalogNotConfiguredError,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402


def _build_openmetadata_registrar(config: dict) -> OpenMetadataRegistrar:
    # No placeholder default. ``https://openmetadata.test`` is a hostname that
    # exists only in this module's HTTP-mocked unit tests; handing it to a real
    # publish made an *unconfigured* target dial it and report
    # "cannot resolve hostname 'openmetadata.test'" — a DNS error where the
    # operator needed "you have not set FLUID_CATALOG_OPENMETADATA_URL".
    # Refusing here is what lets the dispatcher say so; it is also what
    # ``build_registrar``'s own docstring already promised.
    endpoint = pick_endpoint(config)
    if not endpoint:
        raise CatalogNotConfiguredError("openmetadata")
    return OpenMetadataRegistrar(
        base_url=endpoint,
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
