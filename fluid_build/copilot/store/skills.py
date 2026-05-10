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

"""Unified ``skills/*`` namespace — compiled skills in the staged store.

Before this module existed, ``.fluid/skills.compiled.json`` was the sole
source of truth for compiled skills: a per-workspace JSON file produced
by ``fluid skills compile`` and memoized in-process by
:mod:`fluid_build.cli.forge_copilot_skills_cache`. That worked fine for
a single developer on a single machine, but it left skills **outside**
the unified staged store — the only copilot artefact that didn't share
a backend with memory, cache, history, and audit.

This module closes that gap without removing the file path. It exposes
four helpers:

- :func:`workspace_key` — stable, filesystem-safe key derived from the
  workspace root (SHA1 prefix of the resolved absolute path).
- :func:`write_skills_to_store` — persist a compiled payload to
  ``skills/<workspace_key>`` in any :class:`Store` implementation.
- :func:`load_skills_from_store` — read it back.
- :func:`mirror_skills_to_store` — best-effort mirror that resolves a
  store from env/config, logs failures at DEBUG, and never raises so
  that a flaky store never blocks a successful file-based write.

Callers that want to upgrade to team-shared skills point
``FLUID_STORE_BACKEND`` at a shared SQLite/Postgres and now get skills
replicated alongside memory and cache.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.copilot.store.base import Store, StoreRecord
from fluid_build.copilot.store.namespaces import SKILL_NAMESPACES

LOG = logging.getLogger("fluid.copilot.store.skills")

SKILLS_NAMESPACE = "skills"
"""Canonical namespace root for compiled-skills records."""

assert (
    SKILLS_NAMESPACE in SKILL_NAMESPACES
), "skills must be registered in SKILL_NAMESPACES — check fluid_build/copilot/store/namespaces.py"

_WORKSPACE_KEY_LENGTH = 16
"""How many hex chars of the SHA1 to keep. Twelve collision-resistant
chars is overkill for per-user workspace counts; sixteen stays legible
and keeps debug output uncluttered."""


def workspace_key(workspace_root: Path) -> str:
    """Return a stable ``skills/*`` key for ``workspace_root``.

    Hashes the absolute resolved path with SHA1 and keeps the first
    :data:`_WORKSPACE_KEY_LENGTH` characters. The prefix is
    deterministic across runs, safe for every filesystem backend, and
    short enough to appear in logs.
    """
    resolved = Path(workspace_root).expanduser().resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8"), usedforsecurity=False).hexdigest()
    return digest[:_WORKSPACE_KEY_LENGTH]


def write_skills_to_store(
    store: Store,
    workspace_root: Path,
    compiled: Dict[str, Any],
    *,
    ttl: Optional[int] = None,
) -> StoreRecord:
    """Persist ``compiled`` to ``skills/<workspace_key>`` in ``store``.

    The stored value is the compiled payload as-is (a plain JSON dict).
    Metadata captures the workspace path so operators can trace a record
    back to its origin without decoding the key.

    Returns the :class:`StoreRecord` returned by :meth:`Store.put` so
    callers can inspect ``created_at`` / ``expires_at`` if they need to.
    """
    resolved = str(Path(workspace_root).expanduser().resolve())
    key = workspace_key(workspace_root)
    return store.put(
        SKILLS_NAMESPACE,
        key,
        compiled,
        ttl=ttl,
        metadata={"workspace_root": resolved},
    )


def load_skills_from_store(
    store: Store,
    workspace_root: Path,
) -> Optional[Dict[str, Any]]:
    """Return the compiled-skills payload for ``workspace_root``, if any.

    Returns ``None`` when the namespace is empty, the record has
    expired, or the stored value isn't a dict (defensive — protects
    callers from malformed shared-store entries).
    """
    key = workspace_key(workspace_root)
    record = store.get(SKILLS_NAMESPACE, key)
    if record is None:
        return None
    value = record.value
    if not isinstance(value, dict):
        LOG.debug(
            "skills/%s holds non-dict value (%s); ignoring",
            key,
            type(value).__name__,
        )
        return None
    return value


def mirror_skills_to_store(
    workspace_root: Path,
    compiled: Dict[str, Any],
    *,
    store: Optional[Store] = None,
    ttl: Optional[int] = None,
) -> Optional[StoreRecord]:
    """Best-effort write to the staged store; never raises.

    Used as a side effect of the existing file-based
    ``write_compiled_skills`` so that ``.fluid/skills.compiled.json``
    stays canonical while the store gains a read-ready mirror. The
    caller gets back the :class:`StoreRecord` on success or ``None`` on
    any failure (with a DEBUG log entry).

    A ``store=`` argument lets tests inject a deterministic backend;
    when omitted, the helper resolves one from env/config via
    :func:`fluid_build.copilot.store.factory.resolve_store`.
    """
    try:
        resolved_store = store
        if resolved_store is None:
            from fluid_build.copilot.store.factory import resolve_store as _resolve

            resolved_store = _resolve(workspace_root=workspace_root)
        return write_skills_to_store(resolved_store, workspace_root, compiled, ttl=ttl)
    except Exception as exc:  # noqa: BLE001 — best-effort mirror
        LOG.debug("skills mirror failed: %s", exc)
        return None


def load_skills_from_store_best_effort(
    workspace_root: Path,
    *,
    store: Optional[Store] = None,
) -> Optional[Dict[str, Any]]:
    """Best-effort read from the staged store; never raises.

    Mirror of :func:`mirror_skills_to_store` for the load side. Used
    by :mod:`forge_copilot_skills_cache` as a last-resort fallback when
    neither the compiled file nor the raw YAML is present — e.g. a
    teammate cloning a repo that pulls skills from a shared Postgres
    without first running ``fluid skills compile`` locally.
    """
    try:
        resolved_store = store
        if resolved_store is None:
            from fluid_build.copilot.store.factory import resolve_store as _resolve

            resolved_store = _resolve(workspace_root=workspace_root)
        return load_skills_from_store(resolved_store, workspace_root)
    except Exception as exc:  # noqa: BLE001 — best-effort read
        LOG.debug("skills store read failed: %s", exc)
        return None


__all__ = [
    "SKILLS_NAMESPACE",
    "load_skills_from_store",
    "load_skills_from_store_best_effort",
    "mirror_skills_to_store",
    "workspace_key",
    "write_skills_to_store",
]
