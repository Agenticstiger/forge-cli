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

"""langchain-core ChatModel adapter for the forge copilot agent layer.

The legacy
:mod:`fluid_build.cli.forge_copilot_llm_providers` module is
~2.4K lines of bespoke HTTP request-shaping per provider. The world-class
audit identified three real gaps that all trace back to that layer:

* **Provider parity**: Gemini structured output is disabled, Ollama
  has no first-class ``tool_use``, prompt caching is Anthropic-only.
* **Streaming usage**: SSE streaming runs record
  ``record_missing_usage()`` because token blocks aren't extracted on
  the wire — cost dashboards systematically under-report.
* **Maintenance liability**: every new model quirk (Opus 4.7 temperature
  deprecation, Gemini schema budget, o-series thinking blocks) costs
  this team an upstream fix.

This module replaces *just the provider layer* with langchain-core
ChatModels:

* ``ChatAnthropic`` — first-class ``tool_use`` + prompt caching via
  ``cache_control`` on system messages.
* ``ChatOpenAI`` — ``with_structured_output`` over native JSON-Schema
  response format with token usage in ``AIMessage.usage_metadata``.
* ``ChatGoogleGenerativeAI`` — re-enables Gemini structured output via
  ``responseSchema`` (was disabled in the legacy path).
* ``ChatOllama`` — opt-in tool_use via the OpenAI-compatible
  ``/v1/chat/completions`` adapter.

The agent layer's ``_call_once`` branches on the
``FLUID_USE_LANGCHAIN_PROVIDERS`` feature flag (or
``LlmConfig.use_langchain``) so the legacy code path stays available
for users who haven't installed the ``[langchain]`` extra.

The langchain-core import happens inside :func:`build_chat_model` so
this module is safe to import on the legacy path — only opting in
forces the dependency to resolve.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple, Type

from pydantic import BaseModel

from fluid_build.cli.forge_copilot_llm_providers import LlmConfig

LOG = logging.getLogger("fluid.cli.forge_copilot.lc")

__all__ = [
    "build_chat_model",
    "call_structured_via_langchain",
    "extract_usage_from_message",
    "is_langchain_provider_enabled",
]


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


def is_langchain_provider_enabled(
    *, capability_matrix: Optional[Dict[str, Any]] = None
) -> bool:
    """Return ``True`` when the langchain provider path should be used.

    Resolution order:

    1. ``capability_matrix["use_langchain_providers"]`` if explicitly set.
    2. ``FLUID_USE_LANGCHAIN_PROVIDERS`` env var (truthy values).
    3. Default: ``False`` (legacy path stays the default until rollout
       is deemed safe by the corpus replay suite).
    """
    if capability_matrix is not None:
        explicit = capability_matrix.get("use_langchain_providers")
        if explicit is not None:
            return bool(explicit)
    raw = os.environ.get("FLUID_USE_LANGCHAIN_PROVIDERS", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Temperature handling
#
# Anthropic deprecated ``temperature`` on Opus 4.7+ models. Until the
# upstream langchain-anthropic package learns to drop the field
# automatically, we have to do it ourselves — same logic the legacy
# provider uses (model-name prefix match).
# ---------------------------------------------------------------------------


_ANTHROPIC_NO_TEMPERATURE_PREFIXES = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-3-5-sonnet-thinking",
)


def _model_deprecates_temperature(provider: str, model: str) -> bool:
    if provider in {"anthropic", "claude"}:
        return any(model.startswith(p) for p in _ANTHROPIC_NO_TEMPERATURE_PREFIXES)
    return False


def _resolve_temperature(provider: str, model: str) -> Optional[float]:
    """Return the temperature to set on the ChatModel, or ``None`` if
    the model rejects the field."""
    if _model_deprecates_temperature(provider, model):
        return None
    raw = os.environ.get("FLUID_LLM_TEMPERATURE", "0.0")
    try:
        return max(0.0, min(2.0, float(raw)))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# ChatModel factory
# ---------------------------------------------------------------------------


def build_chat_model(
    config: LlmConfig,
    *,
    capability_matrix: Optional[Dict[str, Any]] = None,
) -> Any:
    """Return a langchain-core ``BaseChatModel`` for the resolved
    provider configuration.

    Imports the relevant provider package lazily so unused providers
    don't have to be installed alongside the one(s) actually in use
    (e.g. a user on Anthropic only never imports
    ``langchain-google-genai``).
    """
    cm = capability_matrix or {}
    provider = config.provider
    temperature = _resolve_temperature(provider, config.model)
    timeout = float(config.timeout_seconds) if config.timeout_seconds else 120.0

    if provider in {"anthropic", "claude"}:
        from langchain_anthropic import ChatAnthropic

        kwargs: Dict[str, Any] = {
            "model": config.model,
            "max_tokens": int(cm.get("max_tokens") or 4096),
            "timeout": timeout,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.endpoint:
            # ChatAnthropic accepts ``base_url`` for proxy / Bedrock-compat
            # endpoints. The legacy default-endpoint logic produces a
            # full ``/v1/messages`` URL — strip the path so langchain
            # appends its own.
            kwargs["base_url"] = _strip_path(config.endpoint)
        return ChatAnthropic(**kwargs)

    if provider in {"openai", "azure-openai"}:
        from langchain_openai import ChatOpenAI

        kwargs = {
            "model": config.model,
            "timeout": timeout,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if config.api_key:
            kwargs["api_key"] = config.api_key
        if config.endpoint:
            kwargs["base_url"] = _strip_path(config.endpoint)
        return ChatOpenAI(**kwargs)

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        kwargs = {
            "model": config.model,
            "timeout": timeout,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if config.api_key:
            kwargs["google_api_key"] = config.api_key
        return ChatGoogleGenerativeAI(**kwargs)

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        base_url = _strip_path(config.endpoint) if config.endpoint else None
        kwargs = {"model": config.model}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOllama(**kwargs)

    raise ValueError(
        f"langchain provider path does not yet support provider {provider!r}; "
        "set FLUID_USE_LANGCHAIN_PROVIDERS=0 or extend "
        "fluid_build.cli.forge_copilot_lc_providers.build_chat_model"
    )


def _strip_path(endpoint: str) -> str:
    """Return ``endpoint`` with the URL path removed.

    The legacy provider path stores full method URLs (e.g.
    ``https://api.anthropic.com/v1/messages``); langchain expects a
    base URL (``https://api.anthropic.com``) and appends its own
    method paths. Stripping is best-effort — if the URL doesn't parse
    we hand it back as-is.
    """
    if not endpoint:
        return endpoint
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(endpoint)
        if parsed.scheme and parsed.netloc:
            return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    except Exception:  # noqa: BLE001 — defensive, never break the call
        pass
    return endpoint


# ---------------------------------------------------------------------------
# Structured-output call
# ---------------------------------------------------------------------------


def call_structured_via_langchain(
    config: LlmConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    output_schema: Type[BaseModel],
    capability_matrix: Optional[Dict[str, Any]] = None,
) -> Tuple[BaseModel, Dict[str, int]]:
    """Run one structured-output call through langchain-core.

    Parameters
    ----------
    config
        Resolved :class:`LlmConfig` — same shape used by the legacy
        provider path.
    system_prompt, user_prompt
        The two-message envelope every staged agent uses today. Kept
        as a 2-arg signature so this is a drop-in replacement for the
        legacy ``provider.build_request(...)`` + ``httpx.post`` flow.
    output_schema
        The Pydantic model to validate against. Bound through
        :meth:`langchain_core.language_models.BaseChatModel.with_structured_output`
        with ``include_raw=True`` so we can also extract token usage
        from the underlying ``AIMessage``.
    capability_matrix
        Per-session capability flags (max tokens, prompt caching,
        streaming). Forwarded to :func:`build_chat_model` and consulted
        for prompt caching.

    Returns
    -------
    tuple ``(parsed_instance, usage_dict)``
        The parsed Pydantic instance and a normalized usage dict with
        keys ``input_tokens`` / ``output_tokens`` / ``cache_read_tokens``
        / ``cache_write_tokens`` (cache-* keys may be zero when the
        provider doesn't report them). The usage dict matches the
        existing legacy schema so the cost tracker doesn't have to
        change shape.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    chat_model = build_chat_model(config, capability_matrix=capability_matrix)
    messages = _build_messages(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        provider=config.provider,
        capability_matrix=capability_matrix or {},
        SystemMessage=SystemMessage,
        HumanMessage=HumanMessage,
    )

    structured = chat_model.with_structured_output(output_schema, include_raw=True)
    response = structured.invoke(messages)

    parsed = response.get("parsed")
    raw_msg = response.get("raw")
    parsing_error = response.get("parsing_error")

    if parsed is None:
        # Re-raise the parser's exception so the caller's ``except
        # ValidationError`` (now ``SchemaValidationError`` after the
        # typed-error pass) takes the corrective-feedback path.
        if parsing_error is not None:
            raise parsing_error
        raise RuntimeError(
            f"Structured output for schema {output_schema.__name__} returned None"
        )

    usage = extract_usage_from_message(raw_msg)
    return parsed, usage


def _build_messages(
    *,
    system_prompt: str,
    user_prompt: str,
    provider: str,
    capability_matrix: Dict[str, Any],
    SystemMessage: Any,
    HumanMessage: Any,
) -> list:
    """Build the message list, applying provider-specific extras like
    Anthropic prompt-cache control on the system prompt.

    Anthropic prompt caching gives a ~90% input-token discount on the
    cached span — the system prompt is the obvious cache target since
    it's identical across stage calls within a session. langchain's
    ``ChatAnthropic`` honors ``cache_control`` annotations on message
    content blocks, so we attach one to the system prompt iff the
    capability matrix asks for it.
    """
    cache_system = bool(
        capability_matrix.get(
            "anthropic_prompt_cache",
            provider in {"anthropic", "claude"},
        )
    )
    if cache_system and provider in {"anthropic", "claude"}:
        system_msg = SystemMessage(
            content=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
    else:
        system_msg = SystemMessage(content=system_prompt)
    return [system_msg, HumanMessage(content=user_prompt)]


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------


def extract_usage_from_message(raw_msg: Any) -> Dict[str, int]:
    """Normalize ``AIMessage.usage_metadata`` into the legacy usage dict.

    langchain-core 0.3 standardised ``usage_metadata`` across providers:

    .. code-block:: python

        {
            "input_tokens": int,
            "output_tokens": int,
            "total_tokens": int,
            "input_token_details": {"cache_read": int, "cache_creation": int},
            "output_token_details": {...},
        }

    The legacy cost tracker takes ``input_tokens`` / ``output_tokens``
    plus optional cache fields. Returning an empty dict when usage
    metadata is missing matches the legacy behaviour
    (``record_missing_usage``).
    """
    if raw_msg is None:
        return {}
    usage_md: Dict[str, Any] = getattr(raw_msg, "usage_metadata", None) or {}
    if not usage_md:
        # Fallback to ``response_metadata`` for providers that haven't
        # populated usage_metadata yet (notably some streaming paths).
        response_md: Dict[str, Any] = getattr(raw_msg, "response_metadata", None) or {}
        usage_md = response_md.get("usage", {}) or response_md.get(
            "token_usage", {}
        ) or {}

    input_tokens = int(
        usage_md.get("input_tokens")
        or usage_md.get("prompt_tokens")
        or 0
    )
    output_tokens = int(
        usage_md.get("output_tokens")
        or usage_md.get("completion_tokens")
        or 0
    )

    input_details = usage_md.get("input_token_details") or {}
    cache_read = int(input_details.get("cache_read") or 0)
    cache_write = int(
        input_details.get("cache_creation")
        or input_details.get("cache_write")
        or 0
    )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }
