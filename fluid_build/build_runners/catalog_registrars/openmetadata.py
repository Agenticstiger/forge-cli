# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenMetadata catalog registrar (REST).

Posts to ``/api/v1/tables`` with the standard OpenMetadata payload shape.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

LOG = logging.getLogger("fluid.acquire.catalog.openmetadata")


@dataclass
class OpenMetadataRegistrar(CatalogRegistrar):
    target: str = "openmetadata"
    base_url: str = "https://openmetadata.test"
    api_token: Optional[str] = None
    timeout_seconds: int = 30

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        import httpx

        urn = f"forge://{product_id}/{expose_id}"
        payload = self._build_payload(product_id, expose_id, contract, classifications)
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as c:
                r = c.put("/api/v1/tables", json=payload, headers=headers)
                r.raise_for_status()
            return RegistrationResult(target="openmetadata", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="openmetadata", urn=urn, succeeded=False, error=str(exc)
            )

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        import httpx

        urn = f"forge://{product_id}/{expose_id}"
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as c:
                r = c.delete(f"/api/v1/tables/name/{product_id}.{expose_id}")
                r.raise_for_status()
            return RegistrationResult(target="openmetadata", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="openmetadata", urn=urn, succeeded=False, error=str(exc)
            )

    @staticmethod
    def _build_payload(
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        expose = (contract.get("exposes") or [{}])[0]
        schema_cols = (expose.get("contract") or {}).get("schema") or []
        return {
            "name": expose_id,
            "fullyQualifiedName": f"forge.{product_id}.{expose_id}",
            "description": contract.get("description", ""),
            "tags": [{"tagFQN": t} for t in contract.get("tags", [])],
            "columns": [
                {
                    "name": c.get("name"),
                    "dataType": (c.get("type", "STRING") or "STRING").upper(),
                    "description": c.get("description", ""),
                    "tags": [
                        {"tagFQN": f"PII.{lab}"}
                        for lab in classifications.get(c.get("name", ""), [])
                    ],
                }
                for c in schema_cols
            ],
        }
