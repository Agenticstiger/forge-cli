# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every Airflow DAG forge-cli generates parses on Airflow 2.7+ and on Airflow 3.

Airflow 3 removed ``DAG(schedule_interval=...)`` (``TypeError``) and
``airflow.utils.dates.days_ago`` (``ImportError``), so a DAG that uses either fails to
import. It also moved the core operators and sensors to apache-airflow-providers-standard
and exports ``DAG``, ``dag`` and ``task`` from ``airflow.sdk``; the old paths still
resolve on 3.2 with a deprecation warning. Every generator (``fluid export --engine
airflow`` and ``fluid generate schedule``, both through ``schedulers/airflow``, plus
``fluid init``, ``fluid scaffold-composer`` and ``fluid schedule-sync``) emits the shape
both versions accept without either: ``schedule=``, a fixed ``start_date``, and each
moved name imported from its Airflow 3 home with an ``except ImportError`` fallback to
the Airflow 2 path (``providers/common/codegen_utils.py``).

This renders every generator and checks the source, so it needs no Airflow
install. The author also loaded each rendered DAG into a real Airflow 3.2.2 and
2.10.5 ``DagBag`` (see the PR).
"""

from __future__ import annotations

import ast
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Imported only in an ``except ImportError`` fallback, behind its Airflow 3 path.
AIRFLOW2_ONLY = ("airflow.operators", "airflow.sensors", "airflow.decorators")
AIRFLOW2_DAG_MODULES = {"airflow", "airflow.models", "airflow.models.dag"}
AIRFLOW3_HOMES = ("airflow.sdk", "airflow.providers.standard.")

_TASKS = [
    {
        "taskId": "load",
        "type": "provider_action",
        "action": "aws.athena.query",
        "params": {"query": "SELECT 1", "output_location": "s3://r/"},
    },
    {
        "taskId": "publish",
        "type": "provider_action",
        "action": "aws.athena.query",
        "params": {"query": "SELECT 2", "output_location": "s3://r/"},
        "dependsOn": ["load"],
    },
]


_CONFIG = {
    "aws": {"region": "eu-west-1"},
    "gcp": {"project": "proj", "region": "europe-west1"},
    "snowflake": {"database": "DB"},
}


def _contract(action_prefix: str = "aws.athena") -> dict:
    tasks = [{**t, "action": f"{action_prefix}.query"} for t in _TASKS]
    return {
        "id": "sales.orders",
        "name": "Orders",
        "orchestration": {"schedule": "0 2 * * *", "timezone": "UTC", "tasks": tasks},
    }


def _scheduler(provider: str) -> str:
    from fluid_build.schedulers.airflow import AirflowScheduler

    prefix = {"aws": "aws.athena", "gcp": "gcp.bigquery", "snowflake": "sf.snowflake"}[provider]
    (code,) = (
        AirflowScheduler()
        .generate(
            _contract(prefix),
            provider=provider,
            provider_config={**_CONFIG[provider], "allow_unmapped": True},
        )
        .values()
    )
    return code


def _golden(provider: str, only: tuple = ()) -> str:
    """The golden contract of a provider (every operator it renders), optionally cut
    down to the tasks named in ``only``."""
    from fluid_build.schedulers.airflow import render_dag
    from tests.schedulers.airflow_fixtures import CONTRACTS, PROVIDER_CONFIG

    contract = CONTRACTS[provider]
    if only:
        tasks = [t for t in contract["orchestration"]["tasks"] if t["taskId"] in only]
        contract = {**contract, "orchestration": {**contract["orchestration"], "tasks": tasks}}
    return render_dag(
        contract,
        provider=provider,
        provider_config=PROVIDER_CONFIG[provider],
        allow_unmapped=True,
    )


def _init_dag(tmp_path: Path) -> str:
    from fluid_build.cli.init import create_basic_dag  # re-exported; the helper module imports init

    create_basic_dag(
        tmp_path, {"name": "orders", "orchestration": {"schedule": "@daily"}}, MagicMock()
    )
    (dag,) = tmp_path.rglob("*.py")
    return dag.read_text()


def _scaffold_composer() -> str:
    from fluid_build.cli.scaffold_composer import DAG_TMPL

    return DAG_TMPL.format(
        dag_id="'orders'",
        cron="'0 2 * * *'",
        validate_cmd="'true'",
        plan_cmd="'true'",
        apply_cmd="'true'",
    )


def _schedule_sync() -> str:
    from fluid_build.cli._acquisition_stage_ext import _render_airflow_dag

    return _render_airflow_dag("sales.orders", "b1", {}, "0 2 * * *")


RENDERS = {
    "scheduler aws": lambda tmp: _scheduler("aws"),
    "scheduler gcp": lambda tmp: _scheduler("gcp"),
    "scheduler snowflake": lambda tmp: _scheduler("snowflake"),
    "aws every operator": lambda tmp: _golden("aws"),
    "gcp every operator": lambda tmp: _golden("gcp"),
    "gcp without dataflow": lambda tmp: _golden("gcp", only=("ds", "tbl", "load", "topic")),
    "snowflake every operator": lambda tmp: _golden("snowflake"),
    "fluid init": _init_dag,
    "scaffold-composer": lambda tmp: _scaffold_composer(),
    "schedule-sync": lambda tmp: _schedule_sync(),
}


def _under(module: str, prefixes) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _imported_names(node: ast.AST) -> set[str]:
    return (
        {a.asname or a.name for a in node.names}
        if isinstance(node, (ast.Import, ast.ImportFrom))
        else set()
    )


def _airflow2_only(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(_under(a.name, AIRFLOW2_ONLY) for a in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        names = {a.name for a in node.names}
        return _under(module, AIRFLOW2_ONLY) or (
            module in AIRFLOW2_DAG_MODULES
            and bool(names & {"DAG", "dag", "task", "operators", "sensors"})
        )
    return False


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(
        isinstance(k, ast.Name) and k.id in ("ImportError", "ModuleNotFoundError") for k in kinds
    )


def _airflow2_only_imports_outside_a_fallback(tree: ast.AST) -> list[str]:
    """Each Airflow-2-only import (``from x import y`` or ``import x``), unless it sits in an
    ``except ImportError`` whose ``try`` imports the same names from their Airflow 3 home."""
    fallbacks: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        airflow3 = [
            n
            for stmt in node.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith(AIRFLOW3_HOMES)
        ]
        provided = set().union(*(_imported_names(n) for n in airflow3)) if airflow3 else set()
        for handler in node.handlers:
            if not (airflow3 and _catches_import_error(handler)):
                continue
            for stmt in handler.body:
                for n in ast.walk(stmt):
                    if _imported_names(n) and _imported_names(n) <= provided:
                        fallbacks.add(id(n))
    return [
        f"line {node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(tree)
        if _airflow2_only(node) and id(node) not in fallbacks
    ]


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_the_generated_dag_parses_on_airflow_2_and_3(name: str, tmp_path: Path):
    source = RENDERS[name](tmp_path)
    tree = ast.parse(source)  # it is Python at all
    assert (
        "schedule_interval" not in source
    ), "Airflow 3 removed DAG(schedule_interval=); use schedule="
    assert "days_ago" not in source, "Airflow 3 removed airflow.utils.dates.days_ago"
    assert not _airflow2_only_imports_outside_a_fallback(
        tree
    ), _airflow2_only_imports_outside_a_fallback(tree)
    assert any(
        isinstance(n, ast.keyword) and n.arg == "schedule" for n in ast.walk(tree)
    ), "no schedule="


@pytest.mark.parametrize(
    "source",
    [
        "from airflow import DAG\n",
        "from airflow.operators.bash import BashOperator\n",
        "import airflow.operators.bash\n",
        "from airflow.operators import bash\n",
        "from airflow.models import DAG\n",
        "from airflow.decorators import dag, task\n",
        # A fallback with no Airflow 3 import in its ``try`` is no fallback ...
        "try:\n    import json\nexcept ImportError:\n    from airflow.operators.bash import BashOperator\n",
        # ... nor one that imports a different name than its ``try``.
        "try:\n    from airflow.providers.standard.operators.bash import BashOperator\n"
        "except ImportError:\n    from airflow.operators.python import PythonOperator\n",
    ],
)
def test_the_import_check_can_fail(source: str):
    assert len(_airflow2_only_imports_outside_a_fallback(ast.parse(source))) == 1


def test_the_import_check_accepts_the_fallbacks_generators_emit():
    tuple_handler = (
        "try:\n    from airflow.sdk import DAG\n"
        "except (ImportError, ModuleNotFoundError):\n    from airflow import DAG\n"
    )
    assert _airflow2_only_imports_outside_a_fallback(ast.parse(tuple_handler)) == []
    from fluid_build.providers.common.codegen_utils import (
        airflow_dag_import,
        airflow_standard_import,
    )

    shimmed = airflow_dag_import() + airflow_standard_import("BashOperator")
    assert _airflow2_only_imports_outside_a_fallback(ast.parse(shimmed)) == []


def test_the_dataflow_import_comes_only_with_a_dataflow_task(tmp_path: Path):
    """Its module needs the apache-beam provider; an unused failing import stops the DAG."""
    assert "DataflowTemplatedJobStartOperator" not in RENDERS["gcp without dataflow"](tmp_path)
    assert "DataflowTemplatedJobStartOperator" in RENDERS["gcp every operator"](tmp_path)


def test_no_generator_emits_an_operator_its_provider_removed(tmp_path: Path):
    """snowflake provider 6.0 removed SnowflakeOperator; google provider 17.0 removed
    BigQueryCreateEmptyTableOperator, which survives only behind the newer operator."""
    for name, render in RENDERS.items():
        workdir = tmp_path / name.replace(" ", "_")
        workdir.mkdir()
        source = render(workdir)
        assert "SnowflakeOperator" not in source, name
        if "BigQueryCreateEmptyTableOperator" in source:
            assert "BigQueryCreateTableOperator = None" in source, name


def test_snowflake_sql_runs_through_sql_execute_query_with_its_hook_params(tmp_path: Path):
    source = RENDERS["snowflake every operator"](tmp_path)
    assert source.count("SQLExecuteQueryOperator(") == 5
    assert "conn_id='snowflake_default'" in source
    assert "hook_params={'warehouse': 'COMPUTE_WH', 'database': 'ANALYTICS'}" in source


@pytest.mark.parametrize(
    "task_id", ["task", "dag", "AthenaOperator", "S3CreateBucketOperator", "PythonOperator"]
)
def test_a_task_named_like_an_imported_name_does_not_shadow_it(task_id: str):
    """A task variable named ``dag`` rebinds the DAG every later task is attached to; one
    named like an operator class breaks the next task of that kind."""
    from fluid_build.schedulers.airflow import render_dag

    contract = _contract()
    contract["orchestration"]["tasks"] = [
        {
            "taskId": task_id,
            "type": "provider_action",
            "action": "aws.athena.query",
            "params": {"query": "SELECT 1", "output_location": "s3://r/"},
        },
        {
            "taskId": "after",
            "type": "provider_action",
            "action": "aws.athena.query",
            "params": {"query": "SELECT 2", "output_location": "s3://r/"},
            "dependsOn": [task_id],
        },
    ]
    tree = ast.parse(render_dag(contract, provider="aws", provider_config={}, allow_unmapped=True))
    assigned = [
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    ]
    imported = {
        a.asname or a.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in n.names
    }
    assert assigned.count("dag") == 1, "a task variable rebinds the DAG"
    assert not set(assigned) & imported, "a task variable shadows an import"


# ── Ruff's Airflow 3 rules over every generated DAG ─────────────────────
#
# Ruff's AIR3 rules (AIR301 removed in Airflow 3, AIR302 moved to a provider, AIR311 and
# AIR312 deprecated with a newer home) know the whole Airflow 3 removal map and skip an
# Airflow 2 import that sits in an ``except ImportError`` fallback behind its Airflow 3
# path (ruff ``crates/ruff_linter/src/rules/airflow``, MIT). They need ruff 0.13 or later.
# They do not skip other code in such a fallback, such as the ``datasets=`` keyword of the
# Airflow 2 half of ``asset_or_time_schedule``, so a finding there is allowed: that branch
# never runs on Airflow 3 (the live tests load both halves on 2.10 and 3.x).


def _ruff() -> str:
    ruff = shutil.which("ruff") or str(Path(sys.executable).with_name("ruff"))
    if not Path(ruff).exists():
        pytest.skip("ruff is not installed")
    out = subprocess.run([ruff, "--version"], capture_output=True, text=True, check=False).stdout
    parts = [int(p) for p in out.split()[-1].split(".")[:2]] if out.split() else [0, 0]
    if parts < [0, 13]:
        pytest.skip(f"ruff {out.strip()} predates the AIR3 rules")
    return ruff


def _air3(ruff: str, folder: Path) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - the repo's own dev tool
        [
            ruff,
            "check",
            "--isolated",
            "--no-cache",
            "--select",
            "AIR3",
            "--output-format",
            "concise",
            str(folder),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_ruff_finds_no_airflow_3_migration_issue_in_any_generated_dag(tmp_path: Path):
    from fluid_build.schedulers.airflow import fluid_apply
    from tests.schedulers.fluid_apply_fixtures import CASES, GRAPH_CASES

    ruff = _ruff()
    out = tmp_path / "dags"
    out.mkdir()
    for name, render in sorted(RENDERS.items()):
        workdir = tmp_path / "work" / name.replace(" ", "_")
        workdir.mkdir(parents=True)
        (out / f"{name.replace(' ', '_').replace('-', '_')}.py").write_text(
            render(workdir), encoding="utf-8"
        )
    for case, (contract, env, path) in {**CASES, **GRAPH_CASES}.items():
        rendered = fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=path)
        for file_name, source in rendered.items():
            (out / f"{case}__{file_name}").write_text(source, encoding="utf-8")
    proc = _air3(ruff, out)
    assert proc.returncode in (0, 1), proc.stderr
    outside = _outside_fallbacks(proc.stdout)
    assert not outside, "\n".join(outside)


def _fallback_lines(tree: ast.AST) -> set:
    """Lines inside an ``except ImportError`` body whose ``try`` imports an Airflow 3 home."""
    lines: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        imports_airflow3 = any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith(AIRFLOW3_HOMES)
            for stmt in node.body
            for n in ast.walk(stmt)
        )
        for handler in node.handlers:
            if imports_airflow3 and _catches_import_error(handler):
                for stmt in handler.body:
                    lines.update(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
    return lines


def _outside_fallbacks(report: str) -> list:
    """Ruff's concise findings that are not inside an Airflow 2 fallback branch."""
    import re

    found = []
    for line in report.splitlines():
        match = re.match(r"(.+?\.py):(\d+):\d+: (AIR\d+)", line)
        if not match:
            continue
        path, number = Path(match.group(1)), int(match.group(2))
        if number not in _fallback_lines(ast.parse(path.read_text(encoding="utf-8"))):
            found.append(line)
    return found


def test_the_ruff_check_can_fail(tmp_path: Path):
    ruff = _ruff()
    (tmp_path / "old.py").write_text(
        "from airflow.operators.bash import BashOperator\n"
        "from airflow.utils.dates import days_ago\n"
        "BashOperator(task_id='t', bash_command='true', start_date=days_ago(1))\n",
        encoding="utf-8",
    )
    proc = _air3(ruff, tmp_path)
    outside = _outside_fallbacks(proc.stdout)
    assert any("AIR301" in f for f in outside) and any("AIR312" in f for f in outside)


def test_a_finding_inside_an_airflow_2_fallback_is_allowed(tmp_path: Path):
    ruff = _ruff()
    (tmp_path / "shim.py").write_text(
        "try:\n"
        "    from airflow.sdk import Asset\n"
        "except ImportError:\n"
        "    from airflow.datasets import Dataset as Asset\n"
        "    from airflow.timetables.datasets import DatasetOrTimeSchedule\n\n"
        "    def schedule(timetable, assets):\n"
        "        return DatasetOrTimeSchedule(timetable=timetable, datasets=assets)\n",
        encoding="utf-8",
    )
    assert _outside_fallbacks(_air3(ruff, tmp_path).stdout) == []
