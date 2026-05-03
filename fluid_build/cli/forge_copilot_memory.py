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

"""Project-scoped memory helpers for forge copilot."""

from __future__ import annotations

__all__ = [
    "CopilotMemorySnapshot",
    "CopilotProjectMemory",
    "CopilotMemoryStore",
    "build_copilot_project_memory",
    "resolve_copilot_memory_root",
    "summarize_copilot_memory",
]

import json
import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from fluid_build.cli._common import redact_secrets, resolve_provider_from_contract
from fluid_build.cli.artifact_paths import (
    PRODUCT_MEMORY_FILENAME,
    PRODUCT_STATE_DIRNAME,
    product_memory_path,
)
from fluid_build.util.contract import get_builds

LOG = logging.getLogger("fluid.cli.forge_copilot_memory")

#: Public alias for ``PRODUCT_MEMORY_FILENAME`` — kept under the
#: shorter name because every per-product call site here uses
#: ``MEMORY_FILENAME`` and the rename would touch ~30 read-only refs
#: in this module without changing semantics. Single source of truth
#: still lives in :mod:`fluid_build.cli.artifact_paths`.
MEMORY_FILENAME = PRODUCT_MEMORY_FILENAME
MEMORY_SCHEMA_VERSION = 1
MAX_MEMORY_LIST_VALUES = 8
MAX_MEMORY_SCHEMA_SUMMARIES = 8
MAX_MEMORY_COLUMNS = 12
MAX_MEMORY_RECENT_OUTCOMES = 5
MAX_MEMORY_OUTCOME_EXPOSES = 8
SAFE_PATH_PREFIX = "external"


@dataclass
class CopilotMemorySnapshot:
    """Bounded project memory safe to include in the copilot prompt."""

    saved_at: str
    preferred_template: Optional[str] = None
    preferred_provider: Optional[str] = None
    preferred_domain: Optional[str] = None
    preferred_owner: Optional[str] = None
    domains: List[str] = field(default_factory=list)
    owners: List[str] = field(default_factory=list)
    build_engines: List[str] = field(default_factory=list)
    binding_platforms: List[str] = field(default_factory=list)
    binding_formats: List[str] = field(default_factory=list)
    expose_kinds: List[str] = field(default_factory=list)
    provider_hints: List[str] = field(default_factory=list)
    source_formats: Dict[str, int] = field(default_factory=dict)
    schema_summaries: List[Dict[str, Any]] = field(default_factory=list)
    recent_outcomes: List[Dict[str, Any]] = field(default_factory=list)

    def to_prompt_payload(self) -> Dict[str, Any]:
        """Return a structured project-memory payload for the LLM."""
        return {
            "saved_at": self.saved_at,
            "preferred_template": self.preferred_template,
            "preferred_provider": self.preferred_provider,
            "preferred_domain": self.preferred_domain,
            "preferred_owner": self.preferred_owner,
            "domains": self.domains,
            "owners": self.owners,
            "build_engines": self.build_engines,
            "binding_platforms": self.binding_platforms,
            "binding_formats": self.binding_formats,
            "expose_kinds": self.expose_kinds,
            "provider_hints": self.provider_hints,
            "source_formats": self.source_formats,
            "schema_summaries": self.schema_summaries,
            "recent_outcomes": self.recent_outcomes,
        }


@dataclass
class CopilotProjectMemory:
    """Persisted project-scoped memory document."""

    schema_version: int
    saved_at: str
    project_profile: Dict[str, Any] = field(default_factory=dict)
    conventions: Dict[str, Any] = field(default_factory=dict)
    recent_outcomes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the memory document for storage."""
        return {
            "schema_version": self.schema_version,
            "saved_at": self.saved_at,
            "project_profile": self.project_profile,
            "conventions": self.conventions,
            "recent_outcomes": self.recent_outcomes,
        }

    def to_prompt_snapshot(self) -> CopilotMemorySnapshot:
        """Convert persisted memory into a bounded prompt snapshot."""
        profile = self.project_profile
        domains = _dedupe_strings(
            [profile.get("domain"), *[item.get("domain") for item in self.recent_outcomes]]
        )
        owners = _dedupe_strings(
            [profile.get("owner"), *[item.get("owner") for item in self.recent_outcomes]]
        )
        return CopilotMemorySnapshot(
            saved_at=self.saved_at,
            preferred_template=_clean_scalar(profile.get("template")),
            preferred_provider=_clean_scalar(profile.get("provider")),
            preferred_domain=_clean_scalar(profile.get("domain")),
            preferred_owner=_clean_scalar(profile.get("owner")),
            domains=domains[:MAX_MEMORY_LIST_VALUES],
            owners=owners[:MAX_MEMORY_LIST_VALUES],
            build_engines=_coerce_string_list(self.conventions.get("build_engines")),
            binding_platforms=_coerce_string_list(self.conventions.get("binding_platforms")),
            binding_formats=_coerce_string_list(self.conventions.get("binding_formats")),
            expose_kinds=_coerce_string_list(self.conventions.get("expose_kinds")),
            provider_hints=_coerce_string_list(self.conventions.get("provider_hints")),
            source_formats=_coerce_string_counter(self.conventions.get("source_formats")),
            schema_summaries=_coerce_schema_summaries(self.conventions.get("schema_summaries")),
            recent_outcomes=_coerce_recent_outcomes(self.recent_outcomes),
        )


class CopilotMemoryStore:
    """Load and save project-scoped copilot memory.

    The memory file lives at ``<product>/.fluid/copilot-memory.json``
    (gitignored per-engineer state — see the git-policy matrix in the
    artifact redesign plan).  Slice 6 cuts the path over from the old
    ``runtime/.state/`` location to the new ``.fluid/`` convention.
    No legacy fallback is read: any file still sitting at the old path
    is ignored and left untouched.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        logger: Optional[logging.Logger] = None,
        filename: str = MEMORY_FILENAME,
    ):
        self.project_root = project_root.resolve()
        self.logger = logger or LOG
        # The path is resolved via the artifact_paths helper so every
        # other module that needs to locate the file uses the same rule.
        if filename == MEMORY_FILENAME:
            self.path = product_memory_path(self.project_root)
        else:
            # Non-default filename (rare, used by some tests)
            self.path = self.project_root / PRODUCT_STATE_DIRNAME / filename

    def load(self) -> Optional[CopilotProjectMemory]:
        """Load memory from disk if it exists and is valid."""
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Ignoring invalid copilot memory file %s: %s", self.path, exc)
            return None

        try:
            return _coerce_memory_document(raw, project_root=self.project_root)
        except ValueError as exc:
            self.logger.warning("Ignoring invalid copilot memory file %s: %s", self.path, exc)
            return None

    def save(self, memory: CopilotProjectMemory) -> None:
        """Persist a validated memory document to disk.

        The written JSON carries a minimal envelope (``kind`` +
        ``generated_by``) alongside the existing domain-specific
        ``schema_version`` and ``saved_at`` fields.  Readers ignore
        unknown keys (see :func:`_coerce_memory_document`), so the
        addition is non-breaking for files written by older versions.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalized = _coerce_memory_document(memory.to_dict(), project_root=self.project_root)

        payload = normalized.to_dict()
        payload["kind"] = "ProjectMemory"
        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # pragma: no cover — defensive
            tool_version = ""
        payload["generated_by"] = {
            "tool": "fluid-cli",
            "version": str(tool_version),
            "command": "fluid forge",
        }

        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def delete(self) -> bool:
        """Delete the persisted memory file if it exists."""
        if not self.path.exists():
            return False
        self.path.unlink()
        return True

    def update_from_success(
        self,
        *,
        context: Mapping[str, Any],
        suggestions: Mapping[str, Any],
        contract: Mapping[str, Any],
        discovery_report: Any,
    ) -> CopilotProjectMemory:
        """Merge a successful copilot run into persisted project memory."""
        existing = self.load()
        memory = build_copilot_project_memory(
            project_root=self.project_root,
            context=context,
            suggestions=suggestions,
            contract=contract,
            discovery_report=discovery_report,
            existing_memory=existing,
        )
        self.save(memory)
        return memory


def resolve_copilot_memory_root(workspace_root: Path, target_dir: Optional[Path] = None) -> Path:
    """Choose the most relevant project root for loading or saving memory.

    Probes ``<dir>/.fluid/copilot-memory.json`` (the new slice-6 path)
    first at *target_dir*, then at *workspace_root*.  Falls back to
    *target_dir* (or *workspace_root* when target is ``None``) when
    neither has a memory file yet.

    No legacy ``runtime/.state/`` path is probed — slice 6 is a clean
    cut and old files at the pre-cut location are ignored.
    """
    workspace_root = workspace_root.resolve()
    if target_dir is not None:
        target_dir = target_dir.resolve()
        if product_memory_path(target_dir).exists():
            return target_dir
    if product_memory_path(workspace_root).exists():
        return workspace_root
    return target_dir or workspace_root


def build_copilot_project_memory(
    *,
    project_root: Path,
    context: Mapping[str, Any],
    suggestions: Mapping[str, Any],
    contract: Mapping[str, Any],
    discovery_report: Any,
    existing_memory: Optional[CopilotProjectMemory] = None,
) -> CopilotProjectMemory:
    """Build a bounded project-memory document from a successful copilot run."""
    now = _utc_now()
    contract_provider, _location = resolve_provider_from_contract(dict(contract))
    discovery_payload = _extract_discovery_payload(discovery_report)
    current_outcome = _summarize_outcome(
        saved_at=now,
        suggestions=suggestions,
        contract=contract,
        discovery_payload=discovery_payload,
        fallback_provider=contract_provider,
    )

    existing_profile = (existing_memory.project_profile if existing_memory else {}) or {}
    existing_conventions = (existing_memory.conventions if existing_memory else {}) or {}
    existing_outcomes = list(existing_memory.recent_outcomes if existing_memory else [])

    profile = {
        "template": _choose_scalar(
            suggestions.get("recommended_template"),
            contract.get("metadata", {}).get("template"),
            context.get("template"),
            existing_profile.get("template"),
        ),
        "provider": _choose_scalar(
            suggestions.get("recommended_provider"),
            contract_provider,
            context.get("provider"),
            existing_profile.get("provider"),
        ),
        "domain": _choose_scalar(
            suggestions.get("domain"),
            contract.get("domain"),
            context.get("domain"),
            existing_profile.get("domain"),
        ),
        "owner": _choose_scalar(
            suggestions.get("owner"),
            contract.get("metadata", {}).get("owner", {}).get("team"),
            context.get("owner"),
            existing_profile.get("owner"),
        ),
    }

    schema_summaries = _merge_schema_summaries(
        _extract_schema_summaries(discovery_payload, project_root),
        existing_conventions.get("schema_summaries"),
    )
    source_formats = _merge_counters(
        existing_conventions.get("source_formats"),
        _extract_source_formats(discovery_payload),
    )

    conventions = {
        "build_engines": _merge_string_lists(
            current_outcome.get("build_engines"),
            existing_conventions.get("build_engines"),
        ),
        "binding_platforms": _merge_string_lists(
            current_outcome.get("binding_platforms"),
            existing_conventions.get("binding_platforms"),
        ),
        "binding_formats": _merge_string_lists(
            current_outcome.get("binding_formats"),
            existing_conventions.get("binding_formats"),
        ),
        "expose_kinds": _merge_string_lists(
            current_outcome.get("expose_kinds"),
            existing_conventions.get("expose_kinds"),
        ),
        "provider_hints": _merge_string_lists(
            discovery_payload.get("provider_hints"),
            existing_conventions.get("provider_hints"),
        ),
        "source_formats": source_formats,
        "schema_summaries": schema_summaries,
    }

    recent_outcomes = _coerce_recent_outcomes([current_outcome, *existing_outcomes])[
        :MAX_MEMORY_RECENT_OUTCOMES
    ]

    return CopilotProjectMemory(
        schema_version=MEMORY_SCHEMA_VERSION,
        saved_at=now,
        project_profile=profile,
        conventions=conventions,
        recent_outcomes=recent_outcomes,
    )


def summarize_copilot_memory(memory: Any) -> Dict[str, Any]:
    """Return a compact human-facing summary of project memory."""
    if memory is None:
        return {}
    if hasattr(memory, "to_prompt_snapshot"):
        memory = memory.to_prompt_snapshot()

    if not isinstance(memory, CopilotMemorySnapshot):
        return {}

    return {
        "saved_at": memory.saved_at,
        "preferred_template": memory.preferred_template,
        "preferred_provider": memory.preferred_provider,
        "preferred_domain": memory.preferred_domain,
        "preferred_owner": memory.preferred_owner,
        "build_engines": list(memory.build_engines[:MAX_MEMORY_LIST_VALUES]),
        "binding_formats": list(memory.binding_formats[:MAX_MEMORY_LIST_VALUES]),
        "binding_platforms": list(memory.binding_platforms[:MAX_MEMORY_LIST_VALUES]),
        "provider_hints": list(memory.provider_hints[:MAX_MEMORY_LIST_VALUES]),
        "source_formats": dict(memory.source_formats),
        "schema_summary_count": len(memory.schema_summaries),
        "recent_outcome_count": len(memory.recent_outcomes),
    }


def _coerce_memory_document(raw: Any, *, project_root: Path) -> CopilotProjectMemory:
    if not isinstance(raw, Mapping):
        raise ValueError("memory must be a JSON object")

    schema_version = int(raw.get("schema_version") or 0)
    if schema_version > MEMORY_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version {schema_version}; expected {MEMORY_SCHEMA_VERSION}. "
            "Upgrade fluid-build to read this memory file."
        )
    if schema_version < MEMORY_SCHEMA_VERSION:
        # Future: add migration logic here (e.g. v0 -> v1 transforms).
        # For now, reject older versions that predate the schema contract.
        raise ValueError(
            f"outdated schema_version {schema_version}; expected {MEMORY_SCHEMA_VERSION}. "
            "Delete the memory file or re-run with --save-memory to regenerate."
        )

    profile = raw.get("project_profile")
    conventions = raw.get("conventions")
    recent_outcomes = raw.get("recent_outcomes")
    if not isinstance(profile, Mapping) or not isinstance(conventions, Mapping):
        raise ValueError("project_profile and conventions must be objects")

    return CopilotProjectMemory(
        schema_version=MEMORY_SCHEMA_VERSION,
        saved_at=_clean_scalar(raw.get("saved_at")) or _utc_now(),
        project_profile={
            "template": _clean_scalar(profile.get("template")),
            "provider": _clean_scalar(profile.get("provider")),
            "domain": _clean_scalar(profile.get("domain")),
            "owner": _clean_scalar(profile.get("owner")),
        },
        conventions={
            "build_engines": _coerce_string_list(conventions.get("build_engines")),
            "binding_platforms": _coerce_string_list(conventions.get("binding_platforms")),
            "binding_formats": _coerce_string_list(conventions.get("binding_formats")),
            "expose_kinds": _coerce_string_list(conventions.get("expose_kinds")),
            "provider_hints": _coerce_string_list(conventions.get("provider_hints")),
            "source_formats": _coerce_string_counter(conventions.get("source_formats")),
            "schema_summaries": _coerce_schema_summaries(
                conventions.get("schema_summaries"),
                project_root=project_root,
            ),
        },
        recent_outcomes=_coerce_recent_outcomes(recent_outcomes, project_root=project_root),
    )


def _extract_discovery_payload(discovery_report: Any) -> Dict[str, Any]:
    if discovery_report is None:
        return {}
    if hasattr(discovery_report, "to_prompt_payload"):
        payload = discovery_report.to_prompt_payload()
        if isinstance(payload, Mapping):
            return dict(payload)
    if isinstance(discovery_report, Mapping):
        return dict(discovery_report)

    payload: Dict[str, Any] = {}
    for key in (
        "workspace_roots",
        "sample_files",
        "provider_hints",
    ):
        value = getattr(discovery_report, key, None)
        if value is not None:
            payload[key] = value
    return payload


def _extract_schema_summaries(
    discovery_payload: Mapping[str, Any],
    project_root: Path,
) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    for sample in discovery_payload.get("sample_files") or []:
        if not isinstance(sample, Mapping):
            continue
        path_label = _sanitize_path(sample.get("path"), project_root)
        summary = {
            "path": path_label,
            "format": _clean_scalar(sample.get("format")),
            "columns": _truncate_columns(sample.get("columns")),
        }
        if sample.get("row_count") is not None:
            summary["row_count"] = _coerce_int(sample.get("row_count"))
        if sample.get("schema_source"):
            summary["schema_source"] = _clean_scalar(sample.get("schema_source"))
        summaries.append(summary)
    return _coerce_schema_summaries(summaries, project_root=project_root)


def _extract_source_formats(discovery_payload: Mapping[str, Any]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for sample in discovery_payload.get("sample_files") or []:
        if not isinstance(sample, Mapping):
            continue
        fmt = _clean_scalar(sample.get("format"))
        if fmt:
            counts.update([fmt])
    return dict(counts)


def _summarize_outcome(
    *,
    saved_at: str,
    suggestions: Mapping[str, Any],
    contract: Mapping[str, Any],
    discovery_payload: Mapping[str, Any],
    fallback_provider: Optional[str],
) -> Dict[str, Any]:
    builds = list(get_builds(dict(contract)))
    exposes = list(contract.get("exposes") or [])
    outcome = {
        "saved_at": saved_at,
        "template": _choose_scalar(
            suggestions.get("recommended_template"),
            contract.get("metadata", {}).get("template"),
        ),
        "provider": _choose_scalar(
            suggestions.get("recommended_provider"),
            fallback_provider,
        ),
        "domain": _choose_scalar(
            suggestions.get("domain"),
            contract.get("domain"),
        ),
        "owner": _choose_scalar(
            suggestions.get("owner"),
            contract.get("metadata", {}).get("owner", {}).get("team"),
        ),
        "build_engines": _dedupe_strings(
            [
                build.get("engine") or (build.get("transformation") or {}).get("engine")
                for build in builds
            ]
        )[:MAX_MEMORY_LIST_VALUES],
        "binding_platforms": _dedupe_strings(
            [(expose.get("binding") or {}).get("platform") for expose in exposes]
            + [
                ((build.get("execution") or {}).get("runtime") or {}).get("platform")
                for build in builds
            ]
        )[:MAX_MEMORY_LIST_VALUES],
        "binding_formats": _dedupe_strings(
            [(expose.get("binding") or {}).get("format") for expose in exposes]
        )[:MAX_MEMORY_LIST_VALUES],
        "expose_kinds": _dedupe_strings([expose.get("kind") for expose in exposes])[
            :MAX_MEMORY_LIST_VALUES
        ],
        "expose_ids": _dedupe_strings([expose.get("exposeId") for expose in exposes])[
            :MAX_MEMORY_OUTCOME_EXPOSES
        ],
        "quality_dimensions": _dedupe_strings(
            [rule.get("dimension") for rule in (contract.get("quality") or [])]
        )[:MAX_MEMORY_LIST_VALUES],
        "source_formats": _extract_source_formats(discovery_payload),
    }
    return outcome


def _merge_schema_summaries(
    current: Any,
    existing: Any,
) -> List[Dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    merged: List[Dict[str, Any]] = []
    for item in list(current or []) + list(existing or []):
        if not isinstance(item, Mapping):
            continue
        path_value = _clean_scalar(item.get("path")) or "unknown"
        format_value = _clean_scalar(item.get("format")) or "unknown"
        key = (path_value, format_value)
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "path": path_value,
                "format": format_value,
                "columns": _truncate_columns(item.get("columns")),
                **(
                    {"row_count": _coerce_int(item.get("row_count"))}
                    if item.get("row_count") is not None
                    else {}
                ),
                **(
                    {"schema_source": _clean_scalar(item.get("schema_source"))}
                    if item.get("schema_source")
                    else {}
                ),
            }
        )
        if len(merged) >= MAX_MEMORY_SCHEMA_SUMMARIES:
            break
    return merged


def _merge_counters(existing: Any, current: Any) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for key, value in _coerce_string_counter(existing).items():
        counter[key] += value
    for key, value in _coerce_string_counter(current).items():
        counter[key] += value
    ordered = counter.most_common(MAX_MEMORY_LIST_VALUES)
    return {key: value for key, value in ordered}


def _merge_string_lists(current: Any, existing: Any) -> List[str]:
    return _dedupe_strings([*(_coerce_string_list(current)), *(_coerce_string_list(existing))])[
        :MAX_MEMORY_LIST_VALUES
    ]


def _coerce_recent_outcomes(
    value: Any,
    *,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    outcomes: List[Dict[str, Any]] = []
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return outcomes
    for item in value:
        if not isinstance(item, Mapping):
            continue
        outcome = {
            "saved_at": _clean_scalar(item.get("saved_at")) or _utc_now(),
            "template": _clean_scalar(item.get("template")),
            "provider": _clean_scalar(item.get("provider")),
            "domain": _clean_scalar(item.get("domain")),
            "owner": _clean_scalar(item.get("owner")),
            "build_engines": _coerce_string_list(item.get("build_engines")),
            "binding_platforms": _coerce_string_list(item.get("binding_platforms")),
            "binding_formats": _coerce_string_list(item.get("binding_formats")),
            "expose_kinds": _coerce_string_list(item.get("expose_kinds")),
            "expose_ids": _coerce_string_list(
                item.get("expose_ids"), limit=MAX_MEMORY_OUTCOME_EXPOSES
            ),
            "quality_dimensions": _coerce_string_list(item.get("quality_dimensions")),
            "source_formats": _coerce_string_counter(item.get("source_formats")),
        }
        if project_root is not None and item.get("path"):
            outcome["path"] = _sanitize_path(item.get("path"), project_root)
        outcomes.append(outcome)
        if len(outcomes) >= MAX_MEMORY_RECENT_OUTCOMES:
            break
    return outcomes


def _coerce_schema_summaries(
    value: Any,
    *,
    project_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return summaries
    for item in value:
        if not isinstance(item, Mapping):
            continue
        raw_path = item.get("path")
        path_value = (
            _sanitize_path(raw_path, project_root)
            if project_root is not None
            else _clean_scalar(raw_path)
        ) or "unknown"
        summary = {
            "path": path_value,
            "format": _clean_scalar(item.get("format")) or "unknown",
            "columns": _truncate_columns(item.get("columns")),
        }
        if item.get("row_count") is not None:
            summary["row_count"] = _coerce_int(item.get("row_count"))
        if item.get("schema_source"):
            summary["schema_source"] = _clean_scalar(item.get("schema_source"))
        summaries.append(summary)
        if len(summaries) >= MAX_MEMORY_SCHEMA_SUMMARIES:
            break
    return summaries


def _coerce_string_counter(value: Any) -> Dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    sanitized: Dict[str, int] = {}
    for raw_key, raw_value in value.items():
        key = _clean_scalar(raw_key)
        number = _coerce_int(raw_value)
        if not key or number <= 0 or key.startswith("airbyte_"):
            continue
        sanitized[key] = number
        if len(sanitized) >= MAX_MEMORY_LIST_VALUES:
            break
    return sanitized


def _coerce_string_list(value: Any, *, limit: int = MAX_MEMORY_LIST_VALUES) -> List[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    return _dedupe_strings(value)[:limit]


def _dedupe_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        cleaned = _clean_scalar(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def _truncate_columns(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    truncated: Dict[str, str] = {}
    for raw_name, raw_type in value.items():
        name = _clean_scalar(raw_name)
        inferred_type = _clean_scalar(raw_type)
        if not name or not inferred_type:
            continue
        truncated[name] = inferred_type
        if len(truncated) >= MAX_MEMORY_COLUMNS:
            break
    return truncated


def _sanitize_path(value: Any, project_root: Optional[Path]) -> Optional[str]:
    if value is None:
        return None
    raw_value = str(value)
    try:
        path = Path(raw_value).expanduser()
    except TypeError:
        return None

    # On Windows, Unix-style rooted paths (e.g. /Users/foo/file.avro) may not be
    # considered absolute by pathlib. Treat them as absolute-like to avoid leaking
    # host path structure in prompts.
    looks_absolute_like = raw_value.startswith("/") or raw_value.startswith("\\")

    if not path.is_absolute() and not looks_absolute_like:
        normalized = path.as_posix().lstrip("./")
        if not normalized or normalized.startswith(".."):
            return path.name or None
        return normalized

    if project_root is not None:
        try:
            relative = path.resolve().relative_to(project_root.resolve())
            return relative.as_posix()
        except Exception:  # noqa: BLE001
            name = path.name
            return f"{SAFE_PATH_PREFIX}/{name}" if name else None

    name = path.name
    if not name:
        return None
    return f"{SAFE_PATH_PREFIX}/{name}"


def _choose_scalar(*values: Any) -> Optional[str]:
    for value in values:
        cleaned = _clean_scalar(value)
        if cleaned:
            return cleaned
    return None


def _clean_scalar(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = redact_secrets(text)
    return text[:200]


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
