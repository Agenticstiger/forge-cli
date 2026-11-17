# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Confluent Schema Registry subjects an expose produces: contract to .tf.json."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers.confluent import ConfluentIacPlugin, validate_confluent_binding
from fluid_build.schema_registry.produced import schema_definition
from fluid_build.schema_registry.reference import produced_subjects

pytestmark = [pytest.mark.unit, pytest.mark.provider]

KEY = "gold_orders_orders_v1_value"
URL = "https://sr.example"
HOSTS = "FLUID_ACQUISITION_DEPLOYMENT_HOST_ALLOWLIST"
GRANTS = "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST"


@pytest.fixture(autouse=True)
def no_operator_entries(monkeypatch):
    monkeypatch.delenv(HOSTS, raising=False)
    monkeypatch.delenv(GRANTS, raising=False)


def expose(registry=None, serialization="json", platform="confluent", extra=None):
    registry = {"kind": "confluent", "url": URL} if registry is None else registry
    binding = {
        "platform": platform,
        "format": "kafka_topic",
        "location": {"topic": "orders.v1"},
        "serialization": {"value": serialization},
        "properties": {"registry": registry},
    }
    binding.update(extra or {})
    return {
        "exposeId": "evt",
        "kind": "stream",
        "binding": binding,
        "contract": {
            "schema": [
                {"name": "id", "type": "string", "required": True},
                {"name": "email", "type": "string", "sensitivity": "pii"},
                {"name": "phone", "type": "string", "pii": True},
                {"name": "amount", "type": "long"},
            ]
        },
    }


def make(**kw):
    contract = {
        "id": "gold.orders",
        "name": "Orders",
        "metadata": {"classification": "Confidential"},
        "exposes": [expose(**kw)],
    }
    return contract


def emit(contract):
    return ConfluentIacPlugin().emit(contract)


def test_subject_config_defaults_to_backward_transitive():
    config = emit(make())["confluent_subject_config"][KEY]
    assert config == {
        "subject_name": "orders.v1-value",
        "compatibility_level": "BACKWARD_TRANSITIVE",
    }


@pytest.mark.parametrize("mode", ["FULL_TRANSITIVE", "NONE", "FORWARD"])
def test_the_contracts_compatibility_wins(mode):
    registry = {"kind": "confluent", "url": URL, "compatibility": mode}
    assert (
        emit(make(registry=registry))["confluent_subject_config"][KEY]["compatibility_level"]
        == mode
    )


def test_the_schema_resource():
    schema = emit(make())["confluent_schema"][KEY]
    assert schema["subject_name"] == "orders.v1-value"
    assert schema["format"] == "JSON"
    assert schema["depends_on"] == [f"confluent_subject_config.{KEY}"]
    document = json.loads(schema["schema"])
    assert document["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert document["required"] == ["id"]


@pytest.mark.parametrize(
    "value,fmt", [("avro", "AVRO"), ("avro-confluent", "AVRO"), ("json", "JSON")]
)
def test_the_format_follows_serialization(value, fmt):
    schema = emit(make(serialization=value))["confluent_schema"][KEY]
    assert schema["format"] == fmt
    if fmt == "AVRO":
        document = json.loads(schema["schema"])
        assert document["type"] == "record" and document["namespace"] == "fluid.gold_orders"


def test_protobuf_registers_the_generated_proto_text_with_a_decimal():
    contract = make(serialization="protobuf")
    contract["exposes"][0]["contract"]["schema"][:] = [
        {"name": "id", "type": "string", "required": True, "protobuf": {"field": 1}},
        {"name": "amount", "type": "decimal(18,2)", "protobuf": {"field": 2}},
    ]
    schema = emit(contract)["confluent_schema"][KEY]
    assert schema["format"] == "PROTOBUF"
    assert schema["schema"] == (
        'syntax = "proto3";\n'
        "\n"
        "package fluid.gold_orders;\n"
        "\n"
        'import "confluent/type/decimal.proto";\n'
        "\n"
        "message orders_v1 {\n"
        "  string id = 1;\n"
        "  optional .confluent.type.Decimal amount = 2;\n"
        "}\n"
    )
    (subject,) = produced_subjects(contract)
    assert (schema["format"], schema["schema"]) == schema_definition(subject, "fluid.gold_orders")
    assert "tags" not in schema.get("metadata", {})


def test_protobuf_without_field_numbers_is_refused():
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(make(serialization="protobuf"))
    assert caught.value.kind == "schema-registry-protobuf"


def test_the_schema_text_is_canonical_and_equals_produced_schema_definition():
    contract = make()
    schema = emit(contract)["confluent_schema"][KEY]
    (subject,) = produced_subjects(contract)
    assert (schema["format"], schema["schema"]) == schema_definition(subject, "fluid.gold_orders")
    assert schema["schema"] == json.dumps(
        json.loads(schema["schema"]), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def test_output_is_deterministic():
    assert json.dumps(emit(make()), sort_keys=True) == json.dumps(emit(make()), sort_keys=True)


def test_metadata_carries_classification_and_pii_tags():
    metadata = emit(make(serialization="avro"))["confluent_schema"][KEY]["metadata"]
    assert metadata["properties"] == {"fluid_classification": "Confidential"}
    assert metadata["tags"] == [
        {"key": "fluid.gold_orders.orders_v1.email", "value": ["PII"]},
        {"key": "fluid.gold_orders.orders_v1.phone", "value": ["PII"]},
    ]


def test_an_expose_policy_classification_beats_the_contracts():
    contract = make()
    contract["exposes"][0]["policy"] = {"classification": "Restricted"}
    metadata = emit(contract)["confluent_schema"][KEY]["metadata"]
    assert metadata["properties"] == {"fluid_classification": "Restricted"}


def test_no_metadata_when_there_is_nothing_to_say():
    contract = make()
    del contract["metadata"]
    contract["exposes"][0]["contract"]["schema"] = [{"name": "id", "type": "string"}]
    assert "metadata" not in emit(contract)["confluent_schema"][KEY]


def test_classification_without_pii_columns_has_no_tags():
    contract = make()
    contract["exposes"][0]["contract"]["schema"] = [{"name": "id", "type": "string"}]
    assert "tags" not in emit(contract)["confluent_schema"][KEY]["metadata"]


def test_no_tableflow_resources_for_a_kafka_topic():
    resources = emit(make())
    assert set(resources) == {"confluent_subject_config", "confluent_schema"}
    assert not any("tableflow" in name for name in resources)


def test_a_platform_kafka_expose_with_a_confluent_registry_is_emitted_too():
    resources = emit(make(platform="kafka"))
    assert KEY in resources["confluent_schema"]


def test_a_glue_registry_on_a_kafka_expose_is_refused_here_not_skipped():
    contract = make(registry={"kind": "glue", "registryName": "commerce"}, platform="kafka")
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "schema-registry-route"


def test_a_glue_registry_on_a_confluent_topic_expose_is_skipped():
    contract = make(registry={"kind": "glue", "registryName": "commerce"})
    assert emit(contract) == {}


def test_the_registry_url_never_enters_the_module():
    text = json.dumps(emit(make()))
    assert URL not in text and "sr.example" not in text


def test_no_columns_is_refused():
    contract = make()
    contract["exposes"][0]["contract"]["schema"] = []
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "schema-registry-no-columns"


def test_xml_is_refused():
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(make(serialization="xml"))
    assert caught.value.kind == "schema-registry-format"


def test_duplicate_subjects_are_refused():
    contract = make()
    contract["exposes"].append(copy.deepcopy(contract["exposes"][0]))
    contract["exposes"][1]["exposeId"] = "evt2"
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "schema-registry-duplicate-subject"


def test_a_contract_without_kafka_topics_emits_nothing_new():
    contract = {"id": "x", "exposes": [{"exposeId": "a", "binding": {"platform": "aws"}}]}
    assert emit(contract) == {}


def test_emit_data_is_empty_for_a_topic():
    assert ConfluentIacPlugin().emit_data(make()) == {}


@pytest.mark.parametrize(
    "name",
    [
        "SCHEMA_REGISTRY_ID",
        "SCHEMA_REGISTRY_REST_ENDPOINT",
        "SCHEMA_REGISTRY_API_KEY",
        "SCHEMA_REGISTRY_API_SECRET",
        "CONFLUENT_CLOUD_API_KEY",
        "CONFLUENT_CLOUD_API_SECRET",
    ],
)
def test_credential_env_vars_include_the_registry_names(name):
    assert name in ConfluentIacPlugin.credential_env_vars


# ------------------------------------------------------------ validate_confluent_binding


def test_kafka_topic_without_a_registry_is_an_error():
    contract = make()
    del contract["exposes"][0]["binding"]["properties"]["registry"]
    errors, _ = validate_confluent_binding(contract)
    assert any("binding.properties.registry names none" in e for e in errors)


def test_kafka_topic_with_a_registry_has_no_tableflow_errors():
    errors, warnings = validate_confluent_binding(make())
    assert errors == []
    assert warnings and all("registry" in w for w in warnings)  # the missing operator entries


def test_operator_entries_clear_the_warnings(monkeypatch):
    monkeypatch.setenv(HOSTS, "sr.example")
    monkeypatch.setenv(GRANTS, "SCHEMA_REGISTRY_API_SECRET@sr.example")
    assert validate_confluent_binding(make()) == ([], [])


def test_validate_also_carries_the_registry_shape_checks():
    errors, _ = validate_confluent_binding(make(registry={"kind": "confluent", "url": "nope"}))
    assert any("properties.registry.url" in e for e in errors)


def test_validate_reports_a_produced_subject_without_columns():
    contract = make()
    contract["exposes"][0]["contract"]["schema"] = []
    errors, _ = validate_confluent_binding(contract)
    assert any("declares no columns" in e for e in errors)


def test_validate_runs_registry_checks_for_non_confluent_contracts_too():
    contract = {
        "id": "x",
        "exposes": [
            {
                "exposeId": "e",
                "binding": {
                    "platform": "kafka",
                    "format": "kafka_topic",
                    "location": {"topic": "t"},
                    "properties": {"registry": {"kind": "glue", "url": "https://x.example"}},
                },
                "contract": {"schema": [{"name": "a", "type": "string"}]},
            }
        ],
    }
    errors, _ = validate_confluent_binding(contract)
    assert any("url is for kind: confluent" in e for e in errors)


def test_validate_link_local_registry_is_an_error():
    errors, _ = validate_confluent_binding(
        make(registry={"kind": "confluent", "url": "http://169.254.169.254"})
    )
    assert any("link-local" in e for e in errors)


def test_the_tableflow_checks_are_unchanged_for_an_iceberg_expose():
    contract = {
        "id": "gold.orders",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"platform": "confluent", "format": "iceberg", "location": {}},
            }
        ],
    }
    errors, _ = validate_confluent_binding(contract)
    assert errors and not any("registry" in e for e in errors)


def test_a_glue_registry_on_a_kafka_expose_is_skipped_when_aws_is_bound_too():
    contract = make(registry={"kind": "glue", "registryName": "commerce"}, platform="kafka")
    contract["exposes"].append(
        {"exposeId": "lake", "binding": {"platform": "aws", "format": "iceberg"}}
    )
    assert "confluent_schema" not in emit(contract)


def test_a_json_subject_gets_the_classification_but_no_field_tags():
    metadata = emit(make(serialization="json"))["confluent_schema"][KEY]["metadata"]
    assert metadata == {"properties": {"fluid_classification": "Confidential"}}
