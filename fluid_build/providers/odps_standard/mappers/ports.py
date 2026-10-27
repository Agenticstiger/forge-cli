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
becomes one ``inputPorts[]`` entry. ``contractId`` is the one the expose names
for itself when it names one (``contract.id``, or the id an ODPS import
recorded), and otherwise follows the convention ``{productId}.{portName}``
(matches :mod:`fluid_build.providers.datamesh_manager.datamesh_manager`).
Either way it equals the ``id`` of the per-port ODCS contract the provider
emits as a sibling file — both sides resolve it through
:func:`fluid_build.util.contract.expose_contract_id`, and
:meth:`BitolOdpsProvider.render` refuses an export in which two ports would
resolve to one id. The equality is held by that refusal, not by the two call
sites happening to agree.

Per-port pass-throughs flow through ``expose.odps_passthrough.*`` /
``expect.odps_passthrough.*`` for round-trip:

  - ``sbom``, ``input_contracts``  (output-port only)
  - ``custom_properties``, ``authoritative_definitions``, ``tags``
  - ``port_type``                  (overrides the provider-derived type)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, Dict, List, Optional, Set, Tuple

from fluid_build.util.contract import (
    builds_to_canonical_input_ports,
    consumes_to_canonical_ports,
)

from .base import (
    ExportCtx,
    ImportCtx,
    contract_id_for_expose,
    contract_id_for_port,
    expect_passthrough,
    expose_passthrough,
    fluid_id,
    get_expect_passthrough,
    get_expose_passthrough,
)
from .types import provider_to_port_type

# Bitol ``InputPort`` has no field for a version range — ``version`` names one
# exact version — so the FLUID consume's ``versionConstraint`` rides in the one
# open slot the closed object leaves, under a name that says whose it is.
VERSION_CONSTRAINT_PROPERTY = "fluidVersionConstraint"


def _range_lower_bound(constraint: Any) -> Optional[str]:
    """The lowest version a semver range accepts, as an exact semver string.

    Only the forms ``$defs.semverRange`` documents: ``^1.2``, ``~1.4``,
    ``>=1.0.0`` and an exact version. Anything else returns ``None`` so the
    caller falls back rather than guesses — a wrong exact version in exported
    lineage is worse than the constant it replaces, because it reads as a fact
    somebody established. ``>1.0.0`` is among the refusals on purpose: it does
    not accept 1.0.0, and its true lower bound is the next version, which semver
    gives no way to name.

    Deliberately not a semver library: none of ``semver``,
    ``semantic_version`` or ``packaging`` is a declared dependency of this
    project, and the CLI's core is kept lean on purpose. ``schema_manager.VersionConstraint`` is also not reused: it rejects
    the caret outright ("Invalid FLUID version format"), does not normalise to
    three components, and is the FLUID *schema* version type — pointing it at an
    upstream data product's version would overload it and put ``--min-version``
    at risk for no gain.
    """
    if not isinstance(constraint, str):
        return None
    text = constraint.strip()
    if not text or "," in text or "||" in text or " " in text:
        return None
    for prefix in ("^", "~", ">=", "="):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    if not text or any(c in text for c in "<>=*xX^~"):
        return None
    parts = text.split(".")
    # ``isascii()`` as well as ``isdigit()``: the latter is true for Arabic-Indic
    # digits and superscripts, so ``^١.٢`` would otherwise be read as a range and
    # its "lower bound" emitted verbatim into a version field. A leading zero is
    # refused for the same reason — ``^01.2`` is a schema-valid constraint
    # (``semverRange`` carries no pattern) but ``01.2.0`` matches neither
    # ``$defs.semver`` nor any resolver, so it is worse than the fallback.
    if not 1 <= len(parts) <= 3 or not all(
        p.isascii() and p.isdigit() and (p == "0" or not p.startswith("0")) for p in parts
    ):
        return None
    # ``^2.1`` and ``~1.4`` accept 2.1.0 and 1.4.0 as their lowest member; an
    # omitted component is zero, which is how npm's range grammar — the one
    # ``$defs.semverRange``'s examples come from — reads a partial version.
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts)


# ----- FLUID → ODPS --------------------------------------------------------


def to_odps(ctx: ExportCtx) -> None:
    fluid = ctx.fluid
    product_id = fluid_id(fluid) or "product"

    # Input ports are resolved first because an output port's ``inputContracts``
    # names them: deriving that list from the already-resolved
    # ``(contractId, version)`` pairs is what keeps the two halves of one
    # document agreeing, instead of two sites each deciding a version. Only the
    # authored subset feeds the derivation — see ``_build_input_ports``. The
    # ``ctx.odps`` keys are still assigned outputPorts-first below, so the
    # emitted key order is unchanged.
    input_ports, authored_ports = _build_input_ports(fluid, ctx, product_id)
    derived_input_contracts = _input_contracts_from_ports(authored_ports)

    output_ports = []
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        port = _expose_to_output_port(expose, product_id, derived_input_contracts)
        if port:
            output_ports.append(port)
    if output_ports:
        ctx.odps["outputPorts"] = output_ports

    if input_ports:
        ctx.odps["inputPorts"] = input_ports


def _input_contracts_from_ports(
    input_ports: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The ``outputPorts[].inputContracts`` a product's own consumes imply.

    Takes the *authored* input ports only (``_build_input_ports``' second
    return value), never the full list.

    Every output port receives the same entries. ODPS models the key per port,
    but FLUID's ``consumes[]`` is product-level — ``$defs.consumeRef`` has no
    expose selector — so there is no per-expose upstream mapping to narrow it
    with, and claiming one would be invention.

    Bitol ``$defs.InputContract`` is ``additionalProperties: false`` with
    ``required: [id, version]`` and no other properties, so an entry is exactly
    the input port's resolved ``contractId`` and ``version`` — read from the
    ports rather than recomputed, so the lineage cannot name a contract or a
    version the document's own input ports do not.

    Until this existed, ``inputContracts`` came only from an imported ODPS
    document's pass-through, so a contract written in FLUID exported output
    ports that named none of their upstreams and a catalog reading lineage
    there saw a product with no dependencies at all.
    """
    contracts: List[Dict[str, Any]] = []
    seen: set = set()
    for port in input_ports:
        contract_id = port.get("contractId")
        version = port.get("version")
        if not contract_id or not version:
            continue
        key = (str(contract_id), str(version))
        if key in seen:
            continue
        seen.add(key)
        contracts.append({"id": str(contract_id), "version": str(version)})
    return contracts


def _consume_to_input_port(
    canonical: Mapping[str, Any], name: str, contract_id: Optional[str]
) -> Dict[str, Any]:
    """One Bitol ODPS ``InputPort`` from one normalized ``consumes[]`` entry.

    The version is resolved in a chain: the exact version the author states
    (``contractVersion``) wins; failing that, the lowest version the declared
    range accepts; failing that, the consume's own ``version`` or the provider
    default. The chain ENDS at that constant rather than omitting the field,
    because ODPS v1.0.0 — the default emit target — has ``version`` in
    ``InputPort``'s ``required``, so an omission would make every such export
    invalid. Before the middle step existed, a consume stating only a range had
    neither of the other two and every such export claimed ``version: 1.0.0`` —
    a pin the contract never made.

    ``versionConstraint`` itself has no field in that closed object, so it rides
    along as a ``customProperties`` entry. Dropping it left an export claiming
    the upstream is pinned to exactly one version when the contract said
    "any 1.x".
    """
    constraint = canonical.get("version_constraint")
    resolved_version = (
        canonical.get("contract_version") or _range_lower_bound(constraint) or canonical["version"]
    )
    port: Dict[str, Any] = {
        "name": name,
        "version": str(resolved_version),
        "contractId": contract_id or name,
    }
    if constraint:
        port["customProperties"] = [
            {
                "property": VERSION_CONSTRAINT_PROPERTY,
                "value": str(constraint),
                "description": (
                    "FLUID consumes[].versionConstraint — the semver range "
                    "this product accepts from the upstream contract."
                ),
            }
        ]
    return port


def consume_input_port_contract_ids(fluid: Mapping[str, Any]) -> Set[str]:
    """The ``contractId``s an export's input ports carry for ``consumes[]``.

    A publish target that strips input ports by a cruder rule than "is this a
    product consume" needs this to tell which of the ports it dropped had
    lineage *derived* from ``consumes[]`` — the only lineage
    :func:`remove_output_port_input_contracts` may remove.

    An ``expects[]`` port is the case that makes the distinction matter. It is
    dropped by a "no ``sourceSystemId``" rule just like a product consume, but
    its expose replays the upstream document's own ``inputContracts`` verbatim
    through the ODPS pass-through, and nothing else in the payload records that
    dependency — Access agreements are built from ``consumes[]``. Removing it
    would lose the upstream, not deduplicate it.
    """
    ids: Set[str] = set()
    for canonical in consumes_to_canonical_ports(fluid):
        contract_id = _input_port_contract_id(canonical)
        if contract_id:
            ids.add(str(contract_id))
    return ids


def remove_output_port_input_contracts(
    odps_payload: MutableMapping[str, Any], removed_ports: Iterable[Any]
) -> None:
    """Drop ``outputPorts[].inputContracts`` entries for removed input ports.

    The inverse of :func:`_input_contracts_from_ports`, and it lives beside it
    so the two cannot drift. A publish target that removes an input port has to
    remove the lineage entry that mirrors it: forge derives that key from the
    product's own ``consumes[]``, so one dependency is stated twice in a
    document, and removing only the port leaves the upstream still named.

    Both Data Mesh Manager publish paths need this — the
    :class:`DataMeshManagerRegistrar` that backs ``fluid publish --target
    datamesh-manager`` and ``catalog.register``, and the
    ``DataMeshManagerProvider`` behind ``fluid datamesh-manager publish``. They
    strip by different rules (a missing ``sourceSystemId`` vs. a canonical
    product-consume match), so each passes the ports it dropped and this reads
    the ``contractId``s off them. Entropy would otherwise have to mirror each
    upstream product as a SourceSystem to resolve the reference, and render the
    duplicate graph nodes the strip exists to prevent.

    A dropped port with no ``contractId`` needs no action: it never produced a
    lineage entry, because :func:`_input_contracts_from_ports` skips it.
    """
    contract_ids = {
        str(port["contractId"])
        for port in removed_ports
        if isinstance(port, Mapping) and port.get("contractId")
    }
    if not contract_ids:
        return
    for port in odps_payload.get("outputPorts") or []:
        if not isinstance(port, MutableMapping):
            continue
        entries = port.get("inputContracts")
        if not isinstance(entries, list):
            continue
        kept = [
            entry
            for entry in entries
            if not (isinstance(entry, Mapping) and str(entry.get("id")) in contract_ids)
        ]
        if kept:
            port["inputContracts"] = kept
        else:
            port.pop("inputContracts", None)


def _build_input_ports(
    fluid: Mapping[str, Any], ctx: ExportCtx, product_id: str
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Assemble Bitol ODPS v1.0.0 ``InputPort`` list from any FLUID lineage.

    Three lineage sources are handled, in priority order:

    1. ``consumes[]`` (FLUID 0.7.2+ canonical lineage) — normalized via
       :func:`consumes_to_canonical_ports`. Strips FLUID-specific fields
       (``id``, ``description``, ``reference``, ``required``, ``sourceSystemId``)
       since v1.0.0 ``InputPort`` is ``additionalProperties: false``, and
       carries ``versionConstraint`` — which that closed object has no field
       for — as a ``customProperties`` entry. ``contractId`` comes from
       :func:`_input_port_contract_id`.
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

    Returns ``(input_ports, authored_ports)``. ``authored_ports`` is the path-1
    subset — the upstream *contracts* this contract names, and the only lineage
    ``outputPorts[].inputContracts`` may be derived from.

    Path 2 is excluded because its ``contractId`` is synthesized as
    ``<kind>://<database>/<stream>`` for a source that has no contract at all
    (see :func:`builds_to_canonical_input_ports`). That synthesis exists to
    populate ``inputPorts[].contractId``, which ODPS v1.0.0 requires;
    ``inputContracts`` is optional, so naming an id no catalog can resolve is
    worse than naming nothing — the failure
    :func:`_input_port_contract_id` describes: an id that resolves in no
    catalog following the reference.

    Path 3 is excluded because ``expects[]`` is also the carrier an imported
    ODPS document's input ports arrive on, and an imported document's
    output-port lineage is whatever that document said — including nothing.
    Deriving it there made import → export add a dependency the author never
    wrote, which the round-trip tests caught.
    """
    input_ports: List[Dict[str, Any]] = []
    authored_ports: List[Dict[str, Any]] = []
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
        contract_id = _input_port_contract_id(canonical)
        dedup_key = (canonical["name"], contract_id)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        name = _unique_name(canonical["name"], canonical.get("reference"))
        seen_names.add(name)
        consume_port = _consume_to_input_port(canonical, name, contract_id)
        input_ports.append(consume_port)
        authored_ports.append(consume_port)

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

    return input_ports, authored_ports


def _input_port_contract_id(canonical: Mapping[str, Any]) -> Optional[str]:
    """The contract an upstream consume points at.

    An explicit ``consumes[].contractId`` wins: that is an upstream which has
    an id of its own — a product not built with FLUID, registered under
    something FLUID did not derive. Otherwise the id is the one forge itself
    gives that upstream port's ODCS contract, ``<productId>.<exposeId>``.
    Exporting the bare ``productId`` named a contract that exists nowhere, so
    lineage between two FLUID products resolved in no catalog that follows the
    reference.
    """
    explicit = canonical.get("contract_id")
    if explicit:
        return str(explicit)
    reference = canonical.get("reference")
    upstream_port = canonical.get("id")
    if reference and upstream_port:
        return contract_id_for_port(str(reference), str(upstream_port))
    if reference:
        return str(reference)
    name = canonical.get("name")
    return str(name) if name else None


def _expose_to_output_port(
    expose: Mapping[str, Any],
    product_id: str,
    derived_input_contracts: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
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
        "contractId": contract_id_for_expose(expose, product_id, name),
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
        # An imported ODPS document's own lineage wins, so import → export stays
        # a fixed point: the upstream stated these, and re-deriving them from our
        # consumes could name different contracts.
        port["inputContracts"] = list(pt["input_contracts"])
    elif derived_input_contracts:
        # Otherwise the product's own consumes ARE its lineage. A
        # FLUID-authored contract used to export output ports naming no
        # upstreams at all.
        port["inputContracts"] = [dict(entry) for entry in derived_input_contracts]

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
