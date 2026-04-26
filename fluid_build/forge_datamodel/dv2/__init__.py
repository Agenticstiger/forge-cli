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

"""Data Vault 2.0 helper surface — deterministic IR + hash + naming.

These modules are LLM-free, pure-Python helpers so that once the
modeler emits a :class:`DV2Model`, the physical hash-key values and
table names are computed by deterministic code — not by the model.

Re-exports:

* IR classes (``HubDefinition``, ``LinkDefinition``, ...) from
  :mod:`fluid_build.copilot.schemas.data_model` — one source of truth.
* :func:`compute_hash_key`, :func:`compute_hash_diff` from
  :mod:`fluid_build.forge_datamodel.dv2.hash_keys`.
* ``hub_name``, ``link_name``, ``satellite_name``, ``pit_name``,
  ``bridge_name`` from :mod:`fluid_build.forge_datamodel.dv2.naming`.
"""

from __future__ import annotations

from fluid_build.forge_datamodel.dv2.hash_keys import (
    compute_hash_diff,
    compute_hash_key,
)
from fluid_build.forge_datamodel.dv2.ir import (
    BridgeDefinition,
    DV2Model,
    HashKeyStrategy,
    HubDefinition,
    LinkDefinition,
    PitDefinition,
    SatelliteDefinition,
)
from fluid_build.forge_datamodel.dv2.naming import (
    bridge_name,
    hub_name,
    link_name,
    pit_name,
    satellite_name,
)

__all__ = [
    "BridgeDefinition",
    "DV2Model",
    "HashKeyStrategy",
    "HubDefinition",
    "LinkDefinition",
    "PitDefinition",
    "SatelliteDefinition",
    "bridge_name",
    "compute_hash_diff",
    "compute_hash_key",
    "hub_name",
    "link_name",
    "pit_name",
    "satellite_name",
]
