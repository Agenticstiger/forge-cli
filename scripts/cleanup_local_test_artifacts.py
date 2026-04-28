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

"""Defensive cleanup for local-provider integration tests.

The DuckDB tests run inside ``tmp_path`` fixtures which pytest deletes
automatically, so this script is mostly belt-and-braces — it sweeps
common locations where a crashed test could have left files behind.

Idempotent and safe to run repeatedly.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


def _candidate_dirs() -> list[Path]:
    """Locations where stray ``forge-ci-*`` workspaces might land."""
    candidates: list[Path] = []
    candidates.append(Path(tempfile.gettempdir()))
    if "RUNNER_TEMP" in os.environ:
        candidates.append(Path(os.environ["RUNNER_TEMP"]))
    if "TMPDIR" in os.environ:
        candidates.append(Path(os.environ["TMPDIR"]))
    cwd = Path.cwd()
    if (cwd / "runtime").is_dir():
        candidates.append(cwd / "runtime")
    return [p for p in candidates if p.is_dir()]


def main() -> int:
    swept = 0
    for base in _candidate_dirs():
        for entry in base.glob("forge-ci-*"):
            try:
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
                print(f"removed {entry}")
                swept += 1
            except OSError as exc:
                # Best effort — log and keep going.
                print(f"could not remove {entry}: {exc}", file=sys.stderr)

    print(f"local cleanup complete; {swept} entries removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
