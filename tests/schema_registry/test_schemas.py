# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Columns to Avro / JSON Schema and back."""

from __future__ import annotations

import json

import pytest

from fluid_build.schema_registry.schemas import (
    UnsupportedSchema,
    avro_columns,
    avro_name,
    avro_schema,
    columns_from_registry,
    derive,
    json_columns,
    json_schema,
    registry_format,
    schema_text,
)

pytestmark = pytest.mark.unit


def _avro_field(columns, name="v"):
    record = avro_schema(columns, name="rec")
    return next(f for f in record["fields"] if f["name"] == name)


def _one(type_, **extra):
    return [{"name": "v", "type": type_, "required": True, **extra}]


# --------------------------------------------------------------------- Avro types


@pytest.mark.parametrize(
    "type_,expected",
    [
        ("string", "string"),
        ("int", "int"),
        ("long", "long"),
        ("float", "float"),
        ("double", "double"),
        ("boolean", "boolean"),
        ("binary", "bytes"),
    ],
)
def test_avro_primitives(type_, expected):
    assert _avro_field(_one(type_))["type"] == expected


def test_avro_date():
    assert _avro_field(_one("date"))["type"] == {"type": "int", "logicalType": "date"}


@pytest.mark.parametrize(
    "type_,logical",
    [
        ("timestamp(3)", "local-timestamp-millis"),
        ("timestamp(0)", "local-timestamp-millis"),
        ("timestamp(6)", "local-timestamp-micros"),
        ("timestamp", "local-timestamp-micros"),
        ("timestamp_ltz", "timestamp-micros"),
        ("timestamp_ltz(3)", "timestamp-millis"),
        ("timestamp_ltz(6)", "timestamp-micros"),
    ],
)
def test_avro_timestamps(type_, logical):
    assert _avro_field(_one(type_))["type"] == {"type": "long", "logicalType": logical}


def test_avro_decimal():
    assert _avro_field(_one("decimal(18,2)"))["type"] == {
        "type": "bytes",
        "logicalType": "decimal",
        "precision": 18,
        "scale": 2,
    }


def test_avro_struct_is_a_record():
    cols = _one("struct", fields=[{"name": "x", "type": "int", "required": True}])
    field = _avro_field(cols)["type"]
    assert field["type"] == "record" and field["name"] == "v"
    assert field["fields"] == [{"name": "x", "type": "int"}]


def test_avro_array_and_map():
    arr = _one("array", element={"type": "string", "required": True})
    assert _avro_field(arr)["type"] == {"type": "array", "items": "string"}
    mp = _one(
        "map",
        key={"type": "string", "required": True},
        value={"type": "long", "required": True},
    )
    assert _avro_field(mp)["type"] == {"type": "map", "values": "long"}


def test_avro_nullable_is_union_with_null_default():
    cols = [{"name": "v", "type": "string"}]
    field = _avro_field(cols)
    assert field["type"] == ["null", "string"]
    assert field["default"] is None


def test_avro_required_has_no_default():
    field = _avro_field(_one("string"))
    assert field["type"] == "string" and "default" not in field


def test_avro_nullable_nested_element():
    cols = _one("array", element={"type": "int", "required": False})
    assert _avro_field(cols)["type"] == {"type": "array", "items": ["null", "int"]}


def test_avro_record_name_and_namespace():
    record = avro_schema(_one("string"), name="orders.v1", namespace="fluid.x")
    assert record["name"] == "orders_v1"
    assert record["namespace"] == "fluid.x"
    assert record["type"] == "record"


def test_avro_no_namespace_by_default():
    assert "namespace" not in avro_schema(_one("string"), name="r")


def test_avro_unknown_type_is_refused():
    with pytest.raises(ValueError):
        avro_schema(_one("uuid4000"), name="r")


def test_avro_names_reuse_the_writers_record_names():
    cols = _one("struct", fields=[{"name": "x", "type": "int", "required": True}])
    record = avro_schema(cols, name="r", names={"v": "com.acme.Inner"})
    inner = record["fields"][0]["type"]
    assert inner["name"] == "Inner"
    assert inner["namespace"] == "com.acme"


def test_avro_names_without_namespace():
    cols = _one("struct", fields=[{"name": "x", "type": "int", "required": True}])
    inner = avro_schema(cols, name="r", names={"v": "Inner"})["fields"][0]["type"]
    assert inner["name"] == "Inner" and "namespace" not in inner


def test_avro_a_repeated_record_name_is_defined_once():
    struct = {"type": "struct", "fields": [{"name": "x", "type": "int", "required": True}]}
    cols = [
        {"name": "a", **struct, "required": True},
        {"name": "b", **struct, "required": True},
    ]
    record = avro_schema(cols, name="r", names={"a": "Same", "b": "Same"})
    first, second = (f["type"] for f in record["fields"])
    assert first["name"] == "Same" and second == "Same"  # the producer's one record, by name


def test_avro_a_reused_writer_record_round_trips_by_name():
    writer = {
        "type": "record",
        "name": "Order",
        "namespace": "com.x",
        "fields": [
            {
                "name": "billing",
                "type": {
                    "type": "record",
                    "name": "Address",
                    "fields": [{"name": "c", "type": "string"}],
                },
            },
            {"name": "shipping", "type": "Address"},
        ],
    }
    columns, full, names = avro_columns(writer)
    reader = avro_schema(columns, name="Order", namespace="com.x", names=names)
    assert reader["fields"][1]["type"] == "com.x.Address"
    assert avro_columns(reader)[0] == columns


def test_avro_a_struct_named_like_the_record_gets_its_own_name():
    cols = [{"name": "customer", "type": "struct", "fields": [{"name": "id", "type": "string"}]}]
    record = avro_schema(cols, name="customer", namespace="fluid.p")
    nested = record["fields"][0]["type"][1]
    assert nested["name"] != "customer"


def test_avro_a_decimal_without_precision_is_unsupported_not_a_crash():
    writer = {
        "type": "record",
        "name": "R",
        "fields": [{"name": "d", "type": {"type": "bytes", "logicalType": "decimal"}}],
    }
    with pytest.raises(UnsupportedSchema):
        avro_columns(writer)


@pytest.mark.parametrize(
    "text,expected",
    [("orders", "orders"), ("a.b-c", "a_b_c"), ("1abc", "_1abc"), ("", "_"), ("ok_1", "ok_1")],
)
def test_avro_name(text, expected):
    assert avro_name(text) == expected


# -------------------------------------------------------------------- JSON Schema


def _json_prop(columns, name="v"):
    return json_schema(columns, title="t")["properties"][name]


def test_json_document_frame():
    doc = json_schema(_one("string"), title="Orders")
    assert doc["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert doc["title"] == "Orders" and doc["type"] == "object"
    assert doc["required"] == ["v"]
    assert "additionalProperties" not in doc  # undeclared fields stay allowed


@pytest.mark.parametrize(
    "type_,expected",
    [
        ("string", {"type": "string"}),
        ("int", {"type": "integer"}),
        ("long", {"type": "integer"}),
        ("float", {"type": "number"}),
        ("double", {"type": "number"}),
        ("decimal(10,2)", {"type": "number"}),
        ("boolean", {"type": "boolean"}),
        ("binary", {"type": "string", "contentEncoding": "base64"}),
        ("date", {"type": "string", "format": "date"}),
        ("timestamp", {"type": "string", "format": "date-time"}),
        ("timestamp(3)", {"type": "string", "format": "date-time"}),
        ("timestamp_ltz", {"type": "string", "format": "date-time"}),
    ],
)
def test_json_types(type_, expected):
    assert _json_prop(_one(type_)) == expected


def test_json_nullable_adds_null_to_type():
    assert _json_prop([{"name": "v", "type": "int"}]) == {"type": ["integer", "null"]}


def test_json_optional_columns_are_not_required():
    doc = json_schema([{"name": "v", "type": "int"}], title="t")
    assert "required" not in doc


def test_json_struct_array_map():
    struct = _one("struct", fields=[{"name": "x", "type": "int", "required": True}])
    assert _json_prop(struct) == {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    arr = _one("array", element={"type": "string", "required": True})
    assert _json_prop(arr) == {"type": "array", "items": {"type": "string"}}
    mp = _one(
        "map", key={"type": "string", "required": True}, value={"type": "long", "required": True}
    )
    assert _json_prop(mp) == {"type": "object", "additionalProperties": {"type": "integer"}}


# ----------------------------------------------------------------- schema_text


class TestSchemaText:
    def test_canonical_form(self):
        assert schema_text({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'

    def test_key_order_is_irrelevant(self):
        assert schema_text({"a": 1, "b": {"y": 1, "x": 2}}) == schema_text(
            {"b": {"x": 2, "y": 1}, "a": 1}
        )

    def test_deterministic_for_the_same_input(self):
        cols = _one("struct", fields=[{"name": "x", "type": "int"}])
        first = schema_text(avro_schema(cols, name="r", namespace="n"))
        second = schema_text(avro_schema(cols, name="r", namespace="n"))
        assert first == second and first.encode() == second.encode()

    def test_column_order_matters(self):
        a = [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}]
        assert schema_text(avro_schema(a, name="r")) != schema_text(
            avro_schema(list(reversed(a)), name="r")
        )

    def test_no_whitespace_and_unicode_kept(self):
        text = schema_text({"k": "é x"})
        assert text == '{"k":"é x"}'

    def test_is_valid_json(self):
        doc = json_schema(_one("string"), title="t")
        assert json.loads(schema_text(doc)) == doc


def test_registry_format():
    assert registry_format("json") == "JSON"
    assert registry_format("json-confluent") == "JSON"
    assert registry_format("avro-confluent") == "AVRO"
    assert registry_format("avro") == "AVRO"
    # Any other format is refused, never read as Avro (it used to fall through to AVRO).
    for other in ("", "protobuf", "xml", "JSON", "avro-confluent-v2"):
        assert registry_format(other) is None


def test_derive_dispatches_on_format():
    cols = _one("string")
    assert derive(cols, "JSON", name="t")["$schema"].startswith("http://json-schema")
    assert derive(cols, "AVRO", name="t")["type"] == "record"


# -------------------------------------------------------------------- round trips

ROUND_TRIP = [
    {"name": "s", "type": "string", "required": True},
    {"name": "i", "type": "int", "required": False},
    {"name": "l", "type": "long", "required": True},
    {"name": "f", "type": "float", "required": False},
    {"name": "d", "type": "double", "required": True},
    {"name": "b", "type": "boolean", "required": False},
    {"name": "bin", "type": "binary", "required": False},
    {"name": "dt", "type": "date", "required": True},
    {"name": "ts", "type": "timestamp", "required": False},
    {"name": "tz", "type": "timestamp_ltz", "required": True},
    {"name": "dec", "type": "decimal(10,2)", "required": False},
    {
        "name": "st",
        "type": "struct",
        "required": True,
        "fields": [{"name": "x", "type": "int", "required": True}],
    },
    {
        "name": "ar",
        "type": "array",
        "required": True,
        "element": {"type": "string", "required": True},
    },
    {
        "name": "mp",
        "type": "map",
        "required": False,
        "key": {"type": "string", "required": True},
        "value": {"type": "long", "required": True},
    },
]


def test_avro_round_trip():
    columns, full, names = avro_columns(avro_schema(ROUND_TRIP, name="rec", namespace="ns"))
    assert full == "ns.rec"
    assert names == {"st": "ns.st"} or names == {"st": "st"}
    assert columns == ROUND_TRIP


def test_avro_round_trip_accepts_text():
    text = schema_text(avro_schema(ROUND_TRIP, name="rec"))
    columns, full, _ = avro_columns(text)
    assert full == "rec" and columns == ROUND_TRIP


def test_avro_round_trip_through_a_second_derive_is_stable():
    first = avro_schema(ROUND_TRIP, name="rec")
    again = avro_schema(avro_columns(first)[0], name="rec")
    assert schema_text(first) == schema_text(again)


def test_json_round_trip_widens_to_what_json_can_carry():
    columns = json_columns(json_schema(ROUND_TRIP, title="t"))
    by_name = {c["name"]: c for c in columns}
    assert by_name["i"]["type"] == "long"  # int and long are both integer
    assert by_name["f"]["type"] == "double" and by_name["dec"]["type"] == "double"
    assert by_name["ts"]["type"] == "timestamp_ltz"
    assert by_name["bin"]["type"] == "binary" and by_name["dt"]["type"] == "date"
    assert by_name["st"]["fields"] == [{"name": "x", "type": "long", "required": True}]
    assert [c["required"] for c in columns] == [c["required"] for c in ROUND_TRIP]


def test_json_round_trip_is_a_fixed_point_after_one_pass():
    once = json_columns(json_schema(ROUND_TRIP, title="t"))
    twice = json_columns(json_schema(once, title="t"))
    assert once == twice


def test_columns_from_registry_dispatch():
    assert columns_from_registry(avro_schema(_one("int"), name="r"), "avro") == _one("int")
    assert columns_from_registry(json_schema(_one("string"), title="t"), "JSON") == _one("string")


def test_columns_from_registry_refuses_protobuf():
    with pytest.raises(UnsupportedSchema, match="PROTOBUF"):
        columns_from_registry("syntax = 'proto3';", "PROTOBUF")


# ------------------------------------------------------------ Avro reader specifics


def test_avro_enum_reads_as_string_and_fixed_as_binary():
    schema = {
        "type": "record",
        "name": "r",
        "fields": [
            {"name": "e", "type": {"type": "enum", "name": "E", "symbols": ["A", "B"]}},
            {"name": "f", "type": {"type": "fixed", "name": "F", "size": 4}},
        ],
    }
    columns = avro_columns(schema)[0]
    assert [c["type"] for c in columns] == ["string", "binary"]


def test_avro_named_type_reference_resolves():
    inner = {"type": "record", "name": "In", "fields": [{"name": "x", "type": "int"}]}
    schema = {
        "type": "record",
        "name": "r",
        "namespace": "n",
        "fields": [{"name": "a", "type": inner}, {"name": "b", "type": "n.In"}],
    }
    columns = avro_columns(schema)[0]
    assert columns[0]["type"] == columns[1]["type"] == "struct"
    assert columns[1]["fields"] == columns[0]["fields"]


def test_avro_field_doc_becomes_description():
    schema = {"type": "record", "name": "r", "fields": [{"name": "a", "type": "int", "doc": "hi"}]}
    assert avro_columns(schema)[0][0]["description"] == "hi"


def test_avro_fixed_decimal():
    schema = {
        "type": "record",
        "name": "r",
        "fields": [
            {
                "name": "d",
                "type": {
                    "type": "fixed",
                    "name": "D",
                    "size": 8,
                    "logicalType": "decimal",
                    "precision": 12,
                },
            }
        ],
    }
    assert avro_columns(schema)[0][0]["type"] == "decimal(12,0)"


# ------------------------------------------------------------------- unsupported


@pytest.mark.parametrize(
    "type_",
    [
        ["string", "int"],
        ["null", "string", "int"],
        ["null", "null"],
    ],
)
def test_avro_other_unions_are_unsupported(type_):
    schema = {"type": "record", "name": "r", "fields": [{"name": "a", "type": type_}]}
    with pytest.raises(UnsupportedSchema, match="union"):
        avro_columns(schema)


def test_avro_non_record_is_unsupported():
    with pytest.raises(UnsupportedSchema, match="not an Avro record"):
        avro_columns({"type": "array", "items": "string"})


def test_avro_unknown_named_type_is_unsupported():
    schema = {"type": "record", "name": "r", "fields": [{"name": "a", "type": "Ghost"}]}
    with pytest.raises(UnsupportedSchema, match="unknown Avro type"):
        avro_columns(schema)


def test_avro_field_without_a_name_is_unsupported():
    with pytest.raises(UnsupportedSchema, match="no name"):
        avro_columns({"type": "record", "name": "r", "fields": [{"type": "int"}]})


def test_avro_recursion_is_bounded():
    node: dict = {"type": "record", "name": "n0", "fields": [{"name": "x", "type": "int"}]}
    for i in range(1, 40):
        node = {"type": "record", "name": f"n{i}", "fields": [{"name": "c", "type": node}]}
    with pytest.raises(UnsupportedSchema, match="deeper than 32"):
        avro_columns(node)


def test_avro_text_that_is_not_json_raises_value_error():
    with pytest.raises(ValueError):
        avro_columns("{not json")


@pytest.mark.parametrize(
    "prop",
    [
        {"$ref": "http://example.com/other.json#/x"},
        {"$ref": "other.json"},
        {"$ref": "#/definitions/missing"},
        {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]},
        {"type": ["string", "integer"]},
        {"type": "weird"},
        {},
    ],
)
def test_json_unsupported_constructs(prop):
    schema = {"type": "object", "properties": {"a": prop}}
    with pytest.raises(UnsupportedSchema):
        json_columns(schema)


def test_json_internal_ref_is_resolved():
    schema = {
        "type": "object",
        "definitions": {"Addr": {"type": "object", "properties": {"z": {"type": "string"}}}},
        "properties": {"a": {"$ref": "#/definitions/Addr"}},
    }
    (col,) = json_columns(schema)
    assert col["type"] == "struct" and col["fields"][0]["name"] == "z"


def test_json_one_of_null_is_nullable():
    schema = {
        "type": "object",
        "properties": {"a": {"oneOf": [{"type": "null"}, {"type": "string"}]}},
    }
    assert json_columns(schema) == [{"name": "a", "type": "string", "required": False}]


def test_json_required_only_when_listed_and_not_null():
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": ["string", "null"]}, "b": {"type": "string"}},
    }
    assert [c["required"] for c in json_columns(schema)] == [False, False]


def test_json_non_object_is_unsupported():
    with pytest.raises(UnsupportedSchema, match="not a JSON Schema object"):
        json_columns({"type": "array"})


def test_json_description_is_carried():
    schema = {"type": "object", "properties": {"a": {"type": "string", "description": "d"}}}
    assert json_columns(schema)[0]["description"] == "d"
