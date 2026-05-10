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

"""End-to-end ``fluid apply`` tests with real DuckDB-based output verification.

Closes the largest gap left by ``test_e2e_local.py``: that file exercises
``init``, ``validate``, and ``plan`` but stops short of executing
``apply``. The "60 Seconds to Magic" promise — a fresh contract going
from YAML to materialised data — is unverified without these tests.

Each test runs ``fluid apply`` against the bundled hello-world example,
then opens the produced output through the ``duckdb`` Python module to
assert schema and rows. Reading the CSV back through DuckDB rather than
the stdlib ``csv`` module is deliberate: it round-trips through the
same engine the local provider uses, so a regression that produces CSV
which DuckDB itself cannot read is caught.

Catches the ``unknown_action_op`` shape of regression documented in
CLAUDE.md, where apply reports SUCCESS but accomplishes nothing — the
exit code is 0 but the output file is missing or empty.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]


def _have_duckdb() -> bool:
    try:
        import duckdb  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark.append(pytest.mark.skipif(not _have_duckdb(), reason="duckdb not installed"))


def _fluid(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Invoke the ``fluid`` CLI as a subprocess against ``cwd``.

    ``encoding='utf-8', errors='replace'`` is set on the parent side so
    decoding the captured streams never raises on Windows where the
    default locale is cp1252. ``PYTHONIOENCODING=utf-8`` is forced on
    the child for the same reason."""
    env = os.environ.copy()
    # See test_e2e_local._fluid for the rationale on PYTHONUTF8 +
    # PYTHONIOENCODING. No-op on Linux/macOS; required on Windows
    # until Trello card xsdOYJ6E is resolved in the fluid CLI itself.
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


@pytest.fixture
def hello_world_workspace(tmp_path: Path) -> Path:
    """Workspace with the bundled hello-world contract copied in."""
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "examples" / "01-hello-world" / "contract.fluid.yaml"
    if not src.exists():
        pytest.skip(f"hello-world example not found at {src}")
    dst = tmp_path / "contract.fluid.yaml"
    shutil.copyfile(src, dst)
    return tmp_path


class TestFluidApplyMaterializesData:
    """``fluid apply`` actually materialises data; it doesn't just plan it.

    The expensive insight here over ``test_e2e_local.py``: we observe
    the on-disk side effect rather than just the CLI exit code. If the
    apply pipeline ever silently no-ops (the ``unknown_action_op``
    shape of regression), this is the test that catches it."""

    def test_apply_produces_materialized_csv(self, hello_world_workspace: Path) -> None:
        result = _fluid(
            "--provider",
            "local",
            "apply",
            str(hello_world_workspace / "contract.fluid.yaml"),
            "--yes",
            cwd=hello_world_workspace,
        )
        assert (
            result.returncode == 0
        ), f"apply exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"

        # The hello-world contract binds its 'hello_output' expose to
        # runtime/out/hello-world-v1.csv. If the materialisation step
        # silently skipped, this file is missing.
        output_csv = hello_world_workspace / "runtime" / "out" / "hello-world-v1.csv"
        assert (
            output_csv.exists()
        ), f"apply claimed success but did not produce {output_csv}\nstdout: {result.stdout}"
        assert output_csv.stat().st_size > 0, f"{output_csv} exists but is empty"

    def test_apply_output_has_expected_schema_and_rows(self, hello_world_workspace: Path) -> None:
        """Read the produced CSV through DuckDB, assert schema + rows.

        Using the DuckDB Python API rather than the stdlib csv module
        is deliberate: it is the same engine the provider uses, so a
        regression in the writer that emits CSV unreadable by DuckDB
        is caught here rather than at user-runtime."""
        import duckdb

        result = _fluid(
            "--provider",
            "local",
            "apply",
            str(hello_world_workspace / "contract.fluid.yaml"),
            "--yes",
            cwd=hello_world_workspace,
        )
        assert result.returncode == 0, result.stderr

        output_csv = hello_world_workspace / "runtime" / "out" / "hello-world-v1.csv"

        # ``all_varchar=True`` reads every column as VARCHAR so DuckDB
        # never tries to materialise the TIMESTAMPTZ ``created_at``
        # column. Without this, ``fetchall`` raises if the runtime is
        # missing ``pytz`` (DuckDB requires it for tz-aware timestamps,
        # and the [local] extra does not pull it in). Reading as text
        # is sufficient for the schema + row count + literal-value
        # assertions below.
        rel = duckdb.read_csv(str(output_csv), all_varchar=True)
        rows = rel.fetchall()
        column_names = list(rel.columns)

        # Hello-world contract emits exactly two columns and one row.
        assert "message" in column_names, f"expected 'message' column; got {column_names}"
        assert "created_at" in column_names, f"expected 'created_at' column; got {column_names}"
        assert len(rows) == 1, f"expected exactly 1 row; got {len(rows)}"

        # The literal value is hardcoded in the contract's SQL and is
        # the most readable signal that the SQL ran end-to-end.
        message_idx = column_names.index("message")
        assert (
            rows[0][message_idx] == "Hello, FLUID!"
        ), f"expected 'Hello, FLUID!'; got {rows[0][message_idx]!r}"


class TestFluidApplyIdempotency:
    """Re-running ``fluid apply`` against the same contract is safe.

    The plan documents apply-twice-is-no-op as a target invariant. The
    actual semantic today is weaker (apply succeeds on both runs and the
    output row count stays consistent), so the assertions here are on
    the consistency rather than on the strict no-op claim. If the
    no-op invariant is hardened later, tighten these assertions too."""

    def test_two_apply_runs_both_succeed(self, hello_world_workspace: Path) -> None:
        for run in range(2):
            result = _fluid(
                "--provider",
                "local",
                "apply",
                str(hello_world_workspace / "contract.fluid.yaml"),
                "--yes",
                cwd=hello_world_workspace,
            )
            assert (
                result.returncode == 0
            ), f"apply run {run + 1} exited {result.returncode}\nstderr: {result.stderr}"

    def test_two_apply_runs_produce_consistent_row_count(self, hello_world_workspace: Path) -> None:
        """Two apply runs must leave the output with the same row count.

        Stricter byte-for-byte equality would fail for the legitimate
        reason that the hello-world contract uses CURRENT_TIMESTAMP, so
        the timestamp column changes between runs. Row count is the
        meaningful invariant."""
        import duckdb

        for _ in range(2):
            result = _fluid(
                "--provider",
                "local",
                "apply",
                str(hello_world_workspace / "contract.fluid.yaml"),
                "--yes",
                cwd=hello_world_workspace,
            )
            assert result.returncode == 0, result.stderr

        output_csv = hello_world_workspace / "runtime" / "out" / "hello-world-v1.csv"
        # ``all_varchar=True`` for the same reason as the schema test —
        # avoids materialising TIMESTAMPTZ which needs pytz at runtime.
        row_count = duckdb.read_csv(str(output_csv), all_varchar=True).count("*").fetchone()[0]
        assert row_count == 1, f"expected 1 row after two applies; got {row_count}"


class TestFluidApplyArtifacts:
    """Apply produces the documented side-artifacts (HTML report, log)."""

    def test_apply_emits_html_report(self, hello_world_workspace: Path) -> None:
        result = _fluid(
            "--provider",
            "local",
            "apply",
            str(hello_world_workspace / "contract.fluid.yaml"),
            "--yes",
            cwd=hello_world_workspace,
        )
        assert result.returncode == 0, result.stderr

        report = hello_world_workspace / "runtime" / "apply_report.html"
        assert report.exists(), f"expected {report} to be generated"
        assert report.stat().st_size > 0, f"{report} exists but is empty"
