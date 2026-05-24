# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DataHub catalog registrar (GMS REST API).

Posts a Dataset entity envelope to ``/entities?action=ingest``. Schema fields
become ``schemaMetadata.fields`` aspects; classifications become
``glossaryTerms``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

LOG = logging.getLogger("fluid.acquire.catalog.datahub")


@dataclass
class DataHubRegistrar(CatalogRegistrar):
    target: str = "datahub"
    base_url: str = "https://datahub.test"
    api_token: Optional[str] = None
    timeout_seconds: int = 30

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        from fluid_build.util.safe_http import safe_httpx_client

        urn = self._urn(product_id, expose_id, contract)
        envelope = self._build_envelope(urn, expose_id, contract, classifications)
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        try:
            with safe_httpx_client(
                base_url=self.base_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.post("/entities?action=ingest", json=envelope, headers=headers)
                r.raise_for_status()
            return RegistrationResult(target="datahub", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="datahub", urn=urn, succeeded=False, error=str(exc))

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        urn = self._urn(product_id, expose_id, {})
        # DataHub soft-deletion via GMS POST /entities?action=delete.
        try:
            from fluid_build.util.safe_http import safe_httpx_client

            with safe_httpx_client(
                base_url=self.base_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.post(
                    "/entities?action=delete",
                    json={"urn": urn},
                    headers={"Authorization": f"Bearer {self.api_token}"} if self.api_token else {},
                )
                r.raise_for_status()
            return RegistrationResult(target="datahub", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="datahub", urn=urn, succeeded=False, error=str(exc))

    @staticmethod
    def _urn(product_id: str, expose_id: str, contract: Dict[str, Any]) -> str:
        platform = (contract.get("exposes") or [{}])[0].get("binding", {}).get(
            "platform"
        ) or "forge"
        return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{product_id}.{expose_id},PROD)"

    @staticmethod
    def _build_envelope(
        urn: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> Dict[str, Any]:
        owner = (contract.get("metadata") or {}).get("owner") or {}
        expose = (contract.get("exposes") or [{}])[0]
        schema_cols = (expose.get("contract") or {}).get("schema") or []
        return {
            "entity": {
                "value": {
                    "com.linkedin.metadata.snapshot.DatasetSnapshot": {
                        "urn": urn,
                        "aspects": [
                            {
                                "com.linkedin.dataset.DatasetProperties": {
                                    "name": expose_id,
                                    "description": contract.get("description", ""),
                                    "tags": contract.get("tags", []),
                                }
                            },
                            {
                                "com.linkedin.common.Ownership": {
                                    "owners": [
                                        {
                                            "owner": f"urn:li:corpGroup:{owner.get('team', 'unknown')}",
                                            "type": "DATAOWNER",
                                        }
                                    ]
                                }
                            },
                            {
                                "com.linkedin.schema.SchemaMetadata": {
                                    "schemaName": expose_id,
                                    "platform": urn.split(",")[0].split(":")[-1],
                                    "version": 0,
                                    "fields": [
                                        {
                                            "fieldPath": c.get("name"),
                                            "nativeDataType": c.get("type", "string"),
                                            "description": c.get("description", ""),
                                            "glossaryTerms": classifications.get(
                                                c.get("name", ""), []
                                            ),
                                        }
                                        for c in schema_cols
                                    ],
                                }
                            },
                        ],
                    }
                }
            }
        }
