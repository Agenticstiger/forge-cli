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

from fluid_build.util.contract import consumes_to_canonical_ports

LOG = logging.getLogger("fluid.providers.datamesh_manager.odps")


def is_odps_spec(value: Optional[str]) -> bool:
    """True when ``value`` names ODPS-Bitol (or its ``opds`` legacy alias)."""
    spec = str(value or "").strip().lower()
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

    Entropy's graph uses Access resources for product-to-product
    lineage. If we keep product consumes as ODPS input ports, those
    upstream products have to be mirrored as SourceSystems and the
    UI renders duplicate graph nodes. Explicit source-system
    consumes remain as input ports.
    """
    input_ports = odps_payload.get("inputPorts")
    if not isinstance(input_ports, list) or not input_ports:
        return

    product_port_names: set[str] = set()
    for canonical in consumes_to_canonical_ports(fluid, logger=LOG):
        if not canonical.get("reference") or canonical.get("source_system_id"):
            continue
        for key in ("id", "name"):
            value = canonical.get(key)
            if value:
                product_port_names.add(str(value))

    if not product_port_names:
        return

    retained = [
        port
        for port in input_ports
        if not (isinstance(port, Mapping) and str(port.get("name", "")) in product_port_names)
    ]
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
                "Promoted input port %s contractId %r -> %r " "(product-level -> expose-level)",
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


__all__ = [
    "ensure_odps_input_port_contract_ids",
    "ensure_odps_input_port_source_system_custom_property",
    "ensure_odps_output_port_display_names",
    "is_odps_payload",
    "is_odps_spec",
    "normalize_fluid_for_odps_standard",
    "remove_odps_product_consume_input_ports",
]
