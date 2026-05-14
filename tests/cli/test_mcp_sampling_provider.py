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
"""Tests for ``MCPSamplingProvider`` + the SDK sampling-context bridge.

The bridge is the small piece of code in :mod:`fluid_build.cli.mcp` that lets
``MCPSamplingProvider`` (a sync ``LlmProvider`` shim) call out to
``ctx.session.create_message()`` (an async SDK primitive) from a worker
thread. forge runs under ``asyncio.to_thread(forge.run, ...)`` inside an MCP
tool call; the bridge gives it a way to reach the SDK event loop.

Five shapes verified:

1. ``get_llm_provider("mcp-sampling")`` resolves regardless of hyphenation.
2. ``invoke_blocking`` with **no active context** raises
   ``CopilotGenerationError`` with event ``mcp_sampling_unavailable`` and
   actionable suggestions.
3. ``invoke_blocking`` with a **live event loop + fake Context** does the
   round-trip and returns the text from ``CreateMessageResult.content``.
4. The kwargs forge sends to ``ctx.session.create_message`` match the spec
   (``messages``, ``system_prompt``, ``max_tokens``, ``include_context``).
5. ``invoke_streaming`` falls back to blocking (it yields the full text as
   one chunk).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from anyio.from_thread import start_blocking_portal

from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    MCPSamplingProvider,
    get_llm_provider,
)
from fluid_build.cli.mcp import (
    _reset_sampling_context,
    _set_sampling_context,
    get_sampling_context,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeConfig:
    """Minimal LlmConfig stand-in matching the attribute access invoke_blocking does."""

    max_tokens: int = 1024
    temperature: float = 0.0


class _FakeSession:
    """Stand-in for ``ServerSession`` whose ``create_message`` returns a
    canned response. Records the kwargs it received so tests can assert on
    the wire shape forge sends.
    """

    def __init__(self, response_text: str, model: str = "fake-test-model"):
        self.response_text = response_text
        self.model = model
        self.last_call_kwargs: dict[str, Any] | None = None

    async def create_message(self, **kwargs: Any) -> Any:
        self.last_call_kwargs = kwargs
        result = MagicMock()
        result.content = MagicMock()
        result.content.text = self.response_text
        result.model = self.model
        result.stopReason = "endTurn"
        return result


class _FakeContext:
    """Stand-in for the SDK's ``Context``. Only ``session`` is read by the provider."""

    def __init__(self, session: _FakeSession):
        self.session = session


@pytest.fixture
def anyio_portal():
    """Spin an anyio event loop in a background thread via the canonical
    ``start_blocking_portal()`` API. Yields ``(portal, token)`` where
    ``token`` is the anyio event-loop token (the bridge primitive that
    :meth:`MCPSamplingProvider.invoke_blocking` uses to dispatch back into
    the loop via :func:`anyio.from_thread.run`).

    Borrowed-not-built: anyio is already a transitive dep via the MCP SDK
    (FastMCP is built on it), and this is the canonical "run a coroutine
    on the SDK's loop from a non-loop thread" pattern.
    """
    with start_blocking_portal() as portal:
        # Capture the loop's token by running ``current_token()`` *inside*
        # the portal's task (the same way the production code does inside
        # an @_mcp_app.tool() async function).
        async def _get_token() -> Any:
            from anyio.lowlevel import current_token

            return current_token()

        token = portal.call(_get_token)
        yield portal, token


# ---------------------------------------------------------------------------
# 1. Provider resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["mcp-sampling", "mcp_sampling", "MCP-Sampling"])
def test_get_llm_provider_resolves_mcp_sampling(name: str):
    """Both hyphenated + underscored forms must resolve. Capability is too
    important to bury under a typo-sensitive name.
    """
    provider = get_llm_provider(name)
    assert isinstance(provider, MCPSamplingProvider)
    assert provider.name == "mcp-sampling"


# ---------------------------------------------------------------------------
# 2. Actionable error when no sampling context is active
# ---------------------------------------------------------------------------


def test_invoke_blocking_without_context_raises_actionable_error():
    """The operator hits this path when they shell-run
    ``fluid forge --llm-provider mcp-sampling`` from a plain shell — no MCP
    tool wrapping forge, no context installed. The error must be actionable.
    """
    provider = get_llm_provider("mcp-sampling")
    with pytest.raises(CopilotGenerationError) as exc_info:
        provider.invoke_blocking(_FakeConfig(), "sys prompt", "user prompt")

    err = exc_info.value
    assert err.event == "mcp_sampling_unavailable"
    suggestions = " ".join(err.suggestions).lower()
    assert "forge_run" in suggestions or "mcp tool" in suggestions
    assert "fluid_llm_backend=litellm" in suggestions
    assert "api_key" in suggestions


# ---------------------------------------------------------------------------
# 3. Round-trip with a fake event loop + Context
# ---------------------------------------------------------------------------


def test_invoke_blocking_round_trips_through_sdk_context(anyio_portal):
    """The full path: ``call_llm(provider, ...)`` -> context bridge ->
    ``ctx.session.create_message(...)`` running on the SDK event loop ->
    response -> text. This is what forge sees when running inside an MCP
    ``forge_run`` tool call.
    """
    _portal, token = anyio_portal
    session = _FakeSession("the IDE's LLM said this")
    ctx = _FakeContext(session)
    tokens = _set_sampling_context(ctx, token)
    try:
        provider = get_llm_provider("mcp-sampling")
        text = provider.invoke_blocking(
            _FakeConfig(max_tokens=512, temperature=0.1),
            "system goes here",
            "user message",
        )
        assert text == "the IDE's LLM said this"
    finally:
        _reset_sampling_context(tokens)


# ---------------------------------------------------------------------------
# 4. Wire shape — kwargs sent to ctx.session.create_message must match spec
# ---------------------------------------------------------------------------


def test_create_message_kwargs_match_spec(anyio_portal):
    """The provider must send spec-shaped kwargs so any compliant client
    (Cursor / Kiro / Claude Code) accepts the request.
    """
    _portal, token = anyio_portal
    session = _FakeSession("echoed")
    ctx = _FakeContext(session)
    tokens = _set_sampling_context(ctx, token)
    try:
        provider = get_llm_provider("mcp-sampling")
        provider.invoke_blocking(
            _FakeConfig(max_tokens=2048, temperature=0.0),
            "You are forge's authoring agent.",
            "Build a CDP joining users + orders.",
        )
    finally:
        _reset_sampling_context(tokens)

    kwargs = session.last_call_kwargs
    assert kwargs is not None
    assert kwargs["system_prompt"] == "You are forge's authoring agent."
    assert kwargs["max_tokens"] == 2048
    assert kwargs["include_context"] == "thisServer"
    assert kwargs["temperature"] == 0.0
    # messages is a list of SamplingMessage objects.
    messages = kwargs["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    msg = messages[0]
    # SamplingMessage carries role + content (TextContent with text=...)
    assert msg.role == "user"
    assert msg.content.text == "Build a CDP joining users + orders."


# ---------------------------------------------------------------------------
# 5. Streaming falls back to blocking
# ---------------------------------------------------------------------------


def test_invoke_streaming_falls_back_to_blocking(anyio_portal):
    """MCP sampling has no streaming primitive yet — callers must still get
    a string back, delivered as one chunk.
    """
    _portal, token = anyio_portal
    session = _FakeSession("one-shot reply")
    ctx = _FakeContext(session)
    tokens = _set_sampling_context(ctx, token)
    try:
        provider = get_llm_provider("mcp-sampling")
        chunks = list(provider.invoke_streaming(_FakeConfig(), "sys", "user"))
        assert chunks == ["one-shot reply"]
    finally:
        _reset_sampling_context(tokens)


# ---------------------------------------------------------------------------
# 6. Context-bridge get/set/clear sanity
# ---------------------------------------------------------------------------


def test_sampling_context_set_get_clear(anyio_portal):
    """The bridge uses ContextVars; verify the set/reset round-trip leaves
    the context at the default (``None, None``) outside the with-block.
    """
    _portal, token = anyio_portal
    assert get_sampling_context() == (None, None)
    session = _FakeSession("x")
    ctx = _FakeContext(session)
    tokens = _set_sampling_context(ctx, token)
    try:
        got_ctx, got_token = get_sampling_context()
        assert got_ctx is ctx
        assert got_token is token
    finally:
        _reset_sampling_context(tokens)
    assert get_sampling_context() == (None, None)
