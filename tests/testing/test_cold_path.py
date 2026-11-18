# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The test kit stays off every path a user's command takes.

``fluid_build.testing`` ships in the wheel, so the guard is that nothing imports it, and
that it imports nothing a bare install lacks.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator, List, Set, Tuple

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = ROOT / "fluid_build"
KIT = PRODUCT / "testing"
KIT_PREFIX = "fluid_build.testing"

_PROBE = """
import sys
{preimport}
from fluid_build.cli import main
try:
    main({argv!r})
except SystemExit:
    pass
kit = sorted(m for m in sys.modules if m == {prefix!r} or m.startswith({prefix!r} + "."))
print("KIT_MODULES=" + ",".join(kit))
"""


def _probe(tmp_path: Path, argv: List[str], preimport: str = "") -> List[str]:
    """The kit modules loaded after ``fluid <argv>`` ran in a fresh interpreter."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT")
        if key in os.environ
    }
    env.update(
        {
            "PYTHONPATH": str(ROOT),  # this tree, whatever the venv's editable install points at
            "PYTHONNOUSERSITE": "1",
            "HOME": str(tmp_path),
            "FLUID_NO_TELEMETRY": "1",
            "NO_COLOR": "1",
        }
    )
    code = _PROBE.format(preimport=preimport, argv=argv, prefix=KIT_PREFIX)
    done = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-600:]
    last = done.stdout.strip().splitlines()[-1]
    assert last.startswith("KIT_MODULES="), done.stdout[-600:]
    return [m for m in last.partition("=")[2].split(",") if m]


@pytest.mark.parametrize("argv", [["--help"], ["providers"], ["apply", "--help"]])
def test_the_commands_users_run_never_load_the_kit(tmp_path, argv):
    assert _probe(tmp_path, argv) == []


def test_the_probe_would_see_the_kit_if_something_loaded_it(tmp_path):
    loaded = _probe(tmp_path, ["--help"], preimport="import fluid_build.testing.cli")
    assert loaded == ["fluid_build.testing", "fluid_build.testing.cli"]


# ── static checks ────────────────────────────────────────────────────────────


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _absolute_base(node: ast.ImportFrom, package: Tuple[str, ...]) -> List[str]:
    """The module an ``ImportFrom`` reads from, as dotted parts, a relative one resolved."""
    base: List[str] = []
    if node.level:
        base = list(package[: max(0, len(package) - (node.level - 1))])
    return base + (node.module.split(".") if node.module else [])


def _imported_modules(path: Path, tree: ast.Module) -> Iterator[str]:
    """Every absolute module name ``tree`` imports, relative imports resolved."""
    package = path.relative_to(ROOT).with_suffix("").parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_base(node, package)
            yield ".".join(base)
            yield from (
                ".".join([*base, alias.name]) for alias in node.names
            )  # ``from pkg import mod``


def test_nothing_in_the_product_imports_the_kit():
    offenders = []
    for path in sorted(PRODUCT.rglob("*.py")):
        if KIT in path.parents:
            continue
        for module in _imported_modules(path, _parse(path)):
            if module == KIT_PREFIX or module.startswith(KIT_PREFIX + "."):
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert offenders == []


class _ModuleLevelImports(ast.NodeVisitor):
    """The imports that run when a module loads: all but those inside a function."""

    def __init__(self) -> None:
        self.names: Set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        self.names.update(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.names.add(("." * node.level) + (node.module or ""))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    visit_AsyncFunctionDef = visit_FunctionDef


@pytest.mark.parametrize("path", sorted(KIT.glob("*.py")), ids=lambda p: p.name)
def test_a_kit_module_loads_with_the_standard_library_alone(path):
    tree = _parse(path)
    loads = _ModuleLevelImports()
    loads.visit(tree)
    outside = {
        name
        for name in loads.names
        if name.split(".")[0] not in sys.stdlib_module_names and not name.startswith(KIT_PREFIX)
    }
    assert outside == set()
    pytest_imports = {module.split(".")[0] for module in _imported_modules(path, tree)} & {
        "pytest",
        "_pytest",
    }
    assert pytest_imports == set()  # not even inside a function


def test_the_package_initialiser_imports_nothing():
    tree = _parse(KIT / "__init__.py")
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
