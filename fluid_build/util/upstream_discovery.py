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

"""Discover and index upstream FLUID contracts across one or more workspaces.

This module is shared between the dbt sources generator (which uses upstream
bindings to emit real source identifiers) and the ``fluid forge`` LLM
pipeline (which uses upstream expose schemas to guide transformation SQL).

Search strategy
---------------
1. Caller supplies an anchor ``workspace_root`` — typically the directory
   where the active contract will be written.
2. Additional roots can be added via the ``FLUID_UPSTREAM_CONTRACTS``
   environment variable (colon-separated paths).  This is how operators
   point at upstream Bronze repositories that live in a different
   working directory from the current Silver product.
3. Each root is walked up to ``max_depth`` levels deep.  Directories
   named ``.git``, ``.venv``, ``node_modules``, ``target``, etc. are
   skipped outright.

Tolerant-by-design
------------------
All filesystem and YAML errors are swallowed as debug logs: a broken
contract in the workspace must never crash generation of a different
product.  Callers receive whatever was successfully indexed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import AbstractSet, Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

import yaml

from fluid_build.util.safe_yaml import MAX_YAML_BYTES, UnsafeYamlError, load_yaml_safe

__all__ = [
    "BUILD_OUTPUT_DIRS",
    "IGNORED_DIRS",
    "CONTRACT_FILENAMES",
    "collect_search_roots",
    "discover_upstream_products",
    "index_contract_paths",
    "project_upstream_for_prompt",
]

_logger = logging.getLogger(__name__)


#: Directories we never descend into while walking workspace roots.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".venv.fluid-dev",
        ".venv.fluid-demo",
        "node_modules",
        "target",
        "logs",
        "dbt_packages",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "generated",
    }
)

#: What a walk made for a BUILD also skips: directories forge-cli, a CI job or
#: an installed forge-cli write into. A copy of a contract left in one of them
#: (a venv's ``site-packages/fluid_build/templates``, a ``dist/`` artifact
#: tree) would otherwise be a second contract declaring the same id, and a
#: build must refuse an id declared twice rather than pick one.
BUILD_OUTPUT_DIRS = frozenset(
    {
        "dist",
        "build",
        "out",
        "runtime",
        ".fluid",
        ".fluid-ci",
        ".fluid-venv",
        ".fluid-workspace",
        "venv",
        "site-packages",
        ".ruff_cache",
        ".hg",
        ".svn",
    }
)

#: Canonical contract filenames.  ``.yaml`` is today's format; ``.json``
#: is the legacy variant still carried for backward compatibility.
CONTRACT_FILENAMES = ("contract.fluid.yaml", "contract.fluid.json")


def collect_search_roots(
    workspace_root: Optional[Path],
    *,
    extra_paths: Optional[Iterable[Path]] = None,
    env_var: str = "FLUID_UPSTREAM_CONTRACTS",
) -> List[Path]:
    """Return deduplicated, resolved directories to search for upstream contracts.

    Resolution order:
    1. ``workspace_root`` (if supplied and is a directory).
    2. Each path from ``extra_paths`` (if supplied).
    3. Each colon-separated path in ``os.environ[env_var]``.

    Non-existent or non-directory paths are silently skipped — callers
    shouldn't crash because an operator set a stale env var.
    """
    seen: set = set()
    roots: List[Path] = []

    def _add(p: Optional[Path]) -> None:
        if p is None:
            return
        try:
            resolved = p.expanduser().resolve()
        except (OSError, RuntimeError):
            return
        if resolved in seen or not resolved.is_dir():
            return
        seen.add(resolved)
        roots.append(resolved)

    _add(workspace_root)

    if extra_paths:
        for extra in extra_paths:
            _add(Path(extra) if not isinstance(extra, Path) else extra)

    env_paths = os.environ.get(env_var, "").strip()
    if env_paths:
        for raw in env_paths.split(":"):
            raw = raw.strip()
            if raw:
                _add(Path(raw))

    return roots


def _iter_contracts(
    root: Path,
    max_depth: int = 4,
    _depth: int = 0,
    *,
    ignored: AbstractSet[str] = IGNORED_DIRS,
) -> Iterator[Path]:
    """Yield ``contract.fluid.yaml`` paths under *root*, depth-limited."""
    try:
        entries = sorted(root.iterdir())
    except (PermissionError, OSError):
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_file() and entry.name in CONTRACT_FILENAMES:
            yield entry
        elif entry.is_dir() and entry.name not in ignored:
            if max_depth == -1 or _depth < max_depth:
                yield from _iter_contracts(entry, max_depth, _depth + 1, ignored=ignored)


def _read_discovered_contract(contract_path: Path) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parse one DISCOVERED contract file: ``(document, "")`` or ``(None, reason)``.

    SECURITY (billion-laughs / oversized-YAML DoS): these contracts are
    DISCOVERED by walking the workspace (+ pulled mesh repos via
    FLUID_UPSTREAM_CONTRACTS) — the user did NOT explicitly name them, so a
    hostile upstream contract must not be able to OOM generation of a
    different product.

    stat-before-read: ``load_yaml_safe``'s byte cap only fires AFTER the whole
    file is in memory, so a multi-GB file would already have OOM'd us before
    the cap ran. Stat the file FIRST and skip oversized ones — mirrors the
    stat-before-read in ``forge/federation.py::_read_first_existing_contract``
    and reuses the same :data:`MAX_YAML_BYTES` ceiling. ``load_yaml_safe``
    remains the parse path (defence in depth: alias-bomb + cap).
    """
    try:
        size = contract_path.stat().st_size
        if size > MAX_YAML_BYTES:
            return None, f"{size} bytes > {MAX_YAML_BYTES} cap"
        data = load_yaml_safe(contract_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnsafeYamlError) as exc:
        return None, str(exc)
    if not isinstance(data, Mapping):
        return None, "not a mapping"
    return dict(data), ""


def discover_upstream_products(
    workspace_root: Optional[Path],
    *,
    extra_paths: Optional[Iterable[Path]] = None,
    max_depth: int = 4,
) -> Dict[str, Dict[str, Any]]:
    """Walk *workspace_root* (+ env roots) and return ``{productId: contract}``.

    Duplicate productIds resolve last-write-wins — rare in practice, but
    the walk order is deterministic thanks to ``sorted(root.iterdir())``.
    """
    roots = collect_search_roots(workspace_root, extra_paths=extra_paths)
    if not roots:
        return {}

    index: Dict[str, Dict[str, Any]] = {}
    for root in roots:
        for contract_path in _iter_contracts(root, max_depth=max_depth):
            data, reason = _read_discovered_contract(contract_path)
            if data is None:
                _logger.debug("upstream: skipping %s (%s)", contract_path, reason)
                continue
            contract_id = data.get("id")
            if not contract_id or not isinstance(contract_id, str):
                continue
            index[contract_id] = data
    return index


def index_contract_paths(
    roots: Iterable[Path],
    *,
    max_depth: int = 4,
    ignored: AbstractSet[str] = IGNORED_DIRS | BUILD_OUTPUT_DIRS,
) -> Tuple[Dict[str, List[Path]], List[Tuple[Path, str]]]:
    """Every contract file under *roots*, keyed by the ``id`` it declares.

    The same walk and the same guarded read as :func:`discover_upstream_products`,
    for a caller that must not guess: a build resolving ``consumes[].productId``
    to the contract that owns it. Two differences follow from that:

    * an id maps to EVERY file declaring it (a file reached from two roots is
      listed once), so the caller can refuse an id found twice instead of
      taking whichever the walk met last;
    * the files that could not be read are returned as ``(path, reason)``, so
      "no contract declares this id" can say which files it could not look in.

    ``ignored`` defaults to :data:`IGNORED_DIRS` plus :data:`BUILD_OUTPUT_DIRS`.
    """
    index: Dict[str, List[Path]] = {}
    skipped: List[Tuple[Path, str]] = []
    seen: set = set()
    for root in roots:
        for contract_path in _iter_contracts(root, max_depth=max_depth, ignored=ignored):
            try:
                key = contract_path.resolve()
            except (OSError, RuntimeError):
                key = contract_path
            if key in seen:
                continue
            seen.add(key)
            data, reason = _read_discovered_contract(contract_path)
            if data is None:
                skipped.append((contract_path, reason))
                continue
            contract_id = data.get("id")
            if not contract_id or not isinstance(contract_id, str):
                continue
            index.setdefault(contract_id, []).append(contract_path)
    return index, skipped


def project_upstream_for_prompt(
    contracts: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Reduce a full-contract index to just what the forge LLM prompt needs.

    The LLM is interested in:
    * what products exist and what they declare they're for;
    * what exposes each product publishes and what shape those exposes
      have (columns, types, binding location).

    Everything else (builds, executions, sovereignty blocks, dq rules,
    provenance envelopes) is noise that bloats the prompt without
    changing the generated SQL.
    """
    projection: Dict[str, Dict[str, Any]] = {}
    for product_id, contract in contracts.items():
        if not isinstance(contract, Mapping):
            continue
        exposes = contract.get("exposes") or []
        if not isinstance(exposes, list):
            exposes = []

        exposed: Dict[str, Dict[str, Any]] = {}
        for expose in exposes:
            if not isinstance(expose, Mapping):
                continue
            expose_id = expose.get("exposeId") or expose.get("id")
            if not expose_id or not isinstance(expose_id, str):
                continue
            entry: Dict[str, Any] = {}
            if expose.get("kind"):
                entry["kind"] = expose["kind"]
            if expose.get("title"):
                entry["title"] = expose["title"]
            if expose.get("description"):
                entry["description"] = expose["description"]
            binding = expose.get("binding") or {}
            if isinstance(binding, Mapping):
                location = binding.get("location") or {}
                if isinstance(location, Mapping):
                    compact_loc = {
                        k: v
                        for k, v in location.items()
                        if k in {"database", "schema", "dataset", "table", "object"}
                    }
                    if compact_loc:
                        entry["location"] = compact_loc
                if binding.get("platform"):
                    entry["platform"] = binding["platform"]
            contract_section = expose.get("contract") or {}
            if isinstance(contract_section, Mapping):
                schema = contract_section.get("schema")
                if isinstance(schema, list) and schema:
                    compact_schema = []
                    for col in schema:
                        if not isinstance(col, Mapping):
                            continue
                        name = col.get("name")
                        if not name:
                            continue
                        compact_schema.append(
                            {
                                "name": name,
                                "type": col.get("type", "string"),
                                "required": bool(col.get("required", False)),
                            }
                        )
                    if compact_schema:
                        entry["schema"] = compact_schema
            exposed[expose_id] = entry

        if exposed:
            projection[product_id] = {
                "name": contract.get("name"),
                "description": contract.get("description"),
                "domain": contract.get("domain"),
                "exposes": exposed,
            }
    return projection
