# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Section mapper protocol, contexts, and the round-trip pass-through namespace.

Every ODCS section maps to one module under :mod:`fluid_build.providers.odcs.mappers`
exposing a pair of pure functions: :func:`to_fluid` (ODCS → FLUID) and
:func:`to_odcs` (FLUID → ODCS). Pass-through data lives under a single key
per level so the round-trip surface is auditable in one place.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


PASSTHROUGH_KEY = "odcs_passthrough"


def metadata_passthrough(fluid: MutableMapping[str, Any]) -> Dict[str, Any]:
    """Get or create the contract-level pass-through bucket under ``metadata``."""
    metadata = fluid.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = dict(metadata)
        fluid["metadata"] = metadata
    return metadata.setdefault(PASSTHROUGH_KEY, {})


def get_metadata_passthrough(fluid: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = fluid.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return {}
    pt = metadata.get(PASSTHROUGH_KEY)
    return pt if isinstance(pt, Mapping) else {}


def expose_passthrough(expose: MutableMapping[str, Any]) -> Dict[str, Any]:
    """Get or create an expose's pass-through bucket."""
    return expose.setdefault(PASSTHROUGH_KEY, {})


def get_expose_passthrough(expose: Mapping[str, Any]) -> Mapping[str, Any]:
    pt = expose.get(PASSTHROUGH_KEY)
    return pt if isinstance(pt, Mapping) else {}


def field_passthrough(fld: MutableMapping[str, Any]) -> Dict[str, Any]:
    """Get or create a field's pass-through bucket."""
    return fld.setdefault(PASSTHROUGH_KEY, {})


def get_field_passthrough(fld: Mapping[str, Any]) -> Mapping[str, Any]:
    pt = fld.get(PASSTHROUGH_KEY)
    return pt if isinstance(pt, Mapping) else {}


@dataclass
class ImportCtx:
    """Mutable context threaded through ODCS → FLUID mappers."""

    odcs: Mapping[str, Any]
    fluid: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportCtx:
    """Mutable context threaded through FLUID → ODCS mappers."""

    fluid: Mapping[str, Any]
    odcs: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


def fluid_id(fluid: Mapping[str, Any]) -> Optional[str]:
    """Resolve a FLUID contract's id from any of the supported locations."""
    if "id" in fluid:
        return fluid["id"]
    contract = fluid.get("contract")
    if isinstance(contract, Mapping) and contract.get("id"):
        return contract["id"]
    metadata = fluid.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("id"):
        return metadata["id"]
    return None
