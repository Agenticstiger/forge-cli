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
field from the contract YAML and resolve a path under ``contract.parent``. A
malicious contract author could set ``repository: ../../etc`` and trick the
runner into executing a script outside the workspace, or pointing ``dbt build``
at a project in a sensitive location. ``confine_to_workspace`` rejects any
resolved path that is not a descendant of the workspace root (the contract's
parent directory). The ``cli/security.py`` validator handles the same threat
class for copilot tools; this helper is the build-runner counterpart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


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
