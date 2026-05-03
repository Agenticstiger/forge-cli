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

"""Cross-CLI run-id correlation.

The 11-stage pipeline issues separate ``fluid bundle``, ``fluid plan``,
``fluid apply``, ``fluid verify``, ``fluid publish`` invocations.
Without a shared run-id, OpenTelemetry spans emitted by each stage
land in unrelated traces — operators can't reconstruct "what
happened on Monday's deploy of orders_v1?" by ID.

This module provides a single canonical run-id resolver:

1. ``$FLUID_RUN_ID`` env var (operator override / CI injection).
2. ``.fluid/run-id.txt`` (persisted between stages).
3. Newly-generated id (first stage of a run).

Every CLI stage that emits OTel spans should call
:func:`get_or_create_run_id` early and stamp the result onto its root
span via ``traced_span(name, {"fluid.run_id": run_id})``. The id is
short (12 chars) so it fits in span attributes without truncation.

For the staged copilot pipeline, the run_id flows from the staged
session into ``fluid.copilot.staged.invocation`` spans (see
``coordinator.py``). For non-AI commands (bundle / plan / apply),
the run_id is read from this module at command entry.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("fluid.observability.run_id")

#: Canonical env var name. Set to override the persisted id.
RUN_ID_ENV_VAR = "FLUID_RUN_ID"

#: Persisted file location relative to ``.fluid/``. Cleaned up on
#: successful completion of the final pipeline stage (publish or
#: schedule-sync) — see ``cli/_cleanup.py``.
RUN_ID_FILE = "run-id.txt"


def _generate_id() -> str:
    """Build a fresh run-id. 12 hex chars (48 bits) is plenty of entropy
    for a single workspace + short enough for OTel attribute tags."""
    return secrets.token_hex(6)


def _runtime_dir(workspace_root: Optional[Path] = None) -> Path:
    """Resolve ``<workspace>/.fluid``, creating it if missing."""
    root = workspace_root or Path.cwd()
    target = root / ".fluid"
    target.mkdir(parents=True, exist_ok=True)
    return target


def get_or_create_run_id(
    workspace_root: Optional[Path] = None,
    *,
    create_persisted_file: bool = True,
) -> str:
    """Return the active run-id, creating one if none exists.

    Resolution order:

    1. ``$FLUID_RUN_ID`` (env var override).
    2. ``.fluid/run-id.txt`` (persisted from a prior stage).
    3. Newly generated id, persisted to ``.fluid/run-id.txt`` when
       ``create_persisted_file=True`` (the default).

    Args:
        workspace_root: Workspace directory containing ``.fluid``.
            Defaults to the current working directory.
        create_persisted_file: When True (default), a freshly
            generated id is persisted to ``.fluid/run-id.txt`` so
            subsequent CLI invocations in the same workspace can
            correlate. Set False to suppress the side effect (tests).

    Returns:
        12-char hex run-id, e.g. ``"a3f9b21c44d8"``.
    """
    env_value = os.environ.get(RUN_ID_ENV_VAR)
    if env_value:
        return env_value.strip()

    persisted_path = _runtime_dir(workspace_root) / RUN_ID_FILE
    if persisted_path.is_file():
        try:
            existing = persisted_path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError as exc:  # pragma: no cover — defensive
            LOG.debug("run_id_read_failed: path=%s error=%s", persisted_path, exc)

    run_id = _generate_id()
    if create_persisted_file:
        try:
            persisted_path.write_text(run_id + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            LOG.debug("run_id_write_failed: path=%s error=%s", persisted_path, exc)
    return run_id


def clear_run_id(workspace_root: Optional[Path] = None) -> None:
    """Delete the persisted run-id file.

    Called by the final pipeline stage (publish / schedule-sync) when
    the run completes successfully — keeps the workspace from
    accumulating stale ids that would silently cross-correlate
    across logically-distinct deploys.
    """
    persisted_path = _runtime_dir(workspace_root) / RUN_ID_FILE
    try:
        persisted_path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        LOG.debug("run_id_clear_failed: path=%s error=%s", persisted_path, exc)


def run_id_span_attribute(workspace_root: Optional[Path] = None) -> dict:
    """Convenience helper for OTel span emission.

    Returns ``{"fluid.run_id": <id>}`` ready to merge into a
    ``traced_span(name, {...})`` attributes dict. Operators query by
    this attribute in observability dashboards to group all spans
    from one logical pipeline run.
    """
    return {"fluid.run_id": get_or_create_run_id(workspace_root)}


__all__ = [
    "RUN_ID_ENV_VAR",
    "RUN_ID_FILE",
    "get_or_create_run_id",
    "clear_run_id",
    "run_id_span_attribute",
]
