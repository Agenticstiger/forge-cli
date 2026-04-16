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
