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

"""Backwards-compat shim for the typed error catalog.

The definitions moved to the tier-0 shared leaf :mod:`fluid_build._errors`
so ``fluid_build.build_runners`` can import the runtime error types without
inducing a ``build_runners → cli`` edge (enforced by the
``[tool.importlinter]`` contracts). This module re-exports them verbatim so
the long-standing ``from fluid_build.cli._errors import ...`` /
``from ._errors import ...`` sites across the CLI (and the providers, and the
tests) keep working unchanged.
"""

from __future__ import annotations

from fluid_build._errors import (
    _DOC_BASE,
    BudgetExceededError,
    CapabilityMismatchError,
    ConnectivityProbeError,
    DLQOverflowError,
    FluidUserError,
    InfraDriftError,
    LockHeldError,
    MissingExtraError,
    PartialFailureError,
    ResidencyViolationError,
    SchemaDriftError,
    SchemaValidationError,
    SecretResolutionError,
    SovereigntyViolationError,
    StaleReplayError,
    SupplyChainViolationError,
)

__all__ = [
    "_DOC_BASE",
    "BudgetExceededError",
    "CapabilityMismatchError",
    "ConnectivityProbeError",
    "DLQOverflowError",
    "FluidUserError",
    "InfraDriftError",
    "LockHeldError",
    "MissingExtraError",
    "PartialFailureError",
    "ResidencyViolationError",
    "SchemaDriftError",
    "SchemaValidationError",
    "SecretResolutionError",
    "SovereigntyViolationError",
    "StaleReplayError",
    "SupplyChainViolationError",
]
