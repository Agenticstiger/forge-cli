#!/usr/bin/env python3
# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Check license headers on tracked Python files.

The header is the two-line REUSE-style form: a copyright notice plus an
``SPDX-License-Identifier: Apache-2.0`` line (REUSE spec 3.3; the ASF also
accepts an SPDX header form in place of the long boilerplate). The Apache-2.0
terms, warranty disclaimer included (sections 7-8), bind through the LICENSE
file regardless of what the header says, so the per-file block only has to
name the holder and the license.

The year is one fixed value, so the header never needs a yearly bump. The
holder is spelled as in LICENSE, NOTICE and pyproject.toml, and the check pins
holder AND year rather than any "Copyright" line, so a misspelt entity cannot
creep back in (the license files once carried "Pty Ltd"). The legacy 14-line
boilerplate (with the old "2024-2026 ... Ltd" notice) fails the check;
running ``add_license_headers.py`` migrates it. The writer and this checker
share one definition of a valid header; tests/test_license_headers.py pins
that.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

REQUIRED_HEADER_TOKEN = "Copyright 2025 Agentics Transformation Limited"
EXCLUDED_PREFIXES = (
    "examples/",
    # Vendored, generated upstream, and byte-identical across every repo that
    # carries it — that identity is the whole integrity guarantee: check.py
    # hashes its own source into CANON_ID, so a per-repo header would change
    # the digest here and make the gate report BLIND. It also cannot carry an
    # Apache header honestly, because the same bytes ship into a proprietary
    # repo. Licensing for the directory is stated once in
    # tools/product_guardrail/README.md instead of per file.
    "tools/product_guardrail/",
)

# Every further line the header must carry. Matched whitespace-insensitively
# against the file preamble so a reflowed comment or an extra leading blank line
# doesn't fail the gate. Keep in sync with the ``apache2`` template in
# scripts/add_license_headers.py; tests/test_license_headers.py pins that the
# writer's output satisfies this checker.
REQUIRED_HEADER_LINES = ("SPDX-License-Identifier: Apache-2.0",)

# The header is two lines; allow room for a shebang, an encoding declaration,
# and blank lines before giving up.
HEADER_PREAMBLE_LINES = 10


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

    Empty list means the file carries the complete two-line header. Comparison
    collapses whitespace so comment reflowing doesn't matter, but a missing
    line, wrong holder or wrong year does.
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
    """Check the file preamble for the copyright line and the SPDX identifier."""
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
