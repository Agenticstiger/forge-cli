# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A ``fluid ship`` stage never imports a module from the checkout it runs in.

Ship runs each stage in the contract's directory. Started as
``python -m fluid_build.cli``, the stage had that directory first on
``sys.path``, so a ``yaml.py`` (or ``rich/``, or a ``fluid_build/``) in the
checkout ran inside every stage, before ``main()``, with the operator's
environment and the store token ship hands on. The stage now starts through a
bootstrap that runs ``fluid_build.cli`` as ``-m`` would, without that entry.
These tests start real stage processes.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fluid_build.cli import ship

LOG = logging.getLogger("test_ship_stage_start")


def _plant(project: Path, marker: Path) -> None:
    """Modules that record their import in ``marker`` and stop the process."""
    body = f"open({str(marker)!r}, 'a', encoding='utf-8').write(__name__ + '\\n')\nraise SystemExit(9)\n"
    for name in ("yaml", "rich", "httpx", "argparse_extras"):
        (project / f"{name}.py").write_text(body, encoding="utf-8")
    shadow = project / "fluid_build"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(body, encoding="utf-8")


def test_a_module_planted_in_the_checkout_never_runs_in_a_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "planted-imports.txt"
    _plant(project, marker)
    monkeypatch.chdir(project)  # ship runs its stages in the contract's directory
    rc = ship._run_stage(ship._resolve_fluid_bin(), "--version", [], LOG, env=dict(os.environ))
    assert not marker.exists(), f"a planted module ran in the stage: {marker.read_text()}"
    assert rc == 0


def test_the_bootstrap_drops_the_checkout_before_any_import_it_could_shadow(
    tmp_path: Path,
) -> None:
    # Under -S nothing is preloaded by site (a venv's .pth files import importlib,
    # types and warnings early), so what runpy imports is looked up as it would be
    # in a bare interpreter. keyword stands in for fluid_build.cli: a stdlib module
    # the planted one would shadow too. The planted modules run only in the child.
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "planted-imports.txt"
    body = f"open({str(marker)!r}, 'a', encoding='utf-8').write(__name__ + '\\n')\nraise SystemExit(9)\n"
    for name in ("runpy", "warnings", "types", "keyword"):
        (project / f"{name}.py").write_text(body, encoding="utf-8")
    (project / "importlib").mkdir()
    (project / "importlib" / "__init__.py").write_text(body, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-S", "-c", ship._stage_bootstrap("keyword")],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert not marker.exists(), f"a planted module ran first: {marker.read_text()}"
    assert result.returncode == 0, result.stderr


def test_a_stage_still_gets_its_arguments_and_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = ship._run_stage(
        ship._resolve_fluid_bin(),
        "validate",
        ["no-such-contract.fluid.yaml"],
        LOG,
        env=dict(os.environ),
    )
    assert rc not in (0, 9)  # fluid validate's own refusal of a missing file


def test_the_bootstrap_runs_the_cli_as_dash_m_would() -> None:
    cmd = ship._resolve_fluid_bin()
    assert cmd[0] == sys.executable
    assert "-m" not in cmd  # -m puts the working directory first on sys.path
    assert len(cmd) == 3  # the stage name is cmd[3], as with "-m fluid_build.cli"
