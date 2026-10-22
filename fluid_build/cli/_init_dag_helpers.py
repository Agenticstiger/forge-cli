# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Airflow DAG generation helpers — physical extraction from ``init.py``.

The DAG-emission utilities (``should_generate_dag``,
``generate_dag_for_project``, ``create_basic_dag``,
``create_dags_readme``) used to live inline in ``init.py`` (~250 LOC).
They are self-contained — no shared state with the init flow — so
they extracted cleanly into this sibling module.

``init.py`` re-imports each function at module top so existing
test patches that target ``fluid_build.cli.init.<dag_helper>`` still
resolve via the namespace.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

# ``_init`` indirection — these helpers reference ``RICH_AVAILABLE``
# (and a few other module toggles) on the original ``cli.init``
# namespace. Tests routinely
# ``patch("fluid_build.cli.init.RICH_AVAILABLE", False)`` to exercise
# the no-rich code path; resolving via attribute access at call time
# means those patches still flow through after the physical extraction.
from fluid_build.cli import init as _init  # noqa: E402
from fluid_build.cli._logging import info, warn
from fluid_build.cli.console import cprint, success


def _rich_available() -> bool:
    """Read ``RICH_AVAILABLE`` from the canonical ``cli.init`` module
    so test patches on ``fluid_build.cli.init.RICH_AVAILABLE`` flow
    through to the moved DAG helpers."""
    return getattr(_init, "RICH_AVAILABLE", False)


def should_generate_dag(contract: dict, template: str = None) -> bool:
    """
    Determine if DAG should be auto-generated for this project.

    Auto-generate DAGs when:
    1. Contract has explicit orchestration config
    2. A build has a cron trigger (what ``generate_dag_for_project`` writes a DAG for)
    3. Template is orchestration-focused (customer-360, sales-analytics, ml-features, data-quality)
    4. Project has multiple provider actions (complex pipeline)
    """
    # Check for explicit orchestration config
    if "orchestration" in contract:
        return True

    from fluid_build.schedulers.airflow import fluid_apply

    # ``build:`` is the schema's legacy singular form of ``builds:``.
    build = contract.get("build")
    if fluid_apply.has_scheduled_builds(contract) or (
        isinstance(build, dict) and fluid_apply.has_scheduled_builds({"builds": [build]})
    ):
        return True

    # Check for orchestration-focused templates
    orchestrated_templates = ["customer-360", "sales-analytics", "ml-features", "data-quality"]
    if template and template in orchestrated_templates:
        return True

    # Check for complex pipelines (multiple actions)
    binding = contract.get("binding", {})
    provider_actions = binding.get("providerActions", [])
    if len(provider_actions) > 1:
        return True

    return False


def generate_dag_for_project(
    project_dir: Path, contract: dict, logger, console, template: str = None
) -> bool:
    """
    Generate the project's Airflow DAG with ``fluid generate schedule``.

    Creates dags/ folder with:
    - the DAG file(s) ``fluid generate schedule --scheduler airflow`` writes: a
      ``fluid apply`` DAG per build with a cron trigger, or the scheduler's DAG
      for the contract's ``orchestration.tasks``
    - README.md naming each DAG, with usage instructions

    Writes nothing and returns False, saying why, when the contract schedules
    nothing (a manual or continuous build runs on no timetable, so its DAG
    would only run a placeholder) or when the command refuses the contract,
    an invalid cron for instance: a stand-in DAG on a schedule the contract
    never declared would hide the error. Both are judged on
    ``contract.fluid.yaml`` as the command reads it, not on ``contract``.
    """
    try:
        from fluid_build.schedulers.airflow import fluid_apply

        loaded = _load_as_the_command_does(project_dir.absolute() / "contract.fluid.yaml", logger)
        if loaded is not None and not (
            fluid_apply.has_scheduled_builds(loaded) or fluid_apply.has_explicit_tasks(loaded)
        ):
            return _no_dag(
                "the contract schedules nothing (no build has a cron trigger and there "
                "are no orchestration.tasks)",
                logger,
                console,
            )

        dag_dir = project_dir.absolute() / "dags"
        if _rich_available():
            console.print("\n[cyan]📅 Generating Airflow DAG...[/cyan]")

        before = _dag_files(dag_dir)
        exit_code = _run_generate_schedule(project_dir, dag_dir, logger)
        if exit_code != 0:
            return _no_dag(
                f"fluid generate schedule refused the contract (exit {exit_code}); "
                "its message above says why",
                logger,
                console,
                warning=True,
            )
        written = sorted(
            path for path, stamp in _dag_files(dag_dir).items() if before.get(path) != stamp
        )
        if not written:
            return _no_dag("fluid generate schedule wrote no DAG file", logger, console)

        names = _write_dags_readme(dag_dir, written)
        if _rich_available():
            from rich.markup import escape

            for name in names:
                console.print(f"[green]✅ DAG created: dags/{escape(name)}[/green]")

        return True

    except Exception as e:
        logger.warning(f"Failed to generate DAG: {e}")
        return False


def _load_as_the_command_does(contract_path: Path, logger):
    """The contract as ``fluid generate schedule`` reads it, or None when it does not load.

    The command's loader resolves ``$ref`` builds and the legacy singular
    ``build:`` key, which the raw YAML does not show. When the contract does not
    load, the command itself reports why.
    """
    from fluid_build._contract_loader import load_contract_with_overlay

    try:
        return load_contract_with_overlay(str(contract_path), None, logger)
    except Exception:
        return None


def _no_dag(reason: str, logger, console, warning: bool = False) -> bool:
    """Say why no DAG was written; return False."""
    (logger.warning if warning else logger.info)(f"No Airflow DAG written: {reason}")
    if _rich_available():
        from rich.markup import escape

        console.print(
            f"[{'yellow' if warning else 'dim'}]No Airflow DAG written: {escape(reason)}[/]"
        )
    return False


def _generate_schedule_argv(project_dir: Path, dag_dir: Path) -> list:
    """The ``fluid generate schedule`` arguments that write the project's Airflow DAGs.

    Absolute paths and ``--contract-path`` make the result independent of the
    current directory: a ``fluid apply`` DAG records the contract's path relative
    to ``$FLUID_PROJECT_DIR`` on the worker, which is the project root here.
    ``--overwrite`` lets a later run regenerate the DAGs an earlier one wrote.
    """
    contract = project_dir.absolute() / "contract.fluid.yaml"
    return ["generate", "schedule", str(contract), "--scheduler", "airflow"] + [
        "--output",
        str(dag_dir),
        "--contract-path",
        "contract.fluid.yaml",
        "--overwrite",
    ]


def _run_generate_schedule(project_dir: Path, dag_dir: Path, logger) -> int:
    """Run ``fluid generate schedule`` in this process; return its exit code.

    As stage 3 runs it (``forge/core/artifact_fanout.py``), but parsed by the
    CLI's own ``build_parser``, so a flag the command does not have fails here
    as it would on the command line. The command prints its own messages.
    """
    from fluid_build.cli import build_parser

    try:
        args = build_parser().parse_args(_generate_schedule_argv(project_dir, dag_dir))
    except SystemExit as exc:  # argparse has printed why
        return exc.code if isinstance(exc.code, int) else 2
    return args.func(args, logger)


def _dag_files(dag_dir: Path) -> dict:
    """Each regular ``*.py`` file under *dag_dir*, with its modification stamp.

    Comparing a snapshot taken before a run with one taken after tells the
    files the run wrote from ones already there. Only regular files count:
    reading a FIFO named ``x.py`` would block.
    """
    if not dag_dir.is_dir():
        return {}
    stamps = {}
    for path in dag_dir.rglob("*.py"):
        if path.is_file():
            stat = path.stat()
            stamps[path] = (stat.st_mtime_ns, stat.st_size)
    return stamps


def _write_dags_readme(dag_dir: Path, dag_files: list) -> list:
    """Write dags/README.md naming each DAG file; return their names."""
    names = [path.relative_to(dag_dir).as_posix() for path in dag_files]
    dags = [
        (dag_id or "(see the DAG file)", schedule or "(see the DAG file)", name)
        for (dag_id, schedule), name in zip(map(_dag_facts, dag_files), names, strict=True)
    ]
    create_dags_readme(dag_dir, *dags[0], others=dags[1:])
    return names


def _dag_facts(path: Path) -> tuple:
    """``(dag_id, schedule)`` as a generated DAG file spells them, for the README.

    Read with ``ast``, not imported: importing needs Airflow and runs the file.
    ``schedule=`` may name a module constant (the ``fluid apply`` DAG's
    ``SCHEDULE``). A value not written as a string comes back as ``""``.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, ValueError, RecursionError):
        return "", ""
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def string_value(node) -> str:
        if isinstance(node, ast.Constant):
            value = node.value
        else:
            value = constants.get(getattr(node, "id", None))
        return value if isinstance(value, str) else ""

    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        if isinstance(node, ast.Call) and "DAG" in (
            getattr(func, "id", None),
            getattr(func, "attr", None),
        ):
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            return string_value(kwargs.get("dag_id")), string_value(kwargs.get("schedule"))
    return "", ""


def _basic_dag_commands(project_dir: Path) -> tuple:
    """The basic DAG's validate, plan and apply commands, as Python string literals.

    The project path goes into a shell command inside a Python string literal, so
    it is quoted for both: a path holding a quote must not end the literal and run
    what follows when Airflow parses the DAG.
    """
    import shlex

    from fluid_build.providers.common.codegen_utils import py_str_literal

    in_project = f"cd {shlex.quote(str(project_dir.absolute()))} && fluid"
    return tuple(
        py_str_literal(f"{in_project} {step} contract.fluid.yaml{flags}")
        for step, flags in (("validate", ""), ("plan", ""), ("apply", " --yes"))
    )


def create_basic_dag(project_dir: Path, contract: dict, logger):
    """Create a basic validate, plan and apply DAG for the project (Python API).

    ``generate_dag_for_project`` does not fall back to it: when ``fluid generate
    schedule`` refuses a contract, a DAG on a schedule the contract never
    declared would hide the error.
    """

    import re as _re

    from fluid_build.providers.common.codegen_utils import (
        airflow_dag_import,
        airflow_standard_import,
    )

    validate_cmd, plan_cmd, apply_cmd = _basic_dag_commands(project_dir)
    contract_name = contract.get("name", "my_product")
    orchestration = contract.get("orchestration", {})
    # Sanitize values to prevent code injection in generated Python.
    dag_id = _re.sub(r"[^a-zA-Z0-9_]", "_", contract_name)[:128]
    schedule = _re.sub(r"[^a-zA-Z0-9@*/, _-]", "", orchestration.get("schedule", "@daily"))[:64]
    contract_name = _re.sub(r"[^a-zA-Z0-9 _.-]", "_", contract_name)[:128]
    # Numbers, never text: both are interpolated into the DAG's Python source, so a
    # contract value such as `3, __import__('os').system('...')` would run at parse time.
    try:
        retries = max(0, min(int(orchestration.get("retries", 3)), 100))
    except (TypeError, ValueError, OverflowError):  # OverflowError: `retries: .inf`
        retries = 3
    delay = _re.fullmatch(r"\s*(\d{1,4})\s*m?\s*", str(orchestration.get("retry_delay", "5m")))
    retry_minutes = int(delay.group(1)) if delay else 5

    dag_content = f'''"""
Airflow DAG for FLUID contract: {contract_name}
Auto-generated by fluid init
"""

from datetime import datetime, timedelta

{airflow_dag_import()}{airflow_standard_import("BashOperator")}
default_args = {{
    'owner': 'fluid',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': {retries},
    'retry_delay': timedelta(minutes={retry_minutes}),
}}

with DAG(
    dag_id='{dag_id}',
    default_args=default_args,
    description='FLUID data product: {contract_name}',
    schedule='{schedule}',
    catchup=False,
    tags=['fluid', 'data-product'],
) as dag:

    # Validate contract
    validate = BashOperator(
        task_id='validate_contract',
        bash_command={validate_cmd},
    )

    # Plan execution
    plan = BashOperator(
        task_id='plan_execution',
        bash_command={plan_cmd},
    )

    # Apply changes
    apply = BashOperator(
        task_id='apply_contract',
        bash_command={apply_cmd},
    )

    validate >> plan >> apply
'''

    dag_dir = project_dir / "dags"
    dag_dir.mkdir(exist_ok=True)
    dag_file = dag_dir / f"{dag_id}_dag.py"

    with open(dag_file, "w", encoding="utf-8") as f:
        f.write(dag_content)

    logger.info(f"Created basic DAG template: {dag_file}")


def create_dags_readme(
    dag_dir: Path, dag_id: str, schedule: str, dag_filename: str, others: tuple = ()
):
    """Create README in dags/ folder with usage instructions.

    ``others`` holds one ``(dag_id, schedule, dag_filename)`` per further DAG,
    when ``fluid generate schedule`` wrote more than one.
    """

    dags = [(dag_id, schedule, dag_filename), *others]
    listing = "\n\n".join(
        f"- **DAG ID**: `{one_id}`\n- **Schedule**: `{one_schedule}`\n- **File**: `{one_file}`"
        for one_id, one_schedule, one_file in dags
    )
    readme_content = f"""# Airflow DAG Configuration

This folder contains the Airflow {"DAG" if len(dags) == 1 else "DAGs"} for your FLUID data product, \
generated from `contract.fluid.yaml` by `fluid generate schedule`.

## Generated {"DAG" if len(dags) == 1 else "DAGs"}

{listing}

## Usage

Copy the DAG file into your Airflow deployment's DAGs folder (Cloud Composer, MWAA,
Astronomer or a self-hosted Airflow), after reading its docstring: a `fluid apply` DAG
needs `FLUID_PROJECT_DIR` set to this project on the Airflow worker and `fluid` on PATH.

### Manual Execution

Run the FLUID pipeline manually:

```bash
# Validate contract
fluid validate contract.fluid.yaml

# Plan execution
fluid plan contract.fluid.yaml

# Apply changes and run the builds
fluid apply contract.fluid.yaml --mode amend-and-build --yes
```

## Customization

The DAG is generated: edit the contract, not the DAG file. To change the schedule or
the tasks, update `contract.fluid.yaml` and regenerate:

```bash
fluid generate schedule contract.fluid.yaml --scheduler airflow --output dags --overwrite
```

Regenerating does not delete the DAG of a build you renamed or removed: delete that
file from `dags/` yourself.

## Next Steps

- **Add data quality checks**: Use Great Expectations or Soda
- **Set up alerting**: Configure email/Slack notifications
- **Add lineage tracking**: Enable OpenLineage integration
- **Monitor performance**: Use Airflow metrics

For more information, see: https://agenticstiger.github.io/forge_docs/advanced/airflow.html
"""

    readme_path = dag_dir / "README.md"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)


# ============================================================================
# MODE HANDLERS
# ============================================================================
