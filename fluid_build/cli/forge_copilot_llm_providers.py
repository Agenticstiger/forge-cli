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
    "detect_provider_from_api_key",
    "check_llm_readiness",
    "live_provider_models",
]

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

import httpx

from fluid_build.cli._common import CLIError
from fluid_build.cli.forge_copilot_response_schema import (
    anthropic_tool_definition,
    ollama_supports_structured_output,
    openai_response_format,
)

LOG = logging.getLogger("fluid.cli.forge_copilot.llm")


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

# Constant seed for OpenAI-compatible providers.  Combined with
# temperature 0 this makes sampling fully deterministic for a given
# prompt (modulo model-version changes on the provider side).
_OPENAI_SEED: int = 42


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


class OpenAIProvider(LlmProvider):
    name = "openai"
    default_model = "gpt-4o-mini"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        base = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        if base != "https://api.openai.com/v1":
            LOG.warning(
                "OPENAI_BASE_URL is set to a non-default value (%s). "
                "Your API key will be sent to this host.",
                base,
            )
        return base + "/chat/completions"

    def list_available_models(
        self, api_key: Optional[str], env: Mapping[str, str]
    ) -> Optional[List[str]]:
        if not api_key:
            return None
        base = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        response = httpx.get(
            f"{base}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data") or []
        return [str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")]

    def build_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        payload: Dict[str, Any] = {
            "model": config.model,
            "temperature": _get_temperature(),
            "seed": _OPENAI_SEED,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        # Slice UX-I: provider-native structured outputs.  For
        # gpt-4o-mini / gpt-4o-2024-08-06+ this is strict
        # ``json_schema`` mode which literally cannot return
        # anything else; for older models it falls back to the
        # weaker ``json_object`` mode.  Either path eliminates the
        # "LLM returned markdown fences" repair retries.
        if _structured_outputs_enabled():
            payload["response_format"] = openai_response_format(config.model)
        return headers, payload

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        return response_json["choices"][0]["message"]["content"]

    def extract_usage(self, response_json: Dict[str, Any]) -> Dict[str, int]:
        usage = response_json.get("usage") or {}
        inp = usage.get("prompt_tokens", 0)
        out = usage.get("completion_tokens", 0)
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}

    def iter_stream_chunks(self, response: httpx.Response) -> Iterator[str]:
        """Parse an OpenAI Chat Completions SSE stream.

        Each SSE event is of the form::

            data: {"choices":[{"delta":{"content":"..."}}], ...}
            data: [DONE]

        The concatenation of every yielded chunk equals the
        ``choices[0].message.content`` value that
        :meth:`extract_text` returns on the blocking path.
        """
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", "replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    return
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if content:
                yield content

    # -- Tool use (slice UX-K) ------------------------------------------

    def build_tool_request(
        self,
        config: LlmConfig,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in tools
        ]
        payload: Dict[str, Any] = {
            "model": config.model,
            "temperature": _get_temperature(),
            "seed": _OPENAI_SEED,
            "messages": [
                {"role": "system", "content": system_prompt},
                *messages,
            ],
            "tools": openai_tools,
        }
        return config.endpoint, headers, payload

    def extract_tool_calls(self, response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        choices = response_json.get("choices") or []
        if not choices:
            return []
        msg = choices[0].get("message") or {}
        raw_calls = msg.get("tool_calls") or []
        result = []
        for tc in raw_calls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            result.append(
                {
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": args,
                }
            )
        return result

    def build_tool_result_messages(
        self, tool_calls: List[Dict[str, Any]], results: List[Any]
    ) -> List[Dict[str, Any]]:
        # OpenAI expects: assistant message with tool_calls, then one
        # tool-role message per call result.
        assistant_msg: Dict[str, Any] = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"]),
                    },
                }
                for tc in tool_calls
            ],
        }
        tool_msgs = [
            {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, default=str),
            }
            for tc, result in zip(tool_calls, results, strict=True)
        ]
        return [assistant_msg] + tool_msgs


class OllamaProvider(OpenAIProvider):
    name = "ollama"
    default_model = "llama3.1"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        return host + "/v1/chat/completions"

    def list_available_models(
        self, api_key: Optional[str], env: Mapping[str, str]
    ) -> Optional[List[str]]:
        response = httpx.get(f"{_ollama_host(env)}/api/tags", timeout=2.0)
        response.raise_for_status()
        result: List[str] = []
        for item in response.json().get("models") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            result.append(name)
            if name.endswith(":latest"):
                result.append(name.removesuffix(":latest"))
        return result

    def build_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        headers, payload = super().build_request(config, system_prompt, user_prompt)
        headers.pop("Authorization", None)
        # Slice UX-I: Ollama's chat-compat endpoint does not accept
        # OpenAI's ``response_format: json_schema`` directive.  For
        # known-good local models fall back to the OpenAI-compat
        # ``{"type": "json_object"}`` (supported by recent Ollama
        # builds via ``format: "json"``); for unknown models drop
        # the directive entirely and rely on the in-prompt JSON
        # nudge.
        if _structured_outputs_enabled():
            payload.pop("response_format", None)
            if ollama_supports_structured_output(config.model):
                payload["response_format"] = {"type": "json_object"}
        else:
            payload.pop("response_format", None)
        return headers, payload


class AnthropicProvider(LlmProvider):
    name = "anthropic"
    default_model = "claude-sonnet-4-6"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        return "https://api.anthropic.com/v1/messages"

    def list_available_models(
        self, api_key: Optional[str], env: Mapping[str, str]
    ) -> Optional[List[str]]:
        if not api_key:
            return None
        response = httpx.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data") or []
        return [str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")]

    def choose_available_model(
        self, requested_model: str, available_models: List[str]
    ) -> Optional[str]:
        if requested_model in available_models:
            return requested_model
        requested = requested_model.lower()
        for family in ("sonnet", "opus", "haiku"):
            if family not in requested:
                continue
            for model in available_models:
                if family in model.lower():
                    return model
        return available_models[0] if available_models else None

    def build_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if config.api_key:
            headers["x-api-key"] = config.api_key
        # Slice UX-I: the ``system`` field is a blocks array with a
        # ``cache_control: ephemeral`` hint.  Anthropic marks this
        # prefix as a 5-minute cache candidate; subsequent requests
        # with the byte-identical prefix read cached tokens at ~10%
        # of the normal input cost and ~50-80% faster TTFT.
        # ``build_system_prompt`` is memoized upstream so the prefix
        # is stable across retries and non-interactive reruns.
        # Pin sampling temperature explicitly for the Anthropic API.
        # Without this, the request would inherit the provider-side
        # default (typically 1.0 on Claude), which makes structured
        # contract generation non-deterministic — a regression
        # against the determinism-ladder contract we advertise via
        # ``--deterministic`` and ``FLUID_LLM_TEMPERATURE``. The
        # Anthropic API does not yet expose a public ``seed``
        # parameter, so OpenAI-style seeding is omitted; the
        # ``--deterministic`` flag's audit-trail metadata records
        # this provider gap so users know byte-stability is
        # OpenAI-only today.
        #
        # NOTE — Newer Claude models (opus-4-7+, the o-series-style
        # reasoning models) DEPRECATED the ``temperature`` parameter.
        # Sending it returns ``400 invalid_request_error: 'temperature'
        # is deprecated for this model.``  We detect the deprecated
        # set and skip the field for those models. The catalog tier
        # ``deep`` points at ``claude-opus-4-7`` so this gate is
        # exactly what makes ``--tiered`` work end-to-end.
        payload: Dict[str, Any] = {
            "model": config.model,
            "max_tokens": 8192,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if not _model_deprecates_temperature(config.model):
            payload["temperature"] = _get_temperature()
        # Slice UX-I: force the model to emit its response via a
        # single ``emit_forge_contract`` tool call.  The tool's
        # ``input_schema`` is the response envelope JSON Schema, so
        # the model cannot return anything outside the shape the
        # validator expects.  ``extract_text`` below will unwrap the
        # tool_use block and return the JSON-encoded ``input``.
        if _structured_outputs_enabled():
            payload["tools"] = [anthropic_tool_definition()]
            payload["tool_choice"] = {
                "type": "tool",
                "name": "emit_forge_contract",
            }
        return headers, payload

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        content = response_json.get("content") or []
        # Slice UX-I: support both legacy ``text`` blocks and
        # ``tool_use`` blocks emitted by structured-output forced
        # tool calls (``emit_forge_contract``).  When a tool_use
        # block is present, return its JSON-encoded input so the
        # downstream ``extract_json_object`` parser can consume it
        # without any natural-language unwrapping.
        for part in content:
            if part.get("type") == "tool_use":
                tool_input = part.get("input") or {}
                return json.dumps(tool_input)
        for part in content:
            if part.get("type") == "text":
                return part.get("text", "")
        raise KeyError("Anthropic response did not contain a text or tool_use block")

    def extract_usage(self, response_json: Dict[str, Any]) -> Dict[str, int]:
        usage = response_json.get("usage") or {}
        inp = usage.get("input_tokens", 0)
        out = usage.get("output_tokens", 0)
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}

    def iter_stream_chunks(self, response: httpx.Response) -> Iterator[str]:
        """Parse an Anthropic Messages API SSE stream.

        Anthropic ships events in this shape::

            event: content_block_start
            data: {"type":"content_block_start","content_block":{"type":"text","text":""}}

            event: content_block_delta
            data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}

        When the generation path is using the forced
        ``emit_forge_contract`` tool (slice UX-I structured outputs),
        the deltas carry ``input_json_delta`` with ``partial_json``
        fragments that concatenate into the tool's JSON input.  This
        parser handles both — the concatenation of every yielded
        chunk matches what :meth:`extract_text` returns on the
        blocking path (a JSON string for the tool_use path, plain
        text for the legacy text path).
        """
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", "replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            event_type = event.get("type")
            if event_type == "content_block_delta":
                delta = event.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text")
                    if text:
                        yield text
                elif delta_type == "input_json_delta":
                    partial = delta.get("partial_json")
                    if partial:
                        yield partial
            elif event_type == "message_stop":
                return

    # -- Tool use (slice UX-K) ------------------------------------------

    def build_tool_request(
        self,
        config: LlmConfig,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if config.api_key:
            headers["x-api-key"] = config.api_key
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]
        payload: Dict[str, Any] = {
            "model": config.model,
            "max_tokens": 8192,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": list(messages),
            "tools": anthropic_tools,
        }
        return config.endpoint, headers, payload

    def extract_tool_calls(self, response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        content = response_json.get("content") or []
        result = []
        for block in content:
            if block.get("type") == "tool_use":
                result.append(
                    {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input") or {},
                    }
                )
        return result

    def build_tool_result_messages(
        self, tool_calls: List[Dict[str, Any]], results: List[Any]
    ) -> List[Dict[str, Any]]:
        # Anthropic expects: assistant message with the raw content
        # blocks (text + tool_use), then a user message with
        # tool_result blocks.
        # We reconstruct a simplified assistant message from the calls.
        assistant_content = [
            {
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["arguments"],
            }
            for tc in tool_calls
        ]
        user_content = [
            {
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": json.dumps(result, default=str),
            }
            for tc, result in zip(tool_calls, results, strict=True)
        ]
        return [
            {"role": "assistant", "content": assistant_content},
            {"role": "user", "content": user_content},
        ]


class GeminiProvider(LlmProvider):
    name = "gemini"
    default_model = "gemini-2.5-flash"

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        safe_model = _sanitize_model_for_url(model)
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{safe_model}:generateContent"
        )

    def list_available_models(
        self, api_key: Optional[str], env: Mapping[str, str]
    ) -> Optional[List[str]]:
        if not api_key:
            return None
        response = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result: List[str] = []
        for item in payload.get("models") or []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            name = str(item.get("name") or "")
            if name.startswith("models/"):
                name = name.removeprefix("models/")
            if name:
                result.append(name)
        return result

    def build_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[Dict[str, str], Dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["x-goog-api-key"] = config.api_key
        generation_config: Dict[str, Any] = {"temperature": _get_temperature()}
        # Slice UX-I note: Gemini's responseSchema doesn't handle
        # deeply nested free-form objects (like the ``contract``
        # field which is itself a full FLUID contract with 10+
        # nested levels).  Gemini strips ``additionalProperties``
        # during schema processing, which means nested free-form
        # objects become ``{"type": "object", "properties": {}}``
        # — interpreted as "empty object, no fields allowed".
        # The natural-language JSON nudge in the system prompt is
        # sufficient for Gemini; structured outputs are left to
        # OpenAI (json_schema) and Anthropic (tool_use) where the
        # nested free-form handling works correctly.
        #
        # If Gemini improves nested schema support in the future,
        # uncomment the block below:
        # if _structured_outputs_enabled():
        #     generation_config.update(gemini_response_schema_config())
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }
        return headers, payload

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        candidates = response_json.get("candidates") or []
        for candidate in candidates:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                text = part.get("text")
                if text:
                    return text
        raise KeyError("Gemini response did not contain any text")

    def extract_usage(self, response_json: Dict[str, Any]) -> Dict[str, int]:
        usage = response_json.get("usageMetadata") or {}
        inp = usage.get("promptTokenCount", 0)
        out = usage.get("candidatesTokenCount", 0)
        total = usage.get("totalTokenCount", inp + out)
        return {"input_tokens": inp, "output_tokens": out, "total_tokens": total}

    # -- Streaming (slice UX-I) --------------------------------------------

    def _streaming_url(self, config: LlmConfig) -> str:
        """Rewrite the Gemini generate-content URL for SSE streaming.

        Gemini exposes streaming via ``:streamGenerateContent?alt=sse``
        rather than ``:generateContent``.  We patch the URL in place
        so ``build_streaming_request`` can reuse the existing blocking
        payload without caring about endpoint resolution.
        """
        endpoint = config.endpoint or ""
        if ":generateContent" in endpoint:
            endpoint = endpoint.replace(":generateContent", ":streamGenerateContent")
        if "alt=sse" not in endpoint:
            sep = "&" if "?" in endpoint else "?"
            endpoint = f"{endpoint}{sep}alt=sse"
        return endpoint

    def build_streaming_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        headers, payload = self.build_request(config, system_prompt, user_prompt)
        return self._streaming_url(config), headers, payload

    def iter_stream_chunks(self, response: httpx.Response) -> Iterator[str]:
        """Parse a Gemini ``streamGenerateContent?alt=sse`` response.

        Each SSE event carries a partial Gemini response with one or
        more text parts under ``candidates[0].content.parts``.  We
        yield every non-empty text fragment in order; the
        concatenation matches :meth:`extract_text` on the blocking
        path.
        """
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", "replace")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            for candidate in event.get("candidates") or []:
                content = candidate.get("content") or {}
                for part in content.get("parts") or []:
                    text = part.get("text")
                    if text:
                        yield text

    # -- Tool use (slice UX-K) ------------------------------------------

    def build_tool_request(
        self,
        config: LlmConfig,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> tuple[str, Dict[str, str], Dict[str, Any]]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            headers["x-goog-api-key"] = config.api_key
        from fluid_build.cli.forge_copilot_response_schema import _strip_for_gemini

        gemini_tools = [
            {
                "functionDeclarations": [
                    {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": _strip_for_gemini(
                            t.get("input_schema", {"type": "object", "properties": {}})
                        ),
                    }
                    for t in tools
                ]
            }
        ]
        # Convert chat messages to Gemini's contents format
        contents = []
        for msg in messages:
            role = "user" if msg.get("role") == "user" else "model"
            content = msg.get("content", "")
            if isinstance(content, str):
                contents.append({"role": role, "parts": [{"text": content}]})
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append({"text": block.get("text", "")})
                        elif "functionCall" in block:
                            parts.append(block)
                        elif "functionResponse" in block:
                            parts.append(block)
                if parts:
                    contents.append({"role": role, "parts": parts})
        payload: Dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "tools": gemini_tools,
            "generationConfig": {"temperature": _get_temperature()},
        }
        return config.endpoint, headers, payload

    def extract_tool_calls(self, response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates = response_json.get("candidates") or []
        result = []
        for candidate in candidates:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                fc = part.get("functionCall")
                if fc:
                    result.append(
                        {
                            "id": fc.get("name", ""),
                            "name": fc.get("name", ""),
                            "arguments": fc.get("args") or {},
                        }
                    )
        return result

    def build_tool_result_messages(
        self, tool_calls: List[Dict[str, Any]], results: List[Any]
    ) -> List[Dict[str, Any]]:
        # Gemini: model turn with functionCall parts, then user turn
        # with functionResponse parts
        model_parts = [
            {"functionCall": {"name": tc["name"], "args": tc["arguments"]}} for tc in tool_calls
        ]
        user_parts = [
            {
                "functionResponse": {
                    "name": tc["name"],
                    "response": {"result": result},
                }
            }
            for tc, result in zip(tool_calls, results, strict=True)
        ]
        return [
            {"role": "model", "content": model_parts},
            {"role": "user", "content": user_parts},
        ]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

BUILTIN_LLM_PROVIDERS: Dict[str, LlmProvider] = {
    "openai": OpenAIProvider(),
    "anthropic": AnthropicProvider(),
    "claude": AnthropicProvider(),
    "gemini": GeminiProvider(),
    "ollama": OllamaProvider(),
}


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
    """Resolve a provider adapter by name."""
    normalized = (name or "").strip().lower()
    provider = BUILTIN_LLM_PROVIDERS.get(normalized)
    if not provider:
        raise CopilotGenerationError(
            "copilot_invalid_llm_provider",
            f"Unsupported LLM provider '{name}'.",
            suggestions=[
                "Choose one of: openai, anthropic, gemini, ollama",
                "Use --llm-provider or FLUID_LLM_PROVIDER to select a provider",
            ],
        )
    return provider


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


def resolve_llm_config(args: Any, environ: Optional[Mapping[str, str]] = None) -> LlmConfig:
    """Resolve provider, model, endpoint, and API key from flags and env vars."""
    env = dict(environ or os.environ)
    provider_name = (
        getattr(args, "llm_provider", None)
        or env.get("FLUID_LLM_PROVIDER")
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
    if provider.name != "ollama" and not api_key:
        raise CopilotGenerationError(
            "copilot_missing_llm_api_key",
            f"No API key was configured for the {provider.name} copilot adapter.",
            suggestions=[
                "Set FLUID_LLM_API_KEY or the provider-specific API key environment variable",
                "Examples: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY",
                "For local models, use --llm-provider ollama and optionally --llm-endpoint",
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
    )


def _default_routing_model(provider_name: str, strong_model: str) -> Optional[str]:
    """Return the catalog's routing model for *provider_name*.

    Reads the ``routing`` field from the catalog (v2 schema) instead
    of a hardcoded mapping.  Returns ``None`` when no cheaper
    alternative is available or when the routing model would be the
    same as the strong model (no point routing to self).
    """
    catalog = _load_model_catalog()
    entry = catalog.get("providers", {}).get(provider_name, {})
    routing = entry.get("routing")
    if routing and routing != strong_model:
        return routing
    return None


# ---------------------------------------------------------------------------
# LLM Call with Retry
# ---------------------------------------------------------------------------

_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
_LLM_MAX_RETRIES = 2
_LLM_RETRY_BASE_SECONDS = 2.0

# Cumulative token usage across all LLM calls in this process.
# Not thread-safe by design — LLM calls are sequential in the current
# architecture.  If call_llm is ever invoked from threads, wrap updates
# with a threading.Lock.
_cumulative_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def get_cumulative_token_usage() -> Dict[str, int]:
    """Return cumulative token usage across all LLM calls in this process."""
    return dict(_cumulative_usage)


def reset_token_usage() -> None:
    """Reset cumulative token counters (useful for testing)."""
    _cumulative_usage.update({"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})


def call_llm(
    provider: LlmProvider,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Call the configured provider and return free-form response text."""
    headers, payload = provider.build_request(config, system_prompt, user_prompt)

    _LLM_REQUEST_SUGGESTIONS = [
        "Check the selected model and endpoint are correct",
        "Verify the API key environment variable is set",
        "Use --llm-endpoint only when you need to override the provider default",
    ]

    for attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=config.timeout_seconds) as client:
                response = client.post(config.endpoint, headers=headers, json=payload)
                response.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _TRANSIENT_STATUS_CODES and attempt < _LLM_MAX_RETRIES:
                delay = _LLM_RETRY_BASE_SECONDS * (2**attempt)
                LOG.info(
                    "LLM request returned %s, retrying in %.1fs", exc.response.status_code, delay
                )
                time.sleep(delay)
                continue
            status = exc.response.status_code
            suggestions = list(_LLM_REQUEST_SUGGESTIONS)
            if status == 404 and config.provider == "ollama":
                suggestions.insert(
                    0,
                    f"Model '{config.model}' may not be installed. "
                    f"Run: ollama pull {config.model}",
                )
            elif status == 401:
                suggestions.insert(0, "API key may be invalid or expired. Run: fluid ai setup")
            raise CopilotGenerationError(
                "copilot_llm_request_failed",
                f"LLM request failed ({status}) for {config.provider} model '{config.model}'.",
                suggestions=suggestions,
            ) from exc
        except httpx.HTTPError as exc:
            raise CopilotGenerationError(
                "copilot_llm_network_error",
                f"LLM network error for provider {config.provider}: {exc}",
                suggestions=_LLM_REQUEST_SUGGESTIONS,
            ) from exc

    try:
        resp_json = response.json()
        usage = provider.extract_usage(resp_json)
        _cumulative_usage["input_tokens"] += usage.get("input_tokens", 0)
        _cumulative_usage["output_tokens"] += usage.get("output_tokens", 0)
        _cumulative_usage["total_tokens"] += usage.get("total_tokens", 0)
        return provider.extract_text(resp_json)
    except CopilotGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CopilotGenerationError(
            "copilot_llm_response_invalid",
            f"LLM response from {config.provider} could not be parsed.",
            suggestions=[
                "Verify the selected model supports JSON-friendly instruction following",
                "Try a different --llm-model or --llm-provider",
            ],
        ) from exc


def call_llm_streaming(
    provider: LlmProvider,
    config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
) -> Iterator[str]:
    """Slice UX-I: stream text deltas from the configured provider.

    This is a generator that yields text chunks as they arrive via
    SSE.  Callers typically accumulate into a buffer::

        chunks = []
        for chunk in call_llm_streaming(provider, config, sys, usr):
            chunks.append(chunk)
            # optional: update a live progress view
        raw_text = "".join(chunks)

    The concatenated buffer is byte-identical to what
    :func:`call_llm` would have returned for the same request.  Every
    downstream parser (``extract_json_object``, the retry loop,
    validation) works unchanged.

    Errors are translated to :class:`CopilotGenerationError` with the
    same suggestions as the blocking path.  HTTP transient failures
    are NOT retried here — callers that need retries should fall back
    to :func:`call_llm` or wrap the generator in their own loop.
    Retrying a partial SSE stream would require re-parsing chunks
    delivered before the failure, which is not worth the complexity
    when the blocking path already has a solid retry story.
    """
    url, headers, payload = provider.build_streaming_request(config, system_prompt, user_prompt)
    suggestions = [
        "Check the selected model and endpoint are correct",
        "Verify the API key environment variable is set",
        "Set FLUID_LLM_STREAMING=0 to fall back to the blocking path",
    ]
    try:
        with httpx.Client(timeout=config.timeout_seconds) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    # Drain the error body so the message is populated.
                    try:
                        response.read()
                    except Exception:  # noqa: BLE001
                        pass
                    status = exc.response.status_code
                    raise CopilotGenerationError(
                        "copilot_llm_request_failed",
                        f"LLM streaming request failed ({status}) "
                        f"for {config.provider} model '{config.model}'.",
                        suggestions=suggestions,
                    ) from exc
                yielded_any = False
                for chunk in provider.iter_stream_chunks(response):
                    if chunk:
                        yielded_any = True
                        yield chunk
                if not yielded_any:
                    raise CopilotGenerationError(
                        "copilot_llm_stream_empty",
                        f"LLM streaming response from {config.provider} was empty.",
                        suggestions=suggestions,
                    )
    except httpx.HTTPError as exc:
        raise CopilotGenerationError(
            "copilot_llm_network_error",
            f"LLM streaming network error for provider {config.provider}: {exc}",
            suggestions=suggestions,
        ) from exc


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

_SAFE_MODEL_RE = re.compile(r"^[a-zA-Z0-9._:/-]+$")


def _sanitize_model_for_url(model: str) -> str:
    """Reject model names that could cause path traversal in URL interpolation."""
    if not model or not _SAFE_MODEL_RE.match(model) or ".." in model:
        raise CopilotGenerationError(
            "copilot_invalid_model_name",
            f"Model name contains unsafe characters: {model!r}",
            suggestions=["Use a model name like 'gemini-2.5-flash' or 'gpt-4o'"],
        )
    return model


def _infer_provider_from_env(env: Mapping[str, str]) -> Optional[str]:
    detected = []
    if env.get("OPENAI_API_KEY"):
        detected.append("openai")
    if env.get("ANTHROPIC_API_KEY"):
        detected.append("anthropic")
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
        detected.append("gemini")
    if env.get("OLLAMA_HOST") or detect_ollama_available(env):
        detected.append("ollama")
    if len(detected) == 1:
        return detected[0]
    if not detected:
        # No env vars found — check the keyring for any saved provider key.
        return _infer_provider_from_keyring()
    return None


def _infer_provider_from_keyring() -> Optional[str]:
    """Return the provider name if exactly one has a saved keyring key."""
    detected = []
    for name in ("openai", "anthropic", "gemini"):
        if _get_api_key_from_keyring(name):
            detected.append(name)
    if len(detected) == 1:
        return detected[0]
    return None


def _resolve_api_key(provider: str, env: Mapping[str, str]) -> Optional[str]:
    if env.get("FLUID_LLM_API_KEY"):
        return env["FLUID_LLM_API_KEY"]
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var:
        key = env.get(env_var)
        if key:
            return key
    # Gemini accepts either the Forge-centric alias or Google's native env var.
    if provider == "gemini":
        key = env.get("GEMINI_API_KEY")
        if key:
            return key
        key = env.get("GOOGLE_API_KEY")
        if key:
            return key
    # Fallback: check the OS keyring for a saved key.
    return _get_api_key_from_keyring(provider)


# ---------------------------------------------------------------------------
# Keyring helpers
# ---------------------------------------------------------------------------

_LLM_KEYRING_PREFIX = "llm"


def _keyring_key(provider: str) -> str:
    return f"{_LLM_KEYRING_PREFIX}.{provider}.api_key"


def _get_api_key_from_keyring(provider: str) -> Optional[str]:
    """Retrieve a saved LLM API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        return KeyringCredentialStore.get_credential(_keyring_key(provider))
    except Exception:  # noqa: BLE001
        LOG.debug("Keyring read failed for %s", _keyring_key(provider))
        return None


def save_api_key_to_keyring(provider: str, api_key: str) -> bool:
    """Persist an LLM API key in the OS keyring for future runs."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.set_credential(_keyring_key(provider), api_key)
        return True
    except Exception:  # noqa: BLE001
        LOG.debug("Keyring write failed for %s", _keyring_key(provider))
        return False


def clear_api_key_from_keyring(provider: str) -> bool:
    """Remove a saved LLM API key from the OS keyring."""
    try:
        from fluid_build.credentials.keyring_store import KeyringCredentialStore

        KeyringCredentialStore.delete_credential(_keyring_key(provider))
        return True
    except Exception:  # noqa: BLE001
        LOG.debug("Keyring delete failed for %s", _keyring_key(provider))
        return False


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

    Checks (in order): env vars, then ``~/.fluid/ai_config.json`` (which
    contains the API key directly).  Used by ``fluid doctor`` and forge.
    """
    env = dict(environ or os.environ)

    provider_name = env.get("FLUID_LLM_PROVIDER") or _infer_provider_from_env(env)
    saved = None
    saved_model = None
    saved_key = None

    # If env vars don't reveal a provider, check the saved config file
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


def _load_model_catalog() -> Dict[str, Any]:
    """Load the model catalog with a two-tier resolution.

    1. ``~/.fluid/llm_models.json`` (user override — checked first)
    2. ``fluid_build/cli/llm_models.json`` (bundled baseline)

    The user override lets users add models or change defaults
    between CLI releases without touching installed packages.
    Cached per-process after the first successful load.
    """
    global _model_catalog_cache  # noqa: PLW0603
    if _model_catalog_cache is not None:
        return _model_catalog_cache

    # Tier 1: user override
    user_catalog = Path.home() / ".fluid" / "llm_models.json"
    if user_catalog.is_file():
        try:
            data = json.loads(user_catalog.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("providers"):
                _model_catalog_cache = data
                LOG.debug("Loaded user model catalog from %s", user_catalog)
                return _model_catalog_cache
        except Exception as exc:  # noqa: BLE001
            LOG.debug("User catalog at %s unreadable: %s", user_catalog, exc)

    # Tier 2: bundled baseline
    bundled_path = Path(__file__).with_name("llm_models.json")
    try:
        _model_catalog_cache = json.loads(bundled_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not load model catalog %s: %s", bundled_path, exc)
        _model_catalog_cache = {}
    return _model_catalog_cache


def get_catalog_default(provider: str) -> Optional[str]:
    """Return the catalog's default model for *provider*."""
    catalog = _load_model_catalog()
    entry = catalog.get("providers", {}).get(provider)
    if entry:
        return entry.get("default") or entry.get("flagship")
    return None


def get_catalog_routing_model(provider_name: str, strong_model: str = "") -> Optional[str]:
    """Return the catalog routing model when it differs from *strong_model*."""
    return _default_routing_model(provider_name, strong_model or "")


def get_catalog_tier_model(provider_name: str, tier: str = "flagship") -> Optional[str]:
    """Return the model for a given tier with v1/v2 compatibility mapping."""
    catalog = _load_model_catalog()
    tier_entry = catalog.get("tiers", {}).get(provider_name, {})
    if tier in tier_entry:
        return tier_entry.get(tier)
    entry = catalog.get("providers", {}).get(provider_name, {})
    legacy_tier = {
        "deep": "flagship",
        "balanced": "balanced",
        "fast": "routing",
    }.get(tier, tier)
    return (
        entry.get(legacy_tier) or entry.get(tier) or entry.get("flagship") or entry.get("default")
    )


def get_catalog_tier_models(provider_name: str) -> Dict[str, str]:
    """Return non-empty configured tier models for *provider_name*.

    The result is intentionally small and explicit: only ``deep``,
    ``balanced``, and ``fast`` are returned, and provider-schema fallback
    is applied for older catalog shapes. This gives command help,
    preflight, receipts, and stage agents one shared view of the model
    tiers instead of each caller re-walking ``llm_models.json``.
    """
    result: Dict[str, str] = {}
    for tier in ("deep", "balanced", "fast"):
        model = get_catalog_tier_model(provider_name, tier)
        if isinstance(model, str) and model.strip():
            result[tier] = model.strip()
    return result


def has_distinct_tier_models(provider_name: str) -> bool:
    """Return ``True`` iff the provider's tier map exposes ≥2 distinct models.

    The plan promises that ``--tiered`` (or
    ``copilot.tiered: true`` in ``ai_config.json``) silently
    collapses to single-model with a one-line warning when the
    selected provider has no distinct tiers — so we never crash, and
    we never bill the deep tier for a stage that can't actually
    benefit. Today ``llm_models.json`` ships Ollama with
    ``deep == balanced == llama3.1`` (only ``fast`` differs). When a
    future config or a user override flattens all three tiers to a
    single model, the helper reports ``False`` and the caller
    downgrades to single-model mode without surfacing a misleading
    "deep" indicator on every stage banner.

    Pure read against the catalog — no I/O on the hot path beyond the
    cached ``_load_model_catalog`` call ``get_catalog_tier_model``
    already uses.
    """
    catalog = _load_model_catalog()
    tier_entry = catalog.get("tiers", {}).get(provider_name, {})
    distinct = {value for value in tier_entry.values() if isinstance(value, str) and value.strip()}
    return len(distinct) >= 2


def build_llm_run_plan(config: LlmConfig, *, tiered: bool = False) -> Dict[str, Any]:
    """Build the user-facing plan for an AI forge run.

    The plan is deliberately honest: deterministic stages are marked
    deterministic even when a provider has a balanced tier configured.
    That prevents the UX from implying "balanced executes" for dbt SQL,
    which is generated deterministically from the logical sidecar.
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


def model_supports_structured_output(provider_name: str, model: str) -> bool:
    """Check the catalog for structured_output capability on *model*.

    Returns ``False`` for unknown models — the caller should fall
    back to the prompt-level JSON nudge.
    """
    return _model_has_capability(provider_name, model, "structured_output")


def model_supports_tool_use(provider_name: str, model: str) -> bool:
    """Check the catalog for tool_use capability on *model*."""
    return _model_has_capability(provider_name, model, "tool_use")


def _model_has_capability(provider_name: str, model: str, capability: str) -> bool:
    """Generic capability check against the catalog."""
    catalog = _load_model_catalog()
    models = catalog.get("providers", {}).get(provider_name, {}).get("models") or []
    lower = (model or "").lower()
    for m in models:
        if lower == m["id"].lower() or lower in [a.lower() for a in (m.get("aliases") or [])]:
            return bool(m.get("capabilities", {}).get(capability, False))
    return False


def resolve_model_name(provider: str, user_input: str) -> str:
    """Resolve a potentially fuzzy model name to its canonical id.

    Checks the bundled catalog for exact id matches and aliases.
    Returns *user_input* unchanged if no match is found (the API will
    decide whether it is valid).
    """
    text = (user_input or "").strip()
    if not text:
        return text
    catalog = _load_model_catalog()
    models = catalog.get("providers", {}).get(provider, {}).get("models") or []
    lower = text.lower()
    for entry in models:
        if lower == entry["id"].lower():
            return entry["id"]
        for alias in entry.get("aliases") or []:
            if lower == alias.lower():
                return entry["id"]
    return text


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
# ---------------------------------------------------------------------------
_sync_provider_defaults_from_catalog()
