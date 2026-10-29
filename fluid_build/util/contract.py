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

"""
FLUID Contract Field Adapter

Provides utilities for accessing contract fields. These helpers read
the fluid-schema-0.7.x contract shape only; pre-0.7 schemas are not
supported.
"""

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple


def get_expose_id(expose: Mapping[str, Any]) -> Optional[str]:
    """
    Get the expose ID from an expose object (schema 0.7.x: ``exposeId``).

    Args:
        expose: The expose dictionary

    Returns:
        The expose ID or None
    """
    return expose.get("exposeId")


def get_expose_kind(expose: Mapping[str, Any]) -> Optional[str]:
    """
    Get the expose kind from an expose object (schema 0.7.x: ``kind``).

    Args:
        expose: The expose dictionary

    Returns:
        The expose kind or None
    """
    return expose.get("kind")


def get_expose_binding(expose: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get the expose binding from an expose object (schema 0.7.x:
    ``binding`` — an object with platform, location, etc.).

    Args:
        expose: The expose dictionary

    Returns:
        The binding object or None
    """
    return expose.get("binding")


def get_expose_location(expose: Mapping[str, Any]) -> Optional[str]:
    """
    Get the physical location string from an expose object (schema
    0.7.x: ``binding.location``).

    Args:
        expose: The expose dictionary

    Returns:
        The location string or None
    """
    binding = get_expose_binding(expose)
    if binding and isinstance(binding, dict):
        return binding.get("location")

    return None


def get_builds(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """
    Get the builds array from a contract (schema 0.7.x: ``builds``).

    Args:
        contract: The contract dictionary

    Returns:
        List of build objects (may be empty)
    """
    builds = contract.get("builds")
    if builds and isinstance(builds, list):
        return builds

    return []


def get_primary_build(contract: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get the primary/first build from a contract.

    Args:
        contract: The contract dictionary

    Returns:
        The first build object or None
    """
    builds = get_builds(contract)
    return builds[0] if builds else None


def get_build_engine(build: Mapping[str, Any]) -> Optional[str]:
    """
    Get the build engine from a build object (schema 0.7.x: ``engine``).

    Args:
        build: The build dictionary

    Returns:
        The engine name (e.g., 'dbt', 'dataform', 'spark')
    """
    return build.get("engine")


#: Engines and code languages whose jobs run on a platform outside forge-cli,
#: such as the Flink Kubernetes Operator or AWS Managed Service for Apache
#: Flink. forge-cli validates and plans such a build but ships no runner for
#: it, and never runs its ``properties.sql`` as its own SQL. ``engine: flink``
#: is new in schema 0.7.6; ``properties.language: flink_sql`` is how 0.7.5 and
#: older contracts declare the same job.
EXTERNALLY_RUN_ENGINES = frozenset({"flink"})
EXTERNALLY_RUN_LANGUAGES = frozenset({"flink_sql"})


def is_externally_run_build(build: Mapping[str, Any]) -> bool:
    """True when the build's job runs on a platform forge-cli does not drive:
    its engine is ``flink``, or its ``properties.language`` is ``flink_sql``."""
    engine = get_build_engine(build)
    if isinstance(engine, str) and engine.strip().lower() in EXTERNALLY_RUN_ENGINES:
        return True
    props = build.get("properties")
    language = props.get("language") if isinstance(props, Mapping) else None
    return isinstance(language, str) and language.strip().lower() in EXTERNALLY_RUN_LANGUAGES


def builds_writing_expose(expose_id: Any, builds: Any) -> List[Mapping[str, Any]]:
    """The builds that write the expose ``expose_id``: those whose ``outputs``
    name it or, when none does, those that declare no ``outputs`` (a build that
    declares them has said what it writes)."""
    dict_builds = [b for b in builds or [] if isinstance(b, Mapping)]
    return [
        b for b in dict_builds if isinstance(b.get("outputs"), list) and expose_id in b["outputs"]
    ] or [b for b in dict_builds if not isinstance(b.get("outputs"), list)]


def is_written_outside_forge(expose_id: Any, builds: Any) -> bool:
    """True when a build that runs outside forge-cli writes the expose, or may
    (:func:`builds_writing_expose`): forge-cli lands nothing under its name."""
    return any(is_externally_run_build(b) for b in builds_writing_expose(expose_id, builds))


def describe_externally_run_build(build: Mapping[str, Any]) -> str:
    """What makes the build run elsewhere, for messages: ``engine 'flink'`` or
    ``language 'flink_sql'``."""
    engine = get_build_engine(build)
    if isinstance(engine, str) and engine.strip().lower() in EXTERNALLY_RUN_ENGINES:
        return f"engine '{engine}'"
    props = build.get("properties")
    return f"language '{props.get('language') if isinstance(props, Mapping) else None}'"


def is_continuous_build(build: Mapping[str, Any]) -> bool:
    """True when the build's ``execution.trigger.type`` is ``continuous`` (new in
    schema 0.7.6): its platform keeps the job running, so no scheduler starts
    its runs. Named after Databricks Jobs' continuous trigger, not Airflow's
    ``@continuous``, which starts batch runs back to back."""
    execution = build.get("execution")
    trigger = execution.get("trigger") if isinstance(execution, Mapping) else None
    kind = trigger.get("type") if isinstance(trigger, Mapping) else None
    return isinstance(kind, str) and kind.strip().lower() == "continuous"


def is_unscheduled_build(build: Mapping[str, Any]) -> bool:
    """True when no scheduler starts the build's runs: its job runs outside
    forge-cli (:func:`is_externally_run_build`), or continuously
    (:func:`is_continuous_build`)."""
    return is_externally_run_build(build) or is_continuous_build(build)


def has_only_unscheduled_builds(contract: Mapping[str, Any]) -> bool:
    """True when the contract has builds and no scheduler starts any of them:
    each runs outside forge-cli, or continuously (:func:`is_unscheduled_build`).
    A contract with no builds is False: no build of it was left out."""
    builds = [b for b in get_builds(contract) if isinstance(b, Mapping)]
    return bool(builds) and all(is_unscheduled_build(b) for b in builds)


def get_contract_version(contract: Mapping[str, Any]) -> Optional[str]:
    """
    Get the FLUID schema version from a contract.

    Args:
        contract: The contract dictionary

    Returns:
        The fluidVersion string (e.g., '0.7.3')
    """
    return contract.get("fluidVersion")


def get_expose_contract(expose: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get the contract section from an expose object.

    In fluid-schema-0.7.x, ``schema`` and ``dq`` are nested under a
    ``contract`` key on each expose.

    Args:
        expose: The expose dictionary

    Returns:
        The contract section object or None
    """
    return expose.get("contract")


# The per-expose pass-through bucket the Bitol ODPS importer records an
# imported output port's own ``contractId`` in, and the root extension object
# the same importer parks that bucket in on its way out of the import. Spelled
# here rather than imported from that provider because every caller below has
# to read the same keys, and the ODCS exporter has no business importing the
# ODPS one.
_ODPS_PASSTHROUGH_KEY = "odps_passthrough"
_EXTENSIONS_KEY = "extensions"
_ODPS_EXTENSION_NAMESPACE = "odps"


def _expose_key(expose: Mapping[str, Any]) -> Optional[str]:
    """The name an expose answers to, in the order every exporter accepts it.

    ``exposeId`` is canonical; ``id`` is the 0.7.1 legacy spelling and ``name``
    is what the forge AI templates emit. The ODPS output-port mapper accepts
    all three for the port name, so the derived contract id and the ODPS
    round-trip bucket have to key off the same lookup or they would disagree
    about which expose a bucket entry belongs to.
    """
    for key in ("exposeId", "id", "name"):
        value = expose.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _imported_contract_id(contract: Mapping[str, Any], expose_key: Optional[str]) -> Optional[str]:
    """The ``contractId`` an ODPS import recorded for ``expose_key``, in the
    root extension object, or ``None``.

    An ODPS import parks its per-expose round-trip state in root
    ``extensions.odps.exposes.<exposeId>.expose`` — the one object the FLUID
    schema declares open, and therefore the only legal home for it.
    ``BitolOdpsProvider.render`` pushes that state back down into the inline
    per-expose bucket before it maps anything, but four other entry points
    render a per-expose ODCS straight from the contract and never do, so
    reading only the inline bucket made an imported id resolve for the ODPS
    exporter and silently re-derive for everybody else. Reading the extension
    here is what makes the resolution a property of the contract rather than of
    whoever happened to rehydrate it first.
    """
    if not expose_key:
        return None
    extensions = contract.get(_EXTENSIONS_KEY)
    if not isinstance(extensions, Mapping):
        return None
    bucket = extensions.get(_ODPS_EXTENSION_NAMESPACE)
    if not isinstance(bucket, Mapping):
        return None
    exposes = bucket.get("exposes")
    if not isinstance(exposes, Mapping):
        return None
    entry = exposes.get(expose_key)
    if not isinstance(entry, Mapping):
        return None
    inner = entry.get("expose")
    if not isinstance(inner, Mapping):
        return None
    return _as_contract_id(inner.get("contract_id"))


def _as_contract_id(value: Any) -> Optional[str]:
    """``value`` when it is a usable contract id, else ``None``.

    A contract id is a non-empty string: it becomes the ODCS document's ``id``
    and a URL path segment in every catalog that stores it. ``str(value)``
    would coerce a YAML number or a mapping into something that looks like an
    id and is not — the shapes ``exposeContract.id`` already rejects — so a
    non-string is refused here too and the port keeps its derived id.
    """
    return value if isinstance(value, str) and value else None


def explicit_expose_contract_id(
    expose: Mapping[str, Any],
    contract: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Return the contract id an expose names for itself, or ``None``.

    Two exporters mint a per-port contract id — the ODCS document's ``id`` and
    the ODPS output port's ``contractId``, which Bitol fragments mode requires
    to be the same string — and both derive ``<product id>.<exposeId>`` when
    the contract names none. Every caller resolves the named one here, so no
    two of them can disagree about what a port's contract is called.

    Three places hold one, in priority order:

    1. ``contract.id`` (fluid-schema 0.7.6). What an author pins when the
       derived id is not the one consumers or a catalog know the port by —
       without it, renaming the product or the expose silently renames the
       contract and breaks every reference to it. An ODPS import writes the
       imported port's own ``contractId`` here too, whenever the version the
       source document recorded declares the field.
    2. The ODPS importer's per-expose pass-through record of the ``contractId``
       the imported port already answered to, inline on the expose. Still the
       only home for it when the import declares an older version:
       ``exposeContract`` is closed before 0.7.6, so writing the field there
       would make the whole imported contract fail ``fluid validate``.
    3. The same record where an import actually leaves it on disk — root
       ``extensions.odps`` — which is why ``contract`` is worth passing. Only
       ``BitolOdpsProvider.render`` rehydrates (2) from it, so a caller that
       omits ``contract`` gets the imported id from an ODPS export and the
       derived one from every other entry point. Pass the contract.
    """
    expose_contract = expose.get("contract")
    if isinstance(expose_contract, Mapping):
        pinned = _as_contract_id(expose_contract.get("id"))
        if pinned:
            return pinned
    passthrough = expose.get(_ODPS_PASSTHROUGH_KEY)
    if isinstance(passthrough, Mapping):
        inline = _as_contract_id(passthrough.get("contract_id"))
        if inline:
            return inline
    if contract is not None:
        return _imported_contract_id(contract, _expose_key(expose))
    return None


def derived_contract_id(product_id: Any, port_name: str) -> str:
    """``<product id>.<port name>`` — the contract id an exporter mints for a
    port that names none.

    The one place this convention is spelled. ``fluid validate`` has to derive
    exactly the string the exporters will, or its collision check compares a
    pinned id against an id nothing ever publishes; a second copy of the
    f-string is how the two drift apart.
    """
    return f"{product_id}.{port_name}"


def expose_contract_id(
    contract: Mapping[str, Any],
    expose: Mapping[str, Any],
    *,
    expose_id: Optional[str] = None,
    product_id: Optional[str] = None,
) -> Optional[str]:
    """The contract id one output port will be published under: the id the
    expose names for itself, else the derived ``<product id>.<exposeId>``.

    The fallback-included form of :func:`explicit_expose_contract_id`, and the
    one every id-minting caller should use — the per-port ODCS ``id``, the ODPS
    output port's ``contractId``, the Data Mesh Manager publish url, and
    ``fluid validate``'s collision check, which can only compare a pinned id
    against a derived one if it derives them by the same rule. ``None`` only
    when the port names no id and the contract has no id to derive from, which
    is what leaves a per-port ODCS without a scoped ``id`` today.

    ``product_id`` overrides the half of the derived id that comes from the
    contract. Data Mesh Manager publishes under the id of the product it is
    writing (``dp["id"]``, which a caller may have set), not necessarily the
    contract's own, and its derived ids have to keep matching the url its
    companion ODCS contracts are PUT to.
    """
    from fluid_build.providers._mapper_common import fluid_id

    explicit = explicit_expose_contract_id(expose, contract)
    if explicit:
        return explicit
    port_name = expose_id or _expose_key(expose)
    product_id = product_id or fluid_id(contract)
    if not port_name or not product_id:
        return None
    return derived_contract_id(product_id, port_name)


def expose_contract_id_for_ref(
    contract: Mapping[str, Any],
    expose_ref: str,
    *,
    product_id: Optional[str] = None,
) -> Optional[str]:
    """:func:`expose_contract_id` for a caller that holds the port's name
    rather than the expose object.

    The Data Mesh Manager publish flow walks the ODPS payload's output ports
    and the contract's exposes in separate loops, so each site had only the
    name — and each derived ``{product_id}.{name}`` for itself, which
    overwrote a pinned contractId on the wire and PUT the companion ODCS body
    under an id its own output port did not reference. Resolving by name here
    lets those sites share the one resolution without reaching into an ODPS
    mapper for it. ``None`` when no expose answers to ``expose_ref``, which
    leaves the caller's own ``contractId`` untouched.
    """
    if not expose_ref:
        return None
    for expose in get_exposes(contract):
        if not isinstance(expose, Mapping):
            continue
        if expose_ref in (expose.get("exposeId"), expose.get("id"), expose.get("name")):
            return expose_contract_id(contract, expose, expose_id=expose_ref, product_id=product_id)
    return None


def _collision_label(index: int, expose: Mapping[str, Any]) -> str:
    """How one expose is named in a collision message.

    The same lookup the exporters use for the output port's name, so the two
    labels in the message are the strings the author will find in the
    document. A port that names itself nothing at all is pointed at by its
    array position, because there is nothing else to call it.
    """
    return _expose_key(expose) or f"exposes[{index}]"


def _collision_message(
    contract_id: str,
    first: Tuple[str, bool],
    second: Tuple[str, bool],
) -> str:
    """One collision, phrased so the two moves that resolve it both fit.

    ``first``/``second`` are ``(label, pinned)`` in document order. Which
    advice is right depends on whether either side pins the id: an un-pinned
    expose has no ``contract.id`` to change, so "give each expose its own
    contract.id" would send the author to a field that is not in the document
    — the move for that side is to rename the expose, since the id is derived
    from its name.
    """
    first_label, first_pinned = first
    second_label, second_pinned = second
    shared = (
        f"contract id {contract_id!r} is used by exposes {first_label!r} and"
        f" {second_label!r} — two output ports under one contract id publish two"
        " different schemas under a single identity, so a consumer resolving it"
        " gets one of the two arbitrarily and the other port's schema is lost"
    )
    if first_pinned and second_pinned:
        return f"{shared}; give each expose its own contract.id"
    if first_pinned != second_pinned:
        pinned_label = first_label if first_pinned else second_label
        derived_label = second_label if first_pinned else first_label
        return (
            f"{shared}. {derived_label!r} pins no contract.id, so the exporters"
            f" derive this id for it; change the contract.id on {pinned_label!r},"
            f" or rename the expose {derived_label!r}"
        )
    # Defensive. Two un-pinned exposes derive one id only when they answer to
    # one name, and a same-named pair is skipped by the caller as the
    # duplicate-port-name error it is — a more specific message than this one.
    # Kept so a change to the derivation cannot make this function hand back
    # advice that names the wrong side.
    return f"{shared}; rename one of the two exposes so their derived ids differ"


def contract_id_collisions(
    contract: Mapping[str, Any],
    *,
    product_id: Optional[str] = None,
) -> List[str]:
    """Every pair of output ports this contract would publish under one
    contract id, as ready-to-raise messages in document order.

    The collision is a property of the contract, not of one exporter, which is
    why it is resolved here next to :func:`expose_contract_id` rather than in
    whichever exporter happened to find it first. It used to live inside
    ``BitolOdpsProvider.render`` alone, so the ODPS doors refused a colliding
    contract while ``fluid generate standard --format odcs``, ``fluid generate
    artifacts --emit odcs`` and the Data Mesh Manager publish accepted it and
    emitted two ODCS documents carrying one ``id`` — the same defect the
    refusal exists to prevent, since one of the two schemas is what a consumer
    resolving that id gets, and in the file-per-id layout one simply overwrites
    the other. Every door calls this; a fourth exporter added later looks for
    it where the id itself is minted.

    The published id of a port is exactly what :func:`expose_contract_id`
    answers for it, so collisions are duplicates of that — which is what keeps
    the false-positive case accepted: a pin equal to a *pinned* sibling's
    derived string collides with nothing, because that sibling is published
    under its pin and its derived string is an id no contract answers to.

    ``product_id`` is passed through to :func:`expose_contract_id` for the
    Data Mesh Manager, which derives its ids from the id of the product it is
    writing rather than the contract's own.
    """
    claimed: Dict[str, Tuple[str, bool]] = {}
    errors: List[str] = []
    for index, expose in enumerate(get_exposes(contract)):
        if not isinstance(expose, Mapping):
            continue
        resolved = expose_contract_id(contract, expose, product_id=product_id)
        if resolved is None:
            # No pin and nothing to derive from. Such a port reaches a
            # per-port ODCS without a scoped id at all, which is a different
            # gap; it cannot collide with anything here.
            continue
        entry = (
            _collision_label(index, expose),
            explicit_expose_contract_id(expose, contract) is not None,
        )
        first = claimed.get(resolved)
        if first is None:
            claimed[resolved] = entry
            continue
        if first[0] == entry[0]:
            # Not two ports sharing an id — one name used twice, which every
            # exporter reports in its own, more specific words ("Duplicate
            # output port name"). Reporting it as a contract-id collision here
            # took that message away and told the author to rename an expose
            # without saying that the two are already the same name.
            continue
        errors.append(_collision_message(resolved, first, entry))
    return errors


def contract_id_collision(
    contract: Mapping[str, Any],
    *,
    product_id: Optional[str] = None,
) -> Optional[str]:
    """The first message :func:`contract_id_collisions` returns, or ``None``.

    For the exporters and the publish flow, which refuse the whole contract on
    the first collision rather than reporting all of them: the export is not
    going to happen either way, and the author fixes one pin at a time.
    """
    collisions = contract_id_collisions(contract, product_id=product_id)
    return collisions[0] if collisions else None


def get_consumes(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return ``contract.consumes`` as a list of upstream-reference
    dicts. Empty list when the contract declares none or carries a
    malformed value.

    Used by providers that need to enumerate upstream products (e.g.
    the local planner emits a sources.yml entry per consume). Distinct
    from :func:`consumes_to_canonical_ports` (below) which normalises
    into the per-port shape used by the dataflow graph; this getter
    returns the raw list.
    """
    consumes = contract.get("consumes")
    return consumes if isinstance(consumes, list) else []


def get_exposes(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return ``contract.exposes`` as a list of port dicts. Empty list
    when the contract declares none or carries a malformed value.

    Mirror of :func:`get_consumes` for the downstream side. Providers
    iterate this to plan provisioning per output port.
    """
    exposes = contract.get("exposes")
    return exposes if isinstance(exposes, list) else []


def get_consume_id(consume: Mapping[str, Any]) -> Optional[str]:
    """Return the consume's local port id (schema 0.7.x: ``exposeId``)."""
    return consume.get("exposeId")


def get_consume_ref(consume: Mapping[str, Any]) -> Optional[str]:
    """Return the upstream data-product reference for a consume
    (schema 0.7.x: ``productId``)."""
    return consume.get("productId")


def get_owner(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the owner block from ``metadata.owner`` — the canonical
    location fluid-schema-0.7.x mandates (top-level ``owner`` is not in
    the 0.7.x top-level whitelist).

    Returns an empty mapping when no owner information is present.
    """
    meta = contract.get("metadata")
    if isinstance(meta, Mapping):
        meta_owner = meta.get("owner")
        if isinstance(meta_owner, Mapping) and meta_owner:
            return meta_owner

    return {}


def consumes_to_canonical_ports(
    contract: Mapping[str, Any],
    *,
    default_version: str = "1",
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Normalize ``consumes[]`` into a canonical list of input-port dicts.

    The canonical shape is a complete read-view over every field the FLUID
    0.7.x ``$defs/consumeRef`` schema permits, plus a few extension
    fields, so providers can forward or drop anything they support
    without having to re-parse the raw contract::

        {
            # --- always present ---
            "id": str,                             # exposeId

            # --- 0.7.x consumeRef canonical fields ---
            "reference": Optional[str],            # productId
            "description": str,                    # purpose
            "version_constraint": Optional[str],  # semverRange
            "contract_version": Optional[str],     # semver, explicit only
            "qos_expectations": Optional[Mapping], # freshnessMax / maxStaleness / ...
            "required_policies": Optional[list],
            "tags": Optional[list],
            "labels": Optional[Mapping],

            # --- extension fields ---
            "name": str,                           # defaults to id
            "version": str,                        # defaults to default_version
            "contract_id": Optional[str],          # explicit only
            "required": Optional[bool],            # explicit only
            "source_system_id": Optional[str],     # explicit only
            "kind": Optional[str],
            "constraints": Optional[Any],
        }

    The returned ``tags`` list and ``labels`` mapping are defensive copies
    of the source contract values. Providers often rewrite them locally, and
    mutating the canonical view must not mutate the source contract.

    Semantics:
      * Fields that are not explicitly set on the consume entry are ``None``
        (or an empty string / default) rather than being fabricated with
        synthetic values — so providers can do ``if canonical["tags"]:`` and
        forward the list only when the author actually declared one.
      * Malformed entries (non-mapping, or missing ``exposeId``) are
        skipped with a warning rather than raising. FLUID contracts in
        the wild often carry partial lineage; providers should degrade
        gracefully rather than crash on first bad entry.
    """
    canonical: List[Dict[str, Any]] = []
    raw_consumes = contract.get("consumes", [])
    if not isinstance(raw_consumes, list):
        return canonical

    for index, consume in enumerate(raw_consumes):
        if not isinstance(consume, Mapping):
            if logger is not None:
                logger.warning(
                    "Skipping consumes[%d]: expected mapping, got %s",
                    index,
                    type(consume).__name__,
                )
            continue

        consume_id = get_consume_id(consume)
        if not consume_id:
            if logger is not None:
                logger.warning(
                    "Skipping consumes[%d]: missing required 'exposeId' field (keys=%s)",
                    index,
                    sorted(consume.keys()),
                )
            continue

        # 0.7.2 canonical fields (all optional on consumeRef).
        qos = consume.get("qosExpectations")
        required_policies = consume.get("requiredPolicies")
        tags = consume.get("tags")
        labels = consume.get("labels")
        version_constraint = consume.get("versionConstraint")
        source_system_id = (
            consume.get("sourceSystemId")
            or consume.get("source_system")
            or consume.get("sourceSystem")
        )

        port: Dict[str, Any] = {
            "id": str(consume_id),
            "name": str(consume.get("name") or consume_id),
            "description": str(consume.get("purpose") or consume.get("description") or ""),
            "version": str(consume.get("version", default_version)),
            "reference": get_consume_ref(consume),
            # 0.7.2 canonical fields
            "version_constraint": version_constraint if version_constraint else None,
            # 0.7.6: the exact upstream version this product was built against,
            # which is what an exported input port and its matching
            # ``inputContracts`` entry should claim. ``version`` above is the
            # consume's own ``version`` or the provider default, and a consume
            # that states only a range has neither — that is how an export came
            # to say ``version: 1.0.0`` for ``versionConstraint: ^2.1``.
            "contract_version": consume.get("contractVersion") or consume.get("contract_version"),
            "qos_expectations": qos if isinstance(qos, Mapping) and qos else None,
            "required_policies": (
                list(required_policies)
                if isinstance(required_policies, list) and required_policies
                else None
            ),
            "tags": list(tags) if isinstance(tags, list) and tags else None,
            "labels": dict(labels) if isinstance(labels, Mapping) and labels else None,
            # Extension fields — only populated when explicitly set.
            "contract_id": consume.get("contractId") or consume.get("contract_id"),
            "required": consume["required"] if "required" in consume else None,
            "source_system_id": str(source_system_id) if source_system_id else None,
            "kind": consume.get("kind"),
            "constraints": consume.get("constraints"),
        }
        canonical.append(port)

    return canonical


# ── Source-system canonicalization (Source-Aligned Data Products) ──────────
#
# SDPs (pre-* contracts) carry their upstream system info under
# ``builds[].properties.source`` rather than ``consumes[]``. The helpers
# below mirror ``consumes_to_canonical_ports`` so providers can treat
# both shapes uniformly when emitting input ports / registering
# SourceSystem entities.
#
# Per /borrow-before-build receipts:
#   - DMM (Entropy Data CE) has a first-class SourceSystem entity with a
#     PUT /api/sourcesystems/{id} endpoint and an InputPort.sourceSystemId
#     linkage field — confirmed against the published gitops example
#     (https://github.com/datamesh-manager/example-gitops-repository).
#   - ODPS-Bitol v1.0.0 InputPort is closed (additionalProperties:false)
#     with no sourceSystemId — so the standalone artifact uses
#     ``customProperties[{property: sourceSystem}]`` and the DMM-publish
#     overlay (in datamesh_manager/_odps_helpers.py) lifts it to the
#     native field for the wire payload.
#   - Connection-config field naming follows ODCS v3 ``servers[]``
#     conventions (host/port/database/schema/account/project/dataset)
#     which our FLUID 0.7.3 source.connection block already uses, so
#     pass-through is the right call.

# Connection fields safe to publish to the catalog. Anything else
# (passwords, secrets, tokens, even the username — DMM is a metadata
# catalog, not a credential store) is dropped.
_SAFE_CONNECTION_KEYS: frozenset = frozenset(
    {
        "host",
        "port",
        "database",
        "schema",
        "schemas",
        "account",  # snowflake / azure
        "project",  # bigquery / gcp
        "project_id",
        "dataset",  # bigquery
        "warehouse",  # snowflake
        "role",  # snowflake (read-only role is OK to disclose)
        "region",  # cloud locality
        "endpoint",  # rest / s3
        "url",
        "topic",  # kafka
        "topics",
        "bootstrap_servers",
    }
)

# Values that look like secrets even when the key is allowlisted (defence
# in depth — catches operators who put a token into ``host`` by accident).
_SECRET_VALUE_RE = re.compile(r"(?i)(password|secret|token|key|credential|bearer)")


# FLUID source ``kind`` (lowercase, snake_case per FLUID schema) →
# DMM SourceSystem / InputPort ``type`` (TitleCase per DMM enum).
# DMM defaults un-mapped values to "API" in the lineage UI, which is
# misleading for non-HTTP sources (Postgres database shows as "API").
# Add new entries here as we onboard more source kinds.
_KIND_TO_DMM_TYPE: Dict[str, str] = {
    "postgres": "Postgres",
    "postgresql": "Postgres",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "mssql": "SQL Server",
    "sqlserver": "SQL Server",
    "oracle": "Oracle",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "databricks": "Databricks",
    "duckdb": "DuckDB",
    "clickhouse": "ClickHouse",
    "kafka": "Kafka",
    "kinesis": "Kinesis",
    "pubsub": "Pub/Sub",
    "s3": "S3",
    "gcs": "GCS",
    "azure-blob": "Azure Blob",
    "azure_blob": "Azure Blob",
    "rest": "API",
    "api": "API",
    "graphql": "GraphQL",
    "salesforce": "Salesforce",
    "stripe": "Stripe",
    "shopify": "Shopify",
    "github": "GitHub",
    "file": "File",
}


def kind_to_dmm_type(kind: Optional[str]) -> Optional[str]:
    """Translate FLUID source ``kind`` → DMM TitleCase ``type`` enum.

    DMM's lineage UI renders the SourceSystem / InputPort ``type`` field
    as the connector icon. Unknown values default to "API", which
    mis-renders Postgres / Snowflake / file sources as HTTP APIs in the
    graph. Mapping to DMM's documented TitleCase enum (e.g. ``Postgres``,
    ``Snowflake``, ``BigQuery``, ``Kafka``) restores the correct icon.

    Returns ``None`` for ``None`` input, the mapped TitleCase string for
    known kinds, and the raw input (with first letter capitalised) for
    unknown kinds — preserves operator intent rather than silently
    erasing it. Add unknown kinds to ``_KIND_TO_DMM_TYPE`` when DMM
    starts rendering them with a dedicated icon.
    """
    if not kind:
        return None
    normalized = str(kind).strip().lower()
    if not normalized:
        return None
    return _KIND_TO_DMM_TYPE.get(normalized) or normalized.capitalize()


def redact_source_connection(connection: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a metadata-safe copy of a FLUID source ``connection`` dict.

    Whitelist-keeps fields in ``_SAFE_CONNECTION_KEYS`` and rejects values
    whose KEY OR string value matches a secret-y pattern. Used when
    publishing source-system info to a metadata catalog (DMM, OpenMetadata,
    DataHub, …) — none of those should hold credentials.

    FLUID's ``{{ env.X }}`` placeholders are passed through verbatim;
    we never resolve them client-side. The catalog records "host comes
    from env var X" as the connection metadata, not the resolved value.
    """
    safe: Dict[str, Any] = {}
    for key, value in connection.items():
        if key not in _SAFE_CONNECTION_KEYS:
            continue
        if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
            continue
        safe[key] = value
    return safe


def _source_system_id_for_build(source: Mapping[str, Any]) -> Optional[str]:
    """Derive a stable SourceSystem id from a ``builds[].properties.source``.

    Convention: ``<kind>-<database>`` (slug-sanitised). Two SDPs ingesting
    from the same Postgres database collapse to ONE SourceSystem entity,
    which matches the operator mental model (one Postgres = one source
    system) and avoids per-stream proliferation in the DMM UI.

    Falls back to the raw ``kind`` when database is absent (REST APIs,
    file sources, etc.). Returns ``None`` when even ``kind`` is missing
    (caller can then skip the entry rather than emit a junk id).
    """
    kind = (source.get("kind") or "").strip().lower()
    if not kind:
        return None
    conn = source.get("connection") or {}
    db = conn.get("database") or conn.get("project") or conn.get("dataset")
    parts = [kind, str(db).strip()] if db else [kind]
    return _slugify_core("-".join(p for p in parts if p)) or kind


def builds_to_canonical_input_ports(
    contract: Mapping[str, Any],
    *,
    default_version: str = "1",
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Normalize ``builds[].properties.source`` into canonical input ports.

    Returns one canonical port per ``streams[]`` entry on each acquisition
    build's source. The shape matches :func:`consumes_to_canonical_ports`
    so downstream code (ODPS exporter, DMM source-system upserter) can
    UNION the two lists and treat them uniformly.

    For SDPs (which is the only contract shape that has acquisition builds
    today), this is the only path that produces input ports — the
    ``consumes[]`` block is empty.

    Per-port shape additions over the consumes-derived port:
      * ``source_system_id`` is always populated (derived from
        ``<kind>-<database>``, slugified). This is the linkage to the
        SourceSystem entity the DMM provider will upsert.
      * ``kind`` is the source kind (postgres / mysql / mssql / ...).
      * ``source_connection`` is a metadata-safe copy of the connection
        dict (via :func:`redact_source_connection`) — used by the DMM
        provider to populate SourceSystem ``custom`` fields.
      * ``contract_id`` is synthesized as
        ``<kind>://<database>/<stream>`` (mirrors OpenLineage namespace+
        name convention) so the ODPS-Bitol-required field is populated
        even though no upstream FLUID contract exists.

    Malformed entries are skipped with a warning rather than raising,
    matching the consumes-side semantics.
    """
    canonical: List[Dict[str, Any]] = []
    raw_builds = contract.get("builds", [])
    if not isinstance(raw_builds, list):
        return canonical

    for build_index, build in enumerate(raw_builds):
        if not isinstance(build, Mapping):
            continue
        properties = build.get("properties") or {}
        source = properties.get("source")
        if not isinstance(source, Mapping):
            continue

        source_system_id = _source_system_id_for_build(source)
        if not source_system_id:
            if logger is not None:
                logger.warning(
                    "Skipping builds[%d].properties.source: missing 'kind' "
                    "(can't derive source system id, keys=%s)",
                    build_index,
                    sorted(source.keys()),
                )
            continue

        kind = (source.get("kind") or "").strip().lower()
        connection = source.get("connection") or {}
        if not isinstance(connection, Mapping):
            connection = {}
        safe_connection = redact_source_connection(connection)
        database = (
            connection.get("database")
            or connection.get("project")
            or connection.get("dataset")
            or "default"
        )

        streams = source.get("streams") or []
        if not isinstance(streams, list) or not streams:
            # Source declared without explicit streams — emit one port
            # representing the whole source (e.g. "this SDP ingests
            # everything from the postgres telco_source DB"). The port id
            # falls back to the source-system id.
            streams = [source_system_id]

        for stream in streams:
            stream_str = str(stream).strip()
            if not stream_str:
                continue
            # OpenLineage-style synthetic contract id: kind://database/stream.
            # Stable, human-readable, doesn't pretend to be an upstream
            # ODCS contract, satisfies ODPS-Bitol's required ``contractId``.
            synth_contract_id = f"{kind}://{database}/{stream_str}"

            canonical.append(
                {
                    "id": stream_str,
                    "name": stream_str,
                    "description": (
                        f"{stream_str} from {kind} source {database} (SDP source-aligned input)"
                    ),
                    "version": default_version,
                    "reference": None,
                    # 0.7.2 canonical fields — none authored on a build.
                    "version_constraint": None,
                    "qos_expectations": None,
                    "required_policies": None,
                    "tags": None,
                    "labels": None,
                    # Identity / lineage fields.
                    "contract_id": synth_contract_id,
                    "required": True,
                    "source_system_id": source_system_id,
                    "kind": kind,
                    "constraints": None,
                    # Build-derived extras for DMM source-system upsert.
                    "source_connection": safe_connection,
                }
            )

    return canonical


# Slug sanitization ---------------------------------------------------------

_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SLUG_EDGE_DASH = re.compile(r"(^-+|-+$)")


def _slugify_core(raw: str) -> str:
    """Shared slug-cleaning step. Lowercases, collapses non-alphanumerics to
    a single dash, and strips leading/trailing dashes. Does NOT apply the
    leading-digit guard — that is applied at the outer layer so it can
    protect both the input and the fallback."""
    lowered = (raw or "").strip().lower()
    slug = _SLUG_NON_ALNUM.sub("-", lowered)
    return _SLUG_EDGE_DASH.sub("", slug)


def slugify_identifier(value: str, *, fallback: str = "project") -> str:
    """Convert an arbitrary string to a FLUID-0.7.2-valid identifier segment.

    - Lowercases the input.
    - Replaces any run of non-alphanumeric characters with a single dash.
    - Strips leading/trailing dashes.
    - Falls back to ``fallback`` when the input collapses to an empty string.
      The fallback itself is slug-cleaned in the same way and still goes
      through the leading-digit guard, so callers cannot bypass the FLUID
      identifier rules with malformed fallback values.
    - Prefixes a leading digit with ``x-`` so the result ALWAYS satisfies the
      0.7.2 identifier pattern ``^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$|^[a-z0-9_]$``.
      This guard runs AFTER the fallback is chosen, so a numeric fallback
      (e.g. ``"123"``) is still rewritten to ``"x-123"``.
    - As a final safety net, if even the cleaned fallback is empty, returns
      the single-character sentinel ``"x"`` — guaranteed valid.
    """
    slug = _slugify_core(value)
    if not slug:
        slug = _slugify_core(fallback)
    if not slug:
        return "x"
    if slug[0].isdigit():
        slug = f"x-{slug}"
    return slug
