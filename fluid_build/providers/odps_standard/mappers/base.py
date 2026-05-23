# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Section mapper protocol + pass-through namespace for the Bitol ODPS provider.

Mirrors the design of :mod:`fluid_build.providers.odcs.mappers.base` so the
two providers share the same modular vocabulary. Per-level pass-through
namespaces:

- contract-level → ``metadata.odps_passthrough.*``
- per-expose    → ``expose.odps_passthrough.*``
- per-expect    → ``expect.odps_passthrough.*``
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


PASSTHROUGH_KEY = "odps_passthrough"


def metadata_passthrough(fluid: MutableMapping[str, Any]) -> Dict[str, Any]:
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
    return expose.setdefault(PASSTHROUGH_KEY, {})


def get_expose_passthrough(expose: Mapping[str, Any]) -> Mapping[str, Any]:
    pt = expose.get(PASSTHROUGH_KEY)
    return pt if isinstance(pt, Mapping) else {}


def expect_passthrough(expect: MutableMapping[str, Any]) -> Dict[str, Any]:
    return expect.setdefault(PASSTHROUGH_KEY, {})


def get_expect_passthrough(expect: Mapping[str, Any]) -> Mapping[str, Any]:
    pt = expect.get(PASSTHROUGH_KEY)
    return pt if isinstance(pt, Mapping) else {}


@dataclass
class ImportCtx:
    odps: Mapping[str, Any]
    fluid: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportCtx:
    fluid: Mapping[str, Any]
    odps: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


def fluid_id(fluid: Mapping[str, Any]) -> Optional[str]:
    if "id" in fluid:
        return fluid["id"]
    contract = fluid.get("contract")
    if isinstance(contract, Mapping) and contract.get("id"):
        return contract["id"]
    metadata = fluid.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("id"):
        return metadata["id"]
    return None


def contract_id_for_port(product_id: str, port_name: str) -> str:
    """Convention used by export + datamesh-manager: ``{productId}.{portName}``.

    The OdcsProvider's per-port render guarantees the emitted ODCS contract's
    ``id`` matches this string exactly, which is the linking invariant for
    Bitol ODPS fragments mode.
    """
    return f"{product_id}.{port_name}"
