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

"""FLUID-document normalisation for the ODCS importer.

The section mappers each write their own slice in whatever shape reads most
naturally for that slice — including the ``odcs_passthrough`` buckets that make
the round-trip lossless. That intermediate shape is *not* a valid FLUID
contract: the FLUID JSON Schema sets ``additionalProperties: false`` on the
root, on ``metadata``, on every expose and on every column, so a pass-through
bucket parked next to a column would be rejected outright, as would
``metadata.status``, ``metadata.version`` or a top-level ``contract:`` block.

This module is the seam between the two shapes:

``to_document``
    Runs once at the end of the import pipeline. Restructures the root into the
    FLUID layout (``fluidVersion``/``kind``/``id``/``name``/``metadata``/
    ``exposes``), fills in the fields the schema requires, and hoists every
    pass-through bucket into ``extensions.odcs`` — the one place the FLUID
    schema declares as open (``additionalProperties: true``) and therefore the
    only legal home for vendor round-trip state.

``rehydrate``
    The exact inverse, run at the top of every export. It pushes
    ``extensions.odcs`` back down into the inline buckets the export mappers
    already read, so the mappers stay unchanged and
    ``export(import(export(x))) == export(x)`` still holds.

Both functions are pure: they copy their input and never mutate it.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Dict, List, Optional

from .base import PASSTHROUGH_KEY
from .types import (
    binding_format,
    odcs_to_fluid_status,
    physical_type_to_expose_kind,
    physical_type_to_platform,
    server_type_to_platform,
)

LOG = logging.getLogger(__name__)

# The single open bucket in the FLUID schema, and our namespace inside it.
EXTENSIONS_KEY = "extensions"
EXTENSION_NAMESPACE = "odcs"

# FLUID ``metadata`` is a closed object; only these keys may appear there.
# Anything else the ODCS side wants to carry goes to the pass-through.
#
# ``team`` is in here but is NOT accepted by every bundled schema: the roster
# arrived in FLUID 0.7.6, and before it the import had to bucket the Bitol team
# it read, which is why an ODPS export of an ODCS-imported contract emitted only
# the owner-derived member. Ask :func:`metadata_keys_for` rather than this set
# directly, so a document declaring an older version still gets the roster
# bucketed instead of written into a ``metadata`` that rejects it.
_FLUID_METADATA_KEYS = frozenset(
    {
        "businessContext",
        "classification",
        "createdAt",
        "experimental",
        "layer",
        "owner",
        "productType",
        "provenance",
        "tags",
        "team",
    }
)

# The ``metadata`` keys above that only some bundled schema versions declare.
# Every other key has been there since the oldest version we bundle, so there is
# nothing to ask the schema about.
_VERSIONED_METADATA_KEYS = frozenset({"team"})

# FLUID ``metadata.owner`` is closed too.
_FLUID_OWNER_KEYS = frozenset({"team", "email", "slack", "oncall"})

# ODCS server keys that have a home in FLUID ``binding.location``.
_LOCATION_KEYS = (
    "account",
    "project",
    "dataset",
    "database",
    "schema",
    "table",
    "bucket",
    "path",
    "region",
    "topic",
    "warehouse",
)


# --------------------------------------------------------------------------
# which schema version an imported document may declare
# --------------------------------------------------------------------------


def resolve_declared_version(recorded: Any) -> str:
    """The FLUID schema version an imported contract may declare.

    ``recorded`` is whatever the source document said its FLUID version was: an
    ODCS ``customProperties`` entry written by an earlier export, or an upstream
    import's own root field. ``None`` means the document recorded nothing, which
    is the normal case for a genuinely foreign document; it gets the latest
    stable version, as before.

    A recorded value is **foreign text that selects a JSON Schema**, so it is
    honoured only when it names a version this build actually bundles. An
    unknown string, a path, a number, an empty string, a version newer than
    anything shipped: each falls back to the stable default and logs the value
    it rejected. Without that check a document could make forge declare a
    version forge does not ship, and every later ``fluid validate`` of the
    imported contract would either fall through to a schema nobody chose or go
    looking for one over the network.

    A preview version (``PREVIEW_VERSIONS``) is bundled and fully validatable,
    so it is honoured — but only for a document that already recorded it, and
    the INFO line says so, because the alternative is a user wondering why an
    import declares a preview schema. Preview never becomes the default: the
    fallback is :meth:`FluidSchemaManager.latest_bundled_version`, which
    excludes it.
    """
    # Function-local: schema_manager pulls in jsonschema, and the CLI's startup
    # budget (tests/perf/test_startup_budget.py) forbids that on ``--help``.
    from fluid_build.schema_manager import FluidSchemaManager

    default = FluidSchemaManager.latest_bundled_version()
    if recorded is None:
        return default
    if isinstance(recorded, str) and recorded in FluidSchemaManager.BUNDLED_VERSIONS:
        if recorded in FluidSchemaManager.PREVIEW_VERSIONS:
            LOG.info(
                "Import: honouring the source document's recorded fluidVersion %s, a "
                "PREVIEW schema version. The imported contract declares it because the "
                "document did; it is not the default for a document that records none.",
                recorded,
            )
        return recorded
    # %r: the value is document text and may be a non-string or carry control
    # characters.
    LOG.warning(
        "Import: ignoring the recorded fluidVersion %r — this build bundles only %s. "
        "The imported contract declares %s instead. A document cannot select a schema "
        "version forge does not ship.",
        recorded,
        ", ".join(FluidSchemaManager.BUNDLED_VERSIONS),
        default,
    )
    return default


def metadata_keys_for(version: str) -> frozenset:
    """``_FLUID_METADATA_KEYS`` minus the keys this schema version has no slot for.

    ``metadata`` is closed in every bundled version, so a key the declared
    version does not declare has to be bucketed rather than written — writing it
    is what makes the import emit a contract its own validator rejects.
    """
    return _FLUID_METADATA_KEYS - {
        key for key in _VERSIONED_METADATA_KEYS if not _metadata_declares(version, key)
    }


@lru_cache(maxsize=None)
def _metadata_declares(version: str, key: str) -> bool:
    """Does this bundled FLUID schema declare ``metadata.<key>``?"""
    from fluid_build.schema_manager import FluidSchemaManager

    schema = FluidSchemaManager().get_schema(version, offline_only=True)
    if not isinstance(schema, Mapping):
        # A version we cannot read: assume the narrower answer. Bucketing a key
        # the schema would have accepted is lossless (it exports verbatim from
        # the bucket); writing one it rejects fails the whole contract.
        return False
    metadata = (schema.get("properties") or {}).get("metadata")
    properties = metadata.get("properties") if isinstance(metadata, Mapping) else None
    return isinstance(properties, Mapping) and key in properties


@lru_cache(maxsize=None)
def _def_declares(version: str, definition: str, key: str) -> bool:
    """Does this bundled FLUID schema declare ``$defs.<definition>.<key>``?

    Asked of the schema rather than compared against the version string that
    introduced a field, so promoting a preview version to stable, or adding a
    later one that keeps the field, needs no edit here. Same narrow-answer
    fallback as :func:`_metadata_declares`: ``$defs.column`` and
    ``$defs.exposeContract`` are both ``additionalProperties: false``, so
    guessing "declared" on a schema we cannot read would make the import emit a
    contract its own validator rejects, while guessing "not declared" only
    routes the value to the pass-through the export already reads back.
    """
    from fluid_build.schema_manager import FluidSchemaManager

    schema = FluidSchemaManager().get_schema(version, offline_only=True)
    if not isinstance(schema, Mapping):
        return False
    block = (schema.get("$defs") or {}).get(definition)
    properties = block.get("properties") if isinstance(block, Mapping) else None
    return isinstance(properties, Mapping) and key in properties


@lru_cache(maxsize=None)
def _identifier_pattern(version: str) -> Optional[re.Pattern]:
    """The regex this bundled FLUID schema's ``$defs.identifier`` declares.

    Read from the schema rather than copied into a constant because the pattern
    is not the same in every bundled version: 0.7.1 and 0.7.2 accept lowercase
    only, 0.7.3 widened it to both casings. ``None`` when the schema cannot be
    read or declares no pattern, which the caller treats as "cannot say", so an
    id is left exactly as the document stated it.
    """
    from fluid_build.schema_manager import FluidSchemaManager

    schema = FluidSchemaManager().get_schema(version, offline_only=True)
    if not isinstance(schema, Mapping):
        return None
    identifier = (schema.get("$defs") or {}).get("identifier")
    pattern = identifier.get("pattern") if isinstance(identifier, Mapping) else None
    if not isinstance(pattern, str) or not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:  # pragma: no cover - a bundled schema we ship is valid
        return None


# --------------------------------------------------------------------------
# import side: intermediate shape → valid FLUID document
# --------------------------------------------------------------------------


def to_document(fluid: Mapping[str, Any], odcs: Mapping[str, Any]) -> Dict[str, Any]:
    """Restructure the import pipeline's output into a valid FLUID contract."""
    work = copy.deepcopy(dict(fluid))
    bucket: Dict[str, Any] = {}

    metadata = dict(work.get("metadata") or {})
    metadata_pt = dict(metadata.pop(PASSTHROUGH_KEY, {}) or {})

    recorded_version = work.pop("fluidVersion", None)
    restored = work.get(EXTENSIONS_KEY)
    planted = restored.get(EXTENSION_NAMESPACE) if isinstance(restored, dict) else None
    if isinstance(planted, dict):
        # Only this function writes the marker (below). One the source document
        # restored through fluidExtras would make the export leave out a version.
        planted.pop("defaulted_version", None)
    doc: Dict[str, Any] = {
        # ODCS carries no FLUID schema version of its own. Keep the one the
        # extras mapper restored so an imported contract stays pinned to the
        # version it was authored against; only a genuinely foreign document
        # gets the default. The restored value came out of a document that may
        # be foreign or hostile, so it goes through the allowlist.
        "fluidVersion": resolve_declared_version(recorded_version),
        "kind": work.pop("kind", None) or "DataProduct",
    }
    if recorded_version is None:
        # The version is this reader's default, not the source's: the document
        # recorded none. The export reads this (:func:`defaulted_version`) to
        # leave it unrecorded while nothing else needs it, so a foreign
        # document's ODCS → FLUID → ODCS stays a fixed point.
        bucket["defaulted_version"] = doc["fluidVersion"]

    contract_block = work.get("contract")
    contract_id = None
    if isinstance(contract_block, Mapping):
        contract_id = contract_block.get("id")
    doc["id"], verbatim_id = _root_identifier(
        work.get("id") or contract_id or odcs.get("id"), str(doc["fluidVersion"])
    )
    if verbatim_id is not None:
        # The ODCS ``id`` the root could not carry. ``rehydrate`` replays it so
        # a whole-contract re-export still publishes the id the source document
        # named, and :func:`_pin_contract_id` keeps the per-port export on it
        # too. Without one of those two the import would answer an export with
        # a different contract id than the one every consumer references.
        bucket["contract_id"] = verbatim_id

    # ``name`` is required by FLUID and optional in ODCS. Fall back to the id
    # rather than inventing a title, and remember that we did so, so re-export
    # reproduces the source document (which had no ``name``) exactly.
    odcs_name = odcs.get("name") or metadata.get("name")
    if odcs_name:
        doc["name"] = odcs_name
    else:
        doc["name"] = doc["id"]
        bucket["name_synthesized"] = True
    metadata.pop("name", None)

    description = metadata.pop("description", None)
    if description:
        doc["description"] = description

    domain = metadata.pop("domain", None)
    if domain:
        doc["domain"] = domain

    # The root list is the canonical one and the only place the ODCS importer
    # writes ``tags``; a ``metadata.tags`` here can only be one the extras
    # mapper restored, so it stays where the source contract put it.
    root_tags = work.pop("tags", None)
    if root_tags:
        doc["tags"] = list(root_tags)

    # ODCS ``status`` → FLUID ``lifecycle.state``. The raw ODCS string is kept
    # verbatim because the two vocabularies are not one-to-one (ODCS
    # ``proposed`` and ``draft`` both land on FLUID ``preview``).
    status = metadata.pop("status", None)
    if status:
        doc["lifecycle"] = {"state": status}
    if odcs.get("status"):
        bucket["status"] = odcs["status"]

    version = metadata.pop("version", None)
    if version:
        bucket["version"] = version

    # Remaining non-FLUID metadata keys (tenant, dataProduct, ...) → bucket.
    # Asked per declared version, because ``team`` has a home only from 0.7.6:
    # on an older document the roster the team mapper just wrote is bucketed,
    # and ``rehydrate`` puts it back for the export.
    accepted_metadata = metadata_keys_for(str(doc["fluidVersion"]))
    stray = {k: v for k, v in metadata.items() if k not in accepted_metadata}
    for key in stray:
        metadata.pop(key)
    if stray:
        bucket["metadata_extra"] = stray

    # ``metadata.owner`` restored verbatim by the extras mapper wins over the
    # lossy one the team mapper derives from ODCS ``team``.
    derived_owner = work.pop("owner", None)
    owner = _normalize_owner(metadata.get("owner") or derived_owner, bucket)
    metadata["owner"] = owner
    doc["metadata"] = metadata

    if metadata_pt:
        bucket["metadata"] = metadata_pt

    servers = metadata_pt.get("servers") if isinstance(metadata_pt, Mapping) else None
    doc["exposes"] = [
        _normalize_expose(expose, servers, bucket, str(doc["fluidVersion"]))
        for expose in (work.get("exposes") or [])
        if isinstance(expose, Mapping)
    ]
    if verbatim_id is not None:
        _pin_contract_id(doc["exposes"], verbatim_id, str(doc["fluidVersion"]))

    # ``expects`` is not a FLUID root key (the modern spelling is ``consumes``,
    # which requires a productId/exposeId pair ODCS servers cannot supply). The
    # servers are already preserved verbatim in the pass-through, so drop the
    # intermediate list rather than emit an illegal one.
    work.pop("expects", None)

    for key, value in work.items():
        if key in ("metadata", "exposes", "contract", "id", "name", "owner"):
            continue
        if key.startswith("_"):  # in-memory scoping/marker keys, never on disk
            continue
        doc.setdefault(key, value)

    if bucket:
        # Root ``extensions`` did not exist before FLUID 0.7.3, and every schema
        # version sets ``additionalProperties: false`` at the root, so writing
        # the bucket into a 0.7.1/0.7.2 document produced "root: Additional
        # properties are not allowed ('extensions' was unexpected)" — an
        # importer reporting success while emitting a contract its own validator
        # rejects. It was the sole remaining error on four shipped examples.
        #
        # The document therefore has to declare a version that can hold what it
        # needs to carry. Omitting the bucket instead is NOT an option: it is
        # what makes the ODCS leg a fixed point (tenant, owner extras and the
        # verbatim servers/slaProperties lists all live here), and dropping it
        # breaks ``test_fluid_emitted_odcs_roundtrips_zero_diff`` on the 0.7.1
        # fixture with four fields lost. Raising the declared version costs one
        # machine-written label; omitting the bucket costs user content.
        #
        # Only documents that *declare* a pre-0.7.3 version reach this branch —
        # a third-party ODCS document carries no fluidVersion and defaults to
        # the latest schema — so it is confined to the FLUID → ODCS → FLUID leg.
        source_version = str(doc["fluidVersion"])
        if not _supports_extensions(source_version):
            LOG.warning(
                "ODCS import: emitting fluidVersion %s instead of %s. This contract "
                "carries ODCS round-trip state (%s) which lives in root "
                "`extensions`, added in FLUID %s; %s has additionalProperties:false "
                "at the root and cannot hold it. The alternative would be to drop "
                "that state and lose ODCS-native fields such as servers, "
                "slaProperties and metadata.tenant.",
                _MIN_EXTENSIONS_VERSION,
                source_version,
                ", ".join(sorted(bucket)),
                _MIN_EXTENSIONS_VERSION,
                source_version,
            )
            # Not a stale copy of ``fluidVersion``: a distinct fact — the schema
            # version the contract was authored against, which the emitted
            # document had to exceed. ``rehydrate`` replays it so the published
            # ODCS keeps naming the source's own version and the ODCS leg stays
            # an exact fixed point.
            bucket["authored_version"] = source_version
            doc["fluidVersion"] = _MIN_EXTENSIONS_VERSION
        extensions = dict(doc.get(EXTENSIONS_KEY) or {})
        extensions[EXTENSION_NAMESPACE] = bucket
        doc[EXTENSIONS_KEY] = extensions
    return doc


# Root ``id`` for a document that names no contract id at all.
_IMPORTED_ID_FALLBACK = "imported.odcs.contract"


def _slugged_root_id(raw: Any) -> str:
    """``raw`` reduced to something ``$defs.identifier`` accepts in every version.

    :func:`fluid_build.util.contract.slugify_identifier` lowercases, collapses
    every run of other characters to a single dash and guards a leading digit,
    so its result satisfies the lowercase-only 0.7.1/0.7.2 pattern as well as
    the wider one from 0.7.3 on.
    """
    from fluid_build.util.contract import slugify_identifier

    return slugify_identifier(str(raw), fallback=_IMPORTED_ID_FALLBACK)


def _root_identifier(raw: Any, version: str) -> tuple[str, Optional[str]]:
    """The FLUID root ``id`` for an imported document, plus the id it could not use.

    ODCS puts no format on its ``id`` — a catalog's own URN or UUID is exactly
    what ``exposes[].contract.id`` exists to pin, and a pinned id is published
    as the ODCS ``id`` verbatim. FLUID's root ``id`` is narrower: it has to
    match ``$defs.identifier``. The importer reconstructed the root id straight
    from the ODCS ``id``, so a contract pinned to ``urn:acme:contract:…``
    exported fine and then came back as a contract ``fluid validate`` refuses —
    ``fluid odcs import`` exited 1 on a document forge itself had written, and
    the only way out was to un-pin the id the catalog requires.

    The ODCS id is still used verbatim whenever it is a legal FLUID identifier,
    which is every derived ``<productId>.<exposeId>`` and most hand-written
    ones, so nothing changes for them. Only an id FLUID cannot spell is
    replaced, by a slug of itself rather than by the ODCS ``name`` or the schema
    object's: those are the *port's* name, not the product's, so two products
    with a port called ``orders`` would import under one id, and a name is prose
    that may carry no identifier characters at all. The slug is derived from the
    id, so it is deterministic and traceable back to it, and the second return
    value carries the original for the round-trip to replay.
    """
    if raw is None or raw == "":
        return _IMPORTED_ID_FALLBACK, None
    text = str(raw)
    pattern = _identifier_pattern(version)
    if pattern is None or pattern.match(text):
        return text, None
    slug = _slugged_root_id(text)
    # %r: the id is document text and may carry control characters.
    LOG.warning(
        "ODCS import: the contract id %r is not a FLUID identifier (%s), so the "
        "imported contract's root `id` is %s instead. The id itself is kept as the "
        "output port's `exposes[].contract.id` where the schema version declares "
        "that field, and in `extensions.odcs.contract_id`, so re-exporting this "
        "contract publishes it verbatim.",
        text,
        pattern.pattern,
        slug,
    )
    return slug, text


def _pin_contract_id(exposes: List[Dict[str, Any]], contract_id: str, version: str) -> None:
    """Pin an un-FLUID-able ODCS ``id`` as the single output port's contract id.

    An ODCS document is one contract, so a document with exactly one expose has
    exactly one port that the id belongs to and pinning it is unambiguous.
    Several exposes (a foreign document with several schema objects) is the case
    where the id names the document rather than any one port, so none is pinned
    and the ``extensions.odcs.contract_id`` replay is what carries it.

    Only written when the declared schema declares ``exposeContract.id`` (0.7.6
    added it) and only for an id the root could not keep, so an import that
    already round-tripped its id through the root is untouched.
    """
    if len(exposes) != 1 or not _def_declares(version, "exposeContract", "id"):
        return
    contract = exposes[0].get("contract")
    if isinstance(contract, dict) and not contract.get("id"):
        contract["id"] = contract_id


# Lowest FLUID schema version with a root ``extensions`` object — the promotion
# target, chosen as the *lowest* that works so the document stays as close to
# the author's declared version as the content allows. Asserted against the
# bundled schemas in tests/providers/odcs/test_fluid_roundtrip_fidelity.py
# rather than trusted as a bare constant.
_MIN_EXTENSIONS_VERSION = "0.7.3"


@lru_cache(maxsize=None)
def _supports_extensions(version: str) -> bool:
    """Does this bundled FLUID schema declare a root ``extensions`` property?"""
    from fluid_build.schema_manager import FluidSchemaManager

    schema = FluidSchemaManager().get_schema(version, offline_only=True)
    if not isinstance(schema, Mapping):
        # Unknown version: assume it can carry the bucket. Rewriting the version
        # of a schema we cannot reason about would be a guess, and dropping the
        # bucket would be silent loss.
        return True
    properties = schema.get("properties")
    return isinstance(properties, Mapping) and EXTENSIONS_KEY in properties


def _normalize_owner(owner: Any, bucket: Dict[str, Any]) -> Dict[str, Any]:
    """FLUID ``metadata.owner`` is required, and accepts only {team,email,slack,oncall}."""
    if not isinstance(owner, Mapping) or not owner:
        # FLUID requires an owner; ODCS does not have to name one. Record that
        # we invented it so re-export does not mint a ``team`` block the source
        # document never carried.
        bucket["owner_synthesized"] = True
        return {"team": "unknown"}
    kept = {k: v for k, v in owner.items() if k in _FLUID_OWNER_KEYS and v}
    extra = {k: v for k, v in owner.items() if k not in _FLUID_OWNER_KEYS}
    if extra:
        bucket["owner_extra"] = extra
    if not kept:
        kept = {"team": str(owner.get("name") or "unknown")}
    return kept


def _normalize_expose(
    expose: Mapping[str, Any],
    servers: Optional[Any],
    bucket: Dict[str, Any],
    declared_version: str,
) -> Dict[str, Any]:
    work = dict(expose)
    expose_pt = dict(work.pop(PASSTHROUGH_KEY, {}) or {})
    expose_id = work.get("exposeId") or work.pop("id", None) or "dataset"
    work.pop("id", None)
    work["exposeId"] = expose_id

    # FLUID requires ``kind`` and a complete ``binding``; ODCS supplies neither
    # directly. Note every key we invent so ``rehydrate`` can take it back off —
    # otherwise a re-export would claim the source document stated something it
    # never did, and the round-trip would stop being a fixed point.
    synthesized: Dict[str, Any] = {}
    physical_type = expose_pt.get("physical_type")
    if "kind" not in work:
        work["kind"] = physical_type_to_expose_kind(physical_type)
        synthesized["kind"] = True
    work["binding"], binding_synthesized = _normalize_binding(
        work.get("binding"), expose_pt, servers, expose_id, physical_type
    )
    if binding_synthesized:
        synthesized["binding"] = binding_synthesized

    contract = dict(work.get("contract") or {})
    fields: List[Dict[str, Any]] = []
    field_buckets: Dict[str, Any] = {}
    for fld in contract.get("schema") or []:
        if not isinstance(fld, Mapping):
            continue
        clean, fld_pt = _normalize_field(fld, declared_version)
        fields.append(clean)
        if fld_pt:
            field_buckets[clean["name"]] = fld_pt
    contract["schema"] = fields
    work["contract"] = contract

    entry: Dict[str, Any] = {}
    if expose_pt:
        entry["expose"] = expose_pt
    if field_buckets:
        entry["fields"] = field_buckets
    if synthesized:
        entry["synthesized"] = synthesized
    if entry:
        exposes_bucket = bucket.setdefault("exposes", {})
        exposes_bucket[expose_id] = entry
    return work


def _normalize_binding(
    binding: Any,
    expose_pt: Mapping[str, Any],
    servers: Optional[Any],
    expose_id: str,
    physical_type: Optional[str],
) -> tuple[Dict[str, Any], List[str]]:
    """Build a FLUID ``binding`` — platform/format/location are all required.

    ``servers[].type`` is the authoritative platform signal; ``physicalType``
    only says what kind of object it is. The server that names this expose wins,
    then the sole server when there is exactly one, then the physicalType
    heuristic.
    """
    # Anything already here was restored verbatim by the extras mapper — it is
    # the contract's own binding and outranks anything reconstructed from the
    # ODCS projection. Derivation only fills the gaps.
    restored = dict(binding) if isinstance(binding, Mapping) else {}
    restored.pop("physical_name", None)

    server = _match_server(servers, expose_id)
    platform = None
    if isinstance(server, Mapping) and server.get("type"):
        platform = server_type_to_platform(server["type"])
    if not platform:
        platform = physical_type_to_platform(physical_type or "")

    location: Dict[str, Any] = {}
    if isinstance(server, Mapping):
        for key in _LOCATION_KEYS:
            if key in server:
                location[key] = server[key]
    # ``warehouse`` is a Snowflake server field with no bindingLocation home.
    location.pop("warehouse", None)
    # ODCS ``physicalName`` names the object; it only belongs in ``table`` when
    # the location does not already address the object some other way (a file
    # ``path``, a Kafka ``topic``, a Kinesis ``stream``).
    physical_name = expose_pt.get("physical_name")
    if physical_name and not any(k in location for k in ("table", "path", "topic", "stream")):
        location["table"] = physical_name

    derived = {
        "platform": platform,
        "format": binding_format(platform, physical_type),
        "location": location,
    }
    synthesized: List[str] = []
    for key, value in derived.items():
        if key not in restored:
            restored[key] = value
            synthesized.append(key)
    return restored, synthesized


def _match_server(servers: Optional[Any], expose_id: str) -> Optional[Mapping[str, Any]]:
    if not isinstance(servers, list) or not servers:
        return None
    candidates = [s for s in servers if isinstance(s, Mapping)]
    if not candidates:
        return None
    for server in candidates:
        if server.get("server") == expose_id or server.get("name") == expose_id:
            return server
    return candidates[0] if len(candidates) == 1 else candidates[0]


# ODCS property keys the schema mapper writes inline for the export side to
# read back, but which the *closed* FLUID ``column`` object has no slot for
# ($defs/column, additionalProperties: false — it declares businessDefinition,
# businessName, description, labels, name, required, semanticType, sensitivity,
# tags, type, validationRules and nothing else). Left inline they made the
# importer emit a contract that failed FLUID's own validator: ODCS's
# ``classification`` on the official full-example.odcs.yaml produced seven
# "Additional properties are not allowed ('classification' was unexpected)"
# errors. They ride in the pass-through instead and ``rehydrate`` puts them
# back, so the export side is unchanged and the round-trip stays lossless.
_FIELD_ONLY_IN_PASSTHROUGH = ("quality", "classification")


def _normalize_field(
    fld: Mapping[str, Any], declared_version: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    work = dict(fld)
    field_pt = dict(work.pop(PASSTHROUGH_KEY, {}) or {})
    for key in _FIELD_ONLY_IN_PASSTHROUGH:
        if key in work:
            field_pt[key] = work.pop(key)
    # ``column.quality`` arrived in FLUID 0.7.6, so from that version on the
    # rules have a home on the column itself and do not need the pass-through.
    # Keeping them in it was a silent loss one leg further out: the ODPS
    # importer merges a resolved port's ``expose.contract`` into its port stub
    # and discards the sub-import's root ``extensions``, so every column-level
    # ODCS quality rule vanished on a FLUID → ODPS → FLUID cycle — not even
    # into the ODPS bucket. Inline, the rules travel inside the block the merge
    # already carries.
    #
    # An EMPTY list stays in the pass-through. It is bookkeeping, not author
    # content: ``quality.to_odcs_property`` reads any list as "replay verbatim,
    # synthesise nothing", so the empty list is what records that an imported
    # column had no rules, and writing ``quality: []`` onto every column of
    # every imported contract would be noise an author has to read past.
    quality = field_pt.get("quality")
    if quality and _def_declares(declared_version, "column", "quality"):
        work["quality"] = field_pt.pop("quality")
    return work, field_pt


# --------------------------------------------------------------------------
# export side: valid FLUID document → intermediate shape
# --------------------------------------------------------------------------


def defaulted_version(fluid: Mapping[str, Any]) -> Optional[str]:
    """The version :func:`to_document` gave a document that recorded none, if it did."""
    extensions = fluid.get(EXTENSIONS_KEY)
    bucket = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
    value = bucket.get("defaulted_version") if isinstance(bucket, Mapping) else None
    return value if isinstance(value, str) else None


def rehydrate(fluid: Mapping[str, Any]) -> Mapping[str, Any]:
    """Inverse of :func:`to_document` — no-op when there is no ODCS bucket."""
    extensions = fluid.get(EXTENSIONS_KEY)
    bucket = extensions.get(EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
    if not isinstance(bucket, Mapping) or not bucket:
        return fluid

    work = copy.deepcopy(dict(fluid))
    metadata = dict(work.get("metadata") or {})

    # Replay the authored schema version so the published ODCS names the version
    # the contract was written against rather than the one ``to_document`` had
    # to emit to fit the round-trip bucket. Guarded exactly like ``status``
    # below: only while the document still carries the promoted version — the
    # moment someone edits ``fluidVersion`` themselves, their edit wins.
    authored = bucket.get("authored_version")
    if authored and work.get("fluidVersion") == _MIN_EXTENSIONS_VERSION:
        work["fluidVersion"] = authored

    # Replay the source document's own contract id when the FLUID root could not
    # spell it (a catalog URN, say — see :func:`_root_identifier`). The
    # whole-contract export reads the root ``id``, so without this the published
    # ODCS would answer to the slug the import had to invent and no consumer
    # reference to the real id would resolve. Guarded like ``authored_version``:
    # only while the root still carries that slug, so the moment someone edits
    # ``id`` themselves their edit wins.
    verbatim_id = bucket.get("contract_id")
    if verbatim_id and work.get("id") == _slugged_root_id(verbatim_id):
        work["id"] = verbatim_id

    if isinstance(bucket.get("metadata"), Mapping):
        metadata[PASSTHROUGH_KEY] = dict(bucket["metadata"])
    if isinstance(bucket.get("metadata_extra"), Mapping):
        metadata.update(bucket["metadata_extra"])
    if bucket.get("version"):
        metadata["version"] = bucket["version"]

    # Status: replay the source document's verbatim ODCS spelling only while it
    # still agrees with ``lifecycle.state``. The two vocabularies are not
    # one-to-one (ODCS ``proposed`` and ``draft`` both import as FLUID
    # ``preview``), so the verbatim value is what keeps the round-trip exact —
    # but the moment someone edits ``lifecycle.state`` the edit must win, or the
    # exporter would publish a status the contract no longer claims.
    lifecycle = work.get("lifecycle")
    state = lifecycle.get("state") if isinstance(lifecycle, Mapping) else None
    verbatim = bucket.get("status")
    if verbatim and (state is None or odcs_to_fluid_status(verbatim) == state):
        metadata["status"] = verbatim
    elif state:
        metadata["status"] = state

    if not bucket.get("name_synthesized") and work.get("name"):
        metadata["name"] = work["name"]
    if work.get("description"):
        metadata["description"] = work["description"]
    if work.get("domain"):
        metadata["domain"] = work["domain"]
    # ``tags`` is *not* mirrored into metadata here: the exporter reads the
    # contract root first, and copying it down would make an imported
    # contract's real ``metadata.tags`` indistinguishable from the root list.

    if bucket.get("owner_synthesized"):
        metadata.pop("owner", None)
    else:
        owner = dict(metadata.get("owner") or {})
        if isinstance(bucket.get("owner_extra"), Mapping):
            owner.update(bucket["owner_extra"])
        if owner:
            metadata["owner"] = owner
            work["owner"] = owner
    work["metadata"] = metadata

    exposes_bucket = bucket.get("exposes") if isinstance(bucket.get("exposes"), Mapping) else {}
    exposes: List[Dict[str, Any]] = []
    for expose in work.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        exposes.append(_rehydrate_expose(expose, exposes_bucket))
    work["exposes"] = exposes
    return work


def _rehydrate_expose(
    expose: Mapping[str, Any], exposes_bucket: Mapping[str, Any]
) -> Dict[str, Any]:
    work = dict(expose)
    expose_id = work.get("exposeId") or work.get("id")
    entry = exposes_bucket.get(expose_id) if expose_id else None
    entry = entry if isinstance(entry, Mapping) else {}

    if isinstance(entry.get("expose"), Mapping):
        work[PASSTHROUGH_KEY] = dict(entry["expose"])

    # Undo the schema-required repairs ``to_document`` applied, so the export
    # pipeline sees the expose exactly as the source document described it.
    synthesized = entry.get("synthesized")
    if isinstance(synthesized, Mapping):
        if synthesized.get("kind"):
            work.pop("kind", None)
        binding_keys = synthesized.get("binding")
        if isinstance(binding_keys, list):
            binding = dict(work.get("binding") or {})
            for key in binding_keys:
                binding.pop(key, None)
            if binding:
                work["binding"] = binding
            else:
                work.pop("binding", None)

    field_buckets = entry.get("fields") if isinstance(entry.get("fields"), Mapping) else {}
    contract = dict(work.get("contract") or {})
    fields: List[Dict[str, Any]] = []
    for fld in contract.get("schema") or []:
        if not isinstance(fld, Mapping):
            continue
        clean = dict(fld)
        fld_pt = field_buckets.get(clean.get("name"))
        if isinstance(fld_pt, Mapping):
            fld_pt = dict(fld_pt)
            for key in _FIELD_ONLY_IN_PASSTHROUGH:
                if key in fld_pt:
                    clean[key] = fld_pt.pop(key)
            if fld_pt:
                clean[PASSTHROUGH_KEY] = fld_pt
        fields.append(clean)
    if contract:
        contract["schema"] = fields
        work["contract"] = contract
    return work


__all__ = ["to_document", "rehydrate", "EXTENSIONS_KEY", "EXTENSION_NAMESPACE"]
