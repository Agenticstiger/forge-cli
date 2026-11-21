# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The profiler's import boundary (flux-engine ADR 0003, founder decision 11).

``fluid profile`` depends on FLUX's statistic registry alone, through one seam,
and never on the engine. ``[tool.importlinter]`` runs with
``include_external_packages = false``, so it cannot forbid an outside package;
this test reads the imports itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import fluid_build.profiler as profiler

pytestmark = pytest.mark.unit

PACKAGE = Path(profiler.__file__).parent
MODULES = sorted(PACKAGE.rglob("*.py")) + [
    Path(profiler.__file__).parents[1] / "cli" / "profile_cmd.py"
]


def _imports(path: Path):
    """(top-level package, imported at module scope) for every import in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], id(node) in top, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".")[0], id(node) in top, node.module


def test_the_modules_are_found():
    names = {path.name for path in MODULES}
    assert {"_stats.py", "disclosure.py", "document.py", "runner.py", "sources.py"} <= names
    assert {"randomness.py", "discrete_gaussian.py"} <= names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_nothing_imports_the_flux_engine(path):
    assert not [name for top, _, name in _imports(path) if top == "flux_engine"]


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_only_the_seam_imports_the_statistic_registry(path):
    if path.name == "_stats.py":
        return
    assert not [name for top, _, name in _imports(path) if top == "flux_stats"]


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_heavy_package_is_imported_at_module_scope(path):
    heavy = {
        "duckdb",
        "rfc8785",
        "flux_stats",
        "jsonschema",
        "pydantic",
        "httpx",
        "opendp",
        "mbi",
        "polars",
        "jax",
        "numpy",
    }
    assert not [name for top, scoped, name in _imports(path) if scoped and top in heavy]
