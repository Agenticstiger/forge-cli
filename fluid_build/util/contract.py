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
