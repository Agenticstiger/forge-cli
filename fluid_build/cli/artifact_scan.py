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

"""Workspace artifact scanner — what files exist, at what sha256.

``fluid init`` does not thread :class:`ArtifactWriter` through every
scaffold helper (legacy template-copy code paths are out of scope for
the current slice track).  To produce a receipt without touching those
handlers, slice 2 takes a before/after snapshot of the workspace and
diffs them.

This module implements the snapshot.  Tracked paths are exactly the
files the CLI is expected to write at slice 2's point in the plan:

* ``<workspace>/fluid.workspace.yaml``
* ``<workspace>/.fluid/skills.yaml``
* ``<workspace>/<product>/contract.fluid.yaml`` for every product
  directory one level deep inside the workspace (plus the workspace
  root itself, for the legacy "flat" case).

Future slices that properly thread the :class:`ArtifactWriter` will
replace the scan with direct receipt recording at each write site.  The
scanner's API is intentionally small so it can be deleted in one commit
when that happens.
"""

from __future__ import annotations

__all__ = [
    "ArtifactSnapshot",
    "snapshot_workspace",
    "diff_snapshots",
]

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List

from fluid_build.cli.artifact_paths import (
    CONTRACT_FILENAME,
    WORKSPACE_CONFIG_FILENAME,
    WORKSPACE_SKILLS_FILENAME,
    WORKSPACE_STATE_DIRNAME,
)
from fluid_build.cli.artifact_receipts import ReceiptEntry


@dataclass
class ArtifactSnapshot:
    """Map of ``path -> sha256`` for every tracked file under a root."""

    root: Path
    files: Dict[Path, str] = field(default_factory=dict)

    def __contains__(self, path: Path) -> bool:
        return Path(path) in self.files

    def sha256_for(self, path: Path) -> str | None:
        return self.files.get(Path(path))


def snapshot_workspace(root: Path, *, max_product_depth: int = 2) -> ArtifactSnapshot:
    """Walk *root* and record the sha256 of every tracked artifact.

    Tracked files:

    * ``<root>/fluid.workspace.yaml`` — workspace config
    * ``<root>/.gitignore`` — gitignore template written by ``fluid init``
    * ``<root>/.fluid/skills.yaml``
    * ``<root>/contract.fluid.yaml`` and ``<root>/contract.fluid.json``
      (legacy flat layout; both formats are supported by the loader)
    * ``<root>/<subdir>/contract.fluid.{yaml,json}`` down to
      ``max_product_depth`` levels deep (so a new product scaffolded by
      ``fluid init`` is caught regardless of whether the handler placed
      it at the root or in a named subdirectory, and regardless of the
      serialisation format the scaffolder chose).

    Non-existent files are silently omitted.  Unreadable files (permission
    errors) are also omitted — the scan is best-effort and never raises.
    """
    root = Path(root).resolve()
    snapshot = ArtifactSnapshot(root=root)

    _maybe_record(snapshot, root / WORKSPACE_CONFIG_FILENAME)
    _maybe_record(snapshot, root / ".gitignore")
    _maybe_record(
        snapshot,
        root / WORKSPACE_STATE_DIRNAME / WORKSPACE_SKILLS_FILENAME,
    )
    _maybe_record(snapshot, root / CONTRACT_FILENAME)
    _maybe_record(snapshot, root / "contract.fluid.json")

    for contract_path in _iter_contracts(root, max_depth=max_product_depth):
        _maybe_record(snapshot, contract_path)

    return snapshot


def diff_snapshots(
    before: ArtifactSnapshot,
    after: ArtifactSnapshot,
) -> List[ReceiptEntry]:
    """Diff two snapshots and return one :class:`ReceiptEntry` per change.

    Rules:

    * Present in ``after`` but not ``before`` → ``action: "create"``.
    * Present in both with the same sha → ``action: "unchanged"``.
    * Present in both with a different sha → ``action: "update"``.
    * Present in ``before`` but not ``after`` → ignored (init/forge do
      not delete files as part of normal operation; rollback is handled
      elsewhere).
    """
    entries: List[ReceiptEntry] = []
    scope_root = after.root

    for path, sha_after in after.files.items():
        try:
            rel = str(Path(path).relative_to(scope_root))
        except ValueError:
            rel = str(path)

        if path not in before.files:
            size = _file_size(path)
            entries.append(
                ReceiptEntry(
                    path=rel,
                    action="create",
                    sha256=sha_after,
                    size=size,
                )
            )
            continue

        sha_before = before.files[path]
        if sha_before == sha_after:
            size = _file_size(path)
            entries.append(
                ReceiptEntry(
                    path=rel,
                    action="unchanged",
                    sha256=sha_after,
                    size=size,
                )
            )
        else:
            size = _file_size(path)
            entries.append(
                ReceiptEntry(
                    path=rel,
                    action="update",
                    sha256=sha_after,
                    size=size,
                )
            )

    return entries


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_IGNORED_DIR_NAMES = {
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
    WORKSPACE_STATE_DIRNAME,
    ".fluid-workspace",
}


def _maybe_record(snapshot: ArtifactSnapshot, path: Path) -> None:
    if not path.is_file():
        return
    try:
        data = path.read_bytes()
    except OSError:
        return
    snapshot.files[path.resolve()] = hashlib.sha256(data).hexdigest()


_CONTRACT_FILENAMES = frozenset({CONTRACT_FILENAME, "contract.fluid.json"})


def _iter_contracts(root: Path, *, max_depth: int) -> Iterator[Path]:
    """Yield every ``contract.fluid.{yaml,json}`` file under *root*.

    Walks only up to *max_depth* levels deep (the workspace root is
    depth 0).  Ignores virtualenvs, git metadata, and the hidden
    ``.fluid/`` directory.  Both YAML and JSON contract forms are
    collected because the blank-init path historically writes JSON
    while forge writes YAML.
    """
    def walk(current: Path, depth: int) -> Iterator[Path]:
        if depth > max_depth:
            return
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_file():
                if entry.name in _CONTRACT_FILENAMES:
                    yield entry
            elif entry.is_dir() and entry.name not in _IGNORED_DIR_NAMES:
                yield from walk(entry, depth + 1)

    yield from walk(root, depth=0)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
