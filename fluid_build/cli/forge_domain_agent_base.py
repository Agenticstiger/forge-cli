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

"""Shared base classes and helpers for Forge domain agents."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_agent_specs import (
    AgentSpec,
    load_builtin_agent_spec,
    load_user_or_builtin_spec,
)
from fluid_build.cli.forge_dialogs import normalize_choice_value
from fluid_build.cli.forge_ui import show_domain_analysis, show_next_steps_panel

GLOBAL_SECURITY_REQUIREMENTS = [
    "Implement encryption at rest and in transit for regulated and personal data",
    "Enforce least-privilege RBAC with MFA for privileged and operational access",
    "Enable immutable audit logging and continuous security monitoring for all data access",
    "Define GDPR-aligned retention, deletion, and data subject request workflows",
    "Minimize, classify, and pseudonymize personal data wherever full identity is not required",
    "Run regular vulnerability scanning, dependency patching, and security-control reviews",
]

GLOBAL_PRIVACY_BEST_PRACTICES = [
    "Apply privacy-by-design and secure-by-default controls from the first iteration",
    "Version data classification, retention, and access-control decisions with the project",
]


def _raw_answer(context: Dict[str, Any], key: str) -> str:
    raw_answers = context.get("raw_answers") or {}
    return str(raw_answers.get(key) or context.get(key) or "").strip()


def _resolve_context_choice(
    context: Dict[str, Any],
    *,
    field_name: str,
    choices: List[Dict[str, Any]],
    default: Optional[str] = None,
) -> Optional[str]:
    return normalize_choice_value(
        context.get(field_name),
        field_name=field_name,
        choices=choices,
        default=None,
    ) or normalize_choice_value(
        _raw_answer(context, field_name),
        field_name=field_name,
        choices=choices,
        default=default,
    )


def _choice_label(choices: List[Dict[str, Any]], value: Any) -> str:
    current = str(value or "").strip()
    for choice in choices:
        if str(choice.get("value") or "").strip() == current:
            return str(choice.get("label") or current)
    return current.replace("_", " ").title() if current else "Not specified"


class AIAgentBase:
    """Base class for minimal domain agents."""

    def __init__(self, name: str, description: str, domain: str):
        self.name = name
        self.description = description
        self.domain = domain
        try:
            from rich.console import Console

            self.console = Console()
        except ImportError:
            self.console = None

    def get_questions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def analyze_requirements(self, context: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def create_project(self, target_dir: Path, context: Dict[str, Any]) -> bool:
        """Create project using the shared ForgeEngine-backed path."""
        try:
            suggestions = self.analyze_requirements(context)
            self._show_ai_analysis(context, suggestions)
            project_config = self._create_forge_config(target_dir, context, suggestions)
            success = self._create_with_forge_engine(project_config)

            if success:
                self._show_next_steps(target_dir, context, suggestions)
                return True
            if self.console:
                self.console.print("[red]❌ Project creation failed validation[/red]")
            return False
        except Exception as exc:
            if self.console:
                self.console.print(f"[red]❌ Failed to create project: {exc}[/red]")
            else:
                console_error(f"Failed to create project: {exc}")
            return False

    def _create_forge_config(
        self, target_dir: Path, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> Dict[str, Any]:
        goal = context.get("project_goal", "Data Product")
        project_name = self._sanitize_project_name(goal)
        return {
            "name": project_name,
            "description": f"AI-generated {goal} ({self.domain} domain)",
            "template": suggestions["recommended_template"],
            "provider": suggestions["recommended_provider"],
            "target_dir": str(target_dir),
            "ai_context": context,
            "ai_suggestions": suggestions,
            "domain": self.domain,
        }

    def _sanitize_project_name(self, goal: str) -> str:
        import re

        name = goal.lower()
        name = re.sub(r"[^a-z0-9\s\-_]", "", name)
        name = re.sub(r"\s+", "-", name)
        name = re.sub(r"-+", "-", name)
        name = name.strip("-")

        if name and not name[0].isalpha():
            name = f"project-{name}"
        if not name:
            name = f"{self.domain}-data-product"
        return name

    def _create_with_forge_engine(self, project_config: Dict[str, Any]) -> bool:
        """Use ForgeEngine to create and validate project output."""
        try:
            from fluid_build.forge import ForgeEngine

            if self.console:
                with self.console.status(
                    f"[bold blue]🔧 Generating {self.domain} project...", spinner="dots"
                ):
                    engine = ForgeEngine()
                    return engine.run_with_config(project_config, dry_run=False)
            cprint(f"🔧 Generating {self.domain} project...")
            engine = ForgeEngine()
            return engine.run_with_config(project_config, dry_run=False)
        except Exception as exc:
            if self.console:
                self.console.print(f"[red]❌ ForgeEngine integration failed: {exc}[/red]")
            return False

    def _show_ai_analysis(self, context: Dict[str, Any], suggestions: Dict[str, Any]) -> None:
        """Show a shared domain analysis panel."""
        if not self.console:
            return
        try:
            product_choices = self.get_questions()[0]["choices"]
        except (NotImplementedError, IndexError, KeyError, TypeError):
            product_choices = []
        product_type = _choice_label(product_choices, context.get("product_type"))
        show_domain_analysis(
            self.console,
            goal=context.get("project_goal", "Data Product"),
            data_sources=context.get("data_sources", "Not specified"),
            product_type=product_type,
            suggestions=suggestions,
            domain=self.domain,
        )

    def _show_next_steps(
        self, target_dir: Path, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> None:
        """Show official-command next steps plus domain-specific tips."""
        if not self.console:
            return

        show_next_steps_panel(
            self.console,
            target_dir=target_dir,
            provider=suggestions["recommended_provider"],
        )

    def _build_next_step_tips(
        self, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> List[str]:
        """Return domain-specific next-step tips."""
        domain_tips: List[str] = []
        if self.domain == "finance" and suggestions.get("security_requirements"):
            domain_tips.extend(
                [
                    "Review security and compliance requirements",
                    "Set up audit logging and access controls",
                ]
            )
        elif self.domain == "healthcare":
            domain_tips.extend(
                [
                    "Ensure HIPAA compliance measures are in place",
                    "Review PHI handling procedures",
                ]
            )
        elif self.domain == "retail":
            domain_tips.extend(
                [
                    "Configure personalization engine settings",
                    "Set up A/B testing framework",
                ]
            )
        return domain_tips


class DeclarativeDomainAgent(AIAgentBase):
    """Domain agent backed by a declarative YAML spec (built-in or user-defined)."""

    def __init__(self, spec_name: Optional[str] = None, *, spec: Optional[AgentSpec] = None):
        self._spec = spec or load_user_or_builtin_spec(str(spec_name or "").strip())
        super().__init__(
            name=self._spec.name,
            description=self._spec.description,
            domain=self._spec.domain,
        )

    def get_questions(self) -> List[Dict[str, Any]]:
        return deepcopy(self._spec.questions)

    def analyze_requirements(self, context: Dict[str, Any]) -> Dict[str, Any]:
        resolved_context = self._resolve_analysis_context(context)
        suggestions = deepcopy(self._spec.suggestion_defaults)
        self._apply_global_compliance_baseline(suggestions)
        for rule in self._spec.rules:
            if self._condition_matches(rule["when"], resolved_context):
                for action in rule["actions"]:
                    self._apply_action(suggestions, action)
        return suggestions

    def _resolve_analysis_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        resolved: Dict[str, Any] = {}
        for question in self._spec.questions:
            key = question["key"]
            default = self._spec.resolver_defaults.get(key, question.get("default"))
            if question["type"] == "choice":
                resolved[key] = _resolve_context_choice(
                    context,
                    field_name=key,
                    choices=question.get("choices", []),
                    default=default,
                )
                continue

            raw_value = _raw_answer(context, key)
            resolved[key] = raw_value or context.get(key) or default
        return resolved

    def _condition_matches(
        self, condition: Dict[str, Any], resolved_context: Dict[str, Any]
    ) -> bool:
        if "all" in condition:
            return all(
                self._clause_matches(clause, resolved_context) for clause in condition["all"]
            )
        return any(self._clause_matches(clause, resolved_context) for clause in condition["any"])

    def _clause_matches(self, clause: Dict[str, Any], resolved_context: Dict[str, Any]) -> bool:
        actual = resolved_context.get(clause["field"])
        if "equals" in clause:
            return actual == clause["equals"]
        if isinstance(actual, list):
            return any(item in clause["in"] for item in actual)
        return actual in clause["in"]

    def _apply_action(self, suggestions: Dict[str, Any], action: Dict[str, Any]) -> None:
        path = action["path"]
        value = deepcopy(action["value"])
        if action["op"] == "set":
            suggestions[path] = value
            return

        existing = suggestions.get(path)
        if not isinstance(existing, list):
            existing = []
            suggestions[path] = existing
        values_to_add = value if isinstance(value, list) else [value]
        for item in values_to_add:
            if item not in existing:
                existing.append(item)

    def _append_unique_values(
        self, suggestions: Dict[str, Any], path: str, values: List[str]
    ) -> None:
        existing = suggestions.get(path)
        if not isinstance(existing, list):
            existing = []
            suggestions[path] = existing
        for item in values:
            if item not in existing:
                existing.append(item)

    def _apply_global_compliance_baseline(self, suggestions: Dict[str, Any]) -> None:
        self._append_unique_values(
            suggestions,
            "security_requirements",
            list(GLOBAL_SECURITY_REQUIREMENTS),
        )
        self._append_unique_values(
            suggestions,
            "best_practices",
            list(GLOBAL_PRIVACY_BEST_PRACTICES),
        )

    def _build_next_step_tips(
        self, context: Dict[str, Any], suggestions: Dict[str, Any]
    ) -> List[str]:
        resolved_context = self._resolve_analysis_context(context)
        tips = list(self._spec.next_step_tips)
        for conditional in self._spec.conditional_next_step_tips:
            if self._condition_matches(conditional["when"], resolved_context):
                for tip in conditional["tips"]:
                    if tip not in tips:
                        tips.append(tip)
        return tips


__all__ = [
    "AIAgentBase",
    "DeclarativeDomainAgent",
    "_raw_answer",
    "_resolve_context_choice",
    "_choice_label",
]
