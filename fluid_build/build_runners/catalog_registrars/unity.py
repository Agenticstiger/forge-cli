# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Databricks Unity Catalog registrar (REST 2.1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

LOG = logging.getLogger("fluid.acquire.catalog.unity")


@dataclass
class UnityCatalogRegistrar(CatalogRegistrar):
    target: str = "unity"
    base_url: str = "https://databricks.test"
    workspace_token: Optional[str] = None
    catalog_name: str = "forge"
    schema_name: str = "bronze"
    timeout_seconds: int = 30

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        import httpx

        full_name = f"{self.catalog_name}.{self.schema_name}.{expose_id}"
        urn = f"unity://{full_name}"
        payload = self._build_payload(full_name, contract, classifications)
        headers = {"Content-Type": "application/json"}
        if self.workspace_token:
            headers["Authorization"] = f"Bearer {self.workspace_token}"
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds) as c:
                r = c.post("/api/2.1/unity-catalog/tables", json=payload, headers=headers)
                r.raise_for_status()
            return RegistrationResult(target="unity", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="unity", urn=urn, succeeded=False, error=str(exc))

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

    def _build_payload(
        self, full_name: str, contract: Dict[str, Any], classifications: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        expose = (contract.get("exposes") or [{}])[0]
        schema_cols = (expose.get("contract") or {}).get("schema") or []
        return {
            "name": full_name.split(".")[-1],
            "catalog_name": self.catalog_name,
            "schema_name": self.schema_name,
            "table_type": "MANAGED",
            "data_source_format": "DELTA",
            "comment": contract.get("description", ""),
            "columns": [
                {
                    "name": c.get("name"),
                    "type_name": (c.get("type", "STRING") or "STRING").upper(),
                    "comment": c.get("description", ""),
                    "tags": [
                        {
                            "key": "pii",
                            "value": ",".join(classifications.get(c.get("name", ""), [])),
                        }
                    ],
                }
                for c in schema_cols
            ],
        }
