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

"""Team object — Bitol ODPS schema is byte-identical to Bitol ODCS Team
(minus the optional ``id`` StableId). We delegate to the ODCS team mapper
to avoid duplicating the FLUID owner ↔ team object translation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fluid_build.providers.odcs.mappers import team as _odcs_team

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)


def to_odps(ctx: ExportCtx) -> None:
    pt_team = get_metadata_passthrough(ctx.fluid).get("team")
    if isinstance(pt_team, Mapping) and pt_team:
        ctx.odps["team"] = dict(pt_team)
        return
    team = _odcs_team._owner_to_team(ctx.fluid)
    if team:
        ctx.odps["team"] = team


def to_fluid(ctx: ImportCtx) -> None:
    team = ctx.odps.get("team")
    if not team:
        return
    owner = _odcs_team._team_to_owner(team)
    if owner:
        ctx.fluid["owner"] = owner
    if isinstance(team, Mapping):
        metadata_passthrough(ctx.fluid)["team"] = dict(team)
