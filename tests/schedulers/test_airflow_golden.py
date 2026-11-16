# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Snapshots of the DAG each provider's orchestration contract renders to.

The per-provider generators this renderer replaced are gone; ``golden/DIFFERENCES.md``
lists every way their output differed from what is pinned here and why. To
regenerate after an intended change::

    UPDATE_AIRFLOW_GOLDEN=1 pytest tests/schedulers/test_airflow_golden.py
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from fluid_build.schedulers.airflow import render_dag
from tests.schedulers.airflow_fixtures import CONTRACTS, PROVIDER_CONFIG

GOLDEN = Path(__file__).parent / "golden"

_BUILTINS = set(vars(__import__("builtins")))


def render(provider: str) -> str:
    source = render_dag(
        CONTRACTS[provider], provider=provider, provider_config=PROVIDER_CONFIG[provider]
    )
    return re.sub(r"Generated: .*", "Generated: <ts>", source).rstrip() + "\n"


@pytest.mark.parametrize("provider", sorted(CONTRACTS))
def test_the_rendered_dag_matches_its_snapshot(provider: str):
    snapshot = GOLDEN / f"{provider}.py.txt"
    rendered = render(provider)
    if os.environ.get("UPDATE_AIRFLOW_GOLDEN"):
        snapshot.write_text(rendered, encoding="utf-8")
    assert rendered == snapshot.read_text(encoding="utf-8")


def _bound(tree: ast.Module) -> set:
    """Every name the module binds at any scope."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            names.update(
                x.arg
                for x in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]
                if x is not None
            )
    return names


@pytest.mark.parametrize("provider", sorted(CONTRACTS))
def test_the_rendered_dag_loads_no_undefined_name(provider: str):
    """Airflow runs the module when it parses the DAG, so a name nothing binds fails the whole file."""
    tree = ast.parse(render(provider))
    bound = _bound(tree) | _BUILTINS
    undefined = {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound
    }
    assert not undefined


@pytest.mark.parametrize("provider", sorted(CONTRACTS))
def test_every_task_of_the_contract_is_in_the_dag(provider: str):
    tree = ast.parse(render(provider))
    task_ids = {
        kw.value.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "task_id" and isinstance(kw.value, ast.Constant)
    }
    declared = {t["taskId"] for t in CONTRACTS[provider]["orchestration"]["tasks"]}
    assert task_ids == declared
