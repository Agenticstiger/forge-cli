# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every bundled template runs with ``fluid apply`` on the local (DuckDB) engine.

``fluid init <dir> --template <name> --yes && fluid apply contract.fluid.yaml --yes``
must exit 0 and land real rows in every expose: a placeholder file (``id,value /
1,materialized``) or a skipped build is a defect. Eight templates are
``pattern: multi-stage`` builds whose SQL lives in ``properties.stages[]``; the
local engine runs those stages.

A template that cannot run locally is listed in ``CANNOT_RUN_LOCALLY`` with the
reason, and the list is itself asserted: every entry must really fail, so it
cannot rot. Nothing is skipped silently.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

import fluid_build

duckdb = pytest.importorskip("duckdb")

pytestmark = [pytest.mark.integration]

_REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = Path(fluid_build.__file__).parent / "templates"

TEMPLATES = sorted(p.parent.name for p in TEMPLATES_DIR.glob("*/contract.fluid.yaml"))

#: ``{template name: why it cannot run on the local engine}``. Every entry is asserted
#: to fail (``test_every_listed_template_really_cannot_run_locally``). Empty: every
#: bundled template runs locally.
CANNOT_RUN_LOCALLY: Dict[str, str] = {}

#: The placeholder value the local engine used to write for rows it did not have.
PLACEHOLDER = "materialized"


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


def _scaffold(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    res = _fluid("init", "proj", "--template", name, "--yes", cwd=scratch)
    assert res.returncode == 0, res.stdout + res.stderr
    project = scratch / "proj"
    assert (project / "contract.fluid.yaml").is_file()
    return project


def _expose_files(project: Path) -> List[Path]:
    """The local file each expose of the scaffolded contract lands in."""
    from fluid_build.util.binding_paths import local_provider_landing

    contract = yaml.safe_load((project / "contract.fluid.yaml").read_text("utf-8"))
    files: List[Path] = []
    for expose in contract.get("exposes") or []:
        binding: Dict[str, Any] = expose.get("binding") or {}
        path = (binding.get("location") or {}).get("path")
        assert path, f"expose {expose.get('exposeId')} declares no location.path"
        landed, _ = local_provider_landing(str(path), binding.get("format"))
        files.append(project / landed)
    assert files, "the template declares no expose"
    return files


def _rows(path: Path) -> List[tuple]:
    con = duckdb.connect()
    try:
        return con.execute(f"SELECT * FROM '{path.as_posix()}'").fetchall()
    finally:
        con.close()


def test_the_template_list_is_not_empty() -> None:
    assert len(TEMPLATES) >= 13  # a glob that finds nothing would make the sweep pass vacuously


@pytest.mark.parametrize("flags", [(), ("--mode", "amend-and-build")], ids=["default", "amend"])
@pytest.mark.parametrize("name", [t for t in TEMPLATES if t not in CANNOT_RUN_LOCALLY])
def test_template_applies_locally_and_lands_real_rows(
    tmp_path: Path, name: str, flags: Any
) -> None:
    project = _scaffold(tmp_path, name)
    res = _fluid("apply", "contract.fluid.yaml", "--yes", *flags, cwd=project)
    assert res.returncode == 0, res.stdout + res.stderr
    landed = 0
    for path in _expose_files(project):
        assert path.is_file(), f"{path.relative_to(project)} was not written"
        rows = _rows(path)
        landed += len(rows)
        for row in rows:  # an expose may hold no rows for the sample data (customer-360's VIPs)
            assert PLACEHOLDER not in [str(v) for v in row], f"placeholder row in {path.name}"
    assert landed, "no expose holds a row"


def test_every_listed_template_really_cannot_run_locally(tmp_path: Path) -> None:
    # One test over the whole dict (not a parametrization, which an empty dict would skip).
    for name, reason in CANNOT_RUN_LOCALLY.items():
        assert name in TEMPLATES, f"{name} is no bundled template: remove it from the dict"
        project = _scaffold(tmp_path / name, name)
        res = _fluid("apply", "contract.fluid.yaml", "--yes", cwd=project)
        landed = all(p.is_file() and _rows(p) for p in _expose_files(project))
        assert (
            res.returncode != 0 or not landed
        ), f"{name} now runs locally: remove it from CANNOT_RUN_LOCALLY ({reason})"
