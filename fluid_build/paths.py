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

"""Single source of truth for FLUID's filesystem layout.

Two roots:

* **Workspace-local** (``.fluid/``): per-project state — receipts,
  rollback snapshots, agent transcripts, lineage. Lives next to the
  contract. Path resolves against the current working directory by
  default; override via ``FLUID_WORKSPACE_ROOT``.

* **User-global** (``~/.fluid/``): per-engineer settings, credentials,
  cache. Path resolves against ``$HOME`` by default; override via
  ``FLUID_USER_HOME``.

Every site that previously hardcoded a string literal like
``".fluid/agents"`` or ``"~/.fluid/credentials.yaml"`` should call
the helper here instead. One source of truth makes it possible to:

* Run multiple FLUID workspaces side-by-side (point ``FLUID_WORKSPACE_ROOT``
  at a per-test directory).
* Containerise (point ``FLUID_USER_HOME`` at a mounted volume).
* Audit (`fluid env --paths` enumerates every path).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# ── Roots ────────────────────────────────────────────────────────────


def workspace_root() -> Path:
    """Return the workspace root (where ``.fluid/`` lives).

    Defaults to ``Path.cwd()``. Override via ``$FLUID_WORKSPACE_ROOT``
    so test suites and per-tenant runners can sandbox their state
    files without colliding with the user's working directory.
    """
    override = os.environ.get("FLUID_WORKSPACE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd()


def user_home() -> Path:
    """Return the user-global FLUID config root.

    Defaults to ``~/.fluid``. Override via ``$FLUID_USER_HOME`` for
    container deployments where ``$HOME`` is read-only or shared.
    """
    override = os.environ.get("FLUID_USER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path("~/.fluid").expanduser().resolve()


# ── Workspace-local paths (under .fluid/) ────────────────────────────


def workspace_dotfluid(*, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid``"""
    return (root or workspace_root()) / ".fluid"


def rollback_state_file(*, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/rollback-state.json`` — destructive-apply backups."""
    return workspace_dotfluid(root=root) / "rollback-state.json"


def agents_dir(*, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/agents`` — per-run AI transcripts + receipts."""
    return workspace_dotfluid(root=root) / "agents"


def agent_run_dir(run_id: str, *, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/agents/<run-id>``."""
    return agents_dir(root=root) / run_id


def runs_dir(*, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/runs`` — engine run records (state-store)."""
    return workspace_dotfluid(root=root) / "runs"


def run_record_dir(product_id: str, build_id: str, *, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/runs/<product-id>/<build-id>``."""
    return runs_dir(root=root) / product_id / build_id


def dlq_dir(run_id: str, *, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/dlq/<run-id>`` — DLQ NDJSON sinks."""
    return workspace_dotfluid(root=root) / "dlq" / run_id


def context_file(*, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/context.json`` — interview state + interrupt resume."""
    return workspace_dotfluid(root=root) / "context.json"


def skills_compiled_cache(*, root: Optional[Path] = None) -> Path:
    """``<root>/.fluid/skills.compiled.json`` — industry skills pack."""
    return workspace_dotfluid(root=root) / "skills.compiled.json"


def runtime_dir(*, root: Optional[Path] = None) -> Path:
    """``<root>/runtime`` — apply outputs, generated parquet, reports."""
    return (root or workspace_root()) / "runtime"


def apply_report_html(*, root: Optional[Path] = None) -> Path:
    """``<root>/runtime/apply_report.html``."""
    return runtime_dir(root=root) / "apply_report.html"


# ── User-global paths (under ~/.fluid/) ──────────────────────────────


def user_config_file(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/config.yaml`` — global behavior + provider defaults."""
    return (root or user_home()) / "config.yaml"


def user_credentials_file(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/credentials.yaml`` — Fernet-encrypted credential store."""
    return (root or user_home()) / "credentials.yaml"


def user_ai_config(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/ai_config.json`` — interactive ai-setup state."""
    return (root or user_home()) / "ai_config.json"


def user_personal_memory(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/personal-memory.json`` — per-engineer copilot memory."""
    return (root or user_home()) / "personal-memory.json"


def user_usage_file(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/usage.json`` — first-run telemetry / tip cadence."""
    return (root or user_home()) / "usage.json"


def user_cache_dir(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/cache`` — model price snapshots, schema introspections."""
    return (root or user_home()) / "cache"


def user_store_dir(*, root: Optional[Path] = None) -> Path:
    """``~/.fluid/store`` — episodic memory store (semantic memory backend)."""
    return (root or user_home()) / "store"


__all__ = [
    "workspace_root",
    "user_home",
    "workspace_dotfluid",
    "rollback_state_file",
    "agents_dir",
    "agent_run_dir",
    "runs_dir",
    "run_record_dir",
    "dlq_dir",
    "context_file",
    "skills_compiled_cache",
    "runtime_dir",
    "apply_report_html",
    "user_config_file",
    "user_credentials_file",
    "user_ai_config",
    "user_personal_memory",
    "user_usage_file",
    "user_cache_dir",
    "user_store_dir",
]
