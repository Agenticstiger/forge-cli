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

"""Shared Forge context loading, memory management, and dialog utilities."""

from __future__ import annotations

__all__ = [
    "gather_copilot_context",
    "get_cli_arg",
    "get_target_directory",
    "handle_memory_management",
    "load_context",
    "resolve_memory_store",
]


import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional, Type

import yaml

from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.forge_copilot_interview import InterviewQuestion
from fluid_build.cli.forge_copilot_memory import (
    CopilotMemoryStore,
    resolve_copilot_memory_root,
    summarize_copilot_memory,
)
from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context
from fluid_build.cli.forge_dialogs import ask_dialog_question
from fluid_build.cli.forge_ui import show_lines_panel

try:
    from rich.console import Console

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through non-Rich fallbacks elsewhere
    Console = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


def get_target_directory(args: Any, default_name: str = "my-fluid-project") -> Path:
    """
    Determine target directory for project creation.

    If no target is specified and we're inside the package, create outside the
    repository root by default.
    """
    if args.target_dir:
        return Path(args.target_dir)

    cwd = Path.cwd()
    try:
        package_root = Path(__file__).parent.parent.parent
        if cwd.is_relative_to(package_root):
            suggested_parent = package_root.parent
            if suggested_parent.exists() and suggested_parent.is_dir():
                return suggested_parent / default_name
            return Path.home() / "fluid-projects" / default_name
    except (ValueError, Exception):
        pass

    return cwd / default_name


def get_cli_arg(args: Any, name: str, default: Any = None) -> Any:
    """Read argparse-style attributes without letting MagicMock invent values."""
    if hasattr(args, "__dict__") and name in vars(args):
        return vars(args)[name]
    return default


def resolve_memory_store(
    args: Any,
    logger: Any,
    *,
    target_directory_fn: Callable[[Any, str], Path] = get_target_directory,
    memory_root_resolver: Callable[..., Path] = resolve_copilot_memory_root,
    memory_store_class: Type[CopilotMemoryStore] = CopilotMemoryStore,
) -> CopilotMemoryStore:
    """Resolve the project-scoped memory store for management actions."""
    target_dir_value = get_cli_arg(args, "target_dir")
    target_dir = Path(target_dir_value).expanduser() if target_dir_value else None
    if target_dir is None:
        target_dir = target_directory_fn(SimpleNamespace(target_dir=None), "my-fluid-project")
        target_dir = None if target_dir.name == "my-fluid-project" else target_dir
    project_root = memory_root_resolver(Path.cwd(), target_dir=target_dir)
    return memory_store_class(project_root, logger=logger)


def handle_memory_management(
    args: Any,
    logger: Any,
    *,
    memory_store_class: Type[CopilotMemoryStore] = CopilotMemoryStore,
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Show or reset project-scoped copilot memory and exit."""
    console = console_factory() if console_factory else None
    store = resolve_memory_store(args, logger, memory_store_class=memory_store_class)

    if get_cli_arg(args, "reset_memory", False):
        deleted = store.delete()
        if console:
            lines = (
                [f"Deleted project-scoped copilot memory at `{store.path}`"]
                if deleted
                else [f"No project-scoped copilot memory found at `{store.path}`"]
            )
            show_lines_panel(
                console,
                lines,
                title="🧠 Project Memory",
                border_style="green" if deleted else "yellow",
            )
        else:
            if deleted:
                success(f"Deleted project-scoped copilot memory at {store.path}")
            else:
                warning(f"No project-scoped copilot memory found at {store.path}")
        if get_cli_arg(args, "show_memory", False):
            return handle_memory_management(
                SimpleNamespace(**{**vars(args), "reset_memory": False}),
                logger,
                memory_store_class=memory_store_class,
                console_factory=console_factory,
            )
        return 0

    memory = store.load()
    if console:
        if not memory:
            show_lines_panel(
                console,
                [f"No project-scoped copilot memory found at `{store.path}`"],
                title="🧠 Project Memory",
                border_style="yellow",
            )
            return 0
        summary = summarize_copilot_memory(memory)
        details = [
            f"Path: `{store.path}`",
            f"Saved at: {summary.get('saved_at') or 'unknown'}",
            f"Preferred template: {summary.get('preferred_template') or 'unknown'}",
            f"Preferred provider: {summary.get('preferred_provider') or 'unknown'}",
            f"Preferred domain: {summary.get('preferred_domain') or 'unknown'}",
            f"Preferred owner: {summary.get('preferred_owner') or 'unknown'}",
            "Build engines: " + (", ".join(summary.get("build_engines") or []) or "none"),
            "Binding formats: " + (", ".join(summary.get("binding_formats") or []) or "none"),
            "Provider hints: " + (", ".join(summary.get("provider_hints") or []) or "none"),
            f"Schema summaries: {summary.get('schema_summary_count', 0)}",
            f"Recent successful outcomes: {summary.get('recent_outcome_count', 0)}",
        ]
        if summary.get("source_formats"):
            details.append(
                "Source formats: "
                + ", ".join(
                    f"{key}={value}" for key, value in sorted(summary["source_formats"].items())
                )
            )
        show_lines_panel(console, details, title="🧠 Project Memory", border_style="cyan")
        return 0

    if not memory:
        warning(f"No project-scoped copilot memory found at {store.path}")
        return 0

    summary = summarize_copilot_memory(memory)
    cprint(f"Project memory: {store.path}")
    cprint(f"  template={summary.get('preferred_template') or 'unknown'}")
    cprint(f"  provider={summary.get('preferred_provider') or 'unknown'}")
    cprint(f"  domain={summary.get('preferred_domain') or 'unknown'}")
    cprint(f"  owner={summary.get('preferred_owner') or 'unknown'}")
    cprint("  build_engines=" + (", ".join(summary.get("build_engines") or []) or "none"))
    cprint(f"  schema_summaries={summary.get('schema_summary_count', 0)}")
    cprint(f"  recent_outcomes={summary.get('recent_outcome_count', 0)}")
    return 0


def gather_copilot_context(copilot: Any, console: Any) -> Dict[str, Any]:
    """Gather context through interactive questioning."""
    context: Dict[str, Any] = {}
    dialog_transcript: List[Dict[str, Any]] = []
    raw_answers: Dict[str, str] = {}

    if not console or not RICH_AVAILABLE:
        return context

    try:
        questions = copilot.get_questions()

        for question_def in questions:
            key = question_def["key"]
            follow_up = question_def.get("follow_up")
            question = InterviewQuestion.from_payload(question_def)
            result = ask_dialog_question(console, question)

            if result.context_patch:
                context.update(result.context_patch)
            elif result.value is not None:
                context[key] = result.value
            if result.raw_input:
                raw_answers[key] = result.raw_input
            if result.raw_input or result.value is not None:
                dialog_transcript.append(
                    {
                        "role": "user",
                        "field": key,
                        "question_id": question.id,
                        "content": result.raw_input or str(result.value or "").strip(),
                        "raw_input": result.raw_input,
                        "resolved_value": result.value,
                        "resolution_status": result.resolution_status,
                    }
                )

            answer = context.get(key)
            if (
                follow_up
                and answer
                and answer == follow_up.get("trigger_value")
                and follow_up.get("key")
                and follow_up.get("question")
                and not context.get(follow_up["key"])
            ):
                follow_up_result = ask_dialog_question(
                    console,
                    InterviewQuestion.from_payload(
                        {
                            "id": follow_up["key"],
                            "field": follow_up["key"],
                            "prompt": follow_up["question"],
                            "type": "text",
                            "required": False,
                            "default": follow_up.get("default"),
                        }
                    ),
                )
                if follow_up_result.value:
                    context[follow_up["key"]] = follow_up_result.value
                if follow_up_result.raw_input:
                    raw_answers[follow_up["key"]] = follow_up_result.raw_input
                    dialog_transcript.append(
                        {
                            "role": "user",
                            "field": follow_up["key"],
                            "question_id": follow_up["key"],
                            "content": follow_up_result.raw_input,
                            "raw_input": follow_up_result.raw_input,
                            "resolved_value": follow_up_result.value,
                            "resolution_status": follow_up_result.resolution_status,
                        }
                    )

        context = normalize_copilot_context(context)
        if dialog_transcript:
            context["dialog_transcript"] = dialog_transcript
        if raw_answers:
            context["raw_answers"] = raw_answers
    except Exception:
        context = {
            "project_goal": "Data Product",
            "data_sources": "Various sources",
            "use_case": "analytics",
            "complexity": "intermediate",
        }

    return context


def load_context(
    context_input: str,
    console: Optional[Any] = None,
    *,
    context_error_cls: Type[Exception] = ValueError,
) -> Dict[str, Any]:
    """Load and validate additional context from JSON or YAML text/files."""
    try:
        if context_input.strip().startswith("{"):
            try:
                context = json.loads(context_input)
            except json.JSONDecodeError as exc:
                raise context_error_cls(f"Invalid JSON: {exc}")
            if not isinstance(context, dict):
                raise context_error_cls("Context must be a JSON object")
            return context

        context_path = Path(context_input)
        if not context_path.exists():
            raise context_error_cls(f"Context file not found: {context_path}")
        if not context_path.is_file():
            raise context_error_cls(f"Context path is not a file: {context_path}")
        if context_path.stat().st_size > 1024 * 1024:
            raise context_error_cls("Context file too large (max 1MB)")

        with open(context_path, encoding="utf-8") as handle:
            if context_path.suffix in {".md", ".markdown", ".txt"}:
                content = handle.read().strip()
                if not content:
                    raise context_error_cls("Context file is empty")
                context = {
                    "project_goal": content,
                    "description": content,
                }
            elif context_path.suffix in {".yaml", ".yml"}:
                context = yaml.safe_load(handle)
            elif context_path.suffix == ".json":
                context = json.load(handle)
            else:
                content = handle.read()
                try:
                    context = json.loads(content)
                except json.JSONDecodeError:
                    try:
                        context = yaml.safe_load(content)
                    except yaml.YAMLError as exc:
                        raise context_error_cls(f"Could not parse as JSON or YAML: {exc}")

        if not isinstance(context, dict):
            raise context_error_cls("Context must be a dictionary/object")

        valid_keys = {
            "project_goal",
            "data_sources",
            "use_case",
            "use_case_other",
            "complexity",
            "team_size",
            "domain",
            "canonical_model",
            "supporting_standards",
            "provider",
            "owner",
            "description",
            "technologies",
        }
        invalid_keys = set(context.keys()) - valid_keys
        if invalid_keys and console:
            console.print(
                f"[yellow]Warning:[/yellow] Unknown context keys: {', '.join(invalid_keys)}"
            )

        return context
    except context_error_cls:
        raise
    except Exception as exc:
        raise context_error_cls(f"Failed to load context: {exc}")
