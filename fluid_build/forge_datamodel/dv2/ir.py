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
