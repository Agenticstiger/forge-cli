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

"""Core copilot agent orchestration for Forge."""

from __future__ import annotations

__all__ = [
    "AIAgent",
    "CopilotAgentBase",
    "recommend_template_for_use_case",
]


import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_copilot_interview import run_post_generation_clarification
from fluid_build.cli.forge_copilot_memory import CopilotMemoryStore
from fluid_build.cli.forge_copilot_memory_mixin import CopilotProjectMemoryMixin
from fluid_build.cli.forge_copilot_runtime import (
    CopilotGenerationError,
    CopilotGenerationResult,
    build_capability_matrix,
    discover_local_context,
    generate_copilot_artifacts,
    normalize_provider_name,
    normalize_template_name,
    resolve_llm_config,
)
from fluid_build.cli.forge_copilot_scaffold_mixin import CopilotLegacyScaffoldMixin
from fluid_build.cli.forge_copilot_taxonomy import (
    USE_CASE_CHOICES,
    normalize_copilot_context,
    normalize_use_case,
)
from fluid_build.cli.forge_copilot_taxonomy import (
    canonicalize_use_case_text as _canonicalize_use_case_text,
)
from fluid_build.cli.forge_copilot_taxonomy import clean_text as _clean_text
from fluid_build.cli.forge_dialogs import ask_confirmation
from fluid_build.cli.forge_ui import print_assumptions_panel, show_lines_panel
from fluid_build.schema_manager import FluidSchemaManager

try:
    from rich.console import Console

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through non-Rich fallbacks elsewhere
    Console = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

LOG = logging.getLogger("fluid.cli.forge")


def _infer_template_from_text(text: str) -> Optional[str]:
    """Infer the closest template from free-form text."""
    normalized_text = _canonicalize_use_case_text(text)
    if not normalized_text:
        return None
    if (
        "machine learning" in normalized_text
        or "feature engineering" in normalized_text
        or "feature store" in normalized_text
        or "model" in normalized_text
        or re.search(r"\bml\b", normalized_text)
    ):
        return "ml_pipeline"
    if any(
        token in normalized_text
        for token in ("streaming", "real time", "realtime", "kafka", "event")
    ):
        return "streaming"
    if any(
        token in normalized_text
        for token in (
            "etl",
            "ingest",
            "cdc",
            "multi source",
            "sync",
            "pipeline",
            "data lake",
            "data platform",
            "lakehouse",
        )
    ):
        return "etl_pipeline"
    if any(
        token in normalized_text
        for token in ("analytics", "report", "dashboard", "visualization", "business intelligence")
    ) or re.search(r"\bbi\b", normalized_text):
        return "analytics"
    return None


def recommend_template_for_use_case(context: Dict[str, Any]) -> str:
    """Choose the best template for the normalized use case and context."""
    use_case_raw = _clean_text(context.get("use_case"))
    use_case_other = _clean_text(context.get("use_case_other"))
    has_use_case_input = bool(use_case_raw or use_case_other)
    canonical = normalize_use_case(use_case_raw)
    if not has_use_case_input:
        canonical = "analytics"

    if canonical == "analytics":
        return "analytics"
    if canonical == "etl_pipeline":
        return "etl_pipeline"
    if canonical == "streaming":
        return "streaming"
    if canonical == "ml_pipeline":
        return "ml_pipeline"
    if canonical == "data_platform":
        return "etl_pipeline"

    inference_text = " ".join(
        part
        for part in (
            use_case_raw,
            use_case_other,
            _clean_text(context.get("project_goal")),
            _clean_text(context.get("data_sources")),
        )
        if part
    )
    inferred = _infer_template_from_text(inference_text)
    if canonical == "other":
        return inferred or "starter"
    if inferred:
        return inferred
    return "starter" if has_use_case_input else "analytics"


class AIAgent:
    """Base public AI agent abstraction."""

    def __init__(self, name: str, description: str, domain: str):
        self.name = name
        self.description = description
        self.domain = domain
        self.console = Console() if RICH_AVAILABLE else None

    def create_project(self, target_dir: Path, context: Dict[str, Any]) -> bool:
        raise NotImplementedError

    def get_questions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError


class CopilotAgentBase(CopilotProjectMemoryMixin, CopilotLegacyScaffoldMixin, AIAgent):
    """Shared copilot implementation with overrideable dependencies."""

    recommend_template_for_use_case = staticmethod(recommend_template_for_use_case)

    def __init__(self):
        super().__init__(
            name="copilot",
            description="General-purpose AI assistant for data product creation",
            domain="general",
        )
        self._project_memory_enabled = True
        self._project_memory_snapshot = None
        self._project_memory_path = None
        # Canonical per-run memory root — resolved once (load) and reused by
        # save + management so state never splits across roots (Trello
        # 69d42ad4). ``None`` until first resolution.
        self._project_memory_root = None

    def _resolve_llm_config_dependency(self, options: SimpleNamespace):
        return resolve_llm_config(options)

    def _discover_local_context_dependency(self, options: SimpleNamespace):
        return discover_local_context(
            getattr(options, "discovery_path", None),
            discover=getattr(options, "discover", True),
            workspace_root=Path.cwd(),
            logger=LOG,
        )

    def _build_capability_matrix_dependency(self):
        return build_capability_matrix()

    def _generate_copilot_artifacts_dependency(
        self,
        context: Dict[str, Any],
        *,
        llm_config: Any,
        discovery_report: Any,
        project_memory: Any,
        team_memory: Any = None,
        capability_matrix: Any,
    ) -> CopilotGenerationResult:
        return generate_copilot_artifacts(
            context,
            llm_config=llm_config,
            discovery_report=discovery_report,
            project_memory=project_memory,
            team_memory=team_memory,
            capability_matrix=capability_matrix,
            logger=LOG,
        )

    def _make_memory_store_dependency(self, project_root: Path) -> CopilotMemoryStore:
        return CopilotMemoryStore(project_root, logger=LOG)

    def _ask_confirmation_dependency(self, prompt: str, preview: str) -> bool:
        if self.console and RICH_AVAILABLE:
            return ask_confirmation(
                self.console,
                prompt,
                default=False,
                title="🧠 Save Project Memory?",
                preview=preview,
                border_style="cyan",
            )
        for line in preview.splitlines():
            cprint(line)
        answer = input(f"{prompt} [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    def _run_post_generation_clarification_dependency(
        self,
        interview_state: Any,
        *,
        llm_config: Any,
        discovery_report: Any,
        capability_matrix: Any,
        project_memory: Any,
        failure_summary: List[str],
    ) -> Any:
        return run_post_generation_clarification(
            interview_state,
            console=self.console,
            llm_config=llm_config,
            discovery_report=discovery_report,
            capability_matrix=capability_matrix,
            project_memory=project_memory,
            failure_summary=failure_summary,
        )

    def get_questions(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "project_goal",
                "question": "What are you trying to build? (e.g., 'Customer analytics dashboard', 'ML recommendation engine')",
                "type": "text",
                "required": True,
            },
            {
                "key": "data_sources",
                "question": "What data sources will you use? (e.g., 'BigQuery tables', 'REST APIs', 'CSV files')",
                "type": "text",
                "required": True,
            },
            {
                "key": "use_case",
                "question": "What's your primary use case?",
                "type": "choice",
                "choices": USE_CASE_CHOICES,
                "follow_up": {
                    "trigger_value": "other",
                    "key": "use_case_other",
                    "question": "Tell me more about your use case (e.g., 'customer 360', 'CDC sync', 'executive scorecards')",
                },
                "required": True,
            },
            {
                "key": "team_size",
                "question": "How large is your team?",
                "type": "choice",
                "choices": ["solo", "small (2-5)", "medium (6-15)", "large (15+)"],
                "required": False,
            },
            {
                "key": "complexity",
                "question": "Preferred complexity level?",
                "type": "choice",
                "choices": ["simple", "intermediate", "advanced"],
                "default": "intermediate",
            },
        ]

    def analyze_requirements(self, context: Dict[str, Any]) -> Dict[str, Any]:
        context = normalize_copilot_context(context)
        complexity = context.get("complexity", "intermediate").lower()
        data_sources = context.get("data_sources", "").lower()

        suggestions = {
            "recommended_template": recommend_template_for_use_case(context),
            "recommended_provider": "local",
            "recommended_patterns": [],
            "architecture_suggestions": [
                "Start with modular design",
                "Implement proper data validation",
                "Set up monitoring and logging",
            ],
            "best_practices": [
                "Use version control for all code",
                "Implement automated testing",
                "Document data lineage",
                "Set up CI/CD pipeline",
            ],
            "technology_stack": [],
        }

        if "bigquery" in data_sources or "gcp" in data_sources:
            suggestions["recommended_provider"] = "gcp"
        elif "snowflake" in data_sources:
            suggestions["recommended_provider"] = "snowflake"
        elif "aws" in data_sources:
            suggestions["recommended_provider"] = "aws"

        if complexity == "advanced":
            suggestions["recommended_patterns"] = ["data_mesh", "microservices"]
        elif complexity == "intermediate":
            suggestions["recommended_patterns"] = ["layered_architecture"]
        else:
            suggestions["recommended_patterns"] = ["simple_pipeline"]

        return suggestions

    def prepare_runtime_inputs(
        self, copilot_options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Resolve LLM, discovery, memory, and capability inputs once per run."""
        options = SimpleNamespace(**(copilot_options or {}))
        llm_config = getattr(options, "llm_config", None) or self._resolve_llm_config_dependency(
            options
        )
        target_dir = getattr(options, "target_dir", None)
        target_path = Path(target_dir).expanduser() if target_dir else None
        discovery_report = getattr(
            options, "discovery_report", None
        ) or self._discover_local_context_dependency(options)
        project_memory = (
            getattr(options, "project_memory", None) if hasattr(options, "project_memory") else None
        )
        if project_memory is None:
            project_memory = self._load_project_memory(
                enabled=getattr(options, "memory", True),
                target_dir=target_path,
            )
        capability_matrix = getattr(options, "capability_matrix", None)
        capability_warnings = list(getattr(options, "capability_warnings", []) or [])
        if capability_matrix is None:
            capability_matrix = self._build_capability_matrix_dependency()
            capability_warnings = list(capability_matrix.get("warnings") or [])
            for capability_warning in capability_warnings:
                LOG.warning("Copilot capability check warning: %s", capability_warning)
        capability_warnings = list(dict.fromkeys(capability_warnings))
        return {
            "llm_config": llm_config,
            "discovery_report": discovery_report,
            "project_memory": project_memory,
            "capability_matrix": capability_matrix,
            "capability_warnings": capability_warnings,
        }

    def generate_project_artifacts(
        self, context: Dict[str, Any], copilot_options: Optional[Dict[str, Any]] = None
    ) -> CopilotGenerationResult:
        """Generate and validate copilot artifacts via the LLM runtime."""
        context = normalize_copilot_context(context)
        options = dict(copilot_options or {})
        if options.get("tiered"):
            context["tiered"] = True
        if options.get("require_llm"):
            context["require_llm"] = True
        runtime_inputs = self.prepare_runtime_inputs(options)

        # Load team memory from workspace root (if available).
        team_memory_payload = None
        try:
            from fluid_build.cli.forge_team_memory import load_team_memory
            from fluid_build.util.workspace import find_workspace_root

            ws_root = find_workspace_root(Path.cwd()) or Path.cwd()
            tm = load_team_memory(ws_root)
            if tm is not None:
                team_memory_payload = tm.to_prompt_payload()
        except Exception:  # noqa: BLE001
            pass

        return self._generate_copilot_artifacts_dependency(
            context,
            llm_config=runtime_inputs["llm_config"],
            discovery_report=runtime_inputs["discovery_report"],
            project_memory=runtime_inputs["project_memory"],
            team_memory=team_memory_payload,
            capability_matrix=runtime_inputs["capability_matrix"],
        )

    def create_project(
        self,
        target_dir: Path,
        context: Dict[str, Any],
        copilot_options: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> bool:
        """Create a project from validated LLM-generated artifacts."""
        try:
            context = normalize_copilot_context(context)
            options = dict(copilot_options or {})
            options.setdefault("target_dir", str(target_dir))
            try:
                generation_result = self.generate_project_artifacts(context, options)
            except CopilotGenerationError as generation_error:
                recovered_result = self._attempt_generation_recovery(
                    context=context,
                    options=options,
                    error=generation_error,
                )
                if recovered_result is None:
                    raise
                context = normalize_copilot_context(
                    options.get("interview_state").normalized_context
                    if options.get("interview_state")
                    else context
                )
                generation_result = recovered_result
            suggestions = generation_result.suggestions

            self._show_ai_analysis(context, suggestions, generation_result)
            project_config = self._create_forge_config(
                target_dir,
                context,
                suggestions,
                generation_result,
            )
            success = self._create_with_forge_engine(project_config, dry_run=dry_run)

            if success:
                self._maybe_save_project_memory(
                    target_dir=target_dir,
                    context=context,
                    suggestions=suggestions,
                    generation_result=generation_result,
                    copilot_options=options,
                    dry_run=dry_run,
                )
                self._show_next_steps(target_dir, context, suggestions)
                return True

            if self.console:
                self.console.print("[red]❌ Project creation failed validation[/red]")
            return False
        except CopilotGenerationError as exc:
            if self.console:
                self.console.print(f"[red]❌ {exc.message}[/red]")
                for suggestion in exc.suggestions:
                    self.console.print(f"[dim]• {suggestion}[/dim]")
            else:
                console_error(exc.message)
                for suggestion in exc.suggestions:
                    cprint(f"  • {suggestion}")
            return False
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            LOG.exception("Project creation failed")
            if self.console:
                self.console.print(f"[red]❌ Failed to create project: {exc}[/red]")
            else:
                console_error(f"Failed to create project: {exc}")
            return False

    def _attempt_generation_recovery(
        self,
        *,
        context: Dict[str, Any],
        options: Dict[str, Any],
        error: CopilotGenerationError,
    ) -> Optional[CopilotGenerationResult]:
        # Auth failures (401) get their own recovery: re-prompt for a fresh
        # key and retry the run in place. This branch is independent of the
        # interview state (a bad key can fail on the very first call).
        if error.context.get("failure_class") == "auth":
            return self._attempt_auth_recovery(context=context, options=options)

        interview_state = options.get("interview_state")
        if (
            not interview_state
            or options.get("non_interactive")
            or error.context.get("failure_class") != "ambiguous_intent"
            or options.get("clarification_recovery_used")
        ):
            return None

        runtime_inputs = self.prepare_runtime_inputs(options)
        if self.console:
            show_lines_panel(
                self.console,
                [
                    "Forge needs one more clarification pass to tighten the semantic intent for a valid 0.7.2 contract."
                ],
                title="🧭 One More Question Round",
                border_style="yellow",
            )

        updated_state = self._run_post_generation_clarification_dependency(
            interview_state,
            llm_config=runtime_inputs["llm_config"],
            discovery_report=runtime_inputs["discovery_report"],
            capability_matrix=runtime_inputs["capability_matrix"],
            project_memory=runtime_inputs["project_memory"],
            failure_summary=error.context.get("attempt_summaries") or error.suggestions,
        )
        options["interview_state"] = updated_state
        options["clarification_recovery_used"] = True
        updated_context = normalize_copilot_context(updated_state.finalize())
        return self.generate_project_artifacts(updated_context, options)

    def _attempt_auth_recovery(
        self,
        *,
        context: Dict[str, Any],
        options: Dict[str, Any],
    ) -> Optional[CopilotGenerationResult]:
        """Re-prompt for an API key after a 401 and retry generation once.

        Gated to genuinely interactive sessions (a real TTY with a Rich
        console, and not ``--non-interactive``) and to a single attempt per run
        (``auth_recovery_used``) so a user who keeps pasting a bad key can't get
        stuck in a prompt loop. On success the freshly-entered key is already
        persisted + exported to the process env by ``rotate_api_key_interactive``,
        so re-running ``generate_project_artifacts`` re-resolves the config and
        picks it up. If the guard rejects or the user cancels, we return
        ``None`` and the original auth error surfaces exactly as before.
        """
        if options.get("non_interactive") or options.get("auth_recovery_used") or not self.console:
            return None
        try:
            if not sys.stdin.isatty():
                return None
        except Exception:  # noqa: BLE001 - odd stream → treat as non-interactive
            return None

        # Mark used *before* the retry so a second 401 (e.g. the replacement
        # key is also bad) can't recurse into another prompt.
        options["auth_recovery_used"] = True

        from fluid_build.cli.ai_setup import rotate_api_key_interactive

        new_config = rotate_api_key_interactive(self.console)
        if new_config is None:
            return None
        return self.generate_project_artifacts(context, options)

    def _create_forge_config(
        self,
        target_dir: Path,
        context: Dict[str, Any],
        suggestions: Dict[str, Any],
        generation_result: Optional[CopilotGenerationResult] = None,
    ) -> Dict[str, Any]:
        goal = context.get("project_goal", "Data Product")
        contract_name = (
            generation_result.contract.get("name")
            if generation_result and generation_result.contract
            else goal
        )
        project_name = self._sanitize_project_name(contract_name)

        config = {
            "name": project_name,
            "description": suggestions.get("description") or f"AI-generated {goal}",
            "domain": suggestions.get("domain") or context.get("domain") or "analytics",
            "owner": suggestions.get("owner") or "data-team",
            "template": normalize_template_name(suggestions["recommended_template"]),
            "provider": normalize_provider_name(suggestions["recommended_provider"]),
            "target_dir": target_dir.as_posix(),
            "ai_context": context,
            "ai_suggestions": suggestions,
        }
        if generation_result:
            config["fluid_version"] = generation_result.contract.get(
                "fluidVersion", FluidSchemaManager.latest_bundled_version()
            )
            config["copilot_generated_contract"] = generation_result.contract
            config["copilot_generated_readme"] = generation_result.readme_markdown
            if generation_result.additional_files:
                config["copilot_generated_files"] = generation_result.additional_files
        return config


# CopilotAgent is defined in forge.py where it wires module-level dependencies.
# Import it from there: ``from fluid_build.cli.forge import CopilotAgent``
