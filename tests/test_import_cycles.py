# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every ``fluid_build`` module imports when it is the first one loaded.

A parent module that re-exports a helper's names at module level, while the
helper imports the parent (or copies names from it) at import time, works when
the parent loads first and fails when the helper does::

    ImportError: cannot import name 'X' from partially initialized module ...

Thirteen modules had this until 2026-11. The CLI never showed it, because a
command always loads the parent first, but a test or a tool that imported one
of those helpers directly passed or failed depending on what an earlier import
had already loaded.

A static import graph cannot see this: the order of execution decides it. So,
like SciPy's ``scipy/_lib/tests/test_import_cycles.py`` (BSD-3-Clause), each
module is imported in its own fresh interpreter, a few at a time.

Only a circular-import failure fails this test. A module that needs an optional
extra this environment lacks stops at ``ModuleNotFoundError`` first, and that
is another test's concern.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _REPO_ROOT / "fluid_build"
# SciPy caps its pool at 6 to bound memory and open files (scipy gh-24163).
_WORKERS = min(6, os.cpu_count() or 2)
_TIMEOUT_S = 120
# CPython's wording for both the ImportError and the AttributeError a
# half-built module raises.
_CYCLE_MARKER = "partially initialized module"

_FIX = (
    "Fix: the helper must not need its parent before its own names exist. "
    "Import the parent at the end of the helper, or inside the function that "
    "uses it; move a name-binding call below the helper's definitions; or, when "
    "the helper subclasses a class of the parent, have the parent re-export the "
    "helper's names lazily (PEP 562 __getattr__, as fluid_build/cli/auth.py does)."
)


def _importable_modules() -> List[str]:
    """Dotted names of the modules in regular packages under ``fluid_build/``.

    Read from the file tree, so nothing is imported into this test process.
    ``__main__`` modules run the CLI, so they are left out.
    """
    names = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
        package_dirs = (_REPO_ROOT.joinpath(*parts[:i]) for i in range(1, len(parts)))
        if not all((d / "__init__.py").is_file() for d in package_dirs):
            continue  # a template or data directory, not a package
        if parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return names


def _import_alone(module: str, cwd: Path) -> Tuple[str, int, str]:
    env = os.environ.copy()
    # This tree first, so a worktree is tested rather than wherever the
    # venv's editable install points.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH")]))
    try:
        done = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return module, -1, f"no result after {_TIMEOUT_S}s"
    return module, done.returncode, done.stderr


def _report(cycles: Dict[str, str], hung: List[str]) -> str:
    lines = [f"{len(cycles) + len(hung)} module(s) fail when imported first:"]
    for module, stderr in sorted(cycles.items()):
        errors = [line.strip() for line in stderr.splitlines() if _CYCLE_MARKER in line]
        lines.append(f"  python -c 'import {module}'\n    {errors[-1]}")
    lines += [
        f"  python -c 'import {module}'\n    hung: no result after {_TIMEOUT_S}s" for module in hung
    ]
    lines.append(_FIX)
    return "\n".join(lines)


def test_the_walk_finds_the_package_tree() -> None:
    modules = _importable_modules()
    assert "fluid_build" in modules
    assert "fluid_build.cli._init_dag_helpers" in modules
    assert not any(name.endswith("__main__") for name in modules)
    assert len(modules) > 500, len(modules)


def test_every_module_imports_when_it_is_loaded_first(tmp_path: Path) -> None:
    modules = _importable_modules()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        results = list(pool.map(lambda name: _import_alone(name, tmp_path), modules))

    cycles = {name: err for name, rc, err in results if rc not in (0, -1) and _CYCLE_MARKER in err}
    hung = [name for name, rc, _err in results if rc == -1]
    assert not cycles and not hung, _report(cycles, hung)
