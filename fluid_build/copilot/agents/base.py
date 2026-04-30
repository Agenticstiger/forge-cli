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

"""Base infrastructure for staged agents."""

from __future__ import annotations

import dataclasses
import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Type, TypeVar

import httpx
from pydantic import ValidationError

from fluid_build.cli.forge_copilot_llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    LlmConfig,
    LlmProvider,
    get_catalog_tier_model,
)
from fluid_build.copilot.agents.errors import (
    AgentExecutionError,
    ContextOverflowError,
    ProviderAuthError,
    ProviderError,
    RateLimitError,
    SchemaValidationError,
)
from fluid_build.copilot.industry.pack import IndustryPack
from fluid_build.copilot.schemas.stage_outputs import StructuredOutputModel
from fluid_build.copilot.store.base import Store
from fluid_build.copilot.store.keys import generate_cache_key
from fluid_build.copilot.store.policy import default_ttl_for_namespace
from fluid_build.copilot.utils.json import safe_json_parse
from fluid_build.schema_manager import FluidSchemaManager

StageOutputT = TypeVar("StageOutputT", bound=StructuredOutputModel)
_T = TypeVar("_T")

# Default retry envelope applied to every staged LLM call. Matches the
# semantics of tenacity's
# ``@retry(stop_after_attempt(3), wait_exponential(multiplier=1, max=8))``
# with a small uniform jitter — port of the cherry-pick convention from
# ``fluid_ddl_workflow.py:42-46`` without introducing ``tenacity`` as a
# dependency.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 8.0
RETRY_JITTER = 0.1

# Errors that are pointless to retry — re-issuing the same request will
# produce the same failure. The agent loop must compact the prompt
# (ContextOverflow), surface to the user (Auth), or send corrective
# feedback to the LLM (Schema), not retry blindly.
_NON_RETRYABLE_ERRORS = (
    ContextOverflowError,
    ProviderAuthError,
    SchemaValidationError,
)


def retry_with_backoff(
    func: Callable[[], _T],
    *,
    attempts: int = RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY,
    max_delay: float = RETRY_MAX_DELAY,
    jitter: float = RETRY_JITTER,
    sleep: Callable[[float], None] = time.sleep,
    retry_if: Optional[Callable[[Exception], bool]] = None,
) -> _T:
    """Call ``func`` with typed-error-aware retry.

    Three attempts by default, delays ``base_delay * 2**(n-1)`` capped at
    ``max_delay`` with up to ``jitter * delay`` extra uniform noise.
    ``sleep`` is injectable so tests can stub it out without patching
    :mod:`time`.

    Two behaviour upgrades over a generic exponential-backoff loop:

    1. **Non-retryable errors fail fast.** :class:`ContextOverflowError`,
       :class:`ProviderAuthError`, and :class:`SchemaValidationError`
       guarantee the same failure on retry — re-raising immediately
       saves credits and surfaces the underlying problem faster.

    2. **Retry-After is honored.** When a :class:`ProviderError` carries
       a server-supplied ``retry_after`` (parsed from the HTTP header),
       we sleep for that duration instead of the default exponential
       delay. This prevents hammering rate-limited endpoints into
       longer cool-downs.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            # Honor the explicit retry_if predicate first when callers
            # need stage-specific behaviour (e.g. the staged call() path
            # disables retry for schema errors so they surface a repair
            # event instead of being retried opaquely).
            if retry_if is not None and not retry_if(exc):
                raise
            # Fail fast on errors that cannot be helped by retry.
            if isinstance(exc, _NON_RETRYABLE_ERRORS):
                raise
            last_error = exc
            if attempt == attempts:
                break
            # Provider-supplied Retry-After wins over our default
            # exponential delay so we respect the upstream's pacing.
            retry_after = getattr(exc, "retry_after", None) if isinstance(
                exc, ProviderError
            ) else None
            if retry_after is not None and retry_after > 0:
                delay = float(retry_after)
            else:
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                if jitter:
                    delay += random.uniform(0, jitter * delay)
            sleep(delay)
    assert last_error is not None
    raise last_error


@dataclass
class StageSession:
    """Shared runtime state for staged agent calls."""

    store: Store
    workspace_root: Path = field(default_factory=Path.cwd)
    llm_config: Optional[LlmConfig] = None
    active_provider: Optional[str] = None
    tiered: bool = False
    no_cache: bool = False
    cache_ttl: Optional[int] = None
    fluid_version: str = field(default_factory=FluidSchemaManager.latest_bundled_version)
    capability_matrix: Dict[str, Any] = field(default_factory=dict)
    project_memory: Optional[Dict[str, Any]] = None
    team_memory: Optional[Dict[str, Any]] = None
    personal_memory: Optional[Dict[str, Any]] = None
    discovery_report: Any = None
    industry_pack: Optional[IndustryPack] = None
    require_llm: bool = False
    fallback_used: bool = False
    fallback_events: list[Dict[str, str]] = field(default_factory=list)
    scratchpad: Any = None
    """Shared inter-agent scratchpad (Missing #1).

    Lazily created on first access via :meth:`get_scratchpad` so
    tests / callers that don't use it don't pay the import cost.
    Stage agents (Logical, Builder, Critic, Validator) read / write
    typed slots: critic findings, RAG retrievals, structured stage
    feedback. See :class:`fluid_build.copilot.scratchpad.Scratchpad`."""
    repair_used: bool = False
    repair_events: list[Dict[str, str]] = field(default_factory=list)
    agent_events: list[Dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.active_provider is None and self.llm_config is not None:
            self.active_provider = self.llm_config.provider

    def get_scratchpad(self):
        """Return the per-session :class:`Scratchpad`, creating it
        on first access. Lazy so importers that never touch the
        scratchpad don't pay the module-load cost."""
        if self.scratchpad is None:
            from fluid_build.copilot.scratchpad import Scratchpad

            self.scratchpad = Scratchpad()
        return self.scratchpad

    def record_fallback(
        self,
        *,
        stage: str,
        reason: str,
        error_type: str = "",
    ) -> None:
        """Record an intentional non-strict fallback for run-level evidence."""
        self.fallback_used = True
        self.fallback_events.append(
            {
                "stage": stage,
                "reason": reason,
                "error_type": error_type,
            }
        )

    def record_repair(
        self,
        *,
        stage: str,
        reason: str,
        error_type: str = "",
        detail: str = "",
    ) -> None:
        """Record an LLM output repair that stayed on the agentic path."""
        event = {
            "stage": stage,
            "reason": reason,
            "error_type": error_type,
        }
        if detail:
            event["detail"] = detail[:500]
        self.repair_used = True
        self.repair_events.append(event)

    def record_agent_event(
        self,
        *,
        stage: str,
        agent: str,
        mode: str,
        status: str = "completed",
        tier: str = "",
        model: str = "",
        notes: str = "",
    ) -> None:
        """Record the accountable agent that owned a pipeline stage."""
        event = {
            "stage": stage,
            "agent": agent,
            "mode": mode,
            "status": status,
        }
        if tier:
            event["tier"] = tier
        if model:
            event["model"] = model
        if notes:
            event["notes"] = notes
        if event not in self.agent_events:
            self.agent_events.append(event)


class BaseStageAgent:
    """Shared LLM + cache plumbing for staged agents."""

    def __init__(self, *, stage: str, tier: str) -> None:
        self.stage = stage
        self.tier = tier

    def resolve_model(self, session: StageSession) -> Optional[LlmConfig]:
        """Resolve the per-stage config, respecting single-model and tiered modes."""
        if session.llm_config is None:
            return None
        if not session.tiered:
            return session.llm_config
        provider_name = session.active_provider or session.llm_config.provider
        tier_model = (
            session.llm_config.tier_models.get(self.tier)
            or get_catalog_tier_model(provider_name, self.tier)
            or session.llm_config.model
        )
        provider = BUILTIN_LLM_PROVIDERS[provider_name]
        endpoint = provider.default_endpoint(tier_model, dict(os.environ))
        return dataclasses.replace(session.llm_config, model=tier_model, endpoint=endpoint)

    def cache_namespace(self) -> str:
        return f"llm/{self.stage}"

    def call(
        self,
        session: StageSession,
        *,
        system_prompt: str,
        user_prompt: str,
        output_schema: Type[StageOutputT],
        params: Optional[Mapping[str, Any]] = None,
        retry_schema_errors: bool = True,
    ) -> StageOutputT:
        """Call the configured provider and parse a structured stage output.

        Wrapped in :func:`retry_with_backoff` — three attempts with
        exponential backoff, matching the cherry-pick retry envelope
        shared with the rest of the staged flow.
        """
        retry_if: Optional[Callable[[Exception], bool]] = None
        if not retry_schema_errors:

            def _retry_non_schema_errors(exc: Exception) -> bool:
                return not isinstance(exc, (ValidationError, json.JSONDecodeError))

            retry_if = _retry_non_schema_errors
        return retry_with_backoff(
            lambda: self._call_once(
                session,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                output_schema=output_schema,
                params=params,
            ),
            retry_if=retry_if,
        )

    def _call_once(
        self,
        session: StageSession,
        *,
        system_prompt: str,
        user_prompt: str,
        output_schema: Type[StageOutputT],
        params: Optional[Mapping[str, Any]] = None,
    ) -> StageOutputT:
        """Execute one provider call attempt."""
        config = self.resolve_model(session)
        if config is None:
            # Surface as ``AgentExecutionError`` so callers that wrap
            # the staged loop in ``except AgentExecutionError`` (the
            # documented path in the typed-exception hierarchy) catch
            # this misconfiguration alongside provider/parse failures
            # rather than having to also handle a bare ``RuntimeError``.
            raise AgentExecutionError("No LLM configuration available for staged agent call")
        provider_name = session.active_provider or config.provider
        provider: LlmProvider = BUILTIN_LLM_PROVIDERS[provider_name]
        if provider.name != config.provider:
            # Provider leak is the safety net that enforces the
            # one-provider-per-run rule from the plan; raising the
            # typed exception keeps the failure inside
            # ``AgentExecutionError`` so external orchestrators see a
            # single error class for "stage couldn't run".
            raise AgentExecutionError(f"Provider leak: {provider.name} != {config.provider}")

        prompt_blob = f"{system_prompt}\n\n{user_prompt}"
        # Pass ``session.capability_matrix`` as a separate hash
        # segment so flipping a capability flag (extended-thinking
        # budget, prompt-cache mode, structured-output strictness)
        # invalidates the cache cleanly. Keeping it out of ``params``
        # avoids collisions with stage-specific param keys.
        cache_key = generate_cache_key(
            config.model,
            prompt_blob,
            params or {},
            capability_matrix=session.capability_matrix or None,
        )
        cache_ttl = session.cache_ttl or default_ttl_for_namespace(self.cache_namespace())
        if not session.no_cache:
            cached = session.store.get(self.cache_namespace(), cache_key)
            if cached is not None:
                return output_schema.model_validate(cached.value)

        headers, payload = provider.build_request(config, system_prompt, user_prompt)
        self._inject_provider_schema(provider.name, payload, output_schema)
        # Wrap the network + parse path in a lightweight "thinking" status
        # panel so users can see which agent is active without staring at a
        # silent prompt. The panel self-disables on non-TTY, ``FLUID_QUIET``,
        # or when rich is unavailable — import locally so we don't pay for
        # rich on every import of this module.
        from fluid_build.cli.progress import AgentStatus

        # Item 2 + Item 3 — streaming auto-detect.
        # An explicit capability flag wins; otherwise default to ON
        # when stdout is interactive AND not in quiet mode. CI
        # runners (non-TTY stdout) keep the blocking path so test
        # fleets and pipelines stay deterministic.
        cm = session.capability_matrix or {}
        explicit = cm.get("streaming_enabled")
        if explicit is not None:
            streaming_enabled = bool(explicit)
        else:
            import sys as _sys

            quiet_env = os.environ.get("FLUID_QUIET") == "1"
            quiet_cap = bool(cm.get("quiet"))
            try:
                tty = bool(_sys.stdout.isatty())
            except Exception:  # pragma: no cover — defensive
                tty = False
            streaming_enabled = tty and not (quiet_env or quiet_cap)

        # All httpx + parse failures are funneled through
        # ``classify_provider_error`` / ``SchemaValidationError`` so
        # ``retry_with_backoff`` can branch on operationally distinct
        # failure modes (rate-limit honors Retry-After; context-overflow
        # fails fast; auth surfaces immediately; schema errors route
        # corrective feedback back to the LLM at the agent-loop layer).
        from fluid_build.copilot.agents.error_classification import (
            classify_provider_error,
        )

        with AgentStatus(
            stage=self.stage,
            agent=type(self).__name__,
            provider=provider.name,
            model=config.model,
        ):
            try:
                if streaming_enabled:
                    from fluid_build.copilot.streaming import (
                        NullStreamHandler,
                        StreamingCall,
                    )

                    stream_url, stream_headers, stream_payload = provider.build_streaming_request(
                        config,
                        system_prompt,
                        user_prompt,
                    )
                    self._inject_provider_schema(
                        provider.name, stream_payload, output_schema
                    )
                    with httpx.stream(
                        "POST",
                        stream_url,
                        headers=stream_headers,
                        json=stream_payload,
                        timeout=config.timeout_seconds,
                    ) as response:
                        response.raise_for_status()
                        handler = cm.get("stream_handler") or NullStreamHandler()
                        with StreamingCall(
                            provider.iter_stream_chunks(response),
                            handler,
                        ) as call:
                            for _chunk in call:
                                pass
                        raw = call.full_text
                    # Streaming providers don't return a usage block on
                    # the SSE wire (provider-specific); we fall back to
                    # an empty raw_response so the cost tracker records
                    # the call as missing-usage rather than crashing.
                    raw_response = {}
                    parsed = safe_json_parse(raw)
                else:
                    response = httpx.post(
                        config.endpoint,
                        headers=headers,
                        json=payload,
                        timeout=config.timeout_seconds,
                    )
                    response.raise_for_status()
                    raw_response = response.json()
                    raw = provider.extract_text(raw_response)
                    parsed = safe_json_parse(raw)
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                raise classify_provider_error(exc, provider=provider.name) from exc
            try:
                result = output_schema.model_validate(parsed)
            except ValidationError as exc:
                raise SchemaValidationError(
                    f"Stage '{self.stage}' output failed schema validation",
                    schema_name=output_schema.__name__,
                    validation_errors=list(exc.errors()),
                    raw_output=raw if isinstance(raw, str) else "",
                ) from exc
        # Record this call's token usage with the run-level cost
        # tracker so ``fluid forge data-model`` can print a per-run
        # cost summary at the end. Two failure modes to guard against:
        #
        # 1. ``extract_usage`` raises — provider's usage extractor
        #    blew up. Mark the call as "missing usage" so the user
        #    sees "N calls had no usage data; cost may be
        #    under-reported" in the summary footer.
        # 2. ``extract_usage`` returns empty / 0,0 — handled inside
        #    ``record_call`` (it auto-flags the call as missing usage
        #    when both token counts are zero on a non-Ollama
        #    provider).
        from fluid_build.copilot.cost import get_run_tracker

        try:
            usage = provider.extract_usage(raw_response) or {}
        except Exception:  # pragma: no cover — defensive
            usage = None
        if usage is None:
            get_run_tracker().record_missing_usage()
        else:
            # Missing-#5 attribution — pass stage + concrete class
            # name so the cost summary's per-agent table tells
            # operators WHICH agent drove the cost (not just which
            # model was billed).
            get_run_tracker().record_call(
                provider=provider.name,
                model=config.model,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                stage=self.stage,
                agent_class=type(self).__name__,
            )
        # Sprint #6 — enforce the cost ceiling (if any). Runs
        # AFTER the call's tokens are recorded so the running
        # total reflects this call. ``CostLimitExceeded`` is a
        # ``RuntimeError`` subclass that propagates out of the
        # staged pipeline; the forge aborts with a precise
        # error message naming the limit and current total.
        from fluid_build.copilot.cost import check_cost_ceiling

        check_cost_ceiling()
        if not session.no_cache:
            session.store.put(
                self.cache_namespace(),
                cache_key,
                result.model_dump(mode="json", by_alias=True),
                ttl=cache_ttl,
                metadata={"model": config.model, "stage": self.stage},
                fluid_version=session.fluid_version,
            )
        return result

    def _inject_provider_schema(
        self,
        provider_name: str,
        payload: Dict[str, Any],
        output_schema: Type[StageOutputT],
    ) -> None:
        if provider_name in {"openai", "azure-openai"}:
            payload["response_format"] = output_schema.to_openai_json_schema()
            return
        if provider_name in {"anthropic", "claude"}:
            tool = output_schema.to_anthropic_tool()
            payload["tools"] = [tool]
            payload["tool_choice"] = {"type": "tool", "name": tool["name"]}
            return
        if provider_name == "gemini":
            generation_config = payload.setdefault("generationConfig", {})
            generation_config["responseMimeType"] = "application/json"
            # LogicalDraft is too large for Gemini's responseSchema state
            # budget, especially on 2.5 Pro. Keep JSON mode by default and
            # leave schema forcing behind an explicit debug opt-in.
            if os.environ.get("FLUID_GEMINI_RESPONSE_SCHEMA", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }:
                generation_config.update(output_schema.to_gemini_config())
            return
        if provider_name == "ollama":
            payload["response_format"] = {"type": "json_object"}
