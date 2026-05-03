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

"""Adaptive copilot interview orchestration for interactive forge sessions."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .forge_copilot_runtime import (
    DiscoveryReport,
    LlmConfig,
    build_clarification_system_prompt,
    build_clarification_user_prompt,
    call_llm,
    extract_json_object,
    get_llm_provider,
    normalize_provider_name,
)
from .forge_copilot_taxonomy import (
    USE_CASE_CHOICES,
    format_use_case_label,
    normalize_copilot_context,
    normalize_use_case,
)
from .forge_dialogs import (
    ask_dialog_question as ask_interview_question,
)
from .forge_dialogs import (
    ask_flexible_choice,
    ask_friendly_text,
    normalize_prompt_choices,
    resolve_choice_input,
)

INTERVIEW_MAX_ROUNDS = 3
INTERVIEW_MAX_QUESTIONS_PER_ROUND = 2
INTERVIEW_TRANSCRIPT_WINDOW = 6
KNOWN_SCHEDULER_ENGINES = ("airflow", "dagster", "prefect")

# Slice UX-I: the set of context slots that together are sufficient for
# the generation LLM to produce a defensible contract WITHOUT a
# clarification round.  When every slot in this tuple is already
# populated, ``is_context_sufficient`` returns True and the interview
# loop short-circuits the ``request_interview_decision`` LLM call — a
# ~5-10s saving per run for users whose context is already rich.
CONTEXT_SUFFICIENT_SLOTS: tuple[str, ...] = (
    "project_goal",
    "data_sources",
    "use_case",
)


def is_context_sufficient(context: Mapping[str, Any]) -> bool:
    """Return True when ``context`` already contains the minimum slots.

    The generation LLM can produce a defensible contract as long as it
    knows what the user is building (``project_goal``), where the data
    comes from (``data_sources``), and what shape the output should
    take (``use_case``).  Everything else has safe defaults or is
    inferred by the scaffold heuristics in
    ``forge_copilot_runtime._build_scaffold_decision``.

    Callers can force a clarification round regardless by setting the
    ``FLUID_COPILOT_FORCE_INTERVIEW=1`` environment variable; the
    interview loop reads that before consulting this helper.
    """
    if not isinstance(context, Mapping):
        return False
    for slot in CONTEXT_SUFFICIENT_SLOTS:
        value = context.get(slot)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
        if isinstance(value, (list, tuple, dict)) and len(value) == 0:
            return False
    return True


SOURCE_PRECEDENCE = {
    "default": 0,
    "clarifier": 1,
    "project_memory": 2,
    "discovery": 3,
    "interactive": 4,
    "explicit": 5,
}

SUMMARY_FIELDS = {
    "project_goal",
    "use_case",
    "use_case_other",
    "data_sources",
    "provider",
    "provider_hint",
    "domain",
    "canonical_model",
    "supporting_standards",
    "owner_team",
    "build_engine",
    "output_kind",
    "primary_entity",
    "primary_measures",
    "primary_dimensions",
    "time_dimension",
    "time_granularity",
    "refresh_cadence",
    "consumes",
    "ci_provider",
    "ci_complexity",
    "byot_path",
    "transformation_engine",
    "user_data_model",
    "data_model_source",
    "data_model_paths",
    "data_model_description",
    "review_data_model",
    "schedule_engine",
    "byos_path",
    "data_modeling_technique",
}

LIST_LIKE_FIELDS = {"primary_measures", "primary_dimensions", "supporting_standards"}
SCALAR_FIELDS = {
    "project_goal",
    "data_sources",
    "provider",
    "provider_hint",
    "domain",
    "canonical_model",
    "owner_team",
    "build_engine",
    "output_kind",
    "primary_entity",
    "time_dimension",
    "time_granularity",
    "refresh_cadence",
    "use_case_other",
    "ci_provider",
    "ci_complexity",
    "byot_path",
    "transformation_engine",
    "user_data_model",
    "data_model_source",
    "data_model_paths",
    "data_model_description",
    "review_data_model",
    "schedule_engine",
    "byos_path",
    "data_modeling_technique",
}

# Canonical values for the data_modeling_technique field. ``data_vault_2`` is
# the default because most of our demo customers standardize on DV2 raw vault
# before any dimensional layer; the interview lets the user pick ``dimensional``
# for the classic Kimball / star schema shape.
_DATA_VAULT_2_ALIASES = {
    "dv2",
    "dv 2",
    "dv2.0",
    "dv 2.0",
    "history",
    "change history",
    "audit history",
    "raw vault",
    "historical",
    "data vault",
    "data vault 2",
    "data vault 2.0",
    "datavault",
    "datavault2",
    "data_vault_2",
    "data-vault-2",
}
_DIMENSIONAL_ALIASES = {
    "dimensional",
    "dimensional modeling",
    "dim",
    "kimball",
    "reporting",
    "bi",
    "dashboard",
    "dashboards",
    "analytics",
    "star model",
    "star",
    "star schema",
}
_DATA_MODEL_SOURCE_ALIASES = {
    "ddl": "ddl",
    "sql": "ddl",
    "intent": "intent",
    "yaml": "intent",
    "samples": "samples",
    "sample": "samples",
    "chat": "chat",
    "describe": "chat",
    "blank": "blank",
    "none": "blank",
}


@dataclass
class InterviewTurn:
    """Single turn in the copilot interview transcript."""

    role: str
    content: str
    field: str = ""
    question_id: str = ""
    raw_input: str = ""
    resolved_value: Any = None
    resolution_status: str = ""

    def to_payload(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "field": self.field,
            "question_id": self.question_id,
            "raw_input": self.raw_input,
            "resolved_value": self.resolved_value,
            "resolution_status": self.resolution_status,
        }


@dataclass
class InterviewQuestion:
    """A question to present to the user during the adaptive interview."""

    id: str
    field: str
    prompt: str
    type: str = "text"
    choices: List[Dict[str, Any]] = dc_field(default_factory=list)
    required: bool = False
    allow_skip: bool = True
    default: Optional[str] = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "InterviewQuestion":
        field_name = str(
            payload.get("field") or payload.get("key") or payload.get("id") or ""
        ).strip()
        choices = normalize_prompt_choices(list(payload.get("choices") or []))
        if field_name == "use_case" and not choices:
            choices = list(USE_CASE_CHOICES)

        return cls(
            id=str(payload.get("id") or field_name or "question").strip(),
            field=field_name,
            prompt=str(payload.get("prompt") or payload.get("question") or "Tell me more.").strip(),
            type=str(payload.get("type") or "text").strip().lower(),
            choices=choices[: INTERVIEW_MAX_QUESTIONS_PER_ROUND * 4],
            required=bool(payload.get("required", False)),
            allow_skip=bool(payload.get("allow_skip", not payload.get("required", False))),
            default=str(payload.get("default") or "").strip() or None,
        )


@dataclass
class InterviewDecision:
    """LLM-generated decision: ask more questions or proceed to generation."""

    status: str
    reason: str = ""
    context_patch: Dict[str, Any] = dc_field(default_factory=dict)
    assumptions: List[str] = dc_field(default_factory=list)
    questions: List[InterviewQuestion] = dc_field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "InterviewDecision":
        status = str(payload.get("status") or "ready").strip().lower()
        if status not in {"ask", "ready"}:
            status = "ready"
        context_patch = payload.get("context_patch")
        if not isinstance(context_patch, Mapping):
            context_patch = {}
        assumptions = [
            str(item).strip()
            for item in (payload.get("assumptions") or [])
            if str(item or "").strip()
        ][:6]
        raw_questions = payload.get("questions") or []
        questions = [
            InterviewQuestion.from_payload(raw_question)
            for raw_question in raw_questions[:INTERVIEW_MAX_QUESTIONS_PER_ROUND]
            if isinstance(raw_question, Mapping)
        ]
        return cls(
            status=status,
            reason=str(payload.get("reason") or "").strip(),
            context_patch=dict(context_patch),
            assumptions=assumptions,
            questions=questions,
        )


@dataclass
class CopilotInterviewState:
    """Mutable state accumulated across the adaptive interview rounds."""

    normalized_context: Dict[str, Any] = dc_field(default_factory=dict)
    transcript: List[Dict[str, Any]] = dc_field(default_factory=list)
    answered_fields: set[str] = dc_field(default_factory=set)
    assumptions: List[str] = dc_field(default_factory=list)
    remaining_rounds: int = INTERVIEW_MAX_ROUNDS
    ready: bool = False
    field_sources: Dict[str, str] = dc_field(default_factory=dict)

    def apply_patch(self, patch: Mapping[str, Any], *, source: str) -> None:
        for key, raw_value in patch.items():
            normalized_value = normalize_interview_value(key, raw_value)
            if normalized_value in (None, "", [], {}):
                continue
            current_source = self.field_sources.get(key, "default")
            if SOURCE_PRECEDENCE.get(source, 0) < SOURCE_PRECEDENCE.get(current_source, 0):
                continue
            self.normalized_context[key] = normalized_value
            self.field_sources[key] = source
            if key in SUMMARY_FIELDS:
                self.answered_fields.add(key)
        self.normalized_context = normalize_copilot_context(self.normalized_context)

    def add_assumptions(self, values: List[str]) -> None:
        for value in values:
            if value and value not in self.assumptions:
                self.assumptions.append(value)

    def record_turn(
        self,
        *,
        role: str,
        content: str,
        field: str = "",
        question_id: str = "",
        raw_input: str = "",
        resolved_value: Any = None,
        resolution_status: str = "",
    ) -> None:
        text = str(content or "").strip()
        if not text:
            return
        turn = InterviewTurn(
            role=role,
            content=text,
            field=field,
            question_id=question_id,
            raw_input=str(raw_input or "").strip(),
            resolved_value=resolved_value,
            resolution_status=resolution_status,
        )
        self.transcript.append(turn.to_payload())
        self.transcript = self.transcript[-INTERVIEW_TRANSCRIPT_WINDOW:]

    def to_prompt_payload(self) -> Dict[str, Any]:
        return {
            "normalized_context": dict(self.normalized_context),
            "interview_summary": build_interview_summary_from_context(self.normalized_context),
            "answered_fields": sorted(self.answered_fields),
            "assumptions": list(self.assumptions),
            "remaining_rounds": self.remaining_rounds,
            "transcript": list(self.transcript[-INTERVIEW_TRANSCRIPT_WINDOW:]),
        }

    def finalize(self) -> Dict[str, Any]:
        final_context = normalize_copilot_context(dict(self.normalized_context))
        final_context["interview_summary"] = build_interview_summary_from_context(final_context)
        if self.assumptions:
            final_context["assumptions_used"] = list(self.assumptions)
        return final_context


def normalize_interview_value(field_name: str, value: Any) -> Any:
    """Coerce a raw interview answer into its canonical form for the given field."""
    key = str(field_name or "").strip()
    if key in {"provider", "provider_hint"}:
        text = str(value or "").strip()
        return normalize_provider_name(text) if text else None
    if key == "use_case":
        return normalize_use_case(value) or str(value or "").strip() or None
    if key == "consumes":
        return _normalize_consumes(value)
    if key == "data_modeling_technique":
        text = str(value or "").strip().lower()
        if text in _DATA_VAULT_2_ALIASES:
            return "data_vault_2"
        if text in _DIMENSIONAL_ALIASES:
            return "dimensional"
        # Accept the canonical values verbatim.
        if text in {"data_vault_2", "dimensional"}:
            return text
        return None
    if key == "data_model_source":
        text = str(value or "").strip().lower()
        return _DATA_MODEL_SOURCE_ALIASES.get(text) or None
    if key == "review_data_model":
        text = str(value or "").strip().lower()
        if text in {"yes", "y", "true", "1"}:
            return "true"
        if text in {"no", "n", "false", "0"}:
            return "false"
        return None
    if key in LIST_LIKE_FIELDS:
        return _listify_strings(value)
    if key in SCALAR_FIELDS:
        text = str(value or "").strip()
        return text or None
    return value


def _listify_strings(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").replace("\n", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _normalize_consumes(value: Any) -> Any:
    if isinstance(value, list):
        normalized = []
        for item in value:
            if isinstance(item, Mapping):
                product_id = str(item.get("productId") or item.get("product_id") or "").strip()
                expose_id = str(item.get("exposeId") or item.get("expose_id") or "").strip()
                if product_id and expose_id:
                    normalized.append({"productId": product_id, "exposeId": expose_id})
            elif str(item or "").strip():
                normalized.append(str(item).strip())
        return normalized
    if isinstance(value, Mapping):
        product_id = str(value.get("productId") or value.get("product_id") or "").strip()
        expose_id = str(value.get("exposeId") or value.get("expose_id") or "").strip()
        if product_id and expose_id:
            return [{"productId": product_id, "exposeId": expose_id}]
        return []
    text = str(value or "").strip()
    return [text] if text else []


def build_interview_summary_from_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Build a compact summary dict from the current interview context."""
    existing = context.get("interview_summary")
    if isinstance(existing, Mapping):
        return dict(existing)

    normalized = normalize_copilot_context(dict(context))
    answered_fields = sorted(key for key in SUMMARY_FIELDS if normalized.get(key))
    summary = {
        "project_goal": normalized.get("project_goal"),
        "use_case": normalized.get("use_case"),
        "use_case_other": normalized.get("use_case_other"),
        "use_case_label": format_use_case_label(
            normalized.get("use_case"), normalized.get("use_case_other")
        ),
        "data_sources": normalized.get("data_sources"),
        "provider_hint": normalized.get("provider") or normalized.get("provider_hint"),
        "domain": normalized.get("domain"),
        "canonical_model": normalized.get("canonical_model"),
        "supporting_standards": _listify_strings(normalized.get("supporting_standards")),
        "owner_team": normalized.get("owner_team") or normalized.get("owner"),
        "build_engine": normalized.get("build_engine"),
        "output_kind": normalized.get("output_kind"),
        "semantic_intent": {
            "primary_entity": normalized.get("primary_entity"),
            "primary_measures": _listify_strings(normalized.get("primary_measures")),
            "primary_dimensions": _listify_strings(normalized.get("primary_dimensions")),
            "time_dimension": normalized.get("time_dimension"),
            "time_granularity": normalized.get("time_granularity"),
        },
        "refresh_cadence": normalized.get("refresh_cadence"),
        "consumes": _normalize_consumes(normalized.get("consumes")),
        "assumptions": list(normalized.get("assumptions_used") or []),
        "answered_fields": answered_fields,
    }
    return summary


def bootstrap_interview_state(
    initial_context: Mapping[str, Any],
    *,
    discovery_report: DiscoveryReport,
    project_memory: Optional[Any] = None,
) -> CopilotInterviewState:
    """Create the initial interview state from explicit context, discovery, and memory."""
    state = CopilotInterviewState()
    state.apply_patch(normalize_copilot_context(dict(initial_context)), source="explicit")

    if not state.normalized_context.get("provider") and discovery_report.provider_hints:
        provider_hint = normalize_provider_name(discovery_report.provider_hints[0])
        if provider_hint:
            state.apply_patch({"provider": provider_hint}, source="discovery")

    if project_memory:
        if not state.normalized_context.get("provider") and getattr(
            project_memory, "preferred_provider", None
        ):
            state.apply_patch(
                {"provider": project_memory.preferred_provider}, source="project_memory"
            )
        if not state.normalized_context.get("domain") and getattr(
            project_memory, "preferred_domain", None
        ):
            state.apply_patch({"domain": project_memory.preferred_domain}, source="project_memory")
        if not state.normalized_context.get("owner_team") and getattr(
            project_memory, "preferred_owner", None
        ):
            state.apply_patch(
                {"owner_team": project_memory.preferred_owner}, source="project_memory"
            )
        memory_engines = list(getattr(project_memory, "build_engines", []) or [])
        if not state.normalized_context.get("build_engine") and memory_engines:
            state.apply_patch({"build_engine": memory_engines[0]}, source="project_memory")

    # Ensure ``data_modeling_technique`` always has a value so downstream
    # codepaths (prompt injection, engine fallback, validation guardrail)
    # can rely on it even when the interview is skipped entirely (non-
    # interactive / ``--no-interaction`` / piped stdin).  ``source="default"``
    # is the lowest precedence in SOURCE_PRECEDENCE, so any explicit
    # answer — bootstrap question, clarifier LLM, CLI flag — still wins.
    if not state.normalized_context.get("data_modeling_technique"):
        state.apply_patch({"data_modeling_technique": "data_vault_2"}, source="default")

    return state


def run_adaptive_copilot_interview(
    *,
    initial_context: Mapping[str, Any],
    console: Any,
    llm_config: LlmConfig,
    discovery_report: DiscoveryReport,
    capability_matrix: Mapping[str, Any],
    project_memory: Optional[Any] = None,
    previous_failure: Optional[List[str]] = None,
    target_dir: Optional[Path] = None,
    quiet: bool = False,
) -> CopilotInterviewState:
    """Run the multi-round adaptive interview, calling the LLM for dynamic questions.

    ``quiet`` is forwarded to ``print_v2_banner("init_copilot")`` so the
    ``fluid init copilot --quiet`` flag suppresses the banner consistently
    with every other forge surface. The env-var path
    (``FLUID_QUIET=1`` / ``FLUID_NONINTERACTIVE=1``) is honoured by
    ``forge_banner.banner_enabled`` regardless of this kwarg.
    """
    from .forge_ui import print_interview_phase

    state = bootstrap_interview_state(
        initial_context,
        discovery_report=discovery_report,
        project_memory=project_memory,
    )

    # Mode-aware short-circuits — the world-class fix for the user's
    # complaint that compose/refine were running the same full interview
    # as a fresh-product flow even though the system already had the
    # context. Each branch returns a state ready to feed the LLM with
    # only the user-specific delta.
    interview_mode = _detect_interview_mode(state.normalized_context)
    if interview_mode == "compose":
        if console:
            print_interview_phase(console, phase=1, total=1, label="Compose from upstream products")
        _run_compose_interview(state, console)
        state.ready = True
        return state
    if interview_mode == "refine":
        if console:
            print_interview_phase(console, phase=1, total=1, label="Refine existing contract")
        _run_refine_interview(state, console)
        state.ready = True
        return state

    # World-class bootstrap (Phase 0.6) — the only interview path.
    # Detect-first, examples in prompts, productType-first, progress
    # + cost, :auto escape, no redundant generic questions. The
    # opt-out (``FLUID_INTERVIEW_LEGACY=1``) was deleted as legacy.
    if console:
        print_interview_phase(console, phase=1, total=1, label="Detect-first, world-class")
    try:
        from fluid_build.cli._world_class_interview import (
            assess_coverage,
            run_world_class_bootstrap,
        )

        run_world_class_bootstrap(
            state=state,
            console=console,
            target_dir=target_dir,
            project_memory=project_memory,
        )
        # Schema-coverage gate: if anything required is missing,
        # fall through to the gap-filler bootstrap so the LLM still
        # sees a complete context.
        coverage = assess_coverage(state.normalized_context)
        if coverage.is_complete:
            state.ready = True
            return state
        # Otherwise fall through to the gap-filler questions for the
        # remaining required slots.
        if console:
            try:
                console.print(
                    f"[dim]Filling {len(coverage.missing)} remaining "
                    f"gap(s): {', '.join(coverage.missing[:3])}…[/dim]"
                )
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        LOG.debug("world_class_bootstrap_failed: %s", exc)

    if console:
        print_interview_phase(console, phase=1, total=3, label="Tell us about your project")
    _ask_bootstrap_questions(
        state,
        console,
        discovery_report=discovery_report,
        target_dir=target_dir,
    )
    try:
        from .forge_banner import print_v2_banner

        print_v2_banner("init_copilot", quiet=quiet)
    except Exception:  # noqa: BLE001
        pass

    # Slice UX-I: short-circuit the clarification LLM round if the
    # bootstrap questions + discovery + project memory already filled
    # in the minimum slots.  This saves one LLM call (~5-10s) per run
    # for users who answered the local bootstrap questions fully.
    # Users can force the old behaviour by setting
    # ``FLUID_COPILOT_FORCE_INTERVIEW=1`` in their environment.
    import os

    force_interview = bool(os.environ.get("FLUID_COPILOT_FORCE_INTERVIEW"))
    if (
        not force_interview
        and console
        and not previous_failure
        and is_context_sufficient(state.normalized_context)
    ):
        state.ready = True
        # Slice UX-L: mark the skip so the performance summary can
        # surface it.  The marker is carried on normalized_context
        # (which becomes the forge_modes context dict).
        state.normalized_context["_interview_skipped"] = True

    round_number = 0
    while console and state.remaining_rounds > 0 and not state.ready:
        decision = request_interview_decision(
            state,
            llm_config=llm_config,
            discovery_report=discovery_report,
            capability_matrix=capability_matrix,
            project_memory=project_memory,
            previous_failure=previous_failure,
        )
        if decision.reason:
            state.record_turn(role="assistant", content=decision.reason)
        state.apply_patch(decision.context_patch, source="clarifier")
        state.add_assumptions(decision.assumptions)
        if decision.status == "ready" or not decision.questions:
            state.ready = True
            break
        round_number += 1
        if console:
            label = (
                "Clarifying details"
                if round_number == 1
                else f"Clarifying details (round {round_number})"
            )
            print_interview_phase(console, phase=2, total=3, label=label)
        _ask_dynamic_questions(state, console, decision.questions)
        state.remaining_rounds -= 1
        if not decision.questions:
            break

    if console:
        print_interview_phase(console, phase=3, total=3, label="Building your contract")

    state.normalized_context = state.finalize()
    return state


def run_post_generation_clarification(
    state: CopilotInterviewState,
    *,
    console: Any,
    llm_config: LlmConfig,
    discovery_report: DiscoveryReport,
    capability_matrix: Mapping[str, Any],
    project_memory: Optional[Any] = None,
    failure_summary: Optional[List[str]] = None,
) -> CopilotInterviewState:
    """Run one extra clarification round after a generation failure."""
    if not console:
        return state
    decision = request_interview_decision(
        state,
        llm_config=llm_config,
        discovery_report=discovery_report,
        capability_matrix=capability_matrix,
        project_memory=project_memory,
        previous_failure=failure_summary,
    )
    if decision.reason:
        state.record_turn(role="assistant", content=decision.reason)
    state.apply_patch(decision.context_patch, source="clarifier")
    state.add_assumptions(decision.assumptions)
    if decision.status == "ask" and decision.questions:
        _ask_dynamic_questions(state, console, decision.questions)
    state.normalized_context = state.finalize()
    return state


def request_interview_decision(
    state: CopilotInterviewState,
    *,
    llm_config: LlmConfig,
    discovery_report: DiscoveryReport,
    capability_matrix: Mapping[str, Any],
    project_memory: Optional[Any] = None,
    previous_failure: Optional[List[str]] = None,
) -> InterviewDecision:
    """Call the LLM to decide whether to ask more questions or proceed.

    Slice UX-J: if ``llm_config`` carries a ``routing_model``, the
    clarification call uses the cheap/fast routing model instead of
    the strong generation model.  Interview planning is a low-stakes
    task (it only decides which questions to ask, not what the
    contract contains) so a ~3-10x cheaper model is usually fine.
    """
    # Slice UX-J: route the interview clarification to the cheap model.
    routing_config = llm_config.for_routing() if hasattr(llm_config, "for_routing") else llm_config

    system_prompt = build_clarification_system_prompt(capability_matrix)
    user_prompt = build_clarification_user_prompt(
        interview_state=state.to_prompt_payload(),
        discovery_report=discovery_report,
        capability_matrix=capability_matrix,
        project_memory=project_memory,
        previous_failure=previous_failure or [],
    )
    raw = call_llm(
        provider=get_llm_provider(routing_config.provider),
        config=routing_config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    payload = extract_json_object(raw)
    return InterviewDecision.from_payload(payload)


# ---------------------------------------------------------------------------
# Mode-aware interview short-circuits (compose / refine)
# ---------------------------------------------------------------------------


def _detect_interview_mode(context: Mapping[str, Any]) -> str:
    """Return ``compose`` / ``refine`` / ``standard`` based on context.

    *compose* — the runtime resolved upstream products (``--from-product``
    or the picker's compose flow) and stuck them under
    ``context["composition"]``.

    *refine* — the runtime loaded an existing contract via ``--refine``
    and put it under ``context["refine_existing_contract"]``.

    *standard* — anything else; run the full bootstrap interview.
    """
    if context.get("refine_existing_contract"):
        return "refine"
    composition = context.get("composition")
    if isinstance(composition, dict) and composition.get("upstream_products"):
        return "compose"
    return "standard"


def _run_compose_interview(state: "CopilotInterviewState", console: Any) -> None:
    """Compose-mode interview — 3 questions max.

    The system already has every upstream's schema, productType, exposes,
    and domain. Asking "do you have a data model?" or "got sample data?"
    in this state is nonsense — the data model IS the upstreams. So we
    skip the full bootstrap and ask only what we don't know:

    1. **Goal** — one sentence on what this product is for.
    2. **Type** — ADP (Silver) or CDP (Gold). Default ADP unless the
       user already passed ``--data-product-type``.
    3. **Join keys** — pre-suggested from the schema overlap; Enter to
       accept, free-text to override.

    Domain defaults to the most-common upstream domain. Owner/team
    falls through to existing project memory if available.
    """
    from .forge_dialogs import ask_friendly_text

    composition = state.normalized_context.get("composition") or {}
    upstreams = list(composition.get("upstream_products") or [])
    target_type = (
        composition.get("target_type") or state.normalized_context.get("data_product_type") or "ADP"
    )

    # Render a cheat-sheet panel so the user sees what's already known.
    if console:
        try:
            from rich.panel import Panel as _Panel
            from rich.table import Table as _Table

            table = _Table.grid(padding=(0, 2))
            table.add_column(style="dim cyan")
            table.add_column()
            table.add_column(style="cyan")
            for u in upstreams:
                cols = sum(len((ex or {}).get("schema") or []) for ex in (u.get("exposes") or []))
                table.add_row(
                    u.get("productType", "?"),
                    u.get("name") or u.get("id", "?"),
                    f"{cols} cols · {u.get('domain', '-')}",
                )
            console.print(
                _Panel(
                    table,
                    title=(
                        f"[bold]Composing from {len(upstreams)} upstream "
                        f"product{'s' if len(upstreams) != 1 else ''}[/bold]"
                    ),
                    subtitle=f"[dim]target: {target_type}[/dim]",
                    border_style="cyan",
                )
            )
        except Exception:  # noqa: BLE001
            pass

    # Q1 — goal (only ask if not already filled by --context)
    if not state.normalized_context.get("project_goal"):
        goal = ask_friendly_text(
            console,
            "What's the goal of this composition? (one sentence)",
            required=True,
        )
        if goal:
            state.apply_patch({"project_goal": goal}, source="interactive")
            state.record_turn(
                role="user",
                content=goal,
                field="project_goal",
                question_id="compose_goal",
                raw_input=goal,
                resolved_value=goal,
                resolution_status="matched",
            )

    # Q2 — productType (skip when already set)
    if not state.normalized_context.get("data_product_type"):
        from fluid_build.forge.product_types import get_product_type as _resolve_pt

        type_answer = ask_friendly_text(
            console,
            "Type? ADP (Silver, joined/cleaned) or CDP (Gold, consumption mart). " "[default: ADP]",
            required=False,
        )
        type_answer = (type_answer or "ADP").strip()
        pt = _resolve_pt(type_answer) or _resolve_pt("ADP")
        state.apply_patch(
            {
                "data_product_type": pt.code,
                "layer": pt.layer,
                "productType": pt.code,
            },
            source="interactive",
        )
        state.record_turn(
            role="user",
            content=type_answer,
            field="data_product_type",
            question_id="compose_type",
            raw_input=type_answer,
            resolved_value=pt.code,
            resolution_status="matched",
        )

    # Q3 — join keys (suggestion first, override allowed)
    suggested_keys = _suggest_join_keys(upstreams)
    join_prompt = (
        f"Join keys? [Enter to accept: {', '.join(suggested_keys)}]"
        if suggested_keys
        else "Join keys? (comma-separated, e.g. customer_id,order_id)"
    )
    join_answer = ask_friendly_text(console, join_prompt, required=False)
    join_keys = [k.strip() for k in (join_answer or "").split(",") if k.strip()] or suggested_keys
    if join_keys:
        state.apply_patch({"join_keys": join_keys}, source="interactive")
        state.record_turn(
            role="user",
            content=join_answer or "",
            field="join_keys",
            question_id="compose_join_keys",
            raw_input=join_answer or "",
            resolved_value=", ".join(join_keys),
            resolution_status="matched",
        )

    # Inferred fields — domain falls back to the upstreams' most-common.
    if not state.normalized_context.get("domain"):
        domains = [u.get("domain") for u in upstreams if u.get("domain")]
        if domains:
            from collections import Counter

            common = Counter(domains).most_common(1)[0][0]
            state.apply_patch({"domain": common}, source="inferred")
            state.add_assumptions([f"Domain inferred as '{common}' from upstream products."])

    # Pre-fill consumes[] for the seed contract — every upstream
    # product becomes one consumes[] row using its FIRST expose.
    consumes_rows: List[Dict[str, str]] = []
    for u in upstreams:
        expose_id = ""
        first_expose = (u.get("exposes") or [{}])[0]
        if isinstance(first_expose, dict):
            expose_id = first_expose.get("exposeId", "")
        if u.get("id"):
            consumes_rows.append({"productId": u["id"], "exposeId": expose_id or "main"})
    if consumes_rows:
        state.apply_patch({"consumes": consumes_rows}, source="inferred")
        state.add_assumptions(
            [f"Pre-filled consumes[] from {len(consumes_rows)} upstream product(s)."]
        )

    # No more questions. The seed builder + LLM take it from here.
    if console:
        try:
            console.print(
                "[green]✓[/green] Composition context captured — "
                "generating contract with the upstream schemas as input.\n"
            )
        except Exception:  # noqa: BLE001
            pass


def _suggest_join_keys(upstreams: List[Dict[str, Any]]) -> List[str]:
    """Pick column names that appear in ≥ 2 upstreams as plausible join keys.

    Heuristics: an exact column-name match in 2+ upstream exposes is a
    strong signal. Common synthetic keys (``id``, ``created_at``,
    ``updated_at``, ``pk``) get filtered unless that's the only match
    so a meaningful business key surfaces first.
    """
    from collections import Counter

    name_to_count: Counter = Counter()
    for product in upstreams:
        seen_in_product: set = set()
        for expose in product.get("exposes") or []:
            if not isinstance(expose, dict):
                continue
            for col in expose.get("schema") or []:
                if not isinstance(col, dict):
                    continue
                name = col.get("name") or ""
                if name and name not in seen_in_product:
                    seen_in_product.add(name)
        for name in seen_in_product:
            name_to_count[name] += 1

    multi = [n for n, c in name_to_count.most_common() if c >= 2]
    if not multi:
        return []
    blacklist = {"id", "created_at", "updated_at", "pk", "uuid", "version"}
    business_keys = [n for n in multi if n.lower() not in blacklist]
    return business_keys[:3] if business_keys else multi[:1]


def _run_refine_interview(state: "CopilotInterviewState", console: Any) -> None:
    """Refine-mode interview — one question.

    The system already has the entire contract loaded into
    ``context["refine_existing_contract"]`` and the prior run's
    reasoning under ``.fluid/agents/<run-id>/``. The only thing we
    don't know is *what the user wants to change*.
    """
    from .forge_dialogs import ask_friendly_text

    existing = state.normalized_context.get("refine_existing_contract") or {}
    contract_path = state.normalized_context.get("refine_contract_path", "")

    # Show the user what's loaded so they don't second-guess.
    if console and isinstance(existing, dict):
        try:
            from rich.panel import Panel as _Panel

            md = existing.get("metadata") or {}
            exposes = existing.get("exposes") or []
            cols = sum(
                len((e.get("contract") or {}).get("schema") or [])
                for e in exposes
                if isinstance(e, dict)
            )
            body = (
                f"[bold]ID:[/bold]    {existing.get('id', '?')}\n"
                f"[bold]Name:[/bold]  {existing.get('name', '?')}\n"
                f"[bold]Type:[/bold]  {md.get('productType', '?')} / {md.get('layer', '?')}\n"
                f"[bold]Domain:[/bold]{existing.get('domain', '?')}\n"
                f"[bold]Exposes:[/bold] {len(exposes)} ({cols} cols total)"
            )
            console.print(
                _Panel(
                    body,
                    title=f"[bold]Refining {contract_path}[/bold]",
                    border_style="cyan",
                )
            )
        except Exception:  # noqa: BLE001
            pass

    # The single question.
    change = ask_friendly_text(
        console,
        "What would you like to change? (one or two sentences — "
        "e.g. 'add an LTV measure', 'switch engine to dbt', "
        "'rename customer_id to user_id')",
        required=True,
    )
    if change:
        state.apply_patch({"refine_request": change}, source="interactive")
        state.record_turn(
            role="user",
            content=change,
            field="refine_request",
            question_id="refine_change_request",
            raw_input=change,
            resolved_value=change,
            resolution_status="matched",
        )

    # Surface prior-run reasoning so the LLM can pick up where the
    # last run left off (instead of starting fresh).
    prior_reasoning = _load_latest_reasoning_md(contract_path)
    if prior_reasoning:
        state.apply_patch({"prior_reasoning": prior_reasoning}, source="loaded")
        state.add_assumptions(["Loaded prior-run reasoning.md into context for continuity."])

    # Pre-fill the seed: refine flow uses the existing contract verbatim.
    state.apply_patch(
        {"seed_contract_override": dict(existing) if isinstance(existing, dict) else {}},
        source="loaded",
    )

    if console:
        try:
            console.print(
                "[green]✓[/green] Loaded existing contract — generating "
                "the requested change against it.\n"
            )
        except Exception:  # noqa: BLE001
            pass


def _load_latest_reasoning_md(contract_path: str) -> str:
    """Read the most recent ``.fluid/agents/*/reasoning.md`` next to
    *contract_path* so refine runs see what the prior agent was thinking.
    """
    if not contract_path:
        return ""
    try:
        contract_dir = Path(contract_path).resolve().parent
        agents_dir = contract_dir / ".fluid" / "agents"
        if not agents_dir.is_dir():
            return ""
        latest = None
        latest_mtime = 0.0
        for run_dir in agents_dir.iterdir():
            md = run_dir / "reasoning.md"
            if md.is_file():
                m = md.stat().st_mtime
                if m > latest_mtime:
                    latest_mtime = m
                    latest = md
        if latest is None:
            return ""
        text = latest.read_text(encoding="utf-8")
        # Cap so we don't blow up the prompt with megabytes of trace.
        return text[:8000]
    except Exception:  # noqa: BLE001
        return ""


# ── ask-helper imports (extracted) ────────────────────────────────
# The ``_ask_*`` dialog helpers (~960 LOC of individual field
# prompts) were physically extracted into the
# ``_interview_ask_helpers`` sibling module. Re-imported at
# module top so test patches on this namespace still resolve.
from fluid_build.cli._interview_ask_helpers import (  # noqa: E402,F401
    _ask_bootstrap_questions,
    _ask_byos_question,
    _ask_byot_question,
    _ask_data_model_question,
    _ask_data_modeling_technique,
    _ask_delivery_setup,
    _ask_dynamic_questions,
    _ask_engine_selection,
    _ask_scheduler_delivery,
    _ask_transformation_delivery,
    _default_data_model_source,
    _discovery_is_thin,
    _format_source_label,
    _list_configured_sources,
    _looks_like_existing_artifact_ref,
    _print_discovered_data,
    _scaffold_data_dirs_and_prompt,
    _should_prompt_for_scheduler,
    _suggest_modeling_default,
)
