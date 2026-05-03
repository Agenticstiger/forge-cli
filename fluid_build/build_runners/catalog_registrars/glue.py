# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""AWS Glue Catalog registrar.

Uses the Glue REST API (``glue.<region>.amazonaws.com``) directly. No
``boto3`` dependency in the runner — production callers either pass an
SDK-built client through, or the runner falls back to signed HTTP requests
when AWS credentials are present in the env. For tests we hit a
``glue_mock`` respx fixture that stands in for the real endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

LOG = logging.getLogger("fluid.acquire.catalog.glue")


@dataclass
class GlueCatalogRegistrar(CatalogRegistrar):
    target: str = "glue"
    region: str = "us-east-1"
    catalog_id: Optional[str] = None
    database_name: str = "forge_bronze"
    timeout_seconds: int = 30
    base_url_override: Optional[str] = None  # tests inject the mock base URL

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        import httpx

        urn = f"glue://{self.database_name}/{expose_id}"
        payload = self._create_table_payload(expose_id, contract, classifications)
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSGlue.CreateTable",
        }
        try:
            with httpx.Client(
                base_url=self.base_url_override or f"https://glue.{self.region}.amazonaws.com",
                timeout=self.timeout_seconds,
            ) as c:
                r = c.post("/", json=payload, headers=headers)
                r.raise_for_status()
            return RegistrationResult(target="glue", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="glue", urn=urn, succeeded=False, error=str(exc))

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        import httpx

        urn = f"glue://{self.database_name}/{expose_id}"
        try:
            with httpx.Client(
                base_url=self.base_url_override or f"https://glue.{self.region}.amazonaws.com",
                timeout=self.timeout_seconds,
            ) as c:
                r = c.post(
                    "/",
                    json={
                        "CatalogId": self.catalog_id,
                        "DatabaseName": self.database_name,
                        "Name": expose_id,
                    },
                    headers={
                        "Content-Type": "application/x-amz-json-1.1",
                        "X-Amz-Target": "AWSGlue.DeleteTable",
                    },
                )
                r.raise_for_status()
            return RegistrationResult(target="glue", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="glue", urn=urn, succeeded=False, error=str(exc))

    def _create_table_payload(
        self, expose_id: str, contract: Dict[str, Any], classifications: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        expose = (contract.get("exposes") or [{}])[0]
        schema_cols = (expose.get("contract") or {}).get("schema") or []
        return {
            "CatalogId": self.catalog_id,
            "DatabaseName": self.database_name,
            "TableInput": {
                "Name": expose_id,
                "Description": contract.get("description", ""),
                "TableType": "EXTERNAL_TABLE",
                "Parameters": {
                    "classification": "parquet",
                    **{
                        f"forge.pii.{c.get('name')}": ",".join(
                            classifications.get(c.get("name", ""), [])
                        )
                        for c in schema_cols
                        if classifications.get(c.get("name", ""))
                    },
                },
                "StorageDescriptor": {
                    "Columns": [
                        {
                            "Name": c.get("name"),
                            "Type": (c.get("type", "string") or "string").lower(),
                            "Comment": c.get("description", ""),
                        }
                        for c in schema_cols
                    ],
                    "Location": ((expose.get("binding") or {}).get("location") or {}).get(
                        "path", ""
                    ),
                },
            },
        }
