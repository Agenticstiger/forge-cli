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
    """Show or reset project-scoped copilot memory and exit.

    ``--show-memory`` renders all three on-disk tiers (team / project /
    personal) in precedence order so the engineer can see which value
    wins.  This is issue #50 from the memory E2E findings — previously
    only the project tier was surfaced and personal/team values were
    invisible.  Provenance is shown next to each value so a confused
    "why is my provider gcp?" question is one ``--show-memory`` away
    from "(from personal memory)".
    """
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

    # --- Show-memory path: render all three on-disk tiers. ---
    layered = _collect_layered_memory(store)

    # ``--memory-json`` short-circuits to a stable machine-readable dump
    # for scripts (``fluid forge --show-memory --memory-json | jq``).
    # NOTE: write to ``sys.stdout`` directly rather than via ``cprint``
    # so Rich's terminal-width-aware line wrapping does NOT inject
    # newlines into long path strings (which would break ``jq``).
    if get_cli_arg(args, "memory_json", False):
        import sys

        payload = json.dumps(layered, indent=2, default=str, sort_keys=False)
        sys.stdout.write(payload + "\n")
        return 0

    if console:
        _render_layered_memory_panel(console, layered)
        return 0

    _render_layered_memory_plain(layered)
    return 0


# ---------------------------------------------------------------------
# Layered memory dump (--show-memory, issue #50)
# ---------------------------------------------------------------------


def _collect_layered_memory(store: CopilotMemoryStore) -> Dict[str, Any]:
    """Read every on-disk memory tier and return a stable structure.

    The output is the same whether the caller wants Rich rendering,
    plain text, or JSON — the renderers below just project this dict
    differently.  Keys are deliberately stable so external scripts
    relying on ``--memory-json`` aren't broken by future re-orderings.

    The precedence ladder rendered here mirrors the one called out at
    the top of :mod:`fluid_build.cli.forge_team_memory` and exercised
    by :func:`run_with_ai_copilot` — CLI args > discovery > team >
    project > personal > defaults.  We only have access to the
    *on-disk* tiers from this exit-path; the CLI-args / discovery
    tiers are rendered as ``"resolved at run-time"`` because they
    depend on an active forge invocation.
    """
    # Personal tier — lives in ``~/.fluid/personal-memory.json``.
    personal_raw: Dict[str, Any] = {}
    personal_path: Optional[Path] = None
    try:
        from fluid_build.cli.artifact_paths import user_personal_memory_path
        from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

        personal_path = user_personal_memory_path()
        personal_raw = load_personal_memory() or {}
    except Exception:  # noqa: BLE001 — best-effort
        pass

    # Team tier — lives in ``.fluid/team-memory.yaml`` at the workspace root.
    team_payload: Dict[str, Any] = {}
    team_summary: str = "empty"
    team_path: Optional[Path] = None
    try:
        from fluid_build.cli.forge_team_memory import TEAM_MEMORY_FILENAME, load_team_memory
        from fluid_build.cli.workspace_config import find_workspace_root as _find_ws

        ws_root = _find_ws(Path.cwd()) or Path.cwd()
        team_path = ws_root / ".fluid" / TEAM_MEMORY_FILENAME
        tm = load_team_memory(ws_root)
        if tm is not None:
            team_payload = tm.to_prompt_payload()
            team_summary = tm.summary_line()
    except Exception:  # noqa: BLE001
        pass

    # Project tier — lives in ``<product>/.fluid/copilot-memory.json``.
    project_summary: Dict[str, Any] = {}
    project_path = store.path
    try:
        memory = store.load()
        if memory is not None:
            project_summary = summarize_copilot_memory(memory)
    except Exception:  # noqa: BLE001
        pass

    return {
        "tiers": [
            {
                "tier": 1,
                "source": "CLI args / interview answers",
                "scope": "current run",
                "values": None,
                "note": "resolved at run-time; visible in AI Analysis panel",
            },
            {
                "tier": 2,
                "source": "Discovery report",
                "scope": "current workspace",
                "values": None,
                "note": "resolved at run-time; depends on files on disk",
            },
            {
                "tier": 3,
                "source": "Team memory",
                "path": str(team_path) if team_path else None,
                "exists": bool(team_payload),
                "summary": team_summary,
                "values": team_payload or None,
            },
            {
                "tier": 4,
                "source": "Project memory",
                "path": str(project_path),
                "exists": bool(project_summary),
                "values": project_summary or None,
            },
            {
                "tier": 5,
                "source": "Personal memory",
                "path": str(personal_path) if personal_path else None,
                "exists": bool(personal_raw),
                "values": personal_raw or None,
            },
            {
                "tier": 6,
                "source": "Built-in defaults",
                "scope": "fallback",
                "values": None,
                "note": "kicks in when no higher tier sets the value",
            },
        ],
        "precedence": ("CLI args > discovery > team > project > personal > built-in defaults"),
    }


def _render_layered_memory_panel(console: Any, layered: Dict[str, Any]) -> None:
    """Render the three-tier on-disk memory in a Rich-friendly panel."""
    lines: List[str] = [
        "**Memory layers (highest precedence → lowest)**",
        "",
    ]
    for tier in layered.get("tiers", []):
        n = tier.get("tier")
        source = tier.get("source", "unknown")
        path = tier.get("path")
        note = tier.get("note")
        scope = tier.get("scope")
        values = tier.get("values")

        # Header line for the tier.
        header = f"**{n}. {source}**"
        if path:
            header += f"  ([dim]{path}[/dim])"
        elif scope:
            header += f"  ([dim]{scope}[/dim])"
        lines.append(header)

        # Body — show concrete values when present, otherwise the note.
        if isinstance(values, dict) and values:
            for value_line in _format_tier_values(source, values):
                lines.append(f"   - {value_line}")
        elif note:
            lines.append(f"   - [dim]{note}[/dim]")
        else:
            lines.append("   - [dim](no values recorded)[/dim]")
        lines.append("")

    lines.append(f"[dim]Precedence: {layered.get('precedence', '')}[/dim]")
    show_lines_panel(console, lines, title="🧠 Memory Layers", border_style="cyan")


def _render_layered_memory_plain(layered: Dict[str, Any]) -> None:
    """Render the three-tier on-disk memory as plain text (no Rich)."""
    cprint("Memory layers (highest precedence → lowest)")
    cprint("─" * 50)
    for tier in layered.get("tiers", []):
        n = tier.get("tier")
        source = tier.get("source", "unknown")
        path = tier.get("path")
        note = tier.get("note")
        scope = tier.get("scope")
        values = tier.get("values")

        header = f"{n}. {source}"
        if path:
            header += f"  ({path})"
        elif scope:
            header += f"  ({scope})"
        cprint(header)

        if isinstance(values, dict) and values:
            for value_line in _format_tier_values(source, values):
                cprint(f"     - {value_line}")
        elif note:
            cprint(f"     - {note}")
        else:
            cprint("     - (no values recorded)")
    cprint("")
    cprint(f"Precedence: {layered.get('precedence', '')}")


def _format_tier_values(source: str, values: Mapping[str, Any]) -> List[str]:
    """Project a tier's payload into one human-readable line per slot.

    The formatting differs slightly per tier so the output reads as a
    summary, not a raw dump.  Tags every formatted line with
    ``(from <tier>)`` provenance so an engineer copy-pasting a line
    into a bug report can see which file it came from.
    """
    out: List[str] = []
    src = source.lower()

    # Common pretty-printer for scalar-ish keys.
    def _push(label: str, raw: Any) -> None:
        if raw is None or raw == "":
            return
        if isinstance(raw, (list, tuple)):
            if not raw:
                return
            shown = ", ".join(str(item) for item in list(raw)[:8])
            out.append(f"{label}: {shown} (from {source.lower()})")
            return
        if isinstance(raw, dict):
            if not raw:
                return
            # Compact dict: ``key=value`` pairs.
            shown = ", ".join(f"{k}={v}" for k, v in list(raw.items())[:6])
            out.append(f"{label}: {shown} (from {source.lower()})")
            return
        out.append(f"{label}: {raw} (from {source.lower()})")

    if "personal" in src:
        # Flat shape from ``load_personal_memory`` (preferred_* / recent_*).
        _push("provider", values.get("preferred_provider"))
        _push("engine", values.get("preferred_engine"))
        _push("domain", values.get("preferred_domain"))
        _push("owner_team", values.get("owner_team"))
        _push("ci_provider", values.get("preferred_ci_provider"))
        _push("ci_complexity", values.get("preferred_ci_complexity"))
        _push("recent_domains", values.get("recent_domains"))
        _push("recent_use_cases", values.get("recent_use_cases"))
        return out

    if "team" in src:
        # ``to_prompt_payload`` shape from TeamMemory.
        conventions = values.get("conventions") or {}
        if isinstance(conventions, dict):
            naming = conventions.get("naming") or {}
            if naming:
                _push("naming", naming)
            defaults = conventions.get("defaults") or {}
            if defaults:
                _push("defaults", defaults)
        decisions = values.get("decisions") or []
        if decisions:
            out.append(f"decisions: {len(decisions)} recorded (from team memory)")
        vocab = values.get("vocabulary") or {}
        if isinstance(vocab, dict):
            for k in ("entities", "measures", "dimensions"):
                _push(f"vocabulary.{k}", vocab.get(k))
        return out

    if "project" in src:
        # ``summarize_copilot_memory`` shape.
        _push("template", values.get("preferred_template"))
        _push("provider", values.get("preferred_provider"))
        _push("domain", values.get("preferred_domain"))
        _push("owner", values.get("preferred_owner"))
        _push("build_engines", values.get("build_engines"))
        _push("binding_formats", values.get("binding_formats"))
        _push("provider_hints", values.get("provider_hints"))
        sc = values.get("schema_summary_count")
        if sc:
            out.append(f"schema_summaries: {sc} (from project memory)")
        oc = values.get("recent_outcome_count")
        if oc:
            out.append(f"recent_outcomes: {oc} (from project memory)")
        return out

    # Unknown tier — best-effort dump.
    for k, v in list(values.items())[:8]:
        _push(k, v)
    return out


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
