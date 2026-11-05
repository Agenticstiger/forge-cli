# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
