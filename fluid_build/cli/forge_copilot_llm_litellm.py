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
import threading
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
        payload: Dict[str, Any] = {
            "model": _litellm_model_for(self.name, config.model),
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
        """
        usage = (response_json or {}).get("usage") or {}
        prompt = _coerce_nonnegative_int(usage.get("prompt_tokens"))
        completion = _coerce_nonnegative_int(usage.get("completion_tokens"))
        total = _coerce_nonnegative_int(usage.get("total_tokens")) or (prompt + completion)
        return {
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": total,
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
        """Return a sentinel URL so ``call_llm_streaming`` routes here."""
        _, payload = self.build_request(config, system_prompt, user_prompt)
        payload = dict(payload)
        payload["stream"] = True
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
        payload: Dict[str, Any] = {
            "model": _litellm_model_for(self.name, config.model),
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
        litellm = _get_litellm()
        _, payload = self.build_request(config, system_prompt, user_prompt)
        if extra_payload:
            payload.update(extra_payload)
        try:
            response = litellm.completion(**payload)
        except Exception as exc:  # noqa: BLE001 — translated to typed error
            raise CopilotGenerationError(
                "copilot_litellm_request_failed",
                f"litellm request failed: {exc}",
                suggestions=[
                    "Check the API key for the underlying provider is set / valid",
                    "Run `fluid doctor` for an environment readiness check",
                ],
            ) from exc

        response_json = _to_dict(response)
        usage = self.extract_usage(response_json)
        _cumulative_usage["input_tokens"] += usage.get("input_tokens", 0)
        _cumulative_usage["output_tokens"] += usage.get("output_tokens", 0)
        _cumulative_usage["total_tokens"] += usage.get("total_tokens", 0)
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
        litellm = _get_litellm()
        _, _, payload = self.build_streaming_request(config, system_prompt, user_prompt)
        if extra_payload:
            payload.update(extra_payload)
        try:
            stream = litellm.completion(**payload)
        except Exception as exc:  # noqa: BLE001
            raise CopilotGenerationError(
                "copilot_litellm_streaming_failed",
                f"litellm streaming failed: {exc}",
                suggestions=[
                    "Check the API key for the underlying provider is set / valid",
                    "Try again without `--stream` to isolate streaming-vs-blocking issues",
                ],
            ) from exc

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
            cached = _coerce_nonnegative_int(
                (usage_dict.get("prompt_tokens_details") or {}).get("cached_tokens")
            )
            _record_streaming_usage(
                input_tokens=prompt,
                output_tokens=completion,
                cache_read_tokens=cached,
                cache_write_tokens=0,
            )


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


_PROVIDER_CACHE: Dict[str, LiteLLMProvider] = {}


def get_litellm_provider(name: str) -> LiteLLMProvider:
    """Return the cached LiteLLMProvider for *name*; create on first use."""
    canonical = (name or "openai").strip().lower() or "openai"
    if canonical not in _PROVIDER_CACHE:
        _PROVIDER_CACHE[canonical] = LiteLLMProvider(canonical)
    return _PROVIDER_CACHE[canonical]


__all__ = [
    "LiteLLMProvider",
    "get_last_litellm_cost_usd",
    "get_litellm_provider",
]
