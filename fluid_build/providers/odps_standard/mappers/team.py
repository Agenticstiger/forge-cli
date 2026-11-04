# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Team object — Bitol ODPS schema is byte-identical to Bitol ODCS Team
(minus the optional ``id`` StableId). We delegate to the ODCS team mapper
to avoid duplicating the FLUID ``metadata.team`` / ``metadata.owner`` ↔ team
object translation, including which of the two wins when a contract sets both.
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
    team = _odcs_team.fluid_team_to_bitol(
        ctx.fluid, get_metadata_passthrough(ctx.fluid).get("team")
    )
    if team:
        ctx.odps["team"] = team


def to_fluid(ctx: ImportCtx) -> None:
    team = ctx.odps.get("team")
    if not team:
        return
    owner = _odcs_team._team_to_owner(team)
    if owner:
        ctx.fluid["owner"] = owner
    _odcs_team.write_roster(ctx.fluid, team)
    if isinstance(team, Mapping):
        metadata_passthrough(ctx.fluid)["team"] = dict(team)
