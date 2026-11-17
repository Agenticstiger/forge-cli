# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Glue Schema Registry subjects an AWS product's Kafka exposes produce: contract to .tf.json."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.aws import AwsIacPlugin
from fluid_build.schema_registry.clients import GlueRegistry, RegistryError
from fluid_build.schema_registry.produced import schema_definition
from fluid_build.schema_registry.reference import produced_subjects
from fluid_build.schema_registry.schemas import avro_columns

pytestmark = [pytest.mark.unit, pytest.mark.provider]

CID = "commerce_orders_stream"
REGISTRY_KEY = f"{CID}_commerce"
SCHEMA_KEY = f"{CID}_commerce_commerce_orders_value"
COLUMNS = [
    {"name": "order_id", "type": "string", "required": True},
    {"name": "email", "type": "string", "sensitivity": "pii"},
    {"name": "amount", "type": "decimal(18,2)"},
]


def table_expose():
    return {
        "exposeId": "orders",
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": "iceberg",
            "location": {
                "database": "commerce",
                "table": "orders",
                "bucket": "commerce-lake",
                "path": "iceberg/commerce/orders/",
                "region": "eu-west-1",
            },
        },
        "contract": {"schema": [{"name": "order_id", "type": "string", "required": True}]},
    }


def kafka_expose(registry=None, serialization="avro", binding_extra=None):
    registry = {"kind": "glue", "registryName": "commerce"} if registry is None else registry
    binding = {
        "platform": "kafka",
        "format": "kafka_topic",
        "location": {"topic": "commerce.orders", "host": "b-1.msk.example:9098"},
        "serialization": {"value": serialization},
        "properties": {"registry": registry},
    }
    binding.update(binding_extra or {})
    return {
        "exposeId": "orders_events",
        "kind": "stream",
        "binding": binding,
        "contract": {"schema": copy.deepcopy(COLUMNS)},
    }


def make(*, registry=None, packaging=None, serialization="avro", kafka=True, binding_extra=None):
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "commerce.orders-stream",
        "name": "Orders stream",
        "exposes": [table_expose()],
    }
    if kafka:
        contract["exposes"].append(kafka_expose(registry, serialization, binding_extra))
    if packaging is not None:
        contract["packaging"] = packaging
    return contract


def packaging(schema_registry=None, mode="isolated"):
    containers = {"cluster": "shared"}
    if schema_registry:
        containers["schemaRegistry"] = schema_registry
    return {"mode": mode, "pool": "commerce-platform", "containers": containers}


def emit(contract):
    plugin = AwsIacPlugin()
    return plugin.emit(contract), plugin.emit_data(contract)


# ------------------------------------------------------------- shared registries


@pytest.mark.parametrize(
    "pkg",
    [None, packaging(), packaging("shared")],
    ids=["no-packaging", "packaging-without-key", "shared"],
)
def test_shared_or_absent_packaging_looks_the_registry_up(pkg):
    resources, data = emit(make(packaging=pkg))
    assert data["aws_glue_registry"] == {REGISTRY_KEY: {"name": "commerce"}}
    assert "aws_glue_registry" not in resources
    schema = resources["aws_glue_schema"][SCHEMA_KEY]
    assert schema["registry_arn"] == f"${{data.aws_glue_registry.{REGISTRY_KEY}.arn}}"


def test_the_schema_resource():
    resources, _ = emit(make())
    schema = resources["aws_glue_schema"][SCHEMA_KEY]
    assert schema["schema_name"] == "commerce.orders-value"
    assert schema["data_format"] == "AVRO"
    assert schema["compatibility"] == "BACKWARD_ALL"
    assert schema["tags"] == {"managed_by": "fluid", "fluid_contract": CID}
    assert "commerce.orders" in schema["description"]
    json.loads(schema["schema_definition"])


def test_the_schema_definition_derives_from_the_exposes_columns():
    resources, _ = emit(make())
    document = json.loads(resources["aws_glue_schema"][SCHEMA_KEY]["schema_definition"])
    assert document["type"] == "record"
    assert document["name"] == "commerce_orders"
    assert document["namespace"] == f"fluid.{CID}"
    columns, _, _ = avro_columns(document)
    assert [c["name"] for c in columns] == ["order_id", "email", "amount"]
    assert columns[0]["required"] is True and columns[1]["required"] is False


def test_the_definition_equals_produced_schema_definition():
    contract = make()
    resources, _ = emit(contract)
    (subject,) = produced_subjects(contract)
    fmt, text = schema_definition(subject, f"fluid.{CID}")
    emitted = resources["aws_glue_schema"][SCHEMA_KEY]
    assert (emitted["data_format"], emitted["schema_definition"]) == (fmt, text)


def test_the_definition_is_deterministic():
    first = emit(make())[0]["aws_glue_schema"][SCHEMA_KEY]["schema_definition"]
    second = emit(make())[0]["aws_glue_schema"][SCHEMA_KEY]["schema_definition"]
    assert first == second and first.encode() == second.encode()


def test_the_whole_module_is_deterministic():
    a, b = emit(make()), emit(make())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_changing_a_column_changes_the_definition():
    base = emit(make())[0]["aws_glue_schema"][SCHEMA_KEY]["schema_definition"]
    contract = make()
    contract["exposes"][1]["contract"]["schema"].append({"name": "extra", "type": "int"})
    changed = emit(contract)[0]["aws_glue_schema"][SCHEMA_KEY]["schema_definition"]
    assert base != changed


# ------------------------------------------------------------- isolated registries


def test_isolated_registry_is_created_and_referenced_without_a_data_lookup():
    resources, data = emit(make(packaging=packaging("isolated")))
    assert resources["aws_glue_registry"][REGISTRY_KEY]["registry_name"] == "commerce"
    assert "FLUID" in resources["aws_glue_registry"][REGISTRY_KEY]["description"]
    assert resources["aws_glue_registry"][REGISTRY_KEY]["tags"]["managed_by"] == "fluid"
    schema = resources["aws_glue_schema"][SCHEMA_KEY]
    assert schema["registry_arn"] == f"${{aws_glue_registry.{REGISTRY_KEY}.arn}}"
    assert "aws_glue_registry" not in data


def test_blanket_isolated_mode_does_not_own_the_registry():
    resources, data = emit(make(packaging=packaging(None, mode="isolated")))
    assert "aws_glue_registry" not in resources and "aws_glue_registry" in data


def test_the_binding_packaging_wins_over_the_contracts():
    contract = make(
        packaging=packaging("isolated"),
        binding_extra={"packaging": {"containers": {"schemaRegistry": "shared"}}},
    )
    resources, data = emit(contract)
    assert "aws_glue_registry" not in resources and "aws_glue_registry" in data


def test_isolated_without_a_registry_name_is_refused():
    contract = make(registry={"kind": "glue"}, packaging=packaging("isolated"))
    with pytest.raises(UnsupportedBindingError) as caught:
        AwsIacPlugin().emit(contract)
    assert caught.value.kind == "schema-registry-unnamed"


def test_no_registry_name_and_not_owned_leaves_registry_arn_out():
    resources, data = emit(make(registry={"kind": "glue"}))
    schema = resources["aws_glue_schema"][f"{CID}_default_registry_commerce_orders_value"]
    assert "registry_arn" not in schema
    assert "aws_glue_registry" not in data and "aws_glue_registry" not in resources


# ----------------------------------------------------------- compatibility, format


def test_compatibility_default_is_backward_all():
    schema = emit(make())[0]["aws_glue_schema"][SCHEMA_KEY]
    assert schema["compatibility"] == "BACKWARD_ALL"


@pytest.mark.parametrize("mode", ["FULL_ALL", "NONE", "FORWARD", "DISABLED"])
def test_the_contracts_compatibility_wins(mode):
    registry = {"kind": "glue", "registryName": "commerce", "compatibility": mode}
    schema = emit(make(registry=registry))[0]["aws_glue_schema"][SCHEMA_KEY]
    assert schema["compatibility"] == mode


def test_a_subject_override_names_the_schema():
    registry = {"kind": "glue", "registryName": "commerce", "subject": "orders.v2"}
    resources, _ = emit(make(registry=registry))
    (schema,) = resources["aws_glue_schema"].values()
    assert schema["schema_name"] == "orders.v2"


@pytest.mark.parametrize(
    "value,expected",
    [("avro", "AVRO"), ("avro-confluent", "AVRO"), ("json", "JSON"), ("json-confluent", "JSON")],
)
def test_data_format_follows_serialization_value(value, expected):
    schema = emit(make(serialization=value))[0]["aws_glue_schema"][SCHEMA_KEY]
    assert schema["data_format"] == expected
    document = json.loads(schema["schema_definition"])
    if expected == "JSON":
        assert document["$schema"] == "http://json-schema.org/draft-07/schema#"
    else:
        assert document["type"] == "record"


def test_unset_serialization_is_avro():
    contract = make()
    del contract["exposes"][1]["binding"]["serialization"]
    assert emit(contract)[0]["aws_glue_schema"][SCHEMA_KEY]["data_format"] == "AVRO"


def test_protobuf_is_refused():
    with pytest.raises(UnsupportedBindingError) as caught:
        AwsIacPlugin().emit(make(serialization="protobuf"))
    assert caught.value.kind == "schema-registry-format"


def test_a_produced_subject_without_columns_is_refused():
    contract = make()
    contract["exposes"][1]["contract"]["schema"] = []
    with pytest.raises(UnsupportedBindingError) as caught:
        AwsIacPlugin().emit(contract)
    assert caught.value.kind == "schema-registry-no-columns"


# ------------------------------------------------------------------ routing, scope


def test_a_confluent_registry_on_a_kafka_expose_is_refused_not_skipped():
    contract = make(registry={"kind": "confluent", "url": "https://sr.example.com"})
    with pytest.raises(UnsupportedBindingError) as caught:
        AwsIacPlugin().emit(contract)
    assert caught.value.kind == "schema-registry-route"
    with pytest.raises(UnsupportedBindingError):
        AwsIacPlugin().emit_data(contract)


def test_duplicate_subjects_are_refused():
    contract = make()
    contract["exposes"].append(copy.deepcopy(contract["exposes"][1]))
    contract["exposes"][2]["exposeId"] = "orders_events_copy"
    with pytest.raises(UnsupportedBindingError) as caught:
        AwsIacPlugin().emit(contract)
    assert caught.value.kind == "schema-registry-duplicate-subject"


def test_no_kafka_exposes_emit_no_registry_resources():
    resources, data = emit(make(kafka=False))
    assert "aws_glue_schema" not in resources and "aws_glue_registry" not in resources
    assert "aws_glue_registry" not in data


def test_a_kafka_expose_without_a_registry_changes_nothing_else():
    plain = make(kafka=False)
    with_topic = make()
    del with_topic["exposes"][1]["binding"]["properties"]["registry"]
    a_resources, a_data = emit(plain)
    b_resources, b_data = emit(with_topic)
    for key in ("aws_glue_schema", "aws_glue_registry"):
        assert key not in b_resources and key not in b_data
    assert set(a_resources) == set(b_resources) and a_data == b_data


def test_contracts_without_kafka_are_byte_identical_to_the_table_only_emit():
    contract = make(kafka=False)
    first = json.dumps(emit(contract), sort_keys=True)
    second = json.dumps(emit(copy.deepcopy(contract)), sort_keys=True)
    assert first == second
    assert "aws_glue_schema" not in first and "aws_glue_registry" not in first


def test_emitted_resources_are_json_serialisable():
    resources, data = emit(make(packaging=packaging("isolated")))
    json.dumps(resources)
    json.dumps(data)


# ------------------------------------------------------------- GlueRegistry on moto


@pytest.fixture
def glue(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    for name, value in (
        ("AWS_ACCESS_KEY_ID", "testing"),  # pragma: allowlist secret
        ("AWS_SECRET_ACCESS_KEY", "testing"),  # pragma: allowlist secret
        ("AWS_DEFAULT_REGION", "us-east-1"),
    ):
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        yield boto3.client("glue", region_name="us-east-1")


def test_glue_registry_reads_a_version_from_moto(glue):
    contract = make()
    (subject,) = produced_subjects(contract)
    fmt, definition = schema_definition(subject, f"fluid.{CID}")
    glue.create_registry(RegistryName="commerce")
    glue.create_schema(
        RegistryId={"RegistryName": "commerce"},
        SchemaName="commerce.orders-value",
        DataFormat=fmt,
        Compatibility="BACKWARD_ALL",
        SchemaDefinition=definition,
    )
    sv = GlueRegistry(client=glue).get_version("commerce", "commerce.orders-value")
    assert sv.version == 1
    assert sv.data_format == "AVRO"
    assert json.loads(sv.schema) == json.loads(definition)
    pinned = GlueRegistry(client=glue).get_version("commerce", "commerce.orders-value", 1)
    assert pinned.version == 1


def test_glue_registry_reads_back_what_the_emitter_registers_and_verify_passes(glue):
    from fluid_build.schema_registry.verify import verify_consumed

    contract = make()
    (subject,) = produced_subjects(contract)
    fmt, definition = schema_definition(subject, f"fluid.{CID}")
    glue.create_registry(RegistryName="commerce")
    glue.create_schema(
        RegistryId={"RegistryName": "commerce"},
        SchemaName="commerce.orders-value",
        DataFormat=fmt,
        Compatibility="BACKWARD_ALL",
        SchemaDefinition=definition,
    )
    reader = {
        "builds": [
            {
                "id": "ingest",
                "properties": {
                    "stream": {
                        "kind": "kafka",
                        "topic": "commerce.orders",
                        "format": "avro-confluent",
                        "schema": COLUMNS,
                        "registry": {"kind": "glue", "registryName": "commerce"},
                    }
                },
            }
        ]
    }
    records, errors, _ = verify_consumed(reader, environ={}, glue=lambda: GlueRegistry(client=glue))
    assert errors == [] and records[0]["result"] == "compatible" and records[0]["version"] == 1


def test_entity_not_found_is_a_registry_error(glue):
    glue.create_registry(RegistryName="commerce")
    with pytest.raises(RegistryError) as caught:
        GlueRegistry(client=glue).get_version("commerce", "missing-value")
    assert caught.value.code == "EntityNotFoundException"


def test_missing_registry_is_a_registry_error(glue):
    with pytest.raises(RegistryError):
        GlueRegistry(client=glue).get_version("nope", "x")


def test_two_registry_names_that_are_one_resource_name_are_refused():
    from fluid_build.iac.base import UnsupportedBindingError
    from fluid_build.iac.providers.aws_schema_registry import emit_schema_registry

    def kafka(eid, registry):
        return {
            "exposeId": eid,
            "binding": {
                "platform": "kafka",
                "format": "kafka_topic",
                "location": {"topic": eid},
                "properties": {"registry": {"kind": "glue", "registryName": registry}},
            },
            "contract": {"schema": [{"name": "id", "type": "string"}]},
        }

    contract = {"id": "p", "exposes": [kafka("a", "team-a"), kafka("b", "team_a")]}
    with pytest.raises(UnsupportedBindingError) as caught:
        emit_schema_registry({}, contract, "p", {})
    assert caught.value.kind == "schema-registry-name-collision"
