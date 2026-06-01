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

"""LLM provider adapters and configuration for the forge copilot."""

from __future__ import annotations

__all__ = [
    "CopilotGenerationError",
    "LlmConfig",
    "LlmReadinessCheck",
    "LlmProvider",
    "OpenAIProvider",
    "OllamaProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "BUILTIN_LLM_PROVIDERS",
    "PROVIDER_DISPLAY_NAMES",
    "check_llm_readiness",
    "clear_api_key_from_keyring",
    "get_catalog_default",
    "get_catalog_routing_model",
    "get_catalog_tier_models",
    "get_cumulative_prompt_cache_metrics",
    "build_llm_run_plan",
    "detect_ollama_available",
    "detect_provider_from_api_key",
    "has_llm_api_key",
    "get_llm_provider",
    "normalize_llm_provider_name",
    "query_ollama_models",
    "reset_llm_caches",
    "resolve_llm_config",
    "resolve_model_name",
    "resolve_ollama_model",
    "save_api_key_to_keyring",
    "call_llm",
    "call_llm_streaming",
    "streaming_is_enabled",
    "live_provider_models",
]

import json
import logging
import os
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

import httpx

from fluid_build.cli._common import CLIError

# Per-provider response_format helpers (``openai_response_format``,
# ``gemini_response_schema_config``, ``anthropic_tool_definition``,
# ``ollama_supports_structured_output``) used to be imported here for
# the deleted per-provider classes. They're now superseded by litellm's
# unified ``response_format={"type": "json_schema", ...}`` directive
# which normalises to whatever the underlying provider needs. The
# ``forge_copilot_response_schema`` module retains only the canonical
# ``FORGE_RESPONSE_SCHEMA`` constant + the strict-mode hardening
# helper for OpenAI's strict json_schema mode.

LOG = logging.getLogger("fluid.cli.forge_copilot.llm")


# ---------------------------------------------------------------------------
# Streaming usage capture
#
# Closes the "record_missing_usage on every streaming run" gap. SSE
# streams from all three cloud providers carry token-usage events
# (Anthropic ``message_delta.usage``, OpenAI final chunk's ``usage``
# field when ``stream_options.include_usage`` is set, Gemini's
# ``usageMetadata`` on the terminal chunk). The legacy
# ``iter_stream_chunks`` discarded these. We now stash them in a
# thread-local so the streaming caller (``BaseStageAgent._call_once``)
# can pull them back out after the SSE iterator drains.
#
# Thread-local because providers are module-level singletons and the
# coordinator runs stages in a ThreadPoolExecutor — using an instance
# attribute would race across concurrent stages on the same provider.
# ---------------------------------------------------------------------------


_streaming_usage_state = threading.local()


def _record_streaming_usage(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Stash a per-call usage record on the current thread.

    Called from each provider's ``iter_stream_chunks`` when a
    usage-bearing SSE event arrives. The record is read back via
    :func:`consume_streaming_usage` after the SSE iterator drains;
    callers should always pop (rather than peek) to avoid leaking
    state across calls on long-lived threads.
    """
    _streaming_usage_state.usage = {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int((input_tokens or 0) + (output_tokens or 0)),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "cache_write_tokens": int(cache_write_tokens or 0),
    }


def consume_streaming_usage() -> Optional[Dict[str, int]]:
    """Pop the most recent streaming-usage record for this thread.

    Returns ``None`` if no streaming call has recorded usage on the
    current thread (or if the previous record has already been
    consumed). Pops the value so a subsequent blocking call doesn't
    accidentally see a stale streaming record.
    """
    usage = getattr(_streaming_usage_state, "usage", None)
    _streaming_usage_state.usage = None
    return usage


def _structured_outputs_enabled() -> bool:
    """Slice UX-I kill-switch for provider-native JSON enforcement.

    Set ``FLUID_LLM_STRUCTURED_OUTPUTS=0`` to disable structured
    outputs across all providers and fall back to the legacy
    text-JSON + ``extract_json_object`` pipeline.  Intended as a
    break-glass escape for users on models that silently reject the
    new response-format directives.
    """
    value = os.environ.get("FLUID_LLM_STRUCTURED_OUTPUTS", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# Determinism controls
# ---------------------------------------------------------------------------

# The legacy ``_OPENAI_SEED = 42`` constant was deleted with the
# native ``OpenAIProvider.build_request`` it served. Determinism is
# now requested via ``litellm.completion(temperature=0)`` plus
# ``seed=42`` when the underlying provider supports it; litellm passes
# ``seed`` through to OpenAI / Bedrock and silently ignores it
# elsewhere, which is exactly the behaviour we used to hand-roll.


def _get_temperature() -> float:
    """Return the LLM sampling temperature.

    Defaults to ``0.0`` (fully deterministic) which is appropriate for
    structured-JSON contract generation.  Override with
    ``FLUID_LLM_TEMPERATURE`` for experimentation.
    """
    raw = os.environ.get("FLUID_LLM_TEMPERATURE", "0.0")
    try:
        return max(0.0, min(2.0, float(raw)))
    except ValueError:
        return 0.0


# Anthropic deprecated the ``temperature`` parameter on newer models
# (opus-4-7 onward, plus reasoning-style models). Sending it produces
# ``400 invalid_request_error: 'temperature' is deprecated for this
# model.`` Detection is by model-name prefix because Anthropic doesn't
# advertise this as a structured capability flag — the API just
# rejects the request. Update this set as more models join.
_ANTHROPIC_TEMPERATURE_DEPRECATED_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
)


def _model_deprecates_temperature(model: str) -> bool:
    """Return True when the Anthropic model rejects ``temperature``.

    Used by :class:`AnthropicProvider.build_request` to skip the
    field for models in the deprecated set. Conservative — defaults
    to False (still send temperature) for unknown models so older
    Sonnet / Haiku continue to work.
    """
    if not model:
        return False
    lowered = model.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _ANTHROPIC_TEMPERATURE_DEPRECATED_PREFIXES)


def _get_timeout_seconds(args: Any, env: Mapping[str, str]) -> int:
    raw = getattr(args, "llm_timeout_seconds", None) or env.get("FLUID_LLM_TIMEOUT_SECONDS")
    if raw in (None, ""):
        return 120
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 120
    return max(1, min(3600, value))


def streaming_is_enabled() -> bool:
    """Slice UX-I kill-switch for SSE streaming of LLM responses.

    Set ``FLUID_LLM_STREAMING=0`` to disable streaming across all
    providers.  The legacy blocking ``call_llm`` path is preserved
    in full; every call site that wants streaming checks this helper
    and falls back to ``call_llm`` when it returns False.
    """
    value = os.environ.get("FLUID_LLM_STREAMING", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


# Provider → environment variable mapping.  Shared across ai_setup.py and
# this module to avoid duplication.
PROVIDER_ENV_VARS: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    # GitHub Models — litellm's ``github/`` provider reads GITHUB_API_KEY.
    # Inside GitHub Actions this is the built-in GITHUB_TOKEN (granted the
    # ``models: read`` permission), so CI exercises the LLM path with no
    # provider API key at all. Selected explicitly via
    # ``--llm-provider github`` / ``FLUID_LLM_PROVIDER=github`` — github is
    # deliberately NOT added to the auto-inference helpers, so a stray
    # GITHUB_TOKEN present in any CI environment can never hijack provider
    # selection.
    "github": "GITHUB_API_KEY",
}

PROVIDER_DISPLAY_NAMES: Dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "ollama": "Ollama (local)",
}


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class CopilotGenerationError(CLIError):
    """Structured error for copilot generation failures."""

    def __init__(
        self,
        event: str,
        message: str,
        suggestions: Optional[List[str]] = None,
        context: Optional[Dict[str, Any]] = None,
    ):
        payload = {"message": message}
        if context:
            payload.update(context)
        super().__init__(1, event, payload)
        self.message = message
        self.suggestions = suggestions or []


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class LlmConfig:
    """Resolved configuration for a provider-backed LLM call."""

    provider: str
    model: str
    endpoint: str
    api_key: Optional[str]
    timeout_seconds: int = 120
    # Slice UX-I: opt-in/out of server-sent-event streaming on
    # supported providers.  Defaults to True so the user sees tokens
    # flowing instead of a silent spinner.  Set to False (or set
    # ``FLUID_LLM_STREAMING=0``) to fall back to the legacy blocking
    # ``call_llm`` path.  The blocking path is preserved in full for
    # providers/models that don't yet support streaming, and for any
    # user who needs a break-glass escape.
    streaming: bool = True
    # Slice UX-J: optional cheap/fast "routing" model used for the
    # interview clarification round and other non-critical LLM calls.
    # When unset, falls back to the strong ``model`` for everything.
    # ``resolve_llm_config`` populates these from
    # ``FLUID_LLM_ROUTING_MODEL`` / ``FLUID_LLM_ROUTING_ENDPOINT``
    # env vars, or from provider-specific defaults if available.
    routing_model: Optional[str] = None
    routing_endpoint: Optional[str] = None
    model_source: str = "catalog"
    model_resolution_notes: List[str] = field(default_factory=list)
    tier_models: Dict[str, str] = field(default_factory=dict)
    # Drive mode for coding-agent providers: "envelope" (agent returns the
    # response JSON on stdout — the default) or "agentic" (agent writes
    # contract.fluid.yaml into the workspace with its own tools). Ignored by
    # every other provider.
    agent_mode: str = "envelope"

    def for_routing(self) -> "LlmConfig":
        """Return a shallow copy configured for the routing model.

        If no routing model is set, returns ``self`` unchanged — so
        callers don't need to branch on ``routing_model``.
        """
        if not self.routing_model:
            return self
        import dataclasses

        overrides: Dict[str, Any] = {"model": self.routing_model}
        if self.routing_endpoint:
            overrides["endpoint"] = self.routing_endpoint
        else:
            # Re-derive endpoint for the routing model using the same
            # provider's default-endpoint logic.
            provider = BUILTIN_LLM_PROVIDERS.get(self.provider)
            if provider:
                overrides["endpoint"] = provider.default_endpoint(
                    self.routing_model, dict(os.environ)
                )
        return dataclasses.replace(self, **overrides)

    @property
    def redacted_endpoint(self) -> str:
        endpoint = self.endpoint
        # Redact common credential query parameters
        endpoint = re.sub(
            r"([?&](?:key|token|api_key|auth|secret|credential|password)=)[^&]+",
            r"\1***",
            endpoint,
            flags=re.I,
        )
        # Redact userinfo in URLs (user:pass@host)
        endpoint = re.sub(r"(https?://)([^@/]+)@", r"\1***:***@", endpoint)
        return endpoint


# ---------------------------------------------------------------------------
# Provider Interface & Implementations
# ---------------------------------------------------------------------------


class LlmProvider(ABC):
    """Interface for provider-specific request/response translation."""

    name: str
    default_model: str

    @abstractmethod
    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        """Return the provider's default endpoint for the resolved model."""

    @abstractmethod
    def build_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        """Build request headers and JSON payload."""

    @abstractmethod
    def extract_text(self, response_json: Dict[str, Any]) -> str:
        """Extract free-form response text from the provider response."""

    def extract_usage(self, response_json: Dict[str, Any]) -> Dict[str, int]:
        """Extract token usage from the provider response.

        Returns ``{input_tokens, output_tokens, total_tokens}``.
        Default implementation returns zeros; providers override with
        their specific response shapes.
        """
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def extract_prompt_cache(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """Extract prompt-cache usage from the provider response."""
        return _prompt_cache_metrics(0, 0)

    def list_available_models(
        self, api_key: Optional[str], env: Mapping[str, str]
    ) -> Optional[List[str]]:
        """Return live provider model ids when a model-list API exists.

        Providers without a lightweight list endpoint return ``None``.
        Callers should treat that as "preflight unavailable", not as
        "this provider has no models".
        """
        return None

    def choose_available_model(
        self, requested_model: str, available_models: List[str]
    ) -> Optional[str]:
        """Pick a live replacement for a stale catalog default."""
        return requested_model if requested_model in available_models else None

    # ------------------------------------------------------------------
    # Streaming (slice UX-I)
    # ------------------------------------------------------------------
    #
    # Streaming is a sibling to the blocking request path, not a
    # replacement.  ``build_streaming_request`` returns a triple of
    # (url, headers, payload) because Gemini's streaming endpoint
    # differs from its blocking endpoint (``:streamGenerateContent``
    # vs ``:generateContent``), so we can't reuse ``config.endpoint``
    # directly.  ``iter_stream_chunks`` consumes the SSE response and
    # yields text deltas — concatenating those deltas produces the
    # same string that ``extract_text`` would have returned on the
    # blocking path.

    def build_streaming_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        """Return (url, headers, payload) for a streaming request.

        Default implementation reuses ``build_request`` and adds
        ``stream: True`` to the payload.  Providers whose streaming
        endpoint differs from their blocking endpoint (Gemini) must
        override this.
        """
        headers, payload = self.build_request(config, system_prompt, user_prompt)
        payload = dict(payload)
        payload["stream"] = True
        return config.endpoint, headers, payload

    def iter_stream_chunks(self, response: httpx.Response) -> Iterator[str]:
        """Yield text deltas from an SSE-streamed response.

        Default implementation drops the generator silently; every
        concrete provider overrides this with its own SSE parser.
        """
        return
        yield  # pragma: no cover — makes this a generator for type purposes

    # ------------------------------------------------------------------
    # Tool use / agent loop (slice UX-K)
    # ------------------------------------------------------------------
    #
    # These methods are siblings to the single-shot ``build_request`` /
    # ``extract_text`` path.  They're only called from
    # ``forge_copilot_agent_loop.run_copilot_agent_loop`` when
    # ``--agent-loop`` is set; the default single-shot flow in
    # ``generate_copilot_artifacts`` never touches them.

    def build_tool_request(
        self,
        config: LlmConfig,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        """Build (url, headers, payload) for a multi-turn tool-use call.

        Must be overridden by providers that support tool use.
        Default raises ``NotImplementedError``.
        """
        raise NotImplementedError(f"Provider {self.name} does not support tool use")

    def extract_tool_calls(self, response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool calls from a provider response.

        Returns a list of ``{"id": str, "name": str, "arguments": dict}``
        dicts, or an empty list if the response has no tool calls
        (i.e. the model emitted a final text response instead).
        """
        return []

    def extract_text_from_tool_response(self, response_json: Dict[str, Any]) -> Optional[str]:
        """Extract final text content from a tool-use response.

        Returns ``None`` if the response only contains tool calls and
        no final text.
        """
        try:
            return self.extract_text(response_json)
        except (KeyError, IndexError):
            return None

    def build_tool_result_messages(
        self, tool_calls: List[Dict[str, Any]], results: List[Any]
    ) -> List[Dict[str, Any]]:
        """Build the message(s) to feed tool results back to the LLM.

        Must be overridden per provider because the message format
        for tool results differs between OpenAI/Anthropic/Gemini.
        """
        raise NotImplementedError(f"Provider {self.name} does not support tool result messages")


@dataclass
class ToolCall:
    """A parsed tool call from an LLM response."""

    id: str
    name: str
    arguments: Dict[str, Any]


def _sync_provider_defaults_from_catalog() -> None:
    """Override provider class ``default_model`` attrs with catalog defaults.

    Called once at module import time.  This makes the catalog the
    single source of truth for model defaults — the hardcoded strings
    on the provider classes are safety fallbacks only, used when the
    catalog file is missing or corrupt.
    """
    try:
        catalog = _load_model_catalog()
        providers_data = catalog.get("providers", {})
        for name, provider in BUILTIN_LLM_PROVIDERS.items():
            if name == "claude":
                continue  # alias for anthropic, shares the same instance
            entry = providers_data.get(name, {})
            default_model = entry.get("default") or entry.get("flagship")
            if default_model:
                provider.default_model = default_model
    except Exception:  # noqa: BLE001 — never break import
        pass


def normalize_llm_provider_name(value: Any) -> str:
    """Normalize LLM provider aliases (openai, anthropic, gemini, ollama).

    Unlike ``normalize_provider_name`` in ``forge_copilot_runtime`` (which
    handles infrastructure providers like gcp/aws/local), this function
    understands LLM-specific aliases such as ``"claude"`` → ``"anthropic"``.
    """
    if value is None:
        return "gemini"
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized == "claude":
        return "anthropic"
    return normalized


def get_llm_provider(name: str) -> LlmProvider:
    """Resolve a provider adapter by name.

    Every provider in fluid is a :class:`LiteLLMProvider` shim — the
    native per-provider httpx classes were deleted in favour of
    litellm's unified backend. ``name`` is canonicalised (``"claude"``
    → ``"anthropic"``) and dispatched through
    :func:`get_litellm_provider`.

    Special case ``"mcp-sampling"`` / ``"mcp_sampling"``: returns the
    :class:`MCPSamplingProvider` shim which routes LLM calls back to the
    MCP client (the IDE) via ``sampling/createMessage``. The IDE pays for
    the LLM; forge never sees an API key. Requires an active sampling
    channel — typically installed when forge runs inside the ``forge_run``
    MCP tool.

    ``litellm`` is a hard dependency of fluid (declared in
    ``pyproject.toml``); the import never fails in a working install.
    """
    normalized = (name or "").strip().lower()
    if normalized in ("mcp-sampling", "mcp_sampling"):
        return MCPSamplingProvider()
    # Coding-agent providers (claude-code/codex/cursor/kiro) shell out to a
    # local agent CLI — keyless for Claude Code, key-reusing for the rest.
    # Resolved BEFORE the litellm delegation so their hyphenated names aren't
    # collapsed by ``normalize_llm_provider_name`` (claude-code -> anthropic).
    from fluid_build.cli.forge_copilot_coding_agent import (
        get_coding_agent_provider,
        is_coding_agent,
    )

    if is_coding_agent(normalized):
        return get_coding_agent_provider(normalized)
    from fluid_build.cli.forge_copilot_llm_litellm import get_litellm_provider

    return get_litellm_provider(normalize_llm_provider_name(normalized))


class MCPSamplingProvider(LlmProvider):
    """Route LLM calls through the MCP client's ``sampling/createMessage``.

    Why this exists
    ---------------
    Data-team members on Cursor / Kiro / Claude Code shouldn't need a
    separate LLM API key — their IDE already pays for an LLM. MCP sampling
    is the canonical spec mechanism: the server (forge) asks the client
    (IDE) to make the LLM call. Human-in-the-loop is mandatory per spec —
    the IDE shows the user the prompt before sending and the user approves.

    Implementation
    --------------
    Borrowed-not-built on the official
    `modelcontextprotocol/python-sdk <https://github.com/modelcontextprotocol/python-sdk>`_
    (which itself is built on `anyio <https://anyio.readthedocs.io>`_).
    The bridge to the SDK is the ``(ctx, anyio_token)`` pair stored in
    :class:`contextvars.ContextVar` (Python stdlib, request-scoped state
    propagates across ``asyncio.to_thread`` automatically since 3.9). The
    FastMCP ``forge_run`` tool installs them on entry and clears on exit.
    From a worker thread (forge runs in ``asyncio.to_thread``),
    :meth:`invoke_blocking` calls :func:`anyio.from_thread.run` —
    the canonical anyio primitive for "call async code from a
    non-event-loop thread" — to dispatch ``ctx.session.create_message``
    back into the SDK's loop. The SDK handles every detail of the
    spec-compliant wire format, request-id correlation, capability
    checks, and human-in-the-loop UX.

    How it's invoked
    ----------------
    Set ``FLUID_LLM_BACKEND=mcp-sampling`` (or pass ``--llm-provider
    mcp-sampling``). ``call_llm(provider, ...)`` then routes through this
    class's :meth:`invoke_blocking`, which reads the active sampling
    context and raises an actionable :class:`CopilotGenerationError` if
    forge is running outside an MCP tool-call context.

    Streaming is not supported in v1; callers fall back to blocking.
    """

    name = "mcp-sampling"
    default_model = "mcp-sampling"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        # No HTTP endpoint — the transport is stdio JSON-RPC to the MCP client.
        return "mcp://sampling"

    def build_request(
        self, config: "LlmConfig", system_prompt: str, user_prompt: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        # ``call_llm`` calls ``invoke_blocking`` directly on the provider —
        # this method is only invoked via the legacy HTTP path which doesn't
        # apply to MCP sampling. Return inert placeholders so the abstract
        # contract is satisfied.
        return ({}, {})

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        # Same: not used by the MCP sampling path.
        return str(response_json.get("text", ""))

    def invoke_blocking(
        self,
        config: "LlmConfig",
        system_prompt: str,
        user_prompt: str,
        *,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        # Lazy imports avoid an import cycle: ``mcp.py`` imports the
        # provider catalog from this module (via LiteLLM), so importing
        # ``mcp`` at module-load time here would deadlock.
        import anyio.from_thread
        from mcp.types import SamplingMessage, TextContent

        from fluid_build.cli.mcp import get_sampling_context

        ctx, anyio_token = get_sampling_context()
        if ctx is None or anyio_token is None:
            raise CopilotGenerationError(
                "mcp_sampling_unavailable",
                "MCP sampling context not active. forge_run installs it for "
                "the duration of an MCP tool call; outside that context the "
                "IDE has no way to receive sampling requests.",
                suggestions=[
                    "Invoke forge via the `forge_run` MCP tool from your IDE "
                    "(installs the sampling context automatically).",
                    "Or set FLUID_LLM_BACKEND=litellm and an LLM API key "
                    "(ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY).",
                ],
            )

        max_tokens = int(getattr(config, "max_tokens", 4096) or 4096)
        temperature = getattr(config, "temperature", None)
        sampling_kwargs: Dict[str, Any] = {
            "messages": [
                SamplingMessage(
                    role="user",
                    content=TextContent(type="text", text=user_prompt),
                )
            ],
            "max_tokens": max_tokens,
            "system_prompt": system_prompt,
            "include_context": "thisServer",
        }
        if temperature is not None:
            sampling_kwargs["temperature"] = float(temperature)

        # We're on a worker thread (forge runs under ``asyncio.to_thread``).
        # ``anyio.from_thread.run`` dispatches the coroutine to the SDK's
        # event loop and blocks this thread until the response arrives.
        # The ``token`` was captured by the ``forge_run`` tool via
        # ``anyio.lowlevel.current_token()`` and propagated here through
        # the ContextVar (auto-propagates across ``asyncio.to_thread``).
        async def _do_sample() -> Any:
            return await ctx.session.create_message(**sampling_kwargs)

        try:
            result = anyio.from_thread.run(_do_sample, token=anyio_token)
        except Exception as exc:  # noqa: BLE001
            raise CopilotGenerationError(
                "mcp_sampling_failed",
                f"MCP sampling round-trip failed: {exc}",
                suggestions=[
                    "Check that your IDE supports the 'sampling' capability.",
                    "Fall back to FLUID_LLM_BACKEND=litellm + an API key.",
                ],
            ) from exc

        # CreateMessageResult.content is either a TextContent or a list.
        content = result.content
        if hasattr(content, "text"):
            return content.text
        if isinstance(content, list):
            return "".join(b.text for b in content if hasattr(b, "text"))
        return ""

    def invoke_streaming(
        self,
        config: "LlmConfig",
        system_prompt: str,
        user_prompt: str,
        *,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> "Iterator[str]":
        # Streaming via MCP sampling isn't in the spec yet — fall back
        # to blocking. The caller's loop sees one chunk == full text.
        yield self.invoke_blocking(config, system_prompt, user_prompt, extra_payload=extra_payload)


# ---------------------------------------------------------------------------
# Config Resolution
# ---------------------------------------------------------------------------


def _env_flag(env: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _tiered_mode_requested(args: Any, env: Mapping[str, str]) -> bool:
    return bool(getattr(args, "tiered", False)) or _env_flag(env, "FLUID_TIERED")


def _model_preflight_requested(args: Any, env: Mapping[str, str]) -> bool:
    return (
        bool(getattr(args, "require_llm", False))
        or _env_flag(env, "FLUID_LLM_MODEL_PREFLIGHT")
        or _tiered_mode_requested(args, env)
    )


def live_provider_models(
    provider_name: str,
    *,
    api_key: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[List[str]]:
    """Fetch live provider model ids when supported by the adapter."""
    env = dict(environ or os.environ)
    provider = get_llm_provider(provider_name)
    return provider.list_available_models(api_key or _resolve_api_key(provider.name, env), env)


def has_llm_api_key(
    provider_name: str,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Return whether the provider can resolve credentials without exposing them."""
    env = dict(environ or os.environ)
    provider = get_llm_provider(provider_name)
    if provider.name == "ollama":
        return detect_ollama_available(env)
    return bool(_resolve_api_key(provider.name, env))


# Providers that never require an LLM API key — the call is paid for
# elsewhere, so the api-key gate below must skip them. ``ollama`` runs a
# local server; ``mcp-sampling`` routes the call back through the MCP client
# (the IDE) via ``sampling/createMessage`` so the IDE's own LLM answers and
# forge never sees a key. Part B's coding-agent providers
# (claude-code/codex/cursor/kiro) are appended here too — they shell out to an
# installed agent CLI, and any per-agent key (CODEX_API_KEY, CURSOR_API_KEY,
# KIRO_API_KEY) is validated *inside* the provider at call time, not at this
# resolve-time gate. ``provider.name`` is always canonical (hyphen form); the
# underscore alias is included defensively.
_KEYLESS_PROVIDERS: frozenset = frozenset(
    {"ollama", "mcp-sampling", "mcp_sampling", "claude-code", "codex", "cursor", "kiro"}
)


def resolve_llm_config(args: Any, environ: Optional[Mapping[str, str]] = None) -> LlmConfig:
    """Resolve provider, model, endpoint, and API key from flags and env vars."""
    env = dict(environ or os.environ)
    provider_name = (
        getattr(args, "llm_provider", None)
        or env.get("FLUID_LLM_PROVIDER")
        or env.get("FLUID_FORGE_AGENT")
        or _infer_provider_from_env(env)
        or "gemini"
    )
    provider = get_llm_provider(provider_name)

    # Resolve model: explicit flag → env var → catalog default → class default.
    catalog_default = get_catalog_default(provider.name)
    explicit_model = getattr(args, "llm_model", None) or env.get("FLUID_LLM_MODEL")
    if explicit_model:
        model = resolve_model_name(provider.name, explicit_model)
        model_source = "explicit"
    elif provider.name == "ollama":
        model = resolve_ollama_model(env)
        model_source = "ollama"
    else:
        model = catalog_default or provider.default_model
        model_source = "catalog"

    if not model:
        raise CopilotGenerationError(
            "copilot_missing_llm_model",
            "No LLM model was configured for forge copilot.",
            suggestions=[
                "Set FLUID_LLM_MODEL before running fluid forge --mode copilot",
                "Or pass --llm-model on the command line",
            ],
        )

    api_key = _resolve_api_key(provider.name, env)
    if provider.name not in _KEYLESS_PROVIDERS and not api_key:
        raise CopilotGenerationError(
            "copilot_missing_llm_api_key",
            f"No API key was configured for the {provider.name} copilot adapter.",
            suggestions=[
                "Set FLUID_LLM_API_KEY or the provider-specific API key environment variable",
                "Examples: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY",
                "For local models, use --llm-provider ollama and optionally --llm-endpoint",
                "For keyless authoring: run forge from your IDE (mcp-sampling), or use a "
                "local coding agent (e.g. --llm-provider claude-code)",
            ],
        )

    model_notes: List[str] = []
    preflight_requested = _model_preflight_requested(args, env)
    available_models: Optional[List[str]] = None

    def _available_models() -> Optional[List[str]]:
        nonlocal available_models
        if available_models is not None:
            return available_models
        try:
            available_models = provider.list_available_models(api_key, env)
        except Exception as exc:  # noqa: BLE001
            raise CopilotGenerationError(
                "copilot_llm_model_preflight_failed",
                f"Could not preflight {provider.name} model availability: {exc}",
                suggestions=[
                    "Check the provider API key and network connectivity",
                    "For Ollama, start the local Ollama server and pull the requested model",
                    "Pass --llm-model with a known available model",
                    "Unset FLUID_LLM_MODEL_PREFLIGHT for non-strict local experimentation",
                ],
            ) from exc
        return available_models

    if preflight_requested:
        available = _available_models()
        if available is not None and not available:
            raise CopilotGenerationError(
                "copilot_llm_model_unavailable",
                f"No {provider.name} models are available for strict LLM mode.",
                suggestions=[
                    "Pass --llm-model with an available model",
                    "For Ollama, run `ollama list` and pull a local model first",
                ],
                context={"provider": provider.name, "model": model},
            )
        if available is not None and model not in available:
            if explicit_model:
                raise CopilotGenerationError(
                    "copilot_llm_model_unavailable",
                    f"Explicit {provider.name} model '{model}' is not available to this key.",
                    suggestions=[
                        f"Available models include: {', '.join(available[:5])}",
                        "Choose a model returned by the provider's live model list",
                    ],
                    context={"provider": provider.name, "model": model},
                )
            replacement = provider.choose_available_model(model, available)
            if replacement is None:
                raise CopilotGenerationError(
                    "copilot_llm_model_unavailable",
                    f"Configured {provider.name} model '{model}' is not available to this key.",
                    suggestions=[
                        f"Available models include: {', '.join(available[:5])}",
                        "Pass --llm-model with an available model",
                    ],
                    context={"provider": provider.name, "model": model},
                )
            model_notes.append(
                f"catalog model '{model}' was unavailable; using live model '{replacement}'"
            )
            LOG.warning(
                "llm_model_preflight_replaced: provider=%s catalog_model=%s live_model=%s",
                provider.name,
                model,
                replacement,
            )
            model = replacement
            model_source = "live_preflight"

    endpoint = getattr(args, "llm_endpoint", None) or env.get("FLUID_LLM_ENDPOINT")
    if not endpoint:
        endpoint = provider.default_endpoint(model, env)

    # Slice UX-J: resolve the optional routing model for cheap tasks
    # (interview clarification, classification, etc.).  Env vars take
    # precedence, then provider-specific defaults kick in.
    routing_model = getattr(args, "llm_routing_model", None) or env.get("FLUID_LLM_ROUTING_MODEL")
    routing_endpoint = getattr(args, "llm_routing_endpoint", None) or env.get(
        "FLUID_LLM_ROUTING_ENDPOINT"
    )
    explicit_routing_model = bool(routing_model)
    if not routing_model:
        routing_model = _default_routing_model(provider.name, model)

    tier_models = (
        get_catalog_tier_models(provider.name) if _tiered_mode_requested(args, env) else {}
    )
    if preflight_requested:
        available = _available_models()
        if available is not None:
            models_to_check: Dict[str, str] = {}
            if routing_model:
                models_to_check["routing"] = resolve_model_name(provider.name, routing_model)
            for tier_name, tier_model in tier_models.items():
                models_to_check[f"tier:{tier_name}"] = resolve_model_name(provider.name, tier_model)
            for role, candidate in sorted(models_to_check.items()):
                if not candidate or candidate == model:
                    continue
                if candidate not in available:
                    if role == "routing" and not explicit_routing_model:
                        model_notes.append(
                            f"catalog routing model '{candidate}' was unavailable; "
                            "using the primary model for routing"
                        )
                        routing_model = None
                        continue
                    raise CopilotGenerationError(
                        "copilot_llm_model_unavailable",
                        (
                            f"Resolved {provider.name} {role} model '{candidate}' is not "
                            "available to this key."
                        ),
                        suggestions=[
                            f"Available models include: {', '.join(available[:5])}",
                            "Run `fluid ai models` to inspect the configured model tiers",
                            "Pass --llm-model / --llm-routing-model or update ~/.fluid/llm_models.json",
                        ],
                        context={"provider": provider.name, "model": candidate, "role": role},
                    )

    agent_mode = (
        str(getattr(args, "forge_agent_mode", None) or env.get("FLUID_FORGE_AGENT_MODE") or "")
        .strip()
        .lower()
    )
    if agent_mode not in ("envelope", "agentic"):
        agent_mode = "envelope"
    return LlmConfig(
        provider=provider.name,
        model=model,
        endpoint=endpoint,
        api_key=api_key,
        timeout_seconds=_get_timeout_seconds(args, env),
        routing_model=routing_model,
        routing_endpoint=routing_endpoint,
        model_source=model_source,
        model_resolution_notes=model_notes,
        tier_models=tier_models,
        agent_mode=agent_mode,
    )


def _default_routing_model(provider_name: str, strong_model: str) -> Optional[str]:
    """Re-export shim for the extracted helper.

    Catalog logic now lives in :mod:`fluid_build.cli._llm_model_catalog`.
    Tests that ``patch("fluid_build.cli.forge_copilot_llm_providers._default_routing_model", ...)``
    still work because the patched name remains in this module's namespace.
    """
    from fluid_build.cli._llm_model_catalog import _default_routing_model as _impl

    return _impl(provider_name, strong_model)


# ---------------------------------------------------------------------------
# LLM Call with Retry
# ---------------------------------------------------------------------------

# Retry / transient-error handling lives in :class:`LiteLLMProvider`,
# which configures ``num_retries=2`` and lets litellm own the
# exponential backoff. No module-level retry constants here — every
# tunable goes through the provider config.

# Cumulative token usage across all LLM calls in this process.
# Not thread-safe by design — LLM calls are sequential in the current
# architecture.  If call_llm is ever invoked from threads, wrap updates
# with a threading.Lock.
_cumulative_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
_cumulative_prompt_cache: Dict[str, int] = {"read_tokens": 0, "total_tokens": 0}


def get_cumulative_token_usage() -> Dict[str, int]:
    """Return cumulative token usage across all LLM calls in this process."""
    return dict(_cumulative_usage)


def _coerce_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _prompt_cache_metrics(read_tokens: Any, total_tokens: Any) -> Dict[str, Any]:
    """Compose normalised prompt-cache metrics.

    ``total_tokens`` is the per-call total INPUT-side token count
    (cached + uncached), so ``hit_rate = read / total`` always means
    "fraction of input that was a cache hit." Each provider's
    ``extract_prompt_cache`` is responsible for reconciling its own
    accounting to this convention before calling here.
    """
    read = _coerce_nonnegative_int(read_tokens)
    total = _coerce_nonnegative_int(total_tokens)
    return {
        "read_tokens": read,
        "total_tokens": total,
        "hit_rate": (read / total) if total else 0.0,
    }


def get_cumulative_prompt_cache_metrics() -> Dict[str, Any]:
    """Return cumulative prompt-cache metrics across all LLM calls in this process."""
    return _prompt_cache_metrics(
        _cumulative_prompt_cache["read_tokens"],
        _cumulative_prompt_cache["total_tokens"],
    )


def _record_prompt_cache_usage(metrics: Mapping[str, Any]) -> None:
    read = _coerce_nonnegative_int(metrics.get("read_tokens"))
    total = _coerce_nonnegative_int(metrics.get("total_tokens"))
    # Streaming hooks call this for every event; provider extractors
    # return zeros for events that don't carry usage. Short-circuit so
    # we don't over-count cumulative metrics across a single stream.
    if not read and not total:
        return
    _cumulative_prompt_cache["read_tokens"] += read
    _cumulative_prompt_cache["total_tokens"] += total
    cumulative = get_cumulative_prompt_cache_metrics()
    LOG.info(
        "LLM prompt cache usage: read_tokens=%s total_tokens=%s hit_rate=%.4f "
        "cumulative_read_tokens=%s cumulative_total_tokens=%s cumulative_hit_rate=%.4f",
        read,
        total,
        (read / total) if total else 0.0,
        cumulative["read_tokens"],
        cumulative["total_tokens"],
        cumulative["hit_rate"],
    )


def _record_prompt_cache_from_response(
    provider: LlmProvider, response_json: Mapping[str, Any]
) -> None:
    try:
        _record_prompt_cache_usage(provider.extract_prompt_cache(dict(response_json)))
    except Exception as exc:  # noqa: BLE001 — metrics must never break generation
        LOG.debug("prompt_cache_metrics_extract_failed", extra={"error": str(exc)})


def reset_token_usage() -> None:
    """Reset both cumulative token counters and prompt-cache metrics.

    Called from ``forge.run`` at the top of each invocation and used
    extensively by tests; resets ``_cumulative_usage`` and
    ``_cumulative_prompt_cache`` together so callers don't have to
    track them separately.
    """
    _cumulative_usage.update({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    _cumulative_prompt_cache.update({"read_tokens": 0, "total_tokens": 0})


_cost_tracker_suppress = threading.local()


def suppress_call_llm_cost_recording() -> "_SuppressionToken":
    """Suppress ``call_llm`` / ``call_llm_streaming`` cost recording
    on the current thread for the duration of the returned context.

    The staged pipeline (``BaseStageAgent._call_once``) already records
    the per-call usage with the rich per-(stage, agent_class) attribution
    that the cost summary's "Per-agent attribution" panel surfaces. When
    that pipeline calls ``call_llm`` internally, the new H1 bridge would
    double-count unless suppressed.

    Callers use this as a context manager::

        with suppress_call_llm_cost_recording():
            raw = call_llm(provider, config, sys, usr)
            # ... record_call(...) here with full attribution

    Outside the context the bridge runs normally so the runtime's
    direct ``call_llm`` users (forge_copilot_runtime, the legacy
    interview path, judge_agent) continue to feed the tracker.
    """
    return _SuppressionToken()


class _SuppressionToken:
    """Thread-local on/off switch for the call_llm cost bridge."""

    def __enter__(self) -> "_SuppressionToken":
        depth = int(getattr(_cost_tracker_suppress, "depth", 0) or 0)
        _cost_tracker_suppress.depth = depth + 1
        return self

    def __exit__(self, *exc) -> None:
        depth = int(getattr(_cost_tracker_suppress, "depth", 0) or 0)
        _cost_tracker_suppress.depth = max(0, depth - 1)


def _record_call_in_run_tracker(
    provider: LlmProvider,
    config: LlmConfig,
    *,
    before: Mapping[str, int],
) -> None:
    """Snapshot the per-call usage delta and feed the RunCostTracker.

    Closes the H1 gap: ``call_llm`` updates ``_cumulative_usage`` (this
    module's module-level dict) but historically never invoked the
    ``RunCostTracker``. The tracker is what the preview panel reads to
    write ``cost.json``, what ``fluid stats`` aggregates, and what the
    cost ceiling (``FLUID_COST_LIMIT_USD_PER_RUN``) checks against. The
    runtime's main authoring loop in ``forge_copilot_runtime.py`` calls
    ``call_llm`` directly (not through ``BaseStageAgent._call_once``
    which is where the staged pipeline's record_call lives), so the
    tracker stayed empty and the user saw ``$0 / 0 tokens`` even when
    the LLM had spent thousands of tokens.

    Implementation: ``invoke_blocking`` increments ``_cumulative_usage``
    in-place; we read the dict before and after to compute the per-call
    delta. The per-call litellm USD and Anthropic cache-token counts
    sit on a thread-local in ``forge_copilot_llm_litellm`` so we pull
    them through the same bridge.

    Fail-safe: any exception is swallowed (logged at DEBUG). A failure
    here must never bubble up and break a user-facing run.

    Suppression: when the staged pipeline (``BaseStageAgent._call_once``)
    is on the call stack it wraps the ``call_llm`` invocation in
    :func:`suppress_call_llm_cost_recording` so the rich per-agent
    attribution it produces wins over the bridge's bare delta.
    """
    if int(getattr(_cost_tracker_suppress, "depth", 0) or 0) > 0:
        return
    try:
        delta_in = max(0, _cumulative_usage["input_tokens"] - int(before.get("input_tokens", 0)))
        delta_out = max(0, _cumulative_usage["output_tokens"] - int(before.get("output_tokens", 0)))
        # Pull the litellm-supplied USD + cache tokens off the thread
        # locals if available; non-litellm providers (MCP sampling) yield
        # None/zero which still drives a correct call into record_call.
        usd_override: Optional[float] = None
        cache_creation = 0
        cache_read = 0
        try:
            from fluid_build.cli.forge_copilot_llm_litellm import (
                get_last_cache_tokens,
                get_last_litellm_cost_usd,
            )

            usd_override = get_last_litellm_cost_usd()
            if usd_override is None:
                # Coding-agent providers (Claude Code) report cost on their own
                # thread-local; litellm never ran for them, so fall back to it.
                try:
                    from fluid_build.cli.forge_copilot_coding_agent import (
                        get_last_agent_cost_usd,
                    )

                    usd_override = get_last_agent_cost_usd()
                except Exception:  # pragma: no cover — defensive
                    pass
            ct = get_last_cache_tokens()
            cache_creation = int(ct.get("cache_creation_input_tokens", 0) or 0)
            cache_read = int(ct.get("cache_read_input_tokens", 0) or 0)
        except Exception:  # pragma: no cover — defensive
            pass

        from fluid_build.copilot.cost import record_call_from_cumulative_usage

        record_call_from_cumulative_usage(
            provider=str(getattr(provider, "name", "") or getattr(config, "provider", "")),
            model=str(getattr(config, "model", "")),
            input_tokens=delta_in,
            output_tokens=delta_out,
            usd_override=usd_override,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
        )
    except Exception:  # pragma: no cover — never let cost wiring break the run
        import logging as _logging

        _logging.getLogger(__name__).debug("record_call_in_run_tracker_failed", exc_info=True)


def call_llm(
    provider: LlmProvider,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Call the configured provider and return free-form response text.

    Every provider in fluid is a :class:`LiteLLMProvider` shim — the
    native per-provider httpx classes were deleted in favour of
    litellm's unified backend. We delegate straight to the provider's
    ``invoke_blocking`` method which owns retries, error translation,
    cost capture, and prompt-cache normalisation.

    ``extra_payload`` is the structured-output / JSON-schema
    response_format directive the agent layer injects. Plumbing it
    through here keeps the litellm path honest about the schema
    constraint the caller cares about.

    Cost wiring (H1 fix): we snapshot ``_cumulative_usage`` before the
    call and after, then feed the delta into the process-wide
    ``RunCostTracker`` via ``_record_call_in_run_tracker``. Without
    this bridge, the runtime's main authoring loop (which calls
    ``call_llm`` directly, not through ``BaseStageAgent._call_once``)
    leaves the tracker empty even when thousands of tokens flowed
    through it.
    """
    before = dict(_cumulative_usage)
    try:
        if extra_payload:
            # Forward only when the provider supports the kwarg — older
            # base-class subclasses ignore it; LiteLLMProvider honours it.
            try:
                text = provider.invoke_blocking(  # type: ignore[call-arg]
                    config, system_prompt, user_prompt, extra_payload=extra_payload
                )
            except TypeError:
                text = provider.invoke_blocking(config, system_prompt, user_prompt)
        else:
            text = provider.invoke_blocking(config, system_prompt, user_prompt)
        return text
    finally:
        _record_call_in_run_tracker(provider, config, before=before)


def call_llm_streaming(
    provider: LlmProvider,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    extra_payload: Optional[Dict[str, Any]] = None,
) -> Iterator[str]:
    """Stream text deltas from the configured provider.

    Yields text chunks as they arrive. Callers accumulate the chunks::

        chunks = []
        for chunk in call_llm_streaming(provider, config, sys, usr):
            chunks.append(chunk)
        raw_text = "".join(chunks)

    Concatenated output matches what :func:`call_llm` would have
    returned for the same request. After the iterator drains, the
    process-wide cumulative usage tracker reflects the streamed
    tokens (litellm's ``stream_options.include_usage=True`` carries
    final usage on the terminal chunk).

    ``extra_payload`` carries structured-output / JSON-schema
    response_format directives — same purpose as in :func:`call_llm`.

    Cost wiring (H1 fix): same as :func:`call_llm` — snapshot the
    cumulative usage dict before yielding, then feed the delta into
    the ``RunCostTracker`` once the iterator drains. The streaming
    provider's ``invoke_streaming`` is responsible for updating
    ``_cumulative_usage`` via ``_record_streaming_usage`` once it sees
    the closing-chunk usage block — both code paths land in the same
    place by the time the iterator is exhausted.
    """
    before = dict(_cumulative_usage)
    try:
        if extra_payload:
            try:
                yield from provider.invoke_streaming(  # type: ignore[call-arg]
                    config, system_prompt, user_prompt, extra_payload=extra_payload
                )
                return
            except TypeError:
                pass
        yield from provider.invoke_streaming(config, system_prompt, user_prompt)
    finally:
        # Streaming providers feed ``_cumulative_usage`` via the
        # ``_record_streaming_usage`` -> consume_streaming_usage path
        # OR directly inside ``invoke_streaming`` once the closing
        # chunk arrives. Either way, by the time the iterator drains
        # the delta against ``before`` reflects the call's tokens.
        # The streaming usage state also lives on a thread-local —
        # peek it WITHOUT popping so the existing
        # ``consume_streaming_usage()`` reader (BaseStageAgent) still
        # gets the same shape it has always seen.
        try:
            streamed = getattr(_streaming_usage_state, "usage", None)
            if streamed and (
                _cumulative_usage["input_tokens"] == int(before.get("input_tokens", 0))
                and _cumulative_usage["output_tokens"] == int(before.get("output_tokens", 0))
            ):
                # The streaming invoke_streaming kept the cache-side
                # bookkeeping but didn't increment _cumulative_usage
                # (older provider implementations). Backfill so the
                # delta-bridge picks up the tokens.
                _cumulative_usage["input_tokens"] += int(streamed.get("input_tokens", 0) or 0)
                _cumulative_usage["output_tokens"] += int(streamed.get("output_tokens", 0) or 0)
                _cumulative_usage["total_tokens"] += int(streamed.get("total_tokens", 0) or 0)
        except Exception:  # pragma: no cover — defensive
            pass
        _record_call_in_run_tracker(provider, config, before=before)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

# ``_sanitize_model_for_url`` was deleted alongside the per-provider
# wire-format classes — model names no longer interpolate into URL
# paths because litellm owns endpoint construction. The regex constant
# is gone too.


# Provider-inference + keyring helpers (~133 LOC) physically
# extracted to ``cli/_llm_provider_resolve.py``. Re-exported here
# under the same names so existing call sites and test patches keep
# resolving.
from fluid_build.cli._llm_provider_resolve import (  # noqa: E402,F401
    _LLM_KEYRING_PREFIX,
    _get_api_key_from_keyring,
    _infer_provider_from_ambient,
    _infer_provider_from_env,
    _infer_provider_from_explicit_keys,
    _infer_provider_from_keyring,
    _keyring_key,
    _resolve_api_key,
    clear_api_key_from_keyring,
    save_api_key_to_keyring,
)


def reset_llm_caches() -> None:
    """Clear per-process caches so detection and catalog are re-evaluated."""
    global _ollama_available_cache, _model_catalog_cache  # noqa: PLW0603
    _ollama_available_cache = None
    _model_catalog_cache = None


def _redact_endpoint_text(endpoint: Any) -> str:
    if not endpoint:
        return ""
    return LlmConfig(provider="", model="", endpoint=str(endpoint), api_key=None).redacted_endpoint


# ---------------------------------------------------------------------------
# API Key Detection
# ---------------------------------------------------------------------------

PROVIDER_DISPLAY_NAMES = {
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "ollama": "Ollama",
}


def detect_provider_from_api_key(api_key: str) -> Optional[str]:
    """Detect the LLM provider from an API key's format.

    Returns the provider name (``"openai"``, ``"anthropic"``, ``"gemini"``)
    or ``None`` if the format is not recognised.
    """
    key = (api_key or "").strip()
    if not key:
        return None
    # Anthropic keys start with sk-ant- — check before the generic sk- prefix.
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("sk-"):
        return "openai"
    if key.startswith("AIza") and 35 <= len(key) <= 45:
        return "gemini"
    return None


# ---------------------------------------------------------------------------
# API Key Detection & Readiness Helpers
# ---------------------------------------------------------------------------


@dataclass
class LlmReadinessCheck:
    """Result of an LLM readiness probe."""

    ready: bool
    provider: Optional[str] = None
    model: Optional[str] = None
    endpoint: Optional[str] = None
    auth_available: bool = False
    error: Optional[str] = None


def check_llm_readiness(environ: Optional[Mapping[str, str]] = None) -> LlmReadinessCheck:
    """Non-throwing check of whether an LLM provider is accessible.

    Resolution ladder (highest precedence first):

    1. ``FLUID_LLM_PROVIDER`` env var — explicit selector wins everything.
    2. Explicit API-key env vars (``OPENAI_API_KEY`` /
       ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``
       / ``OLLAMA_HOST``) when exactly one is set — deliberate
       per-run override.
    3. ``~/.fluid/ai_config.json`` — the user's persisted choice from
       ``fluid ai setup``.  This MUST beat ambient detection (e.g.
       Ollama running on ``localhost:11434``); operators report
       confusion when ``fluid doctor`` shows ``ollama`` despite
       having configured Gemini / Anthropic / OpenAI explicitly.
    4. Keyring — a saved provider key in the OS keyring.
    5. Ambient Ollama on localhost — last-resort discovery.

    Used by ``fluid doctor`` and forge.
    """
    env = dict(environ or os.environ)

    saved = None
    saved_model = None
    saved_key = None

    # Step 1 — explicit env-var selector.
    provider_name = env.get("FLUID_LLM_PROVIDER")

    # Step 2 — explicit API-key env vars (deliberate per-run override).
    if not provider_name:
        provider_name = _infer_provider_from_explicit_keys(env)

    # Step 3 — saved ``~/.fluid/ai_config.json`` choice.
    if not provider_name:
        try:
            from fluid_build.cli.ai_setup import _load_ai_config

            saved = _load_ai_config()
            if saved:
                provider_name = saved.get("provider")
                saved_model = saved.get("model")
                saved_key = saved.get("api_key")
                LOG.debug("LLM readiness: found provider=%s in config file", provider_name)
        except ImportError:
            pass

    # Step 4 — keyring fallback.
    if not provider_name:
        provider_name = _infer_provider_from_keyring()

    # Step 5 — last-resort ambient discovery (Ollama on localhost).
    if not provider_name:
        provider_name = _infer_provider_from_ambient(env)

    # Even when the provider was selected via env-var inference, we
    # still want the saved config's model + key for the matching
    # provider — that way ``GOOGLE_API_KEY=…`` plus a saved
    # ``ai_config.json`` of ``{provider: gemini, model: gemini-2.5-pro}``
    # honours the saved model.
    if provider_name and saved is None:
        try:
            from fluid_build.cli.ai_setup import _load_ai_config

            saved = _load_ai_config()
            if saved and saved.get("provider") == provider_name:
                saved_model = saved.get("model")
                saved_key = saved.get("api_key")
        except ImportError:
            pass

    if not provider_name:
        LOG.debug("LLM readiness: no provider detected from env or config")
        return LlmReadinessCheck(
            ready=False,
            error="No LLM provider configured. Run 'fluid ai setup' or set an API key env var.",
        )

    provider = BUILTIN_LLM_PROVIDERS.get(provider_name)
    if not provider:
        return LlmReadinessCheck(
            ready=False,
            provider=provider_name,
            error=f"Unknown LLM provider '{provider_name}'.",
        )

    # For ollama from config, ensure OLLAMA_HOST is set
    if provider.name == "ollama" and saved and saved.get("ollama_host"):
        if not env.get("OLLAMA_HOST"):
            env["OLLAMA_HOST"] = saved["ollama_host"]

    # Resolve API key: env vars → config file
    api_key = _resolve_api_key(provider.name, env) or saved_key
    auth_ok = bool(api_key) or provider.name == "ollama"

    if not auth_ok:
        LOG.debug("LLM readiness: provider=%s found but no API key", provider.name)
        return LlmReadinessCheck(
            ready=False,
            provider=provider.name,
            model=provider.default_model,
            auth_available=False,
            error=f"No API key found for {provider.name}. Run 'fluid ai setup'.",
        )

    model = env.get("FLUID_LLM_MODEL") or saved_model or provider.default_model
    endpoint = env.get("FLUID_LLM_ENDPOINT") or provider.default_endpoint(model, env)

    LOG.debug("LLM readiness: provider=%s model=%s ready=True", provider.name, model)
    return LlmReadinessCheck(
        ready=True,
        provider=provider.name,
        model=model,
        endpoint=endpoint,
        auth_available=True,
    )


# ---------------------------------------------------------------------------
# Model Catalog
# ---------------------------------------------------------------------------

_model_catalog_cache: Optional[Dict[str, Any]] = None


# Model-catalog query helpers — physically extracted to
# ``cli/_llm_model_catalog.py``. ~220 LOC of pure JSON-walk lifted
# without behavior change. Re-exported here under the same names so
# test patches on
# ``fluid_build.cli.forge_copilot_llm_providers.<helper>`` flow
# through to the moved functions via the module-attribute-access
# indirection pattern.
from fluid_build.cli._llm_model_catalog import (  # noqa: E402,F401
    _load_model_catalog,
    _model_has_capability,
    get_catalog_default,
    get_catalog_routing_model,
    get_catalog_tier_model,
    get_catalog_tier_models,
    has_distinct_tier_models,
    model_supports_structured_output,
    model_supports_tool_use,
    resolve_model_name,
)


def build_llm_run_plan(config: "LlmConfig", *, tiered: bool = False) -> Dict[str, Any]:
    """Build the user-facing plan for an AI forge run.

    Stays in the host module because it consumes :class:`LlmConfig`
    (defined here) — extracting it would require a forward reference
    or a circular import. The catalog query is delegated to
    :func:`get_catalog_tier_models` (now in ``_llm_model_catalog``).
    """
    tier_models = config.tier_models or (get_catalog_tier_models(config.provider) if tiered else {})
    logical_model = tier_models.get("deep") if tiered else None
    logical_model = logical_model or config.model
    routing_model = config.routing_model or config.model
    return {
        "provider": config.provider,
        "primary_model": config.model,
        "routing_model": routing_model,
        "tiered": bool(tiered),
        "tier_models": dict(tier_models),
        "policy": (
            "LLM stages handle semantic/modeling judgement; contract forging, "
            "dbt SQL generation, and validation remain deterministic from the "
            "logical sidecar."
        ),
        "stages": [
            {
                "stage": "interview",
                "mode": "llm_routing",
                "tier": "fast",
                "model": routing_model,
            },
            {
                "stage": "logical_modeler",
                "mode": "strict_llm_or_llm",
                "tier": "deep" if tiered else "primary",
                "model": logical_model,
            },
            {
                "stage": "contract_forge",
                "mode": "deterministic",
                "tier": None,
                "model": None,
            },
            {
                "stage": "transformation",
                "mode": "deterministic_from_sidecar",
                "tier": None,
                "model": None,
            },
            {
                "stage": "validator",
                "mode": "deterministic",
                "tier": None,
                "model": None,
            },
            {
                "stage": "self_eval",
                "mode": "llm_routing",
                "tier": "fast",
                "model": routing_model,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Ollama Auto-Detection
# ---------------------------------------------------------------------------


_LOCALHOST_PREFIXES = (
    "http://localhost",
    "http://127.0.0.1",
    "http://[::1]",
    "http://0.0.0.0",
)

_ollama_available_cache: Optional[bool] = None


def _ollama_host(env: Mapping[str, str]) -> str:
    host = (env.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    # SSRF guard: only allow localhost targets for Ollama.
    if not any(host.lower().startswith(prefix) for prefix in _LOCALHOST_PREFIXES):
        LOG.warning("OLLAMA_HOST points to a non-localhost address (%s), ignoring.", host)
        return "http://localhost:11434"
    return host


def detect_ollama_available(env: Mapping[str, str]) -> bool:
    """Return ``True`` if a local Ollama instance is reachable (cached per-process)."""
    global _ollama_available_cache  # noqa: PLW0603
    if _ollama_available_cache is not None:
        return _ollama_available_cache
    try:
        resp = httpx.get(f"{_ollama_host(env)}/api/version", timeout=1.0)
        _ollama_available_cache = resp.status_code == 200
    except Exception:  # noqa: BLE001
        _ollama_available_cache = False
    return _ollama_available_cache


def _parse_param_size(value: str) -> float:
    """Parse an Ollama ``parameter_size`` string like ``'32.8B'`` into a float."""
    text = (value or "").strip().upper()
    match = re.match(r"^([\d.]+)\s*([BM]?)$", text)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2)
    if unit == "M":
        return number / 1000.0
    return number


def query_ollama_models(env: Mapping[str, str]) -> List[Dict[str, Any]]:
    """Query the local Ollama instance for downloaded models.

    Returns a list of model dicts sorted by parameter size (largest first),
    or an empty list if Ollama is unreachable.
    """
    try:
        resp = httpx.get(f"{_ollama_host(env)}/api/tags", timeout=2.0)
        resp.raise_for_status()
        models = resp.json().get("models") or []
        for m in models:
            size_str = (m.get("details") or {}).get("parameter_size", "")
            m["_param_size"] = _parse_param_size(size_str)
        models.sort(key=lambda m: m["_param_size"], reverse=True)
        return models
    except Exception:  # noqa: BLE001
        return []


def resolve_ollama_model(env: Mapping[str, str]) -> str:
    """Return the best locally-available Ollama model, or the static fallback."""
    models = query_ollama_models(env)
    if models:
        return models[0]["name"]
    return get_catalog_default("ollama") or OllamaProvider.default_model


# ---------------------------------------------------------------------------
# Module init: sync provider defaults from catalog (must run AFTER all
# functions and the model catalog section are defined above).
# (Moved below ``BUILTIN_LLM_PROVIDERS`` so the sync sees the actual
# instance attrs.)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Provider shims (litellm-backed)
# ---------------------------------------------------------------------------
#
# What used to be ~1,300 lines of per-provider wire-format code is now
# four thin subclasses that delegate every method to LiteLLMProvider.
# litellm normalises every supported providers payload + response shape
# to the OpenAI shape so the per-provider `build_request` / `extract_text`
# / `extract_usage` / `extract_prompt_cache` / `extract_tool_calls` /
# `iter_stream_chunks` overrides are all gone.
#
# These shim classes exist so legacy import paths
# (`OpenAIProvider`, `AnthropicProvider`, ...) keep resolving — the
# code that *used* them already calls the abstract `LlmProvider` API,
# so swapping in a litellm-backed subclass is invisible at the call
# site.
#
# Adding a new provider becomes one row in `BUILTIN_LLM_PROVIDERS`
# below: litellm already speaks every major provider so the per-class
# wire format wrapping is no longer required.


def _make_litellm_shim(provider_name: str, default_model: str):
    """Build a LiteLLMProvider-backed subclass with a fixed provider name.

    Returns a class so legacy code that does ``OpenAIProvider()``
    keeps working unchanged. We avoid a top-level
    ``from forge_copilot_llm_litellm import LiteLLMProvider`` because
    that module imports from this one — the circular import would
    deadlock at module load time when callers do
    ``from forge_copilot_llm_litellm import LiteLLMProvider`` first.

    Instead we resolve ``LiteLLMProvider`` lazily on first
    *instantiation* of the shim class. By that point both modules
    are fully loaded.
    """
    _shim_class: list = [None]  # closed-over cache

    def _resolve_shim_class():
        if _shim_class[0] is not None:
            return _shim_class[0]
        from fluid_build.cli.forge_copilot_llm_litellm import LiteLLMProvider

        class _Shim(LiteLLMProvider):
            def __init__(self):
                super().__init__(provider_name, default_model=default_model)

        _Shim.__name__ = provider_name.title() + "Provider"
        _Shim.__qualname__ = _Shim.__name__
        _shim_class[0] = _Shim
        return _Shim

    class _LazyShim:
        """Public stand-in: instantiating it builds the real class
        on first call and forwards through. ``isinstance`` checks
        against ``LiteLLMProvider`` post-construction will work
        because the constructed object IS a ``LiteLLMProvider``
        subclass."""

        def __new__(cls, *args, **kwargs):
            real_cls = _resolve_shim_class()
            return real_cls(*args, **kwargs)

    _LazyShim.__name__ = provider_name.title() + "Provider"
    _LazyShim.__qualname__ = _LazyShim.__name__
    return _LazyShim


# Keep the historical class names available for any caller that
# imported them. New code should not reference these directly — use
# `get_llm_provider(name)` instead. Defaults pulled from the central
# model registry so a single edit to ``copilot/models.py`` propagates
# here.
from fluid_build.copilot.models import default_model_for as _default_model

OpenAIProvider = _make_litellm_shim(
    "openai", _default_model("openai", "default", fallback="gpt-4.1-mini")
)
AnthropicProvider = _make_litellm_shim(
    "anthropic",
    _default_model("anthropic", "fast", fallback="claude-haiku-4-5"),
)
GeminiProvider = _make_litellm_shim(
    "gemini", _default_model("gemini", "default", fallback="gemini-2.5-flash")
)


class OllamaProvider(
    _make_litellm_shim("ollama", _default_model("ollama", "default", fallback="gemma3:4b"))
):
    """Local Ollama — same shim shape, kept as its own class so call
    sites that special-case Ollama (zero cost, host detection, etc.)
    still find a stable subclass."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _coding_agent_provider(name: str) -> "LlmProvider":
    """Lazy factory bridge for the coding-agent registry rows.

    Imported lazily so the registry doesn't pull in
    ``forge_copilot_coding_agent`` (which imports back from this module) at
    module-load time.
    """
    from fluid_build.cli.forge_copilot_coding_agent import get_coding_agent_provider

    return get_coding_agent_provider(name)


class _LazyBuiltinProviders(dict):
    """Lazy registry for built-in LLM providers.

    Instantiating ``OpenAIProvider`` etc. requires
    :class:`LiteLLMProvider` from ``forge_copilot_llm_litellm``, which
    in turn imports from this module. Eager instantiation at module
    load time deadlocks the circular import. We defer per-key
    construction to the first ``__getitem__`` / ``get`` call.

    The dict-subclass shape preserves the public contract:
    ``BUILTIN_LLM_PROVIDERS["openai"]`` works,
    ``"openai" in BUILTIN_LLM_PROVIDERS`` works, ``.values()`` returns
    instantiated providers (it forces resolution of every key).
    """

    _factories: Dict[str, Any] = {
        "openai": lambda: OpenAIProvider(),
        "anthropic": lambda: AnthropicProvider(),
        "claude": lambda: AnthropicProvider(),
        "gemini": lambda: GeminiProvider(),
        "ollama": lambda: OllamaProvider(),
        # Coding-agent providers — shell out to a local agent CLI (Part B).
        "claude-code": lambda: _coding_agent_provider("claude-code"),
        "codex": lambda: _coding_agent_provider("codex"),
        "cursor": lambda: _coding_agent_provider("cursor"),
        "kiro": lambda: _coding_agent_provider("kiro"),
    }

    def __init__(self):
        super().__init__()

    def __contains__(self, key) -> bool:  # type: ignore[override]
        return key in self._factories or super().__contains__(key)

    def __getitem__(self, key):
        if not super().__contains__(key):
            factory = self._factories.get(key)
            if factory is None:
                raise KeyError(key)
            super().__setitem__(key, factory())
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):  # type: ignore[override]
        # Union of cached + lazy keys.
        return list(set(self._factories.keys()) | set(super().keys()))

    def values(self):  # type: ignore[override]
        return [self[k] for k in self.keys()]

    def items(self):  # type: ignore[override]
        return [(k, self[k]) for k in self.keys()]


BUILTIN_LLM_PROVIDERS: Dict[str, LlmProvider] = _LazyBuiltinProviders()


# Sync defaults from the catalog now that BUILTIN_LLM_PROVIDERS exists.
# The shim ``__init__`` writes its hardcoded fallback ``default_model``;
# this runs immediately after to override it with the catalog flagship,
# keeping the catalog file as the single source of truth.
_sync_provider_defaults_from_catalog()
