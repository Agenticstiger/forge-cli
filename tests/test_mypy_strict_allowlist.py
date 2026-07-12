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

"""Pin the incremental ``mypy --strict`` hotspot allowlist.

The strict allowlist is declared in exactly one place — the
``[[tool.mypy.overrides]] strict = true`` block in ``pyproject.toml`` — and the
``typecheck-strict`` CI job derives its target file list from there (via
``tomllib``). These tests guard that single source of truth: every listed
module must resolve to a real source file, and the CI job must keep deriving
its list from pyproject rather than hand-maintaining a second copy that could
silently drift.
"""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 — mypy pulls ``tomli`` as a dependency
    import tomli as tomllib

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _strict_allowlist_modules() -> list[str]:
    """Return the dotted module names in the strict override block."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = data["tool"]["mypy"].get("overrides", [])
    modules: list[str] = []
    for override in overrides:
        if override.get("strict") is True:
            mod = override["module"]
            modules.extend(mod if isinstance(mod, list) else [mod])
    return modules


@pytest.mark.unit
def test_strict_allowlist_is_nonempty() -> None:
    assert _strict_allowlist_modules(), (
        "Expected at least one module in the [[tool.mypy.overrides]] strict "
        "block in pyproject.toml"
    )


@pytest.mark.unit
def test_every_strict_module_resolves_to_a_source_file() -> None:
    missing = []
    for module in _strict_allowlist_modules():
        path = REPO_ROOT / (module.replace(".", "/") + ".py")
        if not path.is_file():
            missing.append(f"{module} -> {path.relative_to(REPO_ROOT)}")
    assert not missing, (
        "Strict-allowlist modules with no matching source file (fix the module "
        "name in pyproject.toml or drop the stale entry):\n  " + "\n  ".join(missing)
    )


@pytest.mark.unit
def test_shared_strict_runner_derives_from_pyproject() -> None:
    """``scripts/mypy_strict.py`` — the one runner both CI and the Makefile
    call — must derive its file list from pyproject (via ``tomllib``) rather
    than duplicating the allowlist, and must scope the run with
    ``--follow-imports=silent`` so it can never fail on the rest of the tree."""
    runner = REPO_ROOT / "scripts" / "mypy_strict.py"
    assert runner.is_file(), "missing the shared strict runner scripts/mypy_strict.py"
    src = runner.read_text(encoding="utf-8")
    assert "pyproject.toml" in src and "tomllib" in src, (
        "the strict runner should derive its module list from pyproject.toml "
        "(tomllib), not a duplicated hardcoded list"
    )
    assert "--strict" in src and "--follow-imports=silent" in src, (
        "the strict runner must run mypy --strict --follow-imports=silent so it "
        "only fails on the allowlist, never the rest of the tree"
    )


@pytest.mark.unit
def test_ci_and_makefile_use_the_shared_strict_runner() -> None:
    """Both the CI gate and the local Makefile target must invoke the shared
    runner, so there is a single implementation and no drift."""
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "typecheck-strict:" in ci, "missing the typecheck-strict CI job"
    assert "scripts/mypy_strict.py" in ci, "CI job must call the shared strict runner"

    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "typecheck-strict:" in makefile, "missing the typecheck-strict Makefile target"
    assert "scripts/mypy_strict.py" in makefile, "Makefile target must call the shared runner"
