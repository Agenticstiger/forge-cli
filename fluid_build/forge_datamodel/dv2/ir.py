# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Thin re-exports of DV2 IR classes.

The Pydantic schemas live in :mod:`fluid_build.copilot.schemas.data_model`
(one source of truth, shared with the staged-agent outputs).  This module
re-exports them under the ``forge_datamodel.dv2`` namespace so downstream
callers in ``forge_datamodel`` can depend on the IR without reaching into
the copilot package.
"""

from __future__ import annotations

from fluid_build.copilot.schemas.data_model import (
    BridgeDefinition,
    DV2Model,
    EntityRelationship,
    FieldDefinition,
    HashKeyStrategy,
    HubDefinition,
    JoinKeyDetail,
    LinkDefinition,
    PitDefinition,
    SatelliteDefinition,
)

__all__ = [
    "BridgeDefinition",
    "DV2Model",
    "EntityRelationship",
    "FieldDefinition",
    "HashKeyStrategy",
    "HubDefinition",
    "JoinKeyDetail",
    "LinkDefinition",
    "PitDefinition",
    "SatelliteDefinition",
]
