# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The package costs nothing until it is used: no sqlglot and no DuckDB on import."""

from __future__ import annotations

import subprocess
import sys

import pytest

import fluid_build.profiler.consumer_queries as package


def test_importing_the_package_loads_neither_sqlglot_nor_duckdb() -> None:
    code = (
        "import sys; import fluid_build.profiler.consumer_queries as c; "
        "print(sorted(m for m in ('sqlglot', 'duckdb', 'pyarrow') if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_the_parent_never_imports_duckdb_even_after_a_run() -> None:
    pytest.importorskip("duckdb")
    code = (
        "import sys; from fluid_build.profiler.consumer_queries import run_in_child, Statement; "
        "r = run_in_child({'t': [('a', 'BIGINT', [1])]}, [Statement('s', 'SELECT count(*) FROM t', "
        "'count')], seconds=10, memory=256 << 20); "
        "print(r.results[0].count, 'duckdb' in sys.modules, 'sqlglot' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["1", "False", "False"]


def test_every_name_in_all_exists() -> None:
    for name in package.__all__:
        assert hasattr(package, name)


def test_the_two_parts_are_listed() -> None:
    assert package.check_text and package.run_in_child
    assert "unparsable" in package.REFUSAL_CODES and "crashed" in package.STATUSES


def test_a_new_module_of_the_package_has_the_attribution_line() -> None:
    from pathlib import Path

    root = Path(package.__file__).parent
    for name in ("_allow", "_bounds", "_order", "check", "_child", "runner"):
        head = (root / f"{name}.py").read_text(encoding="utf-8").splitlines()[:4]
        assert any(
            line.startswith("# Adapted from FLUX flux_engine/consumers/") and "Apache-2.0" in line
            for line in head
        ), name
