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

"""DataMesh Manager publish-flow methods — physical extraction.

Lifted from ``providers/datamesh_manager/datamesh_manager.py`` (host
file was 1791 LOC). ~650 LOC of access-agreements + ODCS-per-expose
+ umbrella-contract + ``_to_data_product`` flow methods. Split as a
mixin class (:class:`_PublishFlowMixin`) so existing call sites
(``self._publish_one(...)``) keep resolving via MRO.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

# Regex to strip URL-unsafe characters from access-agreement IDs.
# Lifted from the host module so the extracted methods are self-
# contained (the host originally used the bare name).
_ACCESS_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._~-]+")

from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager._contract_builders import (
    _PROVIDER_TYPE_MAP,
    _STATUS_MAP,
)
from fluid_build.util.contract import consumes_to_canonical_ports

LOG = logging.getLogger("fluid.providers.datamesh_manager.publish")


class _PublishFlowMixin:
    """Holds the publish-flow methods for :class:`DataMeshManagerProvider`.

    Methods rely on self-state populated by the host class
    (``self._request``, ``self._access_agreement_id``,
    ``self._build_access_agreements``, ``self._log``,
    ``self._extract_id``, ``self._derive_team_id``,
    ``self._extract_provider``, ``self._build_data_contract_odcs``,
    etc.). Method bodies are unchanged from the inline originals.
    """

    def _preview_access_agreements(
        self,
        fluid: Mapping[str, Any],
        consumer_product_id: str,
        *,
        auto_approve_access: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return Entropy Access payloads generated from FLUID ``consumes``.

        Entropy models product-to-product graph edges as Access resources. The
        ODPS ``inputPorts[].contractId`` field is still important, but it points
        to the upstream data contract; it is not the graph edge itself.
        """
        previews: List[Dict[str, Any]] = []
        for payload in self._build_access_agreements(fluid, consumer_product_id):
            preview = {
                "method": "PUT",
                "url": f"{self.api_url}/api/access/{payload['id']}",
                "auto_approve": auto_approve_access,
                "payload": payload,
            }
            if auto_approve_access:
                preview["approve_url"] = f"{self.api_url}/api/access/{payload['id']}/approve"
            previews.append(preview)
        return previews

    def _publish_access_agreements(
        self,
        fluid: Mapping[str, Any],
        consumer_product_id: str,
        *,
        auto_approve_access: bool = False,
    ) -> List[Dict[str, Any]]:
        """Create Entropy Access agreements for FLUID ``consumes``.

        Pre-flight: enumerate existing DataProduct IDs and skip access
        agreements for upstream products that don't exist yet (DMM returns
        a generic ``404`` on PUT in that case, which is hard to read in a
        publish log). Each skipped agreement gets a row in the returned
        results so the operator sees the missing-upstream warning.
        """
        payloads = self._build_access_agreements(fluid, consumer_product_id)
        results: List[Dict[str, Any]] = []

        # Pre-flight: build the set of existing product IDs so we can detect
        # missing upstreams without a noisy 404 mid-publish.
        existing_product_ids: set[str] = set()
        try:
            for prod in self.list_products() or []:
                pid = prod.get("id")
                if pid:
                    existing_product_ids.add(str(pid))
        except Exception:  # noqa: BLE001 — best effort; fall back to PUT-and-pray
            existing_product_ids = set()

        for payload in payloads:
            access_id = payload["id"]
            provider = payload.get("provider", {})
            upstream_pid = str(provider.get("dataProductId") or "")
            if existing_product_ids and upstream_pid and upstream_pid not in existing_product_ids:
                # Surface a structured skip rather than letting DMM 404.
                self._log.warning(
                    "Skipping Access agreement %s: upstream product %s does not exist in DMM",
                    access_id,
                    upstream_pid,
                )
                results.append(
                    {
                        "access_id": access_id,
                        "success": False,
                        "skipped": True,
                        "reason": "missing_upstream_product",
                        "provider_data_product_id": upstream_pid,
                        "provider_output_port_id": provider.get("outputPortId"),
                        "consumer_data_product_id": consumer_product_id,
                        "error": (
                            f"upstream product '{upstream_pid}' not published — "
                            f"publish it first or remove the consume from the contract"
                        ),
                    }
                )
                continue
            try:
                put_resp = self._request("PUT", f"/api/access/{access_id}", json_body=payload)
                approve_resp = None
                if auto_approve_access:
                    approve_resp = self._request("POST", f"/api/access/{access_id}/approve")
                self._log.info(
                    "Published Access lineage %s -> %s.%s (HTTP %s%s)",
                    consumer_product_id,
                    provider.get("dataProductId"),
                    provider.get("outputPortId"),
                    put_resp.status_code,
                    f"/{approve_resp.status_code}" if approve_resp is not None else "",
                )
                result = {
                    "access_id": access_id,
                    "success": True,
                    "status_code": put_resp.status_code,
                    "auto_approved": auto_approve_access,
                    "provider_data_product_id": provider.get("dataProductId"),
                    "provider_output_port_id": provider.get("outputPortId"),
                    "consumer_data_product_id": consumer_product_id,
                    "url": f"{self.api_url}/access/{access_id}",
                }
                if approve_resp is not None:
                    result["approval_status_code"] = approve_resp.status_code
                results.append(result)
            except ProviderError as exc:
                self._log.error("Failed to publish Access lineage %s: %s", access_id, exc)
                raise

        return results

    def _build_access_agreements(
        self,
        fluid: Mapping[str, Any],
        consumer_product_id: str,
        *,
        start_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build Entropy Access resources from canonical FLUID consume refs."""
        effective_start_date = start_date or datetime.utcnow().date().isoformat()
        payloads: List[Dict[str, Any]] = []
        seen: set[str] = set()

        for canonical in consumes_to_canonical_ports(fluid, logger=LOG):
            provider_product_id = canonical.get("reference")
            provider_output_port_id = canonical.get("id")
            if not provider_product_id or not provider_output_port_id:
                continue

            provider_product_id = str(provider_product_id)
            provider_output_port_id = str(provider_output_port_id)
            access_id = self._access_agreement_id(
                consumer_product_id,
                provider_product_id,
                provider_output_port_id,
            )
            if access_id in seen:
                continue
            seen.add(access_id)

            purpose = canonical.get("description") or (
                f"{consumer_product_id} consumes {provider_product_id}.{provider_output_port_id}."
            )

            tags = ["fluid", "lineage"]
            for tag in canonical.get("tags") or []:
                tag = str(tag)
                if tag not in tags:
                    tags.append(tag)

            custom = {
                "managedBy": "forge-cli",
                "source": "fluid.consumes",
                "providerContractId": (
                    str(canonical.get("contract_id"))
                    if canonical.get("contract_id")
                    else f"{provider_product_id}.{provider_output_port_id}"
                ),
            }
            if canonical.get("version_constraint"):
                custom["versionConstraint"] = str(canonical["version_constraint"])

            payloads.append(
                {
                    "id": access_id,
                    "info": {
                        "purpose": str(purpose),
                        "startDate": effective_start_date,
                    },
                    "provider": {
                        "dataProductId": provider_product_id,
                        "outputPortId": provider_output_port_id,
                    },
                    "consumer": {
                        "dataProductId": consumer_product_id,
                    },
                    "tags": tags,
                    "custom": custom,
                }
            )

        return payloads

    @staticmethod
    def _access_agreement_id(
        consumer_product_id: str,
        provider_product_id: str,
        provider_output_port_id: str,
    ) -> str:
        raw = f"{consumer_product_id}__uses__{provider_product_id}__{provider_output_port_id}"
        access_id = _ACCESS_ID_UNSAFE.sub("_", raw).strip("_")
        return access_id or str(uuid.uuid4())

    # ---- ODCS per-expose publishing ---------------------------------------

    def _preview_odcs_per_expose(
        self, fluid: Mapping[str, Any], product_id: str
    ) -> List[Dict[str, Any]]:
        """Return the ODCS payloads that *_publish_odcs_per_expose* would PUT
        (used for dry-run mode only — no HTTP calls made).
        """
        try:
            from fluid_build.providers.odcs import OdcsProvider  # lazy import
        except ImportError as exc:
            self._log.warning("OdcsProvider not available — cannot preview ODCS contracts: %s", exc)
            return []

        odcs_prov = OdcsProvider()
        previews: List[Dict[str, Any]] = []
        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id:
                continue
            contract_id = f"{product_id}.{expose_id}"
            try:
                odcs_body = odcs_prov.render(fluid, expose_id=expose_id)
            except Exception as exc:
                self._log.warning("Could not generate ODCS preview for %s: %s", expose_id, exc)
                continue
            previews.append(
                {
                    "method": "PUT",
                    "url": f"{self.api_url}/api/datacontracts/{contract_id}",
                    "payload": odcs_body,
                }
            )
        return previews

    def _publish_odcs_per_expose(
        self,
        fluid: Mapping[str, Any],
        product_id: str,
        *,
        validate_generated_contracts: bool = False,
        validation_mode: str = "warn",
    ) -> List[Dict[str, Any]]:
        """Publish one ODCS data contract for every expose port.

        Each contract is PUT to ``/api/datacontracts/{product_id}.{exposeId}``
        in ODCS v3.1.0 JSON format.  The contract id matches the ``dataContractId``
        already written into the output port by ``_map_output_ports``.

        Returns a list of per-expose result dicts.
        """
        try:
            from fluid_build.providers.odcs import OdcsProvider  # lazy import
        except ImportError as exc:
            raise ProviderError(
                "OdcsProvider is required to publish ODCS contracts.\n"
                "Ensure fluid_build.providers.odcs is installed."
            ) from exc

        odcs_prov = OdcsProvider()
        results: List[Dict[str, Any]] = []

        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id:
                self._log.warning("Expose missing exposeId/id — skipping ODCS contract publish")
                continue

            contract_id = f"{product_id}.{expose_id}"

            try:
                odcs_body = odcs_prov.render(fluid, expose_id=expose_id)
            except Exception as exc:
                self._log.error(
                    "Failed to generate ODCS contract for expose '%s': %s", expose_id, exc
                )
                results.append(
                    {
                        "contract_id": contract_id,
                        "expose_id": expose_id,
                        "success": False,
                        "error": str(exc),
                        "error_type": "RENDER_FAILED",
                    }
                )
                continue

            payload_stats = self._summarize_odcs_payload(odcs_body)
            self._log.info(
                (
                    "Prepared ODCS contract %s for expose '%s' "
                    "(schema_objects=%s, properties=%s, servers=%s, sla_properties=%s)"
                ),
                contract_id,
                expose_id,
                payload_stats["schema_objects"],
                payload_stats["schema_properties"],
                payload_stats["servers"],
                payload_stats["sla_properties"],
            )

            validation_error = None
            is_valid: Optional[bool] = None
            if validate_generated_contracts:
                is_valid, validation_error = self._validate_generated_odcs_contract(
                    odcs_prov, odcs_body
                )
                if is_valid is False:
                    self._log.warning(
                        "Generated ODCS contract failed local validation for expose '%s': %s",
                        expose_id,
                        validation_error,
                    )
                    if validation_mode == "strict":
                        results.append(
                            {
                                "contract_id": contract_id,
                                "expose_id": expose_id,
                                "success": False,
                                "valid": False,
                                "validation_error": validation_error,
                                "error_type": "VALIDATION_FAILED",
                            }
                        )
                        continue

            try:
                resp = self._request(
                    "PUT", f"/api/datacontracts/{contract_id}", json_body=odcs_body
                )
                self._log.info(
                    "Published ODCS contract %s (HTTP %s)", contract_id, resp.status_code
                )
                entry: Dict[str, Any] = {
                    "contract_id": contract_id,
                    "expose_id": expose_id,
                    "success": True,
                    "status_code": resp.status_code,
                    "url": f"{self.api_url}/datacontracts/{contract_id}",
                }
                if is_valid is not None:
                    entry["valid"] = is_valid
                if validation_error:
                    entry["validation_error"] = validation_error
                entry.update(payload_stats)
                results.append(entry)
            except ProviderError as exc:
                self._log.error("HTTP error publishing ODCS contract %s: %s", contract_id, exc)
                entry = {
                    "contract_id": contract_id,
                    "expose_id": expose_id,
                    "success": False,
                    "error": str(exc),
                    "error_type": "HTTP_FAILED",
                }
                if is_valid is not None:
                    entry["valid"] = is_valid
                if validation_error:
                    entry["validation_error"] = validation_error
                entry.update(payload_stats)
                results.append(entry)

        success_count = len([r for r in results if r.get("success")])
        failed_count = len(results) - success_count
        self._log.info(
            "ODCS publish summary for %s: %s succeeded, %s failed",
            product_id,
            success_count,
            failed_count,
        )

        return results

    def _validate_generated_odcs_contract(
        self, odcs_provider: Any, odcs_body: Mapping[str, Any]
    ) -> tuple[bool, Optional[str]]:
        """Validate rendered ODCS payload and return (is_valid, error_message)."""
        try:
            if hasattr(odcs_provider, "validate_contract"):
                odcs_provider.validate_contract(odcs_body)
            else:
                odcs_provider._validate_odcs(odcs_body)
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    @staticmethod
    def _summarize_odcs_payload(odcs_body: Mapping[str, Any]) -> Dict[str, int]:
        schema = odcs_body.get("schema", [])
        servers = odcs_body.get("servers", [])
        sla_properties = odcs_body.get("slaProperties", [])

        schema_objects = len(schema) if isinstance(schema, list) else 0
        schema_properties = 0
        if isinstance(schema, list):
            for schema_object in schema:
                if not isinstance(schema_object, Mapping):
                    continue
                properties = schema_object.get("properties", [])
                if isinstance(properties, list):
                    schema_properties += len(properties)

        return {
            "schema_objects": schema_objects,
            "schema_properties": schema_properties,
            "servers": len(servers) if isinstance(servers, list) else 0,
            "sla_properties": len(sla_properties) if isinstance(sla_properties, list) else 0,
        }

    # ---- Umbrella ODCS contract (product-level resolution target) ---------

    # ODCS v3.1.0 is the spec forge-cli targets when publishing per-expose
    # contracts via ``OdcsProvider``. Keep the umbrella in lockstep so a
    # future ODCS spec bump only needs to be made in one place here (the
    # umbrella does not use ``OdcsProvider.render`` because it has no
    # corresponding expose to extract schema from).
    _UMBRELLA_ODCS_API_VERSION = "v3.1.0"

    def _render_product_umbrella_contract(
        self, fluid: Mapping[str, Any], product_id: str
    ) -> Dict[str, Any]:
        """Render a minimal ODCS v3.1.0 contract at ``{product_id}``.

        Why this exists: DMM's UI renders ``inputPorts[].contractId`` as a
        resolved link if and only if that id maps to an existing
        ``/api/datacontracts/{id}``. forge-cli's canonical address for a
        product's contracts is ``{product_id}.{expose_id}`` (one per expose
        port) — there is no per-product contract by default, so any lineage
        reference that points at just ``{product_id}`` 404s and renders as an
        unresolved link.

        The umbrella contract fills that gap: it's a thin stub that advertises
        "the real schemas are published as one contract per expose; see
        members" so the UI has a valid resolution target and a human landing
        on the page understands why it's empty.

        Intentional shape choices:
          * ``schema: []`` (empty) — an umbrella does not describe a
            queryable surface; it's a resolution pointer. A non-empty schema
            would be a *lie* (there is no table with these columns).
          * ``description.purpose`` names the convention explicitly so the
            reader isn't left wondering why the contract is empty.
          * ``customProperties`` advertises ``umbrella: true`` so downstream
            tooling can filter umbrellas out of "list my real contracts"
            queries.
        """
        metadata = fluid.get("metadata") or {}
        product_name = metadata.get("name") or fluid.get("name") or product_id
        product_description = metadata.get("description") or fluid.get("description") or ""
        status = _STATUS_MAP.get(str(metadata.get("status", "active")).lower(), "active")
        version = str(metadata.get("version") or "1.0.0")

        # Enumerate member exposes so consumers landing on the umbrella can
        # see which per-expose contracts to dereference instead. This is NOT
        # an ODCS schema — it's a reference list carried in customProperties,
        # which is the only free-form field the v3.1.0 spec allows.
        member_expose_ids: List[str] = []
        for expose in fluid.get("exposes", []) or []:
            if not isinstance(expose, Mapping):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if expose_id:
                member_expose_ids.append(str(expose_id))

        purpose_lines = [
            (
                f"Umbrella resolution contract for data product '{product_id}'. "
                "The product's actual per-expose schemas are published as one "
                f"ODCS contract per output port, at '{product_id}.{{exposeId}}'."
            )
        ]
        if product_description:
            purpose_lines.append(str(product_description))
        if member_expose_ids:
            member_list = ", ".join(f"{product_id}.{eid}" for eid in member_expose_ids)
            purpose_lines.append(f"Member contracts: {member_list}.")

        body: Dict[str, Any] = {
            "apiVersion": self._UMBRELLA_ODCS_API_VERSION,
            "kind": "DataContract",
            "id": product_id,
            "version": version,
            "status": status,
            "name": product_name,
            "description": {
                "purpose": " ".join(purpose_lines),
                "usage": (
                    "Reference the per-expose contracts "
                    f"('{product_id}.{{exposeId}}') for binding schemas. "
                    "This umbrella is a resolution target only — its schema is "
                    "intentionally empty."
                ),
            },
            "schema": [],
            "customProperties": [
                {"property": "umbrella", "value": True},
                {
                    "property": "memberContracts",
                    "value": [f"{product_id}.{eid}" for eid in member_expose_ids],
                },
            ],
        }

        # Team: use the same derivation as per-expose contracts / the product
        # itself so the umbrella lands under the same team in DMM. ODCS v3.1.0
        # accepts ``team: { name: <id> }`` (nested object), which matches the
        # shape DMM's per-expose contract endpoint returns. A bare string here
        # is rejected as an unknown teamId.
        try:
            team_id = self._derive_team_id(fluid)
        except Exception:  # pragma: no cover — defensive, should not happen
            team_id = None
        if team_id:
            body["team"] = {"name": str(team_id)}

        tags = metadata.get("tags") or fluid.get("tags")
        if isinstance(tags, list) and tags:
            body["tags"] = list(tags)

        # NOTE: intentionally do NOT emit ``domain`` on the umbrella.
        # DMM's ODCS validator treats certain top-level fields (e.g. bare
        # ``domain``) as team/owner references and 422s on unknown IDs
        # (observed: ``The owner '<domain>' is not a known team ID``).
        # The umbrella is a resolution stub — domain metadata already lives
        # on the data product and per-expose contracts, so adding it here
        # provides no value and trips the validator.

        return body

    def _preview_product_umbrella_contract(
        self, fluid: Mapping[str, Any], product_id: str
    ) -> Dict[str, Any]:
        """Dry-run preview for the umbrella contract — no HTTP calls."""
        body = self._render_product_umbrella_contract(fluid, product_id)
        return {
            "method": "PUT",
            "url": f"{self.api_url}/api/datacontracts/{product_id}",
            "payload": body,
            "umbrella": True,
        }

    def _publish_product_umbrella_contract(
        self, fluid: Mapping[str, Any], product_id: str
    ) -> Dict[str, Any]:
        """PUT the umbrella ODCS stub at ``/api/datacontracts/{product_id}``.

        Returns a result dict with ``success``, ``contract_id``, and either
        ``status_code`` (on success) or ``error`` / ``error_type`` (on
        failure). A failure here is logged but does NOT propagate — a missing
        umbrella just means product-level lineage links render unresolved in
        the UI (pre-umbrella behavior), which is strictly no worse than
        skipping the call.
        """
        body = self._render_product_umbrella_contract(fluid, product_id)
        try:
            resp = self._request("PUT", f"/api/datacontracts/{product_id}", json_body=body)
            self._log.info("Published umbrella contract %s (HTTP %s)", product_id, resp.status_code)
            return {
                "contract_id": product_id,
                "success": True,
                "status_code": resp.status_code,
                "url": f"{self.api_url}/datacontracts/{product_id}",
                "umbrella": True,
            }
        except ProviderError as exc:
            self._log.warning(
                "Failed to publish umbrella contract %s (non-fatal): %s",
                product_id,
                exc,
            )
            return {
                "contract_id": product_id,
                "success": False,
                "error": str(exc),
                "error_type": "UMBRELLA_PUT_FAILED",
                "umbrella": True,
            }

    # ---- mapping: FLUID -> Entropy Data Product ----------------------------

    def _to_data_product(
        self,
        fluid: Mapping[str, Any],
        *,
        data_product_specification: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Map a FLUID contract to the Entropy Data *DataProduct* shape.

        Builds the legacy Data Product Specification v0.0.1 payload.
        Note: callers normally arrive here only when DPS is explicitly
        requested — the resolver in
        :meth:`_resolve_data_product_specification` defaults to ODPS
        because Entropy / Data Mesh Manager rejects DPS payloads on
        ODPS-only organizations. When ``data_product_specification`` is
        ``None`` here the payload still carries
        ``self.DATA_PRODUCT_SPEC_DPS`` ("0.0.1") for backwards
        compatibility with direct callers of this private helper.

        Reference: ``PUT /api/dataproducts/{id}``

        Schema requires:
        - ``id`` at root level
        - ``info.title`` (not ``info.name``)
        - ``info.owner`` (team id)
        - ``dataProductSpecification`` at root (DPS ``"0.0.1"`` here)
        """
        if self._is_odps_spec(data_product_specification):
            return self._to_data_product_odps(fluid)

        meta = fluid.get("metadata", {})
        owner = fluid.get("owner", meta.get("owner", {}))

        product_id = self._extract_id(fluid)
        status = _STATUS_MAP.get(str(meta.get("status", "draft")).lower(), "draft")

        info: Dict[str, Any] = {
            "title": meta.get("name") or fluid.get("name") or product_id,
            "owner": self._derive_team_id(fluid),
            "description": meta.get("description") or fluid.get("description", ""),
            "status": status,
        }

        # Optional info-level fields
        # Canonical Data Mesh mapping (also DMM API archetype values):
        #   Bronze / SDP  → "source-aligned"  (raw acquisition)
        #   Silver / ADP  → "aggregate"       (cleaned, joined, conformed)
        #   Gold   / CDP  → "consumer-aligned"(consumption marts)
        # Prior versions had Silver→consumer-aligned and Gold→aggregate
        # swapped; this is the corrected mapping. metadata.productType is
        # always populated by the equivalence axiom (forge.product_types),
        # so prefer that and fall back to the layer alias only when absent.
        _ARCHETYPE_BY_PRODUCT_TYPE = {
            "SDP": "source-aligned",
            "ADP": "aggregate",
            "CDP": "consumer-aligned",
        }
        _ARCHETYPE_BY_LAYER = {
            "bronze": "source-aligned",
            "raw": "source-aligned",
            "silver": "aggregate",
            "curated": "aggregate",
            "gold": "consumer-aligned",
            "consumption": "consumer-aligned",
        }
        if meta.get("archetype"):
            info["archetype"] = meta["archetype"]
        elif fluid.get("kind"):
            kind_lower = str(fluid["kind"]).lower()
            if kind_lower == "dataproduct":
                product_type = str(meta.get("productType") or "").upper()
                archetype = _ARCHETYPE_BY_PRODUCT_TYPE.get(product_type)
                if archetype is None:
                    layer_lower = str(meta.get("layer", "")).lower()
                    archetype = _ARCHETYPE_BY_LAYER.get(layer_lower)
                if archetype is not None:
                    info["archetype"] = archetype
        if meta.get("maturity"):
            info["maturity"] = meta["maturity"]

        dp: Dict[str, Any] = {
            "dataProductSpecification": data_product_specification or self.DATA_PRODUCT_SPEC_DPS,
            "id": product_id,
            "info": info,
        }

        # Input ports (expects)
        input_ports = self._map_input_ports(fluid)
        if input_ports:
            dp["inputPorts"] = input_ports

        # Output ports (exposes) — pass product_id so each port gets a dataContractId
        output_ports = self._map_output_ports(fluid, product_id=product_id)
        if output_ports:
            dp["outputPorts"] = output_ports

        # Links
        links = self._extract_links(fluid)
        if links:
            dp["links"] = links

        # Tags — merge top-level and metadata tags
        all_tags: List[str] = []
        top_tags = fluid.get("tags", [])
        if isinstance(top_tags, list):
            all_tags.extend(top_tags)
        meta_tags = meta.get("tags", [])
        if isinstance(meta_tags, list):
            for t in meta_tags:
                if t not in all_tags:
                    all_tags.append(t)
        if all_tags:
            dp["tags"] = all_tags

        # Custom fields  (domain, environment, version, etc.)
        custom = self._extract_custom(fluid)
        if custom:
            dp["custom"] = custom

        return dp
