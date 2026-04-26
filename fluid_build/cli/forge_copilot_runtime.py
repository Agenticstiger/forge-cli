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

"""Runtime support for LLM-backed forge copilot generation.

This module is the public orchestration surface for the copilot flow.
Low-level helpers live in dedicated sub-modules and are re-exported here
so that ``from forge_copilot_runtime import X`` keeps working everywhere.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from fluid_build.cli._common import redact_secrets, resolve_provider_from_contract

# ---------------------------------------------------------------------------
# Re-exports: contract helpers
# ---------------------------------------------------------------------------
from fluid_build.cli.forge_copilot_contract_helpers import (  # noqa: F401
    KNOWN_BUILD_ENGINES,
    PROVIDER_ENGINE_COMPATIBILITY,
    TEMPLATE_ALIASES,
    _build_semantics_from_interview_summary,
    _coerce_string_list,
    _normalize_consumes_for_generation,
    _normalize_interview_summary,
    build_structured_repair_feedback,
    classify_generation_failure,
    extract_json_object,
    normalize_provider_name,
    normalize_template_name,
    sanitize_additional_files,
    sanitize_name,
)

# These need thin wrappers below because they inject dependencies:
from fluid_build.cli.forge_copilot_contract_helpers import (
    build_seed_contract as _build_seed_contract_raw,
)
from fluid_build.cli.forge_copilot_contract_helpers import (
    normalize_generation_payload as _normalize_generation_payload_raw,
)
from fluid_build.cli.forge_copilot_contract_helpers import (
    redact_secret_like_text as _redact_secret_like_text_raw,
)
from fluid_build.cli.forge_copilot_contract_helpers import (
    validate_generated_result as _validate_generated_result_raw,
)

# ---------------------------------------------------------------------------
# Re-exports: discovery
# ---------------------------------------------------------------------------
from fluid_build.cli.forge_copilot_discovery import (  # noqa: F401
    DiscoveryReport,
    discover_local_context,
)

# ---------------------------------------------------------------------------
# Re-exports: LLM providers
# ---------------------------------------------------------------------------
from fluid_build.cli.forge_copilot_llm_providers import (  # noqa: F401
    BUILTIN_LLM_PROVIDERS,
    AnthropicProvider,
    CopilotGenerationError,
    GeminiProvider,
    LlmConfig,
    LlmProvider,
    OllamaProvider,
    OpenAIProvider,
    build_llm_run_plan,
    call_llm,
    call_llm_streaming,
    get_llm_provider,
    resolve_llm_config,
    streaming_is_enabled,
)

# ---------------------------------------------------------------------------
# Re-exports: memory, schema inference
# ---------------------------------------------------------------------------
from fluid_build.cli.forge_copilot_memory import CopilotMemorySnapshot  # noqa: F401

# ---------------------------------------------------------------------------
# Re-exports: prompts (need thin wrappers for engine list injection)
# ---------------------------------------------------------------------------
from fluid_build.cli.forge_copilot_prompts import (  # noqa: F401
    build_clarification_system_prompt,
    build_clarification_user_prompt,
    build_user_prompt,
)
from fluid_build.cli.forge_copilot_prompts import (
    build_system_prompt as _build_system_prompt_raw,
)
from fluid_build.cli.forge_copilot_schema_inference import (
    map_inferred_type_to_contract_type as _map_inferred_type_to_contract_type,
)
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.util.contract import get_builds

LOG = logging.getLogger("fluid.cli.forge_copilot")
COPILOT_BUILTIN_PROVIDERS = ("local", "gcp", "aws", "snowflake")


# ---------------------------------------------------------------------------
# Dataclasses (owned by this module)
# ---------------------------------------------------------------------------


@dataclass
class GenerationAttemptReport:
    """Diagnostic information for a single generation attempt."""

    attempt: int
    raw_provider: str
    raw_model: str
    parse_error: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)
    validation_warnings: List[str] = field(default_factory=list)


@dataclass
class ScaffoldDecisionReport:
    """Explain how scaffold seed guidance was chosen before LLM generation."""

    template: str
    provider: str
    template_source: str
    provider_source: str
    template_reason: str
    provider_reason: str


@dataclass
class CopilotGenerationResult:
    """Validated artifacts produced by the LLM-backed copilot flow."""

    suggestions: Dict[str, Any]
    contract: Dict[str, Any]
    readme_markdown: str
    additional_files: Dict[str, str]
    discovery_report: DiscoveryReport
    attempt_reports: List[GenerationAttemptReport]
    scaffold_decision: Optional[ScaffoldDecisionReport] = None
    project_memory: Optional[CopilotMemorySnapshot] = None
    provenance: Optional[Dict[str, Any]] = None
    logical_model: Optional[Dict[str, Any]] = None
    ai_run_plan: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Thin wrappers that inject module-level dependencies
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# System prompt cache (slice UX-I)
# ---------------------------------------------------------------------------
# ``build_system_prompt`` is pure: given the same capability matrix, it
# always returns the same string.  The generation retry loop calls it
# up to 3 times per run with the same input, and every call rebuilds
# ~1400 tokens of interpolated text — wasted CPU and, more
# importantly, wasted provider prompt-cache opportunities.  Memoizing
# the result lets Anthropic's ``cache_control: ephemeral`` block and
# OpenAI's automatic prompt caching actually fire, since both require
# byte-identical prefixes across requests.
#
# The cache holds a single entry (most recent capability matrix →
# prompt).  The retry loop always uses the same matrix, so a 1-entry
# LRU is sufficient and avoids any need for a size policy.  Keyed on
# a stable hash of the matrix JSON so that deep-copied matrices (which
# break ``id()``-based keying) still hit the cache.
_SYSTEM_PROMPT_CACHE: Optional[Dict[str, str]] = None
_SYSTEM_PROMPT_LOCK = threading.Lock()


def clear_system_prompt_cache() -> None:
    """Drop the process-wide system prompt cache.

    Tests that mutate the capability matrix inline (or monkey-patch
    ``_build_system_prompt_raw``) should call this to force the next
    ``build_system_prompt`` to rebuild.  Also chained from
    ``clear_capability_matrix_cache`` so callers that invalidate the
    upstream matrix cache don't end up with a stale system prompt.
    """
    global _SYSTEM_PROMPT_CACHE
    with _SYSTEM_PROMPT_LOCK:
        _SYSTEM_PROMPT_CACHE = None


def _system_prompt_cache_key(capability_matrix: Mapping[str, Any]) -> Optional[str]:
    """Compute a stable hash of *capability_matrix* for the cache.

    Returns ``None`` when the matrix contains values that can't be
    JSON-serialized — the caller then skips the cache entirely and
    rebuilds every time.  That's safe (never a stale hit) and rare
    enough in practice that the overhead is negligible.
    """
    try:
        blob = json.dumps(capability_matrix, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — defensive
        return None
    engines_hash = hash(tuple(sorted(KNOWN_BUILD_ENGINES)))
    return f"{hashlib.sha1(blob.encode('utf-8'), usedforsecurity=False).hexdigest()}:{engines_hash}"


def build_system_prompt(capability_matrix: Mapping[str, Any]) -> str:
    """Build the system prompt, injecting the known build engines list.

    Memoized per-process on a stable hash of the capability matrix
    (slice UX-I).  Subsequent calls with the same matrix return the
    byte-identical cached string, which is what Anthropic's
    ``cache_control: ephemeral`` and OpenAI's automatic prefix caching
    need to actually hit.
    """
    global _SYSTEM_PROMPT_CACHE
    key = _system_prompt_cache_key(capability_matrix)
    if key is not None:
        with _SYSTEM_PROMPT_LOCK:
            cached = _SYSTEM_PROMPT_CACHE
            if cached is not None and cached.get("key") == key:
                return cached["prompt"]

    prompt = _build_system_prompt_raw(capability_matrix, sorted(KNOWN_BUILD_ENGINES))
    if key is not None:
        with _SYSTEM_PROMPT_LOCK:
            _SYSTEM_PROMPT_CACHE = {"key": key, "prompt": prompt}
    return prompt


def build_seed_contract(
    *,
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
    template_name: str,
    provider_name: str,
    project_memory: Optional[CopilotMemorySnapshot] = None,
) -> Dict[str, Any]:
    """Build a seed contract, injecting the type-mapping function."""
    return _build_seed_contract_raw(
        context=context,
        discovery_report=discovery_report,
        template_name=template_name,
        provider_name=provider_name,
        project_memory=project_memory,
        map_inferred_type_fn=_map_inferred_type_to_contract_type,
    )


def normalize_generation_payload(
    payload: Mapping[str, Any],
    *,
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
    capabilities: Mapping[str, Any],
    seed_template: str,
    seed_provider: str,
) -> Dict[str, Any]:
    """Normalize LLM output, injecting contract resolution helpers."""
    try:
        return _normalize_generation_payload_raw(
            payload,
            context=context,
            discovery_report=discovery_report,
            seed_template=seed_template,
            seed_provider=seed_provider,
            resolve_provider_from_contract_fn=resolve_provider_from_contract,
            get_builds_fn=get_builds,
        )
    except ValueError as exc:
        raise CopilotGenerationError(
            "copilot_contract_missing",
            str(exc),
            suggestions=["Ensure the selected model returns strict JSON objects"],
        ) from exc


def validate_generated_result(
    normalized: Mapping[str, Any],
    *,
    capabilities: Mapping[str, Any],
    logger: Optional[logging.Logger] = None,
    context: Optional[Mapping[str, Any]] = None,
) -> tuple[List[str], List[str]]:
    """Validate generated contract, injecting schema manager and helpers.

    ``context`` carries the interview/normalised-context dict so
    technique-aware checks (e.g. DV2 additional_files) fire only on the
    runs that opted in.
    """
    return _validate_generated_result_raw(
        normalized,
        capabilities=capabilities,
        logger=logger or LOG,
        schema_manager_cls=FluidSchemaManager,
        resolve_provider_from_contract_fn=resolve_provider_from_contract,
        get_builds_fn=get_builds,
        context=context,
    )


def redact_secret_like_text(text: str) -> str:
    """Redact secrets, injecting the shared redaction function."""
    return _redact_secret_like_text_raw(text, redact_secrets_fn=redact_secrets)


# ---------------------------------------------------------------------------
# Core orchestration (owned by this module)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Capability matrix cache (slice UX-G)
# ---------------------------------------------------------------------------
# build_capability_matrix() iterates every provider + template in the
# registry, calling get() + get_metadata() for each.  On a fat registry
# this was a 1-2 second hit on every copilot invocation.  The matrix
# only changes when the process reloads the registries, so we memoize
# it for the lifetime of the process behind a lock.
#
# Callers that want to force a fresh build (e.g. tests that mutate the
# registry mid-run) should call ``clear_capability_matrix_cache()``
# before invoking ``build_capability_matrix`` again.
_CAPABILITY_MATRIX_CACHE: Optional[Dict[str, Any]] = None
_CAPABILITY_MATRIX_LOCK = threading.Lock()


def clear_capability_matrix_cache() -> None:
    """Drop the process-wide capability matrix cache.

    Tests (and any future ``fluid doctor refresh`` command) can call
    this to force the next ``build_capability_matrix()`` to recompute
    from scratch.  Also invalidates the downstream system-prompt
    cache (slice UX-I) so a subsequent ``build_system_prompt`` call
    doesn't hand back a prompt that refers to a stale matrix.
    """
    global _CAPABILITY_MATRIX_CACHE
    with _CAPABILITY_MATRIX_LOCK:
        _CAPABILITY_MATRIX_CACHE = None
    clear_system_prompt_cache()


def build_capability_matrix() -> Dict[str, Any]:
    """Describe the locally available templates, providers, and supported engines.

    Cached per-process (slice UX-G).  The first call pays the full
    registry-scan cost; subsequent calls return a deep copy of the
    cached dict so callers that mutate the result can't poison the
    cache for the next caller.  See ``clear_capability_matrix_cache``
    for how to invalidate.
    """
    global _CAPABILITY_MATRIX_CACHE
    with _CAPABILITY_MATRIX_LOCK:
        if _CAPABILITY_MATRIX_CACHE is not None:
            return copy.deepcopy(_CAPABILITY_MATRIX_CACHE)

    matrix = _build_capability_matrix_uncached()
    with _CAPABILITY_MATRIX_LOCK:
        # Another thread may have populated the cache between our checks;
        # that's fine — last writer wins, the content is identical.
        _CAPABILITY_MATRIX_CACHE = matrix
    return copy.deepcopy(matrix)


def _build_capability_matrix_uncached() -> Dict[str, Any]:
    """Describe the locally available templates, providers, and supported engines."""
    warnings: List[str] = []
    try:
        from fluid_build.forge.core.registry import provider_registry, template_registry
    except Exception as exc:  # pragma: no cover
        warnings.append(
            "Copilot couldn't inspect the local provider registry "
            f"({exc}). Continuing with built-in provider defaults."
        )
        return {
            "providers": list(COPILOT_BUILTIN_PROVIDERS),
            "templates": {},
            "build_engines": sorted(KNOWN_BUILD_ENGINES),
            "provider_engine_compatibility": {
                provider: sorted(engines)
                for provider, engines in PROVIDER_ENGINE_COMPATIBILITY.items()
            },
            "warnings": warnings,
        }

    try:
        discovered_provider_names = list(provider_registry.list_available())
    except Exception as exc:
        discovered_provider_names = []
        warnings.append(
            "Copilot couldn't list local providers "
            f"({exc}). Continuing with built-in provider defaults."
        )

    verified_provider_names: List[str] = []
    for provider_name in discovered_provider_names:
        try:
            provider = provider_registry.get(provider_name)
        except Exception as exc:  # pragma: no cover
            provider = None
            if provider_name in COPILOT_BUILTIN_PROVIDERS:
                warnings.append(
                    f"Copilot couldn't inspect the {provider_name} provider ({exc}). "
                    "Continuing without blocking the run."
                )
        if provider is None:
            if provider_name in COPILOT_BUILTIN_PROVIDERS:
                warnings.append(
                    f"Copilot couldn't inspect the {provider_name} provider. "
                    "Continuing without blocking the run."
                )
            continue
        verified_provider_names.append(provider_name)

    if verified_provider_names:
        provider_names = [p for p in COPILOT_BUILTIN_PROVIDERS if p in verified_provider_names]
        provider_names.extend(sorted(p for p in verified_provider_names if p not in provider_names))
    else:
        provider_names = list(COPILOT_BUILTIN_PROVIDERS)
        warnings.append(
            "Copilot couldn't verify any local providers, so it's using built-in provider defaults "
            "for planning. You can still review or override the provider later."
        )

    try:
        template_names = template_registry.list_available()
    except Exception as exc:
        template_names = []
        warnings.append(
            "Copilot couldn't list local templates "
            f"({exc}). Continuing with built-in defaults where possible."
        )
    templates: Dict[str, Any] = {}

    for template_name in template_names:
        try:
            template = template_registry.get(template_name)
        except Exception as exc:  # pragma: no cover
            warnings.append(
                f"Copilot couldn't inspect template '{template_name}' ({exc}). "
                "Continuing with the remaining templates."
            )
            continue
        if not template:
            warnings.append(
                f"Copilot couldn't inspect template '{template_name}'. "
                "Continuing with the remaining templates."
            )
            continue
        try:
            metadata = template.get_metadata()
        except Exception as exc:
            warnings.append(
                f"Copilot couldn't read metadata for template '{template_name}' ({exc}). "
                "Continuing with the remaining templates."
            )
            continue
        templates[template_name] = {
            "description": metadata.description,
            "provider_support": [p for p in metadata.provider_support if p in provider_names],
            "use_cases": metadata.use_cases,
            "technologies": metadata.technologies,
        }

    return {
        "providers": provider_names,
        "templates": templates,
        "build_engines": sorted(KNOWN_BUILD_ENGINES),
        "provider_engine_compatibility": {
            provider: sorted(engines) for provider, engines in PROVIDER_ENGINE_COMPATIBILITY.items()
        },
        "warnings": warnings,
    }


def _call_llm_with_optional_streaming(
    provider_adapter: LlmProvider,
    llm_config: LlmConfig,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Slice UX-I: pick streaming vs blocking LLM transport.

    Returns the full concatenated response text so the downstream
    retry/repair loop and validation stay completely untouched.
    Streaming is enabled when BOTH the per-call
    ``llm_config.streaming`` flag and the process-wide
    ``FLUID_LLM_STREAMING`` env kill-switch allow it.

    When streaming is picked and yields real chunks, we also render
    a live progress line via Rich (if a suitable Rich console is
    importable) so the user sees tokens flowing instead of a silent
    spinner.  Rich is imported lazily so this module stays
    importable in headless test environments.

    Any streaming-side error short-circuits to the blocking
    ``call_llm`` path as a belt-and-braces safety net; the next
    retry attempt will then use the blocking path too.
    """
    use_streaming = bool(getattr(llm_config, "streaming", True)) and streaming_is_enabled()
    if not use_streaming:
        return call_llm(provider_adapter, llm_config, system_prompt, user_prompt)

    chunks: List[str] = []
    live_ctx: Any = None
    try:
        # Lazy Rich import so the module remains importable in
        # environments without Rich (e.g. minimal CI containers).
        from rich.console import Console  # noqa: WPS433
        from rich.live import Live  # noqa: WPS433
        from rich.spinner import Spinner  # noqa: WPS433
        from rich.text import Text  # noqa: WPS433

        live_console = Console(stderr=True)
        spinner = Spinner("dots", text=Text("Generating contract…", style="cyan"))
        live_ctx = Live(spinner, console=live_console, refresh_per_second=12, transient=True)
        live_ctx.__enter__()
    except Exception:  # noqa: BLE001 — Rich is optional
        live_ctx = None

    def _update_live(char_count: int) -> None:
        if live_ctx is None:
            return
        try:
            from rich.spinner import Spinner  # noqa: WPS433
            from rich.text import Text  # noqa: WPS433

            label = Text.assemble(
                ("Generating contract ", "cyan"),
                (f"({char_count:,} chars)", "dim"),
            )
            live_ctx.update(Spinner("dots", text=label))
        except Exception:  # noqa: BLE001
            pass

    try:
        char_total = 0
        for chunk in call_llm_streaming(provider_adapter, llm_config, system_prompt, user_prompt):
            chunks.append(chunk)
            char_total += len(chunk)
            if char_total % 256 < len(chunk):  # cheap throttle
                _update_live(char_total)
        return "".join(chunks)
    except CopilotGenerationError as streaming_exc:
        # Streaming layer failed — fall back to the blocking path so
        # the user doesn't have to manually flip the kill-switch.
        LOG.info(
            "LLM streaming failed (%s); falling back to blocking call_llm",
            streaming_exc,
        )
        return call_llm(provider_adapter, llm_config, system_prompt, user_prompt)
    finally:
        if live_ctx is not None:
            try:
                live_ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def _self_eval_enabled() -> bool:
    """Kill-switch for post-generation self-evaluation."""
    value = os.environ.get("FLUID_COPILOT_SELF_EVAL", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _self_evaluate_contract(
    llm_config: "LlmConfig",
    context: Mapping[str, Any],
    contract: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """Ask the routing model to evaluate a generated contract (fail-open).

    Returns ``{"score": int, "issues": [...], "suggestions": [...]}``
    or ``None`` if the evaluation call fails for any reason.
    """
    if not _self_eval_enabled():
        return None
    try:
        from fluid_build.cli.forge_copilot_prompts import build_evaluation_prompt

        eval_prompt = build_evaluation_prompt(context, contract)
        routing_config = llm_config.for_routing()
        adapter = get_llm_provider(routing_config.provider)
        raw = call_llm(
            adapter,
            routing_config,
            "You are a contract quality evaluator. Return strict JSON only.",
            eval_prompt,
        )
        result = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(result, dict) and "score" in result:
            if logger:
                logger.info("Self-evaluation score: %s/10", result.get("score"))
            return result
    except Exception as exc:  # noqa: BLE001 — fail-open
        if logger:
            logger.debug("Self-evaluation failed (skipping): %s", exc)
    return None


def generate_copilot_artifacts(
    context: Mapping[str, Any],
    *,
    llm_config: LlmConfig,
    discovery_report: DiscoveryReport,
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Dict[str, Any]] = None,
    capability_matrix: Optional[Mapping[str, Any]] = None,
    logger: Optional[logging.Logger] = None,
    max_attempts: int = 3,
) -> CopilotGenerationResult:
    """Generate and validate copilot artifacts with a repair loop."""
    if not bool(os.environ.get("FLUID_FORGE_LEGACY_COPILOT")) and _should_use_staged_copilot(
        context, discovery_report
    ):
        staged_result = _generate_staged_copilot_artifacts(
            context,
            llm_config=llm_config,
            discovery_report=discovery_report,
            project_memory=project_memory,
            team_memory=team_memory,
            capability_matrix=capability_matrix,
            logger=logger,
        )
        if staged_result is not None:
            return staged_result

    capabilities = dict(capability_matrix or build_capability_matrix())
    provider_adapter = get_llm_provider(llm_config.provider)
    ai_run_plan = build_llm_run_plan(
        llm_config,
        tiered=bool(context.get("tiered") or os.environ.get("FLUID_TIERED")),
    )

    # Apply team memory defaults to context gaps (team memory sits between
    # explicit user input and project/personal memory in precedence).
    if team_memory:
        team_defaults = (team_memory.get("conventions") or {}).get("defaults") or {}
        context = dict(context)  # shallow copy to avoid mutating caller's dict
        for key in ("provider", "domain", "owner_team", "build_engine"):
            team_value = team_defaults.get(key)
            if team_value and not context.get(key):
                context[key] = team_value

    scaffold_decision = _build_scaffold_decision(
        context,
        discovery_report,
        capabilities,
        project_memory=project_memory,
    )
    suggested_template = scaffold_decision.template
    suggested_provider = scaffold_decision.provider
    seed_contract = build_seed_contract(
        context=context,
        discovery_report=discovery_report,
        template_name=suggested_template,
        provider_name=suggested_provider,
        project_memory=project_memory,
    )

    attempts: List[GenerationAttemptReport] = []
    previous_errors: List[str] = []
    previous_payload: Optional[Dict[str, Any]] = None

    for attempt_index in range(1, max_attempts + 1):
        system_prompt = build_system_prompt(capabilities)
        user_prompt = build_user_prompt(
            context=context,
            discovery_report=discovery_report,
            capability_matrix=capabilities,
            seed_contract=seed_contract,
            seed_template=suggested_template,
            seed_provider=suggested_provider,
            attempt_index=attempt_index,
            previous_errors=previous_errors,
            previous_payload=previous_payload,
            project_memory=project_memory,
            team_memory=team_memory,
        )

        report = GenerationAttemptReport(
            attempt=attempt_index,
            raw_provider=llm_config.provider,
            raw_model=llm_config.model,
        )
        attempts.append(report)

        raw_text = _call_llm_with_optional_streaming(
            provider_adapter, llm_config, system_prompt, user_prompt
        )
        try:
            payload = extract_json_object(raw_text)
        except ValueError as exc:
            report.parse_error = str(exc)
            previous_errors = [report.parse_error]
            previous_payload = {"raw_text": redact_secret_like_text(raw_text[:2000])}
            continue

        normalized = normalize_generation_payload(
            payload,
            context=context,
            discovery_report=discovery_report,
            capabilities=capabilities,
            seed_template=suggested_template,
            seed_provider=suggested_provider,
        )
        validation_errors, validation_warnings = validate_generated_result(
            normalized,
            capabilities=capabilities,
            logger=logger,
            context=context,
        )
        report.validation_errors = validation_errors
        report.validation_warnings = validation_warnings

        if not validation_errors:
            # Self-evaluation: ask the routing model to rate the contract.
            # If score < 7, treat evaluation issues as repair feedback and
            # loop back for another attempt.  Fail-open — evaluation
            # errors never block a valid contract from being returned.
            eval_result = _self_evaluate_contract(
                llm_config,
                context,
                normalized["contract"],
                logger=logger,
            )
            if eval_result and eval_result.get("score", 10) < 7 and attempt_index < max_attempts:
                issues = eval_result.get("issues") or eval_result.get("suggestions") or []
                if issues:
                    previous_errors = build_structured_repair_feedback(
                        [f"Quality issue: {issue}" for issue in issues]
                    )
                    previous_payload = payload
                    report.validation_warnings.append(
                        f"Self-evaluation score {eval_result.get('score')}/10 — retrying."
                    )
                    continue

            provenance = {
                "llm_provider": llm_config.provider,
                "llm_model": llm_config.model,
                "ai_run_plan": ai_run_plan,
                "system_prompt_hash": hashlib.sha256(system_prompt.encode()).hexdigest()[:16],
                "user_prompt_hash": hashlib.sha256(user_prompt.encode()).hexdigest()[:16],
                "discovery_hash": hashlib.sha256(
                    json.dumps(discovery_report.to_prompt_payload(), sort_keys=True).encode()
                ).hexdigest()[:16],
                "attempt": attempt_index,
                "self_eval_score": eval_result.get("score") if eval_result else None,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            }
            return CopilotGenerationResult(
                suggestions=normalized["suggestions"],
                contract=normalized["contract"],
                readme_markdown=normalized["readme_markdown"],
                additional_files=normalized["additional_files"],
                discovery_report=discovery_report,
                attempt_reports=attempts,
                scaffold_decision=scaffold_decision,
                project_memory=project_memory,
                provenance=provenance,
                ai_run_plan=ai_run_plan,
            )

        previous_errors = build_structured_repair_feedback(validation_errors)
        previous_payload = payload

    attempt_summaries = []
    for report in attempts:
        if report.parse_error:
            attempt_summaries.append(
                f"Attempt {report.attempt}: parse error - {report.parse_error}"
            )
        elif report.validation_errors:
            joined = "; ".join(report.validation_errors[:4])
            attempt_summaries.append(f"Attempt {report.attempt}: validation failed - {joined}")
    failure_class = classify_generation_failure(attempts)
    raise CopilotGenerationError(
        "copilot_generation_failed",
        "Forge copilot could not produce a valid contract after 3 attempts.",
        suggestions=[
            "Check your project_goal/data_sources context for clarity",
            "Verify the selected model supports structured JSON responses",
            "Inspect discovery inputs for unsupported or ambiguous sources",
            *attempt_summaries[:3],
        ],
        context={"failure_class": failure_class, "attempt_summaries": attempt_summaries[:3]},
    )


def _generate_staged_copilot_artifacts(
    context: Mapping[str, Any],
    *,
    llm_config: LlmConfig,
    discovery_report: DiscoveryReport,
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Dict[str, Any]] = None,
    capability_matrix: Optional[Mapping[str, Any]] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[CopilotGenerationResult]:
    """Use the staged coordinator when we have enough context to do so safely."""
    try:
        from fluid_build.copilot.agents.base import StageSession
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.schemas.intent import (
            BusinessContext,
            BusinessIntent,
            Consumption,
            DataProduct,
            DataSource,
            Dimensions,
            Grain,
            Metric,
            ModelingPreferences,
        )
        from fluid_build.copilot.store.factory import resolve_store
        from fluid_build.forge_datamodel.from_ddl.parser import TableDefinition
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.debug("staged_copilot_unavailable: %s", exc)
        return None

    capabilities = dict(capability_matrix or build_capability_matrix())
    scaffold_decision = _build_scaffold_decision(
        context,
        discovery_report,
        capabilities,
        project_memory=project_memory,
    )

    technique = _normalize_stage_technique(context.get("data_modeling_technique"))
    if technique is None:
        technique = "data_vault_2"

    session = StageSession(
        store=resolve_store(workspace_root=Path.cwd()),
        workspace_root=Path.cwd(),
        llm_config=llm_config,
        tiered=bool(context.get("tiered") or os.environ.get("FLUID_TIERED")),
        require_llm=bool(context.get("require_llm")),
        capability_matrix=capabilities,
        project_memory=project_memory.to_prompt_payload() if project_memory else None,
        team_memory=team_memory,
        discovery_report=discovery_report,
    )
    coordinator = StageCoordinator()
    staged_engine = _resolve_staged_engine(
        context,
        capabilities,
        project_memory=project_memory,
        team_memory=team_memory,
    )

    source = _select_staged_source(context, discovery_report)
    coordinator_result = None
    if source["kind"] == "ddl":
        ddl_tables: List[TableDefinition] = source["tables"]
        if ddl_tables:
            coordinator_result = coordinator.from_tables(
                session,
                name=_stage_product_name(context),
                tables=ddl_tables,
                technique=technique,
                source_type=source.get("source_type"),
                engine=staged_engine,
                include_physical=True,
            )
    elif source["kind"] == "intent_file":
        from fluid_build.forge_datamodel.from_intent.intent_loader import load_business_intent

        intent = load_business_intent(source["path"])
        coordinator_result = coordinator.from_intent(
            session,
            intent=intent,
            technique=technique,
            engine=staged_engine,
            include_physical=True,
        )
    if coordinator_result is None:
        intent = _build_business_intent_from_context(context, discovery_report, technique=technique)
        coordinator_result = coordinator.from_intent(
            session,
            intent=intent,
            technique=technique,
            engine=staged_engine,
            include_physical=True,
        )

    physical = coordinator_result.physical
    if physical is None:
        return None

    readme_markdown = physical.readme.readme_markdown if physical.readme else ""
    additional_files = dict(physical.additional_files or {})
    additional_files.update(_transform_plan_to_files(physical.transform_plan, technique=technique))
    suggestions = {
        "recommended_template": scaffold_decision.template,
        "recommended_provider": scaffold_decision.provider,
        "recommended_patterns": [technique, staged_engine],
        "architecture_suggestions": [
            f"Use the staged {technique.replace('_', ' ')} modeling flow as the contract boundary.",
            "Review the logical sidecar before regenerating physical transformations.",
        ],
        "best_practices": [
            "Keep the .model.json sidecar under version control for review and drift detection.",
            "Regenerate transformations from the sidecar instead of editing generated SQL by hand.",
        ],
        "technology_stack": [staged_engine, scaffold_decision.provider],
        "description": physical.contract.get("description") or physical.logical.description,
        "domain": physical.contract.get("domain") or context.get("domain") or "analytics",
        "owner": ((physical.contract.get("metadata") or {}).get("owner") or {}).get(
            "team", "data-team"
        ),
    }
    provenance = {
        "mode": "staged",
        "llm_provider": llm_config.provider,
        "llm_model": llm_config.model,
        "ai_run_plan": build_llm_run_plan(llm_config, tiered=session.tiered),
        "agent_events": list(session.agent_events),
        "fallback_used": bool(session.fallback_used),
        "fallback_events": list(session.fallback_events),
        "repair_used": bool(session.repair_used),
        "repair_events": list(session.repair_events),
        "data_model_source": source["kind"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    return CopilotGenerationResult(
        suggestions=suggestions,
        contract=physical.contract,
        readme_markdown=readme_markdown,
        additional_files=additional_files,
        discovery_report=discovery_report,
        attempt_reports=[
            GenerationAttemptReport(
                attempt=1,
                raw_provider=llm_config.provider,
                raw_model=llm_config.model,
                validation_warnings=["staged_coordinator"],
            )
        ],
        scaffold_decision=scaffold_decision,
        project_memory=project_memory,
        provenance=provenance,
        logical_model=coordinator_result.logical.model_dump(mode="json", by_alias=True),
        ai_run_plan=provenance["ai_run_plan"],
    )


def _should_use_staged_copilot(
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
) -> bool:
    if os.environ.get("FLUID_FORGE_STAGED_COPILOT") == "1":
        return True
    for key in (
        "data_model_source",
        "data_model_paths",
        "data_model_description",
        "review_data_model",
    ):
        if context.get(key):
            return True
    return bool(discovery_report.user_data_models)


def _resolve_staged_engine(
    context: Mapping[str, Any],
    capability_matrix: Mapping[str, Any],
    *,
    project_memory: Optional[CopilotMemorySnapshot] = None,
    team_memory: Optional[Dict[str, Any]] = None,
) -> str:
    explicit = str(context.get("build_engine") or "").strip()
    if explicit:
        return explicit

    remembered = list(getattr(project_memory, "build_engines", []) or [])
    if remembered:
        return str(remembered[0])

    team_defaults = ((team_memory or {}).get("conventions") or {}).get("defaults") or {}
    team_engine = str(team_defaults.get("build_engine") or "").strip()
    if team_engine:
        return team_engine

    engines = [str(engine).strip() for engine in (capability_matrix.get("build_engines") or [])]
    engines = [engine for engine in engines if engine]
    if "sql" in engines:
        return "sql"
    if engines:
        return engines[0]
    return "sql"


def _stage_product_name(context: Mapping[str, Any]) -> str:
    goal = str(context.get("project_goal") or context.get("name") or "forged_data_model")
    value = "".join(ch.lower() if ch.isalnum() else "_" for ch in goal).strip("_")
    return value or "forged_data_model"


def _normalize_stage_technique(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower().replace("-", "_")
    if text in {"data_vault_2", "dimensional"}:
        return text
    return None


def _select_staged_source(
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
) -> Dict[str, Any]:
    from fluid_build.forge_datamodel.from_ddl.parser import parse_ddl_text

    raw_source = str(context.get("data_model_source") or "").strip().lower()
    explicit_paths = context.get("data_model_paths") or context.get("data_model_path") or []
    if isinstance(explicit_paths, str):
        explicit_paths = [segment for segment in explicit_paths.split() if segment]
    explicit_paths = [str(path) for path in explicit_paths]

    if raw_source == "intent" and explicit_paths:
        candidate = Path(explicit_paths[0]).expanduser()
        if candidate.exists():
            return {"kind": "intent_file", "path": str(candidate)}

    ddl_candidates: List[Path] = []
    if raw_source == "ddl":
        ddl_candidates.extend(Path(path).expanduser() for path in explicit_paths)
    if not ddl_candidates:
        for model in getattr(discovery_report, "user_data_models", []) or []:
            path = Path(str(model.get("path") or ""))
            if path.suffix.lower() == ".sql" and path.exists():
                ddl_candidates.append(path)
        for sql_file in discovery_report.sql_files:
            path = Path(str(sql_file.get("path") or ""))
            if path.exists():
                ddl_candidates.append(path)

    tables = []
    for path in ddl_candidates:
        try:
            ddl_text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_ddl_text(ddl_text, dialect=context.get("source_type"))
        tables.extend(parsed.tables)

    if tables:
        return {"kind": "ddl", "tables": tables, "source_type": context.get("source_type")}
    return {"kind": raw_source or "intent"}


def _build_business_intent_from_context(
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
    *,
    technique: str,
):
    from fluid_build.copilot.schemas.intent import (
        BusinessContext,
        BusinessIntent,
        Consumption,
        DataProduct,
        DataSource,
        Dimensions,
        Grain,
        Metric,
        ModelingPreferences,
    )

    goal = str(context.get("project_goal") or "Data Product").strip()
    domain = str(context.get("domain") or "analytics").strip()
    problem_statement = str(
        context.get("problem_statement")
        or context.get("data_model_description")
        or context.get("data_sources")
        or goal
    )
    data_sources_text = str(context.get("data_sources") or "")
    metrics = _split_csvish(context.get("metrics") or context.get("measures"))
    dimensions = _split_csvish(context.get("dimensions"))
    use_cases = _split_csvish(context.get("use_case")) or _split_csvish(context.get("use_cases"))
    sample_sources = [
        DataSource(
            source_name=Path(str(sample.get("path") or "")).stem,
            source_type=str(sample.get("format") or "file"),
            description=str(sample.get("path") or ""),
        )
        for sample in (discovery_report.sample_files or [])[:6]
    ]
    grain_entity = str(context.get("grain_entity") or "").strip() or (
        dimensions[0] if dimensions else _stage_product_name(context)
    )
    intent = BusinessIntent(
        data_product=DataProduct(
            name=_stage_product_name(context),
            domain=domain,
            description=str(context.get("description") or goal),
            owner=str(context.get("owner_team") or "data-team"),
        ),
        business_context=BusinessContext(
            problem_statement=problem_statement,
            decision_supported=str(context.get("decision_supported") or ""),
            consumer=str(context.get("consumer") or ""),
        ),
        metrics=[
            Metric(name=name, description=f"Metric requested for {name}.") for name in metrics
        ],
        grain=Grain(
            entity=grain_entity,
            description=str(context.get("grain_description") or f"Primary grain for {goal}."),
            time_dimension=str(context.get("time_dimension") or ""),
        ),
        dimensions=Dimensions(
            entities=dimensions,
            attributes=_split_csvish(context.get("dimension_attributes")),
        ),
        data_sources=sample_sources
        or [
            DataSource(
                source_name=(data_sources_text or "source").split(",")[0].strip() or "source",
                source_type="context",
                description=data_sources_text or "Context-supplied data source description.",
            )
        ],
        consumption=Consumption(
            use_cases=use_cases,
            output_format=_split_csvish(context.get("output_format")) or ["table"],
            refresh_frequency=str(context.get("refresh_frequency") or ""),
        ),
        business_rules=_split_multiline(context.get("business_rules")),
        modeling=ModelingPreferences(
            technique=technique,
            hash_key_algorithm="md5" if technique == "data_vault_2" else None,
            scd_policy_default="type2" if technique == "dimensional" else None,
        ),
    )
    return intent


def _split_csvish(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        text = str(value).replace("\n", ",")
        items = [segment.strip() for segment in text.split(",")]
    return [item for item in items if item]


def _split_multiline(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _transform_plan_to_files(transform_plan: Any, *, technique: str) -> Dict[str, str]:
    files: Dict[str, str] = {}
    for build in getattr(transform_plan, "builds", []) or []:
        layer = build.layer or ("marts" if technique == "dimensional" else "raw_vault")
        path = f"dbt_project/models/{layer}/{build.name}.sql"
        files[path] = build.sql or "-- generated by staged builder\nselect 1 as placeholder"
    return files


def suggest_scaffold(
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
    capability_matrix: Mapping[str, Any],
    *,
    project_memory: Optional[CopilotMemorySnapshot] = None,
) -> tuple[str, str]:
    """Heuristically choose valid scaffold defaults used only as LLM guidance."""
    decision = _build_scaffold_decision(
        context,
        discovery_report,
        capability_matrix,
        project_memory=project_memory,
    )
    return decision.template, decision.provider


def _build_scaffold_decision(
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
    capability_matrix: Mapping[str, Any],
    *,
    project_memory: Optional[CopilotMemorySnapshot] = None,
) -> ScaffoldDecisionReport:
    """Build explainable scaffold guidance before LLM generation."""
    text = " ".join(
        [
            str(context.get("project_goal", "")),
            str(context.get("use_case", "")),
            str(context.get("use_case_other", "")),
            str(context.get("data_sources", "")),
            " ".join(discovery_report.provider_hints),
        ]
    ).lower()

    available_providers = set(capability_matrix.get("providers") or [])
    fallback_provider = (
        "local"
        if "local" in available_providers
        else (
            sorted(available_providers)[0] if available_providers else COPILOT_BUILTIN_PROVIDERS[0]
        )
    )

    # --- Provider selection ---
    explicit_provider = normalize_provider_name(context.get("provider") or "")
    if explicit_provider in available_providers:
        provider = explicit_provider
        provider_source = "explicit_context"
        provider_reason = (
            f"Using explicit provider hint '{explicit_provider}' from the current run."
        )
    elif discovery_report.provider_hints:
        provider = provider_source = provider_reason = ""
        for hint in discovery_report.provider_hints:
            candidate = normalize_provider_name(hint)
            if candidate in available_providers:
                provider = candidate
                provider_source = "current_discovery"
                provider_reason = (
                    f"Using current discovery provider hint '{candidate}' from local assets."
                )
                break
    elif "snowflake" in text:
        provider, provider_source = "snowflake", "heuristic_context"
        provider_reason = "Using the current run context because it references Snowflake."
    elif any(t in text for t in ("aws", "s3", "redshift", "athena", "glue")):
        provider, provider_source = "aws", "heuristic_context"
        provider_reason = (
            "Using the current run context because it references AWS-oriented sources."
        )
    elif any(t in text for t in ("gcp", "bigquery", "dataform", "composer")):
        provider, provider_source = "gcp", "heuristic_context"
        provider_reason = (
            "Using the current run context because it references GCP-oriented sources."
        )
    else:
        provider = provider_source = provider_reason = ""

    if not provider and project_memory:
        preferred = normalize_provider_name(project_memory.preferred_provider)
        if preferred in available_providers:
            provider, provider_source = preferred, "project_memory"
            provider_reason = f"Reusing saved project memory provider '{preferred}' because the current run was ambiguous."
        else:
            for hint in project_memory.provider_hints:
                candidate = normalize_provider_name(hint)
                if candidate in available_providers:
                    provider, provider_source = candidate, "project_memory"
                    provider_reason = f"Using saved project memory provider hint '{candidate}' because no stronger current signal was available."
                    break
    if not provider:
        provider, provider_source = fallback_provider, "default"
        provider_reason = f"Falling back to the safe default provider '{provider}'."

    # --- Template selection ---
    templates = set((capability_matrix.get("templates") or {}).keys())
    explicit_template = context.get("template") or context.get("recommended_template")
    template = normalize_template_name(explicit_template) if explicit_template else ""

    if template in templates:
        template_source = "explicit_context"
        template_reason = f"Using explicit template hint '{template}' from the current run."
    elif any(t in text for t in ("ml", "machine learning", "feature store", "model")):
        template, template_source = "ml_pipeline", "heuristic_context"
        template_reason = (
            "Using the current run context because it looks like a machine-learning pipeline."
        )
    elif any(t in text for t in ("stream", "kafka", "real-time", "realtime")):
        template, template_source = "streaming", "heuristic_context"
        template_reason = (
            "Using the current run context because it looks like a streaming workload."
        )
    elif any(
        t in text
        for t in (
            "etl",
            "ingest",
            "cdc",
            "multi-source",
            "sync",
            "data_platform",
            "data platform",
            "data lake",
            "lakehouse",
        )
    ):
        template, template_source = "etl_pipeline", "heuristic_context"
        template_reason = (
            "Using the current run context because it looks like an ingestion or ETL workload."
        )
    elif any(t in text for t in ("analytics", "report", "dashboard", "bi", "metric")):
        template, template_source = "analytics", "heuristic_context"
        template_reason = (
            "Using the current run context because it looks like an analytics project."
        )
    elif project_memory and normalize_template_name(project_memory.preferred_template) in templates:
        template = normalize_template_name(project_memory.preferred_template)
        template_source = "project_memory"
        template_reason = f"Reusing saved project memory template '{template}' because the current run was ambiguous."
    else:
        template, template_source = "starter", "default"
        template_reason = "Falling back to the safe default template 'starter'."

    if template not in templates:
        template, template_source = "starter", "default"
        template_reason = "Falling back to the safe default template 'starter'."

    return ScaffoldDecisionReport(
        template=template,
        provider=provider,
        template_source=template_source,
        provider_source=provider_source,
        template_reason=template_reason,
        provider_reason=provider_reason,
    )
