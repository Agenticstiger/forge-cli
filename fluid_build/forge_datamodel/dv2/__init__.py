# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
