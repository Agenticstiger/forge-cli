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

"""Workspace confinement helpers for build runners.

Both the python and dbt runners take a user-controlled ``build['repository']``
field from the contract YAML and resolve a path under a workspace root. A
malicious contract author could set ``repository: ../../etc`` and trick the
runner into executing a script outside the workspace, or pointing ``dbt build``
at a project in a sensitive location. ``confine_to_workspace`` rejects any
resolved path that is not a descendant of the workspace root.

The workspace root depends on the contract's build pattern:

- **Inline** (default): ``contract.parent`` — the directory containing
  the contract YAML. Tightest possible boundary; appropriate when the
  build's assets (dbt project, python script) live next to the contract.
- **hybrid-reference**: walks up to the nearest enclosing ``.git`` /
  workspace marker. The Data Mesh "hybrid reference" pattern intentionally
  shares assets across sibling variants (one or two levels up the tree),
  so the inline boundary is too tight. Walking up to the repo root keeps
  the security boundary at the operator's blast radius (anything inside
  this git repo is co-owned with the contract).

The ``cli/security.py`` validator handles the same threat class for
copilot tools; this helper is the build-runner counterpart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

# Repo-root marker files / dirs (in priority order). The first one found
# walking up from the contract becomes the workspace root for shared-asset
# patterns. ``.git`` is the universal one; the rest catch monorepos and
# non-git workspaces (mercurial, sapling, jj).
_REPO_ROOT_MARKERS: tuple = (
    ".git",
    ".hg",
    ".sl",
    ".jj",
    "WORKSPACE",
    "WORKSPACE.bazel",
    "pnpm-workspace.yaml",
)

# Build patterns that reference assets outside the contract's directory.
# Operators authoring these patterns are explicitly opting into a wider
# workspace; the ``confine_to_workspace`` boundary widens to the repo
# root accordingly.
_SHARED_ASSET_PATTERNS: frozenset = frozenset(
    {
        "hybrid-reference",
        "shared-reference",
    }
)


def _find_repo_root(start: Path, *, outermost: bool = False) -> Optional[Path]:
    """Walk up from ``start`` looking for a repo-root marker.

    ``outermost=False`` (default): returns the FIRST (innermost) ancestor
    containing a marker — the standard "where am I in this repo" question.

    ``outermost=True``: walks all the way to the filesystem root and
    returns the OUTERMOST ancestor with a marker. Catches the
    monorepo-with-nested-repos topology where an outer ``snowflake-biz-lab/.git``
    holds multiple inner workspace repos (e.g. ``gitlab/path-a/.git``,
    ``gitlab/path-b/.git``) and a cross-workspace hybrid-reference build
    needs the outer boundary, not the inner one.
    """
    current = start.resolve()
    found: Optional[Path] = None
    for ancestor in [current, *current.parents]:
        for marker in _REPO_ROOT_MARKERS:
            if (ancestor / marker).exists():
                found = ancestor
                if not outermost:
                    return found
                break  # marker found at this ancestor; keep walking for outer
    return found


def resolve_workspace_root(contract_path: Path, build: Mapping[str, Any]) -> Path:
    """Return the workspace root for ``confine_to_workspace`` given a build.

    Default = ``contract_path.parent`` (tightest boundary). For builds whose
    ``pattern`` is in :data:`_SHARED_ASSET_PATTERNS` (hybrid-reference et al),
    walk up to the OUTERMOST enclosing repo-root marker so cross-repo
    references like
    ``repository: ../../../../path-a-telco-silver-product-demo/reference-assets/dbt_xyz``
    resolve when the lab uses a monorepo-with-nested-repos topology. The
    outermost boundary is still bounded by the operator's checkout — they
    can't escape the parent ``.git`` — but it allows cross-workspace
    references inside the same operator-authored monorepo.

    Falls back to ``contract.parent`` when no repo root is found (e.g. the
    contract isn't in a git repo at all).
    """
    pattern = (build or {}).get("pattern")
    if pattern not in _SHARED_ASSET_PATTERNS:
        return contract_path.parent
    repo_root = _find_repo_root(contract_path.parent, outermost=True)
    return repo_root if repo_root is not None else contract_path.parent


def confine_to_workspace(
    candidate: Path,
    workspace_root: Path,
    *,
    build_id: str,
    kind: str,
    logger=None,
) -> Optional[Path]:
    """Return ``candidate`` if its resolved form is contained in ``workspace_root``.

    The check uses ``Path.resolve()``-d forms of both arguments so symlinks
    that escape the workspace are caught. Returns ``None`` (and emits a
    logger warning) when the path is outside; callers treat ``None`` the
    same as "not found", which is the existing failure-mode contract for
    the build-runner resolvers.
    """
    try:
        resolved_candidate = candidate.resolve()
        resolved_root = workspace_root.resolve()
        is_within = resolved_candidate.is_relative_to(resolved_root)
    except (ValueError, OSError):
        is_within = False

    if not is_within:
        if logger is not None:
            logger.warning(
                "build_runner_path_outside_workspace build_id=%r kind=%r "
                "path=%r workspace_root=%r",
                build_id,
                kind,
                str(candidate),
                str(workspace_root),
            )
        return None

    return candidate
