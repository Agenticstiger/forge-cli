# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DataMesh Manager catalog registrar.

Wraps the existing ``DataMeshManagerProvider`` (used by the standalone
``fluid datamesh-manager publish`` command) so acquisition contracts
that include ``datamesh_manager`` in ``properties.catalog.register`` get
auto-published on first apply through the same dispatcher as the other
catalog targets.

Configuration:

* ``api_url`` — DataMesh Manager / Entropy Data REST endpoint. Defaults
  to the value the provider already uses; overridable via the
  ``DMM_API_URL`` env var.
* ``api_token`` — bearer token. Defaults to ``DMM_API_KEY`` env var.

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
            self.api_url or os.environ.get("DMM_API_URL") or "https://api.datamesh-manager.com"
        )
        self.api_token = self.api_token or os.environ.get("DMM_API_KEY")

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
        try:
            from fluid_build.util.safe_http import safe_httpx_client

            payload = self._build_payload(product_id, expose_id, contract, classifications)
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.put(
                    f"/api/data-products/{product_id}",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_token}",
                        "Content-Type": "application/json",
                    },
                )
                if r.status_code >= 400:
                    return RegistrationResult(
                        target=self.target,
                        urn=urn,
                        succeeded=False,
                        error=f"DMM PUT returned {r.status_code}: " + (r.text or "")[:512],
                    )
            return RegistrationResult(target=self.target, urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=str(exc))

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target, urn=urn, succeeded=False, error="DMM_API_KEY not set"
            )
        try:
            from fluid_build.util.safe_http import safe_httpx_client

            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
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
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=str(exc))

    @staticmethod
    def _build_payload(
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        owner = (contract.get("metadata") or {}).get("owner") or {}
        expose = next(
            (e for e in contract.get("exposes", []) if e.get("exposeId") == expose_id),
            (contract.get("exposes") or [{}])[0],
        )
        schema_cols = (expose.get("contract") or {}).get("schema") or []
        return {
            "id": product_id,
            "name": contract.get("name") or product_id,
            "description": contract.get("description", ""),
            "owner": {
                "team": owner.get("team", "unknown"),
                "email": owner.get("email", ""),
            },
            "ports": [
                {
                    "id": expose_id,
                    "type": expose.get("kind", "table"),
                    "schema": [
                        {
                            "name": c.get("name"),
                            "type": c.get("type", "string"),
                            "classifications": classifications.get(c.get("name", ""), []),
                        }
                        for c in schema_cols
                    ],
                    "platform": (expose.get("binding") or {}).get("platform"),
                }
            ],
            "tags": contract.get("tags", []),
            "metadata": contract.get("metadata", {}),
        }
