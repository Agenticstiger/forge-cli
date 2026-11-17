# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
import shlex

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
    out = AirflowScheduler().generate(
        contract, provider=provider, provider_config={"allow_unmapped": True}
    )
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
    (code,) = (
        AirflowScheduler()
        .generate(contract, provider="snowflake", provider_config={"allow_unmapped": True})
        .values()
    )
    assert_inert(code)


# ---------------------------------------------------------------------------
# Every provider's operators, through the one renderer
# ---------------------------------------------------------------------------


def generate_airflow_dag(contract, provider, **config):
    from fluid_build.schedulers.airflow import render_dag

    return render_dag(contract, provider=provider, provider_config=config, allow_unmapped=True)


def test_gcp_codegen_is_injection_safe():

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
    assert_inert(generate_airflow_dag(contract, "gcp", project="my-project", region="us-central1"))


def test_snowflake_codegen_is_injection_safe():

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
    assert_inert(
        generate_airflow_dag(
            contract, "snowflake", account=POC_SQUOTE, database=POC_SQL, warehouse=POC_SQUOTE
        )
    )


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
    (code,) = (
        AirflowScheduler()
        .generate(contract, provider="snowflake", provider_config={"allow_unmapped": True})
        .values()
    )
    assert_inert(code)
    assert (
        "load = SQLExecuteQueryOperator(" in code
    )  # snowflake provider 6.0 removed SnowflakeOperator
    assert "sql='SELECT 1'" in code
    assert "task_id='load'" in code


# ---------------------------------------------------------------------------
# AWS provider-local generator
#
# The AWS generator was the only one of the four Airflow emitters that never
# imported ``codegen_utils``; it hand-quoted every value as ``'{value}'``. The
# cases below pin both halves of the fix: the payload must stay inert, and the
# emitted DAG must not reference a name it never defines (Airflow executes the
# module at parse time, so a ``NameError`` breaks the DAG just as surely).
# ---------------------------------------------------------------------------

_PY_BUILTINS = set(dir(__builtins__)) | set(vars(__import__("builtins")))


def _bound_names(tree: ast.Module) -> set:
    """Every name the generated module binds at any scope."""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                bound.update(
                    a.arg
                    for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                    + [args.vararg, args.kwarg]
                    if a is not None
                )
        elif isinstance(node, ast.Lambda):
            a = node.args
            bound.update(
                x.arg
                for x in [*a.posonlyargs, *a.args, *a.kwonlyargs] + [a.vararg, a.kwarg]
                if x is not None
            )
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def assert_no_undefined_names(code: str) -> None:
    """Assert the generated module never loads a name it does not define.

    Catches the whole class of "generated DAG explodes at Airflow parse time":
    a missing ``from zoneinfo import ZoneInfo``, a helper function the emitter
    calls but never emits, and raw JSON spliced in as a Python expression
    (``true``/``false``/``null`` are not Python names).
    """
    tree = ast.parse(code)
    bound = _bound_names(tree) | _PY_BUILTINS
    undefined = sorted(
        {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound
        }
    )
    assert not undefined, f"generated DAG references undefined names {undefined}\n---\n{code}"


def _aws_contract(**orchestration):
    base = {
        "timezone": POC_SQUOTE,
        "schedule": POC_SQUOTE,
        "tasks": [
            {
                "taskId": POC_IDENT,
                "type": "provider_action",
                "action": "aws.athena.execute_query",
                "params": {
                    "query": POC_SQL,
                    "database": POC_SQUOTE,
                    "outputLocation": POC_SQL,
                },
            },
            {
                "taskId": "t_s3",
                "type": "provider_action",
                "action": "aws.s3.ensure_bucket",
                "params": {"bucket": POC_SQL, "region": POC_SQUOTE},
                "dependsOn": [POC_IDENT],
            },
            {
                "taskId": "t_glue_db",
                "type": "provider_action",
                "action": "aws.glue.ensure_database",
                "params": {"database": POC_SQL},
            },
            {
                "taskId": "t_glue_tbl",
                "type": "provider_action",
                "action": "aws.glue.ensure_table",
                "params": {
                    "database": POC_SQL,
                    "table": POC_SQUOTE,
                    # a bool proves raw json.dumps splicing is gone: Python has
                    # no bare ``true``
                    "partitioned": True,
                },
            },
            {
                "taskId": "t_redshift",
                "type": "provider_action",
                "action": "aws.redshift.execute_statement",
                "params": {"sql": POC_SQL, "clusterIdentifier": POC_SQUOTE},
            },
            {
                "taskId": "t_lambda",
                "type": "provider_action",
                "action": "aws.lambda.invoke",
                "params": {"functionName": POC_SQL, "payload": {"k": POC_SQL, "flag": False}},
            },
            {
                "taskId": "t_fallback",
                "type": "provider_action",
                "action": "aws.unknown.thing",
                "params": {"anything": POC_SQL, "nested": {"deep": True}},
            },
        ],
    }
    base.update(orchestration)
    return {"id": POC_SQUOTE, "name": POC_SQL, "orchestration": base}


def _aws_dag(contract):
    return generate_airflow_dag(contract, "aws", account_id="123456789012", region="eu-west-1")


def test_aws_codegen_is_injection_safe():
    assert_inert(_aws_dag(_aws_contract()))


def test_aws_codegen_emits_no_undefined_names():
    """Classic path: ZoneInfo, the glue/provider helpers, and JSON booleans."""
    assert_no_undefined_names(_aws_dag(_aws_contract()))


def test_aws_benign_sql_with_apostrophe_still_parses():
    """The bug users hit first: a perfectly ordinary quoted SQL literal.

    ``WHERE c = 'a'`` closed the hand-written ``query='...'`` and produced a
    DAG that would not parse — no attacker required.
    """
    contract = {
        "id": "demo_product",
        "name": "Demo",
        "orchestration": {
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "tasks": [
                {
                    "taskId": "q1",
                    "type": "provider_action",
                    "action": "aws.athena.execute_query",
                    "params": {
                        "query": "SELECT * FROM t WHERE c = 'a'",
                        "database": "db",
                        "outputLocation": "s3://b/o/",
                    },
                }
            ],
        },
    }
    code = _aws_dag(contract)
    tree = ast.parse(code)
    # the SQL survives intact as inert data, quote and all
    literals = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "SELECT * FROM t WHERE c = 'a'" in literals


def test_aws_round_trips_payload_as_inert_data():
    """Escaping, not stripping: the payload must still be readable as data."""
    code = _aws_dag(_aws_contract())
    tree = ast.parse(code)
    literals = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert POC_SQL in literals, "query payload was mangled rather than escaped"


def test_aws_task_id_collisions_do_not_merge_tasks():
    """``load-orders`` and ``load.orders`` must stay two distinct variables."""
    contract = {
        "id": "c",
        "name": "c",
        "orchestration": {
            "tasks": [
                {
                    "taskId": "load-orders",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "a"},
                },
                {
                    "taskId": "load.orders",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                },
            ]
        },
    }
    code = _aws_dag(contract)
    tree = assert_inert(code)
    # Only module-level assignments: the emitted helper functions legitimately
    # reuse local names such as ``client`` across their separate bodies.
    assigned = [
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    ]
    operator_vars = [n for n in assigned if n not in {"default_args", "dag", "logger"}]
    assert len(set(operator_vars)) == len(operator_vars), f"task vars collided: {operator_vars}"
    assert len(operator_vars) == 2, f"expected both tasks emitted, got {operator_vars}"


def test_aws_dep_on_non_dag_task_is_skipped_not_synthesised():
    """A dependsOn naming a filtered-out task must not invent a variable.

    Only ``provider_action`` tasks reach the DAG. Synthesising a name for any
    other dep either emitted an undefined name (``NameError`` at import kills
    every task in the file) or, because ``sanitize_identifier`` is not
    injective, silently wired the edge to a *different* real task.
    """
    contract = {
        "id": "c",
        "name": "c",
        "orchestration": {
            "tasks": [
                {"taskId": "prep", "type": "python", "action": "noop", "params": {}},
                {
                    "taskId": "make_bucket",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                    "dependsOn": ["prep"],
                },
            ]
        },
    }
    code = _aws_dag(contract)
    assert_no_undefined_names(code)
    assert "prep >>" not in code
    assert "skipped" in code


def test_task_id_cannot_rebind_emitted_module_names():
    """A taskId of ``dag`` must not overwrite the DAG object itself."""
    from fluid_build.providers.common.codegen_utils import unique_task_identifiers
    from fluid_build.schedulers.airflow._operators import RESERVED_NAMES

    m = unique_task_identifiers(
        [{"taskId": "dag"}, {"taskId": "_ensure_glue_table"}, {"taskId": "GlueJobOperator"}],
        reserved=RESERVED_NAMES,
    )
    assert all(m[k] != k for k in m)


# ---------------------------------------------------------------------------
# GCP create_table schema, and `fluid init`'s basic DAG
# ---------------------------------------------------------------------------


def test_gcp_create_table_schema_is_injection_safe():
    """The schema reaches both create-table operators as a literal: as ``schema_fields``
    and inside ``table_resource``."""
    hostile = [{"name": POC_SQL, "type": POC_SQUOTE, "description": POC_IDENT, POC_SQL: 1}]
    contract = {
        "id": "p",
        "name": "p",
        "orchestration": {
            "tasks": [
                {
                    "taskId": POC_IDENT,
                    "type": "provider_action",
                    "action": "gcp.bigquery.create_table",
                    "params": {"dataset_id": POC_SQL, "table_id": POC_SQUOTE, "schema": hostile},
                }
            ]
        },
    }
    code = generate_airflow_dag(contract, "gcp", project="my-project", region="us-central1")
    assert_inert(code)
    assert "BigQueryCreateTableOperator(" in code and "BigQueryCreateEmptyTableOperator(" in code


def test_init_basic_dag_retries_are_numbers_not_code(tmp_path):
    """``orchestration.retries`` and ``retry_delay`` used to be spliced into the DAG source
    as text, so a contract value ran at DAG-parse time."""
    import logging

    from fluid_build.cli.init import create_basic_dag

    contract = {
        "name": "p",
        "orchestration": {
            "schedule": "@daily",
            "retries": "3, __import__('os').system('touch /tmp/PWNED4')",
            "retry_delay": "5) or __import__('os').system('touch /tmp/PWNED5') or (5",
        },
    }
    create_basic_dag(tmp_path, contract, logging.getLogger(__name__))
    (dag,) = (tmp_path / "dags").glob("*.py")
    code = dag.read_text()
    assert_inert(code)
    assert "'retries': 3," in code and "timedelta(minutes=5)" in code
    assert "PWNED" not in code


def test_init_basic_dag_infinite_retries_fall_back(tmp_path):
    """``int(float('inf'))`` raises OverflowError, which skipped the DAG instead."""
    import logging

    from fluid_build.cli.init import create_basic_dag

    create_basic_dag(
        tmp_path,
        {"name": "p", "orchestration": {"retries": float("inf")}},
        logging.getLogger(__name__),
    )
    (dag,) = (tmp_path / "dags").glob("*.py")
    assert "'retries': 3," in dag.read_text()
