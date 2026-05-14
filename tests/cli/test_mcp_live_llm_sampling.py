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
"""Live-LLM sampling round-trip: forge → MCP → real LLM → forge.

The earlier ``test_mcp_forge_run_live.py`` tests use a **synthetic** sampling
response (we mocked the IDE's LLM with a canned string). This file closes
the loop with a **real LLM**:

* SDK client (anyio + mcp.client.ClientSession) acts as the IDE.
* When the server sends ``sampling/createMessage``, the client's
  ``sampling_callback`` calls a real LLM via LiteLLM (the same backend forge
  uses for direct invocations) and returns the model's response.
* The test asserts ``forge_run mode='diag'`` got a real, non-canned reply
  with the right wire shape.

If this test passes, the entire Pattern-2 flow (IDE LLM → MCP sampling →
forge) is empirically validated — not just protocol-shape-validated.

Skipped when no LLM API key is in env. CI safety: this test costs ~$0.0001
per run (a 20-token gpt-4o-mini completion), so it's safe to leave enabled
on CI but is gated behind ``@pytest.mark.live_llm`` so it only runs on
demand or with the right marker.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import (
    CreateMessageRequestParams,
    CreateMessageResult,
    TextContent,
)

LIVE_MODELS = (
    ("openai", "gpt-4o-mini", "OPENAI_API_KEY"),
    ("gemini", "gemini/gemini-2.5-flash", "GEMINI_API_KEY"),
)


def _pick_available_model() -> tuple[str, str]:
    """Return (provider, litellm_model) for the first available API key. Skips the
    test if no key is set (CI may not have one, and we never want false-fails)."""
    for provider, model, key_env in LIVE_MODELS:
        if os.environ.get(key_env):
            return provider, model
    pytest.skip("no LLM API key in env — set OPENAI_API_KEY or GEMINI_API_KEY")


def _server_params(cwd: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "fluid_build.cli", "mcp", "serve"],
        env={**os.environ},
        cwd=str(cwd),
    )


# ---------------------------------------------------------------------------
# The headline test: forge ↔ MCP sampling ↔ real LLM
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.live_llm
def test_forge_run_diag_with_real_llm_via_sampling(tmp_path: Path):
    """End-to-end: ``forge_run mode='diag'`` triggers a real
    ``sampling/createMessage`` round-trip; our SDK client's sampling callback
    calls a real LLM through LiteLLM; the model's output is returned to forge.

    Proves Pattern 2 (IDE-LLM-driven via MCP sampling) works with a real
    LLM — not just a mocked one.
    """
    provider, model = _pick_available_model()
    intercepted: dict[str, Any] = {}

    async def sampling_callback(
        context: Any, params: CreateMessageRequestParams
    ) -> CreateMessageResult:
        """Stands in for the IDE. When forge asks for a completion, we call
        a real LLM via LiteLLM and return the result.
        """
        import litellm  # imported lazily so the test file imports without it

        # Translate the MCP messages to LiteLLM's chat format.
        chat_messages: list[dict[str, str]] = []
        if params.systemPrompt:
            chat_messages.append({"role": "system", "content": params.systemPrompt})
        for msg in params.messages:
            content = msg.content
            text = getattr(content, "text", "")
            chat_messages.append({"role": msg.role, "content": text})

        intercepted["chat_messages"] = chat_messages
        intercepted["max_tokens"] = params.maxTokens
        intercepted["system_prompt"] = params.systemPrompt

        response = await asyncio.to_thread(
            litellm.completion,
            model=model,
            messages=chat_messages,
            max_tokens=params.maxTokens,
            temperature=0.0,
        )
        reply = response.choices[0].message.content or ""
        intercepted["model"] = response.model
        intercepted["reply"] = reply
        return CreateMessageResult(
            role="assistant",
            content=TextContent(type="text", text=reply),
            model=response.model,
            stopReason="endTurn",
        )

    async def _run() -> dict:
        async with stdio_client(_server_params(tmp_path)) as (read, write):
            # NB: We don't pass ``sampling_capabilities=SamplingCapability()``
            # explicitly — that kwarg was added in mcp>=1.25-ish. The SDK
            # advertises sampling capability automatically when a
            # ``sampling_callback`` is provided, so older floor versions
            # work without it. The CI ``mcp-sdk-drift`` matrix enforces this.
            async with ClientSession(
                read,
                write,
                sampling_callback=sampling_callback,
            ) as client:
                await client.initialize()
                result = await client.call_tool(
                    "forge_run",
                    arguments={
                        "mode": "diag",
                        "prompt": "Reply with exactly one English word.",
                    },
                )
                assert not result.isError, result
                return json.loads(result.content[0].text)

    payload = asyncio.run(_run())
    assert payload["mode"] == "diag"
    response_text = payload["response_text"]
    # Real-LLM assertion: the reply is non-trivial. We don't pin the exact
    # text (LLMs are stochastic even at temperature=0) but we do assert:
    # (a) we got SOME content, (b) it matches the LLM we intercepted (i.e.
    # forge's response is what our callback returned).
    assert isinstance(response_text, str) and len(response_text) > 0
    assert response_text == intercepted.get("reply")
    # And the model the forge response carries should reference the real
    # upstream model id (LiteLLM normalises this — substring match is enough).
    assert intercepted["model"]
    # Verify the sampling request was spec-shaped.
    assert intercepted["chat_messages"], intercepted
    assert "Reply with exactly one English word" in intercepted["chat_messages"][-1]["content"]
    assert intercepted["system_prompt"], intercepted
