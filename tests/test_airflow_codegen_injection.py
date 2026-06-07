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

"""Regression tests: untrusted contract values must not be able to inject code
into generated Airflow DAG Python files.

A generated DAG is *executed* by Airflow when it parses the file, so any
contract value that is interpolated into the generated source — a ``taskId``,
``sql``/``query``, ``bucket``, ``name``, ``timezone``, ``schedule``, a
``dependsOn`` edge — must be emitted as an escaped Python literal
(``py_str_literal``) or a sanitised identifier (``sanitize_identifier``), never
wrapped in hand-written quotes.

These tests are mechanism-agnostic: rather than asserting a particular escaping
they parse the generated source and assert (1) it is syntactically valid and
(2) the AST contains no executable call to ``os.system``/``exec``/``eval``/
``__import__`` and no import of ``os``/``subprocess`` — i.e. the payload, if
present, survives only as inert string data.
"""

from __future__ import annotations

import ast

import pytest

# The exact PoC payload from the vulnerability report: a triple-quote that
# would close a ``sql=\"\"\"...\"\"\"`` literal, then a top-level os.system call.
POC_SQL = 'SELECT 1"""\nimport os; os.system("touch /tmp/PWNED")\nx="""'
# Single-quote break-out + newline + statement.
POC_SQUOTE = "x'; import os; os.system('touch /tmp/PWNED2')\ny='"
# A taskId / identifier carrying a newline + statement (LHS / dependency vector).
POC_IDENT = "t_evil\nimport os; os.system('touch /tmp/PWNED3')\nx = "

_DANGEROUS_CALLS = {"system", "popen", "exec", "eval", "__import__", "spawn", "Popen"}
_DANGEROUS_MODULES = {"os", "subprocess", "sys", "shutil", "socket"}


def assert_inert(code: str) -> ast.Module:
    """Assert ``code`` parses and contains no injected executable construct."""
    tree = ast.parse(code)  # raises SyntaxError if a payload broke out
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in _DANGEROUS_CALLS:
                offenders.append(f"call:{name}")
        if isinstance(node, ast.Import):
            offenders.extend(
                f"import:{a.name}" for a in node.names if a.name.split(".")[0] in _DANGEROUS_MODULES
            )
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] in _DANGEROUS_MODULES
        ):
            offenders.append(f"from:{node.module}")
    assert not offenders, f"code injection: generated DAG executes {offenders}\n---\n{code}"
    return tree


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def test_py_str_literal_neutralises_breakouts():
    from fluid_build.providers.common.codegen_utils import py_str_literal

    for payload in (POC_SQL, POC_SQUOTE, "a'b", 'a"b', "a\nb", "a\\b", '"""', "''''"):
        literal = py_str_literal(payload)
        # The literal must eval back to exactly the original (round-trip) and be
        # a single expression — proving it cannot contain an executable stmt.
        assert ast.literal_eval(literal) == payload
    # None / non-str are coerced, never emitted as a raw object repr.
    assert py_str_literal(None) == "''"
    assert ast.literal_eval(py_str_literal(123)) == "123"


def test_escape_for_docstring_cannot_close_docstring():
    from fluid_build.providers.common.codegen_utils import escape_for_docstring

    escaped = escape_for_docstring('x"""\nimport os; os.system("p")\ny')
    wrapped = '"""\n' + escaped + '\n"""'
    assert_inert(wrapped)


def test_sanitize_identifier_is_always_a_legal_name():
    from fluid_build.providers.common.codegen_utils import sanitize_identifier

    for payload in (POC_IDENT, "a-b", "a.b", "a b", "1abc", "", "../../x", "a';b"):
        assert sanitize_identifier(payload).isidentifier()


# ---------------------------------------------------------------------------
# Main provider-aware scheduler (fluid_build/schedulers/airflow)
# ---------------------------------------------------------------------------


def _malicious_contract(action_prefix: str, params_key: str) -> dict:
    return {
        "id": POC_SQUOTE,
        "name": POC_SQL,
        "orchestration": {
            "schedule": "0 2 * * *",
            "timezone": POC_SQUOTE,
            "tasks": [
                {
                    "taskId": POC_IDENT,
                    "type": "provider_action",
                    "action": f"{action_prefix}.query",
                    "params": {params_key: POC_SQL, "bucket": POC_SQL, "query": POC_SQL},
                },
                {
                    "taskId": "t2",
                    "type": "provider_action",
                    "action": f"{action_prefix}.query",
                    "params": {params_key: "SELECT 2"},
                    "dependsOn": [POC_IDENT],
                },
            ],
        },
    }


@pytest.mark.parametrize(
    "provider,prefix",
    [("snowflake", "sf.snowflake"), ("gcp", "gcp.bigquery"), ("aws", "aws.athena")],
)
def test_main_scheduler_provider_tasks_are_injection_safe(provider, prefix):
    from fluid_build.schedulers.airflow import AirflowScheduler

    contract = _malicious_contract(prefix, "sql")
    out = AirflowScheduler().generate(contract, provider=provider)
    (code,) = out.values()
    assert_inert(code)
    assert "touch /tmp/PWNED" in code  # payload preserved as inert data


def test_main_scheduler_exact_poc_defeated():
    """The exact report PoC: snowflake sql triple-quote break-out."""
    from fluid_build.schedulers.airflow import AirflowScheduler

    contract = {
        "id": "p",
        "name": "p",
        "orchestration": {
            "tasks": [
                {
                    "taskId": "t1",
                    "type": "provider_action",
                    "action": "sf.snowflake.query",
                    "params": {"sql": POC_SQL},
                }
            ]
        },
    }
    (code,) = AirflowScheduler().generate(contract, provider="snowflake").values()
    assert_inert(code)


# ---------------------------------------------------------------------------
# Provider-local generators
# ---------------------------------------------------------------------------


def test_gcp_codegen_is_injection_safe():
    from fluid_build.providers.gcp.codegen.airflow import generate_airflow_dag

    contract = {
        "id": POC_SQUOTE,
        "name": POC_SQL,
        "orchestration": {
            "timezone": POC_SQUOTE,
            "tasks": [
                {
                    "taskId": POC_IDENT,
                    "type": "provider_action",
                    "action": "gcp.bigquery.query",
                    "params": {"query": POC_SQL},
                },
                {
                    "taskId": "t_gcs",
                    "type": "provider_action",
                    "action": "gcp.gcs.create_bucket",
                    "params": {"bucket": POC_SQL},
                    "dependsOn": [POC_IDENT],
                },
                {
                    "taskId": "t_flow",
                    "type": "provider_action",
                    "action": "gcp.dataflow.run_template",
                    "params": {"template": POC_SQL, "job_name": POC_SQL},
                },
                {
                    "taskId": "t_py",
                    "type": "provider_action",
                    "action": "gcp.unknown.thing",
                    "params": {"anything": POC_SQL},
                },
            ],
        },
    }
    assert_inert(generate_airflow_dag(contract, "my-project", "us-central1"))


def test_snowflake_codegen_is_injection_safe():
    from fluid_build.providers.snowflake.codegen.airflow import generate_airflow_dag

    contract = {
        "id": POC_SQUOTE,
        "name": POC_SQL,
        "orchestration": {
            "timezone": POC_SQUOTE,
            "tasks": [
                {
                    "taskId": POC_IDENT,
                    "type": "provider_action",
                    "action": "sf.snowflake.query",
                    "params": {"sql": POC_SQL, "warehouse": POC_SQL, "database": POC_SQL},
                },
                {
                    "taskId": "t2",
                    "type": "provider_action",
                    "action": "sf.snowflake.query",
                    "params": {"sql": "SELECT 2"},
                    "dependsOn": [POC_IDENT],
                },
            ],
        },
    }
    assert_inert(generate_airflow_dag(contract, account=POC_SQUOTE, database=POC_SQL))


# ---------------------------------------------------------------------------
# Shared header builder
# ---------------------------------------------------------------------------


def test_generate_file_header_docstring_is_injection_safe():
    from fluid_build.providers.common.codegen_utils import generate_file_header

    header = generate_file_header(
        contract_id='x"""\nimport os; os.system("p")\ny',
        contract_name='n"""\nexec("evil")\nz',
        provider="gcp",
        schedule=POC_SQL,
        timezone=POC_SQUOTE,
    )
    # Header is a bare docstring; wrap it in a module to parse.
    assert_inert(header + "\npass\n")


# ---------------------------------------------------------------------------
# Benign output is unchanged (no false-positive churn)
# ---------------------------------------------------------------------------


def test_benign_contract_still_produces_expected_operators():
    from fluid_build.schedulers.airflow import AirflowScheduler

    contract = {
        "id": "sales",
        "name": "Sales Pipeline",
        "orchestration": {
            "tasks": [
                {
                    "taskId": "load",
                    "type": "provider_action",
                    "action": "sf.snowflake.query",
                    "params": {"sql": "SELECT 1"},
                },
            ]
        },
    }
    (code,) = AirflowScheduler().generate(contract, provider="snowflake").values()
    assert_inert(code)
    assert "load = SnowflakeOperator(" in code
    assert "sql='SELECT 1'" in code
    assert "task_id='load'" in code
