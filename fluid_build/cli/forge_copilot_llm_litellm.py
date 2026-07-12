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

"""LiteLLM-backed unified provider adapter — the only LLM backend.

Replaces the per-provider classes (~2,300 lines) with a single
:class:`LiteLLMProvider` that subclasses :class:`LlmProvider` and
delegates every wire-format detail to ``litellm.completion()``.
litellm normalises every supported provider's response shape to the
OpenAI shape so this adapter's ``extract_*`` methods are tiny.

Adding a new provider (Bedrock, Azure, Vertex, Groq, …) becomes zero
new code: litellm already speaks them all. The 200+ lines of
duplicated wire-format classes the legacy native backend used to
require per provider are gone — the registry resolves every name
to a :class:`LiteLLMProvider` shim.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    LlmConfig,
    LlmProvider,
    _coerce_nonnegative_int,
    _cumulative_usage,
    _prompt_cache_metrics,
    _record_prompt_cache_from_response,
    _record_streaming_usage,
)

LOG = logging.getLogger("fluid.cli.forge_copilot.litellm")

# Default cap mirrors the rest of the call_llm loop (sane fallback).
_DEFAULT_TIMEOUT_S = 120

# Rate-limit (429) observable-retry envelope. litellm exposes no native
# pre-retry callback hook (BerriAI/litellm#19806 was Closed as not-planned),
# so we adapt a thin wrapper that makes the otherwise-silent 429 wait visible
# on stderr instead of depending on a hook that doesn't exist.
#
# ``_RATE_LIMIT_MAX_ATTEMPTS`` bounds the wrapper's temporal-backoff envelope
# (total tries, first attempt included). It is deliberately aligned with the
# Router's ``num_retries=3`` and ``copilot.agents.base.RETRY_ATTEMPTS`` so the
# observable envelope stays in the same ballpark as the existing resilience.
# ``_DEFAULT_RATE_LIMIT_WAIT_S`` is used only when the provider sends no
# Retry-After hint (mirrors the Router's ``retry_after=2`` default pacing).
_RATE_LIMIT_MAX_ATTEMPTS = 3
_DEFAULT_RATE_LIMIT_WAIT_S = 2.0

# Thread-local for the most-recent litellm.completion_cost() result.
# The staged copilot pipeline reads this to feed RunCostTracker.record_call's
# ``usd_override`` kwarg so cost reporting is accurate even when the
# embedded MODEL_PRICES_USD table doesn't know the model.
_thread_local = threading.local()


# ---------------------------------------------------------------------------
# Lazy import — keeps litellm out of cold-start path
# ---------------------------------------------------------------------------


def _get_litellm() -> Any:
    """Lazy-import litellm; raise a typed error with the install hint.

    ``litellm`` is a hard dependency in ``pyproject.toml`` so this
    branch only fires in pathological environments (a partial
    installation, an interpreter mismatch). The suggestion points at
    the canonical re-install command.
    """
    try:
        import litellm  # type: ignore[import-untyped]
    except ImportError as exc:
        raise CopilotGenerationError(
            "copilot_litellm_unavailable",
            "litellm is not installed (this is a hard dependency).",
            suggestions=[
                "pip install --upgrade fluid-build",
                "Or install litellm directly: pip install litellm",
            ],
        ) from exc
    return litellm


def _translate_litellm_exception(
    litellm: Any, exc: Exception, *, streaming: bool
) -> CopilotGenerationError:
    """Map a raw litellm exception to a typed, tagged ``CopilotGenerationError``.

    The key signal is ``failure_class`` in the error context, which the
    orchestration layer (``CopilotAgent._attempt_generation_recovery``) reads to
    decide whether to offer an interactive recovery:

    * ``litellm.AuthenticationError`` (HTTP 401 — bad/expired/absent key) →
      ``failure_class="auth"``. This is the one the key-rotation flow re-prompts
      on. We match the concrete exception type rather than string-scanning the
      message (litellm's own guidance) so the classification is robust across
      providers.
    * ``litellm.PermissionDeniedError`` (HTTP 403 — key valid but lacks
      access/model entitlement) → ``failure_class="permission"``. A *new key
      won't help*, so this is tagged distinctly and does NOT trigger rotation.
    * anything else → the generic request/streaming-failed error (unchanged).

    ``failure_class="auth"`` errors are also listed as non-retryable in
    ``copilot.agents.base`` so the staged pipeline fails fast instead of burning
    three backoff attempts on a credential that cannot succeed.
    """
    # Guard with ``isinstance(x, type)``: a mocked litellm module (MagicMock)
    # exposes ``AuthenticationError`` as a Mock attribute, not a real class, and
    # ``isinstance(exc, <Mock>)`` raises TypeError. Only dispatch on genuine
    # exception classes; otherwise fall through to the generic wrap.
    auth_error = getattr(litellm, "AuthenticationError", None)
    permission_error = getattr(litellm, "PermissionDeniedError", None)
    if isinstance(auth_error, type) and isinstance(exc, auth_error):
        return CopilotGenerationError(
            "copilot_llm_auth_failed",
            f"LLM authentication failed (401): {exc}",
            suggestions=[
                "The API key appears invalid or expired — set a fresh one",
                "Run `fluid ai setup` to re-enter your key, or `fluid doctor`",
            ],
            context={"failure_class": "auth"},
        )
    if isinstance(permission_error, type) and isinstance(exc, permission_error):
        return CopilotGenerationError(
            "copilot_llm_permission_denied",
            f"LLM permission denied (403): {exc}",
            suggestions=[
                "The key is valid but lacks access to this model — a new key "
                "won't help; check the provider account's model entitlements",
                "Try a different model with `--model`, or run `fluid doctor`",
            ],
            context={"failure_class": "permission"},
        )
    event = "copilot_litellm_streaming_failed" if streaming else "copilot_litellm_request_failed"
    label = "streaming" if streaming else "request"
    tail_suggestion = (
        "Try again without `--stream` to isolate streaming-vs-blocking issues"
        if streaming
        else "Run `fluid doctor` for an environment readiness check"
    )
    return CopilotGenerationError(
        event,
        f"litellm {label} failed: {exc}",
        suggestions=[
            "Check the API key for the underlying provider is set / valid",
            tail_suggestion,
        ],
    )


# ---------------------------------------------------------------------------
# Provider-name → litellm model prefix mapping
# ---------------------------------------------------------------------------


# Default model per provider when no explicit ``--llm-model`` is set.
# Mirrors what the deleted per-provider classes used to declare on a
# ``default_model`` attribute. Keep this small + obvious so the
# fluid-side default doesn't drift from what users expect.
_DEFAULT_MODEL_BY_PROVIDER: Dict[str, str] = {
    # Matches the ``balanced`` tier in cli/llm_models.json so the
    # picker's first option lands on a current-generation flagship
    # rather than a year-old default.
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-haiku-4-5",
    "claude": "claude-haiku-4-5",
    "gemini": "gemini-2.5-flash",
    "google": "gemini-2.5-flash",
    "ollama": "gemma3:4b",
    "groq": "llama-3.1-70b-versatile",
    "bedrock": "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "azure": "gpt-4.1-mini",
    "vertex_ai": "gemini-2.5-flash",
    "mistral": "mistral-large-latest",
    "cohere": "command-r-plus",
    # GitHub Models hosts the OpenAI family (plus Llama / Mistral /
    # DeepSeek). gpt-4o-mini is a small, cheap default that sits well
    # within the GitHub Models free-tier rate limits.
    "github": "gpt-4o-mini",
}


# Most major providers — litellm uses ``<provider>/<model>``.
_LITELLM_PREFIX_BY_PROVIDER: Dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
    "groq": "groq",
    "bedrock": "bedrock",
    "azure": "azure",
    "vertex": "vertex_ai",
    "vertex_ai": "vertex_ai",
    "mistral": "mistral",
    "cohere": "cohere",
    # Ollama uses ``ollama/<model>`` with api_base; treated specially below.
    "ollama": "ollama",
    # GitHub Models — litellm routes ``github/<model>`` to the GitHub
    # Models inference API, authenticating with GITHUB_API_KEY.
    "github": "github",
}


def _litellm_model_for(provider_name: str, model: str) -> str:
    """Translate fluid's (provider, model) pair into litellm's ``<prefix>/<model>``.

    Honours ``FLUID_LITELLM_MODEL_PREFIX`` for unusual providers
    (azure_us_gov, sagemaker, …) so users don't need a code change to
    address a niche backend.
    """
    override = os.environ.get("FLUID_LITELLM_MODEL_PREFIX", "").strip()
    if override:
        return f"{override}/{model}"
    prefix = _LITELLM_PREFIX_BY_PROVIDER.get(provider_name.lower(), provider_name.lower())
    return f"{prefix}/{model}"


def _is_anthropic_model(model_id: str) -> bool:
    """True when *model_id* targets an Anthropic Claude model.

    Anthropic prompt caching (and litellm's
    ``cache_control_injection_points`` auto-injection) only applies to
    Claude on the Anthropic / Bedrock / Vertex backends. Detection
    matches the bare model id (``claude-...``), the ``anthropic/``
    litellm prefix, and the Bedrock / Vertex Anthropic SKU shapes
    (``anthropic.claude-...`` / ``claude-...@...``).
    """
    if not model_id:
        return False
    lower = model_id.lower()
    if lower.startswith("anthropic/") or lower.startswith("anthropic."):
        return True
    return "claude" in lower


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class LiteLLMProvider(LlmProvider):
    """Single-class drop-in for any provider litellm supports.

    Implements :class:`LlmProvider` with shapes that match the existing
    contract: usage dict has ``input_tokens`` / ``output_tokens`` /
    ``total_tokens``; tool-call list has ``{id, name, arguments}``;
    streaming yields plain text deltas. Every method is a thin wrapper
    around litellm's normalised OpenAI-shape response.
    """

    def __init__(self, provider_name: str, *, default_model: str = ""):
        # ``provider_name`` is the fluid-side name (openai/anthropic/…);
        # litellm sees ``<prefix>/<model>`` derived in _litellm_model_for.
        self.name = provider_name.lower()
        # Default model — caller may pass an explicit one; otherwise fall
        # back to a sane provider-specific default. The shim factory in
        # ``forge_copilot_llm_providers`` always passes its own default
        # so this branch only fires for ad-hoc instantiation.
        self.default_model = default_model or _DEFAULT_MODEL_BY_PROVIDER.get(self.name, "")

    # ----- LlmProvider abstract API ------------------------------------

    def default_endpoint(self, model: str, env: Mapping[str, str]) -> str:
        """litellm owns auth/endpoints; return a sentinel for telemetry."""
        return f"litellm://{self.name}/{model}"

    def build_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """Return ``({}, payload_kwargs)``.

        Headers stay empty — litellm owns auth. Payload is the kwargs
        blob ``invoke_blocking`` will hand to ``litellm.completion``.
        Routing in ``call_llm`` short-circuits this provider before the
        usual ``httpx.post`` path runs, so the "headers" return is
        cosmetic for telemetry only.
        """
        model_id = _litellm_model_for(self.name, config.model)
        payload: Dict[str, Any] = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": getattr(config, "temperature", 0.0) or 0.0,
            "timeout": _DEFAULT_TIMEOUT_S,
        }
        max_tokens = getattr(config, "max_tokens", None)
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        if config.api_key:
            payload["api_key"] = config.api_key
        # Ollama: litellm needs the daemon endpoint as api_base.
        if self.name == "ollama":
            payload["api_base"] = config.endpoint or os.environ.get(
                "OLLAMA_HOST", "http://localhost:11434"
            )
        _maybe_inject_cache_control(payload, model_id)
        return ({}, payload)

    def extract_text(self, response_json: Dict[str, Any]) -> str:
        """Pull the first choice's content (litellm normalises to OpenAI shape)."""
        try:
            return ((response_json or {}).get("choices") or [{}])[0].get("message", {}).get(
                "content", ""
            ) or ""
        except Exception:  # noqa: BLE001 — malformed payload → empty
            return ""

    def extract_usage(self, response_json: Dict[str, Any]) -> Dict[str, int]:
        """Pull token counts in canonical fluid shape.

        litellm always populates ``usage.prompt_tokens`` and
        ``usage.completion_tokens`` — even providers that natively use
        a different field name (Gemini's ``promptTokenCount``,
        Anthropic's ``input_tokens``) get normalised on the way out.

        Anthropic prompt-caching adds two top-level fields on the usage
        object (per litellm GH issue #15056 and the prompt-caching docs):

        * ``cache_creation_input_tokens`` — tokens written into the
          ephemeral cache on THIS call (billed at 1.25x input).
        * ``cache_read_input_tokens`` — tokens served from a previous
          cache write (billed at 0.1x input).

        We surface both in the canonical dict so cost.py can apply the
        split rate when ``usd_override`` isn't available.
        """
        usage = (response_json or {}).get("usage") or {}
        prompt = _coerce_nonnegative_int(usage.get("prompt_tokens"))
        completion = _coerce_nonnegative_int(usage.get("completion_tokens"))
        total = _coerce_nonnegative_int(usage.get("total_tokens")) or (prompt + completion)
        cache_creation = _coerce_nonnegative_int(usage.get("cache_creation_input_tokens"))
        cache_read = _coerce_nonnegative_int(usage.get("cache_read_input_tokens"))
        return {
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": total,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        }

    def extract_prompt_cache(self, response_json: Dict[str, Any]) -> Dict[str, Any]:
        """Pull prompt-cache hit-rate via litellm's normalised ``cached_tokens``.

        litellm surfaces both Anthropic's prompt-cache hits and OpenAI's
        cached prefix discount under
        ``usage.prompt_tokens_details.cached_tokens`` so we don't have
        to special-case per provider.
        """
        usage = (response_json or {}).get("usage") or {}
        details = usage.get("prompt_tokens_details") or usage.get("prompt_token_details") or {}
        cached = _coerce_nonnegative_int(details.get("cached_tokens"))
        prompt = _coerce_nonnegative_int(usage.get("prompt_tokens"))
        # Pass total_tokens as the prompt-side input total so hit_rate
        # = read / total computes correctly per the canonical shape.
        return _prompt_cache_metrics(cached, prompt)

    # ----- Live model preflight (used by resolve_llm_config) ----------

    def choose_available_model(
        self, requested_model: str, available_models: List[str]
    ) -> Optional[str]:
        """Pick a live replacement when the catalog default has drifted.

        Provider-aware: keeps the model family stable (sonnet stays
        sonnet, opus stays opus, etc.) so a stale catalog default
        gracefully maps to the newest member of the same family. Falls
        back to the first available model when no family match exists.
        Mirrors the legacy native providers' fallback behaviour so user
        experience is identical between backends.
        """
        if requested_model in available_models:
            return requested_model
        if not available_models:
            return None
        requested = requested_model.lower()
        # Anthropic family rules — sonnet/opus/haiku are stable family ids.
        if self.name in ("anthropic", "claude"):
            for family in ("sonnet", "opus", "haiku"):
                if family not in requested:
                    continue
                for model in available_models:
                    if family in model.lower():
                        return model
        # OpenAI rules — keep within the same major series (gpt-4o, gpt-4.1, …).
        if self.name == "openai":
            for prefix in ("gpt-4o", "gpt-4.1", "gpt-4", "o1", "o3", "o4"):
                if prefix not in requested:
                    continue
                for model in available_models:
                    if prefix in model.lower():
                        return model
        # Gemini rules — match major version (2.5, 2.0, 1.5, …).
        if self.name in ("gemini", "google"):
            for tag in ("2.5", "2.0", "1.5"):
                if tag not in requested:
                    continue
                for model in available_models:
                    if tag in model.lower():
                        return model
        return available_models[0]

    def list_available_models(
        self, api_key: Optional[str], env: Mapping[str, str]
    ) -> Optional[List[str]]:
        """Return the model ids litellm knows for this provider.

        Used by ``resolve_llm_config`` for model preflight. We surface
        litellm's static catalog of supported model ids — which is
        kept current upstream — so a stale catalog default in
        fluid_build can be auto-corrected to a live name. Falls back
        to ``None`` (preflight unavailable) when litellm is missing or
        the provider isn't in its registry.
        """
        try:
            litellm = _get_litellm()
        except CopilotGenerationError:
            return None
        # Ollama: hit the local daemon for live model list. Everything
        # else: read litellm's static models_by_provider map.
        if self.name == "ollama":
            return _list_ollama_models(env)
        provider_key = _LITELLM_PREFIX_BY_PROVIDER.get(self.name, self.name)
        registry = getattr(litellm, "models_by_provider", None) or {}
        ids = registry.get(provider_key)
        if not ids:
            return None
        # Strip the ``<prefix>/`` prefix that litellm uses internally for
        # some providers so callers see the bare model id.
        prefix = f"{provider_key}/"
        return [
            (m[len(prefix) :] if isinstance(m, str) and m.startswith(prefix) else m) for m in ids
        ]

    # ----- Streaming + tool use ---------------------------------------

    def build_streaming_request(
        self, config: LlmConfig, system_prompt: str, user_prompt: str
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        """Return a sentinel URL so ``call_llm_streaming`` routes here.

        Sets ``stream_options={"include_usage": True}`` so OpenAI / Gemini
        (which only emit usage on the final chunk when explicitly asked)
        surface token counts to ``invoke_streaming``. Without this the
        terminal chunk's ``usage`` block is ``None`` and the
        ``RunCostTracker`` records nothing — the headline H1 bug. Anthropic
        / Bedrock include usage on every chunk regardless, so the option
        is a no-op for them. litellm passes the kwarg straight through
        to the underlying provider without translation.
        """
        _, payload = self.build_request(config, system_prompt, user_prompt)
        payload = dict(payload)
        payload["stream"] = True
        payload.setdefault("stream_options", {"include_usage": True})
        return ("litellm://internal", {}, payload)

    def iter_stream_chunks(self, response: Any) -> Iterator[str]:
        """Streaming is owned by ``invoke_streaming`` — never hits httpx."""
        return
        yield  # pragma: no cover — makes this a generator

    def build_tool_request(
        self,
        config: LlmConfig,
        system_prompt: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        """Build a tool-use request; litellm normalises tools to OpenAI shape."""
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        model_id = _litellm_model_for(self.name, config.model)
        payload: Dict[str, Any] = {
            "model": model_id,
            "messages": full_messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": getattr(config, "temperature", 0.0) or 0.0,
            "timeout": _DEFAULT_TIMEOUT_S,
        }
        if config.api_key:
            payload["api_key"] = config.api_key
        if self.name == "ollama":
            payload["api_base"] = config.endpoint or os.environ.get(
                "OLLAMA_HOST", "http://localhost:11434"
            )
        _maybe_inject_cache_control(payload, model_id)
        return ("litellm://internal", {}, payload)

    def extract_tool_calls(self, response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Pull tool calls from litellm's normalised OpenAI shape.

        Always ``json.loads`` the arguments string so callers see a real
        dict — matches the legacy provider classes which all parse the
        JSON server-side. Malformed JSON falls through as an empty dict
        rather than raising, since the corrective-feedback channel
        will tell the LLM to retry the call.
        """
        try:
            choices = (response_json or {}).get("choices") or []
            if not choices:
                return []
            tool_calls = (choices[0].get("message") or {}).get("tool_calls") or []
        except Exception:  # noqa: BLE001
            return []
        out: List[Dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") or tc.get("name") or ""
            args_raw = fn.get("arguments") or tc.get("arguments") or "{}"
            if isinstance(args_raw, str):
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}
            out.append({"id": tc.get("id") or "", "name": name, "arguments": args})
        return out

    def extract_text_from_tool_response(self, response_json: Dict[str, Any]) -> Optional[str]:
        """Final text emerges in the same place as a non-tool response."""
        text = self.extract_text(response_json)
        return text or None

    def build_tool_result_messages(
        self,
        tool_calls: List[Dict[str, Any]],
        tool_results: List[Any],
    ) -> List[Dict[str, Any]]:
        """OpenAI-shape tool result envelopes (litellm passes through)."""
        msgs: List[Dict[str, Any]] = []
        # First the assistant's tool_calls envelope so the chain follows
        # the OpenAI tool-use protocol.
        msgs.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(tc.get("input") or tc.get("arguments") or {}),
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc, result in zip(tool_calls, tool_results, strict=False):
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "content": (
                        result if isinstance(result, str) else json.dumps(result, default=str)
                    ),
                }
            )
        return msgs

    # ----- Direct invocation (called by call_llm short-circuit) -------

    def invoke_blocking(
        self,
        config: LlmConfig,
        system_prompt: str,
        user_prompt: str,
        *,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Run a single completion through litellm; return the response text.

        ``extra_payload`` carries provider-agnostic structured-output
        directives that the agent layer's ``_inject_provider_schema``
        builds — most importantly ``response_format`` for JSON-schema
        constrained output. Without this passthrough the agent's schema
        injection is silently dropped because the legacy code path
        used to mutate the same dict that built the request.

        Side effects:
        * appends usage to the module-level ``_cumulative_usage`` so the
          cost summary downstream sees the spend;
        * stashes ``litellm.completion_cost`` on a thread-local for the
          staged pipeline's optional ``usd_override`` recording;
        * feeds prompt-cache metrics through the canonical recorder.
        """
        # H1 — clear per-call thread-local USD + cache token state at
        # the start so the bridge in ``call_llm`` doesn't attribute the
        # PREVIOUS call's stash to this one.
        _reset_thread_local_cost_state()
        litellm = _get_litellm()
        _, payload = self.build_request(config, system_prompt, user_prompt)
        if extra_payload:
            payload.update(extra_payload)
        try:
            response = _completion_with_rate_limit_notice(litellm, payload)
        except Exception as exc:  # noqa: BLE001 — translated to typed error
            raise _translate_litellm_exception(litellm, exc, streaming=False) from exc

        response_json = _to_dict(response)
        usage = self.extract_usage(response_json)
        _cumulative_usage["input_tokens"] += usage.get("input_tokens", 0)
        _cumulative_usage["output_tokens"] += usage.get("output_tokens", 0)
        _cumulative_usage["total_tokens"] += usage.get("total_tokens", 0)
        # Stash cache-token counts on the thread-local so the staged
        # pipeline can hand them to RunCostTracker.record_call alongside
        # the usd_override. Anthropic prompt caching is the load-bearing
        # case — provider-neutral so the same plumbing covers Vertex
        # Claude / Bedrock Claude with zero per-backend wiring.
        _stash_cache_tokens(
            cache_creation=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_read=int(usage.get("cache_read_input_tokens", 0) or 0),
        )
        _record_prompt_cache_from_response(self, response_json)
        _record_completion_cost(litellm, response)
        return self.extract_text(response_json)

    def invoke_streaming(
        self,
        config: LlmConfig,
        system_prompt: str,
        user_prompt: str,
        *,
        extra_payload: Optional[Dict[str, Any]] = None,
    ) -> Iterator[str]:
        """Stream a completion through litellm, yielding text deltas.

        ``extra_payload`` carries the same structured-output directives
        as ``invoke_blocking`` so streaming agents see schema-constrained
        output too.

        Mirrors the legacy ``iter_stream_chunks`` contract: yields
        plain text strings, then writes final usage into
        ``_streaming_usage_state`` via ``_record_streaming_usage`` so
        ``consume_streaming_usage()`` returns the dict afterwards.
        """
        # H1 — clear per-call thread-local state (see invoke_blocking).
        _reset_thread_local_cost_state()
        litellm = _get_litellm()
        _, _, payload = self.build_streaming_request(config, system_prompt, user_prompt)
        if extra_payload:
            payload.update(extra_payload)
        try:
            stream = _completion_with_rate_limit_notice(litellm, payload)
        except Exception as exc:  # noqa: BLE001
            raise _translate_litellm_exception(litellm, exc, streaming=True) from exc

        last_chunk: Any = None
        for chunk in stream:
            last_chunk = chunk
            chunk_dict = _to_dict(chunk)
            try:
                delta = (chunk_dict.get("choices") or [{}])[0].get("delta", {}).get("content", "")
            except Exception:  # noqa: BLE001
                delta = ""
            if delta:
                yield delta

        # litellm exposes final usage on the closing chunk for some
        # providers and on the response object for others. Try both.
        usage_dict: Dict[str, Any] = {}
        for source in (last_chunk, getattr(stream, "_response", None)):
            if source is None:
                continue
            try:
                payload_dict = _to_dict(source)
            except Exception:  # noqa: BLE001
                continue
            usage_dict = payload_dict.get("usage") or {}
            if usage_dict:
                break
        if usage_dict:
            prompt = _coerce_nonnegative_int(usage_dict.get("prompt_tokens"))
            completion = _coerce_nonnegative_int(usage_dict.get("completion_tokens"))
            # Prefer Anthropic-shape top-level cache_read_input_tokens
            # when present; fall through to OpenAI-shape
            # prompt_tokens_details.cached_tokens for non-Anthropic
            # streaming providers. Same for cache writes.
            cache_read = _coerce_nonnegative_int(
                usage_dict.get("cache_read_input_tokens")
            ) or _coerce_nonnegative_int(
                (usage_dict.get("prompt_tokens_details") or {}).get("cached_tokens")
            )
            cache_write = _coerce_nonnegative_int(usage_dict.get("cache_creation_input_tokens"))
            _record_streaming_usage(
                input_tokens=prompt,
                output_tokens=completion,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            _stash_cache_tokens(cache_creation=cache_write, cache_read=cache_read)


# ---------------------------------------------------------------------------
# Helpers + lazy cache
# ---------------------------------------------------------------------------


def _list_ollama_models(env: Mapping[str, str]) -> Optional[List[str]]:
    """Hit the local Ollama daemon for its installed model list.

    litellm doesn't ship a static Ollama catalog (model ids depend on
    what the user has pulled). The daemon's ``/api/tags`` endpoint
    returns the live list. Returns ``None`` if the daemon isn't
    reachable so preflight degrades to "unavailable" rather than
    falsely rejecting models.
    """
    base = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    try:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"{base}/api/tags")
        with urllib.request.urlopen(req, timeout=2.0) as resp:  # nosec B310 — local daemon
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — daemon down → preflight unavailable
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    return [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]


def _to_dict(obj: Any) -> Dict[str, Any]:
    """Coerce a litellm response (or stream chunk) to a plain dict."""
    if isinstance(obj, dict):
        return obj
    # litellm returns Pydantic-ish objects with .model_dump() / .dict()
    for attr in ("model_dump", "dict", "to_dict"):
        m = getattr(obj, attr, None)
        if callable(m):
            try:
                return m()
            except Exception:  # noqa: BLE001
                continue
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return {}


def _record_completion_cost(litellm: Any, response: Any) -> None:
    """Stash the per-call USD cost on a thread-local for downstream pickup."""
    fn = getattr(litellm, "completion_cost", None)
    if fn is None:
        return
    try:
        usd = fn(completion_response=response)
    except Exception as exc:  # noqa: BLE001 — never block on cost extraction
        LOG.debug("litellm_completion_cost_failed: %s", exc)
        return
    try:
        _thread_local.last_cost_usd = float(usd) if usd is not None else None
    except (TypeError, ValueError):
        _thread_local.last_cost_usd = None


def get_last_litellm_cost_usd() -> Optional[float]:
    """Read the most-recent litellm.completion_cost result.

    The staged copilot pipeline calls this after a successful request
    and passes the value as ``usd_override`` to
    ``RunCostTracker.record_call`` so the run summary reflects
    litellm's accurate price catalog.
    """
    return getattr(_thread_local, "last_cost_usd", None)


def _stash_cache_tokens(*, cache_creation: int, cache_read: int) -> None:
    """Park per-call cache token counts on the thread-local.

    Read by the staged-pipeline call site (``BaseStageAgent._call_once``)
    so the cache-write 1.25x and cache-read 0.1x prices apply in the
    Cost summary even when ``usd_override`` isn't supplied (e.g. for
    self-hosted Bedrock + Vertex Claude where the litellm catalog
    sometimes doesn't price the SKU).
    """
    _thread_local.last_cache_creation = int(cache_creation or 0)
    _thread_local.last_cache_read = int(cache_read or 0)


def get_last_cache_tokens() -> Dict[str, int]:
    """Read the most-recent cache (creation, read) counts.

    Returns zeros when nothing has been recorded yet on this thread —
    callers can pass the returned dict's values to ``record_call``
    unconditionally without first checking whether caching applied.
    """
    return {
        "cache_creation_input_tokens": int(getattr(_thread_local, "last_cache_creation", 0) or 0),
        "cache_read_input_tokens": int(getattr(_thread_local, "last_cache_read", 0) or 0),
    }


def _reset_thread_local_cost_state() -> None:
    """Wipe the per-call USD + cache-token thread-local slots.

    Called at the start of every ``invoke_blocking`` / ``invoke_streaming``
    so a follow-up call on the same thread doesn't inherit the prior
    call's stash. Without this, the H1 bridge could attribute the
    previous call's USD / cache tokens to the next one (especially
    visible when an LLM provider doesn't expose ``completion_cost`` on
    the response and the prior call's value lingers).
    """
    _thread_local.last_cost_usd = None
    _thread_local.last_cache_creation = 0
    _thread_local.last_cache_read = 0


# ---------------------------------------------------------------------------
# Router dispatch + cache-control auto-injection
# ---------------------------------------------------------------------------


def _completion_via_router_or_direct(litellm: Any, payload: Dict[str, Any]) -> Any:
    """Route through the Router singleton when applicable, else direct.

    A 5xx on the primary deployment would otherwise kill the in-flight
    run. The Router has the cooldown_time / num_retries / fallbacks
    machinery already; we just need to call it instead of
    ``litellm.completion``. Same kwargs shape, same response shape.
    """
    model_id = payload.get("model", "")
    # Lazy import to keep cold-start path off the Router code; tests
    # patch ``forge_llm_router.get_router`` to inject behaviour.
    from fluid_build.cli import forge_llm_router

    router = forge_llm_router.get_router(model_id)
    if router is not None:
        return router.completion(**payload)
    return litellm.completion(**payload)


def _is_rate_limit_error(litellm: Any, exc: Exception) -> bool:
    """True when *exc* is a litellm 429 ``RateLimitError``.

    Guarded with ``isinstance(<cls>, type)`` — a mocked litellm module
    (MagicMock) exposes ``RateLimitError`` as a Mock attribute, not a real
    class, and ``isinstance(exc, <Mock>)`` raises ``TypeError``. Matching the
    concrete exception type (litellm's own guidance) keeps the classification
    robust across providers, exactly like ``_translate_litellm_exception``.
    """
    rate_limit_error = getattr(litellm, "RateLimitError", None)
    return isinstance(rate_limit_error, type) and isinstance(exc, rate_limit_error)


def _resolve_rate_limit_wait(exc: Exception) -> float:
    """Derive the retry wait (seconds) from a rate-limit exception.

    Prefers the server-supplied ``Retry-After`` — litellm surfaces it on the
    exception as ``retry_after``; some providers only put it in the response
    headers, so that is the secondary source. Parsing/clamping is delegated to
    :func:`copilot.agents.error_classification.parse_retry_after` (reused, not
    reinvented). Falls back to ``_DEFAULT_RATE_LIMIT_WAIT_S`` when no usable
    hint is present.
    """
    from fluid_build.copilot.agents.error_classification import parse_retry_after

    seconds = parse_retry_after(getattr(exc, "retry_after", None))
    if seconds is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        if callable(getter):
            try:
                seconds = parse_retry_after(getter("retry-after"))
            except Exception:  # noqa: BLE001 — header lookup is best-effort
                seconds = None
    if seconds is None or seconds <= 0:
        return _DEFAULT_RATE_LIMIT_WAIT_S
    return seconds


def _emit_rate_limit_notice(wait_s: float) -> None:
    """Print the user-facing rate-limit notice to stderr.

    stderr keeps this off the machine-readable stdout stream (repo convention:
    status lines go to stderr, machine output to stdout). Emitted *before* the
    wait so the user understands why the spinner paused rather than staring at
    a frozen one. ``:g`` renders ``2.0`` as ``2`` and ``1.5`` as ``1.5``.
    ``sys.stderr.write`` (not ``print``) mirrors the repo's status-line
    convention (see ``cli/bundle.py``) and is flushed so the notice lands
    before the blocking wait rather than buffering behind it.
    """
    sys.stderr.write(f"Rate limited. Waiting {wait_s:g}s before retrying...\n")
    sys.stderr.flush()
    LOG.info("llm_rate_limited_retry_wait_seconds=%s", wait_s)


def _completion_with_rate_limit_notice(litellm: Any, payload: Dict[str, Any]) -> Any:
    """Run the completion, surfacing a notice before each 429 retry wait.

    This is the *single* observable retry envelope for the litellm direct
    path — both ``invoke_blocking`` and ``invoke_streaming`` route through it,
    so the notice is emitted in exactly one place (no double-printing).

    The Router's own ``num_retries`` / ``retry_after`` govern cross-cloud
    deployment failover (a distinct resilience axis) and are left untouched;
    this wrapper adds only the temporal, user-visible backoff that a rate
    limit warrants. Success semantics are unchanged: a call that would return
    still returns, and an exhausted 429 still raises the same underlying
    ``RateLimitError`` for ``_translate_litellm_exception`` to wrap.
    """
    for attempt in range(1, _RATE_LIMIT_MAX_ATTEMPTS + 1):
        try:
            return _completion_via_router_or_direct(litellm, payload)
        except Exception as exc:  # noqa: BLE001 — re-raised unless a retryable 429
            if attempt >= _RATE_LIMIT_MAX_ATTEMPTS or not _is_rate_limit_error(litellm, exc):
                raise
            wait_s = _resolve_rate_limit_wait(exc)
            _emit_rate_limit_notice(wait_s)
            time.sleep(wait_s)
    # Unreachable: the loop always returns a response or re-raises.
    raise AssertionError("rate-limit retry loop exited without a result")  # pragma: no cover


# litellm's auto-inject parameter shape per
# https://docs.litellm.ai/docs/tutorials/prompt_caching — each entry
# names a message position via location/role/index. We target the
# single system message at index 0 (standard fluid-side prompt shape).
_CACHE_CONTROL_INJECTION_SYSTEM: List[Dict[str, Any]] = [
    {"location": "message", "role": "system", "index": 0},
]


def _maybe_inject_cache_control(payload: Dict[str, Any], model_id: str) -> None:
    """Add ``cache_control_injection_points`` for Anthropic models only.

    No-op for OpenAI / Gemini / Groq / Cohere etc — their providers
    either don't support cache_control or use a different mechanism.
    Injecting the param against a non-Anthropic backend would either
    be silently dropped (best case) or raise (worst case).
    """
    if not _is_anthropic_model(model_id):
        return
    # Don't clobber a caller-supplied value — the agent layer may have
    # already set explicit injection points (multi-turn caching, etc).
    payload.setdefault("cache_control_injection_points", _CACHE_CONTROL_INJECTION_SYSTEM)


_PROVIDER_CACHE: Dict[str, LiteLLMProvider] = {}


def get_litellm_provider(name: str) -> LiteLLMProvider:
    """Return the cached LiteLLMProvider for *name*; create on first use."""
    canonical = (name or "openai").strip().lower() or "openai"
    if canonical not in _PROVIDER_CACHE:
        _PROVIDER_CACHE[canonical] = LiteLLMProvider(canonical)
    return _PROVIDER_CACHE[canonical]


__all__ = [
    "LiteLLMProvider",
    "get_last_cache_tokens",
    "get_last_litellm_cost_usd",
    "get_litellm_provider",
]
