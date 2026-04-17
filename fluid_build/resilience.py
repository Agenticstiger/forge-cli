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

"""Shared resilience primitives used by every provider.

This module exists so ``except CircuitBreakerOpenError`` catches the
same class regardless of which provider's retry/circuit-breaker code
raised it. Provider-local subclasses keep their per-provider signatures
for back-compat; callers that only care "is the circuit open?" can
catch the canonical base.

See CODE_REVIEW C-008: before this module existed, ``CircuitBreakerOpenError``
was defined independently in ``providers/snowflake/util/circuit_breaker.py``
and ``providers/gcp/util/retry.py`` (among others). Same class name, two
different classes — silent behavior differences when callers imported
the wrong one.
"""

from __future__ import annotations


class CircuitBreakerOpenError(Exception):
    """Raised when a circuit breaker is in the OPEN state.

    Provider-specific subclasses (snowflake, gcp, aws, local) may add
    extra context such as ``retry_after_seconds`` or ``service``. Catch
    this base class when you want to handle circuit-open conditions
    uniformly across providers.
    """
