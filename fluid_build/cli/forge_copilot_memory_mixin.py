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

"""Project-memory helpers for the Forge copilot agent."""

from __future__ import annotations

__all__ = [
    "CopilotProjectMemoryMixin",
]


import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.forge_copilot_memory import (
    build_copilot_project_memory,
    resolve_copilot_memory_root,
    summarize_copilot_memory,
)
from fluid_build.cli.forge_ui import show_lines_panel

LOG = logging.getLogger("fluid.cli.forge")


class CopilotProjectMemoryMixin:
    """Shared project-memory load, display, and save behavior for copilot."""

    def _resolve_memory_root(self, target_dir: Optional[Path]) -> Path:
        """Resolve the single canonical project-memory root for this run.

        Load, save, and management all funnel through this one resolver so
        a run reads and writes the SAME file — no workspace-vs-``target_dir``
        drift (Trello 69d42ad4). Borrowed from how ``git rev-parse
        --show-toplevel`` / ``pyprojroot`` resolve one canonical root and
        reuse it for every read and write.

        The first resolution within a run is cached on the instance, so a
        later save can never land in a different root than the load read
        from — even if on-disk state shifts mid-run.
        """
        cached = getattr(self, "_project_memory_root", None)
        if cached is not None:
            return cached
        root = resolve_copilot_memory_root(Path.cwd(), target_dir=target_dir)
        self._project_memory_root = root
        return root

    def _load_project_memory(self, *, enabled: bool, target_dir: Optional[Path]) -> Optional[Any]:
        project_root = self._resolve_memory_root(target_dir)
        store = self._make_memory_store_dependency(project_root)
        self._project_memory_enabled = enabled
        self._project_memory_path = store.path
        self._project_memory_snapshot = None

        if not enabled:
            self._emit_memory_load_feedback()
            return None

        memory = store.load()
        if not memory:
            self._emit_memory_load_feedback()
            return None
        self._project_memory_snapshot = memory.to_prompt_snapshot()
        self._emit_memory_load_feedback()
        return self._project_memory_snapshot

    def _emit_memory_load_feedback(self) -> None:
        summary_lines = self._build_memory_status_lines()
        if self.console:
            show_lines_panel(
                self.console,
                summary_lines,
                title="🧠 Project Memory",
                border_style="cyan",
            )
            return
        for line in summary_lines:
            cprint(line)

    def _build_memory_status_lines(self) -> List[str]:
        relative_path = self._relative_memory_path()
        if not self._project_memory_enabled:
            return [
                f"Project memory is disabled for this run (`--no-memory`). Path: `{relative_path}`"
            ]
        if not self._project_memory_snapshot:
            return [
                "No project-scoped copilot memory was found yet.",
                f"Copilot will rely on your current answers and discovery only. Path: `{relative_path}`",
            ]

        summary = summarize_copilot_memory(self._project_memory_snapshot)
        lines = [f"Loaded project memory from `{relative_path}`."]
        profile = ", ".join(
            [
                part
                for part in (
                    (
                        f"template={summary.get('preferred_template')}"
                        if summary.get("preferred_template")
                        else ""
                    ),
                    (
                        f"provider={summary.get('preferred_provider')}"
                        if summary.get("preferred_provider")
                        else ""
                    ),
                    (
                        f"domain={summary.get('preferred_domain')}"
                        if summary.get("preferred_domain")
                        else ""
                    ),
                )
                if part
            ]
        )
        if profile:
            lines.append(f"Saved profile: {profile}")
        if summary.get("build_engines"):
            lines.append(f"Remembered build engines: {', '.join(summary['build_engines'])}")
        lines.append(
            "Saved schema summaries: "
            f"{summary.get('schema_summary_count', 0)}; recent successful outcomes: {summary.get('recent_outcome_count', 0)}"
        )
        return lines

    def _relative_memory_path(self) -> str:
        if not self._project_memory_path:
            return "runtime/.state/copilot-memory.json"
        try:
            return self._project_memory_path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return str(self._project_memory_path)

    def _build_memory_save_preview_lines(self, memory: Any) -> List[str]:
        summary = summarize_copilot_memory(memory)
        lines = [f"Forge will save project memory to `{self._relative_memory_path()}` with:"]
        profile = ", ".join(
            [
                part
                for part in (
                    (
                        f"template={summary.get('preferred_template')}"
                        if summary.get("preferred_template")
                        else ""
                    ),
                    (
                        f"provider={summary.get('preferred_provider')}"
                        if summary.get("preferred_provider")
                        else ""
                    ),
                    (
                        f"domain={summary.get('preferred_domain')}"
                        if summary.get("preferred_domain")
                        else ""
                    ),
                    (
                        f"owner={summary.get('preferred_owner')}"
                        if summary.get("preferred_owner")
                        else ""
                    ),
                )
                if part
            ]
        )
        if profile:
            lines.append(profile)
        if summary.get("build_engines"):
            lines.append(f"build_engines={', '.join(summary['build_engines'])}")
        if summary.get("source_formats"):
            source_bits = ", ".join(
                f"{key}={value}" for key, value in sorted(summary["source_formats"].items())
            )
            lines.append(f"source_formats={source_bits}")
        lines.append(
            "bounded summaries: "
            f"{summary.get('schema_summary_count', 0)} schema summaries, {summary.get('recent_outcome_count', 0)} recent outcomes"
        )
        return lines

    def _maybe_save_project_memory(
        self,
        *,
        target_dir: Path,
        context: Dict[str, Any],
        suggestions: Dict[str, Any],
        generation_result: Any,
        copilot_options: Dict[str, Any],
        dry_run: bool,
    ) -> None:
        if dry_run:
            return

        options = SimpleNamespace(**(copilot_options or {}))
        # Resolve to the SAME canonical root the load path used, so a run
        # reads and writes one file (no workspace-vs-target_dir drift).
        project_root = self._resolve_memory_root(target_dir)
        store = self._make_memory_store_dependency(project_root)
        # Keep feedback paths (``Saved memory to X``) pointed at the file we
        # actually write — important when save runs without a prior load
        # (e.g. template mode).
        self._project_memory_path = store.path
        candidate_memory = build_copilot_project_memory(
            project_root=project_root,
            context=context,
            suggestions=suggestions,
            contract=generation_result.contract,
            discovery_report=generation_result.discovery_report,
            existing_memory=store.load(),
        )

        should_save = self._should_save_project_memory(options, candidate_memory)
        if not should_save:
            if getattr(options, "non_interactive", False) and not getattr(
                options, "save_memory", False
            ):
                note = "Project memory was not saved. Re-run with `--save-memory` to remember these conventions."
                if self.console:
                    self.console.print(f"[dim]{note}[/dim]")
                else:
                    cprint(note)
            return

        try:
            store.save(candidate_memory)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Failed to save copilot memory at %s: %s", store.path, exc)
            memory_path = self._relative_memory_path()
            if self.console:
                self.console.print(
                    f"[yellow]⚠ Could not save copilot memory to {memory_path}: {exc}[/yellow]"
                )
            else:
                warning(f"Could not save copilot memory: {exc}")
            return

        memory_path = self._relative_memory_path()
        if self.console:
            self.console.print(
                f"[green]✓[/green] Saved project-scoped copilot memory to {memory_path}"
            )
        else:
            success(f"Saved project-scoped copilot memory to {memory_path}")

    def _should_save_project_memory(self, options: SimpleNamespace, memory: Any) -> bool:
        if getattr(options, "non_interactive", False):
            return bool(getattr(options, "save_memory", False))

        prompt = f"Save project-scoped copilot memory to {self._relative_memory_path()}?"
        preview_lines = self._build_memory_save_preview_lines(memory)
        try:
            preview = "\n".join(preview_lines)
            return self._ask_confirmation_dependency(prompt, preview)
        except Exception:  # noqa: BLE001
            return False

    def _build_memory_guidance_lines(
        self,
        generation_result: Optional[Any],
    ) -> List[str]:
        if not self._project_memory_enabled:
            return ["Disabled for this run with `--no-memory`."]
        if not self._project_memory_snapshot:
            return [
                "No saved project memory was available, so only current context and discovery were used."
            ]
        summary = summarize_copilot_memory(self._project_memory_snapshot)
        lines = [
            "Loaded saved conventions"
            + (
                f" (`{summary.get('preferred_template')}` / `{summary.get('preferred_provider')}`)"
                if summary.get("preferred_template") or summary.get("preferred_provider")
                else ""
            )
            + "."
        ]
        decision = generation_result.scaffold_decision if generation_result else None
        if not decision:
            return lines
        lines.append(
            f"Template seed: `{decision.template}` from {self._friendly_source_name(decision.template_source)}."
        )
        lines.append(
            f"Provider seed: `{decision.provider}` from {self._friendly_source_name(decision.provider_source)}."
        )
        if (
            decision.template_source != "project_memory"
            or decision.provider_source != "project_memory"
        ):
            lines.append(
                "Saved memory was treated as a soft preference and did not override stronger current signals."
            )
        return lines

    def _friendly_source_name(self, source: Optional[str]) -> str:
        mapping = {
            "explicit_context": "your explicit input",
            "current_discovery": "current discovery",
            "heuristic_context": "your current answers",
            "project_memory": "saved project memory",
            "default": "safe defaults",
        }
        return mapping.get(source or "", "seed guidance")
