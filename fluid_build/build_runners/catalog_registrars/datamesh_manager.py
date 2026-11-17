# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""DataMesh Manager catalog registrar.

Translates :class:`~fluid_build.api.catalog_publication.CatalogPublicationPayload`
into Data Mesh Manager's two-endpoint shape:

* ``PUT /api/dataproducts/{id}`` — the data product (one per contract).
  Body is the **rendered ODPS spec** from ``payload.specs.odps_yaml``;
  DMM accepts the ODPS-Bitol v1.0.0 schema directly on this endpoint,
  which is the same wire shape its UI consumes.
* ``PUT /api/datacontracts/{product_id}.{expose_id}`` — one **rendered
  ODCS contract** per asset. Body is the YAML from
  ``asset.odcs_yaml`` parsed back to a dict.

That's the exact division DMM's UI expects (the data-product page +
the linked data-contract sub-page). Same DMM payload-shape the older
async :class:`DataMeshManagerProvider` PUTs — this registrar is the
canonical-layer-driven equivalent so contracts that declare
``catalog.register: [datamesh_manager]`` get the same artifacts.

Configuration:

* ``api_url`` — DMM / Entropy Data REST endpoint. Defaults via env
  ``DMM_API_URL``; final fallback is the public datamesh-manager.com.
* ``api_token`` — bearer token. Defaults via env ``DMM_API_KEY``.

Errors are wrapped in ``RegistrationResult.error`` so a DMM outage
downgrades to "not registered" rather than crashing the run — catalog
auto-registration is observability, not correctness.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import (
    AssetPayload,
    CatalogPublicationPayload,
)

LOG = logging.getLogger("fluid.acquire.catalog.datamesh_manager")

# ``GET /api/dataproducts`` returns at most this many per page (``p`` from 0).
_PRODUCT_PAGE_SIZE = 1000
_MAX_PRODUCT_PAGES = 1000


@dataclass
class DataMeshManagerRegistrar(CatalogRegistrar):
    target: str = "datamesh_manager"
    api_url: Optional[str] = None
    api_token: Optional[str] = None
    timeout_seconds: int = 30
    # When True, POST ``/api/access/{id}/approve`` after PUTting each
    # access agreement. DMM only renders lineage from APPROVED agreements
    # — without this, product-to-product edges stay in 'pending' status
    # and the UI lineage graph is empty for any product with consumes[].
    # Defaults False (production safe); sandboxes opt in via
    # ``DMM_AUTO_APPROVE_ACCESS=true``.
    auto_approve_access: bool = False

    def __post_init__(self) -> None:
        # A blank token is no token: it must not read as "set" to the checks
        # below, nor go out as an empty ``Authorization`` header.
        if self.api_token is not None and not str(self.api_token).strip():
            self.api_token = None
        # Late env-var fallback so env state at register-time wins, for the
        # endpoint and its key together: DMM_API_KEY goes only to the endpoint
        # from DMM_API_URL (or the default). An endpoint handed in may be one a
        # project config in the checkout named (``config_manager`` resolves
        # it, and gives it only a key that same file carries), so it is never
        # paired with the runner's key here.
        if not self.api_url:
            self.api_url = os.environ.get("DMM_API_URL") or "https://api.datamesh-manager.com"
            self.api_token = self.api_token or os.environ.get("DMM_API_KEY")
        # Env-var fallback for auto-approve. Explicit constructor value wins;
        # the env var only fires when not set (i.e. default False).
        if not self.auto_approve_access:
            env_value = os.environ.get("DMM_AUTO_APPROVE_ACCESS", "").strip().lower()
            self.auto_approve_access = env_value in {"1", "true", "yes", "on"}

    def _with_dmm_product_ids(
        self, payload: CatalogPublicationPayload
    ) -> CatalogPublicationPayload:
        """The payload rebuilt from a contract whose product ids are spelled as the
        native provider publishes them (``_ids.with_dmm_product_ids``).

        Entropy ids are case-sensitive, and the native provider has always published
        ``Silver.Sales.Orders`` as ``silver.sales.orders``; this path published it as
        written and named upstreams as written, so the two paths, and a producer and
        its consumers, did not meet. Unchanged ids keep the payload as it is.
        """
        from fluid_build.providers.datamesh_manager._ids import (
            needs_existing_ids,
            with_dmm_product_ids,
        )

        contract = payload.contract or {}
        # An upstream Entropy holds as written (one forge did not publish) is kept as
        # written; reading the list is only worth it when some upstream id would change.
        held: Optional[set] = None
        if needs_existing_ids(contract):
            held = self._read_product_ids()
            if held is None:
                LOG.warning(
                    "Could not list Entropy's data products; every consumes[].productId is "
                    "slugged, so an upstream Entropy holds only as written is not reached."
                )
        normalized = with_dmm_product_ids(contract, held)
        if normalized == dict(contract) and (
            payload.product.product_id == normalized.get("id", payload.product.product_id)
        ):
            return payload
        return CatalogPublicationPayload.from_contract(
            normalized, {col: list(labels) for col, labels in payload.classifications.items()}
        )

    def _endpoint_refusal(self) -> Optional[str]:
        """Why the key must not go to ``api_url``, or ``None``.

        The same rule as the native provider's ``_normalize_api_url``: an http(s) URL,
        and plain http only to a local host unless ``DMM_ALLOW_INSECURE_HTTP`` is set.
        The registrar used to send the key (both ``Authorization`` and ``x-api-key``)
        over plain http to any host.
        """
        from fluid_build.providers.base import ProviderError
        from fluid_build.providers.datamesh_manager.datamesh_manager import (
            DataMeshManagerProvider,
        )

        try:
            DataMeshManagerProvider._normalize_api_url(str(self.api_url or ""))
        except ProviderError as exc:
            return str(exc)
        return None

    # ── Canonical entry point ─────────────────────────────────────────

    def register_payload(self, payload: CatalogPublicationPayload) -> RegistrationResult:
        """Publish *payload* to Data Mesh Manager end-to-end.

        Phases (order matters):

        1. ``PUT /api/sourcesystems/{id}`` per build source — upsert
           SourceSystem entities derived from
           ``builds[].properties.source`` so the data product's
           ``inputPorts[].sourceSystemId`` references resolve. Without
           this, SDP bronze products land in DMM with NO upstream
           lineage edge in the graph view (gap #2 — the operator can't
           see "this product reads from Postgres").
        2. ``PUT /api/datacontracts/{product_id}.{expose_id}`` per asset
           — the per-asset ODCS contract. **Must run before the data
           product PUT** because Entropy's
           ``OpenDataProductStandardUpdateService`` resolves each
           ``outputPorts[].contractId`` to an internal
           ``data_contract`` FK at the moment of the product PUT — if
           the contract doesn't exist yet, the FK stays null and the
           UI shows "Add Data Contract…" on every output port even
           though the wire payload includes the right ``contractId``.
        3. ``PUT /api/dataproducts/{product_id}`` — the ODPS-shaped
           data product. Every input port the ODPS render produced is
           sent: a product consume's port names the upstream contract,
           which is what Entropy draws the product -> product edge from,
           and a build source's port names its SourceSystem through the
           ``sourceSystemId`` custom property.
        4. ``PUT /api/access/{access_id}`` per ``contract.consumes[]``
           entry — one Access Agreement per upstream output port — then
           ``POST /api/access/{id}/cancel`` for each agreement forge made
           earlier for a consume the contract no longer has.

        Failures on the data-product PUT short-circuit; failures on
        individual contracts or access agreements are surfaced via the
        returned ``RegistrationResult.error``. SourceSystem upserts are
        non-fatal (logged at WARNING) — a missing source-system entity
        only degrades the lineage graph, not the product publish.
        """
        from fluid_build.providers.datamesh_manager._ids import dmm_product_id

        product_urn = f"dmm://{dmm_product_id(payload.product.product_id)}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=product_urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
        refusal = self._endpoint_refusal()
        if refusal:
            return RegistrationResult(
                target=self.target, urn=product_urn, succeeded=False, error=refusal
            )
        # Only now, with the key cleared to go to this endpoint: it may read the
        # product list to spell upstream ids.
        payload = self._with_dmm_product_ids(payload)
        product_id = payload.product.product_id
        contract_urns: List[str] = []
        access_urns: List[str] = []
        source_system_urns: List[str] = []
        try:
            # Upsert the owner team FIRST — DMM rejects datacontracts whose
            # ``info.owner`` references a team-id that doesn't exist with a
            # 422. The native ``fluid dmm publish`` path auto-creates teams
            # before publishing; this registrar (Surface B / catalog-target)
            # path was missing the same step, causing all team-referencing
            # publishes via ``fluid publish --target datamesh-manager`` to
            # fail. Non-fatal — a team upsert failure logs WARNING and the
            # downstream PUT will surface the real error.
            self._ensure_owner_team(payload)
            for sys_urn in self._ensure_source_systems(payload):
                source_system_urns.append(sys_urn)
            # What the product named before this publish: the contracts it no longer
            # names are retired once the new product is in.
            from fluid_build.providers.datamesh_manager._publish_flow import (
                output_port_contract_ids,
            )

            existing_product = self._get_json_or_none(
                f"/api/dataproducts/{quote(product_id, safe='')}"
            )
            sent_contract_ids: set[str] = set()
            for asset in payload.assets:
                body = self._render_dmm_data_contract_body(asset)
                if body is None:
                    # No ODCS for this asset: nothing to PUT. Its output port keeps a
                    # contractId only if Entropy already has that contract (below).
                    continue
                # The id the contract names for itself, which is the id its output
                # port names (both resolve through ``expose_contract_id``); a pinned
                # ``exposes[].contract.id`` used to be PUT at the derived id instead.
                contract_id = str(body.get("id") or f"{product_id}.{asset.asset_id}")
                self._put_data_contract(contract_id, asset, body=body)
                sent_contract_ids.add(contract_id)
                contract_urns.append(f"dmm://datacontracts/{contract_id}")
            product_body = self._put_data_product(payload, sent_contract_ids=sent_contract_ids)
            # Pre-flight: enumerate existing DMM DataProduct IDs so we can
            # SKIP access-agreement PUTs whose upstream product doesn't
            # exist yet (DMM otherwise returns a generic ``404`` mid-publish
            # that's hard to read in a CI log). Mirrors the native
            # provider's Gap-9 pre-flight check in
            # ``providers/datamesh_manager/_publish_flow.py``.
            existing_pids = self._existing_product_ids()
            agreements = self._build_access_agreements(payload)
            for agreement in agreements:
                upstream = (agreement.get("provider") or {}).get("dataProductId")
                if existing_pids and upstream and str(upstream) not in existing_pids:
                    import logging

                    logging.getLogger(__name__).warning(
                        "Skipping Access agreement %s: upstream product '%s' "
                        "is not published in DMM yet. Publish it first or "
                        "remove the consume from the contract.",
                        agreement["id"],
                        upstream,
                    )
                    access_urns.append(f"dmm://access/{agreement['id']}?skipped=missing_upstream")
                    continue
                self._put_access_agreement(agreement)
                access_urns.append(f"dmm://access/{agreement['id']}")
            for access_id in self._cancel_stale_access_agreements(
                product_id, {str(a["id"]) for a in agreements}
            ):
                access_urns.append(f"dmm://access/{access_id}?cancelled=stale")
            keep_ids = set(output_port_contract_ids(product_body)) | sent_contract_ids
            for contract_id in self._retire_stale_contracts(product_id, existing_product, keep_ids):
                contract_urns.append(f"dmm://datacontracts/{contract_id}?retired=stale")
        except Exception as exc:  # noqa: BLE001 — both HTTP + transport errors
            return RegistrationResult(
                target=self.target,
                urn=product_urn,
                succeeded=False,
                error=str(exc),
                metadata={
                    "contract_urns": contract_urns,
                    "access_urns": access_urns,
                    "source_system_urns": source_system_urns,
                },
            )
        return RegistrationResult(
            target=self.target,
            urn=product_urn,
            succeeded=True,
            metadata={
                "contract_urns": contract_urns,
                "access_urns": access_urns,
                "source_system_urns": source_system_urns,
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

        Builds a canonical payload from the contract, scopes it to the
        requested expose, and delegates to :meth:`register_payload`.
        Preserves the per-expose URN shape the orchestrator's
        historical iteration relied on (``dmm://<product>/<expose>``).
        """
        from fluid_build.providers.datamesh_manager._ids import dmm_product_id

        # The id register_payload and the native provider publish under.
        product_id = dmm_product_id(product_id)
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
        refusal = self._endpoint_refusal()
        if refusal:
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=refusal)
        payload = self._with_dmm_product_ids(
            CatalogPublicationPayload.from_contract(contract, classifications)
        )
        scoped = tuple(a for a in payload.assets if a.asset_id == expose_id)
        if not scoped:
            return RegistrationResult(
                target=self.target,
                urn=urn,
                succeeded=False,
                error=f"expose_id {expose_id!r} not found in contract {product_id!r}",
            )
        try:
            # PUT the data product (whole, ODPS shape) — same as canonical.
            self._put_data_product(payload)
            # PUT only the scoped asset's contract — preserves historical
            # behaviour where ``register("p", "x", ...)`` PUT only the
            # ``p.x`` contract.
            contract_id = f"{product_id}.{expose_id}"
            self._put_data_contract(contract_id, scoped[0])
        except Exception as exc:  # noqa: BLE001
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=str(exc))
        return RegistrationResult(target=self.target, urn=urn, succeeded=True)

    def unregister(self, product_id: str, expose_id: str) -> RegistrationResult:
        from fluid_build.providers.datamesh_manager._ids import dmm_product_id

        product_id = dmm_product_id(product_id)
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target, urn=urn, succeeded=False, error="DMM_API_KEY not set"
            )
        refusal = self._endpoint_refusal()
        if refusal:
            return RegistrationResult(target=self.target, urn=urn, succeeded=False, error=refusal)
        try:
            from fluid_build.util.safe_http import safe_httpx_client

            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.delete(
                    f"/api/dataproducts/{product_id}",
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

    # ── HTTP helpers ──────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        """Auth headers — sends BOTH ``Authorization: Bearer`` and
        ``x-api-key`` so the same registrar talks to either DMM cloud
        (Bearer JWT) or self-hosted Entropy Data (x-api-key, the OSS
        edition's auth header). Neither server complains about the
        unused header; the one it cares about wins. Documented at
        https://docs.datamesh-manager.com/api/authentication."""
        return {
            "Authorization": f"Bearer {self.api_token}",
            "x-api-key": self.api_token,
            "Content-Type": "application/json",
        }

    def _put_data_product(
        self,
        payload: CatalogPublicationPayload,
        *,
        sent_contract_ids: Optional[set] = None,
    ) -> Dict[str, Any]:
        """``PUT /api/dataproducts/{id}`` with the ODPS-shaped payload.

        We prefer the pre-rendered ODPS YAML when available (parsed
        back to a dict via PyYAML) so the wire body is exactly what
        ``fluid render --format odps`` would emit. Falls back to a
        minimal native shape — derived directly from the canonical
        payload — when ODPS rendering wasn't available.
        """
        from fluid_build.providers.datamesh_manager._publish_flow import (
            drop_unresolved_port_contract_ids,
        )
        from fluid_build.util.safe_http import safe_httpx_client

        body = self._render_dmm_data_product_body(payload)
        if sent_contract_ids is not None:
            # A port names only a contract that exists: one this publish sent, or one
            # Entropy already has. Otherwise the product links to a contract nobody has.
            for contract_id in drop_unresolved_port_contract_ids(
                body, sent_contract_ids, self._contract_exists
            ):
                LOG.warning(
                    "Output port contract %s was not sent (no ODCS for it) and is not in "
                    "Entropy; the port goes out without a contractId.",
                    contract_id,
                )
        with safe_httpx_client(
            base_url=self.api_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.put(
                f"/api/dataproducts/{payload.product.product_id}",
                json=body,
                headers=self._headers(),
            )
            if r.status_code >= 400:
                raise _HttpStatusError(
                    f"DMM PUT /data-products returned {r.status_code}: " + (r.text or "")[:512]
                )
        return body

    def _put_data_contract(
        self,
        contract_id: str,
        asset: AssetPayload,
        *,
        body: Optional[Dict[str, Any]] = None,
    ) -> None:
        """``PUT /api/datacontracts/{contract_id}`` with the ODCS body.

        ``contract_id`` is ``{product_id}.{asset_id}`` to match DMM's
        own per-port linkage convention (see
        ``DataMeshManagerProvider._publish_odcs_per_expose``).
        """
        from fluid_build.util.safe_http import safe_httpx_client

        if body is None:
            body = self._render_dmm_data_contract_body(asset)
        if body is None:
            # No ODCS available — silently skip the contract PUT rather
            # than POSTing an empty body that DMM would reject. The
            # data-product PUT above still landed.
            return
        with safe_httpx_client(
            base_url=self.api_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.put(
                f"/api/datacontracts/{quote(contract_id, safe='')}",
                json=body,
                headers=self._headers(),
            )
            if r.status_code >= 400:
                raise _HttpStatusError(
                    f"DMM PUT /datacontracts/{contract_id} returned "
                    f"{r.status_code}: " + (r.text or "")[:512]
                )

    # ── Body renderers ────────────────────────────────────────────────

    @staticmethod
    def _render_dmm_data_product_body(
        payload: CatalogPublicationPayload,
    ) -> Dict[str, Any]:
        """Prefer the ODPS YAML rendered at payload-build time, falling
        back to a native shape so the PUT can't silently no-op."""
        if payload.specs.odps_yaml:
            try:
                import yaml as _yaml

                parsed = _yaml.safe_load(payload.specs.odps_yaml)
                if isinstance(parsed, dict):
                    # Override id deterministically so DMM's path-route
                    # matches even if the renderer ever changes its
                    # ``id`` source.
                    parsed["id"] = payload.product.product_id
                    # ── Output port overlay ─────────────────────────
                    # Two DMM-specific tweaks the legacy
                    # ``DataMeshManagerProvider`` does that the bare
                    # ODPS render skips:
                    #
                    #   1. ``customProperties[displayName]`` — Entropy
                    #      CE renders this as the port's human label.
                    #      Without it the UI falls back to the
                    #      technical name and treats the port as
                    #      "draft / un-named" in some views.
                    #   2. ``version`` semver-normalise — DMM expects
                    #      a full semver string. The OdpsStandardProvider
                    #      sometimes truncates to ``"1"``; coerce to
                    #      ``"1.0.0"`` so DMM doesn't flag the port as
                    #      malformed.
                    DataMeshManagerRegistrar._overlay_dmm_output_port_fields(parsed, payload)
                    # ── Input ports ──────────────────────────────────
                    # Kept as rendered. A product consume's port carries the
                    # upstream contract id, and Entropy draws the product ->
                    # product edge from it (status ``declared``, merged with an
                    # approved Access agreement for the same pair). Lineage
                    # carried only by Access agreements was missing across
                    # teams, where an agreement stays ``requested``. A build
                    # source's port gets the ``sourceSystemId`` / ``type``
                    # custom properties Entropy links it to its SourceSystem by.
                    DataMeshManagerRegistrar._inject_build_input_port_source_systems(
                        parsed, payload
                    )
                    # ── Archetype ────────────────────────────────────
                    # Entropy reads it from ``customProperties[type]``;
                    # without it every product shows as a plain "Data Product".
                    from fluid_build.providers.datamesh_manager._odps_helpers import (
                        ensure_odps_archetype_custom_property,
                    )

                    ensure_odps_archetype_custom_property(parsed, payload.contract or {})
                    return parsed
            except Exception:  # noqa: BLE001 — fall through to native
                LOG.debug(
                    "ODPS YAML parse failed for %s — using native fallback",
                    payload.product.product_id,
                    exc_info=True,
                )
        # Native fallback: a minimal, DMM-readable data product shape.
        product = payload.product
        return {
            "id": product.product_id,
            "name": product.name or product.product_id,
            "description": product.description,
            "owner": {
                "team": product.owner.team if product.owner else "unknown",
                "email": product.owner.email if product.owner else "",
            },
            "ports": [
                {
                    "id": asset.asset_id,
                    "type": "table",
                    "platform": asset.platform,
                    "schema": [
                        {
                            "name": col.name,
                            "type": col.native_type,
                            "classifications": list(payload.classifications.get(col.name) or ()),
                        }
                        for col in asset.schema
                    ],
                }
                for asset in payload.assets
            ],
            "tags": list(product.tags),
            "metadata": {
                "layer": product.layer,
                "productType": product.product_type,
                "domain": product.domain,
                "version": product.version,
            },
        }

    @staticmethod
    def _render_dmm_data_contract_body(asset: AssetPayload) -> Optional[Dict[str, Any]]:
        """Parse the asset's pre-rendered ODCS YAML back to a dict.
        DMM's ``/api/datacontracts/{id}`` endpoint accepts ODCS v3.1
        natively — no further translation needed."""
        if not asset.odcs_yaml:
            return None
        try:
            import yaml as _yaml

            parsed = _yaml.safe_load(asset.odcs_yaml)
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            LOG.debug("ODCS YAML parse failed for asset %s", asset.asset_id, exc_info=True)
            return None

    @staticmethod
    def _overlay_dmm_output_port_fields(
        odps_payload: Dict[str, Any], payload: CatalogPublicationPayload
    ) -> None:
        """Apply the DMM-specific overlay the legacy
        ``DataMeshManagerProvider`` applied to ODPS-shaped output ports.

        Three tweaks the bare ODPS render skips that the DMM UI
        depends on:

        1. **``description`` per output port** — without this, the
           Entropy UI silently suppresses the per-port contract-link
           render (each output port still has ``contractId`` in the
           API payload, but the UI uses ``description`` presence as
           a "this port is fully described" signal before rendering
           the linked-contract chip). The legacy provider derives
           it from ``contract.exposes[].description`` or falls back
           to the product description so every port has *something*.
        2. **``customProperties[displayName]``** — Entropy CE renders
           the port with a human label; without it the UI treats
           unlabelled ports as draft / "deleted" in some views.
        3. **``version``** semver-coerced to ``x.y.z`` — the
           OdpsStandardProvider sometimes emits a bare ``"1"``;
           DMM is tolerant of bare versions but the explicit semver
           form keeps the port detail page consistent with
           rest-of-DMM conventions.
        """
        output_ports = odps_payload.get("outputPorts")
        if not isinstance(output_ports, list) or not output_ports:
            return

        # Build {asset_id: (display_name, description)} from canonical
        # payload + raw contract (for description, which isn't on the
        # AssetPayload yet — read straight from contract.exposes[]).
        display_by_asset: Dict[str, str] = {
            asset.asset_id: asset.asset_id for asset in payload.assets
        }
        description_by_asset: Dict[str, str] = {}
        for expose in (payload.contract or {}).get("exposes") or []:
            if not isinstance(expose, dict):
                continue
            asset_id = expose.get("exposeId") or expose.get("name") or expose.get("id")
            if not asset_id:
                continue
            desc = expose.get("description") or ""
            if desc:
                description_by_asset[str(asset_id)] = str(desc)
            title = expose.get("title") or expose.get("name") or ""
            if title:
                display_by_asset[str(asset_id)] = str(title)

        product_description = payload.product.description or ""

        for port in output_ports:
            if not isinstance(port, dict):
                continue
            port_name = str(port.get("name") or "")

            # 1. description — preferred order: expose-level, then
            #    product-level. Never set an empty string; without
            #    description the UI suppresses the contract-link chip.
            if not port.get("description"):
                desc = description_by_asset.get(port_name) or product_description
                if desc:
                    port["description"] = desc

            # 2. customProperties.displayName
            props = port.get("customProperties")
            if not isinstance(props, list):
                props = []
            already_has = any(
                isinstance(p, dict) and str(p.get("property", "")).lower() == "displayname"
                for p in props
            )
            if not already_has:
                display = display_by_asset.get(port_name, port_name)
                props.append({"property": "displayName", "value": display})
            port["customProperties"] = props

            # 3. semver-normalise version
            version = str(port.get("version") or "").strip()
            if version and "." not in version:
                parts = [p for p in version.split(".") if p]
                while len(parts) < 3:
                    parts.append("0")
                port["version"] = ".".join(parts[:3])

    # ── Pre-flight: enumerate existing DMM products ──────────────────

    def _existing_product_ids(self) -> set[str]:
        """Return the set of DataProduct IDs currently in the DMM tenant.

        Best-effort: a failed GET returns an empty set (which disables the
        pre-flight skip — the access PUT then proceeds blindly, matching
        the pre-2026-05 behavior). Lifted out so the access-agreement
        loop in ``register_payload`` can skip PUTs whose upstream product
        doesn't exist without inflicting a noisy 404 on the operator's
        publish log.
        """
        return self._read_product_ids() or set()

    def _read_product_ids(self) -> Optional[set]:
        """Every product id Entropy holds, or ``None`` when the list cannot be read."""
        from fluid_build.util.safe_http import safe_httpx_client

        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                ids: set[str] = set()
                # Entropy pages the list at 1000 (``p`` from 0); reading page 0 only
                # made every upstream past the first 1000 look missing.
                for page in range(_MAX_PRODUCT_PAGES):
                    r = c.get("/api/dataproducts", params={"p": page}, headers=self._headers())
                    if r.status_code != 200:
                        return None
                    data = r.json()
                    if not isinstance(data, list):
                        return None
                    before = len(ids)
                    ids.update(
                        str(p.get("id")) for p in data if isinstance(p, dict) and p.get("id")
                    )
                    # A short page is the last; a page with nothing new means the
                    # server ignores ``p``, so asking again would only repeat it.
                    if len(data) < _PRODUCT_PAGE_SIZE or len(ids) == before:
                        break
                return ids
        except Exception:  # noqa: BLE001 — best effort; disable pre-flight on failure
            return None

    # ── Owner team upsert ────────────────────────────────────────────
    #
    # DMM requires the data-contract's ``info.owner`` (and the data
    # product's ``team.name``) to reference an EXISTING team id. The
    # native ``fluid dmm publish`` path auto-creates teams before
    # publish; this registrar previously skipped that step, so every
    # contract referencing a non-existing team failed publish with
    # ``422 owner '<team-id>' is not a known team ID``. We mirror the
    # native path: PUT /api/teams/{id} upsert, idempotent.

    def _ensure_owner_team(self, payload: "CatalogPublicationPayload") -> None:
        """Upsert the owner team referenced by the contract.

        Payload shape mirrors the native provider's
        ``_build_team_payload`` (notably ``type: "Data Product Team"`` —
        DMM rejects the PUT with ``400 Failed to read request`` without
        it). Non-fatal: if the upsert fails (HTTP error, missing perms,
        etc.) we log WARNING and let the downstream contract PUT surface
        the real cause. DMM tolerates re-PUT of an existing team.
        """
        owner = payload.product.owner if payload.product else None
        team_id = (owner.team if owner else None) or "unknown"
        if not team_id or team_id == "unknown":
            return
        # GET first — if it already exists, skip the PUT (avoids 4xx noise
        # and matches native provider's behavior).
        from fluid_build.util.safe_http import safe_httpx_client

        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                head = c.get(f"/api/teams/{team_id}", headers=self._headers())
                if head.status_code == 200:
                    return
        except Exception:  # noqa: BLE001 — best-effort GET; proceed to PUT regardless
            pass

        # Construct the canonical Bitol team payload. ``type`` is REQUIRED
        # — without it DMM returns 400 "Failed to read request" because
        # the deserializer can't pick a TeamType.
        team_body: Dict[str, Any] = {
            "id": team_id,
            "name": team_id.replace("-", " ").replace("_", " ").title(),
            "type": "Data Product Team",
            "description": ("Auto-created by forge-cli on first publish referencing " "this team."),
        }
        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.put(
                    f"/api/teams/{team_id}",
                    json=team_body,
                    headers=self._headers(),
                )
                if r.status_code >= 400:
                    import logging

                    logging.getLogger(__name__).warning(
                        "DMM PUT /teams/%s returned %s — downstream contract "
                        "publish will fail if team doesn't exist. Body: %s",
                        team_id,
                        r.status_code,
                        (r.text or "")[:200],
                    )
        except Exception as exc:  # noqa: BLE001 — non-fatal upsert
            import logging

            logging.getLogger(__name__).warning(
                "Team upsert /teams/%s failed (non-fatal): %s", team_id, exc
            )

    # ── Source systems — SDP build-source lineage in DMM ─────────────
    #
    # Source-aligned data products (SDPs) declare their ingestion source
    # under ``builds[].properties.source`` (e.g. ``kind: postgres`` +
    # connection). DMM models that upstream as a ``SourceSystem`` entity
    # and renders it as a labelled node on the lineage graph — but only
    # when:
    #   1. The SourceSystem entity exists in DMM, and
    #   2. The data product's ``inputPorts`` reference it via
    #      ``sourceSystemId``.
    # The legacy native provider handled both. Ported here so the
    # canonical-registrar publish has parity.

    def _ensure_source_systems(self, payload: CatalogPublicationPayload) -> List[str]:
        """Upsert one ``SourceSystem`` per unique build-source declared
        on the contract. Returns the list of DMM URNs upserted (for the
        publish result metadata).

        Failures upsert SourceSystem entities are logged but non-fatal:
        a missing source system only degrades the lineage graph render;
        the data product publish still succeeds.
        """
        from fluid_build.util.contract import (
            builds_to_canonical_input_ports,
            consumes_to_canonical_ports,
            kind_to_dmm_type,
            redact_source_connection,
        )

        contract = payload.contract or {}
        team_id = payload.product.owner.team if payload.product.owner else "unknown"

        seen: set[str] = set()
        urns: List[str] = []

        # builds[].properties.source → SourceSystem (SDP path)
        for port in builds_to_canonical_input_ports(contract, logger=LOG):
            sys_id = port.get("source_system_id")
            if not sys_id or sys_id in seen:
                continue
            seen.add(sys_id)
            ok = self._upsert_source_system(
                sys_id=str(sys_id),
                team_id=team_id,
                kind=port.get("kind"),
                redacted_connection=port.get("source_connection") or None,
                tags=["acquisition", str(port.get("kind"))] if port.get("kind") else None,
                kind_to_dmm_type=kind_to_dmm_type,
            )
            if ok:
                urns.append(f"dmm://sourcesystems/{sys_id}")

        # consumes[].sourceSystem → SourceSystem (legacy / explicit only)
        for port in consumes_to_canonical_ports(contract, logger=LOG):
            sys_id = port.get("source_system_id")
            if not sys_id or sys_id in seen:
                continue
            seen.add(sys_id)
            ok = self._upsert_source_system(
                sys_id=str(sys_id),
                team_id=team_id,
                kind=port.get("kind"),
                redacted_connection=None,
                tags=None,
                kind_to_dmm_type=kind_to_dmm_type,
            )
            if ok:
                urns.append(f"dmm://sourcesystems/{sys_id}")

        return urns

    def _upsert_source_system(
        self,
        *,
        sys_id: str,
        team_id: str,
        kind: Optional[str] = None,
        redacted_connection: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        kind_to_dmm_type=None,
    ) -> bool:
        """PUT a SourceSystem entity to ``/api/sourcesystems/{id}``.

        Body shape matches DMM's published schema (id, name, owner,
        tags, custom). ``custom.type`` carries the TitleCase connector
        kind so DMM's UI renders the right icon on lineage edges;
        ``custom`` also carries the **already-redacted** connection
        block (host/port/database/schema — never credentials). The
        caller guarantees the connection has gone through
        :func:`~fluid_build.util.contract.redact_source_connection`.

        Returns True on success, False on any failure (which is logged
        at WARNING — non-fatal).
        """
        from fluid_build.util.safe_http import safe_httpx_client

        body: Dict[str, Any] = {
            "id": sys_id,
            "name": sys_id,
            "owner": team_id,
        }
        if tags:
            body["tags"] = list(tags)
        custom: Dict[str, Any] = {}
        if kind_to_dmm_type is not None:
            dmm_type = kind_to_dmm_type(kind)
            if dmm_type:
                custom["type"] = dmm_type
        if kind:
            custom["kind"] = str(kind)
        if redacted_connection:
            for k, v in redacted_connection.items():
                custom[k] = str(v)
        if custom:
            body["custom"] = custom

        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.put(
                    f"/api/sourcesystems/{sys_id}",
                    json=body,
                    headers=self._headers(),
                )
                if r.status_code >= 400:
                    LOG.warning(
                        "DMM PUT /sourcesystems/%s returned %s: %s",
                        sys_id,
                        r.status_code,
                        (r.text or "")[:256],
                    )
                    return False
            LOG.info("Upserted SourceSystem %s", sys_id)
            return True
        except Exception as exc:  # noqa: BLE001 — non-fatal by design
            LOG.warning("Could not upsert SourceSystem %s (non-fatal): %s", sys_id, exc)
            return False

    @staticmethod
    def _inject_build_input_port_source_systems(
        odps_payload: Dict[str, Any], payload: CatalogPublicationPayload
    ) -> None:
        """Add the ``sourceSystemId`` / ``type`` custom properties to each
        build-source input port.

        The ODPS export records the source as
        ``customProperties[sourceSystem|sourceKind]``; Entropy links an input
        port to a SourceSystem only by ``customProperties[sourceSystemId]`` and
        labels the edge from ``customProperties[type]``. See
        :func:`~fluid_build.providers.datamesh_manager._odps_helpers.promote_input_port_native_source_system_fields`.
        """
        from fluid_build.providers.datamesh_manager._odps_helpers import (
            ensure_odps_input_port_source_system_custom_property,
            promote_input_port_native_source_system_fields,
        )

        contract = payload.contract or {}
        # A legacy ``consumes[].sourceSystem`` gets its ``sourceSystem`` label
        # first, as the native provider's default mode does, so the
        # SourceSystem ``_ensure_source_systems`` upserts for it is linked too.
        ensure_odps_input_port_source_system_custom_property(
            odps_payload, contract, default_from_reference=False
        )
        promote_input_port_native_source_system_fields(odps_payload, contract)

    # ── Access agreements — product-to-product lineage in DMM ────────

    def _get_json_or_none(self, path: str) -> Optional[Any]:
        """``GET path`` as JSON, or ``None`` when it is absent or could not be read."""
        found, body = self._read_json(path)
        return body if found else None

    def _read_json(self, path: str) -> tuple:
        """``GET path`` as ``(found, body)``: ``(True, body)``, ``(False, None)`` on a 404,
        ``(None, None)`` when it could not be read."""
        from fluid_build.util.safe_http import safe_httpx_client

        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.get(path, headers=self._headers())
            if r.status_code == 404:
                return False, None
            if r.status_code >= 400:
                LOG.warning("DMM GET %s returned %s", path, r.status_code)
                return None, None
            return True, r.json()
        except Exception as exc:  # noqa: BLE001 — a read here only informs a reconcile
            LOG.warning("Could not read %s: %s", path, exc)
            return None, None

    def _contract_exists(self, contract_id: str) -> Optional[bool]:
        """A retired contract counts as absent: a port must not link to one."""
        from fluid_build.providers.datamesh_manager._publish_flow import is_live_contract

        found, body = self._read_json(f"/api/datacontracts/{quote(contract_id, safe='')}")
        return is_live_contract(found, body)

    def _retire_stale_contracts(
        self,
        product_id: str,
        existing_product: Optional[Any],
        keep_ids: set,
    ) -> List[str]:
        """Re-PUT each contract the product no longer names with ``status: retired``.

        Never deleted, best effort, and only contracts that are forge's (see
        :func:`~fluid_build.providers.datamesh_manager._publish_flow.stale_contract_candidates`).
        The native provider's ``_retire_stale_contracts`` does the same.
        """
        from fluid_build.providers.datamesh_manager._publish_flow import (
            retired_contract_body,
            stale_contract_candidates,
        )
        from fluid_build.util.safe_http import safe_httpx_client

        retired: List[str] = []
        existing = existing_product if isinstance(existing_product, dict) else None
        for contract_id in stale_contract_candidates(existing, product_id, keep_ids):
            path = f"/api/datacontracts/{quote(contract_id, safe='')}"
            found, current = self._read_json(path)
            body = retired_contract_body(current, contract_id, product_id) if found else None
            if body is None:
                continue
            try:
                with safe_httpx_client(
                    base_url=self.api_url,
                    timeout=float(self.timeout_seconds),
                    allow_private=True,
                ) as c:
                    r = c.put(path, json=body, headers=self._headers())
                if r.status_code >= 400:
                    LOG.warning(
                        "Could not retire stale contract %s: HTTP %s", contract_id, r.status_code
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 — best effort, like the access reconcile
                LOG.warning("Could not retire stale contract %s: %s", contract_id, exc)
                continue
            LOG.info("Retired contract %s: no output port names it any more", contract_id)
            retired.append(contract_id)
        return retired

    def _cancel_stale_access_agreements(self, product_id: str, keep_ids: set[str]) -> List[str]:
        """``POST /api/access/{id}/cancel`` for each agreement forge made for a
        consume the contract no longer has; returns the cancelled ids.

        An approved agreement keeps drawing its edge after the consume is gone.
        Cancel with an ``endTimestamp`` of now ends it immediately and keeps
        its history; ``DELETE`` refuses an active agreement. Which agreements are
        stale is decided by the native provider's
        :func:`~fluid_build.providers.datamesh_manager._publish_flow.stale_access_agreement_ids`,
        so both publish paths cancel the same set. Best effort: a failure is
        logged and the publish still succeeds.
        """
        from fluid_build.providers.datamesh_manager._publish_flow import (
            _ACCESS_PAGE_SIZE,
            stale_access_agreement_ids,
            stale_access_cancel_body,
        )
        from fluid_build.util.safe_http import safe_httpx_client

        cancelled: List[str] = []
        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.get(
                    "/api/access",
                    params={"consumerDataProductId": product_id, "pageSize": _ACCESS_PAGE_SIZE},
                    headers=self._headers(),
                )
                if r.status_code != 200:
                    LOG.warning(
                        "DMM GET /access returned %s; stale agreements of %s not cancelled",
                        r.status_code,
                        product_id,
                    )
                    return cancelled
                for access_id in stale_access_agreement_ids(r.json(), product_id, keep_ids):
                    cr = c.post(
                        f"/api/access/{quote(access_id, safe='')}/cancel",
                        json=stale_access_cancel_body(),
                        headers=self._headers(),
                    )
                    if cr.status_code >= 400:
                        LOG.warning(
                            "DMM POST /access/%s/cancel returned %s", access_id, cr.status_code
                        )
                        continue
                    LOG.info("Cancelled stale Access lineage %s", access_id)
                    cancelled.append(access_id)
        except Exception as exc:  # noqa: BLE001 — reconcile is best effort
            LOG.warning("Could not cancel stale Access agreements of %s: %s", product_id, exc)
        return cancelled

    @staticmethod
    def _build_access_agreements(
        payload: CatalogPublicationPayload,
    ) -> List[Dict[str, Any]]:
        """Generate one DMM Access Agreement per ``contract.consumes[]``.

        DMM models cross-product lineage as Access Agreements: an
        agreement at ``/api/access/{id}`` declares "consumer X reads
        from provider Y's output port Z" and the DMM UI renders that
        as a directed edge between the two products on the lineage
        graph, and an approved one takes over the ``declared`` edge the
        consume's input port draws.

        Agreement id is deterministic
        (``{consumer}__uses__{provider}__{output_port}``, matching the
        native provider's slug) so a re-publish upserts cleanly rather
        than minting duplicates.
        """
        from datetime import datetime, timezone

        consumer_product_id = payload.product.product_id
        start_date = datetime.now(timezone.utc).date().isoformat()
        consumes = (payload.contract or {}).get("consumes") or []
        agreements: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for ref in consumes:
            if not isinstance(ref, dict):
                continue
            provider_product_id = ref.get("productId")
            provider_output_port_id = ref.get("exposeId")
            if not provider_product_id or not provider_output_port_id:
                continue
            access_id = _slug_access_id(
                consumer_product_id,
                str(provider_product_id),
                str(provider_output_port_id),
            )
            if access_id in seen:
                continue
            seen.add(access_id)
            agreements.append(
                {
                    "id": access_id,
                    "info": {
                        "purpose": (
                            f"{consumer_product_id} consumes "
                            f"{provider_product_id}.{provider_output_port_id}."
                        ),
                        "startDate": start_date,
                    },
                    "provider": {
                        "dataProductId": str(provider_product_id),
                        "outputPortId": str(provider_output_port_id),
                    },
                    "consumer": {"dataProductId": consumer_product_id},
                    "tags": ["fluid", "lineage"],
                    "custom": {
                        "managedBy": "forge-cli",
                        "source": "fluid.consumes",
                        "providerContractId": (f"{provider_product_id}.{provider_output_port_id}"),
                    },
                }
            )
        return agreements

    def _put_access_agreement(self, agreement: Dict[str, Any]) -> None:
        """``PUT /api/access/{id}`` for one agreement. Idempotent —
        same body re-PUT just refreshes the resource.

        When ``self.auto_approve_access`` is True (or
        ``DMM_AUTO_APPROVE_ACCESS=true`` in env), follows the PUT with a
        ``POST /api/access/{id}/approve`` so DMM's lineage graph renders
        the edge immediately. Without approval the edge stays 'pending'
        and the UI shows the product as having no upstreams.
        """
        from fluid_build.util.safe_http import safe_httpx_client

        with safe_httpx_client(
            base_url=self.api_url,
            timeout=float(self.timeout_seconds),
            allow_private=True,
        ) as c:
            r = c.put(
                f"/api/access/{agreement['id']}",
                json=agreement,
                headers=self._headers(),
            )
            if r.status_code >= 400:
                raise _HttpStatusError(
                    f"DMM PUT /access/{agreement['id']} returned "
                    f"{r.status_code}: " + (r.text or "")[:512]
                )

            if self.auto_approve_access:
                approve_r = c.post(
                    f"/api/access/{agreement['id']}/approve",
                    headers=self._headers(),
                )
                if approve_r.status_code >= 400:
                    # Approve is best-effort — DMM may return 4xx if the
                    # agreement is already approved or in a non-pending
                    # state. Log but don't fail the publish.
                    import logging

                    logging.getLogger(__name__).warning(
                        "DMM POST /access/%s/approve returned %s — agreement "
                        "remains in whatever state DMM returned (likely "
                        "already-approved). Body: %s",
                        agreement["id"],
                        approve_r.status_code,
                        (approve_r.text or "")[:200],
                    )


def _slug_access_id(consumer: str, provider: str, output_port: str) -> str:
    """The Access-agreement id for one consume edge: the native provider's own.

    Both publish paths cancel the forge agreements whose ids the current publish
    did not mint, so they must mint the same id for the same edge. The registrar
    used to slug with its own rule, per character and without ``~``, which
    differed from the native one (runs collapsed, ``~`` kept) for an id holding
    either.
    """
    from fluid_build.providers.datamesh_manager._publish_flow import _PublishFlowMixin

    return _PublishFlowMixin._access_agreement_id(consumer, provider, output_port)


class _HttpStatusError(Exception):
    """Lifted to a private exception so ``register_payload`` can
    distinguish HTTP-status failures (caller-actionable) from
    transport-layer failures (network down, DNS broken)."""


# ── Plugin registration ─────────────────────────────────────────────────
#
# Self-register so ``properties.catalog.register: [datamesh_manager]``
# resolves through the canonical layer (rather than only via the
# legacy native async provider that lives under
# ``fluid_build/providers/catalogs/datamesh_manager.py``). The native
# provider stays as the rich DMM-specific Surface A path with team
# management / access agreements; this registrar is the canonical-
# payload-driven Surface B equivalent.

from fluid_build.api.catalog_backend import (  # noqa: E402 — register-on-import is intentional
    CatalogBackendSpec,
    CatalogCapability,
    register_catalog_backend,
)

from ._factory_helpers import pick_endpoint, pick_int, pick_token  # noqa: E402

_ODPS_SPELLINGS = frozenset({"odps", "odps-bitol", "odps_bitol", "bitol"})


def _warn_unused_settings(config: dict) -> None:
    """Say so when a setting asks the registrar for what it does not do.

    It publishes ODPS only (Entropy rejects DPS 0.0.1 with "Supported types: odps"), and
    links lineage from ``consumes[]`` and build sources whatever ``DMM_ODPS_LINEAGE_MODE``
    says; ``fluid datamesh-manager publish`` is the path these settings shape.
    """
    for key, env in (
        ("data_product_specification", "DMM_DATA_PRODUCT_SPECIFICATION"),
        ("provider_hint", "DMM_PROVIDER_HINT"),
    ):
        value = config.get(key)
        if value and str(value).strip().lower() not in _ODPS_SPELLINGS:
            LOG.warning(
                "datamesh-manager %s=%r (%s) is not used by fluid publish: it publishes "
                "ODPS, the only data product format Entropy Data accepts.",
                key,
                value,
                env,
            )
    if os.environ.get("DMM_ODPS_LINEAGE_MODE"):
        LOG.warning(
            "DMM_ODPS_LINEAGE_MODE is not used by fluid publish: lineage comes from the "
            "contract's consumes[] and build sources."
        )


def _build_dmm_registrar(config: dict) -> DataMeshManagerRegistrar:
    _warn_unused_settings(config)
    return DataMeshManagerRegistrar(
        api_url=pick_endpoint(config, default=None) or None,
        api_token=pick_token(config),
        timeout_seconds=pick_int(config, "timeout", 30),
    )


register_catalog_backend(
    CatalogBackendSpec(
        name="datamesh_manager",
        aliases=("datamesh-manager", "entropy-data", "dmm"),
        registrar_factory=_build_dmm_registrar,
        env_vars={
            "endpoint": (
                "FLUID_CATALOG_DMM_URL",
                "DMM_API_URL",
            ),
            "api_token": (
                "FLUID_CATALOG_DMM_TOKEN",
                "DMM_API_KEY",
            ),
        },
        capabilities=frozenset(
            {
                CatalogCapability.DATA_PRODUCT,
                CatalogCapability.PER_ASSET_CONTRACT,
                CatalogCapability.PRODUCT_SPECS,
                CatalogCapability.DOMAIN,
                CatalogCapability.OWNERSHIP,
                CatalogCapability.CUSTOM_PROPERTIES,
            }
        ),
        description=(
            "Data Mesh Manager / Entropy Data via "
            "PUT /api/dataproducts (ODPS) + /api/datacontracts (ODCS per asset)"
        ),
    )
)
