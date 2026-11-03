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

"""No shipped string may name `fluid-build` as the package to install.

This project's PyPI distribution is `data-product-forge`. `fluid-build` exists
on PyPI only as an alias the maintainers registered so old releases stay safe.
Install hints, upgrade advice and doc snippets that said `pip install
fluid-build[...]` sent readers to a package that is not this one.

SCOPE: any `fluid-build` NOT immediately followed by `.` fails. A `fluid-build.`
is a domain (`fluid-build.dev`, `fluid-build.io`), a separate concern covered by
the reserved-host fence. Text is scanned, not the AST, because the offenders were
log strings, comments, docstrings and Markdown alike.

ALLOWLIST: an identity that must stay as it is, matched by its line (not the
whole file), with the reason beside it.
`tests/` is not scanned.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

WRONG_NAME = re.compile(r"fluid-build(?!\.)")

# (path relative to the repo root, fragment of the allowed line) -> reason. An entry
# must still occur, or the coverage guard below fails, so a stale entry cannot
# linger; any other `fluid-build` in the same file still fails.
ALLOWLIST = {
    ("fluid_build/observability/tracing.py", '"service.namespace": "fluid-build"'): (
        "OTel `service.namespace` is telemetry identity, not an install hint"
    ),
}


def _allowed(rel: str, text: str) -> bool:
    return any(rel == path and fragment in text for path, fragment in ALLOWLIST)


def _scanned_files() -> list[pathlib.Path]:
    files = [p for p in (ROOT / "fluid_build").rglob("*.py") if "__pycache__" not in p.parts]
    files += [ROOT / n for n in ("AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml")]
    return sorted(files)


def _occurrences() -> list[tuple[str, int, str]]:
    out = []
    for path in _scanned_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if WRONG_NAME.search(line):
                out.append((rel, lineno, line.strip()))
    return out


def test_the_scan_reaches_the_tree():
    """A scanner that found nothing would make the test below vacuously green."""
    files = _scanned_files()
    assert len(files) > 500, f"only found {len(files)} files to scan"
    occurrences = _occurrences()
    for path, fragment in ALLOWLIST:
        assert any(
            rel == path and fragment in text for rel, _, text in occurrences
        ), f"allowlisted occurrence {fragment!r} in {path} not seen"


def test_no_shipped_string_names_the_wrong_package():
    offenders = [
        f"{rel}:{lineno} {text}" for rel, lineno, text in _occurrences() if not _allowed(rel, text)
    ]
    assert offenders == [], (
        "the PyPI distribution is `data-product-forge`, not `fluid-build`; "
        "install hints must name it:\n  " + "\n  ".join(offenders)
    )
