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

# ruff: noqa: F821 — this helper resolves host-module symbols
# (LlmConfig, BUILTIN_LLM_PROVIDERS, etc.) at call-time via a _host()
# indirection accessor; ruff cannot statically see those bindings.
"""Scaffold-decision builder + ``validate_and_repair`` LLM loop.

Lifted from ``cli/forge_copilot_runtime.py`` (host file was 1590
LOC). ~360 LOC of post-generation logic:

* :func:`suggest_scaffold` / :func:`_build_scaffold_decision` —
  heuristic template/provider picks for the LLM seed prompt.
* :func:`validate_and_repair` — public helper that validates a
  contract and feeds errors back to the LLM for one or more
  repair turns. Used by the migration verb and CI checks that
  need a single-shot "validate this and fix it if you can".

``forge_copilot_runtime.py`` re-imports each public symbol at module
top so existing call sites keep resolving.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Mapping, Optional

from fluid_build.cli.forge_copilot_contract_helpers import (
    normalize_provider_name,
    normalize_template_name,
)
from fluid_build.cli.forge_copilot_discovery import DiscoveryReport


# Resolve ``COPILOT_BUILTIN_PROVIDERS`` lazily to avoid a top-level
# circular import; the constant lives on the host module.
def _builtin_providers():
    from fluid_build.cli import forge_copilot_runtime as _host

    return _host.COPILOT_BUILTIN_PROVIDERS


# Module-level proxy so the bare name resolves at module-attr-access
# time. Bare-name lookups inside functions still need the explicit
# resolution above; we expose this for tests that read the name off
# the module.
def __getattr__(name: str):
    if name == "COPILOT_BUILTIN_PROVIDERS":
        return _builtin_providers()
    if name == "ScaffoldDecisionReport":
        from fluid_build.cli import forge_copilot_runtime as _host

        return _host.ScaffoldDecisionReport
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from fluid_build.cli.forge_copilot_llm_providers import (
    CopilotGenerationError,
    LlmConfig,
    call_llm,
    get_llm_provider,
)
from fluid_build.cli.forge_copilot_memory import CopilotMemorySnapshot
from fluid_build.schema_manager import FluidSchemaManager

LOG = logging.getLogger("fluid.cli.forge_copilot.repair")


# (the earlier ``__getattr__`` above already covers
# ``ScaffoldDecisionReport`` and ``COPILOT_BUILTIN_PROVIDERS``)


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
        else (sorted(available_providers)[0] if available_providers else _builtin_providers()[0])
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

    # ``ScaffoldDecisionReport`` lives on the host module (the public
    # dataclass surface). Resolve at construction time so the
    # extracted function doesn't need a top-level circular import.
    from fluid_build.cli import forge_copilot_runtime as _host

    return _host.ScaffoldDecisionReport(
        template=template,
        provider=provider,
        template_source=template_source,
        provider_source=provider_source,
        template_reason=template_reason,
        provider_reason=provider_reason,
    )


# ---------------------------------------------------------------------------
# Public self-healing helper (Phase 1.1 of the world-class plan)
# ---------------------------------------------------------------------------
#
# ``generate_copilot_artifacts`` runs an end-to-end author-then-validate
# loop where the LLM produces a fresh contract and the loop repairs
# it on schema errors. Operators occasionally need the repair half on
# its own — they have a contract that's almost-right (hand-edited,
# imported from ODCS, post-migration), and they want the LLM to fix
# the last few schema issues without re-authoring from scratch.
#
# ``validate_and_repair`` is that operator-facing helper. It reuses
# every piece of the existing repair toolchain:
#   * ``FluidSchemaManager.validate_contract`` for schema checks
#   * ``_harmonise_agent_policy_inplace`` for the canReason↔reasoning
#     contradiction (deterministic, cheap, runs first)
#   * ``build_schema_validation_message`` for the prescriptive repair
#     prompt the inner loop uses
#   * ``call_llm`` (the litellm-backed path) for provider routing
#
# Returns ``(final_contract, attempt_log)``. The attempt log is a
# human-readable list of what each pass did — "validated clean",
# "schema errors: ...", "repair attempt 1 fixed N issues" — useful
# for surface in CLI output and for tests pinning the loop's
# behaviour.


def validate_and_repair(
    contract: Dict[str, Any],
    *,
    llm: Optional[LlmConfig] = None,
    max_attempts: int = 3,
    logger: Optional[logging.Logger] = None,
) -> tuple[Dict[str, Any], List[str]]:
    """Validate ``contract``; if it fails, ask the LLM to fix it.

    Parameters
    ----------
    contract:
        A contract dict. May be fresh-from-LLM, hand-edited, or imported
        from another standard. Mutated in place by the deterministic
        normalisers; the LLM repair path produces a new dict.
    llm:
        Optional ``LlmConfig``. When omitted, only the deterministic
        normalisers run — useful for offline / blank-mode use cases
        where the contract should already be valid after normalisation.
    max_attempts:
        Cap on LLM repair turns. Default 3 matches
        ``generate_copilot_artifacts``.
    logger:
        Optional logger for attempt-by-attempt diagnostics.

    Returns
    -------
    (final_contract, attempt_log):
        ``final_contract`` is the most-recent contract dict. ``attempt_log``
        is the list of human-readable status strings, one per pass.

    Raises
    ------
    CopilotGenerationError("validate_and_repair_exhausted")
        When ``max_attempts`` LLM passes still produce an invalid
        contract. The error's ``context`` carries the last-attempt
        validation errors so callers can surface them.
    """
    from fluid_build.cli.forge_copilot_contract_helpers import (
        _harmonise_agent_policy_inplace,
        extract_json_object,
    )
    from fluid_build.cli.forge_copilot_corrective_feedback import (
        build_schema_validation_message,
    )

    log = logger or LOG
    attempt_log: List[str] = []
    work = copy.deepcopy(contract)

    # Deterministic normalisers — cheap, safe, and idempotent. Run
    # before any LLM call so we don't waste a turn on trivially-fixable
    # contradictions like canReason=false but reasoning in allowedUseCases.
    _harmonise_agent_policy_inplace(work)

    schema_manager = FluidSchemaManager()

    def _validate(c: Dict[str, Any]) -> tuple[bool, List[str]]:
        result = schema_manager.validate_contract(c)
        return bool(getattr(result, "is_valid", False)), list(getattr(result, "errors", []) or [])

    valid, errors = _validate(work)
    if valid:
        attempt_log.append("validated clean (no LLM repair needed)")
        return work, attempt_log
    attempt_log.append(f"initial validation failed: {len(errors)} error(s)")

    if llm is None:
        # No LLM available — surface the errors and let the caller
        # decide. Same shape as the LLM-exhausted error so callers
        # have one matcher.
        raise CopilotGenerationError(
            "validate_and_repair_no_llm",
            "Contract failed schema validation and no LlmConfig was provided for repair.",
            suggestions=[
                "Pass an llm=LlmConfig(...) to validate_and_repair to "
                "enable the self-healing repair loop.",
                "Or fix the listed errors by hand and re-run validate.",
                *errors[:3],
            ],
            context={"validation_errors": errors[:10]},
        )

    provider = get_llm_provider(llm.provider)
    system_prompt = (
        "You are a FLUID contract repair assistant. The user gives "
        "you a contract that fails schema validation and the list of "
        "errors. You return ONLY the fixed contract as a single JSON "
        "object, with no surrounding text and no markdown fences. "
        "Preserve every field that is not implicated in the error "
        "list. Match every field name and enum value exactly as the "
        "schema expects."
    )

    last_errors = errors
    for attempt in range(1, max_attempts + 1):
        repair_msg = build_schema_validation_message(last_errors)
        user_prompt = json.dumps(
            {
                "contract": work,
                "instruction": repair_msg.get("content", ""),
                "errors": last_errors[:30],
            },
            indent=2,
            sort_keys=True,
        )

        # Resolve ``call_llm`` via the canonical host namespace so
        # tests that ``patch("fluid_build.cli.forge_copilot_runtime.call_llm")``
        # flow through to this caller.
        from fluid_build.cli import forge_copilot_runtime as _host

        _call_llm = getattr(_host, "call_llm", call_llm)
        try:
            raw = _call_llm(provider, llm, system_prompt, user_prompt)
        except CopilotGenerationError as exc:
            attempt_log.append(f"attempt {attempt}/{max_attempts} aborted: {exc.event}")
            log.warning(
                "validate_and_repair_llm_failed: attempt=%d/%d event=%s",
                attempt,
                max_attempts,
                exc.event,
            )
            raise

        try:
            parsed = extract_json_object(raw)
        except ValueError as exc:
            attempt_log.append(f"attempt {attempt}/{max_attempts} parse error: {exc}")
            last_errors = [f"Repair output was not valid JSON: {exc}"]
            continue

        # Re-extract just the contract if the LLM nested it. Common
        # shape: ``{"contract": {...}}`` (matches the seed-contract
        # convention used elsewhere in the runtime).
        candidate = parsed.get("contract") if isinstance(parsed, dict) else None
        if isinstance(candidate, dict):
            work = candidate
        elif isinstance(parsed, dict):
            work = parsed

        # Re-run the deterministic normalisers — the LLM may have
        # re-introduced the canReason contradiction, and we'd rather
        # auto-fix it than burn another turn.
        _harmonise_agent_policy_inplace(work)

        valid, errors = _validate(work)
        if valid:
            attempt_log.append(f"attempt {attempt}/{max_attempts} repaired clean")
            return work, attempt_log
        attempt_log.append(
            f"attempt {attempt}/{max_attempts} still invalid: {len(errors)} error(s)"
        )
        last_errors = errors

    raise CopilotGenerationError(
        "validate_and_repair_exhausted",
        f"Contract still invalid after {max_attempts} repair attempts.",
        suggestions=[
            "Inspect the last-attempt errors and fix them by hand.",
            "Increase max_attempts if the errors are progressing each turn.",
            *last_errors[:3],
        ],
        context={
            "validation_errors": last_errors[:10],
            "attempt_log": attempt_log,
        },
    )
