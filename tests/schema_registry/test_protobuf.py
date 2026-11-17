# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Protobuf for produced subjects: the generated proto3 text, the registry kinds' decimal
rule, the validate errors, and the 0.7.7 ``protobuf: {field: N}`` column key."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.schema_registry.kinds import get_kind
from fluid_build.schema_registry.policy import validation_findings
from fluid_build.schema_registry.produced import (
    ProducedSubjectError,
    data_format,
    schema_definition,
)
from fluid_build.schema_registry.reference import produced_subjects
from fluid_build.schema_registry.schemas import protobuf_schema

from .conftest import produced_contract

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parents[1] / "fixtures" / "schema_registry" / "produced_protobuf_077.yaml"
AVRO_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "schema_registry" / "produced_subjects_077.yaml"
)
SCHEMAS = Path(__file__).parents[2] / "fluid_build" / "schemas"
CONFLUENT_DECIMAL = get_kind("confluent").protobuf_decimal
GLUE_HINT = get_kind("glue").protobuf_decimal_hint


def col(name, type_, number=None, required=False, **extra):
    column = {"name": name, "type": type_, **extra}
    if required:
        column["required"] = True
    if number is not None:
        column["protobuf"] = {"field": number}
    return column


def proto(columns, **kw):
    kw.setdefault("name", "Orders")
    kw.setdefault("package", "acme.orders")
    return protobuf_schema(columns, **kw)


FIXTURE_COLUMNS = [
    col("id", "string", 1, required=True),
    col("count", "int", 2),
    col("total", "long", 3, required=True),
    col("ratio", "float", 4),
    col("score", "double", 5),
    col("active", "boolean", 6, required=True),
    col("blob", "binary", 7),
    col("born", "date", 8),
    col("seen", "timestamp", 9, required=True),
    col("seen_at", "timestamp_ltz", 10),
    col("price", "decimal(10,2)", 11, required=True),
    col(
        "address",
        "struct",
        12,
        fields=[col("city", "string", 1, required=True), col("zip", "string", 2)],
    ),
    col(
        "lines",
        "array",
        13,
        element={
            "type": "struct",
            "fields": [col("sku", "string", 1, required=True), col("qty", "int", 2, required=True)],
        },
    ),
    col("labels", "map", 14, key={"type": "string", "required": True}, value={"type": "string"}),
    col("tags", "array", 15, element={"type": "string"}),
    col(
        "stats",
        "map",
        16,
        key={"type": "string", "required": True},
        value={"type": "struct", "fields": [col("n", "long", 1)]},
    ),
]

EXPECTED = """\
syntax = "proto3";

package acme.orders;

import "confluent/type/decimal.proto";
import "google/protobuf/timestamp.proto";
import "google/type/date.proto";

message Orders {
  message Address {
    string city = 1;
    optional string zip = 2;
  }
  message Lines {
    string sku = 1;
    int32 qty = 2;
  }
  message Stats {
    optional int64 n = 1;
  }
  string id = 1;
  optional int32 count = 2;
  int64 total = 3;
  optional float ratio = 4;
  optional double score = 5;
  bool active = 6;
  optional bytes blob = 7;
  optional .google.type.Date born = 8;
  .google.protobuf.Timestamp seen = 9;
  optional .google.protobuf.Timestamp seen_at = 10;
  .confluent.type.Decimal price = 11;
  Address address = 12;
  repeated Lines lines = 13;
  map<string, string> labels = 14;
  repeated string tags = 15;
  map<string, Stats> stats = 16;
}
"""


# ---------------------------------------------------------------- the text


def test_the_exact_text():
    assert proto(FIXTURE_COLUMNS, decimal=CONFLUENT_DECIMAL) == EXPECTED


def test_deterministic():
    first = proto(copy.deepcopy(FIXTURE_COLUMNS), decimal=CONFLUENT_DECIMAL)
    assert first == proto(copy.deepcopy(FIXTURE_COLUMNS), decimal=CONFLUENT_DECIMAL)


def test_no_import_block_when_nothing_needs_one():
    assert proto([col("a", "string", 1, required=True)]) == (
        'syntax = "proto3";\n\npackage acme.orders;\n\nmessage Orders {\n  string a = 1;\n}\n'
    )


def test_imports_only_when_used_sorted_and_once():
    text = proto(
        [
            col("t1", "timestamp", 1, required=True),
            col("d", "date", 2, required=True),
            col("t2", "timestamp_ltz", 3, required=True),
        ]
    )
    imports = [line for line in text.splitlines() if line.startswith("import")]
    assert imports == [
        'import "google/protobuf/timestamp.proto";',
        'import "google/type/date.proto";',
    ]
    assert "confluent" not in text


def test_a_well_known_type_inside_a_nested_message_is_imported():
    text = proto(
        [col("a", "array", 1, element={"type": "struct", "fields": [col("d", "date", 1)]})]
    )
    assert 'import "google/type/date.proto";' in text
    assert "    optional .google.type.Date d = 1;" in text


def test_fields_keep_declared_order_not_number_order():
    text = proto([col("b", "string", 9, required=True), col("a", "string", 2, required=True)])
    assert text.index("string b = 9;") < text.index("string a = 2;")


def test_required_and_optional_labels():
    text = proto(
        [
            col("r", "string", 1, required=True),
            col("n", "string", 2),
            col("arr", "array", 3, element={"type": "string"}),
            col("m", "map", 4, key={"type": "string", "required": True}, value={"type": "long"}),
            col("s", "struct", 5, fields=[col("x", "string", 1)]),
        ]
    )
    assert "  string r = 1;" in text
    assert "  optional string n = 2;" in text
    assert "  repeated string arr = 3;" in text
    assert "  map<string, int64> m = 4;" in text
    assert "  S s = 5;" in text and "optional S" not in text


def test_columns_whose_json_names_collide_are_refused():
    # proto3 refuses fields equal after lowercasing with underscores dropped (protoc:
    # "The JSON camel-case name of field ... conflicts"), so validate refuses them first.
    raises([col("foo_bar", "string", 1), col("fooBar", "string", 2)], "same Protobuf JSON name")
    raises(
        [col("a_b", "struct", 1, fields=[col("x", "string", 1)]), col("AB", "string", 2)],
        "same Protobuf JSON name",
    )


def test_a_column_named_like_a_maps_entry_type_is_refused():
    raises(
        [
            col(
                "tags", "map", 1, key={"type": "string", "required": True}, value={"type": "string"}
            ),
            col("TagsEntry", "string", 2),
        ],
        "Protobuf defines TagsEntry for this map",
    )


def test_a_struct_named_like_a_maps_entry_type_takes_the_field_number():
    text = proto(
        [
            col(
                "tags", "map", 1, key={"type": "string", "required": True}, value={"type": "string"}
            ),
            col("tags_entry", "struct", 2, fields=[col("x", "string", 1)]),
        ]
    )
    # protoc defines TagsEntry for the map itself; the struct's message steps aside.
    assert "  message TagsEntry_2 {" in text and "  TagsEntry_2 tags_entry = 2;" in text
    assert "message TagsEntry {" not in text


def test_well_known_types_are_absolute_so_a_message_named_google_cannot_shadow_them():
    text = proto([col("at", "timestamp", 1), col("day", "date", 2)], name="google")
    assert "message google {" in text
    assert "optional .google.protobuf.Timestamp at = 1;" in text
    assert "optional .google.type.Date day = 2;" in text


def test_package_and_name_are_sanitised():
    text = proto([col("a", "string", 1, required=True)], name="9-orders.v1", package="1x.my-pkg")
    assert "package f_1x.my_pkg;" in text
    assert "message f_9_orders_v1 {" in text


def test_the_decimal_arrives_as_the_kinds_type():
    text = proto([col("d", "decimal(5,2)", 1)], decimal=("my.Decimal", "my/decimal.proto"))
    assert 'import "my/decimal.proto";' in text and "optional .my.Decimal d = 1;" in text


# ---------------------------------------------------------------- refusals


def raises(columns, match, **kw):
    with pytest.raises(ValueError, match=match):
        proto(columns, **kw)


def test_a_decimal_without_a_kind_type_is_refused_with_the_hint():
    raises([col("d", "decimal(5,2)", 1)], "no decimal type", decimal_hint=GLUE_HINT)
    raises([col("d", "decimal(5,2)", 1)], r"^d: ")


def test_a_missing_field_number_names_the_column_path():
    raises(
        [col("a", "string", 1), col("b", "string")], r"b: a Protobuf column needs protobuf.field"
    )
    raises(
        [col("s", "struct", 1, fields=[col("inner", "string")])],
        r"s\.inner: a Protobuf column needs protobuf.field",
    )
    raises(
        [
            col(
                "arr",
                "array",
                1,
                element={"type": "struct", "fields": [col("inner", "string")]},
            )
        ],
        r"arr\[\]\.inner: a Protobuf column needs",
    )


def test_a_duplicate_number_names_both_columns():
    raises([col("a", "string", 1), col("b", "string", 1)], "b: protobuf.field 1 is also a's")
    # the same number in different messages is fine
    proto([col("a", "string", 1), col("s", "struct", 2, fields=[col("a", "string", 1)])])


@pytest.mark.parametrize("number", [0, -1, 19000, 19500, 19999, 536870912])
def test_an_out_of_range_number_is_refused(number):
    raises([col("a", "string", number)], "out of range")


@pytest.mark.parametrize("number", [1, 18999, 20000, 536870911])
def test_the_range_edges_are_accepted(number):
    proto([col("a", "string", number, required=True)])


@pytest.mark.parametrize("number", ["3", 1.5, True])
def test_a_non_integer_number_is_refused(number):
    raises([col("a", "string", number)], "must be an integer")


def test_arrays_and_maps_of_containers_are_refused():
    raises(
        [col("a", "array", 1, element={"type": "array", "element": {"type": "string"}})],
        "an array of arrays/maps has no Protobuf field; wrap it in a struct",
    )
    raises(
        [
            col(
                "a",
                "array",
                1,
                element={
                    "type": "map",
                    "key": {"type": "string", "required": True},
                    "value": {"type": "string"},
                },
            )
        ],
        "an array of arrays/maps",
    )
    raises(
        [
            col(
                "m",
                "map",
                1,
                key={"type": "string", "required": True},
                value={"type": "array", "element": {"type": "string"}},
            )
        ],
        "a map of arrays/maps has no Protobuf field; wrap it in a struct",
    )


def test_a_column_name_that_is_no_proto_identifier_is_refused():
    with pytest.raises(ValueError, match="_x"):
        proto([col("_x", "string", 1)])


def test_the_stream_type_rules_still_apply():
    with pytest.raises(ValueError, match="unsupported streaming type"):
        proto([col("a", "uuid", 1)])
    with pytest.raises(ValueError, match="duplicate field"):
        proto([col("a", "string", 1), col("A", "string", 2)])


def test_a_package_is_required():
    with pytest.raises(ValueError, match="package"):
        proto([col("a", "string", 1)], package="")


# ---------------------------------------------------------------- the kinds


def test_the_kinds_decimal_rule():
    assert get_kind("confluent").protobuf_decimal == (
        "confluent.type.Decimal",
        "confluent/type/decimal.proto",
    )
    glue = get_kind("glue")
    assert glue.protobuf_decimal is None
    assert "carry this topic as Avro or JSON Schema" in glue.protobuf_decimal_hint


# ---------------------------------------------------------------- produced


def protobuf_contract(kind, columns):
    registry = (
        {"kind": "glue", "registryName": "c"}
        if kind == "glue"
        else {"kind": "confluent", "url": "https://sr.example.com"}
    )
    contract = produced_contract(
        registry,
        platform="kafka" if kind == "glue" else "confluent",
        serialization="protobuf",
    )
    contract["exposes"][0]["contract"]["schema"] = copy.deepcopy(columns)
    return contract


COLUMNS = [col("id", "string", 1, required=True), col("email", "string", 2)]


@pytest.mark.parametrize("kind", ["glue", "confluent"])
def test_a_protobuf_subject_registers(kind):
    contract = protobuf_contract(kind, COLUMNS)
    errors, _ = validation_findings(contract, environ={})
    assert errors == []
    (subject,) = produced_subjects(contract)
    assert data_format(subject) == "PROTOBUF"
    fmt, text = schema_definition(subject, "fluid.t_producer")
    assert fmt == "PROTOBUF"
    assert text == (
        'syntax = "proto3";\n\npackage fluid.t_producer;\n\n'
        "message evt {\n  string id = 1;\n  optional string email = 2;\n}\n"
    )
    assert not text.startswith("{")


@pytest.mark.parametrize("kind", ["glue", "confluent"])
def test_a_missing_field_number_is_a_validate_error(kind):
    contract = protobuf_contract(kind, [col("id", "string", 1), col("email", "string")])
    errors, _ = validation_findings(contract, environ={})
    assert any("email: a Protobuf column needs protobuf.field" in e for e in errors), errors


@pytest.mark.parametrize("kind", ["glue", "confluent"])
def test_a_duplicate_field_number_is_a_validate_error(kind):
    contract = protobuf_contract(kind, [col("id", "string", 4), col("email", "string", 4)])
    errors, _ = validation_findings(contract, environ={})
    assert any("protobuf.field 4 is also id's" in e for e in errors), errors


def test_a_glue_decimal_is_a_validate_error_and_a_confluent_one_is_not():
    columns = [col("id", "string", 1), col("amount", "decimal(10,2)", 2)]
    errors, _ = validation_findings(protobuf_contract("glue", columns), environ={})
    assert any("resolves no decimal type for Protobuf" in e and "amount" in e for e in errors)
    errors, _ = validation_findings(protobuf_contract("confluent", columns), environ={})
    assert errors == []


def test_the_error_carries_the_protobuf_kind():
    contract = protobuf_contract("glue", [col("a", "string")])
    (subject,) = produced_subjects(contract)
    with pytest.raises(ProducedSubjectError) as caught:
        schema_definition(subject, "fluid.t")
    assert caught.value.kind == "schema-registry-protobuf"


def test_other_formats_are_still_refused_and_the_message_names_protobuf():
    contract = produced_contract({"kind": "glue", "registryName": "c"}, serialization="xml")
    (subject,) = produced_subjects(contract)
    with pytest.raises(ProducedSubjectError, match="Avro, JSON Schema and Protobuf"):
        data_format(subject)


def test_a_protobuf_subject_gets_no_pii_field_tags():
    from fluid_build.schema_registry.produced import classification_metadata

    contract = protobuf_contract("confluent", COLUMNS)
    expose = contract["exposes"][0]
    expose["contract"]["schema"][1]["sensitivity"] = "pii"
    expose["policy"] = {"classification": "Confidential"}
    assert classification_metadata(contract, expose, "fluid.t") == {
        "properties": {"fluid_classification": "Confidential"}
    }


def test_avro_and_json_subjects_are_unchanged():
    contract = yaml.safe_load(AVRO_FIXTURE.read_text())
    avro, jsn = None, None
    for subject in produced_subjects(contract):
        fmt, text = schema_definition(subject, "fluid.commerce_orders_enriched")
        if fmt == "AVRO":
            avro = text
        else:
            jsn = text
    assert avro == (
        '{"fields":[{"name":"order_id","type":"string"},{"default":null,"name":"email",'
        '"type":["null","string"]},{"default":null,"name":"amount","type":["null",'
        '{"logicalType":"decimal","precision":18,"scale":2,"type":"bytes"}]}],'
        '"name":"commerce_orders_enriched","namespace":"fluid.commerce_orders_enriched",'
        '"type":"record"}'
    )
    assert json.loads(jsn)["$schema"] == "http://json-schema.org/draft-07/schema#"


# ---------------------------------------------------------------- the fixture


def test_the_fixture_has_one_glue_and_one_confluent_protobuf_subject():
    contract = yaml.safe_load(FIXTURE.read_text())
    errors, _ = validation_findings(contract, environ={})
    assert errors == []
    kinds = {
        s.ref.kind: schema_definition(s, "fluid.commerce_orders_proto")
        for s in produced_subjects(contract)
    }
    assert set(kinds) == {"glue", "confluent"}
    assert all(fmt == "PROTOBUF" for fmt, _ in kinds.values())
    assert "confluent.type.Decimal amount = 2;" in kinds["confluent"][1]
    assert "map<string, string> labels = 7;" in kinds["glue"][1]


# ---------------------------------------------------------------- the schema


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _validate(manager, contract):
    return manager.validate_contract(contract, offline_only=True)


def _contract() -> dict:
    return yaml.safe_load(FIXTURE.read_text())


def _glue_columns(contract):
    return contract["exposes"][1]["contract"]["schema"]


def test_the_fixture_is_valid(manager):
    result = _validate(manager, _contract())
    assert result.is_valid, result.errors


def test_a_column_and_a_nested_struct_field_accept_protobuf(manager):
    contract = _contract()
    columns = _glue_columns(contract)
    columns[0]["protobuf"] = {"field": 3}
    columns[4]["fields"][0]["protobuf"] = {"field": 3}
    result = _validate(manager, contract)
    assert result.is_valid, result.errors


@pytest.mark.parametrize(
    "value",
    [
        {"field": 0},
        {"field": 19000},
        {"field": 19999},
        {"field": 536870912},
        {"field": "3"},
        {"field": 1.5},
        {"field": 3, "extra": True},
        {},
        3,
    ],
)
def test_a_bad_protobuf_key_is_refused(manager, value):
    contract = _contract()
    _glue_columns(contract)[0]["protobuf"] = value
    assert not _validate(manager, contract).is_valid, value


@pytest.mark.parametrize("number", [1, 18999, 20000, 536870911])
def test_the_edges_the_schema_accepts(manager, number):
    contract = _contract()
    _glue_columns(contract)[0]["protobuf"] = {"field": number}
    assert _validate(manager, contract).is_valid


def test_0_7_6_refuses_the_key():
    schema = json.loads((SCHEMAS / "fluid-schema-0.7.6.json").read_text())
    assert "protobuf" not in schema["$defs"]["column"]["properties"]
    assert schema["$defs"]["column"]["additionalProperties"] is False
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    assert not result.is_valid


def test_a_contract_without_the_key_is_untouched():
    from fluid_build.forge.core.plan_digest import compute_contract_digest

    contract = yaml.safe_load(AVRO_FIXTURE.read_text())
    assert "protobuf" not in json.dumps(contract)
    # nothing in the loader or validator adds the key: the digest is a function of the text
    assert compute_contract_digest(contract) == compute_contract_digest(copy.deepcopy(contract))
    result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    assert result.is_valid
    assert "protobuf" not in json.dumps(contract)
