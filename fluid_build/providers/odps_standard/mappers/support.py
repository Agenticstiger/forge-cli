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
