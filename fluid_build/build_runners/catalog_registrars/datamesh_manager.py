# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

from fluid_build.api.catalog import CatalogRegistrar, RegistrationResult
from fluid_build.api.catalog_publication import (
    AssetPayload,
    CatalogPublicationPayload,
)

LOG = logging.getLogger("fluid.acquire.catalog.datamesh_manager")


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
        # Late env-var fallback so env state at register-time wins.
        self.api_url = (
            self.api_url or os.environ.get("DMM_API_URL") or "https://api.datamesh-manager.com"
        )
        self.api_token = self.api_token or os.environ.get("DMM_API_KEY")
        # Env-var fallback for auto-approve. Explicit constructor value wins;
        # the env var only fires when not set (i.e. default False).
        if not self.auto_approve_access:
            env_value = os.environ.get("DMM_AUTO_APPROVE_ACCESS", "").strip().lower()
            self.auto_approve_access = env_value in {"1", "true", "yes", "on"}

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
           data product. Build-driven ``inputPorts`` (with proper
           ``sourceSystemId`` populated by the renderer below) survive;
           product-to-product ``inputPorts`` are stripped because
           Entropy's ``dataproduct-0.0.1.json`` schema requires every
           inputPort to carry a ``sourceSystemId`` (i.e. external
           systems only) — product-to-product lineage flows through
           Access agreements instead.
        4. ``PUT /api/access/{access_id}`` per ``contract.consumes[]``
           entry — one Access Agreement per upstream output port. DMM's
           UI renders these as edges between products on the lineage
           graph.

        Failures on the data-product PUT short-circuit; failures on
        individual contracts or access agreements are surfaced via the
        returned ``RegistrationResult.error``. SourceSystem upserts are
        non-fatal (logged at WARNING) — a missing source-system entity
        only degrades the lineage graph, not the product publish.
        """
        product_id = payload.product.product_id
        product_urn = f"dmm://{product_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=product_urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
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
            for asset in payload.assets:
                contract_id = f"{product_id}.{asset.asset_id}"
                contract_urn = f"dmm://datacontracts/{contract_id}"
                self._put_data_contract(contract_id, asset)
                contract_urns.append(contract_urn)
            self._put_data_product(payload)
            # Pre-flight: enumerate existing DMM DataProduct IDs so we can
            # SKIP access-agreement PUTs whose upstream product doesn't
            # exist yet (DMM otherwise returns a generic ``404`` mid-publish
            # that's hard to read in a CI log). Mirrors the native
            # provider's Gap-9 pre-flight check in
            # ``providers/datamesh_manager/_publish_flow.py``.
            existing_pids = self._existing_product_ids()
            for agreement in self._build_access_agreements(payload):
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
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target,
                urn=urn,
                succeeded=False,
                error="DMM_API_KEY not set; refusing anonymous publish",
            )
        payload = CatalogPublicationPayload.from_contract(contract, classifications)
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
        urn = f"dmm://{product_id}/{expose_id}"
        if not self.api_token:
            return RegistrationResult(
                target=self.target, urn=urn, succeeded=False, error="DMM_API_KEY not set"
            )
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

    def _put_data_product(self, payload: CatalogPublicationPayload) -> None:
        """``PUT /api/dataproducts/{id}`` with the ODPS-shaped payload.

        We prefer the pre-rendered ODPS YAML when available (parsed
        back to a dict via PyYAML) so the wire body is exactly what
        ``fluid render --format odps`` would emit. Falls back to a
        minimal native shape — derived directly from the canonical
        payload — when ODPS rendering wasn't available.
        """
        from fluid_build.util.safe_http import safe_httpx_client

        body = self._render_dmm_data_product_body(payload)
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

    def _put_data_contract(self, contract_id: str, asset: AssetPayload) -> None:
        """``PUT /api/datacontracts/{contract_id}`` with the ODCS body.

        ``contract_id`` is ``{product_id}.{asset_id}`` to match DMM's
        own per-port linkage convention (see
        ``DataMeshManagerProvider._publish_odcs_per_expose``).
        """
        from fluid_build.util.safe_http import safe_httpx_client

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
                f"/api/datacontracts/{contract_id}",
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
                    # Strip product-to-product ``inputPorts``. DMM /
                    # Entropy treat ``inputPorts`` as references to
                    # SourceSystem entities (external producers) with
                    # required ``sourceSystemId`` + custom-property
                    # ``sourceSystem`` definitions that don't exist for
                    # product-to-product flow. Cross-product lineage
                    # belongs on the per-asset ODCS contract (which we
                    # PUT separately) and on DMM Access agreements
                    # (not modelled here). Mirrors the
                    # ``_remove_odps_product_consume_input_ports`` step
                    # in the richer async ``DataMeshManagerProvider``.
                    # ── Input ports ──────────────────────────────────
                    # The ODPS render emits ``inputPorts`` derived from
                    # both ``consumes[]`` (product-to-product) and
                    # ``builds[].properties.source`` (SDP acquisition).
                    # Entropy rejects any inputPort that lacks
                    # ``sourceSystemId`` (its dataproduct-0.0.1.json
                    # schema treats inputPorts as references to
                    # SourceSystem entities only). For product-to-
                    # product flows the lineage moves to Access
                    # agreements (see ``_build_access_agreements``); for
                    # SDP build sources we INJECT the
                    # ``sourceSystemId`` here from the canonical
                    # build-port helper so the port survives the strip
                    # and DMM renders the lineage edge "this product ←
                    # this Postgres source system" in its graph.
                    DataMeshManagerRegistrar._inject_build_input_port_source_systems(
                        parsed, payload
                    )
                    if "inputPorts" in parsed and isinstance(parsed["inputPorts"], list):
                        parsed["inputPorts"] = [
                            ip
                            for ip in parsed["inputPorts"]
                            if isinstance(ip, dict)
                            and "sourceSystemId" in ip
                            and ip["sourceSystemId"]
                        ]
                        if not parsed["inputPorts"]:
                            parsed.pop("inputPorts", None)
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
        from fluid_build.util.safe_http import safe_httpx_client

        try:
            with safe_httpx_client(
                base_url=self.api_url,
                timeout=float(self.timeout_seconds),
                allow_private=True,
            ) as c:
                r = c.get("/api/dataproducts", headers=self._headers())
                if r.status_code != 200:
                    return set()
                data = r.json()
                if not isinstance(data, list):
                    return set()
                return {str(p.get("id")) for p in data if isinstance(p, dict) and p.get("id")}
        except Exception:  # noqa: BLE001 — best effort; disable pre-flight on failure
            return set()

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
        """Wire ``sourceSystemId`` + ``type`` onto each build-driven
        input port by promoting from ODPS ``customProperties``.

        The standalone ODPS-Bitol v1.0.0 artifact carries source-system
        info under ``customProperties[sourceSystem|sourceKind]`` because
        the Bitol ``InputPort`` schema is closed and forbids native
        ``sourceSystemId`` / ``type`` fields. DMM (Entropy Data),
        however, both accepts those native fields AND uses them to
        render the lineage edge in the UI. So we delegate to the
        existing ``promote_input_port_native_source_system_fields``
        helper (battle-tested in the legacy provider) which copies the
        customProperties values into the native fields. Without this
        promotion, the downstream strip-step removes every input port
        for lack of ``sourceSystemId`` and the SDP lands with no
        upstream lineage edge in DMM's graph.

        Idempotent: skips ports that already carry an explicit
        ``sourceSystemId``.
        """
        from fluid_build.providers.datamesh_manager._odps_helpers import (
            promote_input_port_native_source_system_fields,
        )

        promote_input_port_native_source_system_fields(odps_payload)

    # ── Access agreements — product-to-product lineage in DMM ────────

    @staticmethod
    def _build_access_agreements(
        payload: CatalogPublicationPayload,
    ) -> List[Dict[str, Any]]:
        """Generate one DMM Access Agreement per ``contract.consumes[]``.

        DMM models cross-product lineage as Access Agreements: an
        agreement at ``/api/access/{id}`` declares "consumer X reads
        from provider Y's output port Z" and the DMM UI renders that
        as a directed edge between the two products on the lineage
        graph. Without these, ``consumes[]`` has no native DMM
        representation (Entropy's data-product schema rejects
        product-to-product ``inputPorts`` — see the strip step in
        ``_render_dmm_data_product_body``).

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


# Access-agreement ids must be URL-safe (DMM routes by id). Slug rule
# matches the legacy provider's: alphanumeric + dot/dash/underscore.
_ACCESS_ID_UNSAFE = __import__("re").compile(r"[^A-Za-z0-9._-]")


def _slug_access_id(consumer: str, provider: str, output_port: str) -> str:
    # The slug uses ``__uses__`` to match the native provider's
    # ``_access_agreement_id`` format (see
    # ``providers/datamesh_manager/_publish_flow.py``). Previously the
    # registrar slugged with ``__consumes__`` — different access IDs in
    # DMM for the same conceptual edge, so a contract re-published through
    # the registrar would create a parallel access record next to the
    # native provider's existing one, and DMM could end up with two
    # competing approval states for one upstream→downstream lineage edge.
    raw = f"{consumer}__uses__{provider}__{output_port}"
    return _ACCESS_ID_UNSAFE.sub("_", raw).strip("_")


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


def _build_dmm_registrar(config: dict) -> DataMeshManagerRegistrar:
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
