# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The bundled quickstart template lands real rows with ``fluid apply`` (local engine).

``fluid init <dir> --quickstart --yes && fluid apply contract.fluid.yaml --yes`` used to
report success while writing 24-byte parquet files holding a placeholder row, because the
template's single ``multi-stage`` build carried no ``properties.sql`` the local engine runs.
The local engine now runs the stages of a ``multi-stage`` build, so the template is that
build again.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

pytestmark = [pytest.mark.integration]

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _fluid(*args: str, cwd: Path, timeout: int = 180) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH")]))
    env["COLUMNS"] = "200"
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
def scaffold(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    res = _fluid("init", "qs", "--quickstart", "--yes", cwd=scratch)
    assert res.returncode == 0, res.stdout + res.stderr
    project = scratch / "qs"
    assert (project / "contract.fluid.yaml").is_file()
    return project


def _count(project: Path, source: str) -> int:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
    finally:
        con.close()


def _customer_count(project: Path) -> int:
    csv = (project / "data" / "customers.csv").as_posix()
    return _count(project, f"read_csv_auto('{csv}', header=true)")


def _ids(path: Path) -> set[int]:
    con = duckdb.connect()
    try:
        return {
            r[0] for r in con.execute(f"SELECT customer_id FROM '{path.as_posix()}'").fetchall()
        }
    finally:
        con.close()


def _assert_no_placeholder(path: Path) -> None:
    con = duckdb.connect()
    try:
        rows = con.execute(f"SELECT * FROM '{path.as_posix()}'").fetchall()
    finally:
        con.close()
    for row in rows:
        assert "materialized" not in [str(v) for v in row], f"placeholder row in {path.name}"


def _assert_real_outputs(project: Path) -> None:
    master = project / "output" / "customer_360.parquet"
    high_value = project / "output" / "high_value_customers.parquet"
    assert master.is_file(), "customer_360.parquet was not written"
    assert high_value.is_file(), "high_value_customers.parquet was not written"

    expected = _customer_count(project)
    assert expected > 0
    master_ids = _ids(master)
    assert _count(project, f"'{master.as_posix()}'") == expected
    assert len(master_ids) == expected, "customer_360_master has one row per customer"
    # The SQL measures recency against the newest order in the sample data, not today's date,
    # so the VIP segment is the same on any day.
    high_value_ids = _ids(high_value)
    assert high_value_ids, "high_value_customers landed no rows"
    assert high_value_ids <= master_ids

    _assert_no_placeholder(master)
    _assert_no_placeholder(high_value)


def test_scaffold_validates(scaffold: Path) -> None:
    res = _fluid("validate", "contract.fluid.yaml", cwd=scaffold)
    assert res.returncode == 0, res.stdout + res.stderr


def test_default_apply_lands_real_rows(scaffold: Path) -> None:
    res = _fluid("apply", "contract.fluid.yaml", "--yes", cwd=scaffold)
    assert res.returncode == 0, res.stdout + res.stderr
    _assert_real_outputs(scaffold)


def test_amend_and_build_lands_real_rows(scaffold: Path) -> None:
    res = _fluid("apply", "contract.fluid.yaml", "--yes", "--mode", "amend-and-build", cwd=scaffold)
    assert res.returncode == 0, res.stdout + res.stderr
    _assert_real_outputs(scaffold)
