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

"""Check license headers on tracked Python files."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

REQUIRED_HEADER_TOKEN = "Copyright 2024-2026"
EXCLUDED_PREFIXES = ("examples/",)


def tracked_python_files(repo_root: Path) -> list[Path]:
    """Return tracked Python files in the repository."""
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [repo_root / line for line in result.stdout.splitlines() if line.strip()]


def should_check_header(path: Path, repo_root: Path) -> bool:
    """Return whether a tracked Python file should have a required header."""
    relative_path = path.relative_to(repo_root).as_posix()
    return not relative_path.startswith(EXCLUDED_PREFIXES)


def files_requiring_headers(paths: Iterable[Path], repo_root: Path) -> list[Path]:
    """Filter tracked Python files down to the checked set."""
    return [path for path in paths if should_check_header(path, repo_root)]


def has_required_header(path: Path) -> bool:
    """Check the file preamble for the required copyright token."""
    header_preview = "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[:5])
    return REQUIRED_HEADER_TOKEN in header_preview


def find_missing_headers(paths: Iterable[Path], repo_root: Path) -> list[str]:
    """Return repo-relative paths missing the required header."""
    return [
        path.relative_to(repo_root).as_posix() for path in paths if not has_required_header(path)
    ]


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tracked_files = tracked_python_files(repo_root)
    checked_files = files_requiring_headers(tracked_files, repo_root)
    missing_headers = find_missing_headers(checked_files, repo_root)

    if missing_headers:
        print("::error::Files missing license headers:")
        for path in missing_headers:
            print(path)
        print("")
        print("Examples under examples/ are intentionally exempt.")
        print("Run: python scripts/add_license_headers.py")
        return 1

    print("All checked Python files have license headers ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
