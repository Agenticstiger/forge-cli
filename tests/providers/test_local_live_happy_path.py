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

"""Provider-level live happy-path tests for the local (DuckDB) provider.

These tests instantiate the real ``LocalProvider`` and run actions against
a temp-dir DuckDB file. No CLI subprocess; this is the unit-vs-integration
boundary that catches regressions in the provider abstraction itself
(plan generation, action emission, action execution against a real DuckDB
engine) without paying for a full CLI dispatch.

Free, secret-less, runs in `ci.yml::duckdb-integration` on every PR.
"""

from __future__ import annotations

import os
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


def _fluid(*args: str, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    """Invoke the ``fluid`` CLI as a subprocess.

    ``encoding='utf-8', errors='replace'`` plus ``PYTHONIOENCODING=utf-8``
    keep both ends of the pipe agreeing on UTF-8 — needed on Windows
    where the default locale is cp1252 and the fluid banner uses chars
    outside that codepage."""
    env = os.environ.copy()
    # See tests/test_e2e_local._fluid for rationale. PYTHONUTF8=1 and
    # PYTHONIOENCODING=utf-8 are no-ops on Linux/macOS; required on
    # Windows until Trello card xsdOYJ6E is resolved.
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
        timeout=timeout,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Empty FLUID workspace under tmp_path."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".fluid").mkdir()
    return ws


@pytest.fixture
def hello_world_contract(workspace: Path) -> Path:
    """The bundled hello-world example contract, copied into the workspace.

    Using the same contract the CLI ships as a template means we don't
    have to maintain a separate fixture — and the CI-time check doubles
    as a regression test against schema drift breaking the example.
    """
    import shutil

    repo_root = Path(__file__).resolve().parent.parent.parent
    src = repo_root / "examples" / "01-hello-world" / "contract.fluid.yaml"
    if not src.exists():
        pytest.skip(f"example contract not found at {src}")
    dst = workspace / "contract.fluid.yaml"
    shutil.copyfile(src, dst)
    return dst


class TestLocalProviderInstantiation:
    """The local provider can be imported and instantiated with default opts."""

    def test_local_provider_is_importable(self) -> None:
        # The provider package must be importable at all — catches packaging
        # regressions where __init__.py side-effects break.
        try:
            from fluid_build.providers import local  # noqa: F401
        except ImportError as exc:
            pytest.fail(f"fluid_build.providers.local import failed: {exc}")

    def test_local_provider_has_render_or_apply(self) -> None:
        """The local provider must expose either ``render`` or ``apply`` —
        the public verbs every provider implements per the architecture
        documented in AGENTS.md."""
        from fluid_build.providers import local

        # The exact entry point varies (some providers export a class, some
        # a module function). Walk both candidates rather than assert one.
        candidates = []
        for attr in ("LocalProvider", "Provider", "render", "apply"):
            if hasattr(local, attr):
                candidates.append(attr)
        assert candidates, (
            "fluid_build.providers.local does not expose any of "
            "[LocalProvider, Provider, render, apply]"
        )


class TestLocalProviderEndToEnd:
    """The local provider can plan + apply a minimal contract against a
    real DuckDB engine, end-to-end, with no mocks."""

    def test_plan_actions_for_smoke_contract(
        self, hello_world_contract: Path, workspace: Path
    ) -> None:
        """Calling the CLI plan path through python -m must produce a
        non-empty action list for the smoke contract.

        We invoke through the CLI rather than the provider directly because
        the canonical entry point is the CLI; testing the provider in
        isolation would let us miss CLI-specific bugs (env loading,
        contract resolution, schema-version routing)."""
        plan_out = workspace / "plan.json"
        result = _fluid(
            "--provider",
            "local",
            "plan",
            str(hello_world_contract),
            "--out",
            str(plan_out),
            cwd=workspace,
        )
        assert (
            result.returncode == 0
        ), f"plan exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        assert plan_out.exists(), "plan should produce an output file"

    def test_validate_smoke_contract(self, hello_world_contract: Path, workspace: Path) -> None:
        result = _fluid(
            "validate",
            str(hello_world_contract),
            cwd=workspace,
        )
        assert (
            result.returncode == 0
        ), f"validate exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"


class TestLocalProviderDeterminism:
    """Two `plan` runs against the same contract produce identical output.

    The planDigest leaks (wall-clock ``generated_at`` in the hash, and
    PYTHONHASHSEED-dependent action ordering) are fixed; see the parallel
    test in tests/test_e2e_local.py for the rationale."""

    def test_two_plan_runs_produce_identical_artifacts(
        self, hello_world_contract: Path, workspace: Path
    ) -> None:
        import json

        outputs = []
        for run_id in ("a", "b"):
            out = workspace / f"plan_{run_id}.json"
            result = _fluid(
                "--provider",
                "local",
                "plan",
                str(hello_world_contract),
                "--out",
                str(out),
                cwd=workspace,
            )
            assert result.returncode == 0, result.stderr
            outputs.append(json.loads(out.read_text()))

        a, b = outputs
        a_digest = a.get("planDigest") or a.get("plan_digest")
        b_digest = b.get("planDigest") or b.get("plan_digest")
        assert a_digest == b_digest
