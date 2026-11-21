# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The PySpark schema emitter: one ``StructType`` module per output port of a contract.

The emitter renders text and never imports pyspark. These tests run the text it writes
against a stand-in ``pyspark.sql.types`` that records every constructor call, so a hostile
name, a nested type or a nullability flag is checked as a Spark job would meet it.
"""

from __future__ import annotations

import ast
import copy
import io
import os
import sys
import tokenize
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.spark import pyspark_schema as ps

pytestmark = pytest.mark.unit


# ── A stand-in for pyspark.sql.types ────────────────────────────────────


def _fake_types() -> types.ModuleType:
    module = types.ModuleType("pyspark.sql.types")

    class DataType:
        pass

    for name in (
        "StringType",
        "ByteType",
        "ShortType",
        "IntegerType",
        "LongType",
        "FloatType",
        "DoubleType",
        "BooleanType",
        "DateType",
        "TimestampType",
        "TimestampNTZType",
        "BinaryType",
    ):
        setattr(module, name, type(name, (DataType,), {}))

    class DecimalType(DataType):
        def __init__(self, precision: int = 10, scale: int = 0) -> None:
            self.precision, self.scale = precision, scale

    class ArrayType(DataType):
        def __init__(self, elementType: DataType, containsNull: bool = True) -> None:
            assert isinstance(elementType, DataType)
            self.elementType, self.containsNull = elementType, containsNull

    class MapType(DataType):
        def __init__(
            self, keyType: DataType, valueType: DataType, valueContainsNull: bool = True
        ) -> None:
            assert isinstance(keyType, DataType) and isinstance(valueType, DataType)
            self.keyType, self.valueType = keyType, valueType
            self.valueContainsNull = valueContainsNull

    class StructField:
        def __init__(
            self, name: str, dataType: DataType, nullable: bool = True, metadata: Any = None
        ) -> None:
            assert isinstance(name, str) and isinstance(dataType, DataType)
            assert isinstance(nullable, bool)
            self.name, self.dataType, self.nullable = name, dataType, nullable

    class StructType(DataType):
        def __init__(self, fields: Any = None) -> None:
            assert all(isinstance(f, StructField) for f in fields or [])
            self.fields = list(fields or [])

    for cls in (DecimalType, ArrayType, MapType, StructField, StructType):
        setattr(module, cls.__name__, cls)
    return module


_SIMPLE = {
    "StringType": "string",
    "ByteType": "byte",
    "ShortType": "short",
    "IntegerType": "integer",
    "LongType": "long",
    "FloatType": "float",
    "DoubleType": "double",
    "BooleanType": "boolean",
    "DateType": "date",
    "TimestampType": "timestamp",
    "TimestampNTZType": "timestamp_ntz",
    "BinaryType": "binary",
}


def _json(data_type: Any) -> Any:
    """Spark's own JSON form of a type (``DataType.jsonValue()``)."""
    name = type(data_type).__name__
    if name in _SIMPLE:
        return _SIMPLE[name]
    if name == "DecimalType":
        return f"decimal({data_type.precision},{data_type.scale})"
    if name == "ArrayType":
        return {
            "type": "array",
            "elementType": _json(data_type.elementType),
            "containsNull": data_type.containsNull,
        }
    if name == "MapType":
        return {
            "type": "map",
            "keyType": _json(data_type.keyType),
            "valueType": _json(data_type.valueType),
            "valueContainsNull": data_type.valueContainsNull,
        }
    return {
        "type": "struct",
        "fields": [
            {
                "name": f.name,
                "type": _json(f.dataType),
                "nullable": f.nullable,
                "metadata": {},
            }
            for f in data_type.fields
        ],
    }


@pytest.fixture
def load(monkeypatch):
    """``load(source)``: run a generated module against the stand-in, return its SCHEMA."""
    fake = _fake_types()
    pyspark, sql = types.ModuleType("pyspark"), types.ModuleType("pyspark.sql")
    pyspark.sql, sql.types = sql, fake  # type: ignore[attr-defined]
    for name, module in (
        ("pyspark", pyspark),
        ("pyspark.sql", sql),
        ("pyspark.sql.types", fake),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    def run(source: str) -> Any:
        namespace: Dict[str, Any] = {"__name__": "generated"}
        exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test input
        return namespace["SCHEMA"]

    return run


def _contract(*columns: Dict[str, Any], port: str = "orders") -> Dict[str, Any]:
    return {
        "id": "acme.sales.orders",
        "name": "Orders",
        "exposes": [{"exposeId": port, "contract": {"schema": list(columns)}}],
    }


def _source(*columns: Dict[str, Any], port: str = "orders") -> str:
    [module] = ps.render_contract(_contract(*columns, port=port)).modules
    return module.source


def _field(name: str, type_: str = "string", **extra: Any) -> Dict[str, Any]:
    return {"name": name, "type": type_, **extra}


# ── Column types ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "declared, target",
    [
        ("string", "StringType()"),
        ("varchar(255)", "StringType()"),
        ("char(3)", "StringType()"),
        ("text", "StringType()"),
        ("uuid", "StringType()"),
        ("enum(a, b)", "StringType()"),
        ("tinyint", "ByteType()"),
        ("smallint", "ShortType()"),
        ("year", "ShortType()"),
        ("int", "IntegerType()"),
        ("integer", "IntegerType()"),
        ("mediumint", "IntegerType()"),
        ("bigint", "LongType()"),
        ("long", "LongType()"),
        ("int64", "LongType()"),
        ("float", "FloatType()"),
        ("real", "FloatType()"),
        ("float(24)", "FloatType()"),
        ("float(53)", "DoubleType()"),
        ("double", "DoubleType()"),
        ("double precision", "DoubleType()"),
        ("boolean", "BooleanType()"),
        ("bool", "BooleanType()"),
        ("bit", "BooleanType()"),
        ("bit(1)", "BooleanType()"),
        ("date", "DateType()"),
        ("timestamp", "TimestampType()"),
        ("timestamp(3)", "TimestampType()"),
        ("datetime2", "TimestampType()"),
        ("timestamptz", "TimestampType()"),
        ("timestamp_ltz", "TimestampType()"),
        ("Timestamp With Time Zone", "TimestampType()"),
        ("timestamp_ntz", "TimestampNTZType()"),
        ("timestampntz", "TimestampNTZType()"),
        ("timestamp without time zone", "TimestampNTZType()"),
        ("binary", "BinaryType()"),
        ("bytea", "BinaryType()"),
        ("money", "DecimalType(19, 4)"),
        ("decimal", "DecimalType(38, 9)"),
        ("number", "DecimalType(38, 9)"),
        ("decimal(12,2)", "DecimalType(12, 2)"),
        ("DECIMAL( 5 , 5 )", "DecimalType(5, 5)"),
        ("numeric(10)", "DecimalType(10, 0)"),
        ("dec(38,0)", "DecimalType(38, 0)"),
        (None, "StringType()"),
    ],
)
def test_a_column_type_is_the_spark_type_that_holds_every_value_of_it(declared, target):
    outcome = ps.column_outcome({"name": "c", "type": declared})

    assert (outcome.target, outcome.status) == (target, "exact")


@pytest.mark.parametrize(
    "declared, why",
    [
        ("json", "no json type"),
        ("jsonb", "no jsonb type"),
        ("variant", "VariantType needs Spark 4.0"),
        ("super", "no super type"),
        ("time", "no time-of-day type"),
        ("interval", "no single interval type"),
        ("bignumeric", "beyond 38"),
    ],
)
def test_a_type_spark_has_no_form_for_is_text_and_a_warning(declared, why):
    outcome = ps.column_outcome({"name": "c", "type": declared})

    assert (outcome.target, outcome.status) == ("StringType()", "lossy")
    assert why in outcome.why and "Declare string" in outcome.fix


@pytest.mark.parametrize(
    "declared",
    [
        "geography",
        "geometry",
        "geom",
        "point",
        "hll",
        "bit(8)",
        "decimal(39,0)",
        "decimal(0,0)",
        "decimal(5,6)",
        "decimal(5,2,1)",
        "decimal(١٢,٢)",  # Arabic-Indic digits are not a precision
        "varchar(٢٥٥)",
        "mystery_type",
        "int unsigned",
        "struct",
        "array",
        "map",
        "object",
        "record",
        "row",
    ],
)
def test_a_type_no_spark_schema_can_hold_is_refused_not_guessed(declared):
    outcome = ps.column_outcome({"name": "c", "type": declared})

    assert outcome.status == "refused" and outcome.target is None
    assert outcome.why and outcome.fix


def test_required_is_not_nullable_and_anything_else_is_nullable(load):
    schema = load(
        _source(
            _field("a", required=True),
            _field("b", required=False),
            _field("c"),
            _field("d", required="true"),  # not a boolean: schema-invalid, read as not required
        )
    )

    assert [f["nullable"] for f in _json(schema)["fields"]] == [False, True, True, True]


# ── Nested types ────────────────────────────────────────────────────────


def test_structs_arrays_and_maps_nest_with_their_nullability(load):
    schema = load(
        _source(
            _field("tags", "array", element={"type": "string", "required": True}),
            _field("attrs", "map", key={"type": "string"}, value={"type": "long"}),
            _field(
                "address",
                "struct",
                required=True,
                fields=[
                    _field("city", required=True),
                    _field(
                        "geo",
                        "struct",
                        fields=[_field("lat", "double"), _field("lon", "double")],
                    ),
                    _field(
                        "lines",
                        "array",
                        element={"type": "struct", "fields": [_field("n", "int")]},
                    ),
                    _field("prices", "map", key={"type": "string"}, value={"type": "decimal(9,2)"}),
                ],
            ),
        )
    )

    assert _json(schema) == {
        "type": "struct",
        "fields": [
            {
                "name": "tags",
                "type": {"type": "array", "elementType": "string", "containsNull": False},
                "nullable": True,
                "metadata": {},
            },
            {
                "name": "attrs",
                "type": {
                    "type": "map",
                    "keyType": "string",
                    "valueType": "long",
                    "valueContainsNull": True,
                },
                "nullable": True,
                "metadata": {},
            },
            {
                "name": "address",
                "type": {
                    "type": "struct",
                    "fields": [
                        {"name": "city", "type": "string", "nullable": False, "metadata": {}},
                        {
                            "name": "geo",
                            "type": {
                                "type": "struct",
                                "fields": [
                                    {
                                        "name": "lat",
                                        "type": "double",
                                        "nullable": True,
                                        "metadata": {},
                                    },
                                    {
                                        "name": "lon",
                                        "type": "double",
                                        "nullable": True,
                                        "metadata": {},
                                    },
                                ],
                            },
                            "nullable": True,
                            "metadata": {},
                        },
                        {
                            "name": "lines",
                            "type": {
                                "type": "array",
                                "elementType": {
                                    "type": "struct",
                                    "fields": [
                                        {
                                            "name": "n",
                                            "type": "integer",
                                            "nullable": True,
                                            "metadata": {},
                                        }
                                    ],
                                },
                                "containsNull": True,
                            },
                            "nullable": True,
                            "metadata": {},
                        },
                        {
                            "name": "prices",
                            "type": {
                                "type": "map",
                                "keyType": "string",
                                "valueType": "decimal(9,2)",
                                "valueContainsNull": True,
                            },
                            "nullable": True,
                            "metadata": {},
                        },
                    ],
                },
                "nullable": False,
                "metadata": {},
            },
        ],
    }


def test_a_map_value_that_is_required_holds_no_null(load):
    schema = load(
        _source(_field("m", "map", key={"type": "string"}, value={"type": "int", "required": True}))
    )

    assert _json(schema)["fields"][0]["type"]["valueContainsNull"] is False


@pytest.mark.parametrize("alias", ["struct", "object", "record", "row", "STRUCT"])
def test_every_spelling_of_a_struct_is_one(alias, load):
    schema = load(_source(_field("s", alias, fields=[_field("a")])))

    assert _json(schema)["fields"][0]["type"]["type"] == "struct"


def test_nested_types_are_not_limited_to_the_streaming_vocabulary(load):
    schema = load(
        _source(
            _field(
                "s",
                "struct",
                fields=[
                    _field("a", "tinyint"),
                    _field("b", "smallint"),
                    _field("c", "timestamp_ntz"),
                    _field("d", "uuid"),
                    _field("e", "decimal(38,10)"),
                ],
            )
        )
    )

    nested = _json(schema)["fields"][0]["type"]["fields"]
    assert [f["type"] for f in nested] == [
        "byte",
        "short",
        "timestamp_ntz",
        "string",
        "decimal(38,10)",
    ]


@pytest.mark.parametrize(
    "column, why",
    [
        (_field("s", "struct"), "fields, element, or key and value"),
        (_field("s", "array"), "fields, element, or key and value"),
        (_field("s", "map"), "fields, element, or key and value"),
        (_field("s", "struct", fields=[]), "no fields"),
        (_field("s", "struct", fields="a"), "no fields"),
        (_field("s", "struct", fields=[_field("a")], element={"type": "string"}), "cannot"),
        (_field("s", "array", fields=[_field("a")]), "a array cannot"),
        (_field("s", "array", element="string"), "not a typed definition"),
        (_field("s", "map", key={"type": "string"}), "only one of key and value"),
        (_field("s", "map", key={"type": "string"}, value=None), "not a typed definition"),
        (_field("s", "string", fields=[_field("a")]), "but is not a struct, array or map"),
        (_field("s", "struct", fields=[{"type": "string"}]), "not named columns"),
        (_field("s", "struct", fields=["a"]), "not named columns"),
        (_field("s", "struct", fields=[_field("")]), "empty name"),
        (_field("s", "struct", fields=[_field("a"), _field("A")]), "twice"),
        (
            _field("s", "struct", fields=[_field("g", "geography")]),
            "no Spark column type",
        ),
        (_field("s", "array", element={"type": "decimal(40,2)"}), "not a Spark decimal's"),
    ],
)
def test_a_nested_type_spark_cannot_hold_is_refused_with_its_reason(column, why):
    with pytest.raises(UnsupportedBindingError) as raised:
        ps.render_contract(_contract(column))

    assert raised.value.kind == "column-type"
    assert "expose 'orders'" in str(raised.value) and why in str(raised.value)
    assert raised.value.remediation


def _nest(depth: int) -> Dict[str, Any]:
    column: Dict[str, Any] = _field("leaf", "int")
    for level in range(depth):
        column = _field(f"l{level}", "struct", fields=[column])
    return column


def test_nesting_is_bounded_and_both_paths_agree_on_where():
    within, past = _nest(ps.MAX_DEPTH), _nest(ps.MAX_DEPTH + 1)

    assert ps.column_outcome(within).status == "exact"
    assert ps.render_contract(_contract(within)).modules  # renders, as the finding said

    assert ps.column_outcome(past).status == "refused"
    with pytest.raises(UnsupportedBindingError, match="deeper than") as raised:
        ps.render_contract(_contract(past))
    assert "'l32'" in str(raised.value)  # the column is named


def test_a_leaf_written_as_text_inside_a_struct_warns_with_its_path():
    [module] = ps.render_contract(
        _contract(_field("meta", "struct", fields=[_field("payload", "json"), _field("n", "int")]))
    ).modules

    [warning] = module.warnings
    assert "column 'meta'" in warning and "meta.payload is declared 'json'" in warning
    assert "it is written as 'StructType with StringType() leaves'" in warning


# ── What a module looks like ────────────────────────────────────────────

GOLDEN = '''\
# Generated by `fluid generate pyspark-schema`. Do not edit: regenerate.
# -*- coding: utf-8 -*-
# fmt: off
# isort: skip_file

# data product: acme.sales.orders
# output port: orders

"""Schema of output port 'orders' of data product 'acme.sales.orders'.

Needs Spark 3.3 or later: it uses TimestampNTZType.
"""

from pyspark.sql.types import (
    ArrayType,
    DecimalType,
    MapType,
    StringType,
    StructField,
    StructType,
    TimestampNTZType,
    TimestampType,
)

SCHEMA = StructType(
    [
        #: Primary key of the order
        StructField("order_id", StringType(), nullable=False),
        StructField("amount", DecimalType(12, 2), nullable=False),
        StructField("placed_at", TimestampType(), nullable=True),
        StructField("local_ts", TimestampNTZType(), nullable=True),
        StructField("tags", ArrayType(StringType(), containsNull=False), nullable=True),
        StructField(
            "attrs",
            MapType(StringType(), StringType(), valueContainsNull=True),
            nullable=True,
        ),
        StructField(
            "address",
            StructType(
                [
                    StructField("city", StringType(), nullable=False),
                ]
            ),
            nullable=True,
        ),
    ]
)
'''


def test_the_module_is_exactly_this_text():
    source = _source(
        _field("order_id", required=True, description="Primary key of the order"),
        _field("amount", "decimal(12,2)", required=True),
        _field("placed_at", "timestamp"),
        _field("local_ts", "timestamp_ntz"),
        _field("tags", "array", element={"type": "string", "required": True}),
        _field("attrs", "map", key={"type": "string"}, value={"type": "string"}),
        _field("address", "struct", fields=[_field("city", required=True)]),
    )

    assert source == GOLDEN


def test_only_the_names_a_module_uses_are_imported(load):
    source = _source(_field("a"), _field("b", "bigint"))

    imported = [
        alias.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert imported == ["LongType", "StringType", "StructField", "StructType"]
    assert "Needs Spark" not in source


def test_the_output_is_deterministic_and_keeps_contract_order():
    columns = [_field("z"), _field("a"), _field("m", "int")]

    first = _source(*columns)

    assert first == _source(*copy.deepcopy(columns))
    assert first.index('"z"') < first.index('"a"') < first.index('"m"')


def test_the_module_imports_pyspark_and_nothing_else():
    tree = ast.parse(_source(_field("a")))

    assert [type(node).__name__ for node in tree.body] == ["Expr", "ImportFrom", "Assign"]
    [imported] = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    assert imported.module == "pyspark.sql.types"


def test_a_description_is_a_comment_cut_to_one_short_line():
    long = "word " * 60
    source = _source(
        _field("a", description="First line\nsecond line\r\nthird\u2028line"),
        _field("b", description=long),
        _field("c", description="   "),
        _field("d", description=5),
    )

    comments = [line.strip() for line in source.splitlines() if line.strip().startswith("#:")]
    assert "#: First line second line third line" in comments
    assert any(c.endswith("...") and len(c) <= 83 for c in comments)
    assert sum(1 for c in comments if c.startswith("#: word")) == 1
    assert len(comments) == 2  # nothing for a blank or a non-text description


@pytest.mark.parametrize(
    "description",
    [
        "fmt: on",
        "fmt: off",
        "fmt: skip",
        "isort: skip",
        "noqa",
        "type: ignore",
        "pylint: disable=x",
    ],
)
def test_a_description_that_reads_like_a_tool_pragma_is_not_one(description):
    source = _source(_field("a", description=description))

    own = {"# fmt: off", "# isort: skip_file"}
    plain = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("# ") and line.strip() not in own
    ]
    assert plain == [  # only the header's own comments are plain `# ` lines
        "# Generated by `fluid generate pyspark-schema`. Do not edit: regenerate.",
        "# -*- coding: utf-8 -*-",
        "# data product: acme.sales.orders",
        "# output port: orders",
    ]
    assert f"#: {description}" in source


# ── Hostile contract text never becomes code ────────────────────────────

HOSTILE = [
    '"""\nimport os; os.system("x")\n"""',
    "'; import os; x = '",
    '") ; import os ; ("',
    "a\\",
    "\\",
    "line1\nline2",
    "cr\rinject = 1",
    "sep inject = 1",
    "sep inject = 1",
    "nel\x85inject = 1",
    "nul\x00byte",
    "ff\x0cname",
    "{braces}{0}{}",
    "emoji \U0001f600 é中",
    "tab\tname",
    "# not a comment",
    "__import__('os').system('x')",
    "\\x41\\u0041",
    "'''triple'''",
    "coding: utf-7",
    "billing coding=latin-1",
    "x vim: set fileencoding=utf-7 :",
    "# -*- coding: utf-7 -*-",
    "sales.orders vim: set fileencoding=utf-7",
]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_contract_text_reaches_the_module_as_data_only(hostile, load):
    contract = _contract(
        _field(hostile, required=True, description=hostile),
        _field("n", "struct", fields=[_field(hostile + "!", description=hostile)]),
        port=hostile,
    )
    contract["id"] = hostile

    [module] = ps.render_contract(contract).modules

    data = module.source.encode("utf-8")
    tree = ast.parse(data)  # as bytes, so an encoding declaration in the text would apply
    assert [type(node).__name__ for node in tree.body] == ["Expr", "ImportFrom", "Assign"]
    assert module.source.split("\n")[:2] == [ps.HEADER, ps.CODING]
    assert tokenize.detect_encoding(io.BytesIO(data).readline)[0] == "utf-8"
    fields = _json(load(module.source))["fields"]
    assert [f["name"] for f in fields] == [hostile, "n"]
    assert fields[1]["type"]["fields"][0]["name"] == hostile + "!"
    assert module.filename.endswith("_schema.py") and module.filename.isascii()
    assert os.sep not in module.filename
    assert module.filename[: -len(".py")].isidentifier()


# ``load`` installs the stand-in once for the test, which every generated example may share.
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    name=st.text(min_size=1),
    description=st.text(),
    port=st.text(min_size=1),
    product=st.text(),
)
def test_any_text_in_a_contract_is_data_to_the_module(name, description, port, product, load):
    contract = _contract(_field(name, description=description), port=port)
    contract["id"] = product

    [module] = ps.render_contract(contract).modules

    tree = ast.parse(module.source)
    assert [type(node).__name__ for node in tree.body] == ["Expr", "ImportFrom", "Assign"]
    assert [f["name"] for f in _json(load(module.source))["fields"]] == [name]
    module.source.encode("utf-8")  # a UTF-8 file holds it


def test_a_lone_surrogate_does_not_fail_the_write(load):
    [module] = ps.render_contract(
        _contract(_field("a", description="x\ud800y"), port="p\ud800")
    ).modules

    module.source.encode("utf-8")
    assert [f["name"] for f in _json(load(module.source))["fields"]] == ["a"]


# ── Output ports ────────────────────────────────────────────────────────


def test_a_module_for_each_port_with_a_schema_and_a_note_for_the_rest():
    contract = {
        "id": "p",
        "exposes": [
            {"exposeId": "orders", "contract": {"schema": [_field("a")]}},
            {"exposeId": "events"},
            {"exposeId": "dim", "contract": {"schema": []}},
            {"id": "legacy", "contract": {"schema": [_field("b"), _field("c")]}},
            {"contract": {"schema": [_field("d")]}},
        ],
    }

    rendered = ps.render_contract(contract)

    assert [(m.port, m.filename, m.columns) for m in rendered.modules] == [
        ("orders", "orders_schema.py", 1),
        ("legacy", "legacy_schema.py", 2),
        ("port_4", "port_4_schema.py", 1),
    ]
    assert rendered.skipped == ("events", "dim")


def test_a_contract_with_no_ports_renders_nothing():
    for contract in (
        {},
        {"exposes": None},
        {"exposes": []},
        {"exposes": [{"exposeId": "a"}]},
        {"exposes": [{"exposeId": "a", "contract": {}}]},
        {"exposes": [{"exposeId": "a", "contract": {"schema": None}}]},
    ):
        assert ps.render_contract(contract).modules == ()


@pytest.mark.parametrize(
    "contract, why",
    [
        (None, "the contract is not a mapping"),
        ([], "the contract is not a mapping"),
        ("x", "the contract is not a mapping"),
        ({"exposes": "x"}, "exposes is not a list"),
        ({"exposes": {"a": 1}}, "exposes is not a list"),
        ({"exposes": 5}, "exposes is not a list"),
        ({"exposes": [{"exposeId": "a"}, "not a port"]}, r"exposes\[1\] is not an output port"),
        ({"exposes": [{"exposeId": "a", "contract": "x"}]}, "contract is not a mapping"),
        (
            {"exposes": [{"exposeId": "a", "contract": {"schema": {"a": "int"}}}]},
            "contract.schema is not a list of columns",
        ),
        (
            {"exposes": [{"exposeId": "a", "contract": {"schema": "a"}}]},
            "contract.schema is not a list of columns",
        ),
    ],
)
def test_a_contract_that_is_not_shaped_like_one_is_refused_not_skipped(contract, why):
    # a gate that passes on a contract it cannot read passes on anything
    with pytest.raises(UnsupportedBindingError, match=why) as raised:
        ps.render_contract(contract)

    assert raised.value.kind == "contract-shape" and raised.value.remediation


def test_module_names_are_lowercase_identifiers_and_distinct():
    contract = {
        "exposes": [
            {"exposeId": port, "contract": {"schema": [_field("a")]}}
            for port in ("Orders", "orders", "orders-gold", "orders.gold", "1st", "class", "json")
        ]
    }

    names = [m.filename for m in ps.render_contract(contract).modules]

    assert names == [
        "orders_schema.py",
        "orders_2_schema.py",
        "orders_gold_schema.py",
        "orders_gold_2_schema.py",
        "st_schema.py",
        "class__schema.py",
        "json_schema.py",
    ]
    assert len(set(names)) == len(names)


# ── What no schema can hold is refused, all of it at once ────────────────


def test_every_column_that_cannot_be_held_is_named_in_one_refusal():
    contract = {
        "exposes": [
            {
                "exposeId": "a",
                "contract": {
                    "schema": [_field("g", "geography"), _field("ok"), _field("h", "hll")]
                },
            },
            {
                "exposeId": "b",
                "contract": {"schema": [_field("p", "point"), _field("s", "struct")]},
            },
        ]
    }

    with pytest.raises(UnsupportedBindingError) as raised:
        ps.render_contract(contract)

    message = str(raised.value)
    assert raised.value.kind == "column-type"
    assert all(f"expose '{p}'" in message for p in ("a", "b"))
    assert all(f"'{c}'" in message for c in ("g", "h", "p", "s"))
    assert "'ok'" not in message
    assert len(raised.value.remediation) == len(set(raised.value.remediation))


@pytest.mark.parametrize(
    "columns, why",
    [
        ([{"type": "string"}], "column 1 has no name"),
        ([_field("")], "column 1 has no name"),
        ([_field("a"), "b"], "column 2 has no name"),
        ([_field("a"), _field("A")], "differ only in case"),
        ([_field("id"), _field("x"), _field("ID")], "differ only in case"),
    ],
)
def test_a_column_without_a_name_or_a_duplicate_of_one_is_refused(columns, why):
    with pytest.raises(UnsupportedBindingError, match=why):
        ps.render_contract(_contract(*columns))


def test_the_same_fix_is_offered_once_however_many_ports_need_it():
    contract = {
        "exposes": [
            {"exposeId": port, "contract": {"schema": [_field("a"), _field("A")]}}
            for port in ("one", "two", "three")
        ]
    }

    with pytest.raises(UnsupportedBindingError) as raised:
        ps.render_contract(contract)

    assert all(f"expose '{p}'" in str(raised.value) for p in ("one", "two", "three"))
    assert raised.value.remediation == (
        "Give every column a name that differs by more than its case.",
    )


def test_nothing_is_rendered_when_one_port_is_refused():
    contract = {
        "exposes": [
            {"exposeId": "good", "contract": {"schema": [_field("a")]}},
            {"exposeId": "bad", "contract": {"schema": [_field("g", "geometry")]}},
        ]
    }

    with pytest.raises(UnsupportedBindingError, match="expose 'bad'"):
        ps.render_contract(contract)


# ── Writing ─────────────────────────────────────────────────────────────


def _modules(*ports: str) -> List[ps.PortModule]:
    contract = {"exposes": [{"exposeId": p, "contract": {"schema": [_field("a")]}} for p in ports]}
    return list(ps.render_contract(contract).modules)


def test_a_new_module_is_written_and_then_unchanged(tmp_path: Path):
    modules = _modules("orders")

    first = ps.plan(modules, tmp_path / "out")
    ps.write(first)
    second = ps.plan(modules, tmp_path / "out")

    assert [p.status for p in first] == ["new"]
    assert [p.status for p in second] == ["unchanged"]
    assert (tmp_path / "out" / "orders_schema.py").read_text(encoding="utf-8") == modules[0].source
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["orders_schema.py"]  # no .tmp left


def test_a_module_the_command_wrote_is_replaced_when_the_contract_changes(tmp_path: Path):
    [old] = _modules("orders")
    ps.write(ps.plan([old], tmp_path))
    new = ps.render_contract(_contract(_field("a"), _field("b", "int"), port="orders")).modules

    planned = ps.plan(new, tmp_path)
    ps.write(planned)

    assert [p.status for p in planned] == ["stale"]
    assert (tmp_path / "orders_schema.py").read_text(encoding="utf-8") == new[0].source


def test_a_file_the_command_did_not_write_is_foreign_and_never_touched(tmp_path: Path):
    mine = tmp_path / "orders_schema.py"
    mine.write_text("# hand written\nSCHEMA = None\n", encoding="utf-8")
    empty = tmp_path / "events_schema.py"
    empty.write_text("", encoding="utf-8")

    planned = ps.plan(_modules("orders", "events", "dim"), tmp_path)
    ps.write(planned)

    assert [p.status for p in planned] == ["foreign", "foreign", "new"]
    assert mine.read_text(encoding="utf-8") == "# hand written\nSCHEMA = None\n"
    assert empty.read_text(encoding="utf-8") == ""


def test_a_link_or_a_directory_in_the_way_is_foreign(tmp_path: Path):
    target = tmp_path / "elsewhere.py"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / "orders_schema.py").symlink_to(target)
    (tmp_path / "events_schema.py").mkdir()

    planned = ps.plan(_modules("orders", "events"), tmp_path)
    ps.write(planned)

    assert [p.status for p in planned] == ["foreign", "foreign"]
    assert target.read_text(encoding="utf-8") == "keep"


def test_a_link_to_a_generated_module_is_still_not_written_through(tmp_path: Path):
    [module] = _modules("orders")
    real = tmp_path / "real.py"
    real.write_text(module.source, encoding="utf-8")
    (tmp_path / "orders_schema.py").symlink_to(real)

    [item] = ps.plan([module], tmp_path)

    assert item.status == "foreign"


def test_a_checkout_that_converted_line_endings_still_holds_the_same_module(tmp_path: Path):
    [module] = _modules("orders")
    (tmp_path / "orders_schema.py").write_bytes(module.source.replace("\n", "\r\n").encode("utf-8"))

    [item] = ps.plan([module], tmp_path)

    assert item.status == "unchanged"


def test_writing_creates_the_directory_and_nothing_but_the_modules(tmp_path: Path):
    out = tmp_path / "a" / "b"

    ps.write(ps.plan(_modules("x", "y"), out))

    assert sorted(p.name for p in out.iterdir()) == ["x_schema.py", "y_schema.py"]


# ── The module is checked against what it may hold ──────────────────────


def _legit() -> str:
    return _source(_field("a", required=True), _field("n", "array", element={"type": "int"}))


def _names(source: str) -> frozenset:
    [imported] = [n for n in ast.parse(source).body if isinstance(n, ast.ImportFrom)]
    return frozenset(alias.name for alias in imported.names)


def test_a_module_that_holds_exactly_a_schema_passes_the_check():
    source = _legit()

    ps._verify(source, "m.py", _names(source))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda s: s + "\nimport os\n",
        lambda s: s + "\nos.system('x')\n",
        lambda s: s.replace("SCHEMA = StructType(", "SCHEMA = print(1) or StructType(", 1),
        lambda s: s.replace("StringType()", "StringType().x", 1),
        lambda s: s.replace("StringType()", "StringType(os)", 1),
        lambda s: s.replace("StringType()", "(lambda: 1)()", 1),
        lambda s: s.replace("StringType()", "__import__('os')", 1),
        lambda s: s.replace("StringType()", "[x for x in ()]", 1),
        lambda s: s.replace("nullable=False", "nullable=None", 1),
        lambda s: s.replace("nullable=False", "other=False", 1),
        lambda s: s.replace('"a"', 'b"a"', 1),
        lambda s: s.replace("from pyspark.sql.types import", "from os import", 1),
        lambda s: s.replace("    StructField,\n", "    StructField as SF,\n", 1),
        lambda s: s.replace("SCHEMA = ", "SCHEMAS = ", 1),
        lambda s: s.replace("SCHEMA = ", "SCHEMA = OTHER = ", 1),
        lambda s: s.replace('"""Schema', 'print(1)\n"""Schema', 1),
        lambda s: s.replace(ps.CODING, "# coding: utf-7", 1),
        lambda s: s.replace(ps.HEADER, "# coding: latin-1", 1),
        lambda s: s.replace(ps.CODING + "\n", "", 1),
    ],
)
def test_a_module_that_holds_anything_else_is_refused(tamper):
    source = _legit()
    names = _names(source)

    with pytest.raises(ValueError, match="more than its schema|fixed lines"):
        ps._verify(tamper(source), "m.py", names)


def test_every_module_is_checked_before_it_is_returned(monkeypatch):
    def refuse(*args: Any) -> None:
        raise ValueError("refused by the check")

    monkeypatch.setattr(ps, "_verify", refuse)

    with pytest.raises(ValueError, match="refused by the check"):
        ps.render_contract(_contract(_field("a")))


def test_the_check_reads_the_text_as_the_interpreter_does():
    source = _legit()
    # A declaration the generator never writes: UTF-7 decodes `+ACI-` as a quote.
    forged = source.replace(ps.CODING, "# coding: utf-7")

    with pytest.raises(ValueError, match="fixed lines"):
        ps._verify(forged, "m.py", _names(source))


# ── Names, whatever the port ────────────────────────────────────────────


def test_a_long_port_id_makes_a_bounded_name_and_two_long_ones_stay_two():
    long = "x" * 300
    contract = {
        "exposes": [
            {"exposeId": port, "contract": {"schema": [_field("a")]}}
            for port in (long, long, long + "y", "short")
        ]
    }

    names = [m.filename for m in ps.render_contract(contract).modules]

    assert len(set(names)) == 4
    assert all(len(n.encode()) <= 100 for n in names) and names[3] == "short_schema.py"
    assert names[0] != names[1] and names[0] != names[2]


def test_a_port_id_is_one_line_in_the_module_whatever_it_holds(tmp_path: Path, load):
    contract = _contract(_field("a"), port="ord\r\ners\u2028x")
    contract["id"] = "pro\r\nduct\r"

    [module] = ps.render_contract(contract).modules
    ps.write(ps.plan([module], tmp_path))

    assert "\r" not in module.source
    assert [p.status for p in ps.plan([module], tmp_path)] == ["unchanged"]
    assert [f["name"] for f in _json(load(module.source))["fields"]] == ["a"]


def test_a_lone_surrogate_never_reaches_a_message_or_the_file(load):
    contract = _contract(
        _field("a\ud800", "json"),
        _field("b", "struct", fields=[_field("c\udfff", "time")]),
        port="p\ud800",
    )

    [module] = ps.render_contract(contract).modules

    module.source.encode("utf-8")
    assert module.warnings and all(w.encode("utf-8") for w in module.warnings)

    with pytest.raises(UnsupportedBindingError) as raised:
        ps.render_contract(_contract(_field("g\ud800", "geography"), port="p\ud800"))
    str(raised.value).encode("utf-8")
    # each layer holds without the other: a port without columns is only listed, and a
    # message that reaches the merge is cleaned there
    [skipped] = ps.render_contract({"exposes": [{"exposeId": "q\ud800"}]}).skipped
    skipped.encode("utf-8")
    str(ps._merge([UnsupportedBindingError("column-type", "x\ud800", ())])).encode("utf-8")


def test_names_that_differ_only_where_spark_keeps_them_apart_are_accepted(load):
    schema = load(
        _source(_field("stra\u00dfe"), _field("strasse"), _field("\u00e9a"), _field("e\u0301a"))
    )

    assert [f["name"] for f in _json(schema)["fields"]] == [
        "stra\u00dfe",
        "strasse",
        "\u00e9a",
        "e\u0301a",
    ]
    with pytest.raises(UnsupportedBindingError, match="differ only in case"):
        ps.render_contract(_contract(_field("\u00c9a"), _field("\u00e9a")))


# ── The types, against the Glue emitter ─────────────────────────────────


def test_a_type_is_judged_as_the_glue_emitter_judges_it():
    from fluid_build.iac.providers import aws

    declared = (
        set(aws._HIVE_TYPES)
        | set(aws._HIVE_AS_STRING)
        | set(aws._HIVE_REFUSED)
        | set(aws._DECIMAL_FAMILY)
        | {"varchar(255)", "char(3)", "float(53)", "float(24)", "bit(8)", "bit(1)", "enum(a, b)"}
        | {"decimal(12,2)", "decimal(39,0)", "decimal(5,6)", "decimal", "number(10)", "money"}
        | {"struct", "array", "map", "object", "record", "row", "mystery", "timestamp(3)", "year"}
    )

    different = {
        d: (ps._scalar(d).status, aws._hive_outcome(d).status)
        for d in sorted(declared)
        if ps._scalar(d).status != aws._hive_outcome(d).status
    }

    assert different == {}


def test_a_huge_precision_is_refused_without_an_exception_or_an_echo_of_it():
    digits = "9" * 5000

    refused = ps.column_outcome(_field("c", f"decimal({digits})"))
    float_like = ps.column_outcome(_field("c", f"float({digits})"))
    odd = ps.column_outcome(_field("c", f"varchar(\u0662{digits})"))

    assert refused.status == "refused" and len(refused.why) < 120
    assert float_like.status == "exact"
    assert odd.status == "refused" and len(odd.why) < 120


# ── Writing, further ────────────────────────────────────────────────────


def test_a_module_is_written_with_the_mode_an_ordinary_file_gets(tmp_path: Path):
    ps.write(ps.plan(_modules("orders"), tmp_path))

    mask = os.umask(0)
    os.umask(mask)
    assert (tmp_path / "orders_schema.py").stat().st_mode & 0o777 == 0o666 & ~mask


def test_a_link_planted_at_the_temporary_name_is_refused_not_written_through(tmp_path: Path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    (tmp_path / f".orders_schema.py.{os.getpid()}.tmp").symlink_to(victim)

    with pytest.raises(FileExistsError):
        ps.write(ps.plan(_modules("orders"), tmp_path))

    assert victim.read_text(encoding="utf-8") == "keep"
    assert not (tmp_path / "orders_schema.py").exists()


def test_an_existing_file_of_any_size_is_judged_by_its_head(tmp_path: Path):
    [module] = _modules("orders")
    huge = tmp_path / "orders_schema.py"
    with huge.open("wb") as handle:
        handle.write(ps.HEADER.encode("utf-8") + b"\n")
        handle.truncate(3 * 1024**3)  # sparse: nothing is stored for the gap

    [stale] = ps.plan([module], tmp_path)
    with huge.open("r+b") as handle:
        handle.write(b"# someone else's\n")  # no longer starts with the header
    [foreign] = ps.plan([module], tmp_path)

    assert (stale.status, foreign.status) == ("stale", "foreign")


def test_a_module_this_command_wrote_for_a_port_the_contract_dropped_is_an_orphan(tmp_path: Path):
    ps.write(ps.plan(_modules("orders", "events"), tmp_path))
    (tmp_path / "mine_schema.py").write_text("# hand written\n", encoding="utf-8")
    (tmp_path / "events_schema.py.bak").write_text(ps.HEADER, encoding="utf-8")
    (tmp_path / "link_schema.py").symlink_to(tmp_path / "orders_schema.py")
    (tmp_path / "dir_schema.py").mkdir()

    assert ps.orphans(tmp_path, _modules("orders"), "(unnamed)") == ["events_schema.py"]
    assert ps.orphans(tmp_path, [], "(unnamed)") == ["events_schema.py", "orders_schema.py"]
    assert ps.orphans(tmp_path / "missing", _modules("orders"), "(unnamed)") == []


def test_the_first_five_lines_of_a_module_carry_no_contract_text():
    hostile = "vim: set ts=1 :"
    contract = _contract(_field(hostile, description=hostile), port=hostile)
    contract["id"] = hostile

    [module] = ps.render_contract(contract).modules

    assert module.source.split("\n")[:5] == [
        ps.HEADER,
        ps.CODING,
        "# fmt: off",
        "# isort: skip_file",
        "",
    ]


@pytest.mark.parametrize(
    "declared",
    [
        "int(11) unsigned",
        "tinyint(3) unsigned",
        "bigint(20) unsigned",
        "float(53) unsigned",
        "timestamp(6) without time zone",
        "timestamp(6) with time zone",
        "varchar(10) character set utf8",
        "string<x>",
        "decimal(10,2) zerofill",
        "enum(a, b) junk",
    ],
)
def test_text_after_a_types_parameters_is_refused_not_ignored(declared):
    outcome = ps.column_outcome({"name": "c", "type": declared})

    assert outcome.status == "refused" and outcome.target is None
    assert "unsigned" in outcome.why or "parameter list" in outcome.why


@pytest.mark.parametrize(
    "declared, target",
    [
        ("int(11)", "IntegerType()"),
        ("varchar(255)", "StringType()"),
        ("timestamp(3)", "TimestampType()"),
        ("timestamp with time zone", "TimestampType()"),
        ("decimal(12,2)", "DecimalType(12, 2)"),
        ("enum('a)', 'b')", "StringType()"),
        ("enum(a, b)", "StringType()"),
    ],
)
def test_a_parameter_list_after_a_types_name_is_still_read(declared, target):
    assert ps.column_outcome({"name": "c", "type": declared}).target == target


def test_a_cut_name_that_lands_on_another_ports_name_gets_its_own_file():
    long = "a" * 100
    cut = ps._module_name(long)  # what the long one is called
    contract = {
        "exposes": [
            {"exposeId": port, "contract": {"schema": [_field("a")]}} for port in (long, cut, cut)
        ]
    }

    names = [m.filename for m in ps.render_contract(contract).modules]

    assert len(set(names)) == 3
    assert names[0] == f"{cut}_schema.py"  # the first keeps its name
    assert all(len(n) <= ps._MAX_NAME + len(ps.SUFFIX) + 3 for n in names)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_pipe_in_the_way_is_foreign_and_is_never_waited_on(tmp_path: Path):
    os.mkfifo(tmp_path / "orders_schema.py")

    [item] = ps.plan(_modules("orders"), tmp_path)

    assert item.status == "foreign"
    assert ps.orphans(tmp_path, [], "(unnamed)") == []


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="a superuser reads any file"
)
def test_a_file_that_cannot_be_read_is_foreign_to_plan_and_skipped_by_orphans(tmp_path: Path):
    ps.write(ps.plan(_modules("orders", "events"), tmp_path))
    for name in ("orders_schema.py", "events_schema.py"):
        (tmp_path / name).chmod(0)
    try:
        [item] = ps.plan(_modules("orders"), tmp_path)
        found = ps.orphans(tmp_path, _modules("orders"), "(unnamed)")
    finally:
        for name in ("orders_schema.py", "events_schema.py"):
            (tmp_path / name).chmod(0o644)

    assert item.status == "foreign" and found == []


def test_another_products_modules_in_the_same_directory_are_not_orphans(tmp_path: Path):
    one = {**_contract(_field("a"), port="orders"), "id": "one"}
    two = {**_contract(_field("a"), port="events"), "id": "two"}
    for contract in (one, two):
        rendered = ps.render_contract(contract)
        ps.write(ps.plan(rendered.modules, tmp_path))
    gone = ps.render_contract({**one, "exposes": []})

    assert ps.orphans(tmp_path, ps.render_contract(one).modules, "one") == []
    assert ps.orphans(tmp_path, ps.render_contract(two).modules, "two") == []
    assert ps.orphans(tmp_path, gone.modules, gone.product) == ["orders_schema.py"]


# ── Against real pyspark, when it is installed ──────────────────────────


def test_the_module_is_the_schema_real_pyspark_reads_back(tmp_path: Path):
    pytest.importorskip("pyspark")
    from pyspark.sql.types import StructType

    source = _source(
        _field("id", required=True),
        _field("amount", "decimal(12,2)"),
        _field("at", "timestamp_ntz"),
        _field("tags", "array", element={"type": "string", "required": True}),
        _field("m", "map", key={"type": "string"}, value={"type": "long"}),
        _field("s", "struct", fields=[_field("x", "smallint", required=True)]),
    )
    namespace: Dict[str, Any] = {}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - generated here

    schema = namespace["SCHEMA"]
    assert isinstance(schema, StructType)
    assert schema.jsonValue() == {
        "type": "struct",
        "fields": [
            {"name": "id", "type": "string", "nullable": False, "metadata": {}},
            {"name": "amount", "type": "decimal(12,2)", "nullable": True, "metadata": {}},
            {"name": "at", "type": "timestamp_ntz", "nullable": True, "metadata": {}},
            {
                "name": "tags",
                "type": {"type": "array", "elementType": "string", "containsNull": False},
                "nullable": True,
                "metadata": {},
            },
            {
                "name": "m",
                "type": {
                    "type": "map",
                    "keyType": "string",
                    "valueType": "long",
                    "valueContainsNull": True,
                },
                "nullable": True,
                "metadata": {},
            },
            {
                "name": "s",
                "type": {
                    "type": "struct",
                    "fields": [{"name": "x", "type": "short", "nullable": False, "metadata": {}}],
                },
                "nullable": True,
                "metadata": {},
            },
        ],
    }


def test_the_emitter_never_imports_pyspark(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyspark", None)  # an import of it would raise

    assert ps.render_contract(_contract(_field("a"), _field("t", "timestamp_ntz"))).modules
