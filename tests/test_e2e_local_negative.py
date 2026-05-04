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

"""Negative-path CLI smoke tests against the local provider.

State-of-the-art integration tests prove the system fails *correctly*,
not just that it succeeds. These cover the failure modes that
CLI-dispatch refactors most often break:

* Malformed YAML — the parser must surface a useful error and exit non-zero
* Missing required schema fields — schema-validation must reject under-
  specified contracts rather than silently accept them
* Unsupported ``fluidVersion`` — graceful failure, not a stack trace
* Missing contract path — the CLI must reject before doing any work

All assertions are on the exit code being non-zero plus a loose substring
match on the error output. The substrings are deliberately permissive;
over-specifying them would couple the tests to the formatter and break on
every prose tweak.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]


def _fluid(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # See test_e2e_local._fluid for the rationale on PYTHONUTF8 +
    # PYTHONIOENCODING. No-op on Linux/macOS; required on Windows
    # until Trello card xsdOYJ6E is resolved.
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
        timeout=60,
    )


class TestFluidValidateRejectsMalformedContracts:
    """``fluid validate`` exits non-zero on contracts that should not pass."""

    def test_malformed_yaml_exits_nonzero(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("this is: not valid: yaml: at: all:\n")
        result = _fluid("validate", str(contract), cwd=tmp_path)
        assert (
            result.returncode != 0
        ), f"validate accepted broken YAML; output: {result.stdout}{result.stderr}"

    def test_missing_required_fields_exits_nonzero(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text('fluidVersion: "0.7.2"\nkind: "DataProduct"\n')
        result = _fluid("validate", str(contract), cwd=tmp_path)
        assert result.returncode != 0, (
            f"validate accepted contract without required fields; "
            f"output: {result.stdout}{result.stderr}"
        )
        # The error must say what's missing — a refactor that loses this
        # signal is the regression we're guarding against.
        combined = (result.stdout + result.stderr).lower()
        assert "required" in combined or "missing" in combined, (
            f"validate failed but the error did not mention required/missing; "
            f"output: {result.stdout}{result.stderr}"
        )

    def test_unsupported_schema_version_exits_nonzero(self, tmp_path: Path) -> None:
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text(
            'fluidVersion: "9.99.99"\n'
            'kind: "DataProduct"\n'
            'id: "x.y"\n'
            'name: "x"\n'
            'domain: "x"\n'
        )
        result = _fluid("validate", str(contract), cwd=tmp_path)
        assert result.returncode != 0, (
            f"validate accepted unsupported fluidVersion; "
            f"output: {result.stdout}{result.stderr}"
        )


class TestFluidApplyRejectsBadInputs:
    """``fluid apply`` rejects inputs before doing any provider work."""

    def test_apply_nonexistent_contract_exits_nonzero(self, tmp_path: Path) -> None:
        result = _fluid(
            "--provider",
            "local",
            "apply",
            str(tmp_path / "does-not-exist.yaml"),
            "--yes",
            cwd=tmp_path,
        )
        assert (
            result.returncode != 0
        ), f"apply accepted missing contract; output: {result.stdout}{result.stderr}"
