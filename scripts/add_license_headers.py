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

"""
Add or replace license headers in all Python source files.

Usage:
    # Add Apache 2.0 headers (default):
    python scripts/add_license_headers.py

    # Use a custom license template file:
    python scripts/add_license_headers.py --template my_license.txt

    # Change copyright holder and years:
    python scripts/add_license_headers.py --holder "Acme Corp" --years "2025-2026"

    # Dry run — show what would change without modifying files:
    python scripts/add_license_headers.py --dry-run

    # Remove existing headers:
    python scripts/add_license_headers.py --remove

    # Only process specific directories:
    python scripts/add_license_headers.py --dirs fluid_build tests scripts tools
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Built-in license templates — add new ones here to support other licenses.
# The placeholder {HOLDER} and {YEARS} are substituted at runtime.
# ---------------------------------------------------------------------------
LICENSES = {
    "apache2": (
        "# Copyright {YEARS} {HOLDER}\n"
        "#\n"
        '# Licensed under the Apache License, Version 2.0 (the "License");\n'
        "# you may not use this file except in compliance with the License.\n"
        "# You may obtain a copy of the License at\n"
        "#\n"
        "#     http://www.apache.org/licenses/LICENSE-2.0\n"
        "#\n"
        "# Unless required by applicable law or agreed to in writing, software\n"
        '# distributed under the License is distributed on an "AS IS" BASIS,\n'
        "# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n"
        "# See the License for the specific language governing permissions and\n"
        "# limitations under the License.\n"
    ),
    "mit": (
        "# Copyright (c) {YEARS} {HOLDER}\n"
        "#\n"
        "# Permission is hereby granted, free of charge, to any person obtaining a copy\n"
        '# of this software and associated documentation files (the "Software"), to deal\n'
        "# in the Software without restriction, including without limitation the rights\n"
        "# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n"
        "# copies of the Software, and to permit persons to whom the Software is\n"
        "# furnished to do so, subject to the following conditions:\n"
        "#\n"
        "# The above copyright notice and this permission notice shall be included in\n"
        "# all copies or substantial portions of the Software.\n"
        "#\n"
        '# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n'
        "# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n"
        "# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n"
        "# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n"
        "# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n"
        "# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN\n"
        "# THE SOFTWARE.\n"
    ),
    "bsd3": (
        "# Copyright (c) {YEARS} {HOLDER}\n"
        "# All rights reserved.\n"
        "#\n"
        "# Redistribution and use in source and binary forms, with or without\n"
        "# modification, are permitted provided that the following conditions are met:\n"
        "#\n"
        "# 1. Redistributions of source code must retain the above copyright notice,\n"
        "#    this list of conditions and the following disclaimer.\n"
        "# 2. Redistributions in binary form must reproduce the above copyright notice,\n"
        "#    this list of conditions and the following disclaimer in the documentation\n"
        "#    and/or other materials provided with the distribution.\n"
        "# 3. Neither the name of the copyright holder nor the names of its\n"
        "#    contributors may be used to endorse or promote products derived from\n"
        "#    this software without specific prior written permission.\n"
        "#\n"
        '# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"\n'
        "# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE\n"
        "# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE\n"
        "# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE\n"
        "# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR\n"
        "# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF\n"
        "# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS\n"
        "# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN\n"
        "# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)\n"
        "# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE\n"
        "# POSSIBILITY OF SUCH DAMAGE.\n"
    ),
}

DEFAULT_HOLDER = "Agentics Transformation Ltd"
DEFAULT_YEARS = "2024-2026"
DEFAULT_LICENSE = "apache2"
DEFAULT_DIRS = ["fluid_build", "tests", "scripts", "tools"]

# Regex that matches any of our known license header blocks (for replacement/removal).
# Handles single or corrupted dual-header blocks (e.g. MIT + Apache fragments).
HEADER_PATTERN = re.compile(
    r"^(#![^\n]*\n)?"  # optional shebang
    r"(# Copyright.*?\n"  # copyright line
    r"(?:#[^\n]*\n)*?"  # continuation comment lines
    r"# .*(?:License|LICENSE|Permission|WARRANTY|DAMAGE|DEALINGS)[^\n]*\n"
    r"(?:\n?# [^\n]*\n)*?"  # possible blank comment + more license lines (dual-header)
    r"(?:# .*(?:License|LICENSE|Permission|WARRANTY|DAMAGE|DEALINGS)[^\n]*\n)?)",  # second terminator
    re.MULTILINE,
)


def build_header(
    license_key: str, holder: str, years: str, template_path: str | None = None
) -> str:
    """Build the license header string."""
    if template_path:
        text = Path(template_path).read_text()
        # Wrap each line as a Python comment if not already
        lines = []
        for line in text.splitlines():
            if not line.startswith("#"):
                line = f"# {line}".rstrip()
            lines.append(line)
        header = "\n".join(lines) + "\n"
    else:
        if license_key not in LICENSES:
            print(f"Unknown license: {license_key}. Available: {', '.join(LICENSES)}")
            sys.exit(1)
        header = LICENSES[license_key]

    return header.replace("{HOLDER}", holder).replace("{YEARS}", years)


def has_license_header(content: str) -> bool:
    """Check whether the file already contains a license header."""
    # Look in the first 30 lines for a copyright notice
    head = "\n".join(content.splitlines()[:30])
    return "Copyright" in head and ("License" in head or "Permission" in head)


def remove_header(content: str) -> str:
    """Remove an existing license header from file content.

    Handles single headers (Apache or MIT) and corrupted dual-headers
    (MIT body + orphaned Apache fragment) by stripping all leading comment
    lines that are part of a license block.
    """
    lines = content.splitlines(True)

    # Preserve shebang
    start = 0
    shebang = ""
    if lines and lines[0].startswith("#!"):
        shebang = lines[0]
        start = 1

    # Preserve encoding declaration
    if start < len(lines) and lines[start].startswith("# -*- coding"):
        shebang += lines[start]
        start += 1

    # Find the end of the license block: contiguous comment/blank lines
    # that contain license keywords
    end = start
    found_copyright = False
    found_license_end = False

    for i in range(start, len(lines)):
        line = lines[i].rstrip()

        if line.startswith("#"):
            if "Copyright" in line:
                found_copyright = True
            if any(
                kw in line
                for kw in (
                    "License",
                    "LICENSE",
                    "Permission",
                    "WARRANTY",
                    "DEALINGS",
                    "DAMAGE",
                    "apache.org",
                    "THE SOFTWARE",
                )
            ):
                found_license_end = True
            end = i + 1
        elif line == "":
            # Allow blank lines within the header block, but only if we haven't
            # finished the license text yet or the next line is still a comment
            if found_license_end:
                # Check if the next non-blank line is another license fragment
                next_content = i + 1
                while next_content < len(lines) and lines[next_content].strip() == "":
                    next_content += 1
                if (
                    next_content < len(lines)
                    and lines[next_content].startswith("#")
                    and any(
                        kw in lines[next_content]
                        for kw in ("License", "you may not", "apache.org", "Copyright")
                    )
                ):
                    end = i + 1
                    found_license_end = False  # reset — there's more header
                else:
                    break
            else:
                end = i + 1
        else:
            break

    if not found_copyright:
        return content

    # Strip the header and any trailing blank lines
    result = shebang + "".join(lines[end:]).lstrip("\n")
    return result


def add_header(content: str, header: str) -> str:
    """Add a license header to file content, preserving shebang and encoding lines."""
    lines = content.splitlines(True)
    insert_at = 0

    # Preserve shebang
    if lines and lines[0].startswith("#!"):
        insert_at = 1

    # Preserve encoding declaration
    if len(lines) > insert_at and lines[insert_at].startswith("# -*- coding"):
        insert_at += 1

    # Build result
    prefix = "".join(lines[:insert_at])
    suffix = "".join(lines[insert_at:])

    # Ensure blank line between header and code
    separator = "\n" if suffix and not suffix.startswith("\n") else ""

    return prefix + header + separator + suffix


def process_file(
    path: Path, header: str, *, remove_only: bool = False, dry_run: bool = False
) -> str:
    """Process a single file. Returns 'added', 'replaced', 'removed', or 'skipped'."""
    content = path.read_text(encoding="utf-8", errors="replace")

    # Skip empty or autogenerated files
    if not content.strip():
        return "skipped"

    already_has = has_license_header(content)

    if remove_only:
        if not already_has:
            return "skipped"
        new_content = remove_header(content)
        if not dry_run:
            path.write_text(new_content, encoding="utf-8")
        return "removed"

    if already_has:
        # Replace existing header
        stripped = remove_header(content)
        new_content = add_header(stripped, header)
        action = "replaced"
    else:
        new_content = add_header(content, header)
        action = "added"

    if content == new_content:
        return "skipped"

    if not dry_run:
        path.write_text(new_content, encoding="utf-8")
    return action


def collect_files(dirs: list[str], root: Path) -> list[Path]:
    """Collect all .py files under the given directories."""
    files = []
    for d in dirs:
        target = root / d
        if not target.exists():
            print(f"  Warning: directory '{d}' does not exist, skipping")
            continue
        files.extend(sorted(target.rglob("*.py")))
    return files


def main():
    parser = argparse.ArgumentParser(
        description="Add, replace, or remove license headers in Python files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  # Apache 2.0 default
  %(prog)s --license mit --holder "Me"      # MIT license
  %(prog)s --template header.txt            # Custom template file
  %(prog)s --remove                         # Strip all headers
  %(prog)s --dry-run                        # Preview changes
  %(prog)s --dirs fluid_build tests scripts tools  # Only maintained source dirs
        """,
    )
    parser.add_argument(
        "--license",
        choices=list(LICENSES.keys()),
        default=DEFAULT_LICENSE,
        help=f"Built-in license template (default: {DEFAULT_LICENSE})",
    )
    parser.add_argument(
        "--template",
        metavar="FILE",
        help="Path to a custom license template file (overrides --license)",
    )
    parser.add_argument(
        "--holder",
        default=DEFAULT_HOLDER,
        help=f"Copyright holder (default: {DEFAULT_HOLDER})",
    )
    parser.add_argument(
        "--years",
        default=DEFAULT_YEARS,
        help=f"Copyright years (default: {DEFAULT_YEARS})",
    )
    parser.add_argument(
        "--dirs",
        nargs="+",
        default=DEFAULT_DIRS,
        help=f"Directories to process (default: {' '.join(DEFAULT_DIRS)})",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove existing license headers instead of adding them",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying files",
    )

    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent

    header = build_header(args.license, args.holder, args.years, args.template)

    if not args.remove:
        print(f"License: {args.template or args.license}")
        print(f"Holder:  {args.holder}")
        print(f"Years:   {args.years}")
        if args.dry_run:
            print("Mode:    DRY RUN")
        print("---")
        print(header)
        print("---")

    files = collect_files(args.dirs, root)
    print(f"\nProcessing {len(files)} Python files in: {', '.join(args.dirs)}\n")

    counts = {"added": 0, "replaced": 0, "removed": 0, "skipped": 0}
    for path in files:
        rel = path.relative_to(root)
        result = process_file(path, header, remove_only=args.remove, dry_run=args.dry_run)
        counts[result] += 1
        if result != "skipped":
            print(f"  {result:>8}  {rel}")

    print(
        f"\nDone: {counts['added']} added, {counts['replaced']} replaced, "
        f"{counts['removed']} removed, {counts['skipped']} skipped"
    )

    if args.dry_run:
        print("\n(Dry run — no files were modified)")


if __name__ == "__main__":
    main()
