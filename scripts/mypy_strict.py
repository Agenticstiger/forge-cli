#!/usr/bin/env python3
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

"""Run ``mypy --strict`` on the incremental hotspot allowlist.

The strict allowlist is declared in exactly one place — the
``[[tool.mypy.overrides]] strict = true`` block in ``pyproject.toml`` — which
is the single source of truth. This runner derives the target file list from
there and runs::

    mypy --strict --follow-imports=silent <allowlist files>

``--follow-imports=silent`` scopes the check to exactly those modules, so
strict errors in the still-loose rest of the ~40K-LOC tree can never fail the
gate. Both ``make typecheck-strict`` and the ``typecheck-strict`` CI job call
this script, so there is one implementation and no chance of the two drifting.

To add a module to the strict gate: make it clean, then add its dotted module
name to the ``module`` array in pyproject.toml. Nothing here changes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — mypy pulls ``tomli`` as a dependency
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]


def strict_allowlist_files() -> list[str]:
    """Return the allowlist's source-file paths, derived from pyproject.toml."""
    cfg = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    files: list[str] = []
    for override in cfg["tool"]["mypy"].get("overrides", []):
        if override.get("strict") is True:
            mods = override["module"]
            for module in mods if isinstance(mods, list) else [mods]:
                files.append(module.replace(".", "/") + ".py")
    return files


def main() -> int:
    files = strict_allowlist_files()
    if not files:
        print("error: no [[tool.mypy.overrides]] strict block in pyproject.toml", file=sys.stderr)
        return 1
    print("Strict allowlist:", " ".join(files), flush=True)
    return subprocess.call(
        [sys.executable, "-m", "mypy", "--strict", "--follow-imports=silent", *files],
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
