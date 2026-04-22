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

"""Central registry of every file path the CLI writes during init/forge.

Before this module, path constants were scattered across five modules
(``workspace_config.WORKSPACE_FILENAME``, ``forge_copilot_memory.MEMORY_FILENAME``,
``forge_copilot_personal_memory._MEMORY_FILE``, ``config.RUN_STATE_DIR``, plus
``"contract.fluid.yaml"`` hardcoded in ``forge_contract_factory``).  The result
was impossible to audit in one place and any future move became a multi-file
hunt.

This module is the one place to change a filename, a directory name, or a
scope-level layout decision.  Every other module in the CLI should import
from here rather than defining its own constant.

Intentionally dependency-free (stdlib only) so it can be imported from any
module without circular imports.
"""

from __future__ import annotations

__all__ = [
    # Envelope version
    "ENVELOPE_SCHEMA_VERSION",
    # User scope
    "USER_FLUID_DIRNAME",
    "USER_CONFIG_FILENAME",
    "USER_PERSONAL_MEMORY_FILENAME",
    "USER_LOGS_SUBDIR",
    "user_fluid_dir",
    "user_config_path",
    "user_personal_memory_path",
    # Workspace scope
    "WORKSPACE_CONFIG_FILENAME",
    "WORKSPACE_STATE_DIRNAME",
    "WORKSPACE_SKILLS_FILENAME",
    "WORKSPACE_INIT_RECEIPT_FILENAME",
    "workspace_state_dir",
    "workspace_skills_path",
    "workspace_init_receipt_path",
    # Product scope
    "CONTRACT_FILENAME",
    "CONTRACT_BUNDLED_FILENAME",
    "CONTRACT_LOCK_FILENAME",
    "PRODUCT_FRAGMENTS_DIRNAME",
    "PRODUCT_OVERLAYS_DIRNAME",
    "PRODUCT_STATE_DIRNAME",
    "PRODUCT_MEMORY_FILENAME",
    "PRODUCT_CI_STATE_FILENAME",
    "PRODUCT_FORGE_RECEIPT_FILENAME",
    "product_state_dir",
    "product_contract_path",
    "product_bundled_path",
    "product_lock_path",
    "product_fragments_dir",
    "product_overlays_dir",
    "product_memory_path",
    "product_ci_state_path",
    "product_forge_receipt_path",
    # CI header schema identifier
    "CI_HEADER_SCHEMA",
]

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

#: Schema version carried in the envelope on every artifact this module writes.
#: Bumped when the envelope shape itself changes (not the payload).
ENVELOPE_SCHEMA_VERSION: int = 1


# ---------------------------------------------------------------------------
# User scope — ~/.fluid/
# ---------------------------------------------------------------------------

USER_FLUID_DIRNAME: str = ".fluid"
USER_CONFIG_FILENAME: str = "config.yaml"
USER_PERSONAL_MEMORY_FILENAME: str = "personal-memory.json"
USER_LOGS_SUBDIR: str = "logs"


def user_fluid_dir() -> Path:
    """Return ``~/.fluid`` — the user-scope state directory.

    Honors ``FLUID_HOME`` for test overrides, falling back to
    ``Path.home() / ".fluid"``.
    """
    override = os.environ.get("FLUID_HOME")
    if override:
        return Path(override)
    return Path.home() / USER_FLUID_DIRNAME


def user_config_path() -> Path:
    """Absolute path to ``~/.fluid/config.yaml``."""
    return user_fluid_dir() / USER_CONFIG_FILENAME


def user_personal_memory_path() -> Path:
    """Absolute path to ``~/.fluid/personal-memory.json`` (v1 namespaced layout)."""
    return user_fluid_dir() / USER_PERSONAL_MEMORY_FILENAME


# ---------------------------------------------------------------------------
# Workspace scope — <workspace>/
# ---------------------------------------------------------------------------

#: The visible, committed workspace config file at the workspace root.
#: Kept as-is for backward compatibility with every repo in the wild.
WORKSPACE_CONFIG_FILENAME: str = "fluid.workspace.yaml"

#: The hidden state directory at the workspace root.  Same name as the
#: user-level directory by design (``.fluid``), but always referenced through
#: its full path in logs to avoid ambiguity.
WORKSPACE_STATE_DIRNAME: str = ".fluid"

#: Industry skills pack inside ``<workspace>/.fluid/``.  Committed for team
#: reference (the only file under ``.fluid/`` that stays committed at the
#: workspace level).
WORKSPACE_SKILLS_FILENAME: str = "skills.yaml"

#: Per-run init manifest (gitignored, machine-local).
WORKSPACE_INIT_RECEIPT_FILENAME: str = "init-receipt.json"


def workspace_state_dir(workspace_root: Path) -> Path:
    """Return ``<workspace_root>/.fluid`` without creating it."""
    return Path(workspace_root) / WORKSPACE_STATE_DIRNAME


def workspace_skills_path(workspace_root: Path) -> Path:
    """Return ``<workspace_root>/.fluid/skills.yaml`` without creating it."""
    return workspace_state_dir(workspace_root) / WORKSPACE_SKILLS_FILENAME


#: Slice UX-J: compact pre-compiled prompt payload derived from skills.yaml.
WORKSPACE_SKILLS_COMPILED_FILENAME: str = "skills.compiled.json"


def workspace_skills_compiled_path(workspace_root: Path) -> Path:
    """Return ``<workspace_root>/.fluid/skills.compiled.json``."""
    return workspace_state_dir(workspace_root) / WORKSPACE_SKILLS_COMPILED_FILENAME


def workspace_init_receipt_path(workspace_root: Path) -> Path:
    """Return ``<workspace_root>/.fluid/init-receipt.json`` without creating it."""
    return workspace_state_dir(workspace_root) / WORKSPACE_INIT_RECEIPT_FILENAME


# ---------------------------------------------------------------------------
# Product scope — <workspace>/<product>/
# ---------------------------------------------------------------------------

#: The contract file users author, and the marker that identifies a product
#: directory.  Unchanged from the pre-refactor codebase.
CONTRACT_FILENAME: str = "contract.fluid.yaml"

#: The flat, committed, DO-NOT-EDIT bundled contract produced by
#: ``fluid bundle``.  Lives next to the contract at the product root.
CONTRACT_BUNDLED_FILENAME: str = "contract.bundled.yaml"

#: The committed integrity lockfile produced by ``fluid bundle --write-lock``.
#: Carries fragment paths + sha256 + fluidVersion.  Treated like
#: ``package-lock.json`` / ``Cargo.lock`` — small diff, checked in CI.
CONTRACT_LOCK_FILENAME: str = "contract.lock.yaml"

#: Directory that holds ownership-based contract fragments in fragment-first
#: authoring mode.  Optional; only exists when the product was scaffolded
#: with ``--fragments``.
PRODUCT_FRAGMENTS_DIRNAME: str = "fragments"

#: Directory that holds environment-only deltas (``dev.yaml``, ``prod.yaml``).
#: Only contains env-specific changes; base config is forbidden.
PRODUCT_OVERLAYS_DIRNAME: str = "overlays"

#: The per-product hidden state directory — mirrors the workspace convention.
#: Most entries are gitignored; ``ci-state.json`` is the committed exception
#: because it describes committed CI files.
PRODUCT_STATE_DIRNAME: str = ".fluid"

#: Gitignored per-engineer copilot learning history.
PRODUCT_MEMORY_FILENAME: str = "copilot-memory.json"

#: Committed record of the inputs that produced the committed CI files,
#: plus per-file sha256 so ``fluid forge`` can detect user hand-edits on any
#: other teammate's machine.
PRODUCT_CI_STATE_FILENAME: str = "ci-state.json"

#: Gitignored per-run forge manifest.
PRODUCT_FORGE_RECEIPT_FILENAME: str = "forge-receipt.json"

#: Slice UX-J: gitignored discovery-report cache keyed on workspace
#: file-tree hash so subsequent forge runs skip the expensive BFS +
#: schema-inference pass when nothing changed on disk.
WORKSPACE_DISCOVERY_CACHE_FILENAME: str = "discovery-cache.json"


def product_state_dir(product_root: Path) -> Path:
    """Return ``<product_root>/.fluid`` without creating it."""
    return Path(product_root) / PRODUCT_STATE_DIRNAME


def product_contract_path(product_root: Path) -> Path:
    """Return ``<product_root>/contract.fluid.yaml``."""
    return Path(product_root) / CONTRACT_FILENAME


def product_bundled_path(product_root: Path) -> Path:
    """Return ``<product_root>/contract.bundled.yaml``."""
    return Path(product_root) / CONTRACT_BUNDLED_FILENAME


def product_lock_path(product_root: Path) -> Path:
    """Return ``<product_root>/contract.lock.yaml``."""
    return Path(product_root) / CONTRACT_LOCK_FILENAME


def product_fragments_dir(product_root: Path) -> Path:
    """Return ``<product_root>/fragments`` without creating it."""
    return Path(product_root) / PRODUCT_FRAGMENTS_DIRNAME


def product_overlays_dir(product_root: Path) -> Path:
    """Return ``<product_root>/overlays`` without creating it."""
    return Path(product_root) / PRODUCT_OVERLAYS_DIRNAME


def product_memory_path(product_root: Path) -> Path:
    """Return ``<product_root>/.fluid/copilot-memory.json``."""
    return product_state_dir(product_root) / PRODUCT_MEMORY_FILENAME


def product_ci_state_path(product_root: Path) -> Path:
    """Return ``<product_root>/.fluid/ci-state.json``."""
    return product_state_dir(product_root) / PRODUCT_CI_STATE_FILENAME


def product_forge_receipt_path(product_root: Path) -> Path:
    """Return ``<product_root>/.fluid/forge-receipt.json``."""
    return product_state_dir(product_root) / PRODUCT_FORGE_RECEIPT_FILENAME


def workspace_discovery_cache_path(workspace_root: Path) -> Path:
    """Return ``<workspace_root>/.fluid/discovery-cache.json``."""
    return workspace_state_dir(workspace_root) / WORKSPACE_DISCOVERY_CACHE_FILENAME


# ---------------------------------------------------------------------------
# CI provenance header identifier
# ---------------------------------------------------------------------------

#: Schema identifier embedded in the header comment of every generated CI
#: file.  Used by future ``fluid doctor`` checks to verify provenance without
#: parsing a separate state file.
CI_HEADER_SCHEMA: str = "fluid.ci/v1"
