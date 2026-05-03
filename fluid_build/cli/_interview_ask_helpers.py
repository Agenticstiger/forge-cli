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
"""Interview ``_ask_*`` helpers — physical extraction from
``forge_copilot_interview.py``.

The bootstrap state machine + compose/refine flows stay in
``forge_copilot_interview.py``; the ~960 LOC of dialogs that prompt
for individual fields (``_ask_bootstrap_questions``,
``_ask_delivery_setup``, ``_ask_data_model_question``,
``_ask_engine_selection``, etc.) live here so the file count is bounded.

``forge_copilot_interview`` re-imports each helper at module top so
test patches that target the original namespace still resolve.
"""

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


# ── _ask_* helpers ────────────────────────────────────────────────


def _ask_bootstrap_questions(
    state: CopilotInterviewState,
    console: Any,
    *,
    discovery_report: DiscoveryReport,
    target_dir: Optional[Path] = None,
) -> None:
    if not console:
        return
    if not state.normalized_context.get("project_goal"):
        answer = ask_friendly_text(
            console,
            "What are you trying to build?",
            required=True,
        )
        if answer:
            state.apply_patch({"project_goal": answer}, source="interactive")
            state.record_turn(
                role="user",
                content=answer,
                field="project_goal",
                question_id="bootstrap_project_goal",
                raw_input=answer,
                resolved_value=answer,
                resolution_status="matched",
            )

    # ── Early scaffold: create samples/ and models/ dirs ────────────
    if target_dir is not None:
        _scaffold_data_dirs_and_prompt(
            state,
            console,
            target_dir=target_dir,
            discovery_report=discovery_report,
        )

    if not state.normalized_context.get("data_sources") and _discovery_is_thin(discovery_report):
        answer = ask_friendly_text(
            console,
            "What data sources or systems are involved? (leave blank if you're not sure yet)",
            required=False,
        )
        if answer:
            state.apply_patch({"data_sources": answer}, source="interactive")
            state.record_turn(
                role="user",
                content=answer,
                field="data_sources",
                question_id="bootstrap_data_sources",
                raw_input=answer,
                resolved_value=answer,
                resolution_status="matched",
            )

    if not state.normalized_context.get("data_model_source") and (
        discovery_report.user_data_models
        or discovery_report.sql_files
        or discovery_report.sample_files
    ):
        _ask_data_model_question(state, console, discovery_report=discovery_report)

    _ask_delivery_setup(state, console, discovery_report=discovery_report)

    # Ask about data modeling if domain expertise has modeling standards
    domain_expertise = state.normalized_context.get("domain_expertise") or {}
    if domain_expertise.get("data_modeling_standards") and not state.normalized_context.get(
        "data_modeling"
    ):
        answer = ask_friendly_text(
            console,
            "Do you want data modeling (entities, measures, dimensions + dbt models)? (yes/no)",
            required=False,
        )
        if answer and answer.strip().lower() in ("yes", "y", "yeah", "yep", "sure"):
            state.apply_patch({"data_modeling": True}, source="interactive")
            state.record_turn(
                role="user",
                content="yes",
                field="data_modeling",
                question_id="bootstrap_data_modeling",
                raw_input=answer,
                resolved_value="true",
                resolution_status="matched",
            )


def _ask_delivery_setup(
    state: CopilotInterviewState,
    console: Any,
    *,
    discovery_report: DiscoveryReport,
) -> None:
    """Collect data model, transformation, and scheduler choices together."""
    if not console:
        return
    try:
        console.print("\n[bold]Delivery setup[/bold]")
    except Exception:  # noqa: BLE001
        pass

    # Only re-ask when the user hasn't explicitly answered. The default
    # ("data_vault_2") is applied in ``bootstrap_interview_state`` with
    # ``source="default"``, which is the lowest precedence.
    modeling_technique_source = state.field_sources.get("data_modeling_technique")
    if modeling_technique_source in (None, "default"):
        _ask_data_modeling_technique(state, console)

    if not state.normalized_context.get("byot_path") and not state.normalized_context.get(
        "build_engine"
    ):
        _ask_transformation_delivery(state, console, discovery_report=discovery_report)

    if (
        not state.normalized_context.get("schedule_engine")
        and not state.normalized_context.get("byos_path")
        and _should_prompt_for_scheduler(state)
    ):
        _ask_scheduler_delivery(state, console, discovery_report=discovery_report)


def _suggest_modeling_default(state: CopilotInterviewState) -> str:
    """Choose a friendly modeling default from the user's wording."""
    text = " ".join(
        str(state.normalized_context.get(key) or "")
        for key in (
            "project_goal",
            "use_case",
            "use_case_other",
            "data_sources",
            "domain",
            "data_model_description",
        )
    ).lower()
    dimensional_tokens = (
        "dashboard",
        "dashboards",
        "report",
        "reporting",
        "bi",
        "metric",
        "metrics",
        "kpi",
        "scorecard",
        "analytics",
        "star schema",
        "kimball",
    )
    history_tokens = (
        "audit",
        "lineage",
        "history",
        "historical",
        "raw vault",
        "integration",
        "regulatory",
        "compliance",
        "governed",
        "cdc",
    )
    if any(token in text for token in dimensional_tokens):
        return "dimensional"
    if any(token in text for token in history_tokens):
        return "data_vault_2"
    return "data_vault_2"


def _scaffold_data_dirs_and_prompt(
    state: CopilotInterviewState,
    console: Any,
    *,
    target_dir: Path,
    discovery_report: DiscoveryReport,
) -> None:
    """Create samples/ + models/ dirs and prompt user to drop files."""
    samples_dir = target_dir / "samples"
    models_dir = target_dir / "models"
    target_dir.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(exist_ok=True)
    models_dir.mkdir(exist_ok=True)

    try:
        console.print(
            f"\n[green]Created project directory:[/green] {target_dir.name}/\n"
            f"  [cyan]samples/[/cyan]  ← your data files (CSV, Parquet, Avro, JSON)\n"
            f"  [cyan]models/[/cyan]   ← your data model [dim](optional — guides AI transformation design)[/dim]\n"
        )
    except Exception:  # noqa: BLE001
        pass

    # If sample data already exists (user pre-populated), skip the prompt
    existing_samples = list(samples_dir.glob("*"))
    data_files = [
        f
        for f in existing_samples
        if f.is_file()
        and f.suffix.lower() in {".csv", ".json", ".jsonl", ".parquet", ".pq", ".avro"}
    ]
    if data_files:
        # Already have data — just rescan
        from .forge_copilot_discovery import rescan_sample_data

        rescan_sample_data(target_dir, discovery_report)
        _print_discovered_data(console, discovery_report)
        return

    # No data yet — prompt user to drop files
    try:
        console.print(
            "[dim]Place your files now, then press Enter to continue (or Enter to skip)...[/dim]"
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        input()
    except (EOFError, KeyboardInterrupt):
        return

    # Re-scan after user drops files
    from .forge_copilot_discovery import rescan_sample_data

    rescan_sample_data(target_dir, discovery_report)
    _print_discovered_data(console, discovery_report)


def _print_discovered_data(console: Any, discovery_report: DiscoveryReport) -> None:
    """Print a summary of discovered sample files and data models."""
    if not console:
        return
    try:
        if discovery_report.sample_files:
            console.print("\n[green]Discovered:[/green]")
            for sample in discovery_report.sample_files:
                cols = sample.get("columns", {})
                col_names = list(cols.keys())[:4]
                col_preview = ", ".join(col_names)
                if len(cols) > 4:
                    col_preview += ", ..."
                path_name = Path(sample["path"]).name
                console.print(f"  [cyan]{path_name}[/cyan] — {len(cols)} columns ({col_preview})")
        if discovery_report.user_data_models:
            for model in discovery_report.user_data_models:
                path_name = Path(model["path"]).name
                console.print(
                    f"  [cyan]{path_name}[/cyan] — {model.get('tables', 0)} tables, "
                    f"{model.get('total_columns', 0)} columns "
                    f"[dim](used as transformation guardrails)[/dim]"
                )
        if not discovery_report.sample_files and not discovery_report.user_data_models:
            console.print("[dim]No data files found — continuing with AI generation only.[/dim]")
        console.print()
    except Exception:  # noqa: BLE001
        pass


def _ask_byot_question(
    state: CopilotInterviewState,
    console: Any,
) -> None:
    """Ask if user has existing transformation code (BYOT)."""
    answer = ask_friendly_text(
        console,
        "Do you have existing transformation code? (local path / git URL / Enter to generate)",
        required=False,
    )
    if answer and answer.strip():
        trimmed = answer.strip()
        state.apply_patch({"byot_path": trimmed}, source="interactive")
        state.record_turn(
            role="user",
            content=trimmed,
            field="byot_path",
            question_id="bootstrap_byot",
            raw_input=answer,
            resolved_value=trimmed,
            resolution_status="matched",
        )
        try:
            console.print(f"[green]Using existing transformation:[/green] {trimmed}")
        except Exception:  # noqa: BLE001
            pass


def _ask_transformation_delivery(
    state: CopilotInterviewState,
    console: Any,
    *,
    discovery_report: DiscoveryReport,
) -> None:
    """Ask for the transformation code path without mixing in scheduling."""
    choices = [
        {
            "label": "Generate dbt SQL models",
            "value": "generate_dbt",
            "aliases": ["dbt", "generate", "generate dbt", "sql models", "sql"],
        },
        {
            "label": "Choose another transformation engine",
            "value": "choose_engine",
            "aliases": ["other", "engine", "another engine", "spark", "dataform"],
        },
        {
            "label": "Use existing transformation code",
            "value": "existing",
            "aliases": ["existing", "byot", "path", "repo", "git"],
        },
    ]
    # Stage banner — the previous prompt was the modeling-technique
    # question (history vs reporting); jumping straight to
    # ``Transformation [dbt / other / existing]`` reads like the
    # interview pivoted out of nowhere.  This one-liner names the
    # stage so the operator knows we're now picking the build engine
    # that will materialise the model.
    if console is not None:
        try:
            console.print(
                "\n[bold]Transformation engine[/bold]"
                "  [dim]— pick the tool that will build your data model from sources.[/dim]"
            )
        except Exception:  # noqa: BLE001 — non-fatal banner
            pass
    raw_answer = ask_friendly_text(
        console,
        "Transformation [dbt / other / existing]",
        required=False,
        default="dbt",
    )
    match = resolve_choice_input(
        field_name="transformation_delivery",
        raw_input=raw_answer,
        choices=choices,
        allow_skip=True,
    )
    raw_input = (match.raw_input or "").strip()
    if raw_input and _looks_like_existing_artifact_ref(raw_input):
        trimmed = raw_input
        state.apply_patch({"byot_path": trimmed}, source="interactive")
        state.record_turn(
            role="user",
            content=trimmed,
            field="byot_path",
            question_id="bootstrap_byot",
            raw_input=match.raw_input,
            resolved_value=trimmed,
            resolution_status=match.status or "matched",
        )
        return
    value = match.value if match.status in {"matched", "confirmed", "custom"} else None
    if not value and raw_input:
        trimmed = raw_input
        state.apply_patch({"byot_path": trimmed}, source="interactive")
        state.record_turn(
            role="user",
            content=trimmed,
            field="byot_path",
            question_id="bootstrap_byot",
            raw_input=match.raw_input,
            resolved_value=trimmed,
            resolution_status=match.status or "matched",
        )
        return
    if value == "existing":
        _ask_byot_question(state, console)
        return
    if value == "choose_engine":
        _ask_engine_selection(state, console, discovery_report=discovery_report)
        return

    state.apply_patch({"build_engine": "dbt"}, source="interactive")
    state.record_turn(
        role="user",
        content="dbt",
        field="build_engine",
        question_id="bootstrap_transformation",
        raw_input=match.raw_input or "",
        resolved_value="dbt",
        resolution_status=match.status or "matched",
    )


def _ask_scheduler_delivery(
    state: CopilotInterviewState,
    console: Any,
    *,
    discovery_report: DiscoveryReport,
) -> None:
    """Ask the optional scheduler decision once; blank means no scheduler."""
    try:
        from fluid_build.schedulers import list_schedulers, list_schedulers_for_platform

        provider = state.normalized_context.get("provider", "")
        available = list_schedulers_for_platform(provider) if provider else list_schedulers()
    except ImportError:
        available = []
    available = sorted({*available, *KNOWN_SCHEDULER_ENGINES})

    choices = [{"label": "No scheduler", "value": "none", "aliases": ["no", "none", "skip"]}]
    choices.extend(
        {
            "label": scheduler.title(),
            "value": scheduler,
            "aliases": [scheduler.replace("_", " "), scheduler],
        }
        for scheduler in available
    )
    choices.append(
        {
            "label": "Use existing schedule/DAG",
            "value": "existing",
            "aliases": ["existing", "byos", "dag", "schedule path"],
        }
    )

    scheduler_options = ["none", *available, "existing"]
    raw_answer = ask_friendly_text(
        console,
        "Scheduler [" + " / ".join(scheduler_options) + "]",
        required=False,
        default="none",
    )
    match = resolve_choice_input(
        field_name="schedule_delivery",
        raw_input=raw_answer,
        choices=choices,
        allow_skip=True,
    )
    raw_input = (match.raw_input or "").strip()
    if raw_input and _looks_like_existing_artifact_ref(raw_input):
        trimmed = raw_input
        state.apply_patch({"byos_path": trimmed}, source="interactive")
        state.record_turn(
            role="user",
            content=trimmed,
            field="byos_path",
            question_id="bootstrap_byos",
            raw_input=match.raw_input,
            resolved_value=trimmed,
            resolution_status=match.status or "matched",
        )
        return
    value = match.value if match.status in {"matched", "confirmed", "custom"} else None
    if not value and raw_input:
        trimmed = raw_input
        state.apply_patch({"byos_path": trimmed}, source="interactive")
        state.record_turn(
            role="user",
            content=trimmed,
            field="byos_path",
            question_id="bootstrap_byos",
            raw_input=match.raw_input,
            resolved_value=trimmed,
            resolution_status=match.status or "matched",
        )
        return
    if not value or value == "none":
        state.record_turn(
            role="user",
            content="none",
            field="schedule_engine",
            question_id="bootstrap_scheduler",
            raw_input=match.raw_input or "",
            resolved_value="",
            resolution_status=match.status or "matched",
        )
        return
    if value == "existing":
        _ask_byos_question(state, console)
        return

    state.apply_patch({"schedule_engine": value}, source="interactive")
    state.record_turn(
        role="user",
        content=value,
        field="schedule_engine",
        question_id="bootstrap_scheduler",
        raw_input=match.raw_input or value,
        resolved_value=value,
        resolution_status=match.status or "matched",
    )


def _should_prompt_for_scheduler(state: CopilotInterviewState) -> bool:
    """Return True only when the user has signaled scheduling intent."""
    context = state.normalized_context
    if (
        context.get("schedule_engine")
        or context.get("byos_path")
        or context.get("orchestration_pattern")
    ):
        return True
    text = " ".join(
        str(context.get(key) or "")
        for key in (
            "project_goal",
            "data_sources",
            "use_case",
            "use_case_other",
            "refresh_cadence",
            "trigger_type",
            "output_kind",
        )
    ).lower()
    if not text.strip():
        return False
    scheduler_tokens = (
        "airflow",
        "dagster",
        "prefect",
        " dag",
        "dags",
        "scheduler",
        "scheduled",
        "schedule",
        "orchestration",
        "orchestrate",
        "cron",
        "trigger",
        "nightly",
        "hourly",
        "daily run",
        "run daily",
        "batch window",
    )
    return any(token in text for token in scheduler_tokens)


def _looks_like_existing_artifact_ref(value: str) -> bool:
    text = str(value or "").strip()
    lower = text.lower()
    return (
        "/" in text or "\\" in text or "://" in text or lower.startswith(("git@", "./", "../", "~"))
    )


def _ask_data_model_question(
    state: CopilotInterviewState,
    console: Any,
    *,
    discovery_report: DiscoveryReport,
) -> None:
    # V1.5 — auto-discover configured metadata-source catalogs so
    # users with an existing Snowflake / Unity / DataHub setup see
    # their catalog as the first option (and the default when one
    # is configured).  The label surfaces the source TYPE so the
    # operator can tell the credential id is reading from their
    # ``~/.fluid/sources.yaml`` rather than a hardcoded constant.
    configured_sources = _list_configured_sources()

    default = _default_data_model_source(discovery_report, configured_sources)

    choices = []
    if configured_sources:
        # Catalog branch goes FIRST when something is configured
        # — surfaces the highest-value option without scrolling.
        if len(configured_sources) == 1:
            cred_id, source_type = configured_sources[0]
            first_label = f"Use my {_format_source_label(cred_id, source_type)}"
        else:
            previewed = ", ".join(
                _format_source_label(cred_id, source_type)
                for cred_id, source_type in configured_sources[:3]
            )
            tail = f" + {len(configured_sources) - 3} more" if len(configured_sources) > 3 else ""
            first_label = f"Use a catalog I have configured ({previewed}{tail})"
        choices.append({"label": first_label, "value": "source"})
    choices.extend(
        [
            {"label": "DDL files", "value": "ddl"},
            {"label": "Business intent", "value": "intent"},
            {"label": "Sample data only", "value": "samples"},
            {"label": "Describe it in chat", "value": "chat"},
            {"label": "Start blank", "value": "blank"},
        ]
    )
    if not configured_sources:
        # Even when no source is configured, show the option so
        # discovery is consistent — but route to the wizard hint
        # rather than enumerating empty choices.
        choices.append(
            {
                "label": "Configure a metadata source (Snowflake / Unity / BigQuery / Glue / DataHub / DMM)",
                "value": "source-setup",
            }
        )

    prompt = "Do you have a data model yet? " "[" + " / ".join(c["value"] for c in choices) + "]"
    match = ask_flexible_choice(
        console,
        prompt=prompt,
        field_name="data_model_source",
        choices=choices,
        required=False,
        allow_skip=True,
        default=default,
    )
    source = normalize_interview_value("data_model_source", match.value or default)
    if not source:
        return
    state.apply_patch({"data_model_source": source}, source="interactive")
    state.record_turn(
        role="user",
        content=match.label or source,
        field="data_model_source",
        question_id="bootstrap_data_model_source",
        raw_input=match.raw_input or source,
        resolved_value=source,
        resolution_status=match.status or "matched",
    )

    if source == "ddl":
        discovered = [
            str(model.get("path"))
            for model in discovery_report.user_data_models
            if str(model.get("path", "")).lower().endswith(".sql")
        ] or [entry.get("path") for entry in discovery_report.sql_files if entry.get("path")]
        ddl_answer = ask_friendly_text(
            console,
            "Point me at the DDL file(s), or press Enter to use the discovered SQL files",
            required=False,
            default=" ".join(discovered[:4]) if discovered else None,
        )
        if ddl_answer:
            state.apply_patch({"data_model_paths": ddl_answer}, source="interactive")
    elif source == "intent":
        discovered = [
            str(model.get("path"))
            for model in discovery_report.user_data_models
            if str(model.get("path", "")).lower().endswith((".yaml", ".yml", ".json"))
        ]
        intent_answer = ask_friendly_text(
            console,
            "Point me at the intent file, or press Enter to keep using the discovered model files",
            required=False,
            default=discovered[0] if discovered else None,
        )
        if intent_answer:
            state.apply_patch({"data_model_paths": intent_answer}, source="interactive")
    elif source == "chat":
        description = ask_friendly_text(
            console,
            "Describe the model in a sentence or two",
            required=False,
        )
        if description:
            state.apply_patch({"data_model_description": description}, source="interactive")
    elif source == "source":
        # V1.5 — user picked the catalog branch. Capture which
        # configured source (when more than one is set up) plus the
        # database/schema scope. The actual forge later runs through
        # ``run_from_source_command`` (or the MCP forge_from_source
        # tool) so this prompt is metadata-only.
        chosen_source = configured_sources[0] if configured_sources else None
        if len(configured_sources) > 1:
            sub_match = ask_flexible_choice(
                console,
                prompt=("Which configured catalog? " f"[{' / '.join(configured_sources)}]"),
                field_name="data_model_source_name",
                choices=[{"label": s, "value": s} for s in configured_sources],
                required=False,
                allow_skip=True,
                default=configured_sources[0],
            )
            chosen_source = sub_match.value or configured_sources[0]
        if chosen_source:
            state.apply_patch({"data_model_source_name": chosen_source}, source="interactive")
        scope_answer = ask_friendly_text(
            console,
            "What scope should we forge from? (e.g. '<database>.<schema>' "
            "for Snowflake, '<catalog>.<schema>' for Unity, "
            "'<project>.<dataset>' for BigQuery; press Enter to skip)",
            required=False,
        )
        if scope_answer:
            state.apply_patch(
                {"data_model_source_scope": scope_answer.strip()},
                source="interactive",
            )
    elif source == "source-setup":
        # User has no configured catalog yet — point them at the
        # wizard. This is intentionally just a hint; the wizard
        # itself runs as a separate ``fluid ai setup --source ...``
        # command (Sprint C). For now, we surface the next-action
        # so the user knows what to do.
        try:
            cprint = __import__("fluid_build.cli.console", fromlist=["cprint"]).cprint
        except Exception:
            cprint = print
        cprint(
            "No metadata-source catalog configured yet.\n"
            "  Run: fluid ai setup --source snowflake   (or unity / bigquery / glue / datahub / datamesh_manager)\n"
            "  Then re-run `fluid forge` and pick the catalog branch."
        )
        # Fall through to "blank" so the interview doesn't dead-end.
        state.apply_patch({"data_model_source": "blank"}, source="interactive")

    review_answer = ask_friendly_text(
        console,
        "Review the forged model before generation? (yes/no)",
        required=False,
        default="yes" if source in {"ddl", "intent", "chat"} else "no",
    )
    normalized_review = normalize_interview_value("review_data_model", review_answer)
    if normalized_review is not None:
        state.apply_patch({"review_data_model": normalized_review}, source="interactive")


def _list_configured_sources() -> list[tuple[str, str]]:
    """Return ``(credential_id, source_type)`` pairs for configured catalogs.

    Reads ``~/.fluid/sources.yaml`` if present and returns one tuple
    per saved source.  Empty list when the file is missing /
    malformed / has no entries — the interview still works, just
    without the catalog branch as a default.

    The source-type is plumbed through alongside the credential id
    so the interview can render labels like
    ``Snowflake source (<credential_id>)`` instead of bare
    credential ids that read like a hardcoded constant.  When a
    YAML entry is missing the ``source_type`` field, ``"source"``
    is used as the generic fallback.

    Defensive: catches every exception so a corrupted YAML never
    blocks the interview from running.
    """
    try:
        from pathlib import Path

        import yaml  # type: ignore

        path = Path.home() / ".fluid" / "sources.yaml"
        if not path.is_file():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        sources = data.get("sources")
        if not isinstance(sources, dict):
            return []
        out: list[tuple[str, str]] = []
        for name, entry in sources.items():
            if not isinstance(name, str):
                continue
            source_type = ""
            if isinstance(entry, dict):
                raw = entry.get("source_type")
                if isinstance(raw, str):
                    source_type = raw.strip().lower()
            out.append((name, source_type or "source"))
        return out
    except Exception:  # noqa: BLE001 — defensive
        return []


_SOURCE_TYPE_DISPLAY: dict[str, str] = {
    "snowflake": "Snowflake",
    "unity": "Unity Catalog",
    "bigquery": "BigQuery",
    "dataplex": "Dataplex",
    "glue": "AWS Glue",
    "datahub": "DataHub",
    "datamesh_manager": "Data Mesh Manager",
    "source": "Saved",
}


def _format_source_label(credential_id: str, source_type: str) -> str:
    """Render a single configured source as ``<TypeName> source (<id>)``."""

    pretty = _SOURCE_TYPE_DISPLAY.get(source_type, source_type.title() or "Saved")
    return f"{pretty} source ({credential_id})"


def _default_data_model_source(
    discovery_report: DiscoveryReport,
    configured_sources: Optional[list[tuple[str, str]]] = None,
) -> Optional[str]:
    # V1.5 — when a metadata-source catalog is configured, default
    # to the catalog branch. This puts the highest-value option in
    # front of users who already invested in Snowflake / Unity / etc.
    # without making it harder for users with local DDL / intent.
    if configured_sources:
        return "source"
    for model in discovery_report.user_data_models:
        path = str(model.get("path", "")).lower()
        if path.endswith(".sql"):
            return "ddl"
        if path.endswith((".yaml", ".yml", ".json")):
            return "intent"
    if discovery_report.sql_files:
        return "ddl"
    if discovery_report.sample_files:
        return "samples"
    return "blank"


def _ask_engine_selection(
    state: CopilotInterviewState,
    console: Any,
    *,
    discovery_report: DiscoveryReport,
) -> None:
    """Ask which transformation engine to use, filtered by platform."""
    try:
        from fluid_build.engines import list_engines, list_engines_for_platform

        # Filter by platform if known
        provider = state.normalized_context.get("provider", "")
        if provider:
            available = list_engines_for_platform(provider)
        else:
            available = list_engines()

        if not available:
            return

        choices_str = " / ".join(available)
        answer = ask_friendly_text(
            console,
            f"Transformation engine [{choices_str}]",
            required=False,
            default=available[0] if available else None,
        )
        if answer:
            engine_name = answer.strip().lower()
            if engine_name in available:
                state.apply_patch({"build_engine": engine_name}, source="interactive")
                state.record_turn(
                    role="user",
                    content=engine_name,
                    field="build_engine",
                    question_id="bootstrap_engine",
                    raw_input=answer,
                    resolved_value=engine_name,
                    resolution_status="matched",
                )
            elif engine_name:
                # Accept unknown engine names too — the contract schema supports custom
                state.apply_patch({"build_engine": engine_name}, source="interactive")
        elif available:
            # Default to first available engine
            state.apply_patch({"build_engine": available[0]}, source="interactive")
    except ImportError:
        pass  # engines module not available


def _ask_data_modeling_technique(
    state: CopilotInterviewState,
    console: Any,
) -> None:
    """Ask the user to pick a data modeling technique in business wording.

    Runs as a bootstrap question right after the schedule step when the
    current value came from the default precedence — so explicit answers
    (project_memory, CLI, LLM) always take priority.  The helper is a
    no-op when ``console`` is falsy; the non-interactive default is
    applied in :func:`bootstrap_interview_state`.
    """
    if not console:
        return

    default_value = _suggest_modeling_default(state)
    default_label = "reporting" if default_value == "dimensional" else "history"

    choices = [
        {
            "label": "History / audit model",
            "value": "data_vault_2",
            "aliases": list(_DATA_VAULT_2_ALIASES),
        },
        {
            "label": "Reporting / star model",
            "value": "dimensional",
            "aliases": list(_DIMENSIONAL_ALIASES),
        },
    ]
    match = ask_flexible_choice(
        console,
        prompt=(
            "Data model [history / reporting / not sure] "
            "[dim](history keeps changes; reporting creates facts and dimensions)[/dim]"
        ),
        field_name="data_modeling_technique",
        choices=choices,
        required=False,
        allow_skip=True,
        default=default_label,
    )
    resolved = match.value if match.status in {"matched", "confirmed", "custom"} else None
    resolved = normalize_interview_value("data_modeling_technique", resolved) or default_value

    state.apply_patch({"data_modeling_technique": resolved}, source="interactive")
    state.record_turn(
        role="user",
        content=resolved,
        field="data_modeling_technique",
        question_id="bootstrap_data_modeling_technique",
        raw_input=match.raw_input or "",
        resolved_value=resolved,
        resolution_status=match.status or "matched",
    )


def _ask_byos_question(
    state: CopilotInterviewState,
    console: Any,
) -> None:
    """Ask if user has an existing schedule/DAG (BYOS — Bring Your Own Schedule)."""
    answer = ask_friendly_text(
        console,
        "Do you have an existing DAG/schedule? (local path / git URL / Enter to generate)",
        required=False,
    )
    if answer and answer.strip():
        trimmed = answer.strip()
        state.apply_patch({"byos_path": trimmed}, source="interactive")
        state.record_turn(
            role="user",
            content=trimmed,
            field="byos_path",
            question_id="bootstrap_byos",
            raw_input=answer,
            resolved_value=trimmed,
            resolution_status="matched",
        )
        try:
            console.print(f"[green]Using existing schedule:[/green] {trimmed}")
        except Exception:  # noqa: BLE001
            pass


def _ask_dynamic_questions(
    state: CopilotInterviewState,
    console: Any,
    questions: List[InterviewQuestion],
) -> None:
    for question in questions[:INTERVIEW_MAX_QUESTIONS_PER_ROUND]:
        result = ask_interview_question(console, question)
        if result.context_patch:
            state.apply_patch(result.context_patch, source="interactive")
        content = result.raw_input or str(result.value or "").strip()
        if not content:
            continue
        state.record_turn(
            role="user",
            content=content,
            field=question.field,
            question_id=question.id,
            raw_input=result.raw_input,
            resolved_value=result.value,
            resolution_status=result.resolution_status,
        )


def _discovery_is_thin(discovery_report: DiscoveryReport) -> bool:
    return not any(
        (
            discovery_report.detected_sources,
            discovery_report.sql_files,
            discovery_report.dbt_projects,
            discovery_report.terraform_projects,
            discovery_report.existing_contracts,
            discovery_report.provider_hints,
        )
    )
