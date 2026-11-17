# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""DataMesh Manager publish-flow methods — physical extraction.

Lifted from ``providers/datamesh_manager/datamesh_manager.py`` (host
file was 1791 LOC). ~650 LOC of access-agreements + ODCS-per-expose
+ contract-hygiene + ``_to_data_product`` flow methods. Split as a
mixin class (:class:`_PublishFlowMixin`) so existing call sites
(``self._publish_one(...)``) keep resolving via MRO.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import quote

# Regex to strip URL-unsafe characters from access-agreement IDs.
# Lifted from the host module so the extracted methods are self-
# contained (the host originally used the bare name).
_ACCESS_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._~-]+")

from fluid_build.providers.base import ProviderError
from fluid_build.providers.datamesh_manager._contract_builders import (
    _PROVIDER_TYPE_MAP,
    _STATUS_MAP,
)
from fluid_build.util.contract import (
    consumes_to_canonical_ports,
    contract_id_collision,
    expose_contract_id_for_ref,
)

LOG = logging.getLogger("fluid.providers.datamesh_manager.publish")

# Entropy pages ``GET /api/access`` (pageSize 2..10000). One consumer with more
# agreements than this is far outside what a contract's ``consumes[]`` makes.
_ACCESS_PAGE_SIZE = 10000

# What forge stamps on the agreements it creates (``custom``), so a reconcile
# only ever touches its own.
_FORGE_ACCESS_MARKER = {"managedBy": "forge-cli", "source": "fluid.consumes"}

# How far a stale agreement's end is backdated; see ``stale_access_cancel_body``.
_CANCEL_CLOCK_MARGIN = timedelta(minutes=10)

# Agreement states with nothing left to cancel.
_ENDED_ACCESS = frozenset({"cancelled", "rejected"})


def stale_access_agreement_ids(
    existing: Any, consumer_product_id: str, keep_ids: set[str]
) -> List[str]:
    """Ids of forge-made agreements of ``consumer_product_id`` not in ``keep_ids``.

    ``existing`` is a ``GET /api/access`` body. An agreement qualifies only when
    forge made it (``custom`` carries :data:`_FORGE_ACCESS_MARKER`), its consumer
    is this product, and it has not ended (``info.active`` is not ``False`` and
    ``info.status`` is not ``cancelled`` or ``rejected``): an agreement a person
    made, or one already over, is left alone. Shared by both publish paths.
    """
    stale: List[str] = []
    for agreement in existing if isinstance(existing, list) else []:
        if not isinstance(agreement, Mapping):
            continue
        access_id = agreement.get("id")
        custom = agreement.get("custom") or {}
        consumer = agreement.get("consumer") or {}
        info = agreement.get("info") or {}
        if (
            not access_id
            or str(access_id) in keep_ids
            or not isinstance(custom, Mapping)
            or any(custom.get(k) != v for k, v in _FORGE_ACCESS_MARKER.items())
            or not isinstance(consumer, Mapping)
            or consumer.get("dataProductId") != consumer_product_id
            or (
                isinstance(info, Mapping)
                and (info.get("active") is False or info.get("status") in _ENDED_ACCESS)
            )
        ):
            continue
        stale.append(str(access_id))
    return stale


def stale_access_cancel_body() -> Dict[str, str]:
    """The ``POST /api/access/{id}/cancel`` body that ends an agreement now.

    Without ``endTimestamp`` Entropy ends it after the data contract's notice
    period, or at the end of today, and the edge stays drawn until then. An
    ``endTimestamp`` not in the server's future deactivates it immediately; one
    even a second in its future leaves it active until Entropy's own sweep, and
    a later cancel cannot move that end earlier. So the end is backdated by
    :data:`_CANCEL_CLOCK_MARGIN`, which absorbs clock skew between this machine
    and the server (Entropy accepts an end before the agreement's start). The
    reason lands in Entropy's audit trail as ``info.terminationReason``.
    """
    end = datetime.now(timezone.utc) - _CANCEL_CLOCK_MARGIN
    return {
        "endTimestamp": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": "The FLUID contract no longer consumes this output port (forge-cli publish).",
    }


_RETIRED = "retired"


def output_port_contract_ids(product: Optional[Mapping[str, Any]]) -> List[str]:
    """The contract ids a data product body's output ports name, in order: ODPS
    ``contractId`` and the DPS 0.0.1 spelling ``dataContractId``."""
    ids: List[str] = []
    ports = product.get("outputPorts") if isinstance(product, Mapping) else None
    for port in ports if isinstance(ports, list) else []:
        if not isinstance(port, Mapping):
            continue
        for key in ("contractId", "dataContractId"):
            cid = port.get(key)
            if isinstance(cid, str) and cid and cid not in ids:
                ids.append(cid)
    return ids


def is_live_contract(found: Optional[bool], body: Any) -> Optional[bool]:
    """Whether a read contract is one a port may name: ``True``, ``False`` (absent or
    retired), or ``None`` when the read could not tell."""
    if found is None:
        return None
    if not found:
        return False
    return not (isinstance(body, Mapping) and body.get("status") == _RETIRED)


def stale_contract_candidates(
    existing_product: Optional[Mapping[str, Any]], product_id: str, keep_ids: set
) -> List[str]:
    """Contract ids a publish may retire, before reading them.

    Those the product's output ports named in Entropy before this publish, inside the
    product's own ``{product_id}.`` namespace (the ids forge derives), and not named or
    sent now; then ``product_id`` itself, where earlier forge versions published an
    umbrella contract. A port that names a contract outside the namespace (a pinned or
    shared one) is never forge's to retire.
    """
    prefix = f"{product_id}."
    out = [
        cid
        for cid in output_port_contract_ids(existing_product)
        if cid.startswith(prefix) and cid not in keep_ids
    ]
    if product_id not in keep_ids:
        out.append(product_id)
    return out


def _is_umbrella_contract(contract: Mapping[str, Any]) -> bool:
    props = contract.get("customProperties")
    return any(
        isinstance(p, Mapping)
        and p.get("property") == "umbrella"
        and str(p.get("value")).lower() == "true"
        for p in (props if isinstance(props, list) else [])
    )


def retired_contract_body(
    contract: Any, contract_id: str, product_id: str
) -> Optional[Dict[str, Any]]:
    """The body that retires ``contract`` (``status: retired``), or ``None`` to leave it.

    Left alone: anything that is not a contract body, one already retired, and a
    contract at the bare ``product_id`` that is no umbrella forge published (someone
    else's). The rest of the body goes back unchanged, so retiring loses nothing.
    """
    if not isinstance(contract, Mapping) or contract.get("status") == _RETIRED:
        return None
    if contract_id == product_id and not _is_umbrella_contract(contract):
        return None
    body = dict(contract)
    body["status"] = _RETIRED
    return body


def drop_unresolved_port_contract_ids(
    product: Mapping[str, Any], sent_ids: set, exists: Any
) -> List[str]:
    """Remove each ``outputPorts[].contractId`` that names a contract nobody has.

    A port's contract resolves when this publish sends it, or when ``exists(id)`` says
    Entropy has it. ``exists`` returns ``True``, ``False`` (absent: the reference is
    dropped) or ``None`` (could not tell: the reference is kept). Returns the dropped ids.
    """
    dropped: List[str] = []
    ports = product.get("outputPorts")
    for port in ports if isinstance(ports, list) else []:
        cid = port.get("contractId") if isinstance(port, dict) else None
        if not isinstance(cid, str) or not cid or cid in sent_ids:
            continue
        if exists(cid) is False:
            del port["contractId"]
            dropped.append(cid)
    return dropped


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

    def _cancel_stale_access_agreements(
        self, consumer_product_id: str, keep_ids: set[str]
    ) -> List[str]:
        """Cancel the agreements forge made for consumes the contract no longer has.

        An approved agreement keeps drawing its edge after the consume is
        removed, so the graph names an upstream the product no longer reads.
        ``POST /api/access/{id}/cancel`` with :func:`stale_access_cancel_body`
        ends it now and keeps its history; ``DELETE`` refuses an active
        agreement anyway. Re-adding the consume later re-PUTs the agreement,
        which Entropy reactivates. Best effort: a failure is logged, never raised —
        the product itself is already published.
        """
        try:
            resp = self._request(
                "GET",
                f"/api/access?consumerDataProductId={quote(consumer_product_id, safe='')}"
                f"&pageSize={_ACCESS_PAGE_SIZE}",
            )
            existing = resp.json()
        except Exception as exc:  # noqa: BLE001 — reconcile is best effort
            self._log.warning(
                "Could not list Access agreements of %s to cancel stale ones: %s",
                consumer_product_id,
                exc,
            )
            return []
        cancelled: List[str] = []
        for access_id in stale_access_agreement_ids(existing, consumer_product_id, keep_ids):
            try:
                self._request(
                    "POST",
                    f"/api/access/{quote(access_id, safe='')}/cancel",
                    json_body=stale_access_cancel_body(),
                )
            except ProviderError as exc:
                self._log.warning("Could not cancel stale Access %s: %s", access_id, exc)
                continue
            self._log.info("Cancelled stale Access lineage %s", access_id)
            cancelled.append(access_id)
        return cancelled

    def _build_access_agreements(
        self,
        fluid: Mapping[str, Any],
        consumer_product_id: str,
        *,
        start_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Build Entropy Access resources from canonical FLUID consume refs."""
        effective_start_date = start_date or datetime.now(timezone.utc).date().isoformat()
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

        # The render below is wrapped in ``except Exception`` so one
        # unexportable port does not lose the whole preview, which also means
        # the per-port render's own collision refusal would come out as a
        # warning and a silently shorter preview. Refuse here instead, through
        # the same contract-level check every exporter calls: two ports PUT to
        # one ``/api/datacontracts/<id>`` url means the second body overwrites
        # the first and one port's schema is simply not in the catalog.
        collision = contract_id_collision(fluid, product_id=product_id)
        if collision:
            raise ProviderError(collision)

        odcs_prov = OdcsProvider()
        previews: List[Dict[str, Any]] = []
        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id:
                continue
            # Same resolution as the output port's ``contractId`` and as the
            # ODCS body's own ``id``: the url has to be the id the contract
            # declares, or the product references a contract nobody published.
            contract_id = (
                expose_contract_id_for_ref(fluid, expose_id, product_id=product_id)
                or f"{product_id}.{expose_id}"
            )
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

        Each contract is PUT to ``/api/datacontracts/<contract id>`` in ODCS
        v3.1.0 JSON format, where the contract id is what
        :func:`~fluid_build.util.contract.expose_contract_id_for_ref` resolves
        for the port — the id the expose pins or an ODPS import recorded, else
        the derived ``{product_id}.{exposeId}``. That is the same resolution
        the output port's ``contractId`` goes through, which is what keeps the
        reference and the published contract the same string.

        Returns a list of per-expose result dicts.
        """
        try:
            from fluid_build.providers.odcs import OdcsProvider  # lazy import
        except ImportError as exc:
            raise ProviderError(
                "OdcsProvider is required to publish ODCS contracts.\n"
                "Ensure fluid_build.providers.odcs is installed."
            ) from exc

        # See the preview above: a failed render is recorded per port and the
        # loop carries on, so the collision has to be refused before it.
        collision = contract_id_collision(fluid, product_id=product_id)
        if collision:
            raise ProviderError(collision)

        odcs_prov = OdcsProvider()
        results: List[Dict[str, Any]] = []

        for expose in fluid.get("exposes", []):
            if not isinstance(expose, dict):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id:
                self._log.warning("Expose missing exposeId/id — skipping ODCS contract publish")
                continue

            # See the preview above: the url is the resolved id, not the
            # derived one, so the published output port's ``contractId``
            # resolves to something that exists.
            contract_id = (
                expose_contract_id_for_ref(fluid, expose_id, product_id=product_id)
                or f"{product_id}.{expose_id}"
            )

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

    # ---- Contract hygiene: stale contracts, unresolved port references ---------

    def _read_json_or_none(self, path: str) -> tuple:
        """``GET path`` as ``(found, body)``: ``(True, body)``, ``(False, None)`` on a 404,
        ``(None, None)`` when it could not be read (any other error)."""
        try:
            resp = self._request("GET", path)
        except ProviderError as exc:
            if "API error 404 on" in str(exc):
                return False, None
            self._log.warning("Could not read %s: %s", path, exc)
            return None, None
        try:
            return True, resp.json()
        except Exception as exc:  # noqa: BLE001 — a body we cannot read is "could not tell"
            self._log.warning("Could not read %s: %s", path, exc)
            return None, None

    def _contract_exists(self, contract_id: str) -> Optional[bool]:
        """A retired contract counts as absent: a port must not link to one."""
        found, body = self._read_json_or_none(f"/api/datacontracts/{quote(contract_id, safe='')}")
        return is_live_contract(found, body)

    def _existing_product(self, product_id: str) -> Optional[Dict[str, Any]]:
        """The product as Entropy holds it before this publish, or ``None``."""
        found, body = self._read_json_or_none(f"/api/dataproducts/{quote(product_id, safe='')}")
        return body if found and isinstance(body, dict) else None

    def _retire_stale_contracts(
        self,
        product_id: str,
        existing_product: Optional[Mapping[str, Any]],
        keep_ids: set,
    ) -> List[str]:
        """Re-PUT each contract the product no longer names with ``status: retired``.

        Never deleted: a consumer that still reads it sees the contract was retired,
        and its history stays. See :func:`stale_contract_candidates` for which
        contracts are forge's to retire. Best effort: a failure logs a warning and
        the publish still succeeds.
        """
        retired: List[str] = []
        for contract_id in stale_contract_candidates(existing_product, product_id, keep_ids):
            path = f"/api/datacontracts/{quote(contract_id, safe='')}"
            found, current = self._read_json_or_none(path)
            body = retired_contract_body(current, contract_id, product_id) if found else None
            if body is None:
                continue
            try:
                self._request("PUT", path, json_body=body)
            except ProviderError as exc:
                self._log.warning("Could not retire stale contract %s: %s", contract_id, exc)
                continue
            self._log.info("Retired contract %s: no output port names it any more", contract_id)
            retired.append(contract_id)
        return retired

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
