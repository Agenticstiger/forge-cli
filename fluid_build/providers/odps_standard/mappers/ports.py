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

"""Input / output port mapping.

Each FLUID expose becomes one ``outputPorts[]`` entry; each FLUID expect
becomes one ``inputPorts[]`` entry. ``contractId`` follows the convention
``{productId}.{portName}`` (matches :mod:`fluid_build.providers.datamesh_manager.datamesh_manager`)
and equals the ``id`` of the per-port ODCS contract the provider emits as a
sibling file.

Per-port pass-throughs flow through ``expose.odps_passthrough.*`` /
``expect.odps_passthrough.*`` for round-trip:

  - ``sbom``, ``input_contracts``  (output-port only)
  - ``custom_properties``, ``authoritative_definitions``, ``tags``
  - ``port_type``                  (overrides the provider-derived type)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fluid_build.util.contract import (
    builds_to_canonical_input_ports,
    consumes_to_canonical_ports,
)

from .base import (
    ExportCtx,
    ImportCtx,
    contract_id_for_port,
    expect_passthrough,
    expose_passthrough,
    fluid_id,
    get_expect_passthrough,
    get_expose_passthrough,
)
from .types import provider_to_port_type

# ----- FLUID → ODPS --------------------------------------------------------


def to_odps(ctx: ExportCtx) -> None:
    fluid = ctx.fluid
    product_id = fluid_id(fluid) or "product"

    output_ports = []
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        port = _expose_to_output_port(expose, product_id)
        if port:
            output_ports.append(port)
    if output_ports:
        ctx.odps["outputPorts"] = output_ports

    input_ports = _build_input_ports(fluid, ctx, product_id)
    if input_ports:
        ctx.odps["inputPorts"] = input_ports


def _build_input_ports(
    fluid: Mapping[str, Any], ctx: ExportCtx, product_id: str
) -> List[Dict[str, Any]]:
    """Assemble Bitol ODPS v1.0.0 ``InputPort`` list from any FLUID lineage.

    Three lineage sources are handled, in priority order:

    1. ``consumes[]`` (FLUID 0.7.2+ canonical lineage) — normalized via
       :func:`consumes_to_canonical_ports`. Strips FLUID-specific fields
       (``id``, ``description``, ``reference``, ``required``, ``sourceSystemId``)
       since v1.0.0 ``InputPort`` is ``additionalProperties: false``.
       Synthesizes ``contractId`` in priority order: explicit ``contract_id``
       → ``reference`` → ``name``.
    2. ``builds[].properties.source`` (SDP convention) — normalized via
       :func:`builds_to_canonical_input_ports`. Emits per-source-stream
       ports with synthetic ``contractId`` and ``customProperties`` for
       downstream DMM source-system lineage.
    3. ``expects[]`` (FLUID 0.7.1 legacy) — handled inline via
       :func:`_expect_to_input_port` for back-compat.

    De-duped by ``(name, contract_id)`` so authors who wire both ``consumes``
    and ``builds.source`` for the same upstream get one InputPort, not two —
    but two ``consumes[]`` that target *different* upstream products with the
    same ``exposeId`` (e.g. both ``silver.b1`` and ``silver.b2`` expose
    ``data_analytics_platform``) each produce their own InputPort with a
    disambiguating name (``b1__data_analytics_platform`` / ``b2__…``). Before
    this dedup keyed by name alone, the second consume was silently dropped
    and downstream catalogs lost half the lineage.
    """
    input_ports: List[Dict[str, Any]] = []
    seen: set = set()  # ``(name, contract_id)`` tuples — true dedup key.
    seen_names: set = set()  # ``name`` only — drives the collision-rename path.
    logger = getattr(ctx, "logger", None)
    default_version = (ctx.options or {}).get("default_port_version", "1.0.0")

    def _unique_name(base: str, ref: Optional[str]) -> str:
        """Return ``base`` if not yet used, otherwise a disambiguated form
        derived from ``ref`` (the upstream productId). Falls back to a
        numeric suffix when ``ref`` is missing or already collides too."""
        if base not in seen_names:
            return base
        if ref:
            # productId is dot-segmented; take the tail segment as the
            # disambiguator so the resulting name stays readable.
            tail = str(ref).rsplit(".", 1)[-1]
            candidate = f"{tail}__{base}" if tail and tail != base else f"{ref}__{base}"
            if candidate not in seen_names:
                return candidate
        # Last resort: numeric suffix.
        i = 2
        while f"{base}_{i}" in seen_names:
            i += 1
        return f"{base}_{i}"

    # Path 1 — consumes[]
    for canonical in consumes_to_canonical_ports(
        fluid, default_version=default_version, logger=logger
    ):
        contract_id = (
            canonical.get("contract_id") or canonical.get("reference") or canonical.get("name")
        )
        dedup_key = (canonical["name"], str(contract_id) if contract_id else None)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        name = _unique_name(canonical["name"], canonical.get("reference"))
        seen_names.add(name)
        input_ports.append(
            {
                "name": name,
                "version": canonical["version"],
                "contractId": str(contract_id) if contract_id else name,
            }
        )

    # Path 2 — builds[].properties.source (SDP source streams)
    for canonical in builds_to_canonical_input_ports(
        fluid, default_version=default_version, logger=logger
    ):
        name = canonical["name"]
        if name in seen_names:
            continue
        seen_names.add(name)
        port: Dict[str, Any] = {
            "name": name,
            "version": canonical["version"],
            "contractId": str(canonical["contract_id"]),
        }
        custom_props: List[Dict[str, Any]] = []
        sys_id = canonical.get("source_system_id")
        if sys_id:
            custom_props.append({"property": "sourceSystem", "value": sys_id})
        kind = canonical.get("kind")
        if kind:
            custom_props.append({"property": "sourceKind", "value": kind})
        if custom_props:
            port["customProperties"] = custom_props
        input_ports.append(port)

    # Path 3 — expects[] (FLUID 0.7.1 legacy)
    for expect in fluid.get("expects") or []:
        if not isinstance(expect, Mapping):
            continue
        port_obj = _expect_to_input_port(expect, product_id)
        if port_obj and port_obj.get("name") not in seen_names:
            seen_names.add(port_obj["name"])
            input_ports.append(port_obj)

    return input_ports


def _expose_to_output_port(expose: Mapping[str, Any], product_id: str) -> Optional[Dict[str, Any]]:
    from fluid_build.providers.base import ProviderError

    # Bitol ODPS v1.0.0 OutputPort requires ``name``. Accept any of
    # FLUID's three name-bearing fields (LLM-generated contracts vary
    # by template): ``exposeId`` (canonical), ``id`` (FLUID 0.7.1
    # legacy), ``name`` (forge AI default when neither is set).
    name = expose.get("exposeId") or expose.get("id") or expose.get("name")
    if not name:
        raise ProviderError(
            "FLUID expose has no usable name — Bitol ODPS v1.0.0 "
            "OutputPort requires a stable port name. Set expose.exposeId, "
            "expose.id, or expose.name."
        )

    port: Dict[str, Any] = {
        "name": name,
        "version": str(expose.get("version", 1)),
        "contractId": contract_id_for_port(product_id, name),
    }
    if expose.get("description"):
        port["description"] = expose["description"]

    pt = get_expose_passthrough(expose)
    port_type = pt.get("port_type") or _port_type_from_expose(expose)
    if port_type:
        port["type"] = port_type

    if "sbom" in pt:
        port["sbom"] = list(pt["sbom"])
    if "input_contracts" in pt:
        port["inputContracts"] = list(pt["input_contracts"])

    if expose.get("tags"):
        port["tags"] = list(expose["tags"])
    elif "tags" in pt:
        port["tags"] = list(pt["tags"])
    if "custom_properties" in pt:
        port["customProperties"] = list(pt["custom_properties"])
    if "authoritative_definitions" in pt:
        port["authoritativeDefinitions"] = list(pt["authoritative_definitions"])
    return port


def _expect_to_input_port(expect: Mapping[str, Any], product_id: str) -> Optional[Dict[str, Any]]:
    name = expect.get("id")
    if not name:
        return None

    pt = get_expect_passthrough(expect)

    # Prefer an explicit contractId (set when imported); otherwise mint one
    # consistent with the product. Bitol input ports must reference an
    # existing contract — for a fresh FLUID we synthesise a placeholder.
    contract_id = (
        expect.get("contractId")
        or pt.get("contract_id")
        or contract_id_for_port(product_id, f"input.{name}")
    )

    port: Dict[str, Any] = {
        "name": name,
        "version": str(expect.get("version", pt.get("version", 1))),
        "contractId": contract_id,
    }
    if expect.get("tags"):
        port["tags"] = list(expect["tags"])
    elif "tags" in pt:
        port["tags"] = list(pt["tags"])
    if "custom_properties" in pt:
        port["customProperties"] = list(pt["custom_properties"])
    if "authoritative_definitions" in pt:
        port["authoritativeDefinitions"] = list(pt["authoritative_definitions"])
    return port


def _port_type_from_expose(expose: Mapping[str, Any]) -> Optional[str]:
    binding = expose.get("binding")
    if isinstance(binding, Mapping):
        platform = binding.get("platform") or binding.get("provider")
        if platform:
            return provider_to_port_type(platform)
    provider = expose.get("provider")
    if provider:
        return provider_to_port_type(provider)
    return None


# ----- ODPS → FLUID (Phase 3) ---------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    """Skeleton expose/expect generation.

    The full ODCS-resolution step (which actually populates each expose's
    schema/quality/qos) lives in the provider's ``import_contract`` method —
    it needs the :class:`ContractResolver` instance. Here we just stub each
    port with its identifying fields and pass-through metadata, so the
    resolver step can fill in the rest.
    """
    odps = ctx.odps
    fluid = ctx.fluid

    exposes = fluid.setdefault("exposes", [])
    for port in odps.get("outputPorts") or []:
        if not isinstance(port, Mapping):
            continue
        exposes.append(_output_port_to_expose_stub(port))

    expects = fluid.setdefault("expects", [])
    for port in odps.get("inputPorts") or []:
        if not isinstance(port, Mapping):
            continue
        expects.append(_input_port_to_expect_stub(port))


def _output_port_to_expose_stub(port: Mapping[str, Any]) -> Dict[str, Any]:
    name = port.get("name") or "output"
    stub: Dict[str, Any] = {
        "id": name,
        "exposeId": name,
        "version": str(port.get("version", 1)),
    }
    if port.get("description"):
        stub["description"] = port["description"]
    if port.get("tags"):
        stub["tags"] = list(port["tags"])

    pt = expose_passthrough(stub)
    pt["contract_id"] = port.get("contractId")
    if port.get("type"):
        pt["port_type"] = port["type"]
    if port.get("sbom"):
        pt["sbom"] = list(port["sbom"])
    if port.get("inputContracts"):
        pt["input_contracts"] = list(port["inputContracts"])
    if port.get("customProperties"):
        pt["custom_properties"] = list(port["customProperties"])
    if port.get("authoritativeDefinitions"):
        pt["authoritative_definitions"] = list(port["authoritativeDefinitions"])
    return stub


def _input_port_to_expect_stub(port: Mapping[str, Any]) -> Dict[str, Any]:
    name = port.get("name") or "input"
    stub: Dict[str, Any] = {
        "id": name,
        "version": str(port.get("version", 1)),
        "contractId": port.get("contractId"),
    }
    if port.get("tags"):
        stub["tags"] = list(port["tags"])
    pt = expect_passthrough(stub)
    if port.get("customProperties"):
        pt["custom_properties"] = list(port["customProperties"])
    if port.get("authoritativeDefinitions"):
        pt["authoritative_definitions"] = list(port["authoritativeDefinitions"])
    return stub
