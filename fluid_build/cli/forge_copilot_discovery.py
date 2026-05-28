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

"""Local workspace discovery for the forge copilot."""

from __future__ import annotations

__all__ = [
    "DiscoveryReport",
    "discover_local_context",
    "parse_ddl_files",
]

import json
import logging
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import yaml

from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError
from fluid_build.cli.forge_copilot_schema_inference import (
    extract_provider_hints,
    summarize_sample_file,
)
from fluid_build.config import RUN_STATE_DIR
from fluid_build.util.contract import get_builds

LOG = logging.getLogger("fluid.cli.forge_copilot.discovery")

MAX_DISCOVERY_FILES = 300
MAX_SQL_FILES = 25
MAX_READMES = 10
MAX_SAMPLE_FILES = 6  # Slice UX-G: lowered from 12 — copilot only uses a handful
MAX_EXISTING_CONTRACTS = 12
MAX_README_LINES = 80
#: Hard cap on how deep the BFS walks below each discovery root.  Slice
#: UX-G added this to bound the "AI mode is extremely slow" latency on
#: deeply-nested repos.  Previously the walk was effectively unbounded —
#: it would only stop when ``MAX_DISCOVERY_FILES`` was hit, which means
#: a 10-deep tree with 50 dirs/level could easily stat hundreds of
#: thousands of nodes before yielding its first sample.
MAX_DISCOVERY_DEPTH = 6
DISCOVERABLE_SAMPLE_SUFFIXES = {".csv", ".json", ".jsonl", ".parquet", ".pq", ".avro"}
RUN_STATE_PATH_PARTS = tuple(Path(RUN_STATE_DIR).parts)

IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    # The CLI's own hidden state dir must be ignored during discovery so
    # files like copilot-memory.json / init-receipt.json / ci-state.json
    # (which slice 6 introduced) never leak into sample_files or
    # provider_hints and muddle the copilot prompt.
    ".fluid",
    ".fluid-workspace",
}


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryReport:
    """Metadata-only view of locally discovered assets."""

    workspace_roots: List[str]
    files_scanned: int = 0
    detected_sources: List[Dict[str, Any]] = field(default_factory=list)
    sql_files: List[Dict[str, Any]] = field(default_factory=list)
    dbt_projects: List[Dict[str, Any]] = field(default_factory=list)
    terraform_projects: List[Dict[str, Any]] = field(default_factory=list)
    readmes: List[Dict[str, Any]] = field(default_factory=list)
    existing_contracts: List[Dict[str, Any]] = field(default_factory=list)
    sample_files: List[Dict[str, Any]] = field(default_factory=list)
    provider_hints: List[str] = field(default_factory=list)
    build_constraints: List[str] = field(default_factory=list)
    discovery_warnings: List[str] = field(default_factory=list)
    # Authoring layout: "flat" or "fragment-first" (auto-detected).
    authoring_mode: str = "flat"
    # True when no sample data files (CSV, Parquet, etc.) were found.
    sample_data_missing: bool = False
    # User-supplied data model files discovered in models/ folder.
    user_data_models: List[Dict[str, Any]] = field(default_factory=list)
    # Slice UX-L: surfaced in the performance summary panel.
    cache_hit: bool = False
    scan_time_ms: int = 0

    def to_prompt_payload(self) -> Dict[str, Any]:
        """Return a metadata-only payload safe to share with the LLM."""
        home = str(Path.home())
        sanitized_roots = [
            root.replace(home, "~") if isinstance(root, str) else root
            for root in self.workspace_roots
        ]
        return {
            "workspace_roots": sanitized_roots,
            "files_scanned": self.files_scanned,
            "detected_sources": self.detected_sources,
            "sql_files": self.sql_files,
            "dbt_projects": self.dbt_projects,
            "terraform_projects": self.terraform_projects,
            "readmes": self.readmes,
            "existing_contracts": self.existing_contracts,
            "sample_files": self.sample_files,
            "user_data_models": self.user_data_models,
            "provider_hints": self.provider_hints,
            "build_constraints": self.build_constraints,
            "discovery_warnings": self.discovery_warnings,
            "authoring_mode": self.authoring_mode,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def discover_local_context(
    discovery_path: Optional[str],
    *,
    discover: bool = True,
    workspace_root: Optional[Path] = None,
    logger: Optional[logging.Logger] = None,
) -> DiscoveryReport:
    """Scan local workspace files and return a metadata-only discovery report."""
    root = (workspace_root or Path.cwd()).resolve()
    roots = [root]
    if discovery_path:
        extra = Path(discovery_path).expanduser().resolve()
        if not extra.exists():
            raise CopilotGenerationError(
                "copilot_discovery_path_missing",
                f"Discovery path does not exist: {extra}",
                suggestions=["Check the path passed to --discovery-path"],
            )
        if extra not in roots:
            roots.append(extra)

    report = DiscoveryReport(
        workspace_roots=[str(path) for path in roots],
        build_constraints=[
            "Discovery payload must exclude raw sample rows, full file contents, and credentials.",
            "Use only providers and templates supported by the local Forge registries.",
            "Prefer placeholder env vars for destination configuration instead of hard-coded secrets.",
        ],
    )

    if not discover:
        report.build_constraints.append("Discovery was disabled by the user.")
        return report

    # Slice UX-J: check the on-disk discovery cache before doing the
    # expensive classification + schema-inference pass.  The BFS walk
    # still runs (to compute the cache key) but stat() is ~100x
    # cheaper than opening + parsing files for schema inference.
    try:
        from fluid_build.cli.artifact_discovery_cache import (
            compute_file_tree_hash,
            discovery_cache_enabled,
            load_discovery_cache,
            write_discovery_cache,
        )

        _cache_available = True
    except ImportError:
        _cache_available = False

    if _cache_available and discovery_cache_enabled():
        all_candidates: List[Path] = []
        for scan_root in roots:
            for p in _iter_candidate_files(scan_root):
                if not _is_excluded_discovery_artifact(p):
                    all_candidates.append(p)
        tree_hash = compute_file_tree_hash(all_candidates)

        cached_report = load_discovery_cache(root, tree_hash)
        if cached_report is not None:
            try:
                rpt = DiscoveryReport(**cached_report)
                rpt.cache_hit = True
                return rpt
            except Exception:  # noqa: BLE001 — fallback to full scan
                pass
    else:
        tree_hash = None
        all_candidates = None

    import time as _time

    _scan_start = _time.monotonic()

    seen_files: set[Path] = set()
    provider_counts: Counter[str] = Counter()
    detected_sources: List[Dict[str, Any]] = []

    for scan_root in roots:
        for path in _iter_candidate_files(scan_root):
            if path in seen_files:
                continue
            if _is_excluded_discovery_artifact(path):
                continue
            seen_files.add(path)
            report.files_scanned += 1
            suffix = path.suffix.lower()

            if path.name == "dbt_project.yml":
                project = _summarize_dbt_project(path)
                report.dbt_projects.append(project)
                provider_counts.update(project.get("provider_hints") or [])
                continue

            if suffix == ".tf":
                terraform = _summarize_terraform_file(path)
                report.terraform_projects.append(terraform)
                provider_counts.update(terraform.get("provider_hints") or [])
                continue

            if path.name.lower().startswith("readme") and len(report.readmes) < MAX_READMES:
                report.readmes.append(_summarize_readme(path))
                continue

            if path.name.endswith("contract.fluid.yaml") or path.name.endswith(
                "contract.fluid.json"
            ):
                if len(report.existing_contracts) < MAX_EXISTING_CONTRACTS:
                    summary = _summarize_existing_contract(path)
                    report.existing_contracts.append(summary)
                    provider_counts.update(summary.get("providers") or [])
                continue

            # Phase 7 — sniff ODCS / Bitol ODPS docs by filename hint
            # (*.odcs.yaml, *.odps.yaml) so the interview step can offer
            # them as ``--seed-from`` candidates. Adds an entry to
            # ``detected_sources`` rather than ``existing_contracts``
            # because these are *standards-format* sources, not FLUID.
            _name_lower = path.name.lower()
            if _name_lower.endswith(
                (".odcs.yaml", ".odcs.yml", ".odcs.json")
            ) or _name_lower.endswith((".odps.yaml", ".odps.yml", ".odps.json")):
                kind = "odcs" if ".odcs" in _name_lower else "odps"
                detected_sources.append(
                    {
                        "path": str(path.relative_to(scan_root)),
                        "kind": f"standard-{kind}",
                        "suggested_use": "fluid forge --seed-from",
                    }
                )
                continue

            if suffix == ".sql" and len(report.sql_files) < MAX_SQL_FILES:
                report.sql_files.append(_summarize_sql_file(path))
                if "models" in {part.lower() for part in path.parts}:
                    try:
                        from .forge_copilot_schema_inference import summarize_user_data_model

                        model_summary = summarize_user_data_model(path)
                        if model_summary:
                            report.user_data_models.append(model_summary)
                    except Exception as exc:  # noqa: BLE001
                        report.discovery_warnings.append(
                            f"Could not inspect data model {path.name}: {exc}"
                        )
                continue

            if suffix in {".yaml", ".yml", ".json"} and "models" in {
                part.lower() for part in path.parts
            }:
                try:
                    from .forge_copilot_schema_inference import summarize_user_data_model

                    model_summary = summarize_user_data_model(path)
                    if model_summary:
                        report.user_data_models.append(model_summary)
                except Exception as exc:  # noqa: BLE001
                    report.discovery_warnings.append(
                        f"Could not inspect data model {path.name}: {exc}"
                    )
                continue

            if (
                suffix in DISCOVERABLE_SAMPLE_SUFFIXES
                and len(report.sample_files) < MAX_SAMPLE_FILES
            ):
                try:
                    sample = summarize_sample_file(path)
                except Exception as exc:  # noqa: BLE001
                    report.discovery_warnings.append(
                        f"Could not inspect sample file {path.name}: {exc}"
                    )
                    continue
                report.sample_files.append(sample)
                detected_sources.append(sample)
                provider_counts.update(sample.get("provider_hints") or [])
                report.discovery_warnings.extend(sample.get("warnings") or [])

            if report.files_scanned >= MAX_DISCOVERY_FILES:
                break
        if report.files_scanned >= MAX_DISCOVERY_FILES:
            break

    report.detected_sources = sorted(
        detected_sources[:MAX_SAMPLE_FILES],
        key=lambda s: s.get("path", ""),
    )
    # Sort by count descending, then alphabetically for deterministic
    # tie-breaking across runs against the same workspace.
    report.provider_hints = sorted(
        [name for name, _ in provider_counts.most_common()],
        key=lambda x: (-provider_counts[x], x),
    )
    report.sql_files.sort(key=lambda s: s.get("path", ""))
    report.sample_files.sort(key=lambda s: s.get("path", ""))
    report.sample_data_missing = len(report.sample_files) == 0

    if report.sql_files:
        report.build_constraints.append(
            "Existing SQL assets were found; reuse discovered source table names and output naming conventions where possible."
        )
    if any(sample.get("format") == "parquet" for sample in report.sample_files):
        report.build_constraints.append(
            "Parquet files were discovered; prefer discovered column names, logical types, and storage conventions instead of inventing schemas."
        )
    if any(sample.get("format") == "avro" for sample in report.sample_files):
        report.build_constraints.append(
            "Avro files were discovered; preserve the discovered record shape and union/logical-type intent when generating exposes and builds."
        )
    if report.existing_contracts:
        report.build_constraints.append(
            "Existing FLUID contracts were found; stay consistent with discovered contract naming and provider conventions."
        )

    # Detect fragment-first authoring layout.
    for scan_root in roots:
        fragments_dir = scan_root / "fragments"
        try:
            if fragments_dir.is_dir() and any(fragments_dir.rglob("*.yaml")):
                report.authoring_mode = "fragment-first"
                break
        except Exception:  # noqa: BLE001 — detection is best-effort
            pass

    if logger:
        logger.debug(
            "copilot_discovery_complete",
            extra={
                "files_scanned": report.files_scanned,
                "provider_hints": report.provider_hints,
            },
        )

    report.scan_time_ms = int((_time.monotonic() - _scan_start) * 1000)

    # Slice UX-J: persist the discovery report to disk so the next run
    # can skip classification + schema inference if the file tree
    # hasn't changed.  Best-effort — write failures are logged, never
    # raised.
    if _cache_available and discovery_cache_enabled() and tree_hash:
        try:
            write_discovery_cache(root, report, tree_hash)
        except Exception:  # noqa: BLE001
            pass

    return report


# ---------------------------------------------------------------------------
# Rescan helpers (for forge interview early-scaffold flow)
# ---------------------------------------------------------------------------


def rescan_sample_data(
    target_dir: Path,
    report: DiscoveryReport,
    *,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Re-scan *target_dir*/samples/ and *target_dir*/models/ after the user
    drops files during the forge interview.  Mutates *report* in place.
    """
    from .forge_copilot_schema_inference import summarize_sample_file

    # Scan samples/ folder for data files
    samples_dir = target_dir / "samples"
    if samples_dir.is_dir():
        for path in sorted(samples_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in DISCOVERABLE_SAMPLE_SUFFIXES:
                continue
            if len(report.sample_files) >= MAX_SAMPLE_FILES:
                break
            # Skip already-discovered files
            known_paths = {s.get("path") for s in report.sample_files}
            if str(path) in known_paths:
                continue
            try:
                sample = summarize_sample_file(path)
                report.sample_files.append(sample)
                report.detected_sources.append(sample)
            except Exception as exc:  # noqa: BLE001
                if logger:
                    logger.debug("rescan_sample_failed: %s", exc)

    # Re-sort after enrichment for deterministic ordering.
    report.sample_files.sort(key=lambda s: s.get("path", ""))
    report.detected_sources.sort(key=lambda s: s.get("path", ""))
    report.sample_data_missing = len(report.sample_files) == 0

    # Scan models/ folder for user-supplied data models
    models_dir = target_dir / "models"
    if models_dir.is_dir():
        for path in sorted(models_dir.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() in (".sql", ".yaml", ".yml", ".json"):
                try:
                    from .forge_copilot_schema_inference import summarize_user_data_model

                    model_summary = summarize_user_data_model(path)
                    if model_summary:
                        report.user_data_models.append(model_summary)
                except Exception as exc:  # noqa: BLE001
                    if logger:
                        logger.debug("rescan_data_model_failed: %s", exc)


def parse_ddl_files(
    paths: Iterable[Path], *, dialect: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Parse DDL files into compact summaries for the staged data-model path."""
    from fluid_build.forge_datamodel.from_ddl.parser import parse_ddl_text

    results: List[Dict[str, Any]] = []
    for path in paths:
        try:
            ddl_text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        parsed = parse_ddl_text(ddl_text, dialect=dialect)
        if not parsed.tables:
            continue
        results.append(
            {
                "path": str(path),
                "tables": len(parsed.tables),
                "columns": {
                    table.name: {column.name: column.logical_type for column in table.columns}
                    for table in parsed.tables
                },
            }
        )
    return results


# ---------------------------------------------------------------------------
# File traversal
# ---------------------------------------------------------------------------


def _iter_candidate_files(root: Path) -> Iterable[Path]:
    """BFS walk under *root* yielding candidate files for discovery.

    Slice UX-G optimizations (see MAX_DISCOVERY_DEPTH docstring):

    1. Each queue entry carries its depth-below-root so we can skip
       directories deeper than ``MAX_DISCOVERY_DEPTH``.  Without this,
       the walk was effectively unbounded — it only stopped on
       ``MAX_DISCOVERY_FILES``, which for a deeply-nested monorepo
       still meant thousands of ``stat`` calls before the first yield.

    2. As soon as ``yielded >= MAX_DISCOVERY_FILES`` the inner loop
       breaks instead of appending more subdirectories to the queue.
       The previous version kept walking directories to append them
       even though their contents would be ignored.
    """
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return

    queue: deque[tuple[Path, int]] = deque([(root, 0)])
    yielded = 0
    while queue and yielded < MAX_DISCOVERY_FILES:
        current, depth = queue.popleft()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name in IGNORED_DIRECTORIES:
                continue
            if entry.is_dir():
                if depth < MAX_DISCOVERY_DEPTH:
                    queue.append((entry, depth + 1))
                continue
            yielded += 1
            yield entry
            if yielded >= MAX_DISCOVERY_FILES:
                return


def _is_run_state_artifact(path: Path) -> bool:
    parts = path.parts
    if len(parts) < len(RUN_STATE_PATH_PARTS):
        return False
    for index in range(len(parts) - len(RUN_STATE_PATH_PARTS) + 1):
        if tuple(parts[index : index + len(RUN_STATE_PATH_PARTS)]) == RUN_STATE_PATH_PARTS:
            return True
    return False


def _is_excluded_discovery_artifact(path: Path) -> bool:
    if _is_run_state_artifact(path):
        return True
    return any(part.lower() == "airbyte" for part in path.parts)


# ---------------------------------------------------------------------------
# File summarizers
# ---------------------------------------------------------------------------


def _summarize_dbt_project(path: Path) -> Dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {"path": str(path), "warnings": [f"Could not parse dbt project file: {path.name}"]}
    profile = data.get("profile")
    return {
        "path": str(path),
        "name": data.get("name"),
        "profile": profile,
        "model_paths": data.get("model-paths") or [],
        "provider_hints": extract_provider_hints(" ".join([str(profile), str(data)])),
    }


def _summarize_terraform_file(path: Path) -> Dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    resource_matches = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', content)
    return {
        "path": str(path),
        "resources": [
            {"type": resource_type, "name": name} for resource_type, name in resource_matches[:15]
        ],
        "provider_hints": extract_provider_hints(content),
    }


def _summarize_readme(path: Path) -> Dict[str, Any]:
    headings: List[str] = []
    words = 0
    for index, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
        if index >= MAX_README_LINES:
            break
        if line.strip().startswith("#"):
            headings.append(line.lstrip("#").strip())
        words += len(line.split())
    return {"path": str(path), "headings": headings[:12], "word_count": words}


def _summarize_existing_contract(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            contract = json.loads(raw)
        else:
            contract = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001
        return {"path": str(path), "warnings": [f"Could not parse contract file: {path.name}"]}
    if not isinstance(contract, dict):
        return {
            "path": str(path),
            "warnings": [f"Contract file is not a valid mapping: {path.name}"],
        }
    providers = []
    for expose in contract.get("exposes") or []:
        binding = expose.get("binding") or {}
        if binding.get("platform"):
            providers.append(binding["platform"])
    return {
        "path": str(path),
        "fluid_version": contract.get("fluidVersion"),
        "kind": contract.get("kind"),
        "id": contract.get("id"),
        "name": contract.get("name"),
        "providers": sorted(set(providers)),
        "build_ids": [build.get("id") for build in get_builds(contract)[:10] if build.get("id")],
        "expose_ids": [
            expose.get("exposeId")
            for expose in (contract.get("exposes") or [])[:10]
            if expose.get("exposeId")
        ],
    }


def _summarize_sql_file(path: Path) -> Dict[str, Any]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    table_refs = re.findall(
        r"\b(?:from|join|into|update|table)\s+([A-Za-z0-9_.`\"]+)",
        content,
        flags=re.IGNORECASE,
    )
    return {
        "path": str(path),
        "line_count": len(content.splitlines()),
        "referenced_tables": table_refs[:15],
    }
