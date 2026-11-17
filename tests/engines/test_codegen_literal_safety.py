# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Glue, Dataflow and Spark job generators keep every contract value as data.

A column name, a column type, the expose id, the contract name, a stage name or
the embedded SQL reaches the generated script as a string literal, a one-line
comment, an escaped docstring or SQL block, or a unique sanitised identifier, so
none can end its place and add a statement. The generated file must parse, and
nothing in it may call what a hostile value spelled out.
"""

from __future__ import annotations

import ast

import pytest

from fluid_build.engines.base import TransformationIntent
from fluid_build.engines.dataflow import _generate_pipeline
from fluid_build.engines.glue import _generate_glue_job
from fluid_build.engines.spark import _generate_spark_job
from fluid_build.providers.common.codegen_utils import py_comment_text, unique_identifiers

pytestmark = pytest.mark.unit

_CALL = "__import__('os').system('id')"
_LINE_SEPARATOR = chr(0x2028)

_HOSTILE = [
    f'a", {_CALL}, "b',
    f"a')\n{_CALL}\n#",
    f"a\r{_CALL}",
    f'x"""\n{_CALL}\n"""',
    f"x\\'); {_CALL}; ('",
    f"a{_LINE_SEPARATOR}{_CALL}",
]

_GENERATORS = [_generate_glue_job, _generate_pipeline, _generate_spark_job]
_SQL_BLOCK_GENERATORS = [_generate_glue_job, _generate_spark_job]


def _contract(expose_id="orders", column="order_id", ctype="string", name="Orders"):
    return {
        "name": name,
        "exposes": [
            {
                "exposeId": expose_id,
                "schema": [{"name": column, "type": ctype}, {"name": "amount", "type": "decimal"}],
            }
        ],
    }


def _inert(source: str) -> ast.Module:
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "__import__"
    ]
    assert calls == [], f"{len(calls)} injected call(s)"
    return tree


def _strings(tree: ast.Module) -> list:
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _multi_stage(stages, **contract):
    intent = TransformationIntent(stages=stages)
    return _generate_spark_job(
        _contract(**contract), {"pattern": "multi-stage"}, transformation_intent=intent
    )


@pytest.mark.parametrize("generate", _GENERATORS)
@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_column_name_or_type_stays_a_string(generate, value):
    tree = _inert(generate(_contract(column=value, ctype=value), {}))
    assert value in _strings(tree)


@pytest.mark.parametrize("generate", _GENERATORS)
@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_expose_id_stays_data(generate, value):
    _inert(generate(_contract(expose_id=value), {}))


@pytest.mark.parametrize("generate", _GENERATORS)
@pytest.mark.parametrize("value", _HOSTILE)
def test_hostile_embedded_sql_stays_data(generate, value):
    _inert(generate(_contract(), {"properties": {"sql": f"SELECT 1 {value}"}}))


@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_stage_or_contract_name_stays_data(value):
    _inert(_multi_stage([{"name": value, "sql": f"SELECT 2 {value}"}], name=value))


@pytest.mark.parametrize("generate", _SQL_BLOCK_GENERATORS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT order_id,\n       amount\nFROM source",
        "SELECT regexp_replace(x, '\\s+', ' ') FROM source",
        'SELECT "Order Id" FROM source WHERE note = \'a "quoted" word\'',
    ],
)
def test_embedded_sql_keeps_its_block_and_its_text(generate, sql):
    source = generate(_contract(), {"properties": {"sql": sql}})
    assert any(sql in value for value in _strings(_inert(source)))
    assert 'spark.sql("""\n' in source


def test_plain_multi_line_sql_reads_as_written():
    sql = "SELECT order_id,\n       amount\nFROM source"
    for generate in _SQL_BLOCK_GENERATORS:
        assert f'"""\n{sql}\n' in generate(_contract(), {"properties": {"sql": sql}})


def test_stages_whose_names_sanitise_alike_stay_two_functions():
    source = _multi_stage([{"name": "a-b", "sql": "SELECT 1"}, {"name": "a.b", "sql": "SELECT 2"}])
    tree = _inert(source)
    defs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert defs.count("stage_a_b") == 1 and defs.count("stage_a_b_2") == 1
    assert source.index("df = stage_a_b(spark, df)") < source.index("df = stage_a_b_2(spark, df)")
    assert any("SELECT 1" in v for v in _strings(tree))
    assert any("SELECT 2" in v for v in _strings(tree))


@pytest.mark.parametrize("generate", _GENERATORS)
def test_a_value_that_is_not_a_string_does_not_stop_generation(generate):
    _inert(generate(_contract(ctype=None), {"properties": {"sql": 5}}))
    _inert(generate(_contract(ctype=5), {}))


def test_comment_text_is_one_line_and_keeps_plain_text():
    assert py_comment_text("orders") == "orders"
    assert py_comment_text(f"a\rb\nc\x85d{_LINE_SEPARATOR}e\x00f") == "a b c d e f"
    assert py_comment_text(None) == ""


def test_unique_identifiers_suffix_a_collision_in_order():
    assert unique_identifiers(["stage_a-b", "stage_a.b", "stage_a_b", "stage_c"]) == [
        "stage_a_b",
        "stage_a_b_2",
        "stage_a_b_3",
        "stage_c",
    ]
