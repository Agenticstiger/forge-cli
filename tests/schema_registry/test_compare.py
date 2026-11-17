# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The plan-time comparison of a declared stream schema with a subject's columns."""

from __future__ import annotations

import pytest

from fluid_build.schema_registry.compare import describe, normalize, reader_changes

pytestmark = pytest.mark.unit


def col(name, type_="string", required=True):
    return {"name": name, "type": type_, "required": required}


def breaking(declared, writer, fmt="AVRO"):
    return reader_changes(declared, writer, data_format=fmt, topic="t")


def test_identical_schemas_have_no_changes():
    cols = [col("a"), col("b", "int", False)]
    assert breaking(cols, cols) == []


def test_widening_int_to_long_is_not_breaking():
    assert breaking([col("a", "long")], [col("a", "int")]) == []


def test_float_to_double_is_not_breaking():
    assert breaking([col("a", "double")], [col("a", "float")]) == []


def test_narrowing_long_to_int_is_breaking():
    (change,) = breaking([col("a", "int")], [col("a", "long")])
    assert change.severity == "breaking"
    assert "long" in str(change.description).lower() and "int" in str(change.description).lower()


def test_string_to_int_is_breaking():
    assert breaking([col("a", "int")], [col("a", "string")])


def test_new_required_reader_field_is_breaking():
    changes = breaking([col("a"), col("b")], [col("a")])
    assert changes and any("b" in str(c.description) for c in changes)


def test_new_optional_reader_field_is_not_breaking():
    assert breaking([col("a"), col("b", required=False)], [col("a")]) == []


def test_writer_only_field_is_not_breaking():
    assert breaking([col("a")], [col("a"), col("extra")]) == []


def test_nullable_writer_with_required_reader_is_breaking():
    assert breaking([col("a", required=True)], [col("a", required=False)])


def test_required_writer_with_nullable_reader_is_not_breaking():
    assert breaking([col("a", required=False)], [col("a", required=True)]) == []


def test_json_int_and_long_are_not_told_apart():
    assert breaking([col("a", "int")], [col("a", "long")], fmt="JSON") == []
    assert breaking([col("a", "long")], [col("a", "int")], fmt="JSON") == []


def test_avro_does_tell_them_apart():
    assert breaking([col("a", "int")], [col("a", "long")], fmt="AVRO")


def test_json_nullable_writer_with_required_reader_is_breaking():
    assert breaking([col("a")], [col("a", required=False)], fmt="JSON")


def test_json_new_required_field_is_breaking():
    assert breaking([col("a"), col("b")], [col("a")], fmt="JSON")


def test_only_breaking_changes_are_returned():
    changes = breaking([col("a"), col("b", required=False)], [col("a"), col("w")])
    assert all(c.severity == "breaking" for c in changes)


# ---------------------------------------------------------------- normalize


def test_normalize_avro_keeps_int_and_long():
    assert [c["type"] for c in normalize([col("a", "int"), col("b", "long")], "AVRO")] == [
        "int",
        "long",
    ]


def test_normalize_json_collapses_integers_and_floats():
    out = normalize([col("a", "int"), col("b", "float"), col("c", "decimal(5,2)")], "JSON")
    assert [c["type"] for c in out] == ["long", "double", "double"]


def test_normalize_is_idempotent():
    cols = [col("a", "int"), col("b", "timestamp", False)]
    once = normalize(cols, "AVRO")
    assert normalize(once, "AVRO") == once


# ----------------------------------------------------------------- describe


class _Change:
    def __init__(self, description):
        self.description = description


def test_describe_joins_with_semicolons():
    assert describe([_Change("one"), _Change("two")]) == "one; two"


def test_describe_truncates_with_a_count():
    changes = [_Change(f"c{i}") for i in range(11)]
    text = describe(changes)
    assert text.startswith("c0; c1") and "c7" in text and "c8" not in text
    assert text.endswith("... and 3 more")


def test_describe_limit_argument():
    assert describe([_Change("a"), _Change("b"), _Change("c")], limit=1) == "a; ... and 2 more"


def test_describe_empty():
    assert describe([]) == ""


@pytest.mark.parametrize(
    "writer, reader, fmt, ok",
    [
        ("long", "double", "AVRO", True),
        ("int", "float", "AVRO", True),
        ("date", "timestamp", "AVRO", False),
        ("decimal(10,2)", "decimal(12,4)", "AVRO", False),
        ("decimal(10,2)", "decimal(12,2)", "AVRO", True),
        ("decimal(12,2)", "decimal(10,2)", "AVRO", False),
        ("long", "double", "JSON", True),
        ("double", "long", "JSON", False),
    ],
)
def test_a_type_change_follows_the_formats_reader_rules(writer, reader, fmt, ok):
    from fluid_build.schema_registry.compare import reader_changes

    changes = reader_changes(
        [{"name": "x", "type": reader, "required": True}],
        [{"name": "x", "type": writer, "required": True}],
        data_format=fmt,
        topic="t",
    )
    assert (changes == []) is ok
