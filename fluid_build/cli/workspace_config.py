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

"""Lightweight workspace configuration loader for fluid.workspace.yaml.

A FLUID workspace is a directory containing one or more data product
contracts.  The optional ``fluid.workspace.yaml`` file stores shared
team defaults (domain, owner, provider) so ``fluid init`` and
``fluid forge`` can pre-fill new product metadata without re-asking.

This module is intentionally small and has no heavy dependencies.
The richer ``workspace.py`` module provides enterprise collaboration
features (SQLite, git, team RBAC) for users who need them.
"""

from __future__ import annotations

__all__ = [
    "WORKSPACE_FILENAME",
    "WorkspaceDefaults",
    "discover_workspace_products",
    "find_workspace_root",
    "load_workspace_config",
    "save_workspace_config",
]

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.cli.artifact_paths import (
    CONTRACT_FILENAME,
    WORKSPACE_CONFIG_FILENAME,
    WORKSPACE_STATE_DIRNAME,
)

LOG = logging.getLogger("fluid.cli.workspace_config")

#: Canonical workspace-config filename. Defined in
#: :mod:`fluid_build.cli.artifact_paths` (single source of truth) and
#: re-exported here as the public symbol every workspace consumer
#: imports — see ``cli/init.py``, ``cli/forge.py``, etc.
WORKSPACE_FILENAME = WORKSPACE_CONFIG_FILENAME
DEFAULT_PRODUCTS_DIR = "."

# Directories to skip when scanning for contracts.
_IGNORED_DIRS = {
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


@dataclass
class WorkspaceDefaults:
    """Shared defaults read from ``fluid.workspace.yaml``."""

    name: str = ""
    domain: str = ""
    owner_team: str = ""
    owner_email: str = ""
    provider: str = ""
    industry: str = ""
    products_dir: str = DEFAULT_PRODUCTS_DIR
    # Phase 0.2 — workspace specialization. When set, future ``fluid forge``
    # runs default to this productType and reject conflicting
    # ``--data-product-type`` flags. Use ``fluid init --workspace-lock SDP|
    # ADP|CDP`` to opt in.
    data_product_type_lock: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.name


@dataclass
class DiscoveredProduct:
    """Summary of a data product found in the workspace."""

    name: str
    path: Path
    contract_path: Path
    expose_count: int = 0
    provider: str = ""
    fluid_version: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_workspace_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from *start* looking for ``fluid.workspace.yaml``.

    Returns the directory containing the file, or ``None``.
    """
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / WORKSPACE_FILENAME).is_file():
            return parent
    return None


def load_workspace_config(root: Optional[Path] = None) -> WorkspaceDefaults:
    """Load ``fluid.workspace.yaml`` from *root* (or search upward).

    Returns an empty :class:`WorkspaceDefaults` if the file is not found
    or cannot be parsed.
    """
    if root is None:
        root = find_workspace_root()
    if root is None:
        return WorkspaceDefaults()

    ws_path = root / WORKSPACE_FILENAME
    if not ws_path.is_file():
        return WorkspaceDefaults()

    try:
        raw = yaml.safe_load(ws_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        LOG.warning("Could not parse %s", ws_path)
        return WorkspaceDefaults()

    ws = raw.get("workspace") or raw  # top-level key is optional
    owner = ws.get("owner") or {}
    if isinstance(owner, str):
        owner = {"team": owner}

    return WorkspaceDefaults(
        name=str(ws.get("name") or ""),
        domain=str(ws.get("domain") or ""),
        owner_team=str(owner.get("team") or ""),
        owner_email=str(owner.get("email") or ""),
        provider=str(ws.get("provider") or ""),
        products_dir=str(ws.get("products_dir") or DEFAULT_PRODUCTS_DIR),
        data_product_type_lock=str(ws.get("data_product_type_lock") or ""),
        raw=raw,
    )


def save_workspace_config(
    root: Path,
    *,
    name: str,
    domain: str = "",
    owner_team: str = "",
    owner_email: str = "",
    provider: str = "",
    products_dir: str = DEFAULT_PRODUCTS_DIR,
    data_product_type_lock: str = "",
    command: str = "fluid init",
) -> Path:
    """Write a ``fluid.workspace.yaml`` file to *root*.

    The written file carries an envelope (``schema_version``/``kind``/
    ``generated_at``/``generated_by``) at the top level in addition to
    the ``workspace:`` block.  :func:`load_workspace_config` tolerates
    both shapes — old files without the envelope continue to load
    unchanged, and new files parse via the same ``raw.get('workspace')``
    path because envelope keys sit alongside ``workspace:``, not inside
    it.

    Returns the path of the written file.
    """
    # Import inside the function so this module stays free of circular
    # dependency risk with artifact_envelope (which itself imports from
    # artifact_paths — a sibling of this module).
    from fluid_build.cli.artifact_envelope import dump_yaml_with_envelope

    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    ws: Dict[str, Any] = {"name": name}
    if domain:
        ws["domain"] = domain
    owner: Dict[str, str] = {}
    if owner_team:
        owner["team"] = owner_team
    if owner_email:
        owner["email"] = owner_email
    if owner:
        ws["owner"] = owner
    if provider:
        ws["provider"] = provider
    if products_dir != DEFAULT_PRODUCTS_DIR:
        ws["products_dir"] = products_dir
    if data_product_type_lock:
        ws["data_product_type_lock"] = data_product_type_lock

    payload = {"workspace": ws}
    body = dump_yaml_with_envelope(
        payload,
        kind="WorkspaceConfig",
        command=command,
        tool_version=str(tool_version),
    )

    ws_path = root / WORKSPACE_FILENAME
    root.mkdir(parents=True, exist_ok=True)
    ws_path.write_text(body, encoding="utf-8")
    return ws_path


def discover_workspace_products(root: Path) -> List[DiscoveredProduct]:
    """Find ``contract.fluid.yaml`` files within the workspace boundary.

    Reads ``products_dir`` from the workspace config and searches only
    within that directory (depth-limited to 2 levels).

    Returns a list of :class:`DiscoveredProduct` sorted by name.
    """
    ws_config = load_workspace_config(root)
    search_base = (root / ws_config.products_dir).resolve()
    # Guard: ensure search_base is within root.
    try:
        search_base.relative_to(root.resolve())
    except ValueError:
        search_base = root

    products: List[DiscoveredProduct] = []
    for contract_path in _iter_contracts(search_base):
        product_dir = contract_path.parent
        product_name = product_dir.name if product_dir != search_base else "(root)"
        expose_count = 0
        provider = ""
        fluid_version = ""
        try:
            contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
            exposes = contract.get("exposes") or []
            expose_count = len(exposes)
            fluid_version = str(contract.get("fluidVersion") or "")
            # Infer provider from first expose binding
            for expose in exposes:
                binding = expose.get("binding") or {}
                platform = binding.get("platform")
                if platform:
                    provider = str(platform)
                    break
        except Exception:  # noqa: BLE001
            pass
        products.append(
            DiscoveredProduct(
                name=product_name,
                path=product_dir,
                contract_path=contract_path,
                expose_count=expose_count,
                provider=provider,
                fluid_version=fluid_version,
            )
        )
    products.sort(key=lambda p: p.name)
    return products


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _iter_contracts(root: Path, max_depth: int = 2, _depth: int = 0):
    """Yield ``contract.fluid.yaml`` paths under *root*, with depth limiting.

    *max_depth* controls how deep the search goes (default 2 — covers
    ``workspace/product-name/contract.fluid.yaml``).  Use ``-1`` for
    unlimited depth.
    """
    # Accept the canonical filename from the registry plus the legacy .json variant.
    contract_filenames = (CONTRACT_FILENAME, "contract.fluid.json")
    try:
        for entry in sorted(root.iterdir()):
            if entry.is_symlink():
                continue  # Prevent symlink-based traversal outside the workspace.
            if entry.is_file() and entry.name in contract_filenames:
                yield entry
            elif entry.is_dir() and entry.name not in _IGNORED_DIRS:
                if max_depth == -1 or _depth < max_depth:
                    yield from _iter_contracts(entry, max_depth, _depth + 1)
    except PermissionError:
        pass
