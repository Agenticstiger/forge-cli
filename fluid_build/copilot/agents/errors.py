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

The classes provide stable failure categories so operators and callers
can branch on failure type without string-matching exception messages.

These classes deliberately inherit from the repo-wide
:class:`fluid_build.errors.FluidError` so every existing
``except FluidError:`` handler keeps catching them. That preserves the
error-handling contract that predates the staged pipeline while giving
the new agent plumbing the plan-named exception types.

Hierarchy::

    FluidError                       (fluid_build/errors.py)
      └── FluidGenerationError       — any forge-pipeline failure
            ├── DDLGenerationError   — from-ddl parse/convert failures
            └── AgentExecutionError  — stage agent run failures
                  ├── ProviderError              — LLM provider call failures
                  │     ├── RateLimitError       — 429 / quota exceeded
                  │     ├── ProviderTimeoutError — request timeout
                  │     ├── ContextOverflowError — prompt exceeds context window
                  │     ├── ProviderAuthError    — 401 / 403 / bad key
                  │     └── ProviderServerError  — 5xx / transient
                  ├── SchemaValidationError      — LLM output failed schema
                  └── ToolValidationError        — tool args / dispatch failed

The four ``ProviderError`` subclasses exist so :func:`retry_with_backoff`
can branch on type instead of grepping exception messages, and so the
agent loop can route schema/tool failures back to the LLM as corrective
context (which generic exceptions can't).
"""

from __future__ import annotations

from typing import Optional

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


# ---------------------------------------------------------------------------
# Provider-call failure taxonomy.
#
# Generic ``except Exception`` retries waste credits on rate limits
# (where the right move is honoring ``Retry-After``) and on
# context-overflow (where the right move is to fail fast and compact).
# These typed classes let :func:`retry_with_backoff` make the right
# decision per failure mode.
# ---------------------------------------------------------------------------


class ProviderError(AgentExecutionError):
    """Base class for all LLM provider-call failures.

    Subclasses cover the operationally distinct failure modes
    (rate-limit, timeout, context-overflow, auth, server). Calling code
    can ``except ProviderError`` to catch any provider-side failure or
    branch on a specific subclass for targeted recovery.

    ``retry_after`` carries a server-supplied ``Retry-After`` value in
    seconds when present, so callers can honor the provider's hint
    instead of guessing exponential backoff.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    """Raised on HTTP 429 / quota-exceeded responses.

    Distinct from generic ``ProviderError`` so retry logic can honor
    ``Retry-After`` (or fall back to exponential backoff with jitter)
    instead of treating it as a generic transient.
    """


class ProviderTimeoutError(ProviderError):
    """Raised when the HTTP request to the provider times out."""


class ContextOverflowError(ProviderError):
    """Raised when the prompt + completion would exceed the model's
    context window.

    Non-retryable — retrying with the same payload guarantees the same
    failure. Callers should compact / summarize the message history
    before re-attempting.
    """


class ProviderAuthError(ProviderError):
    """Raised on 401 / 403 / bad-credential responses.

    Non-retryable — the agent loop should surface this to the user
    immediately so they can fix their API key.
    """


class ProviderServerError(ProviderError):
    """Raised on 5xx responses from the provider.

    Retryable with backoff; the provider is having a transient issue.
    """


# ---------------------------------------------------------------------------
# Output-validation failures.
#
# These are fundamentally different from provider-call failures: the
# call succeeded, the LLM produced output, but the output was wrong.
# The right recovery is to send corrective feedback back to the LLM,
# not to retry the same prompt.
# ---------------------------------------------------------------------------


class SchemaValidationError(AgentExecutionError):
    """Raised when an LLM response fails Pydantic / JSON-schema validation.

    Carries enough structure for the agent loop to route a corrective
    message back to the LLM (``"your previous output failed validation
    with the following errors: ..."``) instead of blindly retrying the
    same prompt.
    """

    def __init__(
        self,
        message: str,
        *,
        schema_name: str = "",
        validation_errors: Optional[list] = None,
        raw_output: str = "",
    ) -> None:
        super().__init__(message)
        self.schema_name = schema_name
        self.validation_errors = validation_errors or []
        self.raw_output = raw_output


class ToolValidationError(AgentExecutionError):
    """Raised when a tool call fails validation or dispatch.

    Covers: unknown tool name, missing required args, args of wrong
    type, security-confined path violations, tool impl exceptions.
    The ``tool_name`` and ``tool_args`` attributes let the agent loop
    surface a precise corrective message to the LLM.
    """

    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        tool_args: Optional[dict] = None,
        reason: str = "",
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self.reason = reason


__all__ = [
    "AgentExecutionError",
    "ContextOverflowError",
    "DDLGenerationError",
    "FluidGenerationError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "RateLimitError",
    "SchemaValidationError",
    "ToolValidationError",
]
