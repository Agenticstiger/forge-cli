# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Spark SQL dialect of ``recordMapping``, for the forge Spark runner (Spark 3.5).

The same declared mapping the managed Flink lane compiles, written for Spark: arrays
flattened with LATERAL VIEW inline/explode, built structs as named_struct with the same
null propagation, and the Kafka key and metadata read from Spark's Kafka source columns.
Every query here is also parsed with sqlglot's Spark dialect. The Flink output is pinned
by test_mapping.py and test_compiler.py, unchanged.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot.errors import ErrorLevel

from fluid_build.streaming.mapping import (
    compile_select,
    parse_stream,
    spark_schema,
    spark_source_select,
    spark_type,
)
from fluid_build.streaming.types import parse_fields, parse_type


def col(name, type_, **kw):
    return {"name": name, "type": type_, **kw}


VALUE = [
    col("event_id", "string", required=True),
    col(
        "customer",
        "struct",
        fields=[
            col("id", "string"),
            col("address", "struct", fields=[col("country", "string")]),
        ],
    ),
    col(
        "items",
        "array",
        element={
            "type": "struct",
            "fields": [col("sku", "string"), col("price", "decimal(18,2)")],
        },
    ),
    col("tags", "array", element={"type": "string"}),
    col(
        "attributes",
        "map",
        key={"type": "string", "required": True},
        value={"type": "string"},
    ),
    col("quantity", "int"),
    col("event_time", "timestamp_ltz(3)"),
]


def stream(**extra):
    return parse_stream(
        {"kind": "kafka", "topic": "orders", "format": "json", "schema": VALUE, **extra}
    )


def spark(mapping, target, **extra):
    sql = compile_select(stream(**extra), mapping, parse_fields(target), dialect="spark")
    _parses(sql)
    return sql


def _parses(sql):
    """Spark's grammar, as sqlglot reads it, accepts the query whole."""
    (tree,) = sqlglot.parse(sql, read="spark", error_level=ErrorLevel.RAISE)
    assert tree is not None


def select(*lines, join=""):
    return "SELECT\n  " + ",\n  ".join(lines) + "\nFROM `source_events` AS `src`" + join


# ── shapes ──────────────────────────────────────────────────────────────


def test_a_nested_stream_lands_as_it_is_with_no_mapping():
    target = [VALUE[0], VALUE[1], VALUE[2], VALUE[6]]
    assert spark(None, target) == select(
        "`src`.`event_id` AS `event_id`",
        "`src`.`customer` AS `customer`",
        "`src`.`items` AS `items`",
        "`src`.`event_time` AS `event_time`",
    )


def test_an_array_of_structs_explodes_with_inline_and_fields_lift():
    mapping = {
        "explode": {"path": "items", "as": "item"},
        "columns": [
            {"name": "customer_id", "from": "customer.id"},
            {"name": "country", "from": "customer.address.country"},
            {"name": "sku", "from": "item.sku"},
            {"name": "channel", "from": "attributes['channel']"},
        ],
    }
    target = [
        col("event_id", "string", required=True),
        col("customer_id", "string"),
        col("country", "string"),
        col("sku", "string"),
        col("channel", "string"),
    ]
    assert spark(mapping, target) == select(
        "`src`.`event_id` AS `event_id`",
        "`src`.`customer`.`id` AS `customer_id`",
        "`src`.`customer`.`address`.`country` AS `country`",
        "`elem`.`sku` AS `sku`",
        "`src`.`attributes`['channel'] AS `channel`",
        join="\nLATERAL VIEW inline(`src`.`items`) `elem` AS `sku`, `price`",
    )


def test_an_array_of_scalars_explodes_with_explode():
    mapping = {
        "explode": {"path": "tags", "as": "tag"},
        "columns": [{"name": "tag", "from": "tag"}],
    }
    target = [col("event_id", "string", required=True), col("tag", "string")]
    assert spark(mapping, target) == select(
        "`src`.`event_id` AS `event_id`",
        "`elem`.`tag` AS `tag`",
        join="\nLATERAL VIEW explode(`src`.`tags`) `elem` AS `tag`",
    )


def test_a_whole_exploded_struct_element_is_rebuilt_with_named_struct():
    mapping = {
        "explode": {"path": "items", "as": "item"},
        "columns": [{"name": "item", "from": "item"}],
    }
    target = [
        col(
            "item",
            "struct",
            required=True,
            fields=[col("sku", "string"), col("price", "decimal(18,2)")],
        ),
    ]
    assert spark(mapping, target) == select(
        "named_struct('sku', `elem`.`sku`, 'price', `elem`.`price`) AS `item`",
        join="\nLATERAL VIEW inline(`src`.`items`) `elem` AS `sku`, `price`",
    )


def test_a_struct_built_from_flat_paths_is_a_named_struct_never_null():
    mapping = {
        "columns": [
            {"name": "order.id", "from": "event_id"},
            {"name": "order.qty", "from": "quantity"},
        ]
    }
    target = [
        col(
            "order",
            "struct",
            required=True,
            fields=[col("id", "string", required=True), col("qty", "int")],
        ),
    ]
    assert spark(mapping, target) == select(
        "named_struct('id', `src`.`event_id`, 'qty', `src`.`quantity`) AS `order`",
    )


def test_a_struct_read_from_one_nullable_struct_is_null_where_it_is():
    mapping = {
        "columns": [
            {"name": "buyer.id", "from": "customer.id"},
            {"name": "buyer.country", "from": "customer.address.country"},
        ]
    }
    target = [col("buyer", "struct", fields=[col("id", "string"), col("country", "string")])]
    assert spark(mapping, target) == select(
        "CASE WHEN `src`.`customer` IS NULL THEN NULL ELSE named_struct("
        "'id', `src`.`customer`.`id`, 'country', `src`.`customer`.`address`.`country`) END "
        "AS `buyer`",
    )


def test_a_required_struct_from_a_nullable_one_is_refused_as_in_flink():
    mapping = {"columns": [{"name": "buyer.id", "from": "customer.id"}]}
    target = [col("buyer", "struct", required=True, fields=[col("id", "string")])]
    with pytest.raises(ValueError, match="required, but its fields are read from customer"):
        spark(mapping, target)


def test_widening_casts_use_spark_type_names():
    mapping = {
        "columns": [
            {"name": "quantity", "from": "quantity"},
            {"name": "seen", "from": "event_time", "cast": "timestamp_ltz(6)"},
        ]
    }
    target = [col("quantity", "long"), col("seen", "timestamp_ltz(6)")]
    assert spark(mapping, target) == select(
        "CAST(`src`.`quantity` AS BIGINT) AS `quantity`",
        "CAST(`src`.`event_time` AS TIMESTAMP) AS `seen`",
    )


def test_the_same_mapping_still_compiles_to_flink_when_no_dialect_is_named():
    mapping = {"columns": [{"name": "order.id", "from": "event_id"}]}
    target = parse_fields(
        [col("order", "struct", required=True, fields=[col("id", "string", required=True)])]
    )
    assert compile_select(stream(), mapping, target) == select(
        "CAST(ROW(`src`.`event_id`) AS ROW<`id` STRING NOT NULL>) AS `order`"
    )


def test_an_unknown_dialect_is_refused():
    with pytest.raises(ValueError, match="unsupported mapping dialect 'trino'"):
        compile_select(stream(), None, parse_fields([VALUE[0]]), dialect="trino")


# ── maskAtIngest ────────────────────────────────────────────────────────


def test_mask_at_ingest_hashes_the_source_with_sha256():
    mapping = {
        "columns": [
            {"name": "customer_id", "from": "customer.id", "maskAtIngest": "hash"},
            {"name": "event_id", "from": "event_id", "maskAtIngest": "hash"},
            {"name": "qty", "from": "quantity", "maskAtIngest": "hash"},
        ]
    }
    target = [
        col("event_id", "string", required=True),
        col("customer_id", "string"),
        col("qty", "string"),
    ]
    assert spark(mapping, target) == select(
        "sha2(CAST(`src`.`event_id` AS STRING), 256) AS `event_id`",
        "sha2(CAST(`src`.`customer`.`id` AS STRING), 256) AS `customer_id`",
        "sha2(CAST(`src`.`quantity` AS STRING), 256) AS `qty`",
    )


def test_a_binary_source_is_hashed_as_its_bytes():
    value = [col("event_id", "string", required=True), col("payload", "binary")]
    s = parse_stream({"kind": "kafka", "topic": "t", "format": "json", "schema": value})
    mapping = {"columns": [{"name": "payload", "from": "payload", "maskAtIngest": "hash"}]}
    sql = compile_select(s, mapping, parse_fields([col("payload", "string")]), dialect="spark")
    assert "sha2(`src`.`payload`, 256) AS `payload`" in sql


@pytest.mark.parametrize(
    ("entry", "target", "message"),
    [
        (
            {"name": "c", "from": "customer", "maskAtIngest": "hash"},
            col("c", "string"),
            r"customer is struct; a hash reads one of \['binary', 'int', 'long', 'string'\]",
        ),
        (
            {"name": "c", "from": "event_time", "maskAtIngest": "hash"},
            col("c", "string"),
            "event_time is timestamp_ltz",
        ),
        (
            {"name": "c", "from": "event_id", "maskAtIngest": "hash"},
            col("c", "long"),
            "c is long; a hash is a string",
        ),
        (
            {"name": "c", "from": "customer.id", "maskAtIngest": "hash"},
            col("c", "string", required=True),
            "c is required, but customer.id can be null",
        ),
        (
            {"name": "c", "from": "event_id", "maskAtIngest": "encrypt"},
            col("c", "string"),
            "maskAtIngest: expected hash",
        ),
        (
            {"name": "c", "from": "quantity", "maskAtIngest": "hash", "cast": "long"},
            col("c", "string"),
            "a masked column is the hash of its source; drop cast",
        ),
    ],
)
def test_mask_at_ingest_refusals(entry, target, message):
    with pytest.raises(ValueError, match=message):
        spark({"columns": [entry]}, [target])


def test_flink_refuses_mask_at_ingest_by_name():
    mapping = {"columns": [{"name": "c", "from": "event_id", "maskAtIngest": "hash"}]}
    with pytest.raises(ValueError, match="hashing at ingest is a Spark runner feature"):
        compile_select(stream(), mapping, parse_fields([col("c", "string")]))


# ── the source table, from Spark's Kafka columns ────────────────────────


def test_value_fields_are_read_as_their_declared_types():
    sql = spark_source_select(stream())
    _parses(sql)
    assert sql == (
        "SELECT\n  "
        "CASE WHEN `value`.`event_id` IS NULL THEN raise_error('fluid: required field event_id "
        "is null in a Kafka record') ELSE CAST(`value`.`event_id` AS STRING) END AS `event_id`,\n  "
        "CASE WHEN `value`.`customer` IS NULL THEN NULL ELSE named_struct("
        "'id', CAST(`value`.`customer`.`id` AS STRING), "
        "'address', CASE WHEN `value`.`customer`.`address` IS NULL THEN NULL ELSE named_struct("
        "'country', CAST(`value`.`customer`.`address`.`country` AS STRING)) END) END "
        "AS `customer`,\n  "
        "transform(`value`.`items`, `fluid_e0` -> CASE WHEN `fluid_e0` IS NULL THEN NULL "
        "ELSE named_struct('sku', CAST(`fluid_e0`.`sku` AS STRING), "
        "'price', CAST(`fluid_e0`.`price` AS DECIMAL(18,2))) END) AS `items`,\n  "
        "CAST(`value`.`tags` AS ARRAY<STRING>) AS `tags`,\n  "
        "CAST(`value`.`attributes` AS MAP<STRING, STRING>) AS `attributes`,\n  "
        "CAST(`value`.`quantity` AS INT) AS `quantity`,\n  "
        "CAST(`value`.`event_time` AS TIMESTAMP) AS `event_time`"
        "\nFROM `kafka_records`"
    )


def test_a_struct_is_read_by_field_name_not_position():
    """Spark casts a struct by position: an Avro writer schema that orders the fields
    differently would swap their values under a CAST, so each is read by name."""
    nested = col(
        "outer",
        "array",
        element={
            "type": "map",
            "key": {"type": "string", "required": True},
            "value": {"type": "struct", "fields": [col("id", "string"), col("email", "string")]},
        },
    )
    sql = spark_source_select(stream(schema=[nested]))
    _parses(sql)
    assert "CAST(`value`.`outer`" not in sql
    assert (
        "transform(`value`.`outer`, `fluid_e0` -> transform_values(`fluid_e0`, "
        "(`fluid_k1`, `fluid_v1`) -> CASE WHEN `fluid_v1` IS NULL THEN NULL ELSE named_struct("
        "'id', CAST(`fluid_v1`.`id` AS STRING), 'email', CAST(`fluid_v1`.`email` AS STRING)) END))"
    ) in sql


def test_a_json_key_reads_its_fields_under_the_prefix():
    key = {"format": "json", "prefix": "k_", "schema": [col("event_id", "string")]}
    sql = spark_source_select(stream(key=key))
    _parses(sql)
    assert "CAST(`key`.`event_id` AS STRING) AS `k_event_id`" in sql


@pytest.mark.parametrize(
    ("type_", "expected"),
    [("string", "CAST(`key` AS STRING)"), ("binary", "CAST(`key` AS BINARY)")],
)
def test_a_raw_key_is_the_record_key(type_, expected):
    key = {"format": "raw", "schema": [col("record_key", type_)]}
    sql = spark_source_select(stream(key=key))
    _parses(sql)
    assert f"{expected} AS `record_key`" in sql


def test_metadata_reads_sparks_kafka_columns():
    metadata = [
        {"name": "kafka_topic", "from": "topic"},
        {"name": "kafka_partition", "from": "partition"},
        {"name": "kafka_offset", "from": "offset"},
        {"name": "kafka_ts", "from": "timestamp"},
        {"name": "kafka_ts_type", "from": "timestamp-type"},
        {"name": "kafka_headers", "from": "headers"},
    ]
    sql = spark_source_select(stream(metadata=metadata))
    _parses(sql)
    assert sql.endswith(
        "`topic` AS `kafka_topic`,\n  "
        "`partition` AS `kafka_partition`,\n  "
        "`offset` AS `kafka_offset`,\n  "
        "`timestamp` AS `kafka_ts`,\n  "
        "CASE `timestampType` WHEN 0 THEN 'CreateTime' WHEN 1 THEN 'LogAppendTime' "
        "ELSE 'NoTimestampType' END AS `kafka_ts_type`,\n  "
        "COALESCE(map_from_entries(`headers`), CAST(map() AS MAP<STRING, BINARY>)) "
        "AS `kafka_headers`\nFROM `kafka_records`"
    )


def test_the_leader_epoch_is_refused_spark_does_not_read_it():
    s = stream(metadata=[{"name": "epoch", "from": "leader-epoch"}])
    with pytest.raises(ValueError, match="Spark's Kafka source does not read leader-epoch"):
        spark_source_select(s)


def test_metadata_columns_are_ordinary_source_columns_to_the_mapping():
    metadata = [{"name": "kafka_offset", "from": "offset"}]
    target = [col("event_id", "string", required=True), col("kafka_offset", "long", required=True)]
    assert spark(None, target, metadata=metadata) == select(
        "`src`.`event_id` AS `event_id`", "`src`.`kafka_offset` AS `kafka_offset`"
    )


# ── types ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"type": "long"}, "BIGINT"),
        ({"type": "binary"}, "BINARY"),
        ({"type": "timestamp"}, "TIMESTAMP_NTZ"),
        ({"type": "timestamp_ltz(3)"}, "TIMESTAMP"),
        ({"type": "decimal(10,2)", "required": True}, "DECIMAL(10,2)"),
        (
            {"type": "map", "key": {"type": "string", "required": True}, "value": {"type": "int"}},
            "MAP<STRING, INT>",
        ),
    ],
)
def test_spark_type(spec, expected):
    assert spark_type(parse_type(spec)) == expected


def test_spark_schema_is_a_ddl_string_from_json_reads():
    assert spark_schema(parse_fields(VALUE[:2])) == (
        "`event_id` STRING, `customer` STRUCT<`id`: STRING, `address`: STRUCT<`country`: STRING>>"
    )


def test_a_map_key_spark_would_substitute_is_refused():
    mapping = {"columns": [{"name": "channel", "from": "attributes['${env:SECRET}']"}]}
    with pytest.raises(ValueError, match="cannot be a Spark string literal"):
        spark(mapping, [col("channel", "string")])


def test_a_required_field_the_record_holds_as_null_fails_the_job():
    """Spark types carry no NOT NULL: a required field is checked as it is read, inside
    a nullable struct only when the struct is there, and inside arrays element by element."""
    schema = [
        col(
            "customer",
            "struct",
            fields=[col("id", "string", required=True)],
        ),
        col("tags", "array", element={"type": "string", "required": True}),
    ]
    sql = spark_source_select(stream(schema=schema))
    _parses(sql)
    assert (
        "CASE WHEN `value`.`customer` IS NULL THEN NULL ELSE named_struct('id', "
        "CASE WHEN `value`.`customer`.`id` IS NULL THEN raise_error('fluid: required field "
        "customer.id is null in a Kafka record') ELSE CAST(`value`.`customer`.`id` AS STRING) "
        "END) END AS `customer`"
    ) in sql
    assert (
        "transform(`value`.`tags`, `fluid_e0` -> CASE WHEN `fluid_e0` IS NULL THEN "
        "raise_error('fluid: required field tags[] is null in a Kafka record') "
        "ELSE CAST(`fluid_e0` AS STRING) END) AS `tags`"
    ) in sql
