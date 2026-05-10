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

Provides utilities for accessing contract fields in a version-agnostic way.
This allows the codebase to work with schema 0.5.7 while maintaining
clean abstraction for future schema versions.
"""

import logging
import re
from typing import Any, Dict, List, Mapping, Optional


def get_expose_id(expose: Mapping[str, Any]) -> Optional[str]:
    """
    Get the expose ID from an expose object.

    Schema 0.5.7+: exposeId
    Schema 0.4.0: id

    Args:
        expose: The expose dictionary

    Returns:
        The expose ID or None
    """
    return expose.get("exposeId") or expose.get("id")


def get_expose_kind(expose: Mapping[str, Any]) -> Optional[str]:
    """
    Get the expose kind/type from an expose object.

    Schema 0.5.7+: kind
    Schema 0.4.0: type

    Args:
        expose: The expose dictionary

    Returns:
        The expose kind/type or None
    """
    return expose.get("kind") or expose.get("type")


def get_expose_binding(expose: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get the expose binding/location from an expose object.

    Schema 0.5.7+: binding (object with provider, location, etc.)
    Schema 0.4.0: location (string)

    Args:
        expose: The expose dictionary

    Returns:
        The binding object or None
    """
    binding = expose.get("binding")
    if binding:
        return binding

    # Fallback: convert old location string to binding object
    location = expose.get("location")
    if location and isinstance(location, str):
        return {"location": location}

    return None


def get_expose_location(expose: Mapping[str, Any]) -> Optional[str]:
    """
    Get the physical location string from an expose object.

    Schema 0.5.7+: binding.location
    Schema 0.4.0: location

    Args:
        expose: The expose dictionary

    Returns:
        The location string or None
    """
    binding = get_expose_binding(expose)
    if binding and isinstance(binding, dict):
        return binding.get("location")

    # Direct fallback
    return expose.get("location")


def get_builds(contract: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """
    Get the builds array from a contract.

    Schema 0.5.7+: builds (array)
    Schema 0.4.0: build (single object)

    Args:
        contract: The contract dictionary

    Returns:
        List of build objects (may be empty)
    """
    builds = contract.get("builds")
    if builds and isinstance(builds, list):
        return builds

    # Fallback: wrap single build in array
    build = contract.get("build")
    if build and isinstance(build, dict):
        return [build]

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
    Get the build engine from a build object.

    Args:
        build: The build dictionary

    Returns:
        The engine name (e.g., 'dbt', 'dataform', 'spark')
    """
    return build.get("engine") or build.get("type")


def get_contract_version(contract: Mapping[str, Any]) -> Optional[str]:
    """
    Get the FLUID schema version from a contract.

    Args:
        contract: The contract dictionary

    Returns:
        The fluidVersion string (e.g., '0.5.7')
    """
    return contract.get("fluidVersion")


def get_expose_contract(expose: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Get the contract section from an expose object.

    In FLUID 0.5.7+ (including 0.7.2), ``schema`` and ``dq`` are nested
    under a ``contract`` key. In 0.4.0, they were at the top level.

    Args:
        expose: The expose dictionary

    Returns:
        The contract section object or None
    """
    return expose.get("contract")


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
    """Return the consume's local port id.

    Schema 0.5.7+: ``exposeId``. Schema 0.4.0: ``id``.
    """
    return consume.get("exposeId") or consume.get("id")


def get_consume_ref(consume: Mapping[str, Any]) -> Optional[str]:
    """Return the upstream data-product reference for a consume.

    Schema 0.5.7+: ``productId``. Schema 0.4.0: ``ref``.
    """
    return consume.get("productId") or consume.get("ref")


def get_owner(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the owner block, preferring the canonical ``metadata.owner``
    location (where FLUID 0.7.2 mandates it — top-level ``owner`` is not in
    the 0.7.2 top-level whitelist) and falling back to a top-level ``owner``
    key for legacy or pre-migration contracts.

    Returns an empty mapping when no owner information is present.
    """
    meta = contract.get("metadata")
    if isinstance(meta, Mapping):
        meta_owner = meta.get("owner")
        if isinstance(meta_owner, Mapping) and meta_owner:
            return meta_owner

    top = contract.get("owner")
    if isinstance(top, Mapping) and top:
        return top

    return {}


def consumes_to_canonical_ports(
    contract: Mapping[str, Any],
    *,
    default_version: str = "1",
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Normalize ``consumes[]`` into a canonical list of input-port dicts.

    The canonical shape is a complete read-view over every field the FLUID
    0.7.2 ``$defs/consumeRef`` schema permits, plus the legacy-extension
    fields older contracts commonly carry, so providers can forward or drop
    anything they support without having to re-parse the raw contract::

        {
            # --- always present ---
            "id": str,                             # exposeId (or legacy `id`)

            # --- 0.7.2 consumeRef canonical fields ---
            "reference": Optional[str],            # productId (or legacy `ref`)
            "description": str,                    # purpose (or legacy `description`)
            "version_constraint": Optional[str],   # semverRange
            "qos_expectations": Optional[Mapping], # freshnessMax / maxStaleness / ...
            "required_policies": Optional[list],
            "tags": Optional[list],
            "labels": Optional[Mapping],

            # --- 0.4.0 / extension fields (kept for backward compat) ---
            "name": str,                           # defaults to id
            "version": str,                        # stringified legacy `version`, defaults to default_version
            "contract_id": Optional[str],          # explicit only
            "required": Optional[bool],            # explicit only
            "source_system_id": Optional[str],     # legacy extension only
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
      * Malformed entries (non-mapping, or missing both ``exposeId`` and
        ``id``) are skipped with a warning rather than raising. FLUID
        contracts in the wild often carry partial lineage; providers should
        degrade gracefully rather than crash on first bad entry.
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
                    "Skipping consumes[%d]: missing required 'exposeId'/'id' field (keys=%s)",
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
            "qos_expectations": qos if isinstance(qos, Mapping) and qos else None,
            "required_policies": (
                list(required_policies)
                if isinstance(required_policies, list) and required_policies
                else None
            ),
            "tags": list(tags) if isinstance(tags, list) and tags else None,
            "labels": dict(labels) if isinstance(labels, Mapping) and labels else None,
            # Extension / legacy fields — only populated when explicitly set.
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
