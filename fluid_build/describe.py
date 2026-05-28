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

"""
Library-level self-description for the FLUID forge engine.
Importable as: from fluid_build.describe import self_describe
Used by the CC backend to serve GET /api/v1/forge/capabilities.

Pattern adapted from `pulumi about --json` (flat object, top-level keys
per category). See borrow-before-build receipts in the feat/forge-cc-alignment
PR.
"""
from __future__ import annotations

import importlib.util
import sys
from typing import Any, Dict

try:
    import fluid_build

    _fluid_version = fluid_build.__version__
except Exception:
    _fluid_version = "unknown"

# Each capability flag is *derived* from whether its backing module is
# importable in this installation — never asserted as a constant. Mirrors
# pulumi `about`, which discovers plugins/runtime rather than shipping a
# static table that drifts from reality. Add a row here when a new
# capability ships a discoverable module.
_CAPABILITY_MODULES: Dict[str, str] = {
    "lineage": "fluid_build.api.lineage",
    "airflow_dag_gen": "fluid_build.cli._init_dag_helpers",
    "engine_api": "fluid_build.engine",
}


def _detect_capabilities() -> Dict[str, bool]:
    detected: Dict[str, bool] = {}
    for name, module in _CAPABILITY_MODULES.items():
        try:
            detected[name] = importlib.util.find_spec(module) is not None
        except Exception:
            detected[name] = False
    return detected


def self_describe() -> Dict[str, Any]:
    """
    Return a machine-readable description of this forge-cli installation.

    Pattern adapted from `pulumi about --json` (flat object, top-level keys
    per category). See borrow-before-build receipts.

    Returns a dict with stable keys:
      - fluid_version: str
      - python_version: str
      - schema_version: str
      - providers: list[str]
      - build_engines: list[str]
      - templates: list[str]
      - provider_engine_compatibility: dict
      - capabilities: dict[str, bool]
      - warnings: list[str]
    """
    from fluid_build.schema_manager import FluidSchemaManager

    try:
        from fluid_build.cli.forge_copilot_runtime import build_capability_matrix

        matrix = build_capability_matrix()
    except Exception:
        matrix = {
            "providers": [],
            "templates": {},
            "build_engines": [],
            "warnings": ["capability matrix unavailable"],
        }

    return {
        "fluid_version": _fluid_version,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "schema_version": FluidSchemaManager.latest_bundled_version(),
        "providers": matrix.get("providers", []),
        "build_engines": matrix.get("build_engines", []),
        "templates": list(matrix.get("templates", {}).keys()),
        "provider_engine_compatibility": matrix.get("provider_engine_compatibility", {}),
        "capabilities": _detect_capabilities(),
        "warnings": matrix.get("warnings", []),
    }
