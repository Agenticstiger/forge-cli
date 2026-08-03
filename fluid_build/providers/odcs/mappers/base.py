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

"""Section mapper protocol, contexts, and the round-trip pass-through namespace.

Every ODCS section maps to one module under :mod:`fluid_build.providers.odcs.mappers`
exposing a pair of pure functions: :func:`to_fluid` (ODCS → FLUID) and
:func:`to_odcs` (FLUID → ODCS). Pass-through data lives under a single key
per level so the round-trip surface is auditable in one place.

Pass-through accessors + ``fluid_id`` are produced by the shared factory
in :mod:`fluid_build.providers._mapper_common` so the ODCS and Bitol-ODPS
providers stay protocol-identical without code duplication.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Dict

from fluid_build.providers._mapper_common import (
    fluid_id,
    make_passthrough_helpers,
    resolve_status,
)

PASSTHROUGH_KEY = "odcs_passthrough"

(
    metadata_passthrough,
    get_metadata_passthrough,
    expose_passthrough,
    get_expose_passthrough,
    field_passthrough,
    get_field_passthrough,
) = make_passthrough_helpers(PASSTHROUGH_KEY)


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


__all__ = [
    "PASSTHROUGH_KEY",
    "metadata_passthrough",
    "get_metadata_passthrough",
    "expose_passthrough",
    "get_expose_passthrough",
    "field_passthrough",
    "get_field_passthrough",
    "ImportCtx",
    "ExportCtx",
    "fluid_id",
    "resolve_status",
]
