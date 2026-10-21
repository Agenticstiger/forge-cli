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

"""``fluid init``'s DAG helper writes what ``fluid generate schedule`` writes, or nothing.

``generate_dag_for_project`` used to run ``fluid generate-airflow --output-dir ...``,
a flag that command never had: argparse exited 2, so every project got the basic
fallback DAG, or no DAG at all when ``fluid`` was not on PATH. Its tests mocked
``subprocess.run`` and nothing ever parsed that argv. The helper now runs
``fluid generate schedule`` in-process through the CLI's own parser, and writes no
DAG, saying why, when the contract schedules nothing or the command refuses it.
These tests parse its argv with the real parser and run the real command.
"""

from __future__ import annotations

import ast
import logging
import shlex
import shutil
from pathlib import Path
from typing import Dict, Tuple
from unittest.mock import patch

import pytest
import yaml

import fluid_build
from fluid_build.cli import build_parser
from fluid_build.cli import init as cli_init
from fluid_build.cli._init_dag_helpers import (
    _dag_facts,
    _generate_schedule_argv,
    create_basic_dag,
    create_dags_readme,
    generate_dag_for_project,
    should_generate_dag,
)

TEMPLATES = Path(fluid_build.__file__).parent / "templates"
LOG = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _plain_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_init, "RICH_AVAILABLE", False)


def _project(tmp_path: Path, template: str) -> Tuple[Path, dict]:
    project = tmp_path / template
    shutil.copytree(TEMPLATES / template, project)
    return project, yaml.safe_load((project / "contract.fluid.yaml").read_text("utf-8"))


def _bash_commands(source: str) -> Dict[str, str]:
    commands = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "BashOperator":
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            commands[kwargs["task_id"].value] = kwargs["bash_command"].value
    return commands


def test_the_helper_passes_generate_schedule_flags_the_cli_accepts(tmp_path: Path):
    argv = _generate_schedule_argv(tmp_path, tmp_path / "dags")
    args = build_parser().parse_args(argv)  # exits 2 on a flag the command lacks

    assert args.func.__module__ == "fluid_build.cli.generate_schedule"
    assert args.contract == str(tmp_path / "contract.fluid.yaml")
    assert args.scheduler == "airflow"
    assert args.output == str(tmp_path / "dags")
    assert args.dag_contract_path == "contract.fluid.yaml"
    assert args.overwrite is True


def test_the_argv_check_can_fail():
    """The argv init used to build. Parsing it exits 2, which the test above would catch."""
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(
            ["generate-airflow", "contract.fluid.yaml", "--output-dir", "dags"]
        )
    assert exit_info.value.code == 2


@pytest.mark.parametrize(
    "template, dag_file, dag_id, schedule",
    [
        (
            "first-dag",
            "sales_analytics_pipeline_dag.py",
            "tutorial.sales_analytics_pipeline_v1__sales_analytics_pipeline",
            "0 2 * * *",
        ),
        (
            "pipeline-orchestration",
            "orchestrated_pipeline_dag.py",
            "orchestration.complex_pipeline_v1__orchestrated_pipeline",
            "0 3 * * *",
        ),
    ],
)
def test_a_scheduled_build_gets_its_fluid_apply_dag_and_a_readme_naming_it(
    tmp_path: Path, template: str, dag_file: str, dag_id: str, schedule: str
):
    project, contract = _project(tmp_path, template)

    assert generate_dag_for_project(project, contract, LOG, None) is True

    dags = project / "dags"
    assert sorted(path.name for path in dags.iterdir()) == ["README.md", dag_file]
    assert "Edit the contract, not this file" in (dags / dag_file).read_text("utf-8")
    readme = (dags / "README.md").read_text("utf-8")
    assert f"**DAG ID**: `{dag_id}`" in readme
    assert f"**Schedule**: `{schedule}`" in readme
    assert f"**File**: `{dag_file}`" in readme
    assert "--overwrite" in readme  # how to regenerate after editing the contract
    assert "--auto-approve" not in readme


def test_a_manual_build_gets_no_dag(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """customer-360's build is ``trigger.type: manual``, so a nightly DAG would only run a
    placeholder task."""
    project, contract = _project(tmp_path, "customer-360")

    with caplog.at_level(logging.INFO):
        assert generate_dag_for_project(project, contract, LOG, None) is False

    assert not (project / "dags").exists()
    assert "No Airflow DAG written: the contract schedules nothing" in caplog.text


def test_a_continuous_build_gets_no_dag(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    contract = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "streams.clicks",
        "name": "clicks",
        "orchestration": {"engine": "airflow"},
        "builds": [
            {
                "id": "sink",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "execution": {"trigger": {"type": "continuous"}},
            }
        ],
    }
    (tmp_path / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")

    with caplog.at_level(logging.INFO):
        assert generate_dag_for_project(tmp_path, contract, LOG, None) is False

    assert not (tmp_path / "dags").exists()
    assert "No Airflow DAG written: the contract schedules nothing" in caplog.text


def test_a_refused_contract_gets_no_dag_and_says_why(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """No stand-in DAG on a schedule the contract never declared: it would hide the error."""
    project, contract = _project(tmp_path, "first-dag")
    path = project / "contract.fluid.yaml"
    text = path.read_text("utf-8").replace("schedule: 0 2 * * *", "schedule: 61 25 * * *")
    assert "61 25" in text
    path.write_text(text, "utf-8")

    with patch.object(cli_init, "create_basic_dag") as basic, caplog.at_level(logging.INFO):
        assert generate_dag_for_project(project, yaml.safe_load(text), LOG, None) is False

    basic.assert_not_called()
    assert not (project / "dags").exists()
    assert "'61' is not a valid minute field" in caplog.text  # the command's own message
    assert "refused the contract (exit 2)" in caplog.text


def test_a_second_run_regenerates_the_dag(tmp_path: Path):
    project, contract = _project(tmp_path, "first-dag")

    assert generate_dag_for_project(project, contract, LOG, None) is True
    assert generate_dag_for_project(project, contract, LOG, None) is True

    dags = sorted(path.name for path in (project / "dags").iterdir())
    assert dags == ["README.md", "sales_analytics_pipeline_dag.py"]


def test_the_readme_names_only_the_dags_this_run_wrote(tmp_path: Path):
    project, contract = _project(tmp_path, "first-dag")
    (project / "dags").mkdir()
    (project / "dags" / "helpers.py").write_text("X = 1\n", "utf-8")

    assert generate_dag_for_project(project, contract, LOG, None) is True

    readme = (project / "dags" / "README.md").read_text("utf-8")
    assert "`sales_analytics_pipeline_dag.py`" in readme
    assert "helpers.py" not in readme


def test_a_relative_project_dir_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``demo_mode`` passes ``Path(project_name)``, relative to the current directory."""
    monkeypatch.chdir(tmp_path)
    _, contract = _project(tmp_path, "first-dag")

    assert generate_dag_for_project(Path("first-dag"), contract, LOG, None) is True

    assert (tmp_path / "first-dag" / "dags" / "sales_analytics_pipeline_dag.py").is_file()


def test_a_singular_build_key_is_judged_as_the_command_reads_it(tmp_path: Path):
    """The schema's legacy ``build:`` holds one build, and the command's loader turns it
    into ``builds``: the helper must not call that contract unscheduled."""
    project, contract = _project(tmp_path, "first-dag")
    (build,) = contract.pop("builds")
    contract["build"] = build
    (project / "contract.fluid.yaml").write_text(yaml.safe_dump(contract), "utf-8")

    assert should_generate_dag(contract) is True
    assert generate_dag_for_project(project, contract, LOG, None) is True
    assert (project / "dags" / "sales_analytics_pipeline_dag.py").is_file()


def test_should_generate_dag_for_a_scheduled_build_only():
    def build(trigger: dict) -> dict:
        return {"builds": [{"id": "b", "execution": {"trigger": trigger}}]}

    assert should_generate_dag(build({"type": "schedule", "cron": "0 2 * * *"})) is True
    assert should_generate_dag(build({"type": "manual"})) is False


def test_the_readme_lists_each_dag_on_its_own(tmp_path: Path):
    create_dags_readme(tmp_path, "a", "@daily", "a_dag.py", others=[("b", "0 3 * * *", "b_dag.py")])

    readme = (tmp_path / "README.md").read_text("utf-8")
    assert "## Generated DAGs" in readme
    assert "- **DAG ID**: `a`\n- **Schedule**: `@daily`\n- **File**: `a_dag.py`" in readme
    assert "- **DAG ID**: `b`\n- **Schedule**: `0 3 * * *`\n- **File**: `b_dag.py`" in readme


def test_the_basic_dag_quotes_the_project_path_for_python_and_shell(tmp_path: Path):
    project = tmp_path / 'o\'brien $(touch pwned) "data"'
    project.mkdir()

    create_basic_dag(project, {"name": "orders"}, LOG)

    commands = _bash_commands((project / "dags" / "orders_dag.py").read_text("utf-8"))
    assert sorted(commands) == ["apply_contract", "plan_execution", "validate_contract"]
    for command in commands.values():
        assert shlex.split(command)[:4] == ["cd", str(project.absolute()), "&&", "fluid"]
    assert shlex.split(commands["apply_contract"])[4:] == ["apply", "contract.fluid.yaml", "--yes"]


@pytest.mark.parametrize(
    "source, facts",
    [
        ("dag = DAG(dag_id='a', schedule='@daily')\n", ("a", "@daily")),
        # The ``fluid apply`` DAG names its schedule through a module constant.
        (
            "SCHEDULE = '0 2 * * *'\nwith DAG(dag_id='b', schedule=SCHEDULE):\n    pass\n",
            ("b", "0 2 * * *"),
        ),
        ("import airflow.sdk as sdk\nsdk.DAG(dag_id='c', schedule=None)\n", ("c", "")),
        ("﻿dag = DAG(dag_id='d', schedule='@hourly')\n", ("d", "@hourly")),  # a BOM
        ("this is not python", ("", "")),
    ],
)
def test_dag_facts(tmp_path: Path, source: str, facts: Tuple[str, str]):
    path = tmp_path / "x_dag.py"
    path.write_text(source, "utf-8")
    assert _dag_facts(path) == facts
