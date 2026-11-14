# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Compatibility shim: Command Center imports ``build_capability_matrix`` from this path.

The capability matrix lives in :mod:`fluid_build.cli._capability_matrix`; keep this
import path working.
"""

from __future__ import annotations

from fluid_build.cli._capability_matrix import (  # noqa: F401
    COPILOT_BUILTIN_PROVIDERS,
    KNOWN_BUILD_ENGINES,
    PROVIDER_ENGINE_COMPATIBILITY,
    build_capability_matrix,
    clear_capability_matrix_cache,
)

__all__ = [
    "COPILOT_BUILTIN_PROVIDERS",
    "KNOWN_BUILD_ENGINES",
    "PROVIDER_ENGINE_COMPATIBILITY",
    "build_capability_matrix",
    "clear_capability_matrix_cache",
]
