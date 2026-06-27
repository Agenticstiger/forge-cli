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

"""Shared friendly dialog helpers for interactive Forge experiences."""

from __future__ import annotations

__all__ = [
    "ChoiceMatchResult",
    "DialogQuestionResult",
    "ask_confirmation",
    "ask_dialog_question",
    "ask_flexible_choice",
    "ask_friendly_text",
    "ask_secret_text",
    "build_choice",
    "normalize_choice_value",
    "normalize_prompt_choices",
    "resolve_choice_input",
]


import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from difflib import SequenceMatcher
from getpass import getpass
from typing import Any, Dict, List, Mapping, Optional

# Import ``normalize_provider_name`` from its LIGHT defining module rather than
# the heavy ``forge_copilot_runtime`` re-export. ``forge_copilot_runtime`` pulls
# the LLM/discovery stack (httpx) and the schema_manager chain (jsonschema),
# which would drag the whole forge AI runtime onto the ``fluid --help`` cold
# path (forge_dialogs → forge_agent_specs → forge_agents → forge.register).
# ``forge_copilot_contract_helpers`` is the canonical definition site and is
# dependency-light.
from .forge_copilot_contract_helpers import normalize_provider_name
from .forge_copilot_taxonomy import normalize_use_case

try:
    from rich.panel import Panel

    RICH_PANEL_AVAILABLE = True
except ImportError:
    RICH_PANEL_AVAILABLE = False

try:
    from rich.prompt import Prompt

    RICH_PROMPT_AVAILABLE = True
except ImportError:  # pragma: no cover - covered by non-Rich fallback paths
    Prompt = None  # type: ignore[assignment]
    RICH_PROMPT_AVAILABLE = False

CHOICE_HINT = "You can type an option, a short phrase, or describe it in your own words."
CONFIRM_HINT = "You can answer with yes/no, y/n, or a short confirmation."
SKIP_TOKENS = {
    "skip",
    "skip for now",
    "not sure",
    "unsure",
    "i don't know",
    "dont know",
    "idk",
    "no preference",
}

PROVIDER_ALIASES = {
    "gcp": "gcp",
    "google cloud": "gcp",
    "bigquery": "gcp",
    "bq": "gcp",
    "gcs": "gcp",
    "composer": "gcp",
    "snowflake": "snowflake",
    "aws": "aws",
    "s3": "aws",
    "athena": "aws",
    "redshift": "aws",
    "glue": "aws",
    "local": "local",
    "local files": "local",
    "filesystem": "local",
    "csv": "local",
}

BUILD_ENGINE_ALIASES = {
    "sql": "sql",
    "python": "python",
    "py": "python",
    "dbt": "dbt",
    "dataform": "dataform",
    "glue": "glue",
}

OUTPUT_KIND_ALIASES = {
    "table": "table",
    "tables": "table",
    "view": "view",
    "views": "view",
    "dataset": "dataset",
    "semantic model": "dataset",
    "file": "file",
    "files": "file",
    "csv": "file",
    "parquet": "file",
}

COMPLEXITY_ALIASES = {
    "simple": "simple",
    "easy": "simple",
    "basic": "simple",
    "starter": "simple",
    "intermediate": "intermediate",
    "medium": "intermediate",
    "normal": "intermediate",
    "standard": "intermediate",
    "advanced": "advanced",
    "complex": "advanced",
    "enterprise": "advanced",
}

TEAM_SIZE_ALIASES = {
    "solo": "solo",
    "one person": "solo",
    "just me": "solo",
    "small": "small (2-5)",
    "small team": "small (2-5)",
    "two to five": "small (2-5)",
    "medium": "medium (6-15)",
    "mid sized": "medium (6-15)",
    "six to fifteen": "medium (6-15)",
    "large": "large (15+)",
}

BOOLEAN_ALIASES = {
    "yes": "yes",
    "y": "yes",
    "yeah": "yes",
    "yep": "yes",
    "sure": "yes",
    "ok": "yes",
    "okay": "yes",
    "continue": "yes",
    "proceed": "yes",
    "save": "yes",
    "overwrite": "yes",
    "no": "no",
    "n": "no",
    "nope": "no",
    "cancel": "no",
    "stop": "no",
    "not now": "no",
    "skip": "no",
}

CUSTOM_MEANING_FIELDS = {"use_case"}
YES_NO_FIELDS = {
    "confirm",
    "real_time",
    "real_time_personalization",
    "hipaa_required",
}


@dataclass
class ChoiceMatchResult:
    status: str
    value: Optional[str] = None
    label: str = ""
    confidence: float = 0.0
    candidates: List[Dict[str, Any]] = dc_field(default_factory=list)
    raw_input: str = ""


@dataclass
class DialogQuestionResult:
    value: Any = None
    raw_input: str = ""
    resolution_status: str = ""
    context_patch: Dict[str, Any] = dc_field(default_factory=dict)


def build_choice(
    label: str, value: Optional[str] = None, aliases: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Build a labeled choice entry for friendly dialog prompts."""
    return {
        "label": str(label).strip(),
        "value": str(value or label).strip(),
        "aliases": [str(alias).strip() for alias in aliases or [] if str(alias).strip()],
    }


def normalize_prompt_choices(choices: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Normalize string or mapping choices into a stable prompt shape."""
    normalized: List[Dict[str, Any]] = []
    for raw_choice in choices or []:
        aliases: List[str] = []
        if isinstance(raw_choice, Mapping):
            label = str(raw_choice.get("label") or "").strip()
            value = str(raw_choice.get("value") or label).strip()
            aliases = [
                _normalize_match_text(alias)
                for alias in (raw_choice.get("aliases") or [])
                if str(alias or "").strip()
            ]
        else:
            label = str(raw_choice or "").strip()
            value = label
        if label:
            normalized.append(
                {
                    "label": label,
                    "value": value or label,
                    "aliases": [alias for alias in aliases if alias],
                }
            )
    return normalized


def ask_dialog_question(console: Any, question: Any) -> DialogQuestionResult:
    """Ask a question using friendly free-text collection with soft choice matching."""
    if getattr(question, "type", "text") == "choice" and getattr(question, "choices", None):
        match = ask_flexible_choice(
            console,
            prompt=getattr(question, "prompt", "Tell me more."),
            field_name=getattr(question, "field", ""),
            choices=getattr(question, "choices", []),
            required=bool(getattr(question, "required", False)),
            allow_skip=bool(getattr(question, "allow_skip", True)),
            default=getattr(question, "default", None),
        )
        context_patch: Dict[str, Any] = {}
        field_name = str(getattr(question, "field", "") or "").strip()
        if match.status in {"matched", "confirmed"} and match.value is not None:
            context_patch[field_name] = match.value
        elif match.status == "custom" and field_name == "use_case":
            context_patch["use_case"] = "other"
            if match.raw_input:
                context_patch["use_case_other"] = match.raw_input
        elif match.status == "custom" and match.value is not None:
            context_patch[field_name] = match.value

        raw_input = match.raw_input
        if (
            field_name == "use_case"
            and context_patch.get("use_case") == "other"
            and not context_patch.get("use_case_other")
        ):
            follow_up = ask_friendly_text(
                console,
                "Tell me a bit more about the use case. Even a short phrase is enough. (press Enter to skip)",
                required=False,
            )
            if follow_up:
                context_patch["use_case_other"] = follow_up
                raw_input = follow_up

        return DialogQuestionResult(
            value=match.value,
            raw_input=raw_input,
            resolution_status=match.status,
            context_patch=context_patch,
        )

    answer = ask_friendly_text(
        console,
        getattr(question, "prompt", "Tell me more."),
        required=bool(getattr(question, "required", False)),
        default=getattr(question, "default", None),
    )
    field_name = str(getattr(question, "field", "") or "").strip()
    return DialogQuestionResult(
        value=answer,
        raw_input=str(answer or "").strip(),
        resolution_status="matched" if answer else "skipped",
        context_patch={field_name: answer} if answer and field_name else {},
    )


def ask_friendly_text(
    console: Any,
    prompt: str,
    *,
    required: bool,
    default: Optional[str] = None,
) -> Optional[str]:
    """Ask a free-text question with one gentle retry for required fields.

    Phase 0.5 #19 — slash commands like ``:ai-setup`` are detected
    BEFORE the answer is treated as content. The user can re-enter
    AI setup, toggle ``--show-work``, or abort gracefully without
    leaving the interview. After a slash command runs, the prompt
    repeats so the user can answer the original question.
    """
    from fluid_build.cli._interview_slash_commands import maybe_handle_slash_command

    # Up to 5 successive slash commands, then resume the prompt.
    for _ in range(5):
        answer = _read_free_text(console, prompt, default=default)
        if answer and maybe_handle_slash_command(answer, console=console):
            continue
        if answer:
            return answer
        break

    if not required:
        return None
    if console:
        console.print("[dim]A short answer is enough here. A phrase works fine.[/dim]")
    retry = _read_free_text(console, prompt, default=default)
    if retry and maybe_handle_slash_command(retry, console=console):
        # Slash command on the retry — give one more chance.
        retry = _read_free_text(console, prompt, default=default)
    return retry or None


def ask_secret_text(
    console: Any,
    prompt: str,
    *,
    required: bool,
) -> Optional[str]:
    """Ask for sensitive input without echoing it back to the terminal."""
    answer = _read_secret_text(console, prompt)
    if answer:
        return answer
    if not required:
        return None
    if console:
        console.print(
            "[dim]This value won't be displayed. It may be saved to your system keychain.[/dim]"
        )
    retry = _read_secret_text(console, prompt)
    return retry or None


def ask_flexible_choice(
    console: Any,
    *,
    prompt: str,
    field_name: str,
    choices: List[Any],
    required: bool,
    allow_skip: bool,
    default: Optional[str] = None,
) -> ChoiceMatchResult:
    """Ask a choice-like question without enforcing exact matches."""
    normalized_choices = normalize_prompt_choices(list(choices or []))
    if console:
        _render_suggested_options(console, normalized_choices, default=default)
    raw_input = _read_free_text(console, prompt, default=default if not required else None)
    if not raw_input and required and default:
        raw_input = default
    if not raw_input and required and not default:
        if console:
            console.print(f"[dim]{CHOICE_HINT}[/dim]")
        raw_input = _read_free_text(console, prompt)

    match = resolve_choice_input(
        field_name=field_name,
        raw_input=raw_input,
        choices=normalized_choices,
        allow_skip=allow_skip or not required,
    )
    if match.status != "ambiguous":
        return match

    if console:
        primary = match.candidates[0] if match.candidates else {}
        alternative = match.candidates[1] if len(match.candidates) > 1 else {}
        clarification = f"I think you mean '{primary.get('label', '')}'. Press Enter to use that"
        if alternative:
            clarification += f", or type '{alternative.get('label', '')}' if that's closer"
        clarification += "."
        console.print(f"[dim]{clarification}[/dim]")
    retry = _read_free_text(console, prompt="> ")
    if not retry:
        primary = match.candidates[0]
        return ChoiceMatchResult(
            status="confirmed",
            value=primary["value"],
            label=primary["label"],
            confidence=primary.get("score", match.confidence),
            candidates=match.candidates,
            raw_input=match.raw_input,
        )
    follow_up_match = resolve_choice_input(
        field_name=field_name,
        raw_input=retry,
        choices=normalized_choices,
        allow_skip=allow_skip or not required,
    )
    if follow_up_match.status == "ambiguous" and field_name in CUSTOM_MEANING_FIELDS:
        return ChoiceMatchResult(
            status="custom",
            value="other" if field_name == "use_case" else retry,
            label=retry,
            confidence=0.4,
            raw_input=retry,
        )
    return follow_up_match


def resolve_choice_input(
    *,
    field_name: str,
    raw_input: Optional[str],
    choices: List[Any],
    allow_skip: bool,
) -> ChoiceMatchResult:
    """Resolve natural language input to the closest structured choice."""
    normalized_choices = normalize_prompt_choices(list(choices or []))
    raw_text = str(raw_input or "").strip()
    normalized_raw = _normalize_match_text(raw_text)

    if not normalized_raw:
        return ChoiceMatchResult(status="skipped", raw_input=raw_text)
    if allow_skip and normalized_raw in SKIP_TOKENS:
        return ChoiceMatchResult(status="skipped", raw_input=raw_text)

    numeric_match = _match_numeric_choice(normalized_raw, normalized_choices)
    if numeric_match:
        return numeric_match

    exact_match = _match_exact_choice(normalized_raw, normalized_choices, raw_text)
    if exact_match:
        return exact_match

    alias_match = _match_choice_aliases(normalized_raw, normalized_choices, raw_text)
    if alias_match:
        return alias_match

    field_alias_value = _resolve_field_alias(field_name, normalized_raw)
    if field_alias_value:
        field_match = _find_choice_by_value(
            normalized_choices,
            field_alias_value,
            raw_text,
            confidence=0.98,
        )
        if field_match:
            return field_match

    prefix_matches = _prefix_or_substring_matches(normalized_raw, normalized_choices, raw_text)
    if len(prefix_matches) == 1:
        return ChoiceMatchResult(
            status="matched",
            value=prefix_matches[0]["value"],
            label=prefix_matches[0]["label"],
            confidence=prefix_matches[0]["score"],
            candidates=prefix_matches[:2],
            raw_input=raw_text,
        )
    if len(prefix_matches) > 1:
        return ChoiceMatchResult(
            status="ambiguous",
            confidence=prefix_matches[0]["score"],
            candidates=prefix_matches[:2],
            raw_input=raw_text,
        )

    fuzzy_matches = _fuzzy_matches(normalized_raw, normalized_choices, raw_text)
    if fuzzy_matches:
        top = fuzzy_matches[0]
        runner_up = fuzzy_matches[1] if len(fuzzy_matches) > 1 else None
        top_score = float(top.get("score", 0.0))
        runner_up_score = float(runner_up.get("score", 0.0)) if runner_up else 0.0
        if top_score >= 0.9 and top_score - runner_up_score >= 0.08:
            return ChoiceMatchResult(
                status="matched",
                value=top["value"],
                label=top["label"],
                confidence=top_score,
                candidates=fuzzy_matches[:2],
                raw_input=raw_text,
            )
        if top_score >= 0.72:
            return ChoiceMatchResult(
                status="ambiguous",
                confidence=top_score,
                candidates=fuzzy_matches[:2],
                raw_input=raw_text,
            )

    if field_name in CUSTOM_MEANING_FIELDS and raw_text:
        return ChoiceMatchResult(
            status="custom",
            value="other" if field_name == "use_case" else raw_text,
            label=raw_text,
            confidence=0.45,
            raw_input=raw_text,
        )

    return ChoiceMatchResult(status="unresolved", raw_input=raw_text)


def normalize_choice_value(
    raw_value: Any,
    *,
    field_name: str,
    choices: List[Any],
    default: Optional[str] = None,
) -> Optional[str]:
    """Normalize a direct context value against a choice list."""
    match = resolve_choice_input(
        field_name=field_name,
        raw_input=str(raw_value or "").strip(),
        choices=choices,
        allow_skip=True,
    )
    if match.status in {"matched", "confirmed", "custom"} and match.value:
        return str(match.value)
    return default


def ask_confirmation(
    console: Any,
    prompt: str,
    *,
    default: bool = False,
    title: Optional[str] = None,
    preview: Optional[str] = None,
    border_style: str = "cyan",
) -> bool:
    """Ask a friendly confirmation question without strict confirm prompts."""
    if preview and console and RICH_PANEL_AVAILABLE:
        console.print(Panel(preview, title=title or "Please Confirm", border_style=border_style))
    elif preview and console:
        console.print(preview)

    choices = [
        build_choice("Yes", "yes", aliases=["yeah", "yep", "sure", "continue", "proceed"]),
        build_choice("No", "no", aliases=["nope", "cancel", "stop", "not now"]),
    ]
    match = ask_flexible_choice(
        console,
        prompt=prompt,
        field_name="confirm",
        choices=choices,
        required=False,
        allow_skip=True,
        default="yes" if default else "no",
    )
    if match.value == "yes":
        return True
    if match.value == "no":
        return False
    return default


def print_dialog_status(
    console: Any,
    *,
    status: str,
    message: str,
    detail: Optional[str] = None,
) -> None:
    """Print a consistent dialog status message."""
    if not console:
        return
    icon_map = {
        "success": "[green]✓[/green]",
        "warning": "[yellow]⚠[/yellow]",
        "error": "[red]✗[/red]",
        "info": "[cyan]ℹ[/cyan]",
    }
    icon = icon_map.get(status, "[cyan]ℹ[/cyan]")
    console.print(f"{icon} {message}")
    if detail:
        console.print(f"[dim]{detail}[/dim]")
    console.print()


def _render_suggested_options(
    console: Any,
    choices: List[Dict[str, Any]],
    *,
    default: Optional[str] = None,
) -> None:
    if not console or not choices:
        return
    console.print("[dim]Suggested options:[/dim]")
    for index, choice in enumerate(choices, start=1):
        default_suffix = " (default)" if default and choice["value"] == default else ""
        console.print(f"[dim]  {index}. {choice['label']}{default_suffix}[/dim]")
    console.print(f"[dim]{CHOICE_HINT}[/dim]")


def _read_free_text(console: Any, prompt: str, *, default: Optional[str] = None) -> str:
    raw = ""
    if console:
        raw = console.input(f"{prompt.strip()} ")
    text = str(raw or "").strip()
    if text:
        return text
    return str(default or "").strip()


def _read_secret_text(console: Any, prompt: str) -> str:
    clean_prompt = prompt.strip()
    if console and RICH_PROMPT_AVAILABLE and Prompt is not None:
        raw = Prompt.ask(clean_prompt, console=console, password=True)
        return str(raw or "").strip()
    return str(getpass(f"{clean_prompt} ")).strip()


def _normalize_match_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    text = re.sub(r"[_/\\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _match_numeric_choice(
    normalized_raw: str,
    choices: List[Dict[str, Any]],
) -> Optional[ChoiceMatchResult]:
    if not normalized_raw.isdigit():
        return None
    index = int(normalized_raw) - 1
    if index < 0 or index >= len(choices):
        return None
    choice = choices[index]
    return ChoiceMatchResult(
        status="matched",
        value=choice["value"],
        label=choice["label"],
        confidence=1.0,
        raw_input=normalized_raw,
    )


def _match_exact_choice(
    normalized_raw: str,
    choices: List[Dict[str, Any]],
    raw_text: str,
) -> Optional[ChoiceMatchResult]:
    for choice in choices:
        if normalized_raw in {
            _normalize_match_text(choice["label"]),
            _normalize_match_text(choice["value"]),
        }:
            return ChoiceMatchResult(
                status="matched",
                value=choice["value"],
                label=choice["label"],
                confidence=1.0,
                raw_input=raw_text,
            )
    return None


def _match_choice_aliases(
    normalized_raw: str,
    choices: List[Dict[str, Any]],
    raw_text: str,
) -> Optional[ChoiceMatchResult]:
    matches = [
        choice
        for choice in choices
        if normalized_raw in set(choice.get("aliases") or [])
        or any(
            alias in normalized_raw or normalized_raw in alias
            for alias in choice.get("aliases") or []
        )
    ]
    if not matches:
        return None
    if len(matches) == 1:
        choice = matches[0]
        return ChoiceMatchResult(
            status="matched",
            value=choice["value"],
            label=choice["label"],
            confidence=0.99,
            raw_input=raw_text,
        )
    return ChoiceMatchResult(
        status="ambiguous",
        confidence=0.8,
        candidates=[
            {"label": item["label"], "value": item["value"], "score": 0.8} for item in matches[:2]
        ],
        raw_input=raw_text,
    )


def _resolve_field_alias(field_name: str, normalized_raw: str) -> Optional[str]:
    if field_name == "use_case":
        alias = normalize_use_case(normalized_raw)
        if alias:
            return alias
        if any(token in normalized_raw for token in ("cdc", "sync", "replication", "ingest")):
            return "etl_pipeline"
        if any(
            token in normalized_raw for token in ("stream processing", "events", "kafka", "pubsub")
        ):
            return "streaming"
        if any(
            token in normalized_raw
            for token in ("feature store", "feature engineering", "features")
        ):
            return "ml_pipeline"
        if any(token in normalized_raw for token in ("lakehouse", "bronze", "silver", "gold")):
            return "data_platform"
        if any(token in normalized_raw for token in ("report", "dashboard", "scorecard")):
            return "analytics"
        return None
    if field_name in {"provider", "provider_hint"}:
        return _match_alias_map(normalized_raw, PROVIDER_ALIASES) or normalize_provider_name(
            normalized_raw
        )
    if field_name == "build_engine":
        return _match_alias_map(normalized_raw, BUILD_ENGINE_ALIASES)
    if field_name == "output_kind":
        return _match_alias_map(normalized_raw, OUTPUT_KIND_ALIASES)
    if field_name == "complexity":
        return _match_alias_map(normalized_raw, COMPLEXITY_ALIASES)
    if field_name == "team_size":
        alias = _match_alias_map(normalized_raw, TEAM_SIZE_ALIASES)
        if alias:
            return alias
        sizes = [int(item) for item in re.findall(r"\d+", normalized_raw)]
        if sizes:
            team_size = max(sizes)
            if team_size <= 1:
                return "solo"
            if team_size <= 5:
                return "small (2-5)"
            if team_size <= 15:
                return "medium (6-15)"
            return "large (15+)"
    if field_name in YES_NO_FIELDS or field_name == "confirm":
        return _match_alias_map(normalized_raw, BOOLEAN_ALIASES)
    return None


def _match_alias_map(normalized_raw: str, aliases: Mapping[str, str]) -> Optional[str]:
    if normalized_raw in aliases:
        return aliases[normalized_raw]
    matching_aliases = [
        (alias, value)
        for alias, value in aliases.items()
        if alias in normalized_raw or normalized_raw in alias
    ]
    if not matching_aliases:
        return None
    matching_aliases.sort(key=lambda item: len(item[0]), reverse=True)
    return matching_aliases[0][1]


def _find_choice_by_value(
    choices: List[Dict[str, Any]],
    value: str,
    raw_text: str,
    *,
    confidence: float,
) -> Optional[ChoiceMatchResult]:
    normalized_value = _normalize_match_text(value)
    for choice in choices:
        if normalized_value in {
            _normalize_match_text(choice["label"]),
            _normalize_match_text(choice["value"]),
        }:
            return ChoiceMatchResult(
                status="matched",
                value=choice["value"],
                label=choice["label"],
                confidence=confidence,
                raw_input=raw_text,
            )
    return None


def _prefix_or_substring_matches(
    normalized_raw: str,
    choices: List[Dict[str, Any]],
    raw_text: str,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for choice in choices:
        label_key = _normalize_match_text(choice["label"])
        value_key = _normalize_match_text(choice["value"])
        if not normalized_raw:
            continue
        if label_key.startswith(normalized_raw) or value_key.startswith(normalized_raw):
            matches.append(
                {
                    "label": choice["label"],
                    "value": choice["value"],
                    "score": 0.92,
                    "raw_input": raw_text,
                }
            )
            continue
        if (
            normalized_raw in label_key
            or normalized_raw in value_key
            or label_key in normalized_raw
            or value_key in normalized_raw
        ):
            matches.append(
                {
                    "label": choice["label"],
                    "value": choice["value"],
                    "score": 0.84,
                    "raw_input": raw_text,
                }
            )
    matches.sort(key=lambda item: (-float(item["score"]), item["label"]))
    return matches


def _fuzzy_matches(
    normalized_raw: str,
    choices: List[Dict[str, Any]],
    raw_text: str,
) -> List[Dict[str, Any]]:
    raw_tokens = set(normalized_raw.split())
    scored: List[Dict[str, Any]] = []
    for choice in choices:
        label_key = _normalize_match_text(choice["label"])
        value_key = _normalize_match_text(choice["value"])
        label_tokens = set(label_key.split())
        value_tokens = set(value_key.split())
        token_overlap = 0.0
        if raw_tokens:
            token_overlap = max(
                len(raw_tokens & label_tokens) / max(len(raw_tokens), len(label_tokens) or 1),
                len(raw_tokens & value_tokens) / max(len(raw_tokens), len(value_tokens) or 1),
            )
        score = max(
            SequenceMatcher(None, normalized_raw, label_key).ratio(),
            SequenceMatcher(None, normalized_raw, value_key).ratio(),
            token_overlap,
        )
        scored.append(
            {
                "label": choice["label"],
                "value": choice["value"],
                "score": score,
                "raw_input": raw_text,
            }
        )
    scored.sort(key=lambda item: (-float(item["score"]), item["label"]))
    return scored[:3]
