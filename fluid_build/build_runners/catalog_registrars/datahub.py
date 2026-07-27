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
contract's exposes wired in as the product's *assets*.

Entity emission map:

* **Dataset** (``urn:li:dataset:(...)``) — one per expose; carries
  schema, ownership, glossary terms, lineage.
* **DataContract** (``urn:li:dataContract:<product>.<expose>``) — one
  per expose; the per-asset ODCS document lives here as ``rawContract``
  on ``dataContractProperties``. This is DataHub's first-class home
  for ODCS-style contracts; the UI renders it as a dedicated "Data
  Contract" page bound to the dataset.
* **DataProduct** (``urn:li:dataProduct:<contract.id>``) — one per
  contract; lists every expose under ``assets``. Tags travel on the
  native ``globalTags`` aspect. Links to the source FLUID + ODPS
  YAML files travel on ``institutionalMemory`` + ``externalUrl`` so
  the multi-KB documents are *referenced* rather than inlined.
* **Domain** (``urn:li:domain:<id>``) — when the contract declares
  one; both the DataProduct and the Datasets are linked via the
  native ``domains`` aspect.

What we deliberately do **not** do: stuff multi-KB YAML blobs into
``customProperties``. DataHub indexes those values in search and
ships them on every entity GET — both the wrong tradeoff for source
documents that have proper homes elsewhere. The full FLUID + ODPS
specs are reachable via ``institutionalMemory`` (link to source
repo). The full ODCS contract is reachable via the DataContract
entity's ``rawContract`` field.

API shapes: Dataset goes through the legacy snapshot API at
``/entities?action=ingest`` (DataHub's DatasetSnapshot union still
accepts schema + ownership + lineage there). Every other entity
(DataProduct, Domain, DataContract, Tag) goes through the v2
``MetadataChangeProposal`` API at ``/aspects?action=ingestProposal``
because there's no matching Snapshot union for them.

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
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypeVar

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import (
    AssetPayload,
    CatalogPublicationPayload,
    ColumnPayload,
)

from ._http_retry import RetryPolicy, run_with_retry

LOG = logging.getLogger("fluid.acquire.catalog.datahub")

_T = TypeVar("_T")


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to
    *default* on absence or a malformed / non-positive value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


@dataclass
class DataHubRegistrar(CatalogRegistrar):
    target: str = "datahub"
    base_url: str = "https://datahub.test"
    api_token: Optional[str] = None
    timeout_seconds: int = 30
    # Base URL for spec source-of-truth documents. When set, the
    # registrar emits ``institutionalMemory`` links + a
    # ``dataProductProperties.externalUrl`` pointing at
    # ``<spec_source_base_url>/<product_id>/{contract.fluid.yaml, spec.odps.yaml}``
    # so DataHub references the source documents instead of inlining
    # multi-KB YAML blobs in ``customProperties``. Set via env var
    # ``FLUID_CATALOG_DATAHUB_SPEC_BASE_URL``.
    spec_source_base_url: Optional[str] = None
    # Transient-failure resilience. Every GMS call (snapshot ingest,
    # MCP ingestProposal, soft-delete) is wrapped in bounded
    # exponential-backoff retry so a rolling restart / 503 / 429 blip
    # self-heals instead of failing the publish. Defaults mirror
    # DataHub's own ``DatahubRestEmitter`` (4 total attempts, retry on
    # 429 + 5xx). ``retry_max_attempts`` honours
    # ``FLUID_CATALOG_DATAHUB_MAX_RETRIES`` at instantiation so every
    # construction path (factory, ``build_registrar``, direct) picks it
    # up; set it to ``1`` to disable retries. See ``_http_retry``.
    retry_max_attempts: int = field(
        default_factory=lambda: _env_int("FLUID_CATALOG_DATAHUB_MAX_RETRIES", 4)
    )
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0
    # Capability cache: set on first publish. ``None`` means "untested";
    # ``True`` means the server accepted structured-property definitions
    # at bootstrap; ``False`` means the server is too old and we should
    # fall back to customProperties for FLUID classification.
    _structured_properties_supported: Optional[bool] = None

    # ── Canonical entry point ─────────────────────────────────────────

    def register_payload(self, payload: CatalogPublicationPayload) -> RegistrationResult:
        """Publish *payload* to DataHub end-to-end.

        Order of HTTP calls (each idempotent on its own):

        1. **Domain** (MCP) — when ``payload.product.domain`` is set.
        2. **Per asset**:
           a. Dataset (snapshot) — schema, ownership, lineage, small
              typed FLUID property tags.
           b. Dataset → Domain (MCP) when domain is set.
           c. DataContract (MCP) — ``urn:li:dataContract:<product>.<expose>``
              carrying the full ODCS YAML as ``rawContract``. Renders
              in the UI as the dataset's Data Contract tab.
        3. **DataProduct** (MCP) — lists every asset under ``assets``,
           plus ``globalTags`` (native tag aspect) and
           ``institutionalMemory`` links pointing at the source
           FLUID + ODPS YAML files in the contract repo (when
           ``spec_source_base_url`` is configured).
        4. **DataProduct → Domain** (MCP) when domain is set.
        """
        product_id = payload.product.product_id
        product_urn = self._product_urn(product_id)
        domain_name = payload.product.domain
        domain_urn = self._domain_urn(domain_name) if domain_name else None
        dataset_urns: List[str] = []
        contract_urns: List[str] = []
        assertion_urns: List[str] = []

        try:
            # One-shot per-registrar bootstrap of FLUID structured-property
            # definitions. Idempotent; capability-detected so older
            # DataHub OSS releases (no structuredProperty entity model)
            # gracefully fall back to the customProperties path.
            self._bootstrap_structured_properties_once()
            if domain_urn:
                self._publish_domain(domain_name, domain_urn)
            for asset in payload.assets:
                dataset_urn = self._dataset_urn(product_id, asset)
                self._publish_dataset(payload, asset, dataset_urn, domain_urn)
                dataset_urns.append(dataset_urn)
                if asset.odcs_yaml:
                    # Emit Assertion entities derived from ODCS field
                    # rules (required / unique / library notNull /
                    # library unique) BEFORE the DataContract MCP so
                    # the contract can reference live URNs in its
                    # dataQuality bucket.
                    asset_assertions = self._publish_assertions_for_asset(
                        product_id=product_id, asset=asset, dataset_urn=dataset_urn
                    )
                    assertion_urns.extend(u for u, _ in asset_assertions)
                    contract_urn = self._publish_data_contract(
                        product_id,
                        asset,
                        dataset_urn,
                        assertions=asset_assertions,
                    )
                    contract_urns.append(contract_urn)
            self._publish_dataproduct(payload, product_urn, domain_urn)
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(
                target="datahub",
                urn=product_urn,
                succeeded=False,
                error=str(exc),
                metadata={
                    "dataset_urns": dataset_urns,
                    "contract_urns": contract_urns,
                    "assertion_urns": assertion_urns,
                },
            )

        return RegistrationResult(
            target="datahub",
            urn=product_urn,
            succeeded=True,
            metadata={
                "dataset_urns": dataset_urns,
                "contract_urns": contract_urns,
                "assertion_urns": assertion_urns,
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
        domain_urn = self._domain_urn(scoped.product.domain) if scoped.product.domain else None

        try:
            if domain_urn:
                self._publish_domain(scoped.product.domain, domain_urn)
            dataset_urn = self._dataset_urn(scoped.product.product_id, scoped.assets[0])
            self._publish_dataset(scoped, scoped.assets[0], dataset_urn, domain_urn)
            contract_urn: Optional[str] = None
            if scoped.assets[0].odcs_yaml:
                contract_urn = self._publish_data_contract(
                    scoped.product.product_id, scoped.assets[0], dataset_urn
                )
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
            metadata={
                "dataset_urn": dataset_urn,
                "contract_urn": contract_urn,
            },
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
        # Typed FLUID classification via structuredProperties when the
        # server supports them. On older servers this no-ops and the
        # values stay in customProperties as the back-compat fallback.
        self._maybe_publish_structured_properties(
            entity_type="dataset", entity_urn=dataset_urn, payload=payload
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
        # First-class tag aspect rather than a customProperties map.
        # Tag entities are auto-created on first reference by DataHub
        # so we don't need a separate Tag MCP per tag.
        if payload.product.tags:
            self._post_mcp(
                entity_type="dataProduct",
                entity_urn=product_urn,
                aspect_name="globalTags",
                aspect={"tags": [{"tag": self._tag_urn(t)} for t in payload.product.tags]},
            )
        # institutionalMemory links replace the YAML blobs that used
        # to live in customProperties. Only emitted when the operator
        # configures a source-of-truth base URL — otherwise the field
        # is left absent (better than a dangling link).
        memory = self._build_institutional_memory(payload)
        if memory:
            self._post_mcp(
                entity_type="dataProduct",
                entity_urn=product_urn,
                aspect_name="institutionalMemory",
                aspect=memory,
            )
        # Typed FLUID classification via structuredProperties when the
        # server supports them — same pattern as the dataset side.
        self._maybe_publish_structured_properties(
            entity_type="dataProduct", entity_urn=product_urn, payload=payload
        )

    def _publish_data_contract(
        self,
        product_id: str,
        asset: AssetPayload,
        dataset_urn: str,
        assertions: Optional[List[tuple[str, str]]] = None,
    ) -> str:
        """Emit a first-class ``DataContract`` entity for *asset*.

        DataHub's ``dataContractProperties`` aspect is the canonical
        home for ODCS-style contracts: it links the contract to its
        dataset via ``entity`` and carries the raw YAML body on
        ``rawContract``. The UI renders this as a Data Contract page
        attached to the dataset, far better UX than a multi-KB string
        crammed into ``customProperties.odcs_contract``.

        When *assertions* is provided, each ``(urn, bucket)`` tuple
        gets routed into the matching DataContract bucket
        (``schema`` / ``freshness`` / ``dataQuality``) — that's how the
        UI knows which Assertion entities the contract enforces.
        ODCS quality rules → Assertion translation lives in
        :mod:`_datahub_assertions`; the caller publishes the Assertion
        MCPs before this DataContract is upserted so the references
        resolve immediately.

        We also stamp ``dataContractStatus.state = ACTIVE`` so the
        contract isn't shown as pending — these contracts represent
        what's published, not draft work.
        """
        contract_urn = self._data_contract_urn(product_id, asset.asset_id)
        # Each DataContract bucket is a list of ``{assertion: <urn>}``
        # entries (the MCP-aspect shape — the GraphQL surface uses
        # ``assertionUrn`` instead). Populate from the per-asset
        # translator output; leave empty when no rules translated.
        buckets: Dict[str, List[Dict[str, Any]]] = {
            "schema": [],
            "freshness": [],
            "dataQuality": [],
        }
        for assertion_urn, bucket_name in assertions or ():
            if bucket_name in buckets:
                buckets[bucket_name].append({"assertion": assertion_urn})

        properties: Dict[str, Any] = {
            "entity": dataset_urn,
            **buckets,
        }
        if asset.odcs_yaml:
            properties["rawContract"] = asset.odcs_yaml
        self._post_mcp(
            entity_type="dataContract",
            entity_urn=contract_urn,
            aspect_name="dataContractProperties",
            aspect=properties,
        )
        self._post_mcp(
            entity_type="dataContract",
            entity_urn=contract_urn,
            aspect_name="dataContractStatus",
            aspect={"state": "ACTIVE"},
        )
        return contract_urn

    # ── Structured-property bootstrap ────────────────────────────────

    def _bootstrap_structured_properties_once(self) -> None:
        """Upsert FLUID structured-property definitions on the server.

        Idempotent (re-PUT with the same body is a no-op) and cached
        per registrar instance via ``_structured_properties_supported``
        so we don't pay the round trip on every publish. Capability-
        detected: if the server returns 4xx (older DataHub without
        the structuredProperty entity model) we mark the feature
        unsupported and the publish path falls back to
        ``customProperties`` for FLUID classification.
        """
        if self._structured_properties_supported is not None:
            return  # already bootstrapped (success or detected-unsupported)
        from ._datahub_structured_properties import (
            ALL_DEFINITIONS,
            structured_property_urn,
        )

        try:
            for definition in ALL_DEFINITIONS:
                qualified = definition["qualifiedName"]
                urn = structured_property_urn(qualified)
                self._post_mcp(
                    entity_type="structuredProperty",
                    entity_urn=urn,
                    aspect_name="propertyDefinition",
                    aspect=definition,
                )
            self._structured_properties_supported = True
            LOG.info("DataHub structured properties bootstrapped (fluid.layer + fluid.productType)")
        except Exception as exc:  # noqa: BLE001 — capability detect, non-fatal
            self._structured_properties_supported = False
            LOG.info(
                "DataHub server doesn't support structured properties — "
                "falling back to customProperties (%s)",
                exc,
            )

    def _maybe_publish_structured_properties(
        self, *, entity_type: str, entity_urn: str, payload: CatalogPublicationPayload
    ) -> None:
        """Attach ``fluid.layer`` + ``fluid.productType`` to *entity_urn*
        when the server supports structured properties. No-op on
        unsupported servers (callers still emit the same values in
        ``customProperties`` for those — see
        ``_fluid_classification_custom_properties``)."""
        if not self._structured_properties_supported:
            return
        from ._datahub_structured_properties import assignment_for

        body = assignment_for(
            layer=payload.product.layer,
            product_type=payload.product.product_type,
        )
        if not body["properties"]:
            return
        self._post_mcp(
            entity_type=entity_type,
            entity_urn=entity_urn,
            aspect_name="structuredProperties",
            aspect=body,
        )

    # ── Assertion translation (ODCS → DataHub) ───────────────────────

    def _publish_assertions_for_asset(
        self, *, product_id: str, asset: AssetPayload, dataset_urn: str
    ) -> List[tuple[str, str]]:
        """Translate the per-asset ODCS quality rules into DataHub
        ``Assertion`` entities and PUT them. Returns ``(urn, bucket)``
        tuples for the caller to thread into the DataContract bundle.

        Failures on individual assertions log + skip — better to land
        a partial set than abort the whole publish over a rule the
        translator doesn't yet understand.
        """
        if not asset.odcs_yaml:
            return []
        try:
            import yaml as _yaml

            odcs = _yaml.safe_load(asset.odcs_yaml)
        except Exception:  # noqa: BLE001
            LOG.debug(
                "ODCS YAML parse failed for assertion translation on asset %s",
                asset.asset_id,
                exc_info=True,
            )
            return []
        if not isinstance(odcs, dict):
            return []

        from ._datahub_assertions import odcs_to_assertions

        emissions = odcs_to_assertions(
            odcs=odcs,
            product_id=product_id,
            expose_id=asset.asset_id,
            dataset_urn=dataset_urn,
        )
        published: List[tuple[str, str]] = []
        audit_stamp = self._audit_stamp()
        for emission in emissions:
            # Stamp lastUpdated on every emission so DataHub orders
            # them by recency; the translator left ``time: 0`` so the
            # registrar can fill the wall clock here.
            info = dict(emission.info)
            info["lastUpdated"] = audit_stamp
            try:
                self._post_mcp(
                    entity_type="assertion",
                    entity_urn=emission.urn,
                    aspect_name="assertionInfo",
                    aspect=info,
                )
                published.append((emission.urn, emission.bucket))
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "DataHub assertion PUT failed for %s (non-fatal): %s",
                    emission.urn,
                    exc,
                )
        return published

    # ── HTTP helpers ──────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _retry_policy(self) -> "RetryPolicy":
        """Build the per-call retry policy from this registrar's config."""
        return RetryPolicy(
            max_attempts=max(1, self.retry_max_attempts),
            base_delay=self.retry_base_delay,
            max_delay=self.retry_max_delay,
        )

    def _with_retry(self, operation: Callable[[], _T], *, description: str) -> _T:
        """Run a GMS HTTP operation under bounded backoff retry.

        Transient failures (429 / 5xx / connection blips) self-heal;
        a non-transient error (4xx, bad payload) re-raises on the first
        attempt. Either way the *original* exception propagates to the
        registrar's ``try/except`` — which turns it into a clean
        ``succeeded=False`` result, never a pipeline crash.
        """
        return run_with_retry(
            operation,
            policy=self._retry_policy(),
            logger=LOG,
            description=description,
        )

    def _post_snapshot(self, envelope: Dict[str, Any]) -> None:
        """POST a legacy Snapshot envelope to ``/entities?action=ingest``.
        Used for Dataset entities — DataHub still accepts the snapshot
        shape on this endpoint but rejects DataProduct / Domain there
        (they have no DataProductSnapshot / DomainSnapshot models)."""
        from fluid_build.util.safe_http import safe_httpx_client

        def _op() -> None:
            with safe_httpx_client(
                base_url=self.base_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.post("/entities?action=ingest", json=envelope, headers=self._headers())
                r.raise_for_status()

        self._with_retry(_op, description="snapshot ingest")

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

        def _op() -> None:
            with safe_httpx_client(
                base_url=self.base_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.post("/aspects?action=ingestProposal", json=payload, headers=self._headers())
                r.raise_for_status()

        self._with_retry(_op, description=f"mcp {entity_type}/{aspect_name}")

    def _post_delete(self, urn: str) -> None:
        from fluid_build.util.safe_http import safe_httpx_client

        def _op() -> None:
            with safe_httpx_client(
                base_url=self.base_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.post("/entities?action=delete", json={"urn": urn}, headers=self._headers())
                r.raise_for_status()

        self._with_retry(_op, description="soft-delete")

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

    @staticmethod
    def _data_contract_urn(product_id: str, expose_id: str) -> str:
        """DataContract URN — ``urn:li:dataContract:<product>.<expose>``.

        Matches the per-asset id shape every other backend uses (DMM
        publishes ODCS at ``/api/datacontracts/{product_id}.{expose_id}``
        with the same id) so a navigator can move between catalogs
        without an ID translation table.
        """
        return f"urn:li:dataContract:{product_id}.{expose_id}"

    @staticmethod
    def _tag_urn(name: str) -> str:
        return f"urn:li:tag:{name}"

    def _spec_url(self, product_id: str, filename: str) -> Optional[str]:
        """Build a source-of-truth URL for *filename* under *product_id*.

        Returns ``None`` when ``spec_source_base_url`` isn't configured
        so callers can skip the corresponding ``institutionalMemory``
        element rather than emitting a dangling link.
        """
        if not self.spec_source_base_url:
            return None
        return f"{self.spec_source_base_url.rstrip('/')}/{product_id}/{filename}"

    def _build_institutional_memory(
        self, payload: CatalogPublicationPayload
    ) -> Optional[Dict[str, Any]]:
        """Build the DataProduct ``institutionalMemory`` aspect linking
        to the source FLUID + ODPS YAML documents. Returns ``None``
        when no URLs are configured — better to omit the aspect than
        to emit broken links."""
        product_id = payload.product.product_id
        elements: List[Dict[str, Any]] = []
        for filename, label in (
            ("contract.fluid.yaml", "FLUID source contract"),
            ("spec.odps.yaml", "ODPS data product spec"),
        ):
            url = self._spec_url(product_id, filename)
            if not url:
                continue
            elements.append(
                {
                    "url": url,
                    "description": label,
                    "createStamp": self._audit_stamp(),
                }
            )
        if not elements:
            return None
        return {"elements": elements}

    def _build_dataset_institutional_memory(
        self, payload: CatalogPublicationPayload, asset: AssetPayload
    ) -> Optional[Dict[str, Any]]:
        """Build the Dataset's ``institutionalMemory`` link to the
        per-asset ODCS YAML.

        Rationale: DataHub OSS exposes the ``DataContract`` entity
        we emit, but its ``rawContract`` field (where we stash the
        ODCS YAML) is **not** in the OSS GraphQL schema — only Acryl
        Cloud renders that field. Without this link, an OSS operator
        navigating to a dataset has no clickable path to read the
        contract document. The link lands in the dataset's
        Documentation → Links section and is fully OSS-renderable.

        Skipped when ``spec_source_base_url`` isn't configured (we
        don't fabricate a URL that 404s).
        """
        url = self._spec_url(payload.product.product_id, f"{asset.asset_id}.odcs.yaml")
        if not url:
            return None
        return {
            "elements": [
                {
                    "url": url,
                    "description": (f"ODCS data contract for output port '{asset.asset_id}'"),
                    "createStamp": self._audit_stamp(),
                }
            ]
        }

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

        # Dataset-level custom properties carry only the small typed
        # FLUID classification chips. The ODCS contract for this
        # dataset lives on a first-class ``DataContract`` entity
        # (see :meth:`_publish_data_contract`) — NOT here. Domain
        # is published via the native ``domains`` aspect, not as a
        # custom string. Dot-notation keys mirror DataHub's own
        # convention for ingestion-source-tagged properties.
        custom_properties: Dict[str, str] = {}
        if product.layer:
            custom_properties["fluid.layer"] = product.layer
        if product.product_type:
            custom_properties["fluid.productType"] = product.product_type

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
                    "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": "{}"}},
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
            aspects.append({"com.linkedin.dataset.UpstreamLineage": {"upstreams": upstreams}})

        # institutionalMemory link to the ODCS YAML — OSS-renderable
        # workaround for DataHub OSS not exposing DataContract.rawContract
        # in GraphQL. The link appears in the dataset's
        # Documentation → Links section.
        memory = self._build_dataset_institutional_memory(payload, asset)
        if memory:
            aspects.append({"com.linkedin.common.InstitutionalMemory": memory})

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

    def _build_dataproduct_properties(self, payload: CatalogPublicationPayload) -> Dict[str, Any]:
        """Build the ``dataProductProperties`` aspect.

        Carries only small typed FLUID metadata in ``customProperties``
        (layer, product type, version). Domain is published via the
        native ``domains`` aspect; tags via ``globalTags``; and the
        source FLUID + ODPS YAML documents are *linked* via
        ``institutionalMemory`` and ``externalUrl`` rather than
        inlined here — multi-KB YAML in customProperties bloats every
        entity GET and pollutes search.

        ``assets`` lists every expose of the contract so the
        DataProduct page's Assets tab renders the full backing.
        """
        product = payload.product
        custom_properties: Dict[str, str] = {}
        if product.layer:
            custom_properties["fluid.layer"] = product.layer
        if product.product_type:
            custom_properties["fluid.productType"] = product.product_type
        if product.version:
            custom_properties["fluid.version"] = product.version

        assets = [
            {"destinationUrn": self._dataset_urn(product.product_id, asset)}
            for asset in payload.assets
            if asset.asset_id
        ]

        body: Dict[str, Any] = {
            "name": product.name or product.product_id,
            "description": product.description,
            "customProperties": custom_properties,
            "assets": assets,
        }
        # ``externalUrl`` points at the primary source-of-truth
        # document — the FLUID contract — when an operator has
        # configured a base URL. DataHub's UI shows this as an
        # "External URL" link in the product header.
        ext_url = self._spec_url(product.product_id, "contract.fluid.yaml")
        if ext_url:
            body["externalUrl"] = ext_url
        return body


# ── Plugin registration ─────────────────────────────────────────────────
#
# Self-register so ``fluid publish --target datahub`` and contract
# ``properties.catalog.register: [datahub]`` both resolve without edits
# to ``providers/catalogs/__init__.py`` or ``config_manager.py``.

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    CatalogNotConfiguredError,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402


def _require_datahub_endpoint(config: dict) -> str:
    """The endpoint, or a refusal. See the note in the OpenMetadata factory:
    ``https://datahub.test`` exists only in this module's HTTP-mocked tests,
    and defaulting to it turned "not configured" into a DNS failure."""
    endpoint = pick_endpoint(config)
    if not endpoint:
        raise CatalogNotConfiguredError("datahub")
    return endpoint


def _build_datahub_registrar(config: dict) -> DataHubRegistrar:
    import os

    return DataHubRegistrar(
        base_url=_require_datahub_endpoint(config),
        api_token=pick_token(config),
        timeout_seconds=pick_int(config, "timeout", 30),
        spec_source_base_url=(
            config.get("spec_source_base_url")
            or os.environ.get("FLUID_CATALOG_DATAHUB_SPEC_BASE_URL")
        ),
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
