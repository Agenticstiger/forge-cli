# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pluggable Transformation Engine framework.

Public API::

    from fluid_build.engines import get_engine, list_engines, has_engine

    engine = get_engine("dbt")
    if engine:
        files = engine.generate(contract, build)
"""

# Auto-discover and register all engine subpackages.
# Each engine subpackage uses @register_engine on its class.
# New engines are picked up automatically — no need to edit this file.
import importlib
import pkgutil

from .base import (
    GenerationResult,
    Severity,
    TransformationEngine,
    TransformationIntent,
    ValidationIssue,
)
from .registry import (
    get_engine,
    has_engine,
    list_engines,
    list_engines_for_platform,
    register_engine,
)

for _finder, _name, _ispkg in pkgutil.iter_modules(__path__):
    if _ispkg:
        try:
            importlib.import_module(f"{__name__}.{_name}")
        except ImportError:
            pass  # Engine has unmet optional dependencies — skip silently

__all__ = [
    "GenerationResult",
    "Severity",
    "TransformationEngine",
    "TransformationIntent",
    "ValidationIssue",
    "get_engine",
    "has_engine",
    "list_engines",
    "list_engines_for_platform",
    "register_engine",
]
