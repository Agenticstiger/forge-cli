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

"""Typed exception hierarchy for the forge-cli staged copilot pipeline.

Ports the vocabulary from the Model AI reference codebase
(``workflow/fluid_ddl_workflow/exceptions.py:4-37``) so operators and
callers can branch on failure type without string-matching exception
messages.

These classes deliberately inherit from the repo-wide
:class:`fluid_build.errors.FluidError` so every existing
``except FluidError:`` handler keeps catching them. That preserves the
error-handling contract that predates the staged pipeline while giving
the new agent plumbing the plan-named exception types.

Hierarchy::

    FluidError                   (fluid_build/errors.py)
      └── FluidGenerationError   — any forge-pipeline failure
            ├── DDLGenerationError   — from-ddl parse/convert failures
            └── AgentExecutionError  — stage agent run failures
                                      (Pydantic coercion, provider call,
                                      tool dispatch)
"""

from __future__ import annotations

from fluid_build.errors import FluidError


class FluidGenerationError(FluidError):
    """Raised when any stage of the forge pipeline fails.

    Use this as the catch-all for forge-pipeline errors that are not
    more specifically a DDL-parse or agent-execution failure — for
    example, emitter errors, schema-validator rejections, or contract
    assembly bugs.
    """


class DDLGenerationError(FluidGenerationError):
    """Raised on DDL parse/convert failures in the ``from-ddl`` path.

    Covers sqlglot failures, the native ``DDLParser`` fallback, and any
    downstream logical-model derivation that can't proceed because the
    source DDL is malformed or unsupported.
    """


class AgentExecutionError(FluidGenerationError):
    """Raised when a stage agent's run fails.

    Wraps provider-call failures, Pydantic coercion errors on stage
    output, tool-dispatch errors, and repair-loop exhaustion. The
    ``original_error`` attribute on the base class carries the
    underlying provider / validation exception so callers can unwrap
    if they need to branch on the root cause.
    """


__all__ = [
    "AgentExecutionError",
    "DDLGenerationError",
    "FluidGenerationError",
]
