# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The typed refusal a provider raises when its own plan fails validation.

Kept apart from any one provider so ``cli/generate_iac.py`` can recognise it by
type without importing a provider package (which would register every provider
on a path that may be about another cloud).
"""

from __future__ import annotations

from fluid_build.providers.base import ProviderError


class PlanValidationError(ProviderError):
    """A provider planned actions its own validator rejected.

    A planner that ran and refused, unlike one that could not run (no SDK, no
    credentials): ``generate iac`` and ``apply`` treat the first as fatal and
    only the second as best-effort. Typed so callers match on the class, never
    on the message.
    """
