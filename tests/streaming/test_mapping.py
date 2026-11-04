# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Every Kafka record shape the managed lane takes, declared in FLUID and checked.

Flat records, nested records kept nested, and nested records flattened (struct fields
lifted, arrays exploded, map entries looked up) or rebuilt into another nested shape;
the record key and Kafka metadata as columns. forge-cli writes every query, and an
output it cannot read with the declared type fails here, before anything deploys.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest
import yaml

from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.streaming.compiler import compile_contract, managed_config, validation_errors
from fluid_build.streaming.mapping import parse_path, show


def col(name, type_, **kw):
    return {"name": name, "type": type_, **kw}


EVENT_ID = col("event_id", "string", required=True)
EVENT_TIME = col("event_time", "timestamp_ltz(3)")


def _props(contract):
    return contract["builds"][0]["properties"]


def _stream(contract):
    return _props(contract)["stream"]


def _output(contract, columns, mapping=None):
    contract["exposes"][0]["contract"]["schema"] = columns
    if mapping is not None:
        _props(contract)["mapping"] = mapping
    return contract


def _insert(contract):
    (artifact,) = compile_contract(contract)
    return artifact.statements[-1]


def _source_ddl(contract):
    (artifact,) = compile_contract(contract)
    return artifact.statements[3]


def _select(*lines, join=""):
    body = ",\n  ".join(lines)
    return (
        "INSERT INTO `lake`.`commerce`.`orders`\nSELECT\n  "
        + body
        + "\nFROM `source_events` AS `src`"
        + join
    )


# ── shapes ──────────────────────────────────────────────────────────────


def test_a_nested_stream_lands_as_it_is_with_no_mapping(contract):
    assert _insert(contract) == _select(
        "`src`.`event_id` AS `event_id`",
        "`src`.`customer` AS `customer`",
        "`src`.`items` AS `items`",
        "`src`.`attributes` AS `attributes`",
        "`src`.`event_time` AS `event_time`",
    )


def test_a_flat_stream_lands_by_name_in_the_output_order(contract):
    _stream(contract)["schema"] = [
        EVENT_ID,
        col("customer_id", "string"),
        col("amount", "decimal(18,2)"),
        EVENT_TIME,
    ]
    _output(contract, [EVENT_TIME, col("amount", "decimal(18,2)"), EVENT_ID])
    assert _insert(contract) == _select(
        "`src`.`event_time` AS `event_time`",
        "`src`.`amount` AS `amount`",
        "`src`.`event_id` AS `event_id`",
    )


def test_a_nested_event_flattens_to_one_row_per_item(contract):
    _output(
        contract,
        [
            EVENT_ID,
            col("customer_id", "string"),
            col("country", "string"),
            col("latitude", "double"),
            col("channel", "string"),
            col("sku", "string"),
            col("price", "decimal(18,2)"),
            EVENT_TIME,
        ],
        {
            "explode": {"path": "items", "as": "item"},
            "columns": [
                {"name": "customer_id", "from": "customer.id"},
                {"name": "country", "from": "customer.address.country"},
                {
                    "name": "latitude",
                    "from": "customer.address.geo.location.coordinates.latitude",
                },
                {"name": "channel", "from": "attributes['channel']"},
                {"name": "sku", "from": "item.sku"},
                {"name": "price", "from": "item.price"},
            ],
        },
    )
    assert _insert(contract) == _select(
        "`src`.`event_id` AS `event_id`",
        "`src`.`customer`.`id` AS `customer_id`",
        "`src`.`customer`.`address`.`country` AS `country`",
        "`src`.`customer`.`address`.`geo`.`location`.`coordinates`.`latitude` AS `latitude`",
        "`src`.`attributes`['channel'] AS `channel`",
        "`elem`.`sku` AS `sku`",
        "`elem`.`price` AS `price`",
        "`src`.`event_time` AS `event_time`",
        join="\nCROSS JOIN UNNEST(`src`.`items`) AS `elem` (`sku`, `price`)",
    )
    assert validation_errors(contract) == []


def test_a_scalar_array_explodes_and_its_element_is_read_by_its_name(contract):
    _stream(contract)["schema"].append(col("tags", "array", element={"type": "string"}))
    _output(contract, [EVENT_ID, col("tag", "string")], {"explode": {"path": "tags", "as": "tag"}})
    assert _insert(contract) == _select(
        "`src`.`event_id` AS `event_id`",
        "`elem`.`tag` AS `tag`",
        join="\nCROSS JOIN UNNEST(`src`.`tags`) AS `elem` (`tag`)",
    )


def test_an_exploded_struct_element_can_stay_nested(contract):
    item = col(
        "item",
        "struct",
        fields=[col("sku", "string"), col("price", "decimal(18,2)")],
    )
    _output(contract, [EVENT_ID, item], {"explode": {"path": "items", "as": "item"}})
    assert _insert(contract) == _select(
        "`src`.`event_id` AS `event_id`",
        "CAST(ROW(`elem`.`sku`, `elem`.`price`) AS ROW<`sku` STRING, `price` DECIMAL(18,2)>)"
        " AS `item`",
        join="\nCROSS JOIN UNNEST(`src`.`items`) AS `elem` (`sku`, `price`)",
    )


def test_a_flat_stream_can_be_written_as_a_nested_table(contract):
    _stream(contract)["schema"] = [EVENT_ID, col("customer_id", "string"), col("country", "string")]
    customer = col(
        "customer",
        "struct",
        fields=[
            col("id", "string"),
            col("address", "struct", fields=[col("country", "string")]),
        ],
    )
    _props(contract)["parameters"]["flink"]["source"].pop("eventTime")
    _output(
        contract,
        [EVENT_ID, customer],
        {
            "columns": [
                {"name": "customer.id", "from": "customer_id"},
                {"name": "customer.address.country", "from": "country"},
            ]
        },
    )
    assert _insert(contract) == _select(
        "`src`.`event_id` AS `event_id`",
        "CAST(ROW(`src`.`customer_id`, CAST(ROW(`src`.`country`) AS ROW<`country` STRING>))"
        " AS ROW<`id` STRING, `address` ROW<`country` STRING>>) AS `customer`",
    )


def test_a_nested_subset_stays_null_where_its_source_struct_is_null(contract):
    _output(contract, [EVENT_ID, col("customer", "struct", fields=[col("id", "string")])])
    assert _insert(contract) == _select(
        "`src`.`event_id` AS `event_id`",
        "CAST(CASE WHEN `src`.`customer` IS NULL THEN NULL "
        "ELSE ROW(`src`.`customer`.`id`) END AS ROW<`id` STRING>) AS `customer`",
    )


def test_nested_fields_are_reordered_by_name_never_by_position(contract):
    original = contract["exposes"][0]["contract"]["schema"][1]
    swapped = col("customer", "struct", fields=list(reversed(original["fields"])))
    _output(contract, [EVENT_ID, swapped])
    insert = _insert(contract)
    assert "ELSE ROW(`src`.`customer`.`address`, `src`.`customer`.`id`) END" in insert
    assert insert.count("CASE WHEN `src`.`customer` IS NULL") == 1


def test_a_type_is_widened_only_losslessly_and_said_so(contract):
    _stream(contract)["schema"].append(col("quantity", "int"))
    _output(
        contract,
        [EVENT_ID, col("quantity", "long"), col("event_time", "timestamp_ltz(6)")],
    )
    insert = _insert(contract)
    assert "CAST(`src`.`quantity` AS BIGINT) AS `quantity`" in insert
    assert "CAST(`src`.`event_time` AS TIMESTAMP_LTZ(6)) AS `event_time`" in insert


# ── the output is checked before deploy ─────────────────────────────────


@pytest.mark.parametrize(
    "columns,mapping,match",
    [
        # One column the stream does not have: the SELECT cannot silently drop it.
        (
            [EVENT_ID, col("channel", "string")],
            None,
            r"channel is not a column of properties.stream; map channel",
        ),
        ([col("event_id", "int", required=True)], None, r"event_id: string is not int"),
        (
            [EVENT_ID, col("customer_id", "string", required=True)],
            {"columns": [{"name": "customer_id", "from": "customer.id"}]},
            r"can be null, but the output requires it",
        ),
        (
            [EVENT_ID, col("channel", "string", required=True)],
            {"columns": [{"name": "channel", "from": "attributes['channel']"}]},
            r"can be null, but the output requires it",
        ),
        (
            [EVENT_ID, col("customer", "struct", required=True, fields=[col("id", "string")])],
            None,
            r"required, but its fields are read from customer, which can be null",
        ),
        (
            [EVENT_ID, col("customer", "struct", fields=[col("id", "string", required=True)])],
            None,
            r"customer.id.*can be null, but the output requires it",
        ),
        (
            [
                EVENT_ID,
                col(
                    "items",
                    "array",
                    element={"type": "struct", "fields": [col("sku", "string")]},
                ),
            ],
            None,
            r"struct fields at \[\] are \['sku', 'price'\], not \['sku'\]",
        ),
        (
            [EVENT_ID, col("price", "decimal(10,2)")],
            {
                "explode": {"path": "items", "as": "i"},
                "columns": [{"name": "price", "from": "i.price"}],
            },
            r"decimal\(18,2\) is not decimal\(10,2\)",
        ),
        (
            [EVENT_ID, col("x", "string")],
            {"columns": [{"name": "x", "from": "nope"}]},
            r"columns\[0\].from: nope is not a column of properties.stream",
        ),
        (
            [EVENT_ID, col("x", "string")],
            {"columns": [{"name": "x", "from": "customer.nope"}]},
            r"customer has no field nope",
        ),
        (
            [EVENT_ID, col("x", "string")],
            {"columns": [{"name": "x", "from": "event_id['k']"}]},
            r"event_id is string, not a map",
        ),
        (
            [EVENT_ID, col("x", "string")],
            {"columns": [{"name": "x", "from": "event_id.k"}]},
            r"event_id is string, not a struct",
        ),
        (
            [EVENT_ID],
            {"columns": [{"name": "nope", "from": "event_id"}]},
            r"nope is not an output column or a struct field of one",
        ),
        (
            [
                EVENT_ID,
                col(
                    "items",
                    "array",
                    element={
                        "type": "struct",
                        "fields": [col("sku", "string"), col("price", "decimal(18,2)")],
                    },
                ),
            ],
            {"columns": [{"name": "items.sku", "from": "event_id"}]},
            r"items.sku is not an output column or a struct field of one",
        ),
        (
            [EVENT_ID, col("customer", "struct", fields=[col("id", "string")])],
            {
                "columns": [
                    {"name": "customer", "from": "customer"},
                    {"name": "customer.id", "from": "event_id"},
                ]
            },
            r"customer.id is inside customer, mapped whole",
        ),
        (
            [EVENT_ID],
            {"columns": [{"name": "event_id", "from": "event_id"}] * 2},
            r"event_id is mapped twice",
        ),
        ([EVENT_ID], {"explode": {"path": "customer", "as": "c"}}, r"customer is not an array"),
        (
            [EVENT_ID],
            {"explode": {"path": "items", "as": "event_id"}},
            r"event_id is already a column",
        ),
        ([EVENT_ID], {"explode": {"path": "items", "as": "item"}}, r"no output column reads item"),
        (
            [EVENT_ID, col("x", "string")],
            {"columns": [{"name": "x", "from": "event_id", "cast": "int"}]},
            r"string cannot become int",
        ),
        (
            [EVENT_ID, col("x", "string")],
            {"columns": [{"name": "x", "from": "customer", "cast": "struct"}]},
            r"only a scalar type",
        ),
        # Inside containers: an element field, a nullable element, a map value.
        (
            [
                EVENT_ID,
                col(
                    "items",
                    "array",
                    element={
                        "type": "struct",
                        "fields": [col("sku", "string"), col("price", "string")],
                    },
                ),
            ],
            None,
            r"decimal\(18,2\) at \[\]\.price is not string",
        ),
        (
            [
                EVENT_ID,
                col(
                    "items",
                    "array",
                    element={
                        "type": "struct",
                        "required": True,
                        "fields": [col("sku", "string"), col("price", "decimal(18,2)")],
                    },
                ),
            ],
            None,
            r"a value at \[\] can be null",
        ),
        (
            [
                EVENT_ID,
                col(
                    "attributes",
                    "map",
                    key={"type": "string", "required": True},
                    value={"type": "int"},
                ),
            ],
            None,
            r"string at \{value\} is not int",
        ),
        # Nullability survives a widening, a conversion and an exploded element.
        (
            [EVENT_ID, col("n", "long", required=True)],
            {
                "columns": [
                    {
                        "name": "n",
                        "from": "customer.address.geo.location.coordinates.latitude",
                        "cast": "double",
                    }
                ]
            },
            r"can be null, but the output requires it",
        ),
        (
            [EVENT_ID, col("sku", "string", required=True)],
            {
                "explode": {"path": "items", "as": "item"},
                "columns": [{"name": "sku", "from": "item.sku"}],
            },
            r"can be null, but the output requires it",
        ),
        ([EVENT_ID], {"typo": True}, r"unsupported field\(s\) \['typo'\]"),
        ([EVENT_ID], {"columns": []}, r"columns: expected a non-empty list"),
    ],
)
def test_an_output_the_stream_cannot_fill_fails_before_deploy(contract, columns, mapping, match):
    _output(contract, columns, mapping)
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)
    assert validation_errors(contract)


@pytest.mark.parametrize(
    "path,match",
    [
        ("event_id; DROP TABLE x", "expected a path"),
        ("`event_id`", "expected a path"),
        ("attributes['it''s']", "expected a path"),
        ("attributes['a\\b']", "expected a path"),
        ("event_id\n", "expected a path"),
        ("customer..id", "expected a path"),
        ("1st", "expected a path"),
        ("attributes[channel]", "expected a path"),
        ("attributes['" + "k" * 257 + "']", "expected a path"),
        (".".join(["a"] * 65), "at most 64 steps"),
    ],
)
def test_a_path_is_never_sql_and_a_bad_one_is_not_echoed(contract, path, match):
    _output(contract, [EVENT_ID, col("x", "string")], {"columns": [{"name": "x", "from": path}]})
    with pytest.raises(ValueError, match=match) as caught:
        compile_contract(contract)
    assert path not in str(caught.value)


def test_paths_round_trip():
    for text in ("a", "a.b.c", "attributes['channel']", "headers['trace-id'].x"):
        assert show(parse_path(text, "$")) == text


def test_contract_sql_is_bound_by_name(contract):
    _props(contract)["sql"] = "SELECT * FROM source_events"
    insert = _insert(contract)
    assert insert.endswith(
        "SELECT `event_id`, `customer`, `items`, `attributes`, `event_time`\n"
        "FROM (\nSELECT * FROM source_events\n) AS `contract_sql`"
    )


def test_declaring_both_a_mapping_and_sql_is_refused(contract):
    _props(contract)["mapping"] = {"columns": [{"name": "event_id", "from": "event_id"}]}
    _props(contract)["sql"] = "SELECT * FROM source_events"
    with pytest.raises(ValueError, match="not both"):
        compile_contract(contract)


# ── key and Kafka metadata ──────────────────────────────────────────────


def test_a_raw_key_is_a_column_and_the_value_options_are_prefixed(contract):
    _stream(contract)["key"] = {"format": "raw", "schema": [col("order_key", "string")]}
    _output(contract, [EVENT_ID, col("order_key", "string")])
    ddl = _source_ddl(contract)
    assert "`order_key` STRING," in ddl
    for option in (
        "'key.fields' = 'order_key'",
        "'key.format' = 'raw'",
        "'value.fields-include' = 'EXCEPT_KEY'",
        "'value.format' = 'json'",
        "'value.json.ignore-parse-errors' = 'false'",
    ):
        assert option in ddl
    assert "'format' =" not in ddl and "'json." not in ddl
    assert "`src`.`order_key` AS `order_key`" in _insert(contract)


def test_a_record_key_that_repeats_a_value_field_takes_a_prefix(contract):
    key = {"format": "json", "schema": [col("event_id", "string")]}
    _stream(contract)["key"] = key
    with pytest.raises(ValueError, match="declared twice.*set key.prefix"):
        compile_contract(contract)
    key["prefix"] = "key_"
    _output(contract, [EVENT_ID, col("key_event_id", "string")])
    ddl = _source_ddl(contract)
    assert "`key_event_id` STRING" in ddl
    assert "'key.fields' = 'key_event_id'" in ddl
    assert "'key.fields-prefix' = 'key_'" in ddl
    assert "'key.json.fail-on-missing-field' = 'false'" in ddl


def test_an_avro_key_or_value_needs_the_registry(contract):
    _stream(contract)["key"] = {"format": "avro-confluent", "schema": [col("k", "string")]}
    with pytest.raises(ValueError, match="requires source.schemaRegistryUrl"):
        compile_contract(contract)
    managed_config(contract["builds"][0])["source"][
        "schemaRegistryUrl"
    ] = "https://registry.example"
    ddl = _source_ddl(contract)
    assert "'key.avro-confluent.url' = 'https://registry.example'" in ddl
    assert "'value.json.fail-on-missing-field' = 'false'" in ddl


@pytest.mark.parametrize(
    "key,match",
    [
        ({"format": "raw", "schema": [col("a", "string"), col("b", "string")]}, "one column"),
        ({"format": "raw", "schema": [col("a", "int")]}, "string or binary"),
        ({"format": "protobuf", "schema": [col("a", "string")]}, "key.format"),
        ({"format": "raw", "schema": [col("a", "string")], "prefix": "1x"}, "key.prefix"),
        ({"format": "raw", "schema": [col("a", "string")], "typo": 1}, "unsupported field"),
    ],
)
def test_bad_keys_are_refused(contract, key, match):
    _stream(contract)["key"] = key
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)


def test_kafka_metadata_reads_as_typed_virtual_columns(contract):
    _stream(contract)["metadata"] = [
        {"name": "kafka_offset", "from": "offset"},
        {"name": "kafka_partition", "from": "partition"},
        {"name": "kafka_ts", "from": "timestamp"},
        {"name": "kafka_headers", "from": "headers"},
        {"name": "kafka_epoch", "from": "leader-epoch"},
    ]
    managed_config(contract["builds"][0])["source"]["eventTime"]["column"] = "kafka_ts"
    _output(
        contract,
        [
            EVENT_ID,
            col("kafka_offset", "long", required=True),
            col("kafka_partition", "int"),
            col("kafka_ts", "timestamp_ltz(3)", required=True),
            col("trace_id", "string"),
            col(
                "kafka_headers",
                "map",
                key={"type": "string", "required": True},
                value={"type": "binary"},
            ),
            col("kafka_epoch", "int"),
        ],
        {"columns": [{"name": "trace_id", "from": "kafka_headers['trace-id']", "cast": "string"}]},
    )
    ddl = _source_ddl(contract)
    assert "`kafka_offset` BIGINT NOT NULL METADATA FROM 'offset' VIRTUAL" in ddl
    assert "`kafka_ts` TIMESTAMP_LTZ(3) NOT NULL METADATA FROM 'timestamp' VIRTUAL" in ddl
    assert "`kafka_headers` MAP<STRING, BYTES> NOT NULL METADATA FROM 'headers' VIRTUAL" in ddl
    assert "`kafka_epoch` INT METADATA FROM 'leader-epoch' VIRTUAL" in ddl
    assert "WATERMARK FOR `kafka_ts` AS `kafka_ts` - INTERVAL '5' SECOND" in ddl
    insert = _insert(contract)
    assert "CAST(`src`.`kafka_headers`['trace-id'] AS STRING) AS `trace_id`" in insert
    assert "`src`.`kafka_offset` AS `kafka_offset`" in insert


@pytest.mark.parametrize(
    "metadata,match",
    [
        ([{"name": "m", "from": "key"}], "from: expected one of"),
        ([{"name": "event_id", "from": "offset"}], "declared twice"),
        ([{"from": "offset"}], "a column name is required"),
        ([], "non-empty list"),
    ],
)
def test_bad_metadata_is_refused(contract, metadata, match):
    _stream(contract)["metadata"] = metadata
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)


def test_leader_epoch_can_be_null(contract):
    _stream(contract)["metadata"] = [{"name": "epoch", "from": "leader-epoch"}]
    _output(contract, [EVENT_ID, col("epoch", "int", required=True)])
    with pytest.raises(ValueError, match="can be null"):
        compile_contract(contract)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda s: s.update(kind="kinesis"), "only kafka"),
        (lambda s: s.update(topic="orders;payments"), "one Kafka topic"),
        (lambda s: s.update(topic="o'rders"), "one Kafka topic"),
        (lambda s: s.update(typo=1), "unsupported field"),
        (lambda s: s["schema"].append(col("bad`name", "string")), "Invalid SQL identifier"),
    ],
)
def test_bad_streams_are_refused(contract, mutate, match):
    mutate(_stream(contract))
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)


# ── the schema, the CLI and fragments ───────────────────────────────────


def _validate(contract, tmp_path):
    return FluidSchemaManager(cache_dir=tmp_path).validate_contract(contract, offline_only=True)


def test_the_example_validates(contract, tmp_path):
    result = _validate(contract, tmp_path)
    assert result.is_valid, result.errors


@pytest.mark.parametrize(
    "mutate,match",
    [
        # A key the column definition does not have is refused, not silently dropped.
        (lambda c: _stream(c)["schema"][0].update(metadata="offset"), "metadata"),
        (lambda c: _props(c).pop("stream"), "'sql' is a required property"),
        (
            lambda c: _props(c).update(mapping={"columns": [{"name": "a", "from": "b"}]})
            or _props(c).pop("stream"),
            "'stream' is a dependency of 'mapping'",
        ),
        (
            lambda c: _props(c).update(mapping={"columns": [{"name": "a", "from": "b; x"}]}),
            "does not match",
        ),
        (lambda c: _stream(c).update(format="protobuf"), "protobuf"),
        (lambda c: c.update(fluidVersion="0.7.6"), "stream"),
    ],
)
def test_the_schema_checks_streams_and_mappings(contract, tmp_path, mutate, match):
    mutate(contract)
    result = _validate(contract, tmp_path)
    assert not result.is_valid
    assert any(match in str(error) for error in result.errors), result.errors


def _write(path: Path, document) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _fragment_project(tmp_path, contract):
    """The example, with its stream and a mapping kept in fragments, as a team would."""
    root = tmp_path / "product"
    stream = copy.deepcopy(_stream(contract))
    value = _write(root / "fragments/streams/orders.value.yaml", stream.pop("schema"))
    stream["schema"] = {"$ref": "./orders.value.yaml"}
    _write(root / "fragments/streams/orders.yaml", stream)
    mapping = {
        "explode": {"path": "items", "as": "item"},
        "columns": [{"name": "sku", "from": "item.sku"}, {"name": "price", "from": "item.price"}],
    }
    _write(root / "fragments/mappings/order-items.yaml", mapping)
    _props(contract)["stream"] = {"$ref": "./fragments/streams/orders.yaml"}
    _props(contract)["mapping"] = {"$ref": "./fragments/mappings/order-items.yaml"}
    contract["exposes"][0]["contract"]["schema"] = [
        EVENT_ID,
        col("sku", "string"),
        col("price", "decimal(18,2)"),
        EVENT_TIME,
    ]
    return _write(root / "contract.fluid.yaml", contract), value, mapping


def test_the_stream_and_mapping_work_from_fragments(contract, tmp_path, capsys):
    from fluid_build._contract_loader import load_contract_with_overlay
    from fluid_build.cli import main

    inline = copy.deepcopy(contract)
    path, value, mapping = _fragment_project(tmp_path, contract)
    _props(inline)["mapping"] = mapping
    inline["exposes"] = copy.deepcopy(contract["exposes"])
    loaded = load_contract_with_overlay(str(path), None, logging.getLogger("test"))
    assert compile_contract(loaded) == compile_contract(inline)

    cache = ["--cache-dir", str(tmp_path / "cache")]
    assert main(["validate", *cache, str(path)]) == 0
    root = path.parent
    for kind, fragment in (
        ("streamSource", root / "fragments/streams/orders.yaml"),
        ("recordMapping", root / "fragments/mappings/order-items.yaml"),
        ("schema", value),
    ):
        assert main(["validate", *cache, "--fragment", kind, str(fragment)]) == 0, kind
    _write(root / "fragments/mappings/order-items.yaml", {**mapping, "typo": 1})
    assert (
        main(
            [
                "validate",
                *cache,
                "--fragment",
                "recordMapping",
                str(root / "fragments/mappings/order-items.yaml"),
            ]
        )
        == 1
    )
    capsys.readouterr()


def test_a_widened_column_keeps_its_nullability(contract):
    _stream(contract)["schema"].append(col("quantity", "int"))
    _output(contract, [EVENT_ID, col("quantity", "long", required=True)])
    with pytest.raises(ValueError, match="can be null, but the output requires it"):
        compile_contract(contract)
    _stream(contract)["schema"][-1]["required"] = True
    assert "CAST(`src`.`quantity` AS BIGINT) AS `quantity`" in _insert(contract)


def test_a_required_exploded_field_needs_a_required_element(contract):
    items = _stream(contract)["schema"][2]
    items["element"]["fields"][0]["required"] = True
    _output(
        contract,
        [EVENT_ID, col("sku", "string", required=True)],
        {
            "explode": {"path": "items", "as": "item"},
            "columns": [{"name": "sku", "from": "item.sku"}],
        },
    )
    with pytest.raises(ValueError, match="can be null"):
        compile_contract(contract)
    items["element"]["required"] = True
    assert "`elem`.`sku` AS `sku`" in _insert(contract)


def test_a_required_field_of_an_optional_struct_is_required_where_the_struct_is_present(
    contract,
):
    # The rebuilt struct is null where the customer is; where the customer is there,
    # its required id is too. A subset, a reorder and a widening all keep it required.
    customer = _stream(contract)["schema"][1]
    customer["fields"][0]["required"] = True
    customer["fields"].append(col("visits", "int", required=True))
    _output(
        contract,
        [
            EVENT_ID,
            col(
                "customer",
                "struct",
                fields=[col("visits", "long", required=True), col("id", "string", required=True)],
            ),
        ],
    )
    insert = _insert(contract)
    assert (
        "CAST(CASE WHEN `src`.`customer` IS NULL THEN NULL ELSE "
        "ROW(CAST(`src`.`customer`.`visits` AS BIGINT), `src`.`customer`.`id`) END "
        "AS ROW<`visits` BIGINT NOT NULL, `id` STRING NOT NULL>) AS `customer`" in insert
    )
    # The struct itself still can be null, so it cannot be declared required.
    contract["exposes"][0]["contract"]["schema"][1]["required"] = True
    with pytest.raises(ValueError, match="required, but its fields are read from customer"):
        compile_contract(contract)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda s: s.update(topic="."), "one Kafka topic"),
        (lambda s: s.update(topic=".."), "one Kafka topic"),
        (lambda s: s.update(key="raw"), "key: expected an object"),
        (lambda s: s.update(key=[1]), "key: expected an object"),
        (
            lambda s: s.update(key={"format": "raw", "schema": [col("k", "string")], "prefix": ""}),
            "key.prefix",
        ),
        (lambda s: s.update(metadata=[{"name": "m", "from": ["offset"]}]), "from: expected one of"),
    ],
)
def test_malformed_streams_fail_with_a_message_not_a_crash(contract, mutate, match):
    mutate(_stream(contract))
    with pytest.raises(ValueError, match=match):
        compile_contract(contract)
    assert validation_errors(contract)


def test_warnings_never_crash_and_name_a_mapping_nothing_compiles(contract):
    from fluid_build.streaming.compiler import validation_warnings

    assert validation_warnings({**contract, "builds": 5}) == []
    unmanaged = copy.deepcopy(contract)
    _props(unmanaged)["parameters"].pop("flink")
    _props(unmanaged)["sql"] = "SELECT 1"
    _props(unmanaged)["mapping"] = {"columns": [{"name": "event_id", "from": "event_id"}]}
    (warning,) = validation_warnings(unmanaged)
    assert "properties.mapping is compiled only by the managed Flink lane" in warning


def test_an_unmanaged_build_with_a_stream_still_needs_its_sql(contract, tmp_path):
    _props(contract)["parameters"].pop("flink")
    result = _validate(contract, tmp_path)
    assert any("'sql' is a required property" in str(e) for e in result.errors), result.errors
    _props(contract)["sql"] = "SELECT * FROM orders"
    assert _validate(contract, tmp_path).is_valid
