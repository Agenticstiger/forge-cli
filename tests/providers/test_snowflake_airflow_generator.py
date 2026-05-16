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
        # the operator string. The escaped form must reach the output and
        # no bare triple-quote terminator may survive.
        code = _dag_for_sql('val = """boom"""')
        assert r"\"\"\"" in code
        assert '"""boom"""' not in code
        compile(code, "<generated_dag>", "exec")
