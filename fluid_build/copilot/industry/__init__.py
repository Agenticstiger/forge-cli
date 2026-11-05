# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Typed industry packs for staged data-model forging.

Wraps the existing ``fluid_build.cli.industry_skills`` YAML catalog in a
Pydantic-typed :class:`IndustryPack`.  Stages (Modeler/Logical) consume
the pack structurally instead of parsing a free-text prompt fragment.

Public exports:

* :class:`IndustryPack` — typed industry + canonical-model + compliance
* :class:`IndustryPackCompiler` — ``compile(name)`` → :class:`IndustryPack`
"""

from __future__ import annotations

from fluid_build.copilot.industry.compiler import IndustryPackCompiler
from fluid_build.copilot.industry.pack import (
    CanonicalModel,
    ComplianceProfile,
    IndustryDomain,
    IndustryPack,
)

__all__ = [
    "CanonicalModel",
    "ComplianceProfile",
    "IndustryDomain",
    "IndustryPack",
    "IndustryPackCompiler",
]
