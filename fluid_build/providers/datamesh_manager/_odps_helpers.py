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
* :func:`remove_odps_product_consume_input_ports` — drop
  product-level consumes from ODPS input ports (Access resources
  carry that lineage instead).
* :func:`ensure_odps_input_port_contract_ids` — backfill / promote
  ``contractId`` from FLUID ``consumes``.
* :func:`ensure_odps_input_port_source_system_custom_property` —
  legacy DMM compatibility for deployments that still need a
  ``sourceSystem`` custom property.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional

from fluid_build.util.contract import consumes_to_canonical_ports, kind_to_dmm_type

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


def remove_odps_product_consume_input_ports(
    odps_payload: Dict[str, Any], fluid: Mapping[str, Any]
) -> None:
    """Remove product-to-product consumes from ODPS input ports.

    Entropy's graph uses Access resources for product-to-product lineage.
    If we keep product consumes as ODPS input ports, those upstream
    products have to be mirrored as SourceSystems and the UI renders
    duplicate graph nodes. Explicit source-system consumes remain as input
    ports.

    Matching strategy: we identify product-consume ports by **both**
    ``(id, name)`` (the canonical exposeId — matches the inputPort before
    the dedup-rename in :func:`fluid_build.providers.odps_standard.mappers
    .ports.map_input_ports`) **and** ``contractId == upstream productId``
    (which survives the dedup-rename intact). The dedup-rename happens
    when two consumes share an exposeId — the second gets a productId-tail
    prefix on its name but the contractId points at the original upstream
    productId. Without the contractId check, dedup-renamed ports leak
    through as inputPorts on gold products that consume two ports from
    the same silver upstream.
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list) or not input_ports:
        return

    product_port_names: set[str] = set()
    product_contract_ids: set[str] = set()
    for canonical in consumes_to_canonical_ports(fluid, logger=LOG):
        reference = canonical.get("reference")
        if not reference or canonical.get("source_system_id"):
            continue
        for key in ("id", "name"):
            value = canonical.get(key)
            if value:
                product_port_names.add(str(value))
        product_contract_ids.add(str(reference))

    if not product_port_names and not product_contract_ids:
        return

    def _is_product_consume(port: Any) -> bool:
        if not isinstance(port, Mapping):
            return False
        if str(port.get("name", "")) in product_port_names:
            return True
        contract_id = str(port.get("contractId", ""))
        # contractId equals the upstream productId for product-to-product
        # consumes (set by the bitol mapper). May also be the per-port form
        # ``{productId}.{exposeId}`` — strip the suffix and recheck.
        if contract_id in product_contract_ids:
            return True
        head = contract_id.rsplit(".", 1)[0] if "." in contract_id else contract_id
        return head in product_contract_ids

    retained = [port for port in input_ports if not _is_product_consume(port)]
    if retained:
        odps_payload["inputPorts"] = retained
    else:
        odps_payload.pop("inputPorts", None)


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
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list) or not input_ports:
        return

    canonical_ports = consumes_to_canonical_ports(fluid, logger=LOG)
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

    canonical_ports = consumes_to_canonical_ports(fluid, logger=LOG)
    source_system_by_port: Dict[str, str] = {}
    for canonical in canonical_ports:
        port_id = canonical.get("id")
        if not port_id:
            continue
        sys_id = canonical.get("source_system_id")
        if not sys_id and default_from_reference:
            sys_id = canonical.get("reference")
        if sys_id:
            source_system_by_port[str(port_id)] = str(sys_id)

    for port in input_ports:
        if not isinstance(port, dict):
            continue

        props = port.get("customProperties")
        if not isinstance(props, list):
            props = []
        if any(isinstance(p, Mapping) and p.get("property") == "sourceSystem" for p in props):
            continue

        port_id = port.get("id") or port.get("name")
        fallback = source_system_by_port.get(str(port_id)) if port_id else None
        if not fallback and default_from_reference:
            fallback = port.get("reference")
        if not fallback:
            continue

        props.append({"property": "sourceSystem", "value": str(fallback)})
        port["customProperties"] = props


def promote_input_port_native_source_system_fields(
    odps_payload: Dict[str, Any],
) -> None:
    """Lift ODPS ``customProperties[sourceSystem|sourceKind]`` into native
    DMM fields on each input port (``sourceSystemId``, ``type``).

    Why this is an overlay rather than baked into the standalone artifact:
    the ODPS-Bitol v1.0.0 ``InputPort`` schema is closed
    (``additionalProperties: false``) — only ``name, version, contractId,
    tags, customProperties, authoritativeDefinitions`` are permitted.
    ``sourceSystemId`` and ``type`` would fail the spec's JSON-schema
    validator if emitted by the standalone exporter.

    DMM (Entropy Data CE), however, ACCEPTS those native fields on
    InputPort and uses them to render lineage edges in the UI. So:

      * The standalone ``*.odps-bitol.yaml`` artifact stays spec-clean
        with source-system info in ``customProperties[]`` only.
      * The DMM POST payload runs through this overlay, which copies the
        ``sourceSystem`` / ``sourceKind`` custom property values into the
        native fields. DMM's UI then renders the lineage edge to the
        registered SourceSystem entity.

    The customProperties stay in place after promotion — they're the
    canonical source of truth, and DMM tolerates duplication. Idempotent
    (won't overwrite an explicit ``sourceSystemId`` set by the author).
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list):
        return
    promoted_count = 0
    for port in input_ports:
        if not isinstance(port, dict):
            continue
        custom_props = port.get("customProperties") or []
        if not isinstance(custom_props, list):
            continue
        for prop in custom_props:
            if not isinstance(prop, Mapping):
                continue
            name = prop.get("property")
            value = prop.get("value")
            if name == "sourceSystem" and value and not port.get("sourceSystemId"):
                port["sourceSystemId"] = str(value)
                promoted_count += 1
            elif name == "sourceKind" and value and not port.get("type"):
                # Use DMM's TitleCase enum (Postgres / Kafka / Snowflake / …)
                # so the lineage UI renders the correct connector icon —
                # raw lowercase kinds fall back to "API" in DMM's renderer.
                port["type"] = kind_to_dmm_type(str(value)) or str(value)
    if promoted_count > 0:
        LOG.debug(
            "Promoted source-system fields on %d input port(s) for DMM payload "
            "(DMM ODPS-Bitol payloads strip unknown native fields; lineage "
            "still flows via customProperties[sourceSystem])",
            promoted_count,
        )


__all__ = [
    "ensure_odps_input_port_contract_ids",
    "ensure_odps_input_port_source_system_custom_property",
    "ensure_odps_output_port_display_names",
    "is_odps_payload",
    "is_odps_spec",
    "normalize_fluid_for_odps_standard",
    "promote_input_port_native_source_system_fields",
    "remove_odps_product_consume_input_ports",
]
