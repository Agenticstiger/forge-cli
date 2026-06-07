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

"""Tests for ``snowflake.orchestration.airflow_generator``.

Pins that the generated Airflow DAG is syntactically valid Python even
when a task's SQL body contains quote characters.

``_generate_provider_action_task`` interpolates the SQL into a
triple-double-quoted operator argument. A literal triple-double-quote
run in the SQL body would close that string early and emit broken
Python. A prior bug discarded the escape expression's result and
interpolated the raw SQL; this module is the regression guard that the
escape is applied and the SQL round-trips unchanged.
"""

from __future__ import annotations

import ast

import pytest

from fluid_build.providers.snowflake.orchestration import airflow_generator

pytestmark = pytest.mark.unit


def _dag_for_sql(sql: str) -> str:
    """Generate a one-task Airflow DAG whose SnowflakeOperator runs ``sql``."""
    contract = {"id": "test.airflow.dag"}
    orchestration = {
        "tasks": [
            {
                "name": "run_query",
                "type": "provider_action",
                "action": "sf.sql.execute",
                "parameters": {"sql": sql},
            }
        ]
    }
    return airflow_generator.generate_airflow_dag(contract, orchestration)


def _operator_sql_arg(code: str) -> str:
    """Parse the generated DAG; return the SnowflakeOperator ``sql`` literal."""
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "sql" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
    raise AssertionError("no operator sql= argument found in generated DAG")


class TestAirflowGeneratorQuoteSafety:
    """The generated DAG must stay valid Python when the SQL body has quotes."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT 'single' FROM t",
            'SELECT "double" FROM t',
            'SELECT """triple double""" FROM t',
            "note with ''' triple single ''' inside",
            'edge """""" six quotes then a tail "',
        ],
    )
    def test_generated_dag_compiles(self, sql: str) -> None:
        # A SyntaxError here means the SQL body broke the generated file.
        compile(_dag_for_sql(sql), "<generated_dag>", "exec")

    def test_triple_quote_sql_round_trips(self) -> None:
        sql = 'comment """ here """ done'
        code = _dag_for_sql(sql)
        compile(code, "<generated_dag>", "exec")
        # The escaped triple-quote decodes back to the original SQL verbatim.
        assert sql in _operator_sql_arg(code)

    def test_triple_quote_is_escaped_not_discarded(self) -> None:
        # Regression guard: the prior code discarded the escape result and
        # interpolated the raw SQL, so an embedded triple-quote terminated
        # the operator string. The SQL is now emitted via ``py_str_literal``
        # (``repr``), which is mechanism-agnostic about quote style — for a
        # body containing only double-quotes repr picks single quotes, so the
        # triple-double-quote run survives verbatim *inside* a single-quoted
        # literal rather than being backslash-escaped. The security property
        # is unchanged: the generated DAG stays valid Python and the SQL
        # round-trips exactly, with no bare triple-quote terminator escaping
        # the operator argument.
        sql = 'val = """boom"""'
        code = _dag_for_sql(sql)
        compile(code, "<generated_dag>", "exec")
        # The full SQL (triple-quotes included) decodes back verbatim from the
        # parsed operator argument — proving it never terminated the literal.
        assert sql in _operator_sql_arg(code)


# ---------------------------------------------------------------------------
# FIX 1/2 — DDL identifier + type allowlisting at the SQL-content boundary
#
# The generated SnowflakeOperator runs this SQL at DAG *runtime*, so the
# Python-layer ``py_str_literal`` wrapping is not enough: a malicious
# identifier / column type must be rejected at generation time and never
# reach the emitted SQL string.
# ---------------------------------------------------------------------------

from fluid_build.providers._sql_safety import (  # noqa: E402
    SqlTypeError,
    validate_sql_type_name,
)
from fluid_build.providers.snowflake.codegen import airflow as codegen_airflow  # noqa: E402


class TestSqlTypeNameAllowlist:
    """``validate_sql_type_name`` accepts real column types, rejects injection."""

    @pytest.mark.parametrize(
        "type_name",
        ["INT", "VARCHAR(255)", "NUMBER(18,4)", "TIMESTAMP_NTZ", "TIMESTAMP WITHOUT TIME ZONE"],
    )
    def test_accepts_legitimate_types(self, type_name: str) -> None:
        assert validate_sql_type_name(type_name) == type_name.strip()

    @pytest.mark.parametrize(
        "type_name",
        [
            'INT"); DROP TABLE x; --',  # quote + statement terminator + comment
            "INT; DROP TABLE x",  # statement terminator
            "INT) , evil VARCHAR(1)) --",  # comment sequence
            "INT /* c */",  # block comment
            'VARCHAR(10)"',  # bare identifier quote
            "INT-1",  # hyphen excluded
            "",  # empty
            None,  # non-str
        ],
    )
    def test_rejects_injection(self, type_name) -> None:
        with pytest.raises(SqlTypeError):
            validate_sql_type_name(type_name)


def _codegen_create_table_dag(table: str, columns: list) -> str:
    """One-task DAG via the ``codegen/airflow`` create_table operation path."""
    contract = {
        "id": "t.dag",
        "orchestration": {
            "tasks": [
                {
                    "taskId": "make_tbl",
                    "type": "provider_action",
                    "action": "snowflake.table.create_table",
                    "params": {
                        "database": "ANALYTICS",
                        "schema": "PUBLIC",
                        "table": table,
                        "columns": columns,
                    },
                }
            ]
        },
    }
    return codegen_airflow.generate_airflow_dag(contract, account="acct", database="ANALYTICS")


class TestCodegenCreateTableDDLSafety:
    """``codegen/airflow.py`` create_table: identifiers + types are validated."""

    def test_malicious_table_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            _codegen_create_table_dag('orders"); DROP TABLE x; --', [])

    @pytest.mark.parametrize("bad_type", ['INT"); DROP TABLE x; --', "INT; SELECT 1", "INT)--"])
    def test_malicious_column_type_rejected(self, bad_type: str) -> None:
        with pytest.raises(ValueError):
            _codegen_create_table_dag("orders", [{"name": "id", "type": bad_type}])

    def test_malicious_column_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            _codegen_create_table_dag(
                "orders", [{"name": 'id" INT); DROP TABLE x; --', "type": "INT"}]
            )

    def test_normal_create_table_emits_validated_identifiers(self) -> None:
        code = _codegen_create_table_dag(
            "orders", [{"name": "id", "type": "INT"}, {"name": "amount", "type": "NUMBER(18,4)"}]
        )
        compile(code, "<dag>", "exec")
        sql = _operator_sql_arg(code)
        assert "CREATE TABLE IF NOT EXISTS ANALYTICS.PUBLIC.orders" in sql
        assert "id INT" in sql
        assert "amount NUMBER(18,4)" in sql
        # No injection payload survived anywhere in the generated source.
        assert "DROP TABLE" not in code


def _generator_table_dag(params: dict) -> str:
    """One-task DAG via ``orchestration/airflow_generator`` sf.table.ensure path."""
    contract = {"id": "t.dag"}
    orchestration = {
        "tasks": [
            {"name": "ensure_tbl", "type": "provider_action", "action": "sf.table.ensure", **params}
        ]
    }
    return airflow_generator.generate_airflow_dag(contract, orchestration)


class TestGeneratorCreateTableDDLSafety:
    """``orchestration/airflow_generator.py`` create-table DDL is validated."""

    def test_malicious_table_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            _generator_table_dag(
                {"parameters": {"database": "db", "schema": "s", "table": 'x" ; DROP TABLE y --'}}
            )

    @pytest.mark.parametrize("bad_type", ['INT"); DROP TABLE x; --', "INT; SELECT 1"])
    def test_malicious_column_type_rejected(self, bad_type: str) -> None:
        with pytest.raises(ValueError):
            _generator_table_dag(
                {
                    "parameters": {
                        "database": "db",
                        "schema": "s",
                        "table": "t",
                        "columns": [{"name": "c", "type": bad_type}],
                    }
                }
            )

    def test_normal_create_table_emits_quoted_validated_identifiers(self) -> None:
        code = _generator_table_dag(
            {
                "parameters": {
                    "database": "db",
                    "schema": "s",
                    "table": "orders",
                    "columns": [
                        {"name": "id", "type": "INT"},
                        {"name": "amount", "type": "NUMBER(18,4)"},
                    ],
                }
            }
        )
        compile(code, "<dag>", "exec")
        sql = _operator_sql_arg(code)
        assert 'CREATE TABLE IF NOT EXISTS "db"."s"."orders"' in sql
        assert '"id" INT' in sql
        assert '"amount" NUMBER(18,4)' in sql
        assert "DROP TABLE" not in code


# ---------------------------------------------------------------------------
# FIX 3 — task var name in the DEFINITION must match the DEPENDENCY wiring
# even when taskId is missing (codegen/airflow.py).
# ---------------------------------------------------------------------------


def _assignment_targets(code: str) -> set:
    """Top-level ``<name> = ...`` assignment target identifiers in ``code``."""
    targets = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    targets.add(tgt.id)
    return targets


class TestCodegenMissingTaskIdConsistency:
    """A python task with NO taskId: def var == wiring var, never ``>> None``."""

    def test_missing_taskid_def_and_wiring_agree(self) -> None:
        contract = {
            "id": "dep.dag",
            "orchestration": {
                "tasks": [
                    {
                        "taskId": "upstream",
                        "type": "provider_action",
                        "action": "snowflake.table.query",
                        "params": {"sql": "SELECT 1"},
                    },
                    {
                        # No taskId -> must normalise to ``unnamed_task`` in BOTH
                        # the operator definition and the ``>>`` wiring. The
                        # ``python`` service routes to PythonOperator; the task
                        # must be type=provider_action to survive the generator's
                        # ``provider_action`` filter.
                        "type": "provider_action",
                        "action": "snowflake.python.run",
                        "params": {"x": 1},
                        "dependsOn": ["upstream"],
                    },
                ]
            },
        }
        code = codegen_airflow.generate_airflow_dag(contract, account="a", database="d")
        compile(code, "<dag>", "exec")
        # The downstream variable used in the ``>>`` edge must be one that is
        # actually defined — and specifically not the literal ``None``.
        assert "unnamed_task = PythonOperator(" in code
        assert ">> None" not in code
        assert "upstream >> unnamed_task" in code
        # Every name referenced on either side of a ``>>`` edge is a defined var.
        defined = _assignment_targets(code)
        assert "unnamed_task" in defined
        assert "None" not in defined


# ---------------------------------------------------------------------------
# FIX 4 — a contract param that parses to a date/datetime must not crash
# DAG generation (json.dumps default=str), in BOTH snowflake generators.
# ---------------------------------------------------------------------------


class TestPythonTaskDateParamSerialises:
    """``json.dumps(..., default=str)`` so a date param doesn't raise."""

    def test_codegen_python_task_with_date_param(self) -> None:
        import datetime

        contract = {
            "id": "d.dag",
            "orchestration": {
                "tasks": [
                    {
                        "taskId": "py",
                        "type": "provider_action",
                        "action": "snowflake.python.run",
                        "params": {"as_of": datetime.date(2024, 1, 1)},
                    }
                ]
            },
        }
        # Must not raise TypeError: "Object of type date is not JSON serializable".
        code = codegen_airflow.generate_airflow_dag(contract, account="a", database="d")
        compile(code, "<dag>", "exec")
        assert "2024-01-01" in code

    def test_generator_python_task_with_datetime_param(self) -> None:
        import datetime

        contract = {"id": "d.dag"}
        orchestration = {
            "tasks": [
                {
                    "name": "py",
                    "type": "python",
                    "callable": "my_func",
                    "parameters": {"as_of": datetime.datetime(2024, 1, 1, 12, 0, 0)},
                }
            ]
        }
        # ``airflow_generator``'s python task doesn't serialise params into the
        # body, but exercise the path end-to-end to prove a date param is inert.
        code = airflow_generator.generate_airflow_dag(contract, orchestration)
        compile(code, "<dag>", "exec")


# Code-injection payload: closes a single-quoted literal, runs an os.system,
# comments out the trailing quote. The marker file path is asserted-absent.
_PWN = 'import os; os.system("touch /tmp/PWNED")'
_NL = "\n"


def _assert_no_injection(code: str) -> None:
    """The generated DAG must be valid Python with no executable payload.

    The DAG header legitimately imports ``datetime`` / ``airflow`` modules, so
    we only forbid *dangerous* module imports (``os`` / ``subprocess`` / ...)
    that an injected ``import os; ...`` payload would introduce.
    """
    tree = ast.parse(code)  # raises SyntaxError on any string break-out
    danger_calls = {"system", "popen", "exec", "eval", "__import__", "compile"}
    danger_mods = {"os", "subprocess", "shutil", "socket", "pty"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            assert name not in danger_calls, f"dangerous call {name!r} in generated DAG"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert (
                    alias.name.split(".")[0] not in danger_mods
                ), f"dangerous import {alias.name!r} injected into DAG"
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[
                0
            ] not in danger_mods, f"dangerous import-from {node.module!r} injected into DAG"
    # The payload, if present in the source at all, must be inert: it survives
    # ONLY as string-constant data OR confined to ``#`` comment lines — never
    # as a bare executable statement. (Comments are stripped by ``ast.parse``,
    # so a comment-confined payload won't appear in string constants; that is
    # the safe outcome, not a failure.)
    blob = "".join(
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )
    if "os.system" in code:
        in_constant = 'os.system("touch /tmp/PWNED")' in blob
        # Every physical source line that carries the payload is either a
        # comment or contains a quote (i.e. it's a string literal), never a
        # bare ``import os; os.system(...)`` statement.
        for line in code.splitlines():
            if "os.system" in line:
                stripped = line.strip()
                assert (
                    stripped.startswith("#") or "'" in line or '"' in line
                ), f"payload on a bare executable source line: {line!r}"
        assert in_constant or "# " in code, "payload neither a constant nor a comment"


class TestAirflowGeneratorCodeInjection:
    """Untrusted contract values must never become executable code in the DAG.

    Mechanism-agnostic regression guard for the code-injection hardening:
    every sink (SQL, bash_command, schedule, conn_id, sensor ids, dependency
    edges, error/TODO comments) routes contract content through
    ``py_str_literal`` / ``sanitize_identifier`` so a quote / newline /
    triple-quote payload cannot break out and execute at DAG-parse time.
    """

    def test_bash_command_single_quote_injection_is_inert(self) -> None:
        contract = {"id": "inj.dag"}
        orchestration = {
            "tasks": [
                {"name": "shell", "type": "bash", "command": "x'; " + _PWN + " #"},
            ]
        }
        _assert_no_injection(airflow_generator.generate_airflow_dag(contract, orchestration))

    def test_sql_triple_quote_newline_injection_is_inert(self) -> None:
        contract = {"id": "inj.dag"}
        orchestration = {
            "tasks": [
                {
                    "name": "q",
                    "type": "provider_action",
                    "action": "sf.sql.execute",
                    "parameters": {"sql": 'SELECT 1"""' + _NL + _PWN + _NL + 'y="""'},
                },
            ]
        }
        _assert_no_injection(airflow_generator.generate_airflow_dag(contract, orchestration))

    def test_schedule_and_conn_id_injection_is_inert(self) -> None:
        from fluid_build.providers.snowflake.orchestration.common import (
            OrchestrationConfig,
            OrchestrationEngine,
        )

        cfg = OrchestrationConfig(
            engine=OrchestrationEngine.AIRFLOW,
            dag_id="inj_dag",
            schedule="@daily'; " + _PWN + " #",
            description='d"""' + _NL + _PWN + _NL + 'z="""',
            tags=["t'1", 't"2'],
            default_args={"owner": "o'; " + _PWN, "email": ["e'; " + _PWN], "retries": 3},
            snowflake_conn_id="conn'; " + _PWN + " #",
        )
        orchestration = {
            "tasks": [
                {
                    "name": "q",
                    "type": "provider_action",
                    "action": "sf.sql.execute",
                    "parameters": {"sql": "SELECT 1"},
                },
            ]
        }
        _assert_no_injection(
            airflow_generator.generate_airflow_dag({"id": "x"}, orchestration, cfg)
        )

    def test_sensor_and_dependency_injection_is_inert(self) -> None:
        contract = {"id": "inj.dag"}
        orchestration = {
            "tasks": [
                {"name": "a-b", "type": "bash", "command": "echo hi"},
                {
                    "name": "wait",
                    "type": "sensor",
                    "external_dag_id": 'd"; ' + _PWN,
                    "external_task_id": "t'; " + _PWN,
                    "depends_on": ["a-b"],
                },
            ]
        }
        _assert_no_injection(airflow_generator.generate_airflow_dag(contract, orchestration))

    def test_unknown_action_and_unsupported_type_comments_are_inert(self) -> None:
        contract = {"id": "inj.dag"}
        orchestration = {
            "tasks": [
                # Unknown action -> "# ERROR" comment; newline must not escape it.
                {"name": "bad", "type": "provider_action", "action": "foo" + _NL + _PWN},
                # Unsupported type -> "# TODO" comment; newline must not escape it.
                {"name": "weird" + _NL + _PWN, "type": "mystery" + _NL + _PWN},
            ]
        }
        _assert_no_injection(airflow_generator.generate_airflow_dag(contract, orchestration))
