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

"""Where a FLUID workspace starts: the nearest directory holding ``fluid.workspace.yaml``.

Moved here from ``cli/workspace_config.py`` (which re-exports it, as
``cli/artifact_paths.py`` re-exports the file name) so the build runners can
find the workspace a contract belongs to: ``build_runners`` may not import
``cli`` (the import-linter contract in ``pyproject.toml``), and resolving a
build's ``consumes[]`` starts here. Stdlib only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

__all__ = ["WORKSPACE_CONFIG_FILENAME", "find_workspace_root"]

#: The visible, committed workspace config file at the workspace root.
#: Kept as-is for backward compatibility with every repo in the wild.
WORKSPACE_CONFIG_FILENAME: str = "fluid.workspace.yaml"


def find_workspace_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from *start* looking for ``fluid.workspace.yaml``.

    Returns the directory containing the file, or ``None``.
    """
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / WORKSPACE_CONFIG_FILENAME).is_file():
            return parent
    return None
