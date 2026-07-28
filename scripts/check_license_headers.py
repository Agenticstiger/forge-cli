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

"""Check license headers on tracked Python files.

Validates the WHOLE Apache-2.0 boilerplate, not just the copyright line.
Checking only the copyright token let 346 files drift to a header truncated
mid-boilerplate — 323 cut after the LICENSE-2.0 URL and 23 cut right after
"you may not use this file…" — dropping the warranty and liability
disclaimer that Apache-2.0's appendix asks to be attached. Every one of
them passed the old check, and `add_license_headers.py` (which writes the
complete block) rewrote all 346 on any run, so the two scripts disagreed
about what "has a header" means. They now share one definition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

REQUIRED_HEADER_TOKEN = "Copyright 2024-2026"
EXCLUDED_PREFIXES = ("examples/",)

# Every substantive line of the Apache-2.0 boilerplate. Matched
# whitespace-insensitively against the file preamble so a reflowed comment
# or an extra leading blank line doesn't fail the gate, while a genuinely
# truncated block does. Keep in sync with the ``apache2`` template in
# scripts/add_license_headers.py — tests/test_license_headers.py pins that
# the writer's output satisfies this checker.
REQUIRED_HEADER_LINES = (
    'Licensed under the Apache License, Version 2.0 (the "License");',
    "you may not use this file except in compliance with the License.",
    "You may obtain a copy of the License at",
    "http://www.apache.org/licenses/LICENSE-2.0",
    "Unless required by applicable law or agreed to in writing, software",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "See the License for the specific language governing permissions and",
    "limitations under the License.",
)

# The boilerplate is 14 lines; allow room for a shebang, an encoding
# declaration, and blank lines before giving up.
HEADER_PREAMBLE_LINES = 25


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


def missing_header_parts(text: str) -> list[str]:
    """Return the required header pieces absent from ``text``'s preamble.

    Empty list means the file carries the complete boilerplate. Comparison
    collapses whitespace so comment reflowing doesn't matter, but a missing
    clause does.
    """
    preamble = "\n".join(text.splitlines()[:HEADER_PREAMBLE_LINES])
    normalized = " ".join(preamble.split())
    missing = []
    if REQUIRED_HEADER_TOKEN not in normalized:
        missing.append(REQUIRED_HEADER_TOKEN)
    missing.extend(
        line for line in REQUIRED_HEADER_LINES if " ".join(line.split()) not in normalized
    )
    return missing


def has_required_header(path: Path) -> bool:
    """Check the file preamble for the complete Apache-2.0 boilerplate."""
    return not missing_header_parts(path.read_text(encoding="utf-8", errors="ignore"))


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
        print("::error::Files with a missing or incomplete license header:")
        for path in missing_headers:
            absent = missing_header_parts((repo_root / path).read_text(encoding="utf-8"))
            print(f"{path}  (missing {len(absent)} required line(s), first: {absent[0]!r})")
        print("")
        print("Examples under examples/ are intentionally exempt.")
        print("Run: python scripts/add_license_headers.py")
        return 1

    print("All checked Python files have license headers ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
