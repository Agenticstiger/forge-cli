# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Support channels and management ports (opaque pass-through).

FLUID has no native model for support channels or management endpoints, so
both flow through ``metadata.odps_passthrough.{support, management_ports}``
verbatim.
"""

from __future__ import annotations

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)


def to_odps(ctx: ExportCtx) -> None:
    pt = get_metadata_passthrough(ctx.fluid)
    if "support" in pt:
        ctx.odps["support"] = list(pt["support"])
    if "management_ports" in pt:
        ctx.odps["managementPorts"] = list(pt["management_ports"])


def to_fluid(ctx: ImportCtx) -> None:
    pt = metadata_passthrough(ctx.fluid)
    if ctx.odps.get("support"):
        pt["support"] = list(ctx.odps["support"])
    if ctx.odps.get("managementPorts"):
        pt["management_ports"] = list(ctx.odps["managementPorts"])
