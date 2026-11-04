# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pins for the license-header checker + writer staying in agreement.

The two scripts must share one definition of a valid header: the two-line
form (``Copyright 2025 Agentics Transformation Limited`` plus
``SPDX-License-Identifier: Apache-2.0``). When an earlier checker only
grepped the copyright line while the writer wrote the full Apache
boilerplate, following the checker's "Run: python
scripts/add_license_headers.py" remediation turned a one-file fix into a
hundreds-of-files diff.

These tests pin both halves: the checker rejects anything but the exact
two-line header (a missing line, wrong holder, wrong year, the legacy
boilerplate), the writer migrates the legacy block, and the writer is a no-op
on an already-compliant tree.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_license_headers.py"
WRITER = REPO_ROOT / "scripts" / "add_license_headers.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load(CHECKER, "_license_checker")
writer = _load(WRITER, "_license_writer")


FULL_HEADER = writer.build_header("apache2", writer.DEFAULT_HOLDER, writer.DEFAULT_YEARS)

# The legacy 14-line block, kept literal: it models what ~2,100 files carried
# before the two-line header, and the writer must migrate it.
LEGACY_HEADER = (
    "# Copyright 2024-2026 Agentics Transformation Ltd\n"
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
)

COPYRIGHT_LINE = "# Copyright 2025 Agentics Transformation Limited\n"
SPDX_LINE = "# SPDX-License-Identifier: Apache-2.0\n"


# ── The checker requires the exact two-line header ─────────────────────────


def test_writer_template_is_the_two_line_header():
    assert FULL_HEADER == COPYRIGHT_LINE + SPDX_LINE


def test_complete_header_passes():
    assert checker.missing_header_parts(FULL_HEADER + '\n"""Module."""\n') == []


def test_missing_copyright_line_is_rejected():
    missing = checker.missing_header_parts(SPDX_LINE + '\n"""Module."""\n')
    assert checker.REQUIRED_HEADER_TOKEN in missing


def test_missing_spdx_line_is_rejected():
    missing = checker.missing_header_parts(COPYRIGHT_LINE + '\n"""Module."""\n')
    assert missing == ["SPDX-License-Identifier: Apache-2.0"]


def test_no_header_is_rejected():
    assert checker.REQUIRED_HEADER_TOKEN in checker.missing_header_parts('"""No header."""\n')


@pytest.mark.parametrize(
    "bad_copyright",
    [
        "# Copyright 2025 Agentics Transformation Ltd\n",  # wrong holder spelling
        "# Copyright 2024-2026 Agentics Transformation Limited\n",  # wrong year
    ],
)
def test_wrong_holder_or_year_is_rejected(bad_copyright):
    missing = checker.missing_header_parts(bad_copyright + SPDX_LINE + '\n"""M."""\n')
    assert checker.REQUIRED_HEADER_TOKEN in missing


def test_legacy_boilerplate_is_rejected():
    """The old 14-line block (2024-2026, Ltd) no longer satisfies the gate."""
    missing = checker.missing_header_parts(LEGACY_HEADER + '\n"""Module."""\n')
    assert checker.REQUIRED_HEADER_TOKEN in missing


@pytest.mark.parametrize(
    "spdx_line",
    [
        "# SPDX-License-Identifier: Apache-2.0 OR MIT\n",
        "# SPDX-License-Identifier: Apache-2.0-or-later\n",
    ],
)
def test_extra_license_term_is_rejected(spdx_line):
    """The SPDX line is matched whole, not as a prefix of a longer expression."""
    missing = checker.missing_header_parts(COPYRIGHT_LINE + spdx_line + '\n"""M."""\n')
    assert missing == ["SPDX-License-Identifier: Apache-2.0"]


def test_header_text_inside_a_docstring_is_rejected():
    """Only comment lines count: the same words in a docstring are no header."""
    text = (
        '"""Copyright 2025 Agentics Transformation Limited\n'
        'SPDX-License-Identifier: Apache-2.0\n"""\n'
    )
    assert len(checker.missing_header_parts(text)) == 2


def test_shebang_and_encoding_do_not_break_detection():
    text = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + FULL_HEADER + '\n"""M."""\n'
    assert checker.missing_header_parts(text) == []


def test_byte_order_mark_does_not_break_detection():
    assert checker.missing_header_parts("﻿" + FULL_HEADER + '\n"""M."""\n') == []


def test_reflowed_whitespace_is_tolerated():
    """Comparison collapses whitespace, so re-spacing the header is fine —
    only genuinely absent lines fail."""
    text = FULL_HEADER.replace("# SPDX-License-Identifier:", "#   SPDX-License-Identifier:")
    assert checker.missing_header_parts(text) == []


# ── The writer agrees with the checker, and is idempotent ──────────────────


def test_writer_output_satisfies_the_checker():
    """The two scripts must share one definition of a valid header —
    otherwise the checker's suggested remediation produces churn."""
    assert checker.missing_header_parts(FULL_HEADER + '\n"""M."""\n') == []


def test_writer_migrates_the_legacy_header(tmp_path):
    """The old 14-line block is replaced by exactly the two-line header, with
    the shebang, encoding line and code untouched; a second pass is a no-op."""
    target = tmp_path / "legacy.py"
    body = '"""Module."""\n\nCODE = 42\n'
    prefix = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n"
    target.write_text(prefix + LEGACY_HEADER + "\n" + body, encoding="utf-8")

    assert checker.missing_header_parts(target.read_text(encoding="utf-8"))
    assert writer.process_file(target, FULL_HEADER) == "replaced"
    migrated = target.read_text(encoding="utf-8")

    assert migrated == prefix + FULL_HEADER + "\n" + body
    assert checker.missing_header_parts(migrated) == []

    assert writer.process_file(target, FULL_HEADER) == "skipped"
    assert target.read_text(encoding="utf-8") == migrated


def test_writer_adds_the_header_to_a_bare_file(tmp_path):
    target = tmp_path / "bare.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    assert writer.process_file(target, FULL_HEADER) == "added"
    assert target.read_text(encoding="utf-8") == FULL_HEADER + "\nVALUE = 1\n"


@pytest.mark.parametrize("half", [COPYRIGHT_LINE, SPDX_LINE], ids=["copyright-only", "spdx-only"])
def test_writer_completes_a_half_header_in_one_pass(tmp_path, half):
    """Either line alone is taken as the header and replaced, not left under a
    second, stacked header that only a later run would fold away."""
    target = tmp_path / "half.py"
    target.write_text(half + "\nVALUE = 1\n", encoding="utf-8")

    assert writer.process_file(target, FULL_HEADER) == "replaced"
    assert target.read_text(encoding="utf-8") == FULL_HEADER + "\nVALUE = 1\n"
    assert writer.process_file(target, FULL_HEADER) == "skipped"


def test_writer_is_idempotent_on_a_compliant_file(tmp_path):
    """Running the writer twice must leave the second run byte-identical."""
    target = tmp_path / "sample.py"
    target.write_text(FULL_HEADER + '\n"""Module."""\n\nVALUE = 1\n', encoding="utf-8")

    first = writer.process_file(target, FULL_HEADER)
    after_first = target.read_text(encoding="utf-8")
    second = writer.process_file(target, FULL_HEADER)
    after_second = target.read_text(encoding="utf-8")

    assert first == "skipped", "a compliant file should not be rewritten"
    assert second == "skipped"
    assert after_first == after_second


def test_writer_preserves_shebang_and_code(tmp_path):
    target = tmp_path / "script.py"
    target.write_text("#!/usr/bin/env python3\n" + LEGACY_HEADER + "\nCODE = 42\n", "utf-8")

    writer.process_file(target, FULL_HEADER)
    result = target.read_text(encoding="utf-8")

    assert result.startswith("#!/usr/bin/env python3\n")
    assert "CODE = 42" in result
    assert checker.missing_header_parts(result) == []


# ── Whole-tree: the repo is compliant and the writer is a no-op on it ──────


@pytest.mark.slow
def test_writer_makes_no_changes_to_the_clean_tree(tmp_path):
    """The headline regression: on a compliant checkout the writer must
    change nothing, so the checker's remediation can never bury a
    contributor's diff under hundreds of unrelated files.

    Runs against a COPY so a failure can't mutate the working tree.
    """
    if not (REPO_ROOT / ".git").exists():  # pragma: no cover - source checkouts only
        pytest.skip("not a git checkout")

    sandbox = tmp_path / "repo"
    for name in ("fluid_build", "tests", "scripts", "tools"):
        source = REPO_ROOT / name
        if source.exists():
            shutil.copytree(source, sandbox / name, dirs_exist_ok=True)
    shutil.copy2(WRITER, sandbox / "scripts" / WRITER.name)

    before = {p: p.read_bytes() for p in sandbox.rglob("*.py")}
    subprocess.run(
        [sys.executable, str(sandbox / "scripts" / WRITER.name)],
        cwd=sandbox,
        capture_output=True,
        check=True,
    )
    changed = [str(p.relative_to(sandbox)) for p, data in before.items() if p.read_bytes() != data]

    assert changed == [], (
        f"add_license_headers.py rewrote {len(changed)} already-compliant file(s): "
        f"{changed[:5]}. The writer and check_license_headers.py have drifted apart again."
    )
