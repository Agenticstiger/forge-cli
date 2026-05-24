# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DataHub catalog registrar.

A FLUID contract IS a data product, so the primary entity we emit is a
DataHub ``DataProduct`` (``urn:li:dataProduct:<contract.id>``) with the
contract's exposes wired in as the product's *assets*. Each expose
becomes a Dataset entity (``urn:li:dataset:(urn:li:dataPlatform:...,
<contract.id>.<expose.exposeId>,PROD)``) carrying schema, ownership,
glossary terms, lineage edges, and the per-asset ODCS contract.
When the contract declares a domain we also emit a Domain entity and
link both the DataProduct and the Datasets to it.

DataHub historically accepts two ingestion shapes. DataProduct +
Domain are only available via the v2 ``MetadataChangeProposal`` API
at ``/aspects?action=ingestProposal``; Dataset still goes through the
legacy snapshot API at ``/entities?action=ingest``. This module uses
both.

The translator reads exclusively from
:class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`.
The legacy ``register(product_id, expose_id, contract, classifications)``
entry point builds a payload from its args and delegates so existing
callers keep working.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import (
    AssetPayload,
    CatalogPublicationPayload,
    ColumnPayload,
)

LOG = logging.getLogger("fluid.acquire.catalog.datahub")


@dataclass
class DataHubRegistrar(CatalogRegistrar):
    target: str = "datahub"
    base_url: str = "https://datahub.test"
    api_token: Optional[str] = None
    timeout_seconds: int = 30

    # ── Canonical entry point ─────────────────────────────────────────

    def register_payload(
        self, payload: CatalogPublicationPayload
    ) -> RegistrationResult:
        """Publish *payload* to DataHub end-to-end.

        Order of HTTP calls (each idempotent on its own):

        1. Domain (MCP) — when ``payload.product.domain`` is set.
        2. For every asset:
           a. Dataset (snapshot) carrying schema, ownership, lineage,
              per-asset ODCS, and FLUID custom properties.
           b. Dataset → Domain (MCP) when domain is set.
        3. DataProduct (MCP) listing every asset under ``assets``,
           carrying the source FLUID YAML + ODPS spec as custom
           properties.
        4. DataProduct → Domain (MCP) when domain is set.
        """
        product_id = payload.product.product_id
        product_urn = self._product_urn(product_id)
        domain_name = payload.product.domain
        domain_urn = self._domain_urn(domain_name) if domain_name else None
        dataset_urns: List[str] = []

        try:
            if domain_urn:
                self._publish_domain(domain_name, domain_urn)
            for asset in payload.assets:
                dataset_urn = self._dataset_urn(product_id, asset)
                self._publish_dataset(payload, asset, dataset_urn, domain_urn)
                dataset_urns.append(dataset_urn)
            self._publish_dataproduct(payload, product_urn, domain_urn)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="datahub",
                urn=product_urn,
                succeeded=False,
                error=str(exc),
                metadata={"dataset_urns": dataset_urns},
            )

        return RegistrationResult(
            target="datahub",
            urn=product_urn,
            succeeded=True,
            metadata={
                "dataset_urns": dataset_urns,
                # Back-compat singular: the legacy ``register`` path
                # surfaced a single ``dataset_urn`` in metadata; preserve
                # it (first asset) for callers still reading that key.
                "dataset_urn": dataset_urns[0] if dataset_urns else "",
            },
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

        Builds a payload from the contract dict, then *scopes* it so
        only the asset matching ``expose_id`` is published as a
        Dataset — but the DataProduct still references every expose
        of the contract via its ``assets`` list. That preserves the
        historical iteration-driven call shape (one ``register`` per
        expose) while sharing every translator code path with the
        canonical entry point.
        """
        payload = CatalogPublicationPayload.from_contract(contract, classifications)
        scoped_assets = tuple(a for a in payload.assets if a.asset_id == expose_id)
        if not scoped_assets:
            # The orchestrator passed an expose_id the contract didn't
            # declare. Return an honest failure rather than silently
            # publishing nothing.
            return RegistrationResult(
                target="datahub",
                urn=self._product_urn(product_id),
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        scoped = dataclasses.replace(payload, assets=scoped_assets)

        product_urn = self._product_urn(scoped.product.product_id)
        domain_urn = (
            self._domain_urn(scoped.product.domain) if scoped.product.domain else None
        )

        try:
            if domain_urn:
                self._publish_domain(scoped.product.domain, domain_urn)
            dataset_urn = self._dataset_urn(scoped.product.product_id, scoped.assets[0])
            self._publish_dataset(scoped, scoped.assets[0], dataset_urn, domain_urn)
            # DataProduct payload uses the FULL ``payload`` (all
            # assets), not the scoped one — the product entity has to
            # describe the whole thing regardless of which expose
            # triggered this call.
            self._publish_dataproduct(payload, product_urn, domain_urn)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="datahub",
                urn=product_urn,
                succeeded=False,
                error=str(exc),
                metadata={"dataset_urn": dataset_urn},
            )

        return RegistrationResult(
            target="datahub",
            urn=product_urn,
            succeeded=True,
            metadata={"dataset_urn": dataset_urn},
        )

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        """Soft-delete the DataProduct and its dataset asset for *expose_id*.

        DataHub's soft-delete sets ``Status.removed=true`` which hides
        the entity from search / UI without wiping the underlying
        records — a hard delete needs ``datahub delete --hard``.
        """
        product_urn = self._product_urn(product_id)
        # Recompose the dataset URN from name parts: ``_dataset_urn``
        # builds them from an AssetPayload, so synthesise a minimal
        # one for delete. Platform defaults to ``forge`` to match the
        # historical behaviour of ``_urn(product_id, expose_id, {})``.
        synthetic_asset = AssetPayload(asset_id=expose_id, platform="forge")
        dataset_urn = self._dataset_urn(product_id, synthetic_asset)
        last_err: Optional[str] = None
        for urn in (dataset_urn, product_urn):
            try:
                self._post_delete(urn)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
        if last_err is not None:
            return RegistrationResult(
                target="datahub", urn=product_urn, succeeded=False, error=last_err
            )
        return RegistrationResult(target="datahub", urn=product_urn, succeeded=True)

    # ── Phase implementations ─────────────────────────────────────────

    def _publish_domain(self, domain_name: str, domain_urn: str) -> None:
        self._post_mcp(
            entity_type="domain",
            entity_urn=domain_urn,
            aspect_name="domainProperties",
            aspect={
                "name": domain_name,
                "description": f"FLUID domain: {domain_name}",
            },
        )

    def _publish_dataset(
        self,
        payload: CatalogPublicationPayload,
        asset: AssetPayload,
        dataset_urn: str,
        domain_urn: Optional[str],
    ) -> None:
        envelope = self._build_dataset_envelope(payload, asset, dataset_urn)
        self._post_snapshot(envelope)
        if domain_urn:
            # Domains aspect goes via MCP, not the snapshot — DataHub's
            # DatasetSnapshot union doesn't include it.
            self._post_mcp(
                entity_type="dataset",
                entity_urn=dataset_urn,
                aspect_name="domains",
                aspect={"domains": [domain_urn]},
            )

    def _publish_dataproduct(
        self,
        payload: CatalogPublicationPayload,
        product_urn: str,
        domain_urn: Optional[str],
    ) -> None:
        self._post_mcp(
            entity_type="dataProduct",
            entity_urn=product_urn,
            aspect_name="dataProductProperties",
            aspect=self._build_dataproduct_properties(payload),
        )
        if domain_urn:
            self._post_mcp(
                entity_type="dataProduct",
                entity_urn=product_urn,
                aspect_name="domains",
                aspect={"domains": [domain_urn]},
            )

    # ── HTTP helpers ──────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _post_snapshot(self, envelope: Dict[str, Any]) -> None:
        """POST a legacy Snapshot envelope to ``/entities?action=ingest``.
        Used for Dataset entities — DataHub still accepts the snapshot
        shape on this endpoint but rejects DataProduct / Domain there
        (they have no DataProductSnapshot / DomainSnapshot models)."""
        from fluid_build.util.safe_http import safe_httpx_client

        with safe_httpx_client(
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.post("/entities?action=ingest", json=envelope, headers=self._headers())
            r.raise_for_status()

    def _post_mcp(
        self,
        *,
        entity_type: str,
        entity_urn: str,
        aspect_name: str,
        aspect: Dict[str, Any],
    ) -> None:
        """POST a MetadataChangeProposal to ``/aspects?action=ingestProposal``.

        DataHub's modern entities (DataProduct, Domain, Tag, GlossaryTerm,
        …) only ingest via MCP — there's no Snapshot variant. The body
        wraps the aspect payload as a JSON-serialised string inside a
        ``GenericAspect``; this is DataHub's own envelope shape, not
        an artefact of this client.
        """
        from fluid_build.util.safe_http import safe_httpx_client

        payload = {
            "proposal": {
                "entityType": entity_type,
                "entityUrn": entity_urn,
                "changeType": "UPSERT",
                "aspectName": aspect_name,
                "aspect": {
                    "contentType": "application/json",
                    "value": json.dumps(aspect),
                },
            }
        }
        with safe_httpx_client(
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.post(
                "/aspects?action=ingestProposal", json=payload, headers=self._headers()
            )
            r.raise_for_status()

    def _post_delete(self, urn: str) -> None:
        from fluid_build.util.safe_http import safe_httpx_client

        with safe_httpx_client(
            base_url=self.base_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.post(
                "/entities?action=delete", json={"urn": urn}, headers=self._headers()
            )
            r.raise_for_status()

    # ── URN builders ──────────────────────────────────────────────────

    @staticmethod
    def _dataset_urn(product_id: str, asset: AssetPayload) -> str:
        """Dataset URN — one per expose. The asset_id is the
        canonical expose identifier coming from
        ``contract.exposes[].exposeId``."""
        return (
            f"urn:li:dataset:(urn:li:dataPlatform:{asset.platform},"
            f"{product_id}.{asset.asset_id},PROD)"
        )

    @staticmethod
    def _product_urn(product_id: str) -> str:
        """DataProduct URN — one per contract. Uses the FLUID
        ``contract.id`` directly so navigating between FLUID and
        DataHub doesn't require an ID translation table."""
        return f"urn:li:dataProduct:{product_id}"

    @staticmethod
    def _domain_urn(domain_name: str) -> str:
        """Domain URN — one per ``contract.domain`` value. We pass the
        domain name through verbatim (after stripping) so two contracts
        in the same domain land on the same URN."""
        return f"urn:li:domain:{domain_name.strip()}"

    # ── Aspect builders — payload-driven ──────────────────────────────

    @staticmethod
    def _audit_stamp() -> Dict[str, Any]:
        return {"time": int(time.time() * 1000), "actor": "urn:li:corpuser:datahub"}

    @staticmethod
    def _schema_field_type(native_type: str) -> Dict[str, Any]:
        """Map ``native_type`` (raw SQL/Avro/Parquet name) to DataHub's
        required ``SchemaFieldDataType`` union. Best-effort: anything
        we don't recognise falls back to ``StringType`` (matches the
        DataHub UI's own behaviour for unknown native types)."""
        n = (native_type or "").strip().lower()
        if any(s in n for s in ("int", "bigint", "smallint", "tinyint", "long")):
            return {"type": {"com.linkedin.schema.NumberType": {}}}
        if any(s in n for s in ("float", "double", "decimal", "numeric", "real")):
            return {"type": {"com.linkedin.schema.NumberType": {}}}
        if "bool" in n:
            return {"type": {"com.linkedin.schema.BooleanType": {}}}
        if any(s in n for s in ("date", "time", "timestamp")):
            return {"type": {"com.linkedin.schema.DateType": {}}}
        if any(s in n for s in ("bytes", "binary", "blob")):
            return {"type": {"com.linkedin.schema.BytesType": {}}}
        return {"type": {"com.linkedin.schema.StringType": {}}}

    def _build_dataset_envelope(
        self,
        payload: CatalogPublicationPayload,
        asset: AssetPayload,
        dataset_urn: str,
    ) -> Dict[str, Any]:
        """Build the DatasetSnapshot envelope from a single asset payload."""
        audit_stamp = self._audit_stamp()
        product = payload.product
        owner_team = product.owner.team if product.owner else "unknown"

        def _schema_field(col: ColumnPayload) -> Dict[str, Any]:
            field: Dict[str, Any] = {
                "fieldPath": col.name,
                "type": self._schema_field_type(col.native_type),
                "nativeDataType": col.native_type,
                "description": col.description,
            }
            terms = payload.classifications.get(col.name) or ()
            if terms:
                field["glossaryTerms"] = {
                    "terms": [{"urn": f"urn:li:glossaryTerm:{t}"} for t in terms],
                    "auditStamp": audit_stamp,
                }
            return field

        # Dataset-level custom properties: FLUID classification chips +
        # the per-asset ODCS contract. The ODCS YAML is the same
        # payload DMM PUTs to ``/api/datacontracts/{product_id}.{expose_id}``;
        # DataHub has no separate contract surface so we inline it.
        custom_properties: Dict[str, str] = {}
        if product.layer:
            custom_properties["fluid_layer"] = product.layer
        if product.product_type:
            custom_properties["fluid_product_type"] = product.product_type
        if product.domain:
            custom_properties["fluid_domain"] = product.domain
        if asset.odcs_yaml:
            custom_properties["odcs_contract"] = asset.odcs_yaml

        aspects: List[Dict[str, Any]] = [
            {
                "com.linkedin.dataset.DatasetProperties": {
                    "name": asset.asset_id,
                    "description": product.description,
                    "tags": list(product.tags),
                    "customProperties": custom_properties,
                }
            },
            {
                "com.linkedin.common.Ownership": {
                    "owners": [
                        {
                            "owner": f"urn:li:corpGroup:{owner_team}",
                            "type": "DATAOWNER",
                        }
                    ],
                    "lastModified": audit_stamp,
                }
            },
            {
                "com.linkedin.schema.SchemaMetadata": {
                    "schemaName": asset.asset_id,
                    "platform": f"urn:li:dataPlatform:{asset.platform}",
                    "version": 0,
                    "hash": "",
                    "platformSchema": {
                        "com.linkedin.schema.OtherSchema": {"rawSchema": "{}"}
                    },
                    "fields": [_schema_field(c) for c in asset.schema],
                }
            },
        ]

        # Lineage from the payload — translates the canonical
        # ``upstreams`` list into DataHub's ``UpstreamLineage`` union.
        if asset.upstreams:
            upstreams = [
                {
                    "dataset": (
                        f"urn:li:dataset:(urn:li:dataPlatform:"
                        f"{edge.upstream_platform or asset.platform},"
                        f"{edge.upstream_product_id}.{edge.upstream_expose_id},PROD)"
                    ),
                    "type": edge.transformation_type,
                    "auditStamp": audit_stamp,
                }
                for edge in asset.upstreams
            ]
            aspects.append(
                {"com.linkedin.dataset.UpstreamLineage": {"upstreams": upstreams}}
            )

        return {
            "entity": {
                "value": {
                    "com.linkedin.metadata.snapshot.DatasetSnapshot": {
                        "urn": dataset_urn,
                        "aspects": aspects,
                    }
                }
            }
        }

    def _build_dataproduct_properties(
        self, payload: CatalogPublicationPayload
    ) -> Dict[str, Any]:
        """Build the DataProductProperties aspect.

        ``customProperties`` carries the FLUID-native classification
        AND the source FLUID + ODPS specs — mirroring how DMM
        distributes the same data across
        ``/api/dataproducts/{id}`` (ODPS body) and the
        ``contract.fluid.yaml`` file living alongside it. ``assets``
        lists every expose of the contract so the DataProduct page's
        Assets tab renders the full backing.
        """
        product = payload.product
        custom_properties: Dict[str, str] = {}
        if product.layer:
            custom_properties["fluid_layer"] = product.layer
        if product.product_type:
            custom_properties["fluid_product_type"] = product.product_type
        if product.domain:
            custom_properties["fluid_domain"] = product.domain
        if product.version:
            custom_properties["fluid_version"] = product.version
        for tag in product.tags:
            custom_properties.setdefault(f"fluid_tag.{tag}", "true")
        if payload.specs.fluid_yaml:
            custom_properties["fluid_contract"] = payload.specs.fluid_yaml
        if payload.specs.odps_yaml:
            custom_properties["odps_spec"] = payload.specs.odps_yaml

        assets = [
            {"destinationUrn": self._dataset_urn(product.product_id, asset)}
            for asset in payload.assets
            if asset.asset_id
        ]

        return {
            "name": product.name or product.product_id,
            "description": product.description,
            "customProperties": custom_properties,
            "assets": assets,
        }


# ── Plugin registration ─────────────────────────────────────────────────
#
# Self-register so ``fluid publish --target datahub`` and contract
# ``properties.catalog.register: [datahub]`` both resolve without edits
# to ``providers/catalogs/__init__.py`` or ``config_manager.py``.

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402


def _build_datahub_registrar(config: dict) -> DataHubRegistrar:
    return DataHubRegistrar(
        base_url=pick_endpoint(config, default="https://datahub.test"),
        api_token=pick_token(config),
        timeout_seconds=pick_int(config, "timeout", 30),
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="datahub",
        registrar_factory=_build_datahub_registrar,
        env_vars={
            "endpoint": (
                "FLUID_CATALOG_DATAHUB_URL",
                "DATAHUB_GMS_URL",
                "DATAHUB_GMS_HOST",
                "DATAHUB_SERVER",
            ),
            "api_token": (
                "FLUID_CATALOG_DATAHUB_TOKEN",
                "DATAHUB_GMS_TOKEN",
                "DATAHUB_TOKEN",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.DATA_PRODUCT,
                CatalogCapability.DOMAIN,
                CatalogCapability.LINEAGE,
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
                CatalogCapability.CUSTOM_PROPERTIES,
                CatalogCapability.GLOSSARY_TERMS,
                CatalogCapability.OWNERSHIP,
            }
        ),
        description="LinkedIn DataHub (Acryl) via the GMS REST API",
    )
)
