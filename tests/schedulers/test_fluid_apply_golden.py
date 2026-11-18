# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Snapshots of the ``fluid apply`` DAGs the contracts in ``fluid_apply_fixtures.py`` render to.

``golden/fluid_apply/<case>/<file>.txt`` is what the renderer writes now. For a contract that
declares neither ``builds[].dependencies`` nor a dataset trigger (:data:`CASES`), the file is
also what the renderer wrote before it learned either, but for the ``outlets`` of the exposes a
build writes: ``<file>.before.txt`` holds that earlier output, rendered at forge-cli 003e4b08,
and the test holds everything else in the file to it byte for byte. To regenerate the current
snapshots after an intended change::

    UPDATE_FLUID_APPLY_GOLDEN=1 pytest tests/schedulers/test_fluid_apply_golden.py

The ``.before.txt`` files are never regenerated.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Dict

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.schedulers.fluid_apply_fixtures import CASES, GRAPH_CASES

GOLDEN = Path(__file__).parent / "golden" / "fluid_apply"
ALL_CASES = {**CASES, **GRAPH_CASES}

#: The two places outlets reach a DAG of one build: the ``Asset`` import and the task's list.
_ASSET_IMPORT = (
    "try:  # Airflow 3\n"
    "    from airflow.sdk import Asset\n"
    "except ImportError:  # Airflow 2.x\n"
    "    from airflow.datasets import Dataset as Asset\n"
    "\n"
)
_OUTLETS = re.compile(
    r"^        outlets=\[\n(?:            Asset\('[^']*'\),\n)+        \],\n", re.M
)


def _render(case: str) -> Dict[str, str]:
    contract, env, path = ALL_CASES[case]
    return fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=path)


def _without_outlets(source: str) -> str:
    return _OUTLETS.sub("", source.replace(_ASSET_IMPORT, "", 1))


@pytest.mark.parametrize("case", sorted(ALL_CASES))
def test_each_dag_matches_its_snapshot(case: str) -> None:
    rendered = _render(case)
    folder = GOLDEN / case
    if os.environ.get("UPDATE_FLUID_APPLY_GOLDEN"):
        folder.mkdir(parents=True, exist_ok=True)
        for name, source in rendered.items():
            (folder / f"{name}.txt").write_text(source, encoding="utf-8")
    pinned = {p.name[: -len(".txt")] for p in folder.glob("*.py.txt")}
    assert set(rendered) == pinned
    for name, source in rendered.items():
        assert source == (folder / f"{name}.txt").read_text(encoding="utf-8"), name


@pytest.mark.parametrize("case", sorted(CASES))
def test_without_the_new_fields_only_outlets_changed(case: str) -> None:
    """The earlier renderer's bytes, but for the outlets: the seam changed nothing else."""
    for name, source in _render(case).items():
        before = (GOLDEN / case / f"{name}.before.txt").read_text(encoding="utf-8")
        assert _without_outlets(source) == before, name


def test_the_cases_cover_files_with_and_without_outlets() -> None:
    """A demo build lands a relative local path, which names no asset: its DAG is the earlier
    one to the byte. The Spark build's S3 table is an asset, so its DAG gains outlets."""
    changed = sorted(
        f"{case}/{name}"
        for case in CASES
        for name, source in _render(case).items()
        if source != (GOLDEN / case / f"{name}.before.txt").read_text(encoding="utf-8")
    )
    assert changed == ["spark/daily_curated_dag.py"]
    befores = {p.relative_to(GOLDEN).as_posix() for p in GOLDEN.glob("*/*.before.txt")}
    assert befores == {f"{case}/{name}.before.txt" for case in CASES for name in _render(case)}


@pytest.mark.parametrize("case", sorted(ALL_CASES))
def test_every_snapshot_is_python_that_binds_every_name_it_reads(case: str) -> None:
    builtins = set(vars(__import__("builtins")))
    for name, source in _render(case).items():
        tree = ast.parse(source)
        bound = set(builtins)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.arg)):
                bound.add(node.name if isinstance(node, ast.FunctionDef) else node.arg)
            elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
                bound.add(node.target.id)
        loaded = {
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        }
        assert loaded <= bound, (name, loaded - bound)
