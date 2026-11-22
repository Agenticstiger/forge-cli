# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow Variables read from the conventions: ``Variable.get(...)`` and ``{{ var.value.x }}``.

A DAG that takes its environment from a Variable at parse time, or an operator argument that is a
``{{ var.value.x }}`` template, depends on what the Airflow deployment holds. The operator says
what the environment being read holds, per environment, as it does for ``os.environ``; a Variable
nobody named stays unresolved and nothing is invented.
"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

import pytest

from fluid_build._errors import FluidUserError
from fluid_build.cli.import_workflow import _conventions as C
from fluid_build.cli.import_workflow._airflow_dags import fill_variable_templates
from fluid_build.cli.import_workflow._fold import (
    FoldedDict,
    FoldedList,
    FoldedTuple,
    Folder,
    Unresolved,
)
from fluid_build.cli.import_workflow._pysource import parse_module

AF2 = "from airflow.models import Variable\n"


def fold(source: str, **variables: str) -> Any:
    module = parse_module("dag.py", textwrap.dedent(source).encode("utf-8"))
    folder = Folder(module, airflow_variables=variables)
    found = None
    for node in module.tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "x":
            found = node
    assert found is not None
    return folder.fold(found.value)


def why(value: Any) -> str:
    assert isinstance(value, Unresolved), value
    return value.why


@pytest.mark.parametrize(
    "call",
    [
        'Variable.get("environment")',
        'Variable.get("environment", "dev")',  # the default is not assumed: the name is declared
        'Variable.get("environment", default_var="dev")',  # Airflow 2
        'Variable.get("environment", default="dev")',  # Airflow 3, airflow.sdk
        'Variable.get(key="environment")',
        'Variable.get("environment", None, False)',
        'Variable.get("environment", deserialize_json=False)',
    ],
)
def test_a_variable_the_conventions_name_is_what_they_say(call):
    assert fold(f"{AF2}x = {call}\n", environment="prod") == "prod"


@pytest.mark.parametrize(
    "imports",
    [
        "from airflow.models import Variable",
        "from airflow.models.variable import Variable",
        "from airflow.sdk import Variable",
        "from airflow.sdk.definitions.variable import Variable",
        "from airflow.models import Variable as V\nVariable = V",
        "import airflow.models\nVariable = airflow.models.Variable",
        "try:\n    from airflow.sdk import Variable\nexcept ImportError:\n    from airflow.models import Variable",
    ],
)
def test_the_variable_class_is_found_by_where_it_is_imported_from(imports):
    source = f'{imports}\nx = Variable.get("environment")\n'
    value = fold(source, environment="prod")
    if "Variable = " in imports:  # an assignment is a name assigned once: not a class import
        assert isinstance(value, Unresolved)
    else:
        assert value == "prod"


def test_an_alias_of_the_import_is_the_variable_class_too():
    assert (
        fold(
            'from airflow.sdk import Variable as V\nx = V.get("environment")\n', environment="prod"
        )
        == "prod"
    )
    assert (
        fold('import airflow.sdk as sdk\nx = sdk.Variable.get("environment")\n', environment="prod")
        == "prod"
    )


def test_a_variable_nobody_named_stays_unresolved_and_its_default_is_not_assumed():
    value = fold(f'{AF2}x = Variable.get("other", "dev")\n', environment="prod")
    assert why(value) == "the Airflow Variable other"
    assert (
        why(fold(f'{AF2}x = Variable.get("environment")\n')) == "the Airflow Variable environment"
    )


def test_a_value_that_is_empty_is_a_value():
    assert fold(f'{AF2}x = Variable.get("prefix")\n', prefix="") == ""


@pytest.mark.parametrize(
    "call, reason",
    [
        ('Variable.get("environment", deserialize_json=True)', "read as JSON"),
        ('Variable.get("environment", None, True)', "read as JSON"),
        ('Variable.get("environment", deserialize_json=flag)', "read as JSON"),
        ("Variable.get(name)", "name is not a constant"),
        ("Variable.get()", "with no name"),
    ],
)
def test_a_variable_that_cannot_be_read_says_why(call, reason):
    assert reason in why(fold(f"{AF2}x = {call}\n", environment="prod"))


def test_a_class_of_the_project_named_variable_is_not_airflows():
    source = 'from acme.settings import Variable\nx = Variable.get("environment")\n'
    assert "call" in why(fold(source, environment="prod"))


def test_a_variable_is_part_of_what_a_string_is_built_from():
    value = fold(f"{AF2}x = f\"{{Variable.get('environment')}}_orders\"\n", environment="prod")
    assert value == "prod_orders"
    value = fold(
        f'{AF2}ENV = Variable.get("environment")\nx = {{"dev": 1, "prod": 2}}[ENV]\n',
        environment="prod",
    )
    assert value == 2


# ── {{ var.value.x }} in a string ─────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("{{ var.value.env }}", "prod"),
        ("{{var.value.env}}", "prod"),
        ("{{   var.value.env   }}", "prod"),
        ('{{ var.value["env"] }}', "prod"),
        ("{{ var.value['env'] }}", "prod"),
        ("s3://{{ var.value.env }}-bucket/{{ var.value.env }}", "s3://prod-bucket/prod"),
        ("{{ var.value.missing }}", "{{ var.value.missing }}"),  # nobody named it: as written
        ("{{ ds }} {{ var.value.env }}", "{{ ds }} prod"),
        ("{{ var.json.env }}", "{{ var.json.env }}"),
        ("{{ var.value.env | upper }}", "{{ var.value.env | upper }}"),
        ("{{ var.value.env ~ 'x' }}", "{{ var.value.env ~ 'x' }}"),
        ("{{ var.value.get('env') }}", "{{ var.value.get('env') }}"),
        ("no template", "no template"),
        ("{{ var.value.a-b }}", "{{ var.value.a-b }}"),
    ],
)
def test_a_variable_template_is_filled_in_where_the_name_is_declared(text, expected):
    assert fill_variable_templates(text, {"env": "prod"}) == expected


def test_a_template_is_filled_once_not_again_in_what_it_was_filled_with():
    assert (
        fill_variable_templates("{{ var.value.a }}", {"a": "{{ var.value.b }}", "b": "x"})
        == "{{ var.value.b }}"
    )


def test_templates_are_filled_in_what_a_list_a_tuple_and_a_dict_hold():
    pieces = FoldedList(["{{ var.value.env }}", 3, None, ["{{ var.value.env }}"]])
    pieces.complete = False
    filled = fill_variable_templates(pieces, {"env": "prod"})
    assert filled == ["prod", 3, None, ["prod"]]
    assert isinstance(filled, FoldedList) and filled.complete is False
    assert isinstance(
        fill_variable_templates(FoldedTuple(["{{ var.value.env }}"]), {"env": "prod"}), FoldedTuple
    )
    mapped = FoldedDict({"a": "{{ var.value.env }}", "b": {"c": "{{ var.value.env }}"}})
    assert fill_variable_templates(mapped, {"env": "prod"}) == {"a": "prod", "b": {"c": "prod"}}
    unresolved = Unresolved("why", "text")
    assert fill_variable_templates(unresolved, {"env": "prod"}) is unresolved
    assert fill_variable_templates(["{{ var.value.env }}"], {"env": "prod"}) == ["prod"]


def test_with_no_variables_nothing_is_copied_or_changed():
    value = ["{{ var.value.env }}"]
    assert fill_variable_templates(value, {}) is value


# ── the conventions file ──────────────────────────────────────────────


def load(text: str) -> C.Conventions:
    return C.parse_conventions(__import__("yaml").safe_load(textwrap.dedent(text)))


def test_the_variables_of_the_selected_environment_are_kept():
    conv = load(
        """
        environment: prod
        environments: {prod: {ENV: prod}}
        airflow_variables:
          prod: {environment: prod, prefix: ""}
          dev: {environment: dev}
        """
    )
    assert dict(conv.airflow_variables) == {"environment": "prod", "prefix": ""}
    assert dict(conv.variables) == {"ENV": "prod"}


def test_an_environment_may_be_named_by_the_variables_alone():
    assert dict(load("airflow_variables: {only: {a: b}}").airflow_variables) == {"a": "b"}
    conv = load("environment: dev\nairflow_variables: {prod: {a: p}, dev: {a: d}}")
    assert conv.environment == "dev" and dict(conv.airflow_variables) == {"a": "d"}


def test_an_environment_without_variables_has_none():
    conv = load("environment: dev\nenvironments: {dev: {X: 1}}\nairflow_variables: {prod: {a: p}}")
    assert dict(conv.airflow_variables) == {}


@pytest.mark.parametrize(
    "text, expect",
    [
        ("airflow_variables: {a: {x: 1}, b: {x: 2}}", "name the one to use (a, b)"),
        (
            "environment: c\nairflow_variables: {a: {x: 1}}",
            "not named under environments or airflow_variables",
        ),
        ("airflow_variables: {a: {x: }}", 'write ""'),
        ("airflow_variables: {a: {x: [1]}}", 'write ""'),
        ("airflow_variables: {a: {'a b': x}}", "not an acceptable name"),
        ("airflow_variables: [a]", "expected a mapping"),
        ("airflow_variables: {a: [x]}", "expected a mapping"),
    ],
)
def test_a_variables_block_that_is_wrong_is_refused_with_the_key_named(text, expect):
    with pytest.raises(C.ConventionsError) as refused:
        load(text)
    assert expect in str(refused.value)


def test_the_names_of_the_two_blocks_are_chosen_among_together():
    conv = load("environments: {a: {X: 1}}\nairflow_variables: {b: {y: 2}}\nenvironment: b")
    assert (
        conv.environment == "b"
        and dict(conv.variables) == {}
        and dict(conv.airflow_variables) == {"y": "2"}
    )
    with pytest.raises(C.ConventionsError):
        load("environments: {a: {X: 1}}\nairflow_variables: {b: {y: 2}}")  # two names, none chosen
