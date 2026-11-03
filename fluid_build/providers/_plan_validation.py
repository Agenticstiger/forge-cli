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
