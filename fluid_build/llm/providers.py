# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
    "agent_loop_requested",
    "preflight_llm_provider",
    "fill_llm_args_from_setup",
    "named_llm_provider",
    "remember_named_llm_provider",
    "resolve_llm_config",
    "resolve_model_name",
    "resolve_ollama_model",
    "call_llm",
    "call_llm_streaming",
]

import json
import logging
import os
import re
import shutil
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Mapping, Optional

import httpx

from fluid_build._contract_loader import CLIError
from fluid_build.llm.ollama_host import ollama_daemon_url

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


def _get_timeout_seconds(args: Any, env: Mapping[str, str]) -> int:
    raw = getattr(args, "llm_timeout_seconds", None) or env.get("FLUID_LLM_TIMEOUT_SECONDS")
    if raw in (None, ""):
        return 120
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 120
    return max(1, min(3600, value))


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
        # super().__init__ sets error_slug + catalog-enriched suggestions/docs_url.
        super().__init__(1, event, payload)
        self.message = message
        # Caller suggestions win; otherwise keep the catalog enrichment from the
        # base __init__ (don't clobber it back to []). docs_url is untouched here
        # so the catalog's docs link survives.
        self.suggestions = suggestions or self.suggestions


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
    # True when the operator stated ``endpoint`` (``--llm-endpoint`` or
    # ``FLUID_LLM_ENDPOINT``) rather than the provider deriving it. A stated
    # endpoint is where every call goes, so routing calls keep it.
    endpoint_explicit: bool = False

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
        elif not self.endpoint_explicit:
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
        return redact_endpoint(self.endpoint)


def redact_endpoint(endpoint: str) -> str:
    """*endpoint* fit to show a person: credential query values and all userinfo masked.

    The whole ``user:password@`` goes, not only the password: a token is often
    passed as a bare username (``https://TOKEN@host``).
    """
    endpoint = endpoint or ""
    # Redact common credential query parameters
    endpoint = re.sub(
        r"([?&](?:key|token|api_key|auth|secret|credential|password)=)[^&]+",
        r"\1***",
        endpoint,
        flags=re.I,
    )
    # Redact userinfo in URLs (user:pass@host); a URL scheme is case-insensitive.
    return re.sub(r"(https?://)([^@/]+)@", r"\1***:***@", endpoint, flags=re.I)


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
        self,
        api_key: Optional[str],
        env: Mapping[str, str],
        endpoint: Optional[str] = None,
    ) -> Optional[List[str]]:
        """Return live provider model ids when a model-list API exists.

        *endpoint* is the address the operator stated for the provider, when
        there is one: the list must come from where the call will go.
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


_LITELLM_ALTERNATIVES = (
    "Or author without it: --llm-provider claude-code (uses your Claude Code login) "
    "or --template starter."
)


def _require_litellm(provider_name: str) -> None:
    """Fail now, before any context or memory is loaded, when litellm is absent.

    Only the litellm-backed providers reach this; keyless coding agents
    return earlier. The error is a
    :class:`~fluid_build._optional.MissingOptionalDependency`, which the CLI
    prints as the install line and exits 1 on, with no traceback.
    """
    from fluid_build._optional import ensure_installed

    ensure_installed(
        "litellm",
        "litellm",
        f"AI-assisted authoring (--llm-provider {provider_name})",
        _LITELLM_ALTERNATIVES,
    )


def get_llm_provider(name: str) -> LlmProvider:
    """Resolve a provider adapter by name.

    Every provider in fluid is a :class:`LiteLLMProvider` shim — the
    native per-provider httpx classes were deleted in favour of
    litellm's unified backend. ``name`` is canonicalised (``"claude"``
    → ``"anthropic"``) and dispatched through
    :func:`get_litellm_provider`.

    Third-party plugins (entry-point group ``fluid_build.llm_providers``) are
    resolved AFTER the built-ins and BEFORE the litellm catch-all: a built-in
    name always wins a clash, then a discovered plugin resolves, and finally any
    remaining name falls through to litellm's native provider routing. Plugin
    discovery is lazy (first call here) so ``fluid --help`` never scans it.

    ``litellm`` is an optional dependency (the ``[litellm]`` / ``[standard]``
    extra; not in the base install since 0.28.0). Choosing a litellm-backed
    provider without the package raises ``MissingOptionalDependency`` here, up
    front, carrying the install line and the keyless alternatives.
    """
    normalized = (name or "").strip().lower()
    # Coding-agent providers (claude-code/codex/cursor/kiro) shell out to a
    # local agent CLI — keyless for Claude Code, key-reusing for the rest.
    # Resolved BEFORE the litellm delegation so their hyphenated names aren't
    # collapsed by ``normalize_llm_provider_name`` (claude-code -> anthropic).
    from fluid_build.llm.coding_agent import (
        get_coding_agent_provider,
        is_coding_agent,
    )

    if is_coding_agent(normalized):
        return get_coding_agent_provider(normalized)

    # Built-in litellm-backed providers (openai/anthropic/claude/gemini/ollama)
    # win over any plugin of the same name — resolved before consulting plugins.
    canonical = normalize_llm_provider_name(normalized)
    from fluid_build.llm.litellm_backend import get_litellm_provider

    if canonical in BUILTIN_LLM_PROVIDERS:
        _require_litellm(canonical)
        return get_litellm_provider(canonical)

    # Third-party provider plugins (fluid_build.llm_providers entry points).
    # Looked up under the raw (hyphen-preserving) name so an entry point named
    # ``azure-openai`` resolves for ``--llm-provider azure-openai``.
    from fluid_build.llm.provider_plugins import get_plugin_llm_provider

    plugin = get_plugin_llm_provider(normalized)
    if plugin is not None:
        return plugin

    # Fallback: litellm's catch-all for any other provider it natively supports
    # (e.g. github/azure/bedrock/vertex_ai) — unchanged behaviour.
    _require_litellm(canonical)
    return get_litellm_provider(canonical)


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


def has_llm_api_key(
    provider_name: str,
    environ: Optional[Mapping[str, str]] = None,
    *,
    named: bool = False,
) -> bool:
    """Return whether the provider can resolve credentials without exposing them.

    ``named`` as the provider name it was asked for.
    """
    env = dict(environ or os.environ)
    provider = get_llm_provider(provider_name)
    if provider.name == "ollama":
        return detect_ollama_available(env)
    return bool(_resolve_api_key(provider.name, env, named=named))


# Providers that never require an LLM API key — the call is paid for
# elsewhere, so the api-key gate below must skip them. ``ollama`` runs a
# local server. Part B's coding-agent providers
# (claude-code/codex/cursor/kiro) are appended here too — they shell out to an
# installed agent CLI, and any per-agent key (CODEX_API_KEY, CURSOR_API_KEY,
# KIRO_API_KEY) is validated *inside* the provider at call time, not at this
# resolve-time gate. ``provider.name`` is always canonical (hyphen form); the
# underscore alias is included defensively.
_KEYLESS_PROVIDERS: frozenset = frozenset({"ollama", "claude-code", "codex", "cursor", "kiro"})

# The keyless providers that do not go through litellm: the call is made by an
# installed agent CLI, so a missing litellm extra is no obstacle.
_NON_LITELLM_PROVIDERS: frozenset = _KEYLESS_PROVIDERS - {"ollama"}


def _provider_name_from(args: Any, env: Mapping[str, str]) -> Optional[str]:
    """The provider the flags and environment select.

    The named provider (``--llm-provider``, ``FLUID_LLM_PROVIDER``,
    ``FLUID_FORGE_AGENT``), else :func:`_infer_provider_from_env`: one key
    variable, the saved ``fluid ai setup`` choice, one keyring key, then ambient
    discovery. :func:`check_llm_readiness` calls this too (``args=None``).

    ``None`` when nothing selects one. There is no default provider: guessing
    one would send ``FLUID_LLM_API_KEY`` to a vendor it may not belong to, and
    ask for that vendor's key when other keys are set.
    """
    return _explicit_provider_name(args, env) or _infer_provider_from_env(
        env,
        non_interactive=bool(getattr(args, "non_interactive", False)),
        allow_agents=_agents_allowed(args, env),
    )


def agent_loop_requested(flag: Any, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this run asks for the multi-turn agent loop.

    ``flag`` is the ``--agent-loop`` value (callers read it their own way, e.g.
    the CLI's ``get_cli_arg``), or ``FLUID_COPILOT_AGENT_LOOP`` set to a truthy
    value. It is parsed as a boolean, not by presence:
    ``FLUID_COPILOT_AGENT_LOOP=0`` is the documented way to turn the loop off.
    """
    env = os.environ if environ is None else environ
    return bool(flag) or (
        env.get("FLUID_COPILOT_AGENT_LOOP", "").strip().lower() in {"1", "true", "yes", "on"}
    )


def _agents_allowed(args: Any, env: Mapping[str, str]) -> bool:
    """Whether an agent this run did not name may be used: auto-picked or saved.

    Not for the agent loop (it needs tool use, which the agents do not offer),
    and not when ``--llm-endpoint`` / ``--llm-model`` (or their routing twins,
    as flags or ``FLUID_LLM_*`` variables) state where or what to call: that
    destination is never swapped for an agent.
    """
    return not (
        agent_loop_requested(getattr(args, "agent_loop", False), env)
        or any(
            getattr(args, name, None)
            for name in ("llm_endpoint", "llm_model", "llm_routing_endpoint", "llm_routing_model")
        )
        or any(
            env.get(var)
            for var in (
                "FLUID_LLM_ENDPOINT",
                "FLUID_LLM_MODEL",
                "FLUID_LLM_ROUTING_ENDPOINT",
                "FLUID_LLM_ROUTING_MODEL",
            )
        )
    )


def _explicit_provider_name(args: Any, env: Mapping[str, str]) -> Optional[str]:
    """The provider the operator named: ``--llm-provider``, else the env selectors."""
    return (
        getattr(args, "llm_provider", None)
        or env.get("FLUID_LLM_PROVIDER")
        or env.get("FLUID_FORGE_AGENT")
        or None
    )


# Interactive forge puts the provider its inline setup found into
# ``args.llm_provider``, where it would read as named. Before that, it records
# here what the operator named (``None`` for nothing), so ``FLUID_LLM_API_KEY``
# still goes only to a provider the operator named.
_NAMED_PROVIDER_ATTR = "llm_provider_named"
_NOT_RECORDED = object()


def _recorded_named_provider(args: Any) -> Any:
    """What :func:`remember_named_llm_provider` recorded on *args*, else ``_NOT_RECORDED``.

    Only a string or ``None`` is a record; anything else (no attribute, a
    test double's auto-attribute) is not.
    """
    value = getattr(args, _NAMED_PROVIDER_ATTR, _NOT_RECORDED)
    return value if value is None or isinstance(value, str) else _NOT_RECORDED


def remember_named_llm_provider(args: Any, environ: Optional[Mapping[str, str]] = None) -> None:
    """Record which provider the operator named, before ``args.llm_provider`` is replaced.

    Call it before putting a provider that setup found into ``args.llm_provider``.
    A record already made is kept, so a second setup cannot turn its own pick
    into a named provider.
    """
    if _recorded_named_provider(args) is not _NOT_RECORDED:
        return
    env = os.environ if environ is None else environ
    setattr(args, _NAMED_PROVIDER_ATTR, _explicit_provider_name(args, env))


def named_llm_provider(args: Any, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The provider the operator named for this run, else ``None``.

    ``--llm-provider``, else ``FLUID_LLM_PROVIDER``, else ``FLUID_FORGE_AGENT``:
    the first thing every selection reads (:func:`_provider_name_from`). A run
    that names one does not run interactive setup, which cannot know the name.
    """
    env = os.environ if environ is None else environ
    flag = _stated_arg(args, "llm_provider")
    return flag or _explicit_provider_name(None, env)


def _stated_arg(args: Any, name: str) -> Optional[str]:
    """``args.<name>`` when the run states it, else ``None``.

    Only a non-empty string counts, so a test double's invented attribute does not.
    """
    value = getattr(args, name, None)
    return value if isinstance(value, str) and value else None


def fill_llm_args_from_setup(
    args: Any, config: LlmConfig, environ: Optional[Mapping[str, str]] = None
) -> bool:
    """Put what interactive setup found into *args*, under what the operator stated.

    Inline setup (``cli.ai_setup.run_ai_setup_inline``) finds a provider without
    reading the run's flags, so what the operator stated wins:

    * A named provider (:func:`named_llm_provider`) takes nothing from setup.
    * An agent that setup found (a coding-agent CLI) is not used
      when the run states an endpoint or a model, or runs the agent loop, as in
      a run without a TTY (``provider_resolve._passed_over``).
    * Otherwise setup's provider goes into ``args.llm_provider``, recorded first
      as not named (:func:`remember_named_llm_provider`). Its model and endpoint
      fill ``args.llm_model`` and ``args.llm_endpoint`` only where the run states
      neither the flag nor ``FLUID_LLM_MODEL`` / ``FLUID_LLM_ENDPOINT``.

    Returns whether setup's provider is used. Only then may the caller put its
    key into the session (``cli.ai_setup.set_session_env``).
    """
    env = os.environ if environ is None else environ
    remember_named_llm_provider(args, env)
    recorded = _recorded_named_provider(args)
    if isinstance(recorded, str) and recorded:  # the operator named a provider
        return False
    # The run's own statements only, so a test double's invented attribute
    # does not read as a stated endpoint, model or agent loop.
    stated = SimpleNamespace(
        agent_loop=getattr(args, "agent_loop", False) is True,
        **{
            name: _stated_arg(args, name)
            for name in ("llm_endpoint", "llm_model", "llm_routing_endpoint", "llm_routing_model")
        },
    )
    if config.provider in _NON_LITELLM_PROVIDERS and not _agents_allowed(stated, env):
        # A constant line: *config* holds a key (CodeQL
        # py/clear-text-logging-sensitive-data).
        LOG.warning(
            "Not using the agent setup found: an agent is not used when the run states "
            "an endpoint or a model, or runs the agent loop. Name a provider with "
            "--llm-provider."
        )
        return False
    args.llm_provider = config.provider
    if not (_stated_arg(args, "llm_model") or env.get("FLUID_LLM_MODEL")):
        args.llm_model = config.model
    if not (_stated_arg(args, "llm_endpoint") or env.get("FLUID_LLM_ENDPOINT")):
        args.llm_endpoint = config.endpoint
    return True


def _unnamed_provider_note(
    args: Any, env: Mapping[str, str], provider_name: str, named: bool
) -> Optional[str]:
    """Why ``FLUID_LLM_API_KEY`` is not used for *provider_name*, else ``None``.

    ``FLUID_LLM_API_KEY`` does not say which vendor it is for, so a provider
    forge picked itself never gets it (``provider_resolve._resolve_api_key``).
    The note is logged, so it is built from provider names alone and never from
    anything that holds or resolves a key (CodeQL
    py/clear-text-logging-sensitive-data).
    """
    if named or not env.get("FLUID_LLM_API_KEY"):
        return None
    recorded = _recorded_named_provider(args)
    if isinstance(recorded, str) and recorded:  # interactive setup replaced the choice
        return (
            f"FLUID_LLM_API_KEY is set but not used: it is for {recorded}, the provider "
            f"you named, and this run uses {provider_name}."
        )
    return (
        "FLUID_LLM_API_KEY is set but not used: it goes only to a provider named with "
        f"--llm-provider or FLUID_LLM_PROVIDER, and forge picked {provider_name} itself. "
        f"Pass --llm-provider {provider_name} to use the key with it."
    )


def _same_llm_provider(first: str, second: str) -> bool:
    """Whether two provider names select the same provider (``claude`` is ``anthropic``)."""
    return normalize_llm_provider_name(first) == normalize_llm_provider_name(second)


def _provider_is_named(args: Any, env: Mapping[str, str], provider_name: str) -> bool:
    """Whether *provider_name*, the provider this run uses, is one the operator named.

    Without a record, a named provider always wins inference, so the run uses
    it whenever one is named. With one, ``args.llm_provider`` may be what setup
    found, which counts only when it is the provider the operator named.
    """
    recorded = _recorded_named_provider(args)
    explicit = _explicit_provider_name(args, env)
    if recorded is _NOT_RECORDED:
        return bool(explicit)
    if not recorded:
        return False
    return recorded == explicit or _same_llm_provider(recorded, provider_name)


_AGENT_LOOP_ALTERNATIVES = (
    "Or author without it: --template starter. The agent loop needs a provider that "
    "supports tool use, which the keyless providers (claude-code, codex, cursor, kiro) do not."
)


def preflight_llm_provider(args: Any, environ: Optional[Mapping[str, str]] = None) -> None:
    """Fail fast when the selected provider cannot be used on this install.

    Cheap and side-effect free (no key lookup, no litellm import, no network
    beyond the localhost Ollama probe of the keyless auto-pick): it only
    resolves the provider adapter, which raises
    :class:`~fluid_build._optional.MissingOptionalDependency` when a
    litellm-backed provider is chosen and litellm is absent. Call it before
    anything that reads workspace context or memory.

    The error is tailored to the run: ``(--llm-provider X)`` only when the flag
    was passed, and in ``--agent-loop`` only the alternative that works there.
    """
    from fluid_build._optional import MissingOptionalDependency, ensure_installed

    env = dict(environ or os.environ)
    agent_loop = agent_loop_requested(getattr(args, "agent_loop", False), env)
    if not _explicit_provider_name(args, env):
        # Nothing chosen: inference reads the keyring and probes Ollama, and every
        # provider it can pick needs litellm, except the coding agent a
        # non-interactive run auto-picks. Without litellm, that
        # agent is the only provider that can run; otherwise fail first.
        from fluid_build._optional import is_available

        if not is_available("litellm") and _lean_keyless_autopick(
            env,
            non_interactive=bool(getattr(args, "non_interactive", False)),
            allow_agents=_agents_allowed(args, env),
        ):
            return
        ensure_installed(
            "litellm",
            "litellm",
            "AI-assisted authoring",
            _AGENT_LOOP_ALTERNATIVES if agent_loop else _LITELLM_ALTERNATIVES,
        )
    name = _provider_name_from(args, env)
    if not isinstance(name, str):
        return
    try:
        get_llm_provider(name)
    except MissingOptionalDependency as exc:
        flagged = bool(getattr(args, "llm_provider", None))
        how = f"--llm-provider {name}" if flagged else f"provider {name}"
        raise MissingOptionalDependency(
            exc.module,
            exc.extra,
            f"AI-assisted authoring ({how})",
            alternatives=_AGENT_LOOP_ALTERNATIVES if agent_loop else _LITELLM_ALTERNATIVES,
        ) from None


def resolve_llm_config(args: Any, environ: Optional[Mapping[str, str]] = None) -> LlmConfig:
    """Resolve provider, model, endpoint, and API key from flags and env vars."""
    env = dict(environ or os.environ)
    provider_name = _provider_name_from(args, env)
    if provider_name is None:
        # The suggestions come from the error catalog (``_error_catalog``).
        raise CopilotGenerationError("copilot_no_llm_provider", no_provider_message(env))
    provider = get_llm_provider(provider_name)

    # The saved ``fluid ai setup`` choice fills in the model and the Ollama host
    # this run does not state, for its own provider and only when the run names
    # none: what ``check_llm_readiness`` reports.
    saved = _saved_choice_for(args, env, provider.name)

    # A stated endpoint is where the call goes, so the Ollama model pick and the
    # model preflight below ask that daemon, not OLLAMA_HOST or localhost.
    stated_endpoint = getattr(args, "llm_endpoint", None) or env.get("FLUID_LLM_ENDPOINT")
    models_endpoint = stated_endpoint if isinstance(stated_endpoint, str) else None

    # Resolve model: explicit flag → env var → saved choice → catalog default → class default.
    catalog_default = get_catalog_default(provider.name)
    explicit_model = (
        getattr(args, "llm_model", None) or env.get("FLUID_LLM_MODEL") or saved.get("model")
    )
    if explicit_model:
        model = resolve_model_name(provider.name, explicit_model)
        model_source = "explicit"
    elif provider.name == "ollama":
        model = resolve_ollama_model(env, models_endpoint)
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

    named = _provider_is_named(args, env, provider.name)
    api_key = _resolve_api_key(provider.name, env, named=named)
    unnamed_note = _unnamed_provider_note(args, env, provider.name, named)
    # A third-party provider plugin may declare ``keyless = True`` (class attr)
    # when it authenticates by some other means (e.g. an ambient cloud identity
    # or its own env var validated at call time) — same escape the built-in
    # keyless providers get, extended to plugins.
    provider_is_keyless = provider.name in _KEYLESS_PROVIDERS or bool(
        getattr(provider, "keyless", False)
    )
    if not provider_is_keyless and not api_key:
        raise CopilotGenerationError(
            "copilot_missing_llm_api_key",
            f"No API key was configured for the {provider.name} copilot adapter.",
            suggestions=[
                *([unnamed_note] if unnamed_note else []),
                "Run 'fluid ai setup' to configure a provider interactively",
                "Set the provider's own key variable, or FLUID_LLM_API_KEY with --llm-provider",
                "Examples: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY",
                "For local models, use --llm-provider ollama and optionally --llm-endpoint",
                "For keyless authoring: use a local coding agent (e.g. --llm-provider claude-code)",
            ],
        )

    model_notes: List[str] = []
    if unnamed_note:
        model_notes.append(unnamed_note)
        LOG.warning(unnamed_note)
    if (
        not _explicit_provider_name(args, env)
        and not saved
        and provider.name in _NON_LITELLM_PROVIDERS
    ):
        # Nothing named or saved a provider and it is not a key-based one, so the
        # keyless auto-pick chose it: say which, and how to override.
        note = (
            f"No LLM provider is configured; using {provider.name}, the first coding agent "
            f"found on PATH. Override with --llm-provider."
        )
        model_notes.append(note)
        LOG.warning(note)
    preflight_requested = _model_preflight_requested(args, env)
    available_models: Optional[List[str]] = None

    def _available_models() -> Optional[List[str]]:
        nonlocal available_models
        if available_models is not None:
            return available_models
        try:
            # The keyword goes only with a stated endpoint, so a provider plugin
            # written against the two-argument signature keeps working without one.
            stated = {"endpoint": models_endpoint} if models_endpoint else {}
            available_models = provider.list_available_models(api_key, env, **stated)
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

    endpoint = stated_endpoint
    endpoint_explicit = bool(endpoint)
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
        endpoint_explicit=endpoint_explicit,
    )


def _default_routing_model(provider_name: str, strong_model: str) -> Optional[str]:
    """Re-export shim for the extracted helper.

    Catalog logic now lives in :mod:`fluid_build.llm.model_catalog`.
    Tests that ``patch("fluid_build.llm.providers._default_routing_model", ...)``
    still work because the patched name remains in this module's namespace.
    """
    from fluid_build.llm.model_catalog import _default_routing_model as _impl

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
        # locals if available; non-litellm providers (coding agents) yield
        # None/zero which still drives a correct call into record_call.
        usd_override: Optional[float] = None
        cache_creation = 0
        cache_read = 0
        try:
            from fluid_build.llm.litellm_backend import (
                get_last_cache_tokens,
                get_last_litellm_cost_usd,
            )

            usd_override = get_last_litellm_cost_usd()
            if usd_override is None:
                # Coding-agent providers (Claude Code) report cost on their own
                # thread-local; litellm never ran for them, so fall back to it.
                try:
                    from fluid_build.llm.coding_agent import (
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
# ``provider_resolve`` binds this module's names at the end of its own load, so
# either module can be imported first.
from fluid_build.llm.provider_resolve import (  # noqa: E402,F401
    _LLM_KEYRING_PREFIX,
    _canonical_provider_name,
    _explicit_key_signals,
    _get_api_key_from_keyring,
    _infer_provider_from_env,
    _infer_provider_from_explicit_keys,
    _infer_provider_from_keyring,
    _keyring_key,
    _lean_keyless_autopick,
    _resolve_api_key,
    _saved_choice_for,
    ambiguous_keys_message,
    no_provider_message,
)


def reset_llm_caches() -> None:
    """Clear per-process caches so detection and catalog are re-evaluated."""
    global _ollama_available_cache, _model_catalog_cache  # noqa: PLW0603
    _ollama_available_cache = None
    _model_catalog_cache = None
    # Also drop the discovered-plugin registry so a newly `pip install`-ed
    # provider plugin is picked up without restarting the process.
    try:
        from fluid_build.llm.provider_plugins import reset_plugin_registry

        reset_plugin_registry()
    except Exception:  # noqa: BLE001 - cache reset must never raise
        pass


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

    The provider comes from the ladder the real selection walks
    (:func:`_provider_name_from`), so ``fluid doctor`` names what a run uses:

    1. A named provider: ``FLUID_LLM_PROVIDER`` or ``FLUID_FORGE_AGENT``.
    2. Exactly one explicit key variable (``OPENAI_API_KEY`` /
       ``ANTHROPIC_API_KEY`` / ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` /
       ``OLLAMA_HOST``) — a deliberate per-run choice.
    3. ``~/.fluid/ai_config.json`` — the user's persisted choice from
       ``fluid ai setup``.  This MUST beat ambient detection (e.g.
       Ollama running on ``localhost:11434``); operators report
       confusion when ``fluid doctor`` shows ``ollama`` despite
       having configured Gemini / Anthropic / OpenAI explicitly.
    4. Keyring — exactly one saved provider key in the OS keyring.
    5. Ambient discovery — a local Ollama, then (non-interactive, nothing
       configured) the first coding-agent CLI on PATH.

    Unless the provider was named (rung 1), the saved choice's model, key and
    Ollama host apply when it is that provider's: ``GOOGLE_API_KEY=…`` plus a
    saved ``{provider: gemini, model: gemini-2.5-pro}`` reports the saved model.

    Used by ``fluid doctor`` and forge.
    """
    env = dict(environ or os.environ)

    provider_name = _provider_name_from(None, env)
    if not provider_name:
        LOG.debug("LLM readiness: no provider detected from env or config")
        return LlmReadinessCheck(
            ready=False,
            error=no_provider_message(env),
        )

    provider = BUILTIN_LLM_PROVIDERS.get(provider_name)
    if not provider:
        return LlmReadinessCheck(
            ready=False,
            provider=provider_name,
            error=f"Unknown LLM provider '{provider_name}'.",
        )

    # The saved choice's model, key and Ollama host, when it is this provider's.
    saved = _saved_choice_for(None, env, provider.name)

    # Resolve API key: env vars → config file
    # FLUID_LLM_API_KEY counts only for a provider named with FLUID_LLM_PROVIDER or
    # FLUID_FORGE_AGENT, never for one the ladder picked, the saved choice included.
    named = _provider_is_named(None, env, provider.name)
    api_key = _resolve_api_key(provider.name, env, named=named) or saved.get("api_key")
    from fluid_build.llm.coding_agent import _AGENT_SPECS

    agent = _AGENT_SPECS.get(provider.name)
    if agent is not None:
        # A coding agent is ready when its CLI is installed; its login or key is the
        # CLI's own business and is not pre-checked. Search the PATH of the
        # environment we were given, as the real call's child process would.
        if not shutil.which(agent.binary, path=env.get("PATH", "")):
            return LlmReadinessCheck(
                ready=False,
                provider=provider.name,
                model=provider.default_model,
                auth_available=False,
                error=f"The '{agent.binary}' CLI for {provider.name} is not on PATH.",
            )
        auth_ok = True
    else:
        auth_ok = (
            bool(api_key) or provider.name == "ollama" or bool(getattr(provider, "keyless", False))
        )

    if not auth_ok:
        LOG.debug("LLM readiness: provider=%s found but no API key", provider.name)
        return LlmReadinessCheck(
            ready=False,
            provider=provider.name,
            model=provider.default_model,
            auth_available=False,
            error=f"No API key found for {provider.name}. Run 'fluid ai setup'.",
        )

    model = env.get("FLUID_LLM_MODEL") or saved.get("model") or provider.default_model
    endpoint = env.get("FLUID_LLM_ENDPOINT") or provider.default_endpoint(model, env)

    # Every built-in provider except the keyless agent ones runs through
    # litellm, which is an extra since 0.28.0. A configured key is not "ready" without it.
    from fluid_build._optional import is_available

    if provider.name not in _NON_LITELLM_PROVIDERS and not is_available("litellm"):
        return LlmReadinessCheck(
            ready=False,
            provider=provider.name,
            model=model,
            endpoint=endpoint,
            auth_available=True,
            error=(
                f"{provider.name} needs litellm, which is not installed: "
                "pip install 'data-product-forge[litellm]'"
            ),
        )

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
# ``fluid_build.llm.providers.<helper>`` flow
# through to the moved functions via the module-attribute-access
# indirection pattern.
from fluid_build.llm.model_catalog import (  # noqa: E402,F401
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


_ollama_available_cache: Optional[bool] = None


def detect_ollama_available(env: Mapping[str, str]) -> bool:
    """Return ``True`` if a local Ollama instance is reachable (cached per-process)."""
    global _ollama_available_cache  # noqa: PLW0603
    if _ollama_available_cache is not None:
        return _ollama_available_cache
    try:
        resp = httpx.get(f"{ollama_daemon_url(env)}/api/version", timeout=1.0)
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


def query_ollama_models(
    env: Mapping[str, str], endpoint: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Query the Ollama daemon for downloaded models.

    The daemon is *endpoint* when given, else ``OLLAMA_HOST``, else the
    default local address: the one the chat call dials. Returns a list of
    model dicts sorted by parameter size (largest first), or an empty list
    if Ollama is unreachable.
    """
    try:
        resp = httpx.get(f"{ollama_daemon_url(env, endpoint)}/api/tags", timeout=2.0)
        resp.raise_for_status()
        models = resp.json().get("models") or []
        for m in models:
            size_str = (m.get("details") or {}).get("parameter_size", "")
            m["_param_size"] = _parse_param_size(size_str)
        models.sort(key=lambda m: m["_param_size"], reverse=True)
        return models
    except Exception:  # noqa: BLE001
        return []


def resolve_ollama_model(env: Mapping[str, str], endpoint: Optional[str] = None) -> str:
    """Return the best model the Ollama daemon has, or the static fallback."""
    models = query_ollama_models(env, endpoint)
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
        from fluid_build.llm.litellm_backend import LiteLLMProvider

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
from fluid_build.llm.model_catalog import default_model_for as _default_model

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
    from fluid_build.llm.coding_agent import get_coding_agent_provider

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
