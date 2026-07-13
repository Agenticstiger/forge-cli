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

"""MCP sampling-context bridge (tier-0 shared leaf).

When a tool that drives forge's copilot starts, it captures the active MCP SDK
``Context`` and an ``anyio`` event-loop token (the canonical bridge between a
worker thread and the SDK's anyio loop). The values live in
:class:`contextvars.ContextVar` so they propagate automatically across the
``await asyncio.to_thread(...)`` boundary (Python ≥3.9 guarantees this).

Two sides read/write this state:

* ``fluid_build.cli.mcp.server`` (the FastMCP tool dispatch) *sets* it around a
  copilot-driving tool call via :func:`_set_sampling_context` /
  :func:`_reset_sampling_context`.
* ``fluid_build.llm.providers.MCPSamplingProvider`` (running in the worker
  thread) *reads* it via :func:`get_sampling_context` and dispatches
  ``ctx.session.create_message`` back into the SDK's loop.

The two ``ContextVar`` objects previously lived in ``cli/mcp/models.py``; the
LLM provider surface (now ``fluid_build.llm``) needs to read them without
importing anything under ``cli``, so they moved here — a stdlib-only tier-0 leaf
that both the ``cli.mcp`` server and the ``llm`` runtime import. ``cli.mcp.models``
re-exports these names so its existing callers keep resolving them unchanged.

This module stays SDK-free: the ``Context`` type is annotated as ``Any`` so no
``mcp`` import lands on the ``fluid --help`` cold path (pinned by
``tests/perf/test_startup_budget.py``).

Borrowed-not-built per /borrow-before-build:
  contextvars — Python stdlib (https://docs.python.org/3/library/contextvars.html);
  anyio.from_thread — https://anyio.readthedocs.io/en/stable/threads.html
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional, Tuple

_SAMPLING_CTX: ContextVar[Optional[Any]] = ContextVar("forge_mcp_sampling_ctx", default=None)
_SAMPLING_TOKEN: ContextVar[Optional[Any]] = ContextVar(
    "forge_mcp_sampling_anyio_token", default=None
)


def _set_sampling_context(ctx: Optional[Any], anyio_token: Optional[Any]) -> Tuple:
    """Set the active sampling context. Returns a token tuple for
    :func:`_reset_sampling_context` to undo (use in a ``try/finally``)."""
    ctx_token = _SAMPLING_CTX.set(ctx)
    token_token = _SAMPLING_TOKEN.set(anyio_token)
    return ctx_token, token_token


def _reset_sampling_context(tokens: Tuple) -> None:
    """Restore the previous sampling context. Symmetric to :func:`_set_sampling_context`."""
    ctx_token, token_token = tokens
    _SAMPLING_CTX.reset(ctx_token)
    _SAMPLING_TOKEN.reset(token_token)


def get_sampling_context() -> Tuple[Optional[Any], Optional[Any]]:
    """Return ``(ctx, anyio_token)`` if a tool with sampling-capable Context
    is active. Read by ``fluid_build.llm.providers.MCPSamplingProvider`` to
    route LLM calls back through ``ctx.session.create_message`` to the IDE.
    """
    return _SAMPLING_CTX.get(), _SAMPLING_TOKEN.get()


__all__ = [
    "_SAMPLING_CTX",
    "_SAMPLING_TOKEN",
    "_reset_sampling_context",
    "_set_sampling_context",
    "get_sampling_context",
]
