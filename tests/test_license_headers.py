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

"""Pins for the license-header checker + writer staying in agreement.

The two scripts previously disagreed about what "has a header" means:
``check_license_headers.py`` only grepped the copyright line, so 346 files
whose Apache boilerplate had been truncated mid-block (323 cut after the
LICENSE-2.0 URL, 23 cut right after "you may not use this file…") passed
the gate — while ``add_license_headers.py``, which writes the complete
block, rewrote every one of them on any run. Following the checker's own
"Run: python scripts/add_license_headers.py" remediation therefore turned
a one-file fix into a 361-file diff.

These tests pin both halves of the fix: the checker validates the whole
boilerplate, and the writer is a no-op on an already-compliant tree.
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

TRUNCATED_AT_URL = (
    "# Copyright 2024-2026 Agentics Transformation Ltd\n"
    "#\n"
    '# Licensed under the Apache License, Version 2.0 (the "License");\n'
    "# you may not use this file except in compliance with the License.\n"
    "# You may obtain a copy of the License at\n"
    "#\n"
    "#     http://www.apache.org/licenses/LICENSE-2.0\n"
)

TRUNCATED_EARLY = (
    "# Copyright 2024-2026 Agentics Transformation Ltd\n"
    "#\n"
    '# Licensed under the Apache License, Version 2.0 (the "License");\n'
    "# you may not use this file except in compliance with the License.\n"
)


# ── The checker validates the WHOLE boilerplate ────────────────────────────


def test_complete_header_passes():
    assert checker.missing_header_parts(FULL_HEADER + '\n"""Module."""\n') == []


@pytest.mark.parametrize(
    "header,label",
    [(TRUNCATED_AT_URL, "cut after the LICENSE-2.0 URL"), (TRUNCATED_EARLY, "cut mid-grant")],
)
def test_truncated_header_is_rejected(header, label):
    """Both real-world truncation shapes must fail — they passed the old
    copyright-token-only check, which is how 346 files drifted."""
    missing = checker.missing_header_parts(header + '\n"""Module."""\n')
    assert missing, f"truncated header ({label}) slipped through the gate"
    assert "limitations under the License." in missing


def test_missing_copyright_is_rejected():
    assert checker.REQUIRED_HEADER_TOKEN in checker.missing_header_parts('"""No header."""\n')


def test_shebang_and_encoding_do_not_break_detection():
    text = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + FULL_HEADER + '\n"""M."""\n'
    assert checker.missing_header_parts(text) == []


def test_reflowed_whitespace_is_tolerated():
    """Comparison collapses whitespace, so re-indenting the block is fine —
    only genuinely absent clauses fail."""
    text = FULL_HEADER.replace("#     http://", "#   http://")
    assert checker.missing_header_parts(text) == []


# ── The writer agrees with the checker, and is idempotent ──────────────────


def test_writer_output_satisfies_the_checker():
    """The two scripts must share one definition of a valid header —
    otherwise the checker's suggested remediation produces churn."""
    assert checker.missing_header_parts(FULL_HEADER + '\n"""M."""\n') == []


def test_writer_completes_a_truncated_header():
    content = TRUNCATED_AT_URL + '\n"""Module."""\n'
    fixed = writer.add_header(writer.remove_header(content), FULL_HEADER)
    assert checker.missing_header_parts(fixed) == []
    assert '"""Module."""' in fixed


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
    target.write_text("#!/usr/bin/env python3\n" + TRUNCATED_AT_URL + "\nCODE = 42\n", "utf-8")

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
