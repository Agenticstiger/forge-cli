# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""ODPS-shape helpers for the DataMesh Manager provider.

Lifted from ``providers/datamesh_manager/datamesh_manager.py`` (the
host file was 2595 LOC). The functions here are pure transforms over
ODPS-Bitol payloads — no provider state, no I/O. They were
``@staticmethod`` on :class:`DataMeshManagerProvider` and remain
callable that way: the host class re-imports each helper and binds
it as a staticmethod, so existing call sites
(``self._ensure_odps_input_port_contract_ids(...)``) keep resolving.

What's here:

* :func:`is_odps_spec`, :func:`is_odps_payload` — sniff dialect.
* :func:`normalize_fluid_for_odps_standard` — pre-normalise FLUID
  shape before round-trip through the ODPS converter.
* :func:`ensure_odps_output_port_display_names` — DMM display labels
  on ODPS output ports.
* :func:`ensure_odps_input_port_contract_ids` — backfill / promote
  ``contractId`` from FLUID ``consumes``.
* :func:`ensure_odps_input_port_source_system_custom_property` —
  legacy DMM compatibility for deployments that still need a
  ``sourceSystem`` custom property.
* :func:`promote_input_port_native_source_system_fields` — the
  ``sourceSystemId`` / ``type`` custom properties Entropy links an input
  port to a SourceSystem by.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from fluid_build.providers.odps_standard.mappers.ports import input_port_contract_id
from fluid_build.util.contract import (
    builds_to_canonical_input_ports,
    consumes_to_canonical_ports,
    kind_to_dmm_type,
)

LOG = logging.getLogger("fluid.providers.datamesh_manager.odps")


# One-shot tracking of legacy ``"opds"`` spec strings seen on the wire from
# the DMM upstream. We only WARN once per process so audit aggregators can
# count occurrences without flooding the log; this lets us measure how
# quickly upstream callers migrate off the letter-swap.
_LEGACY_OPDS_SPEC_WARNED = False


def is_odps_spec(value: Optional[str]) -> bool:
    """True when ``value`` names ODPS (canonical) or its ``opds`` legacy alias.

    The DMM upstream historically sent both ``"odps"`` and ``"opds"`` for
    the same Bitol ODPS v1.0.0 shape — this helper accepts both as part of
    the **downstream protocol contract** with DMM. We cannot tighten to
    canonical-only without coordinating an upstream change.

    The first ``"opds"`` sighting per process emits a WARNING (via
    ``"opds_legacy_spec_string"``) so audit aggregators can track upstream
    migration off the letter-swap.
    """
    global _LEGACY_OPDS_SPEC_WARNED
    spec = str(value or "").strip().lower()
    if spec == "opds" and not _LEGACY_OPDS_SPEC_WARNED:
        LOG.warning(
            "opds_legacy_spec_string",
            extra={
                "canonical": "odps",
                "note": (
                    "DMM upstream sent the legacy letter-swap spec id 'opds'; "
                    "accepting for back-compat. Track this event to gauge "
                    "upstream migration off the letter-swap."
                ),
            },
        )
        _LEGACY_OPDS_SPEC_WARNED = True
    return spec in {"odps", "opds"}


def is_odps_payload(payload: Mapping[str, Any]) -> bool:
    """True when ``payload`` is shaped like an ODPS-Bitol DataProduct
    rather than a DMM-canonical DataProduct (``info`` field present)."""
    return bool(
        isinstance(payload, Mapping)
        and "apiVersion" in payload
        and str(payload.get("kind", "")).lower() == "dataproduct"
        and "info" not in payload
    )


def normalize_fluid_for_odps_standard(fluid: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize FLUID structure for ODPS-Bitol converter compatibility.

    The ODPS converter walks ``exposes[].id`` (newer FLUID) but some
    legacy contracts use ``exposes[].exposeId``. This shim copies the
    legacy field into ``id`` so the same converter handles both.
    """
    normalized: Dict[str, Any] = dict(fluid)

    exposes = fluid.get("exposes", [])
    normalized_exposes: List[Dict[str, Any]] = []
    if isinstance(exposes, list):
        for expose in exposes:
            if not isinstance(expose, Mapping):
                continue
            expose_dict = dict(expose)
            if not expose_dict.get("id") and expose_dict.get("exposeId"):
                expose_dict["id"] = expose_dict["exposeId"]
            normalized_exposes.append(expose_dict)
    normalized["exposes"] = normalized_exposes

    return normalized


def ensure_odps_output_port_display_names(
    odps_payload: Dict[str, Any], fluid: Mapping[str, Any]
) -> None:
    """Add DMM display names to ODPS output ports without breaking the
    official ODPS shape.

    ODPS-Bitol output ports use ``name`` as the technical identifier.
    Entropy CE stores that as ``output_port.external_id``; some UI
    paths render ``output_port.name`` and treat a missing value as
    "deleted". ``customProperties[displayName]`` is accepted by
    Entropy's ODPS importer and remains valid ODPS, so use it to
    populate the DMM display label.
    """
    output_ports = odps_payload.get("outputPorts")
    if not isinstance(output_ports, list) or not output_ports:
        return

    display_name_by_port: Dict[str, str] = {}
    for expose in fluid.get("exposes", []):
        if not isinstance(expose, Mapping):
            continue
        expose_id = expose.get("exposeId") or expose.get("id") or expose.get("name")
        if not expose_id:
            continue
        display_name = expose.get("title") or expose.get("name") or expose_id
        display_name_by_port[str(expose_id)] = str(display_name)

    for port in output_ports:
        if not isinstance(port, dict):
            continue
        port_name = port.get("name")
        if not port_name:
            continue
        display_name = display_name_by_port.get(str(port_name), str(port_name))

        props = port.get("customProperties")
        if not isinstance(props, list):
            props = []

        has_display_name = any(
            isinstance(prop, Mapping) and str(prop.get("property", "")).lower() == "displayname"
            for prop in props
        )
        if not has_display_name:
            props.append({"property": "displayName", "value": display_name})
        port["customProperties"] = props


def ensure_odps_input_port_contract_ids(
    odps_payload: Dict[str, Any], fluid: Mapping[str, Any]
) -> None:
    """Backfill / promote ODPS input-port contract IDs from FLUID
    ``consumes``.

    Entropy's ODPS product API requires ``inputPorts[].contractId``.
    Three cases:

    * **Explicit** — operator authored ``consumes[].contractId``;
      always respected.
    * **Backfill** — port has no ``contractId``; set to the canonical
      ``{productId}.{exposeId}`` address.
    * **Promote** — port already has a ``contractId`` but it's just
      the upstream product reference; rewrite to expose-level form.

    A port already carrying the id the ODPS export derives for one of the
    consumes is left alone. Matching is by bare exposeId, and two upstreams
    sharing one (``silver.b1`` and ``silver.b2`` both exposing ``customers``)
    collapse to one key: the export renames the second port, and the first
    port used to be overwritten with the second upstream's contract.
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list) or not input_ports:
        return

    canonical_ports = consumes_to_canonical_ports(fluid, logger=LOG)
    derived_contract_ids = {
        str(cid) for cid in (input_port_contract_id(c) for c in canonical_ports) if cid
    }
    explicit_contract_ids: Dict[str, str] = {}
    promoted_contract_ids: Dict[str, str] = {}
    product_references: Dict[str, str] = {}
    for canonical in canonical_ports:
        port_id = canonical.get("id")
        if not port_id:
            continue
        port_id = str(port_id)

        explicit = canonical.get("contract_id")
        if explicit:
            explicit_contract_ids[port_id] = str(explicit)

        reference = canonical.get("reference")
        if reference:
            product_references[port_id] = str(reference)
            promoted_contract_ids[port_id] = f"{reference}.{port_id}"

    for port in input_ports:
        if not isinstance(port, dict):
            continue

        port_id = port.get("id") or port.get("name")
        if not port_id:
            continue
        port_id = str(port_id)

        existing = port.get("contractId")
        if existing and str(existing) in derived_contract_ids:
            continue

        explicit = explicit_contract_ids.get(port_id)
        if explicit:
            if existing != explicit:
                port["contractId"] = explicit
            continue

        promoted = promoted_contract_ids.get(port_id)
        if not promoted:
            continue

        if not existing:
            port["contractId"] = promoted
            continue

        reference = product_references.get(port_id)
        if existing == reference and promoted != existing:
            port["contractId"] = promoted
            LOG.debug(
                "Promoted input port %s contractId %r -> %r (product-level -> expose-level)",
                port_id,
                existing,
                promoted,
            )


def ensure_odps_input_port_source_system_custom_property(
    odps_payload: Dict[str, Any],
    fluid: Mapping[str, Any],
    *,
    default_from_reference: bool = True,
) -> None:
    """Attach ODPS input-port ``customProperties[sourceSystem]`` when
    requested.

    ``default_from_reference=True`` is the explicit legacy
    compatibility mode for DMM deployments that still require a
    ``sourceSystem`` custom property on every input port.
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list) or not input_ports:
        return

    # Keyed by the port's ``contractId``, the identity the ODPS export builds each
    # consume's port from (``input_port_contract_id``). The bare exposeId is not one:
    # two upstreams exposing ``accounts`` share it, the export renames the second port
    # (``erp__accounts``), and a consume's ``name`` can differ from its exposeId. Keyed
    # by exposeId, one port got the other upstream's source system and one got none.
    canonical_ports = consumes_to_canonical_ports(fluid, logger=LOG)
    # A port with no ``contractId`` (not one the export builds) falls back to the
    # exposeId, as before.
    source_system_by_contract: Dict[str, str] = {}
    source_system_by_expose: Dict[str, str] = {}
    for canonical in canonical_ports:
        sys_id = canonical.get("source_system_id")
        if not sys_id and default_from_reference:
            sys_id = canonical.get("reference")
        if not sys_id:
            continue
        contract_id = input_port_contract_id(canonical)
        if contract_id:
            source_system_by_contract.setdefault(str(contract_id), str(sys_id))
        if canonical.get("id"):
            source_system_by_expose.setdefault(str(canonical["id"]), str(sys_id))

    for port in input_ports:
        if not isinstance(port, dict):
            continue

        props = port.get("customProperties")
        if not isinstance(props, list):
            props = []
        if any(isinstance(p, Mapping) and p.get("property") == "sourceSystem" for p in props):
            continue

        contract_id = port.get("contractId")
        if contract_id:
            fallback = source_system_by_contract.get(str(contract_id))
        else:
            port_id = port.get("id") or port.get("name")
            fallback = source_system_by_expose.get(str(port_id)) if port_id else None
        if not fallback and default_from_reference:
            fallback = port.get("reference")
        if not fallback:
            continue

        props.append({"property": "sourceSystem", "value": str(fallback)})
        port["customProperties"] = props


def published_source_system_ids(fluid: Mapping[str, Any]) -> set[str]:
    """The SourceSystem ids a publish upserts for ``fluid``.

    Both publish paths upsert one SourceSystem per build source
    (``builds[].properties.source``, see
    :func:`~fluid_build.util.contract.builds_to_canonical_input_ports`) and per
    explicitly authored ``consumes[].sourceSystem``. Only those ids name a
    SourceSystem that exists, so only those may become an input port's
    ``sourceSystemId``.
    """
    ids: set[str] = set()
    for canonical in builds_to_canonical_input_ports(fluid) + consumes_to_canonical_ports(fluid):
        sys_id = canonical.get("source_system_id")
        if sys_id:
            ids.add(str(sys_id))
    return ids


def promote_input_port_native_source_system_fields(
    odps_payload: Dict[str, Any],
    fluid: Mapping[str, Any],
) -> None:
    """Add the custom properties Entropy links an input port to a SourceSystem by.

    The ODPS export records an acquisition source as
    ``customProperties[sourceSystem|sourceKind]``. Entropy Data reads neither:
    against Entropy Data CE, an ODPS input port draws a SourceSystem -> product
    edge only when it carries ``customProperties[sourceSystemId]``, and the
    edge is labelled from ``customProperties[type]`` (``API`` without it). A
    native ``sourceSystemId`` / ``type`` field on the port is not ODPS — the
    v1.0.0 ``InputPort`` is ``additionalProperties: false`` — and Entropy drops
    it on save, which is why SDPs used to land with their SourceSystem node
    present and no edge to it.

    Only a ``sourceSystem`` value naming a SourceSystem this publish upserts
    (:func:`published_source_system_ids`) is promoted. The ``source-system``
    lineage mode labels a product consume with its upstream product id; that
    label is not a SourceSystem, and the port's ``contractId`` already draws
    the product -> product edge.

    The export's own properties stay; this only adds. Idempotent, and an
    author's explicit ``sourceSystemId`` / ``type`` property wins.
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list):
        return
    known = published_source_system_ids(fluid)
    for port in input_ports:
        if not isinstance(port, dict):
            continue
        props = port.get("customProperties")
        if not isinstance(props, list):
            continue
        values = {
            prop.get("property"): prop.get("value") for prop in props if isinstance(prop, Mapping)
        }
        source_system = values.get("sourceSystem")
        if not source_system or str(source_system) not in known:
            continue
        if not values.get("sourceSystemId"):
            props.append({"property": "sourceSystemId", "value": str(source_system)})
        kind = values.get("sourceKind")
        if kind and not values.get("type"):
            # Entropy's TitleCase connector names (Postgres, Kafka, …) pick the
            # icon; a raw lowercase kind renders as a generic API.
            props.append({"property": "type", "value": kind_to_dmm_type(str(kind)) or str(kind)})


# Canonical Data Mesh mapping, which is also the archetype vocabulary Entropy Data
# recognises (kebab case only; ``sourceAligned`` shows as a plain "Data Product"):
#   Bronze / SDP -> source-aligned, Silver / ADP -> aggregate, Gold / CDP -> consumer-aligned
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
_ARCHETYPES = frozenset(_ARCHETYPE_BY_PRODUCT_TYPE.values())


def _kebab(value: str) -> str:
    out = []
    for ch in value.strip():
        if ch.isupper() and out and out[-1] != "-":
            out.append("-")
        out.append("-" if ch in "_ " else ch.lower())
    return "".join(out)


def dmm_archetype(fluid: Mapping[str, Any]) -> Optional[str]:
    """The Entropy archetype of a FLUID data product, or ``None``.

    ``metadata.archetype`` when set (a known one is spelled the kebab way Entropy reads,
    ``sourceAligned`` -> ``source-aligned``; anything else goes as written), else
    ``metadata.productType`` (SDP / ADP / CDP), else ``metadata.layer``.
    """
    meta = fluid.get("metadata") if isinstance(fluid.get("metadata"), Mapping) else {}
    explicit = meta.get("archetype")
    if explicit:
        kebab = _kebab(str(explicit))
        return kebab if kebab in _ARCHETYPES else str(explicit)
    if str(fluid.get("kind") or "DataProduct").lower() != "dataproduct":
        return None
    by_type = _ARCHETYPE_BY_PRODUCT_TYPE.get(str(meta.get("productType") or "").upper())
    return by_type or _ARCHETYPE_BY_LAYER.get(str(meta.get("layer") or "").lower())


def ensure_odps_archetype_custom_property(
    odps_payload: Dict[str, Any], fluid: Mapping[str, Any]
) -> None:
    """Make ``customProperties[type]`` the product's archetype (DMM overlay only).

    ODPS v1.0.0 has no archetype field; Entropy Data reads it from this custom property
    (v1.1.0 moves it to a top-level ``type``). The ODPS render can already carry a
    ``type`` that means something else: the legacy ``metadata.product_type`` /
    ``metadata.type`` (``analytical`` / ``operational``), which Entropy would read as the
    archetype. An existing ``type`` that is an archetype is kept, spelled Entropy's way;
    one that is not is renamed ``fluidType`` when there is an archetype to send.
    """
    props = odps_payload.get("customProperties")
    if not isinstance(props, list):
        props = []
    existing = [p for p in props if isinstance(p, dict) and p.get("property") == "type"]
    for prop in existing:
        kebab = _kebab(str(prop.get("value") or ""))
        if kebab in _ARCHETYPES:
            prop["value"] = kebab
            return
    archetype = dmm_archetype(fluid)
    if not archetype:
        return
    for prop in existing:
        prop["property"] = "fluidType"
    props.append({"property": "type", "value": archetype})
    odps_payload["customProperties"] = props


__all__ = [
    "dmm_archetype",
    "ensure_odps_archetype_custom_property",
    "ensure_odps_input_port_contract_ids",
    "ensure_odps_input_port_source_system_custom_property",
    "ensure_odps_output_port_display_names",
    "is_odps_payload",
    "is_odps_spec",
    "normalize_fluid_for_odps_standard",
    "promote_input_port_native_source_system_fields",
    "published_source_system_ids",
]
