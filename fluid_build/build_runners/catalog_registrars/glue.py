# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""AWS Glue Catalog registrar.

Talks directly to the Glue HTTP API
(``https://glue.<region>.amazonaws.com``) — no boto3 dependency in
this layer; production callers wire credentials via env / SigV4 if
the target is real AWS, and tests inject ``base_url_override`` for
respx-mocked endpoints.

Spec attachments use Glue's ``TableInput.Parameters`` map. It's
the same canonical attachment shape DataHub uses for
``customProperties`` and Unity uses for ``properties``: a flat
``map<string, string>`` Glue preserves on the table and surfaces in
the AWS console under "Table properties".
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

LOG = logging.getLogger("fluid.acquire.catalog.glue")


@dataclass
class GlueCatalogRegistrar(CatalogRegistrar):
    target: str = "glue"
    region: str = "us-east-1"
    catalog_id: Optional[str] = None
    database_name: str = "forge_bronze"
    timeout_seconds: int = 30
    base_url_override: Optional[str] = None

    def register_payload(self, payload: CatalogPublicationPayload) -> RegistrationResult:
        product_urn = f"glue://{self.database_name}/{payload.product.product_id}"
        last_err: Optional[str] = None
        published: List[str] = []
        for asset in payload.assets:
            urn = f"glue://{self.database_name}/{asset.asset_id}"
            try:
                self._post_create_table(payload, asset)
                published.append(urn)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                break
        if last_err is not None:
            return RegistrationResult(
                target="glue",
                urn=product_urn,
                succeeded=False,
                error=last_err,
                metadata={"published_assets": published},
            )
        return RegistrationResult(
            target="glue",
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
        urn = f"glue://{self.database_name}/{expose_id}"
        if not scoped:
            return RegistrationResult(
                target="glue",
                urn=urn,
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        try:
            self._post_create_table(payload, scoped[0])
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target="glue", urn=urn, succeeded=False, error=str(exc))
        return RegistrationResult(target="glue", urn=urn, succeeded=True)

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        from fluid_build.util.safe_http import safe_httpx_client

        urn = f"glue://{self.database_name}/{expose_id}"
        try:
            with safe_httpx_client(
                base_url=self.base_url_override or f"https://glue.{self.region}.amazonaws.com",
                timeout=float(self.timeout_seconds),
                allow_private=True,
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

    # ── helpers ──────────────────────────────────────────────────────

    def _post_create_table(self, payload: CatalogPublicationPayload, asset: AssetPayload) -> None:
        """Upsert a Glue table — Create on first call, Update on
        ``AlreadyExistsException`` so re-publishing the same FLUID
        contract is idempotent (catalogs are inherently upsert-shaped).
        AWS Glue uses different ``X-Amz-Target`` headers for the two
        operations on the same flat ``POST /`` endpoint."""
        from fluid_build.util.safe_http import safe_httpx_client

        body = self._create_table_payload(payload, asset)
        endpoint = self.base_url_override or f"https://glue.{self.region}.amazonaws.com"
        with safe_httpx_client(
            base_url=endpoint,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.post(
                "/",
                json=body,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSGlue.CreateTable",
                },
            )
            if r.status_code == 400 and "AlreadyExistsException" in (r.text or ""):
                # Fall through to UpdateTable. Glue's UpdateTable expects
                # the same ``DatabaseName`` + ``TableInput`` shape as
                # CreateTable, so we re-POST the body unchanged.
                r = c.post(
                    "/",
                    json=body,
                    headers={
                        "Content-Type": "application/x-amz-json-1.1",
                        "X-Amz-Target": "AWSGlue.UpdateTable",
                    },
                )
            r.raise_for_status()

    def _create_table_payload(
        self, payload: CatalogPublicationPayload, asset: AssetPayload
    ) -> Dict[str, Any]:
        product = payload.product

        # Glue's ``Parameters`` map is the canonical attachment slot.
        # The AWS console renders it under "Table properties" — analysts
        # see ``fluid_layer`` / ``fluid_product_type`` / ``odcs_contract``
        # without leaving the Glue UI.
        parameters: Dict[str, str] = {"classification": "parquet"}
        if product.layer:
            parameters["fluid_layer"] = product.layer
        if product.product_type:
            parameters["fluid_product_type"] = product.product_type
        if product.domain:
            parameters["fluid_domain"] = product.domain
        if product.version:
            parameters["fluid_version"] = product.version
        if asset.odcs_yaml:
            parameters["odcs_contract"] = asset.odcs_yaml
        if payload.specs.fluid_yaml:
            parameters["fluid_contract"] = payload.specs.fluid_yaml
        if payload.specs.odps_yaml:
            parameters["odps_spec"] = payload.specs.odps_yaml
        # Per-column PII annotations preserve the legacy Glue layout
        # (``forge.pii.<col>``) so existing dashboards keep working.
        for col in asset.schema:
            labels = payload.classifications.get(col.name) or ()
            if labels:
                parameters[f"forge.pii.{col.name}"] = ",".join(labels)

        return {
            "CatalogId": self.catalog_id,
            "DatabaseName": self.database_name,
            "TableInput": {
                "Name": asset.asset_id,
                "Description": product.description,
                "TableType": "EXTERNAL_TABLE",
                "Parameters": parameters,
                "StorageDescriptor": {
                    "Columns": [
                        {
                            "Name": c.name,
                            "Type": (c.native_type or "string").lower(),
                            "Comment": c.description,
                        }
                        for c in asset.schema
                    ],
                    "Location": (asset.location or {}).get("path", ""),
                },
            },
        }


# ── Plugin registration ─────────────────────────────────────────────────

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int  # noqa: E402


def _build_glue_registrar(config: dict) -> GlueCatalogRegistrar:
    endpoint = pick_endpoint(config, default="")
    return GlueCatalogRegistrar(
        region=str(config.get("region", "us-east-1")),
        catalog_id=config.get("catalog_id") or config.get("aws_account_id") or None,
        database_name=str(config.get("database_name", "forge_bronze")),
        timeout_seconds=pick_int(config, "timeout", 30),
        base_url_override=endpoint or None,
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="glue",
        aliases=("aws-glue",),
        registrar_factory=_build_glue_registrar,
        env_vars={
            "endpoint": ("FLUID_CATALOG_GLUE_URL",),
            "region": (
                "FLUID_CATALOG_GLUE_REGION",
                "AWS_REGION",
                "AWS_DEFAULT_REGION",
            ),
            "catalog_id": (
                "FLUID_CATALOG_GLUE_CATALOG_ID",
                "AWS_ACCOUNT_ID",
            ),
            "database_name": (
                "FLUID_CATALOG_GLUE_DATABASE",
                "GLUE_DATABASE",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.CUSTOM_PROPERTIES,
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
            }
        ),
        description="AWS Glue Data Catalog via the Glue REST API",
    )
)
