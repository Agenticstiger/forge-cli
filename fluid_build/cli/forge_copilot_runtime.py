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
from typing import Any, Dict, List, Mapping, Optional

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
    active_overlay_validator_rules,
    build_clarification_system_prompt,
    build_clarification_user_prompt,
    build_user_prompt,
    get_active_prompt_profile,
    guidance_cache_token,
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
    # Fold every active guidance override layer (prompt profile + per-tenant
    # home shadow + active domain fragments) into the key so switching any
    # layer never returns a stale (default-guidance) prompt from the cache.
    guidance = guidance_cache_token()
    digest = hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{digest}:{engines_hash}:{guidance}"


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

    Any active ``--prompt-overlay`` stack's ``validator_rules`` are resolved
    here and threaded into the pure validator so an overlay-supplied rule can
    reject a violating contract. No overlays ⇒ empty list ⇒ unchanged behaviour.
    """
    overlay_rules = None
    try:
        overlay_rules = active_overlay_validator_rules()
    except Exception as exc:  # noqa: BLE001 — never block validation on this
        LOG.debug("overlay_validator_rules_resolve_failed: %s", exc)
    return _validate_generated_result_raw(
        normalized,
        capabilities=capabilities,
        logger=logger or LOG,
        schema_manager_cls=FluidSchemaManager,
        resolve_provider_from_contract_fn=resolve_provider_from_contract,
        get_builds_fn=get_builds,
        context=context,
        validator_rules=overlay_rules,
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


def _judge_enabled() -> bool:
    """Kill-switch for the out-of-loop LLM-as-judge pass."""
    value = os.environ.get("FLUID_COPILOT_JUDGE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _judge_contract(
    contract: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
    *,
    build_artifacts: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Run the JudgeAgent and return a flat dict of scores (fail-open).

    Returns ``{"score": int, "axes": {axis: int, ...}, "model": str}`` or
    ``None`` when the judge is disabled or fails. The judge writes its
    own ``judge.json`` receipt to ``.fluid/agents/<run_id>/`` — this
    helper returns the in-process summary so callers can fold scores
    into the provenance block.

    ``build_artifacts`` — when supplied (the post-synthesis enrichment
    pass), the judge prompt includes the deterministic-tool outputs so
    it can credit security / performance / governance axes for fields
    the enrichment fills in.
    """
    if not _judge_enabled():
        return None
    try:
        from fluid_build.copilot.agents.judge_agent import JudgeAgent

        result = JudgeAgent().judge(contract, build_artifacts=build_artifacts)
        if logger:
            logger.info("Judge score: %s/30 (%s)", result.total, result.model)
        return {
            "score": result.total,
            "axes": {axis: score.score for axis, score in result.axes.items()},
            "model": result.model,
        }
    except Exception as exc:  # noqa: BLE001 — fail-open
        if logger:
            logger.debug("Judge pass failed (skipping): %s", exc)
        return None


def _enrich_contract(
    contract: Dict[str, Any],
    logger: Optional[logging.Logger] = None,
) -> Optional[Dict[str, Any]]:
    """Run the post-synthesis deterministic enrichment pass (fail-open).

    Delegates to :func:`fluid_build.copilot.enrichment.enrich_contract`,
    which executes the three Wave 2 tools (dbt tests, freshness,
    physical layout) and writes artifacts to
    ``.fluid/agents/<run_id>/enrichment/``. Returns the in-process dict
    so the caller can pass it to :func:`_judge_contract` as
    ``build_artifacts``.

    Returns ``None`` when enrichment is disabled or fails.
    """
    try:
        from fluid_build.copilot.enrichment import enrich_contract

        return enrich_contract(contract, logger=logger)
    except Exception as exc:  # noqa: BLE001 — fail-open
        if logger:
            logger.debug("Enrichment pass failed (skipping): %s", exc)
        return None


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


def _format_seed_mismatch_for_repair(mismatch: Mapping[str, Any]) -> str:
    """Compact repair feedback for a single ground-truth seed mismatch.

    Pre-fix, the feedback dumped the entire seed list AND the entire
    candidate list per mismatch — ``seed=[{name: ..., quality: [...]},
    ...]; candidate=[{...}]`` produced ~1.5KB per mismatch, ~12KB
    total for an 8-mismatch payload. Live testing with Gemini Flash
    showed that on the third repair attempt the model gave up and
    returned prose ("parse error - Response did not contain a valid
    JSON object"). The verbose feedback was the proximate cause.

    For schema-list mismatches (the overwhelmingly common shape) this
    helper extracts just the field-name diff — the actionable signal
    — and drops the field-level quality/passthrough payload. Other
    mismatch shapes get a value-truncated fallback.
    """
    path = mismatch.get("path", "<unknown>")
    seed = mismatch.get("seed")
    candidate = mismatch.get("candidate")

    # Schema-list mismatch — the renamed-fields case. Extract just the
    # ordered list of names from both sides; that's the load-bearing
    # diff the LLM needs to act on.
    if isinstance(seed, list) and isinstance(candidate, list):
        seed_names = [
            item.get("name") for item in seed if isinstance(item, Mapping) and item.get("name")
        ]
        candidate_names = [
            item.get("name") for item in candidate if isinstance(item, Mapping) and item.get("name")
        ]
        if seed_names or candidate_names:
            return (
                f"Seed schema mismatch at {path}: "
                f"seed field names={seed_names}, "
                f"candidate field names={candidate_names}. "
                f"DO NOT RENAME — restore the seed names exactly."
            )

    # Fallback for non-schema-list mismatches — truncate so a single
    # huge value doesn't dominate the repair context.
    def _trunc(value: Any, *, limit: int = 200) -> str:
        text = repr(value)
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    return (
        f"Seed ground-truth violation at {path}: "
        f"seed={_trunc(seed)}, candidate={_trunc(candidate)}"
    )


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

    # Phase 7 — structural-seed wiring. When ``fluid forge --seed-from``
    # was used, the FLUID skeleton from the ODCS/ODPS document IS the
    # seed the LLM should augment. Override ``seed_contract`` (which was
    # built from templates/discovery) with the actual seed so the user
    # prompt's seed slot shows the structure to preserve, not a template
    # suggestion. Without this override, the system prompt instruction
    # added below talks about a seed the LLM never sees → it hallucinates
    # an unrelated contract or fails to produce a valid one.
    _structural_seed = context.get("structural_seed") if isinstance(context, Mapping) else None
    if _structural_seed is not None and getattr(_structural_seed, "fluid", None):
        seed_contract = _structural_seed.fluid

    attempts: List[GenerationAttemptReport] = []
    previous_errors: List[str] = []
    previous_payload: Optional[Dict[str, Any]] = None

    # Phase 7 (H + I) — structural-seed ground-truth contract. When the
    # user passed ``fluid forge --seed-from``, we prepend a precedence
    # rule to the system prompt so the LLM knows seed-derived
    # schema/quality/qos are immutable, and intent / context-derived
    # builds/executes/governance are the LLM's job. The post-validation
    # guard below enforces it; the prompt addition just gives the model
    # the chance to comply on the first attempt.
    _seed_ground_truth_extension = ""
    if _structural_seed is not None:
        _seed_exposes = (
            (_structural_seed.fluid or {}).get("exposes") or []
            if hasattr(_structural_seed, "fluid")
            else []
        )
        _seed_ground_truth_extension = (
            "\n\nSTRUCTURAL-SEED GROUND-TRUTH CONTRACT (from --seed-from):\n"
            "An ODCS/Bitol-ODPS document was supplied as the structural seed for "
            f"this generation. The following {len(_seed_exposes)} expose(s) "
            "MUST be preserved verbatim from the seed:\n"
            "  - schema (field names, types, required flags, primaryKey, "
            "classification)\n"
            "  - quality / validation rules\n"
            "  - qos / SLA expectations\n\n"
            "CRITICAL — DO NOT RENAME FIELDS. Copy every field name from the "
            "seed exactly, character-for-character. NEVER substitute "
            "'better-sounding' synonyms (e.g. ``order_date`` → "
            "``order_completion_date`` is a violation; ``amount`` → "
            "``order_item_price`` is a violation). The same rule applies to "
            "expose names, contract field names, and column names — they are "
            "load-bearing identifiers, not labels you can improve. If you think "
            "the seed name is poor, leave it alone — the post-validation guard "
            "WILL reject any rename and you'll have to redo the whole "
            "generation.\n\n"
            "PRECEDENCE: when intent or context conflicts with seed schema/qos, "
            "the SEED WINS. Intent steers builds, executes, governance, and any "
            "field the seed doesn't model — never schema/quality/qos. A "
            "post-validation guard runs after generation; mismatches against "
            "the seed are surfaced as repair feedback and the loop retries.\n"
        )

    for attempt_index in range(1, max_attempts + 1):
        system_prompt = build_system_prompt(capabilities) + _seed_ground_truth_extension
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
        # Self-healing (Phase 3 / Gap #2 wiring): run the JSON-schema
        # validator on the LLM's contract output so repair attempts get
        # ALL schema violations, not just the ones validate_generated_result
        # flags. Schema errors get prepended to validation_errors so the
        # corrective-feedback loop sees them — and the next-attempt
        # prompt carries the prescriptive
        # ``build_schema_validation_message`` body that asks the LLM to
        # match field names + enums exactly.
        try:
            from fluid_build.schema_manager import FluidSchemaManager

            schema_result = FluidSchemaManager().validate_contract(normalized.get("contract") or {})
            if not schema_result.is_valid:
                schema_errors = [f"Schema validation: {e}" for e in (schema_result.errors or [])]
                validation_errors = schema_errors + list(validation_errors)
        except Exception as exc:  # noqa: BLE001 — never block on validator failure
            logger.debug("self_healing_schema_validate_failed: %s", exc)
        # Phase 7 — structural-seed ground-truth guard. When the user
        # passed ``fluid forge --seed-from`` (an ODCS contract or Bitol
        # ODPS product), the SeedResult was loaded by forge_modes and
        # stashed on context. The schema/quality/qos in the seed are
        # treated as ground truth; the LLM may augment but not mutate
        # them. If the validated payload diverges from those ground-truth
        # paths, the mismatch report is fed into the repair loop as
        # validation errors so the next attempt sees the precise paths
        # that need to revert to the seed.
        structural_seed = context.get("structural_seed") if isinstance(context, Mapping) else None
        if structural_seed is not None and not validation_errors:
            try:
                from fluid_build.cli.forge_copilot_seed import diff_against_seed

                mismatches = diff_against_seed(structural_seed, normalized.get("contract") or {})
                if mismatches:
                    seed_errors = [_format_seed_mismatch_for_repair(m) for m in mismatches[:8]]
                    validation_errors = list(seed_errors) + list(validation_errors)
                    report.validation_errors = validation_errors
            except Exception as exc:  # noqa: BLE001
                # Loud-fail: the seed guard is what stops silent mutation
                # of the user's ground-truth contract. If it crashes we
                # need to know — otherwise an LLM mutation could ship
                # without the guard ever firing.
                logger.warning("structural_seed_guard_failed (mutation may not be caught): %s", exc)

            # Phase 7 (F2) — post-generation ODCS round-trip guarantee.
            # Beyond the ground-truth diff (which catches mutation of specific
            # paths), confirm the generated FLUID still re-exports to an ODCS
            # document structurally equal to the seed's ODCS shape. Catches
            # subtle losses (e.g., the LLM kept the field but reordered into a
            # different schema object) that the path-diff misses.
            if not validation_errors:
                try:
                    from fluid_build.providers.odcs import OdcsProvider

                    odcs_prov = OdcsProvider()
                    fluid_dict = normalized.get("contract") or {}
                    for expose in fluid_dict.get("exposes") or []:
                        if not isinstance(expose, Mapping):
                            continue
                        eid = expose.get("exposeId") or expose.get("id")
                        if not eid:
                            continue
                        # Render this expose's ODCS; the import → re-render
                        # should be idempotent given the lossless-round-trip
                        # contract. Failure here = either a mapper regression
                        # or the LLM produced an expose that can't round-trip.
                        try:
                            odcs = odcs_prov.render(fluid_dict, expose_id=eid)
                            rt = odcs_prov.roundtrip_check(odcs)
                            if not rt["equal"]:
                                validation_errors.append(
                                    f"Round-trip guarantee broken for expose '{eid}': "
                                    f"missing={len(rt['missing'])} "
                                    f"extra={len(rt['extra'])} "
                                    f"changed={len(rt['changed'])}"
                                )
                        except Exception as exc:  # noqa: BLE001 — surface per-expose only
                            validation_errors.append(
                                f"Round-trip render failed for expose '{eid}': "
                                f"{type(exc).__name__}: {str(exc)[:120]}"
                            )
                    if validation_errors:
                        report.validation_errors = validation_errors
                except Exception as exc:  # noqa: BLE001
                    # Same loud-fail rationale as the seed guard above.
                    logger.warning("structural_seed_roundtrip_guarantee_failed: %s", exc)

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

            enrichment_artifacts = _enrich_contract(normalized["contract"], logger=logger)
            judge_result = _judge_contract(
                normalized["contract"],
                logger=logger,
                build_artifacts=enrichment_artifacts,
            )

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
                "judge_score": judge_result.get("score") if judge_result else None,
                "judge_axes": judge_result.get("axes") if judge_result else None,
                "judge_model": judge_result.get("model") if judge_result else None,
                "enrichment_applied": enrichment_artifacts is not None,
                # Gap 5 — stash the artifact dict so the apply pass in
                # _template_mode can find it without re-running the tools.
                "enrichment_artifacts": enrichment_artifacts,
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

        # Self-healing: build a prescriptive repair message when ANY
        # schema-validation error fired this attempt. The message tells
        # the LLM exactly which JSON paths failed and asks for a re-emit
        # with the existing seed_contract shape preserved.
        schema_only_errors = [e for e in validation_errors if e.startswith("Schema validation:")]
        try:
            from fluid_build.cli.forge_copilot_corrective_feedback import (
                _parse_additional_props_error,
                build_schema_validation_message,
                strip_additional_props_from_contract,
            )

            if schema_only_errors:
                schema_msg = build_schema_validation_message(schema_only_errors)
                if schema_msg.get("content"):
                    if logger:
                        logger.info(
                            "self_healing_repair: attempt %d/%d, %d schema error(s)",
                            attempt_index,
                            max_attempts,
                            len(schema_only_errors),
                        )
                    # Last-resort: on the final attempt, if EVERY remaining
                    # schema error is an additionalProperties violation we can
                    # parse, strip the offending keys programmatically and
                    # re-validate immediately instead of spending another LLM
                    # call on a prompt the model has already ignored twice.
                    # Mixed errors (missing required fields, type mismatches)
                    # are left to the LLM — stripping alone can't fix those.
                    if attempt_index == max_attempts:
                        all_additional_props = all(
                            _parse_additional_props_error(e) is not None for e in schema_only_errors
                        )
                        non_schema_errors = [
                            e for e in validation_errors if not e.startswith("Schema validation:")
                        ]
                        # non_schema_errors may duplicate the schema errors but without the
                        # "Schema validation: " prefix (validate_generated_result re-runs the
                        # same checks). Only block stripping when there are genuinely different
                        # errors (e.g. missing required fields, type mismatches).
                        non_schema_non_additional_props = [
                            e for e in non_schema_errors if _parse_additional_props_error(e) is None
                        ]
                        if all_additional_props and not non_schema_non_additional_props:
                            raw_contract = normalized.get("contract") or {}
                            patched_contract, stripped_log = strip_additional_props_from_contract(
                                raw_contract, schema_only_errors
                            )
                            if stripped_log:
                                if logger:
                                    logger.warning(
                                        "self_healing_strip_keys: last-resort removed %d "
                                        "additionalProperties key(s): %s",
                                        len(stripped_log),
                                        ", ".join(stripped_log),
                                    )
                                # Re-validate the stripped contract.
                                try:
                                    from fluid_build.schema_manager import FluidSchemaManager

                                    strip_result = FluidSchemaManager().validate_contract(
                                        patched_contract
                                    )
                                    if strip_result.is_valid:
                                        # Patch the normalized dict and return success.
                                        normalized = dict(normalized)
                                        normalized["contract"] = patched_contract
                                        report.validation_warnings.append(
                                            f"self_healing_strip: removed {stripped_log}"
                                        )
                                        validation_errors = []
                                        # Fall through to the success return below.
                                except Exception as strip_exc:  # noqa: BLE001
                                    if logger:
                                        logger.debug(
                                            "self_healing_strip_revalidate_failed: %s", strip_exc
                                        )

                    if not validation_errors:
                        # The stripped contract is valid — return it directly.
                        eval_result = None
                        provenance = {
                            "llm_provider": llm_config.provider,
                            "llm_model": llm_config.model,
                            "ai_run_plan": ai_run_plan,
                            "system_prompt_hash": hashlib.sha256(
                                system_prompt.encode()
                            ).hexdigest()[:16],
                            "user_prompt_hash": hashlib.sha256(user_prompt.encode()).hexdigest()[
                                :16
                            ],
                            "discovery_hash": hashlib.sha256(
                                json.dumps(
                                    discovery_report.to_prompt_payload(), sort_keys=True
                                ).encode()
                            ).hexdigest()[:16],
                            "attempt": attempt_index,
                            "self_eval_score": None,
                            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                            "strip_repair": True,
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

                    previous_errors = build_structured_repair_feedback(validation_errors) + [
                        schema_msg["content"]
                    ]
                    previous_payload = payload
                    continue
        except Exception:  # noqa: BLE001
            pass
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
    # Pause/resume wiring (S3) — stamp the resume id from context onto
    # the session BEFORE the coordinator runs. Without this the
    # coordinator's ``_resolve_run_id`` mints a fresh id and the
    # ``skip_if_done`` blocks never find the cached stages from a
    # prior run. ``context["_resume_run_id"]`` is set in
    # ``forge_modes.run_ai_copilot_mode`` (either the explicit --resume
    # id or the freshly-minted id for this run); see also S2.
    _resume_id = context.get("_resume_run_id") if isinstance(context, Mapping) else None
    if _resume_id:
        try:
            session.run_id = str(_resume_id)
        except Exception:  # noqa: BLE001 — defensive
            pass

        # If the run is actually resuming (cache has stages), surface
        # the trust-building "Stage N/8: <name> ← cached, saved $X"
        # summary now so the user sees what's about to be skipped
        # BEFORE the staged work starts. Uses the shared
        # ``StageProgressFormatter`` so the rendering matches the
        # ``fluid agents show`` post-hoc view.
        try:
            from fluid_build.copilot.checkpoint import (
                STAGE_NAMES as _STAGE_NAMES,
            )
            from fluid_build.copilot.checkpoint import (
                get_default_saver as _get_saver,
            )
            from fluid_build.copilot.checkpoint_progress import (
                StageProgressFormatter as _Formatter,
            )

            _saver = _get_saver(workspace_root=Path.cwd())
            _existing = _saver.list_stages(str(_resume_id))
            if _existing:
                _fmt = _Formatter(use_rich=False)
                _total = len(_STAGE_NAMES)
                _lines = [
                    _fmt.render_resume_header(
                        str(_resume_id),
                        f"{len(_existing)} of {_total} cached",
                    )
                ]
                for _idx, _rec in enumerate(_existing, start=1):
                    _lines.append(
                        _fmt.render_stage_line(
                            _rec.stage,
                            "cached",
                            index=_idx,
                            total=_total,
                            saved_usd=float(_rec.cost_usd or 0.0),
                        )
                    )
                _msg = "\n".join(_lines)
                # Emit through the logger so tests can capture it and
                # the rich-render path picks it up via the standard
                # forge UI handler.
                if logger:
                    logger.info("resume_progress:\n%s", _msg)
        except Exception as _resume_ui_exc:  # noqa: BLE001 — UI is best-effort
            if logger:
                logger.debug("resume_progress_render_failed: %s", _resume_ui_exc)

    coordinator = StageCoordinator()
    staged_engine = _resolve_staged_engine(
        context,
        capabilities,
        project_memory=project_memory,
        team_memory=team_memory,
    )

    source = _select_staged_source(context, discovery_report, workspace_root=Path.cwd().resolve())
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
    # SECURITY: the staged path's additional_files derive from LLM-chosen
    # table names (hub/satellite/link/fact/dim) that become physical file
    # paths. The standard AI path runs every model-proposed file map
    # through ``sanitize_additional_files``; the staged path previously did
    # not, leaving a prompt-injection → arbitrary-file-write hole (a name
    # like ``../../../../tmp/pwned`` escaped target_dir). Mirror the
    # standard path here so absolute / parent-traversal / unsafe-extension
    # entries are dropped before they reach the write loop.
    additional_files = sanitize_additional_files(additional_files)
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


# Module-scoped guard so the "backend set but coordinator off" warning
# fires exactly once per process — repeated calls to
# ``_should_use_staged_copilot`` (every retry attempt, every interview
# refresh) must not spam the operator.
_STAGED_COPILOT_BACKEND_WARNING_EMITTED = False


def _reset_staged_copilot_warning() -> None:
    """Test-only: reset the once-per-process backend-warning latch."""
    global _STAGED_COPILOT_BACKEND_WARNING_EMITTED
    _STAGED_COPILOT_BACKEND_WARNING_EMITTED = False


def _maybe_warn_inactive_staged_coordinator() -> None:
    """Warn once when a non-file store backend is configured but the
    staged coordinator is inactive.

    Symptom this guards against (MEMORY-E2E-A finding #53): operators
    export ``FLUID_STORE_BACKEND=postgres`` (or sqlite/vector) but the
    default forge flow stays on the legacy ``CopilotEngine`` codepath,
    so the staged ``StageCoordinator`` checkpoints never fire and the
    configured backend silently does nothing. We prefer the warning
    path over auto-flipping a default — the staged coordinator is a
    behavioural change, not a transparent swap.
    """
    global _STAGED_COPILOT_BACKEND_WARNING_EMITTED
    if _STAGED_COPILOT_BACKEND_WARNING_EMITTED:
        return
    backend_raw = os.environ.get("FLUID_STORE_BACKEND")
    if not backend_raw:
        return
    backend = backend_raw.strip().lower()
    # ``file`` is the default; ``null``/``none``/``0``/``disabled`` all
    # map to the no-op backend in ``resolve_store`` — no point warning
    # about either since neither persists anything anyway.
    if backend in {"", "file", "null", "none", "0", "disabled"}:
        return
    _STAGED_COPILOT_BACKEND_WARNING_EMITTED = True
    message = (
        "Store backend %r configured but staged coordinator is inactive "
        "(only DDL/intent/data-model-driven flows trigger it). Set "
        "FLUID_FORGE_STAGED_COPILOT=1 to activate, OR use a DDL/intent/source "
        "input that triggers it automatically. Episodic / semantic / audit "
        "writers will not fire and the backend will receive no traffic."
    )
    LOG.warning(message, backend)


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
    if discovery_report.user_data_models:
        return True
    # Stayed legacy AND a non-file backend is configured — flag the
    # silent-no-op trap (finding #53). Only emit when the staged path
    # would NOT have fired, so we don't double-up on the active path.
    _maybe_warn_inactive_staged_coordinator()
    return False


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


# SECURITY_REVIEW I6: cap on any file read off an LLM-/discovery-derived
# path — mirrors the 50 MiB ceiling used by ``_dispatch_forge_data_model``
# (finding I3) so a crafted huge file can't exhaust memory.
_STAGED_SOURCE_MAX_BYTES = 50 * 1024 * 1024


def _confine_to_workspace(path: Path, workspace_root: Path) -> Optional[Path]:
    """Resolve ``path`` and confine it under ``workspace_root``.

    SECURITY_REVIEW I6: ``_select_staged_source`` consumes CLI- and
    discovery-derived paths and reads them with ``read_text()``. Without
    confinement an attacker-influenced ``data_model_paths`` value (or a
    poisoned discovery record) could steer a read at ``~/.aws/credentials``
    / ``/etc/passwd`` etc. Returns the resolved path when it lives inside
    the workspace and is a real, size-bounded file; ``None`` otherwise so
    the caller skips it.
    """
    try:
        resolved = path.expanduser().resolve()
        resolved.relative_to(workspace_root)
    except (ValueError, OSError):
        return None
    try:
        if not resolved.is_file():
            return None
        if resolved.stat().st_size > _STAGED_SOURCE_MAX_BYTES:
            return None
    except OSError:
        return None
    return resolved


def _select_staged_source(
    context: Mapping[str, Any],
    discovery_report: DiscoveryReport,
    *,
    workspace_root: Optional[Path] = None,
) -> Dict[str, Any]:
    from fluid_build.forge_datamodel.from_ddl.parser import parse_ddl_text

    # SECURITY_REVIEW I6: every path read below is confined under this
    # root. Defaults to cwd — the same workspace root the staged
    # ``StageSession`` uses.
    root = (workspace_root or Path.cwd()).resolve()

    raw_source = str(context.get("data_model_source") or "").strip().lower()
    explicit_paths = context.get("data_model_paths") or context.get("data_model_path") or []
    if isinstance(explicit_paths, str):
        explicit_paths = [segment for segment in explicit_paths.split() if segment]
    explicit_paths = [str(path) for path in explicit_paths]

    if raw_source == "intent" and explicit_paths:
        candidate = _confine_to_workspace(Path(explicit_paths[0]), root)
        if candidate is not None:
            return {"kind": "intent_file", "path": str(candidate)}

    ddl_candidates: List[Path] = []
    if raw_source == "ddl":
        for raw_path in explicit_paths:
            confined = _confine_to_workspace(Path(raw_path), root)
            if confined is not None:
                ddl_candidates.append(confined)
    if not ddl_candidates:
        for model in getattr(discovery_report, "user_data_models", []) or []:
            raw_path = Path(str(model.get("path") or ""))
            if raw_path.suffix.lower() != ".sql":
                continue
            confined = _confine_to_workspace(raw_path, root)
            if confined is not None:
                ddl_candidates.append(confined)
        for sql_file in discovery_report.sql_files:
            confined = _confine_to_workspace(Path(str(sql_file.get("path") or "")), root)
            if confined is not None:
                ddl_candidates.append(confined)

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


# ``suggest_scaffold`` + ``_build_scaffold_decision`` +
# ``validate_and_repair`` — physically extracted to
# ``cli/_forge_copilot_runtime_repair.py`` (~360 LOC). Re-exported
# here so existing import sites keep resolving without breakage.
from fluid_build.cli._forge_copilot_runtime_repair import (  # noqa: E402,F401
    _build_scaffold_decision,
    suggest_scaffold,
    validate_and_repair,
)
