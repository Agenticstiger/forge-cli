# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Section mapper protocol + pass-through namespace for the Bitol ODPS provider.

Mirrors the design of :mod:`fluid_build.providers.odcs.mappers.base` — both
modules share the same accessor protocol via
:mod:`fluid_build.providers._mapper_common`. Per-level pass-through
namespaces:

- contract-level → ``metadata.odps_passthrough.*``
- per-expose    → ``expose.odps_passthrough.*``
- per-expect    → ``expect.odps_passthrough.*``
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Dict

from fluid_build.providers._mapper_common import (
    fluid_id,
    make_passthrough_helpers,
)

PASSTHROUGH_KEY = "odps_passthrough"

(
    metadata_passthrough,
    get_metadata_passthrough,
    expose_passthrough,
    get_expose_passthrough,
    expect_passthrough,
    get_expect_passthrough,
) = make_passthrough_helpers(PASSTHROUGH_KEY)


@dataclass
class ImportCtx:
    """Mutable context threaded through ODPS → FLUID mappers."""

    odps: Mapping[str, Any]
    fluid: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExportCtx:
    """Mutable context threaded through FLUID → ODPS mappers."""

    fluid: Mapping[str, Any]
    odps: MutableMapping[str, Any]
    logger: logging.Logger
    options: Dict[str, Any] = field(default_factory=dict)


def contract_id_for_port(product_id: str, port_name: str) -> str:
    """Convention used by export + datamesh-manager: ``{productId}.{portName}``.

    The OdcsProvider's per-port render guarantees the emitted ODCS contract's
    ``id`` matches this string exactly, which is the linking invariant for
    Bitol ODPS fragments mode.
    """
    return f"{product_id}.{port_name}"


__all__ = [
    "PASSTHROUGH_KEY",
    "metadata_passthrough",
    "get_metadata_passthrough",
    "expose_passthrough",
    "get_expose_passthrough",
    "expect_passthrough",
    "get_expect_passthrough",
    "ImportCtx",
    "ExportCtx",
    "fluid_id",
    "contract_id_for_port",
]
