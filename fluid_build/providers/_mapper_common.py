# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Shared mapper primitives for the bidirectional spec providers.

Both :mod:`fluid_build.providers.odcs.mappers.base` and
:mod:`fluid_build.providers.odps_standard.mappers.base` need:

  * A pass-through bucket protocol (``set`` + ``get`` accessors at
    metadata / expose / field / expect levels), parameterised by the
    key that distinguishes the two namespaces (``odcs_passthrough`` vs
    ``odps_passthrough``).
  * A canonical ``fluid_id`` resolver (FLUID contracts may carry id at
    top level, under ``contract.id``, or ``metadata.id``).

Each provider keeps its own ``ImportCtx`` / ``ExportCtx`` dataclasses
because the field names differ (``odcs`` vs ``odps`` payload), and that
distinction reads more clearly inline than via a generic shape.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, Callable, Dict, Optional, Tuple


PassthroughHelpers = Tuple[
    Callable[[MutableMapping[str, Any]], Dict[str, Any]],  # metadata set
    Callable[[Mapping[str, Any]], Mapping[str, Any]],      # metadata get
    Callable[[MutableMapping[str, Any]], Dict[str, Any]],  # expose set
    Callable[[Mapping[str, Any]], Mapping[str, Any]],      # expose get
    Callable[[MutableMapping[str, Any]], Dict[str, Any]],  # field/expect set
    Callable[[Mapping[str, Any]], Mapping[str, Any]],      # field/expect get
]


def make_passthrough_helpers(key: str) -> PassthroughHelpers:
    """Generate the 6 pass-through accessors for a given bucket key.

    Returns ``(metadata_set, metadata_get, expose_set, expose_get,
    child_set, child_get)`` where ``child`` is ``field`` for ODCS and
    ``expect`` for ODPS (same shape; different semantics).
    """

    def metadata_set(fluid: MutableMapping[str, Any]) -> Dict[str, Any]:
        metadata = fluid.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = dict(metadata)
            fluid["metadata"] = metadata
        return metadata.setdefault(key, {})

    def metadata_get(fluid: Mapping[str, Any]) -> Mapping[str, Any]:
        metadata = fluid.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            return {}
        pt = metadata.get(key)
        return pt if isinstance(pt, Mapping) else {}

    def expose_set(expose: MutableMapping[str, Any]) -> Dict[str, Any]:
        return expose.setdefault(key, {})

    def expose_get(expose: Mapping[str, Any]) -> Mapping[str, Any]:
        pt = expose.get(key)
        return pt if isinstance(pt, Mapping) else {}

    def child_set(container: MutableMapping[str, Any]) -> Dict[str, Any]:
        return container.setdefault(key, {})

    def child_get(container: Mapping[str, Any]) -> Mapping[str, Any]:
        pt = container.get(key)
        return pt if isinstance(pt, Mapping) else {}

    return (metadata_set, metadata_get, expose_set, expose_get, child_set, child_get)


def fluid_id(fluid: Mapping[str, Any]) -> Optional[str]:
    """Resolve a FLUID contract's id from any of the supported locations.

    FLUID contracts may carry the id at top level, under ``contract.id``,
    or under ``metadata.id``. This is the canonical lookup; both spec
    providers route through here.
    """
    if "id" in fluid:
        return fluid["id"]
    contract = fluid.get("contract")
    if isinstance(contract, Mapping) and contract.get("id"):
        return contract["id"]
    metadata = fluid.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("id"):
        return metadata["id"]
    return None


__all__ = ["fluid_id", "make_passthrough_helpers", "PassthroughHelpers"]
