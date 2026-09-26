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

"""``fluid generate schedule``: a build with a cron trigger gets an Airflow 3
DAG whose every run is ``fluid apply --mode amend-and-build --build-id <id>``.

Measured before this change, on the demo product (one build, trigger
``0 */4 * * *``, no ``orchestration`` block): ``fluid generate schedule``
exited 1 ("No scheduler engine specified"); with ``--scheduler airflow`` it
wrote a PythonOperator that only logged ``Action: generic.duckdb.run``, on
``schedule_interval='0 2 * * *'``, which Airflow 3 rejects and which is not
the build's schedule.
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import zoneinfo
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

import fluid_build
from fluid_build.cli import generate_schedule
from tests.cli._schedule_dag_fixtures import (
    DEMO_CONTRACT,
    DEMO_CONTRACT_PATH,
    load_dag,
    python_stub,
    run_task,
    write_project,
)

LOG = logging.getLogger("test.generate_schedule_fluid_apply")
DAG_FILE = "ingest_subscriptions_dag.py"


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    generate_schedule.register_subcommand(parser.add_subparsers())
    return parser.parse_args(["schedule", *argv])


def _generate(project: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.chdir(project)
    return generate_schedule.run(_parse(DEMO_CONTRACT_PATH, "-o", "out", *argv), LOG)


def _contract(**edits: Any) -> str:
    doc = yaml.safe_load(DEMO_CONTRACT)
    build = doc["builds"][0]
    for key, value in edits.items():
        if key == "build_id":
            build["id"] = value
        elif key == "contract_id":
            doc["id"] = value
        elif key == "trigger":
            build["execution"]["trigger"].update(value)
        elif key == "execution":
            build["execution"].update(value)
        elif key == "builds":
            doc["builds"] = value
    return yaml.safe_dump(doc, sort_keys=False)


class TestTheDemoContractGetsADagThatDoesTheWork:
    def test_a_scheduled_build_without_an_engine_gets_an_airflow3_dag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        assert _generate(tmp_path, monkeypatch) == 0

        source = (tmp_path / "out" / DAG_FILE).read_text()
        assert "schedule_interval" not in source
        assert "PythonOperator" not in source
        loaded = load_dag(source, monkeypatch)
        assert loaded.dag["dag_id"] == "bronze.customer_subscriptions__ingest_subscriptions"
        assert loaded.dag["schedule"] == "0 */4 * * *"
        assert loaded.dag["catchup"] is False
        assert loaded.dag["max_active_runs"] == 1
        assert loaded.dag["start_date"][2] == "UTC"
        (task,) = loaded.tasks
        assert task["task_id"] == "fluid_apply"
        assert task["append_env"] is True
        assert task["skip_on_exit_code"] is None

    def test_each_run_is_fluid_apply_from_the_worker_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ci = tmp_path / "ci-workspace"
        write_project(ci)
        assert _generate(ci, monkeypatch, "--env", "aws") == 0
        loaded = load_dag((ci / "out" / DAG_FILE).read_text(), monkeypatch)

        worker = tmp_path / "worker" / "checkout"
        write_project(worker)
        run = run_task(
            loaded.tasks[0],
            tmp_path,
            {
                "FLUID_PROJECT_DIR": str(worker),
                "FLUID_STATE_BACKEND": "s3://state-bucket/demo",
                "AWS_REGION": "eu-north-1",
                "PGHOST": "db.internal",
                "PGPASSWORD": "pg-secret",  # pragma: allowlist secret
                "AIRFLOW__CORE__FERNET_KEY": "fernet-secret",
                "AIRFLOW_CONN_WAREHOUSE": "postgresql://u:p@h/db",
                "UNRELATED_TOKEN": "tok",
                "VIRTUAL_ENV": "/home/airflow/.local",
                "EXTRA_ONE": "kept",
                "FLUID_DAG_ENV_PASSTHROUGH": "EXTRA_ONE",
                # Not a shell identifier: bash passes it on without listing it.
                "db.password": "k8s-style-secret",  # pragma: allowlist secret
            },
        )
        assert run.returncode == 0, run.stderr
        assert run.argv is not None
        assert os.path.realpath(run.argv[1]) == os.path.realpath(worker / DEMO_CONTRACT_PATH)
        assert run.argv[:1] + run.argv[2:] == [
            "apply",
            "--env",
            "aws",
            "--mode",
            "amend-and-build",
            "--build-id",
            "ingest_subscriptions",
            "--yes",
        ]
        assert os.path.realpath(str(run.cwd)) == os.path.realpath(worker)
        for kept in ("FLUID_STATE_BACKEND", "AWS_REGION", "PGHOST", "PGPASSWORD", "EXTRA_ONE"):
            assert kept in run.env, kept
        for dropped in (
            "AIRFLOW__CORE__FERNET_KEY",
            "AIRFLOW_CONN_WAREHOUSE",
            "UNRELATED_TOKEN",
            "VIRTUAL_ENV",
            "db.password",
        ):
            assert dropped not in run.env, dropped

    def test_without_env_the_flag_is_left_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch)
        run = run_task(loaded.tasks[0], tmp_path, {"FLUID_PROJECT_DIR": str(tmp_path)})
        assert run.returncode == 0, run.stderr
        assert run.argv is not None
        assert run.argv[2:] == [
            "--mode",
            "amend-and-build",
            "--build-id",
            "ingest_subscriptions",
            "--yes",
        ]

    def test_the_task_fails_without_a_project_dir_or_a_contract_in_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        assert _generate(tmp_path, monkeypatch) == 0
        (task,) = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch).tasks

        unset = run_task(task, tmp_path, {})
        assert unset.returncode != 0
        assert unset.argv is None
        assert "FLUID_PROJECT_DIR" in unset.stderr

        empty = tmp_path / "empty-checkout"
        empty.mkdir()
        missing = run_task(task, tmp_path, {"FLUID_PROJECT_DIR": str(empty)})
        assert missing.returncode == 2
        assert missing.argv is None
        assert "no contract at" in missing.stderr

    def test_the_file_still_imports_on_airflow_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path)
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch, airflow3=False)
        assert loaded.dag["schedule"] == "0 */4 * * *"
        assert [t["task_id"] for t in loaded.tasks] == ["fluid_apply"]


class TestNothingSecretOrExecutableIsTemplated:
    def test_a_contract_cannot_ask_for_airflows_own_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        greedy = DEMO_CONTRACT.replace(
            '"{{ env.PGPASSWORD }}"', '"{{ env.AIRFLOW__CORE__FERNET_KEY }}"'
        )
        write_project(tmp_path, greedy)
        assert _generate(tmp_path, monkeypatch) == 0
        (task,) = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch).tasks
        assert "AIRFLOW__CORE__FERNET_KEY" in task["env"]["FLUID_DAG_CONTRACT_ENV"]
        run = run_task(
            task,
            tmp_path,
            {
                "FLUID_PROJECT_DIR": str(tmp_path),
                "AIRFLOW__CORE__FERNET_KEY": "fernet-secret",
                "AIRFLOW_CONN_WAREHOUSE": "postgresql://u:p@h/db",
                "FLUID_DAG_ENV_PASSTHROUGH": "AIRFLOW_CONN_WAREHOUSE",
            },
        )
        assert run.returncode == 0, run.stderr
        assert "AIRFLOW__CORE__FERNET_KEY" not in run.env
        assert "AIRFLOW_CONN_WAREHOUSE" not in run.env

    def test_the_dag_holds_variable_names_never_their_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PGPASSWORD", "hunter2-at-generation-time")
        write_project(tmp_path)
        assert _generate(tmp_path, monkeypatch, "--env", "aws") == 0
        source = (tmp_path / "out" / DAG_FILE).read_text()
        assert "hunter2" not in source
        loaded = load_dag(source, monkeypatch)
        assert (
            loaded.namespace["CONTRACT_ENV_NAMES"] == "PGDATABASE PGHOST PGPASSWORD PGPORT PGUSER"
        )
        assert set(loaded.tasks[0]["env"]) == {
            "FLUID_DAG_CONTRACT",
            "FLUID_DAG_ENV",
            "FLUID_DAG_BUILD_ID",
            "FLUID_DAG_CONTRACT_ENV",
        }

    def test_the_bash_command_is_static_text_with_nothing_for_jinja(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands = []
        for contract_id, build_id in (("orders", "load"), ("bronze.customers", "ingest_v2")):
            project = tmp_path / contract_id
            write_project(project, _contract(contract_id=contract_id, build_id=build_id))
            assert _generate(project, monkeypatch) == 0
            (dag_file,) = (project / "out").iterdir()
            (task,) = load_dag(dag_file.read_text(), monkeypatch).tasks
            assert task["env"]["FLUID_DAG_BUILD_ID"] == build_id
            commands.append(task["bash_command"])
        assert commands[0] == commands[1]
        command = commands[0]
        for marker in ("{{", "{%", "{#", "orders", "ingest_v2"):
            assert marker not in command
        assert not command.rstrip().endswith((".sh", ".bash"))

    @pytest.mark.parametrize(
        "edit",
        [
            {"build_id": 'ingest"; import os; os.system("id") #'},
            {"build_id": "../../etc/passwd"},
            {"build_id": "ingest_subscriptions\n"},
            {"contract_id": "x'); import os #"},
            {"contract_id": "bronze.customer_subscriptions\n"},
            {"trigger": {"schedule": "0 * * * *'); import os #"}},
            {"trigger": {"schedule": "{{ var.value.cron }}"}},
            {"trigger": {"schedule": "every four hours"}},
            {"trigger": {"schedule": "0 * * * $(id)"}},
            # Measured: each exited 0 and wrote a DAG that Airflow 3.3.2 refused
            # to import (AirflowTimetableInvalid / "Invalid day_of_week value").
            {"trigger": {"schedule": "99 * * * *"}},
            {"trigger": {"schedule": "foo bar baz qux quux"}},
            {"trigger": {"schedule": "0 */4 * * MON#9"}},
            # Loads, but croniter never finds a next run for it.
            {"trigger": {"schedule": "0 0 1 * MON#2"}},
            # Quartz puts seconds first, croniter last: refused, not guessed.
            {"trigger": {"schedule": "0 0 */4 * * *"}},
            {"trigger": {"timezone": "UTC'), __import__('os').system('id') #"}},
            {"trigger": {"timezone": "Mars/Olympus_Mons"}},
        ],
    )
    def test_hostile_contract_values_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Dict[str, Any]
    ) -> None:
        write_project(tmp_path, _contract(**edit))
        assert _generate(tmp_path, monkeypatch) == 2
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize(
        "argv",
        [
            ("--env", "aws;id"),
            ("--env", "{{ var.value.env }}"),
            # ``$`` matched before a final newline: 'aws\n' was baked into
            # the DAG, and fluid apply found no overlay named that.
            ("--env", "aws\n"),
            ("--contract-path", DEMO_CONTRACT_PATH + "\n"),
            ("--contract-path", "../../etc/passwd"),
            ("--contract-path", "/etc/passwd"),
            ("--contract-path", "contracts/$(id)/contract.fluid.yaml"),
            ("--contract-path", "contracts/.hidden/contract.fluid.yaml"),
        ],
    )
    def test_hostile_generation_inputs_are_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: tuple
    ) -> None:
        write_project(tmp_path)
        assert _generate(tmp_path, monkeypatch, *argv) == 2
        assert not (tmp_path / "out").exists()


class TestTriggerSemantics:
    def test_timezone_and_retries_come_from_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contract = _contract(
            trigger={"timezone": "Europe/Paris", "schedule": "@daily"},
            execution={"retries": {"maxAttempts": 5}},
        )
        write_project(tmp_path, contract)
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch)
        assert loaded.dag["schedule"] == "@daily"
        assert loaded.dag["start_date"][2] == "Europe/Paris"
        assert loaded.dag["default_args"]["retries"] == 4

    def test_only_builds_a_cron_dag_can_express_are_scheduled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def build(build_id: str, execution: Dict[str, Any]) -> Dict[str, Any]:
            return {"id": build_id, "pattern": "embedded-logic", "execution": execution}

        cron = {"trigger": {"type": "schedule", "schedule": "0 1 * * *"}}
        contract = _contract(
            builds=[
                build("nightly", cron),
                build("hourly_cron_key", {"trigger": {"cron": "0 * * * *"}}),
                build("opted_out", {**cron, "orchestration": {"engine": "none"}}),
                build("on_dagster", {**cron, "orchestration": {"engine": "dagster"}}),
                build("timetable", {"trigger": {"type": "timetable", "schedule": "0 1 * * *"}}),
                build("manual", {"trigger": {"type": "manual"}}),
            ]
        )
        write_project(tmp_path, contract)
        assert _generate(tmp_path, monkeypatch) == 0
        assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
            "hourly_cron_key_dag.py",
            "nightly_dag.py",
        ]

    def test_retries_are_capped_at_ten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_project(tmp_path, _contract(execution={"retries": {"maxAttempts": 50}}))
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch)
        assert loaded.dag["default_args"]["retries"] == 10

    @pytest.mark.parametrize(
        "cron",
        [
            "0 */4 * * *",
            "*/15 9-17 * * MON-FRI",
            "30 2 1,15 jan-jun ?",
            "0 0 L * *",
            "0 6 ? * mon#1",
            "0 0 * * 7",
            "50-10 22-2 * dec-feb fri-mon",
        ],
    )
    def test_crons_airflow_accepts_are_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cron: str
    ) -> None:
        write_project(tmp_path, _contract(trigger={"schedule": cron}))
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch)
        assert loaded.dag["schedule"] == cron

    def test_the_timezone_grammar_holds_where_zoneinfo_cannot_judge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A host with no tz database: every lookup misses and no zone is
        # listed, so only the grammar stands between the contract and the DAG.
        def missing(key: str) -> None:
            raise zoneinfo.ZoneInfoNotFoundError(key)

        monkeypatch.setattr(zoneinfo, "ZoneInfo", missing)
        monkeypatch.setattr(zoneinfo, "available_timezones", set)
        write_project(tmp_path, _contract(trigger={"timezone": "UTC'); import os #"}))
        assert _generate(tmp_path, monkeypatch) == 2
        assert not (tmp_path / "out").exists()

        project = tmp_path / "plain"
        write_project(project, _contract(trigger={"timezone": "Europe/Paris"}))
        assert _generate(project, monkeypatch) == 0


def _literal_connection(contract: str = DEMO_CONTRACT) -> str:
    """The demo contract with a connection that names no variable at all."""
    doc = yaml.safe_load(contract)
    doc["builds"][0]["properties"]["source"]["connection"] = {
        "host": "db.internal",
        "port": 5432,
        "database": "app",
        "user": "fluid",
    }
    return yaml.safe_dump(doc, sort_keys=False)


#: Variables the ``fluid apply`` code path reads that a scheduled run does not
#: need, so the worker environment does not pass them.
NOT_PASSED = {
    "PRODUCTION": "only chooses a --safe-mode tip in the CLI banner",
    "SOURCE_DATE_EPOCH": "tar mtimes for `fluid bundle`, which a scheduled apply never runs",
    "USERPROFILE": "the Windows home directory; the DAG task runs under bash",
    "VIRTUAL_ENV": (
        "names the worker's own environment (Airflow's, in the apache/airflow image), "
        "which fluid's python runner would then run builds with"
    ),
}

_ENV_READERS = {"os.getenv", "getenv", "os.environ.get", "environ.get", "os.environ.pop"}
_ENV_READERS |= {"os.environ.setdefault", "_env_int"}


def _apply_path_modules() -> List[Path]:
    """Every fluid_build module ``fluid apply`` can import: the static import
    closure of the apply command, of every build runner and of every provider
    (runners and providers are imported by name at run time)."""
    package = Path(fluid_build.__file__).parent
    root = package.parent

    def to_file(module: str) -> Any:
        base = root.joinpath(*module.split("."))
        for candidate in (base / "__init__.py", base.with_suffix(".py")):
            if candidate.is_file():
                return candidate
        return None

    todo = [package / "cli" / "apply.py"]
    for sub in ("build_runners", "providers"):
        todo.extend((package / sub).rglob("*.py"))
    seen: Set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        parts = list(path.relative_to(root).with_suffix("").parts)
        pkg = parts[:-1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = pkg[: len(pkg) - node.level + 1] if node.level else []
                module = ".".join(base + ([node.module] if node.module else []))
                names = [module] + [f"{module}.{alias.name}" for alias in node.names]
            for name in names:
                if not name.startswith("fluid_build"):
                    continue
                dotted = name.split(".")
                for depth in range(1, len(dotted) + 1):
                    found = to_file(".".join(dotted[:depth]))
                    if found is not None and found not in seen:
                        todo.append(found)
    return sorted(seen)


def _env_names_read(paths: List[Path]) -> Set[str]:
    """Literal variable names read through os.environ / os.getenv (or the
    catalog registrars' ``_env`` helpers) in *paths*."""

    def literal(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    names: Set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            found: List[Any] = []
            if isinstance(node, ast.Call) and node.args:
                reader = ast.unparse(node.func)
                if reader == "_env":  # _env(*names): the first one set wins
                    found = [literal(arg) for arg in node.args]
                elif reader in _ENV_READERS:  # any later argument is a default
                    found = [literal(node.args[0])]
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                found = [literal(node.slice)]
            elif (
                isinstance(node, ast.Compare)
                and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and ast.unparse(node.comparators[0]) == "os.environ"
            ):
                found = [literal(node.left)]
            names.update(n for n in found if isinstance(n, str) and n.isidentifier())
    return names


class TestTheWorkerPassesWhatFluidReads:
    """Measured: a contract that names its password as ``secretRef:
    env://PGPASSWORD`` (schema-valid, and what the schema recommends) got a
    DAG whose run started fluid without PGPASSWORD, and fluid's resolver
    failed with "environment variable not set". The dbt profiles read
    PGPASSWORD, REDSHIFT_* and ATHENA_* straight from the environment and
    were dropped too."""

    @pytest.mark.parametrize("name", ["PGPASSWORD", "ORDERS_DB_PASSWORD"])
    def test_a_secret_ref_resolves_on_the_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        templated = 'password: "{{ env.PGPASSWORD }}"'  # pragma: allowlist secret
        contract = DEMO_CONTRACT.replace(templated, f'secretRef: "env://{name}"')
        write_project(tmp_path, contract)
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch)
        assert name in loaded.namespace["CONTRACT_ENV_NAMES"].split()

        resolve = (
            "from fluid_build.build_runners._acquisition_common import resolve_secret_ref\n"
            f"ok = resolve_secret_ref('env://{name}') == 'pg-secret'\n"
            "(RECORD / 'resolved').write_text(str(ok))\n"
        )
        run = run_task(
            loaded.tasks[0],
            tmp_path,
            {"FLUID_PROJECT_DIR": str(tmp_path), name: "pg-secret"},
            stub=python_stub(resolve),
        )
        assert run.returncode == 0, run.stderr
        assert (tmp_path / "record" / "resolved").read_text() == "True"

    def test_a_dbt_postgres_build_gets_its_connection_from_the_worker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dbt_build = {
            "id": "transform_orders",
            "pattern": "embedded-logic",
            "engine": "dbt",
            "execution": {
                "trigger": {"type": "schedule", "schedule": "0 3 * * *"},
                "runtime": {"platform": "postgres", "resources": {"schema": "analytics"}},
            },
        }
        write_project(tmp_path, _contract(builds=[dbt_build]))
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / "transform_orders_dag.py").read_text(), monkeypatch)
        assert loaded.namespace["CONTRACT_ENV_NAMES"] == ""

        profile = (
            "import json, yaml\n"
            "from fluid_build.build_runners.dbt.profiles import _build_generated_dbt_profile\n"
            "build = yaml.safe_load(open(sys.argv[2]))['builds'][0]\n"
            "profile = _build_generated_dbt_profile(build, {'profile': 'orders'})\n"
            "(RECORD / 'profile.json').write_text(json.dumps(profile))\n"
        )
        run = run_task(
            loaded.tasks[0],
            tmp_path,
            {
                "FLUID_PROJECT_DIR": str(tmp_path),
                "PGHOST": "db.internal",
                "PGPORT": "5433",
                "PGUSER": "fluid",
                "PGPASSWORD": "pg-secret",  # pragma: allowlist secret
                "PGDATABASE": "warehouse",
                "PGSSLMODE": "require",
            },
            stub=python_stub(profile),
        )
        assert run.returncode == 0, run.stderr
        written = json.loads((tmp_path / "record" / "profile.json").read_text())
        assert written["orders"]["outputs"]["dev"] == {
            "type": "postgres",
            "host": "db.internal",
            "user": "fluid",
            "password": "pg-secret",  # pragma: allowlist secret
            "port": 5433,
            "dbname": "warehouse",
            "schema": "analytics",
            "threads": 4,
            "sslmode": "require",
        }

    def test_every_variable_the_apply_path_reads_reaches_fluid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        read = _env_names_read(_apply_path_modules())
        # The scan found the code it is meant to read, and every exception
        # is still something fluid reads.
        assert {"PGPASSWORD", "REDSHIFT_PASSWORD", "VAULT_TOKEN", "AWS_REGION"} <= read
        assert set(NOT_PASSED) <= read

        write_project(tmp_path, _literal_connection())
        assert _generate(tmp_path, monkeypatch) == 0
        loaded = load_dag((tmp_path / "out" / DAG_FILE).read_text(), monkeypatch)
        assert loaded.namespace["CONTRACT_ENV_NAMES"] == ""
        worker = {name: "C" for name in read if name not in ("PATH", "HOME")}
        run = run_task(loaded.tasks[0], tmp_path, {**worker, "FLUID_PROJECT_DIR": str(tmp_path)})
        assert run.returncode == 0, run.stderr
        dropped = sorted(read - set(NOT_PASSED) - set(run.env))
        assert not dropped, (
            f"fluid reads {dropped} but a scheduled run would not pass them: add them to "
            "PASSTHROUGH_PREFIXES or PASSTHROUGH_NAMES in "
            "fluid_build/schedulers/airflow/fluid_apply.py, or to NOT_PASSED here with "
            "the reason a scheduled apply does not need them"
        )
