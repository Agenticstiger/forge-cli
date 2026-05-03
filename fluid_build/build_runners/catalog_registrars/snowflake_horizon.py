# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Snowflake Horizon registrar.

Uses Snowflake's HTTP API for catalog operations (Native Apps / Snowsight).
Production paths typically go through ``snowflake-connector-python``; here
we use raw HTTP for testability and to keep the registrar dependency-light.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult

LOG = logging.getLogger("fluid.acquire.catalog.snowflake_horizon")


@dataclass
class SnowflakeHorizonRegistrar(CatalogRegistrar):
    target: str = "snowflake_horizon"
    account_url: str = "https://acme.snowflakecomputing.com"
    auth_token: Optional[str] = None
    database: str = "FORGE"
    schema: str = "BRONZE"
    timeout_seconds: int = 30

    def register(
        self,
        product_id: str,
        expose_id: str,
        contract: Dict[str, Any],
        classifications: Dict[str, List[str]],
    ) -> RegistrationResult:
        import httpx

        urn = f"snowflake://{self.database}.{self.schema}.{expose_id.upper()}"
        payload = self._build_payload(expose_id, contract, classifications)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.auth_token:
            headers["Authorization"] = f'Snowflake Token="{self.auth_token}"'
        try:
            with httpx.Client(base_url=self.account_url, timeout=self.timeout_seconds) as c:
                r = c.post(
                    "/api/v2/databases/{db}/schemas/{schema}/tables".format(
                        db=self.database, schema=self.schema
                    ),
                    json=payload,
                    headers=headers,
                )
                r.raise_for_status()
            return RegistrationResult(target="snowflake_horizon", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="snowflake_horizon", urn=urn, succeeded=False, error=str(exc)
            )

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        import httpx

        urn = f"snowflake://{self.database}.{self.schema}.{expose_id.upper()}"
        try:
            with httpx.Client(base_url=self.account_url, timeout=self.timeout_seconds) as c:
                r = c.delete(
                    f"/api/v2/databases/{self.database}/schemas/{self.schema}/tables/{expose_id.upper()}"
                )
                r.raise_for_status()
            return RegistrationResult(target="snowflake_horizon", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="snowflake_horizon", urn=urn, succeeded=False, error=str(exc)
            )

    def _build_payload(
        self, expose_id: str, contract: Dict[str, Any], classifications: Dict[str, List[str]]
    ) -> Dict[str, Any]:
        expose = (contract.get("exposes") or [{}])[0]
        schema_cols = (expose.get("contract") or {}).get("schema") or []
        return {
            "name": expose_id.upper(),
            "kind": "TABLE",
            "comment": contract.get("description", ""),
            "columns": [
                {
                    "name": (c.get("name", "") or "").upper(),
                    "datatype": (c.get("type", "VARCHAR") or "VARCHAR").upper(),
                    "comment": c.get("description", ""),
                    "tags": classifications.get(c.get("name", ""), []),
                }
                for c in schema_cols
            ],
        }
