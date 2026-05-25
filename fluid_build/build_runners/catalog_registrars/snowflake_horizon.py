# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Snowflake Horizon registrar.

Talks to Snowflake's HTTP API for catalog operations (Native Apps /
Snowsight). Production paths typically go through
``snowflake-connector-python``; here we use raw HTTP for testability
and to keep the registrar dependency-light.

Spec attachments use Horizon's table ``comment`` field — which
Snowflake renders verbatim in the UI and surfaces via
``INFORMATION_SCHEMA.TABLES``. The canonical FLUID classification +
ODCS contract land at the bottom of the comment as a fenced YAML
block. Horizon doesn't have a free-form properties map at this layer;
the comment is the most analyst-visible place to attach metadata.
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

LOG = logging.getLogger("fluid.acquire.catalog.snowflake_horizon")


@dataclass
class SnowflakeHorizonRegistrar(CatalogRegistrar):
    target: str = "snowflake_horizon"
    account_url: str = "https://acme.snowflakecomputing.com"
    auth_token: Optional[str] = None
    database: str = "FORGE"
    schema: str = "BRONZE"
    timeout_seconds: int = 30

    def register_payload(self, payload: CatalogPublicationPayload) -> RegistrationResult:
        product_urn = (
            f"snowflake://{self.database}.{self.schema}." f"{payload.product.product_id.upper()}"
        )
        last_err: Optional[str] = None
        published: List[str] = []
        for asset in payload.assets:
            urn = f"snowflake://{self.database}.{self.schema}." f"{asset.asset_id.upper()}"
            try:
                self._post_create_table(payload, asset)
                published.append(urn)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                break
        if last_err is not None:
            return RegistrationResult(
                target="snowflake_horizon",
                urn=product_urn,
                succeeded=False,
                error=last_err,
                metadata={"published_assets": published},
            )
        return RegistrationResult(
            target="snowflake_horizon",
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
        urn = f"snowflake://{self.database}.{self.schema}.{expose_id.upper()}"
        if not scoped:
            return RegistrationResult(
                target="snowflake_horizon",
                urn=urn,
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        try:
            self._post_create_table(payload, scoped[0])
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="snowflake_horizon", urn=urn, succeeded=False, error=str(exc)
            )
        return RegistrationResult(target="snowflake_horizon", urn=urn, succeeded=True)

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        from fluid_build.util.safe_http import safe_httpx_client

        urn = f"snowflake://{self.database}.{self.schema}.{expose_id.upper()}"
        try:
            with safe_httpx_client(
                base_url=self.account_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.delete(
                    f"/api/v2/databases/{self.database}/schemas/{self.schema}/tables/"
                    f"{expose_id.upper()}"
                )
                r.raise_for_status()
            return RegistrationResult(target="snowflake_horizon", urn=urn, succeeded=True)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="snowflake_horizon", urn=urn, succeeded=False, error=str(exc)
            )

    # ── helpers ──────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f'Snowflake Token="{self.auth_token}"'
        return headers

    def _post_create_table(self, payload: CatalogPublicationPayload, asset: AssetPayload) -> None:
        from fluid_build.util.safe_http import safe_httpx_client

        body = self._build_payload(payload, asset)
        url = f"/api/v2/databases/{self.database}/schemas/{self.schema}/tables"
        with safe_httpx_client(
            base_url=self.account_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.post(url, json=body, headers=self._headers())
            r.raise_for_status()

    @staticmethod
    def _build_payload(payload: CatalogPublicationPayload, asset: AssetPayload) -> Dict[str, Any]:
        product = payload.product
        # Build a markdown comment that surfaces in the Snowsight UI.
        # YAML fences keep the embedded specs readable both in the UI
        # and via ``SHOW TABLE COMMENT FOR <table>``.
        sections: List[str] = []
        if product.description:
            sections.append(product.description)
        meta_lines: List[str] = []
        if product.layer:
            meta_lines.append(f"- fluid_layer: {product.layer}")
        if product.product_type:
            meta_lines.append(f"- fluid_product_type: {product.product_type}")
        if product.domain:
            meta_lines.append(f"- fluid_domain: {product.domain}")
        if product.version:
            meta_lines.append(f"- fluid_version: {product.version}")
        if meta_lines:
            sections.append("FLUID classification:\n" + "\n".join(meta_lines))
        if asset.odcs_yaml:
            sections.append("ODCS contract (Bitol v3.1):\n```yaml\n" + asset.odcs_yaml + "\n```")
        if payload.specs.fluid_yaml:
            sections.append("FLUID contract:\n```yaml\n" + payload.specs.fluid_yaml + "\n```")
        if payload.specs.odps_yaml:
            sections.append(
                "ODPS data product spec (v1.0.0):\n```yaml\n" + payload.specs.odps_yaml + "\n```"
            )
        comment = "\n\n".join(sections)

        return {
            "name": asset.asset_id.upper(),
            "kind": "TABLE",
            "comment": comment,
            "columns": [
                {
                    "name": (c.name or "").upper(),
                    "datatype": (c.native_type or "VARCHAR").upper(),
                    "comment": c.description,
                    "tags": list(payload.classifications.get(c.name) or ()),
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


def _build_snowflake_horizon_registrar(config: dict) -> SnowflakeHorizonRegistrar:
    return SnowflakeHorizonRegistrar(
        account_url=pick_endpoint(
            config, "account_url", default="https://acme.snowflakecomputing.com"
        ),
        auth_token=pick_token(config),
        database=str(config.get("database", "FORGE")),
        schema=str(config.get("schema", "BRONZE")),
        timeout_seconds=pick_int(config, "timeout", 30),
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="snowflake_horizon",
        aliases=("snowflake-horizon",),
        registrar_factory=_build_snowflake_horizon_registrar,
        env_vars={
            "endpoint": (
                "FLUID_CATALOG_SNOWFLAKE_HORIZON_URL",
                "SNOWFLAKE_ACCOUNT_URL",
            ),
            "api_token": (
                "FLUID_CATALOG_SNOWFLAKE_HORIZON_TOKEN",
                "SNOWFLAKE_AUTH_TOKEN",
            ),
            "database": (
                "FLUID_CATALOG_SNOWFLAKE_HORIZON_DATABASE",
                "SNOWFLAKE_DATABASE",
            ),
            "schema": (
                "FLUID_CATALOG_SNOWFLAKE_HORIZON_SCHEMA",
                "SNOWFLAKE_SCHEMA",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
            }
        ),
        description="Snowflake Horizon (Native Apps / Snowsight) via HTTP API",
    )
)
