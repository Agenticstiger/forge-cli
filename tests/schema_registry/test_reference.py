# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Resolution of every spelling a contract uses to name a schema registry."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from fluid_build.schema_registry.reference import (
    RegistryRef,
    RegistryShapeError,
    connector_registries,
    consumed_subjects,
    from_flink_url,
    from_kafka_connect,
    from_registry,
    kafka_topic_binding,
    malformed_produced,
    produced_subjects,
    registry_refs,
    shape_errors,
    stray_registries,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONFLUENT = {"kind": "confluent", "url": "https://sr.example.com"}
GLUE = {"kind": "glue", "registryName": "commerce"}


def _ref(spec, topic="orders"):
    return from_registry(spec, topic=topic, origin="test", where="w")


def _stream_contract(registry=None, fmt="avro-confluent", topic="orders", flink_url=None):
    stream = {
        "kind": "kafka",
        "topic": topic,
        "format": fmt,
        "schema": [{"name": "id", "type": "string", "required": True}],
    }
    if registry is not None:
        stream["registry"] = registry
    props = {"stream": stream}
    if flink_url:
        props["parameters"] = {"flink": {"source": {"schemaRegistryUrl": flink_url}}}
    return {"builds": [{"id": "b1", "properties": props}]}


def _produced_contract(registry, platform="kafka", fmt="kafka_topic", location=None):
    return {
        "exposes": [
            {
                "exposeId": "events",
                "binding": {
                    "platform": platform,
                    "format": fmt,
                    "location": location if location is not None else {"topic": "evt"},
                    "serialization": {"value": "avro"},
                    "properties": {"registry": registry},
                },
                "contract": {"schema": [{"name": "id", "type": "string"}]},
            }
        ]
    }


# ---------------------------------------------------------------- from_registry


class TestFromRegistry:
    def test_default_subject_is_topic_value(self):
        assert _ref(CONFLUENT, topic="commerce.orders").subject == "commerce.orders-value"

    def test_explicit_subject_wins(self):
        assert _ref({**CONFLUENT, "subject": "custom"}).subject == "custom"

    def test_version_defaults_to_latest(self):
        assert _ref(CONFLUENT).version == "latest"

    def test_pinned_version(self):
        assert _ref({**CONFLUENT, "version": 3}).version == 3

    def test_confluent_fields(self):
        ref = _ref({**CONFLUENT, "compatibility": "FULL", "auth": {"secretRef": "env://X"}})
        assert (ref.kind, ref.url, ref.compatibility, ref.secret_ref) == (
            "confluent",
            "https://sr.example.com",
            "FULL",
            "env://X",
        )

    def test_glue_default_registry(self):
        ref = _ref({"kind": "glue"})
        assert ref.registry_name is None
        assert ref.glue_registry == "default-registry"

    def test_glue_named_registry(self):
        assert _ref(GLUE).glue_registry == "commerce"

    def test_effective_compatibility_defaults_per_kind(self):
        assert _ref(GLUE).effective_compatibility == "BACKWARD_ALL"
        assert _ref(CONFLUENT).effective_compatibility == "BACKWARD_TRANSITIVE"

    def test_contract_compatibility_wins(self):
        assert _ref({**GLUE, "compatibility": "FULL"}).effective_compatibility == "FULL"

    def test_label(self):
        assert _ref(GLUE).label == "glue:commerce/orders-value@latest"
        assert _ref(CONFLUENT).label == "confluent:https://sr.example.com/orders-value@latest"

    def test_refused_raises_shape_error(self):
        with pytest.raises(RegistryShapeError):
            _ref({"kind": "confluent"})

    def test_ref_is_frozen(self):
        with pytest.raises(Exception):
            _ref(GLUE).kind = "confluent"  # type: ignore[misc]


# ---------------------------------------------------------------- shape_errors


class TestShapeErrors:
    def test_valid_specs_have_none(self):
        assert shape_errors(CONFLUENT, "w") == []
        assert shape_errors(GLUE, "w") == []
        assert shape_errors({"kind": "glue"}, "w") == []

    def test_not_an_object(self):
        assert shape_errors("nope", "w") == ["w: expected an object"]

    def test_unknown_field(self):
        (msg,) = shape_errors({**CONFLUENT, "extra": 1}, "w")
        assert "unsupported field" in msg and "extra" in msg

    @pytest.mark.parametrize("kind", [None, "apicurio", "GLUE", 3])
    def test_bad_kind(self, kind):
        (msg,) = shape_errors({"kind": kind}, "w")
        assert "w.kind" in msg

    def test_glue_refuses_url(self):
        errors = shape_errors({"kind": "glue", "url": "https://x.example"}, "w")
        assert any("w.url is for kind: confluent" in e for e in errors)

    def test_glue_refuses_auth(self):
        errors = shape_errors({**GLUE, "auth": {"secretRef": "env://X"}}, "w")
        assert any("w.auth is for kind: confluent" in e for e in errors)

    def test_confluent_refuses_registry_name(self):
        errors = shape_errors({**CONFLUENT, "registryName": "r"}, "w")
        assert any("registryName is for kind: glue" in e for e in errors)

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "ftp://sr.example",
            "sr.example.com",
            "https://sr.example.com?x=1",
            "https://sr.example.com#frag",
            "https://user:pw@sr.example.com",  # pragma: allowlist secret
            "https://user@sr.example.com",
            "https://sr example.com",
        ],
    )
    def test_bad_url(self, url):
        errors = shape_errors({"kind": "confluent", "url": url}, "w")
        assert any(e.startswith("w.url:") for e in errors)

    @pytest.mark.parametrize(
        "url",
        ["http://localhost:8081", "https://sr.example.com/", "https://sr.example.com/prefix/v1"],
    )
    def test_good_urls(self, url):
        assert shape_errors({"kind": "confluent", "url": url}, "w") == []

    def test_message_never_quotes_a_pasted_credential(self):
        pasted = "https://admin:hunter2secret@sr.example.com"  # pragma: allowlist secret
        errors = shape_errors({"kind": "confluent", "url": pasted}, "w")
        assert errors
        joined = " ".join(errors)
        assert "hunter2secret" not in joined and "admin" not in joined

    def test_credential_in_unknown_field_name_only(self):
        errors = shape_errors(
            {**CONFLUENT, "password": "hunter2secret"}, "w"
        )  # pragma: allowlist secret
        assert errors and "hunter2secret" not in " ".join(errors)

    @pytest.mark.parametrize("name", ["bad name", "a/b", "", "x" * 256])
    def test_glue_registry_name_pattern(self, name):
        errors = shape_errors({"kind": "glue", "registryName": name}, "w")
        assert any("registryName" in e for e in errors)

    @pytest.mark.parametrize("subject", ["has space", "a/b", "", "x" * 256])
    def test_glue_subject_pattern(self, subject):
        errors = shape_errors({**GLUE, "subject": subject}, "w")
        assert any("w.subject" in e for e in errors)

    def test_glue_subject_accepts_dots_and_dashes(self):
        assert shape_errors({**GLUE, "subject": "commerce.orders-value"}, "w") == []

    def test_confluent_subject_allows_slashes_and_spaces(self):
        assert shape_errors({**CONFLUENT, "subject": "a b/c"}, "w") == []

    def test_confluent_subject_refuses_control_characters(self):
        errors = shape_errors({**CONFLUENT, "subject": "a\nb"}, "w")
        assert any("w.subject" in e for e in errors)

    @pytest.mark.parametrize("version", [0, -1, "2", True, 1.5, "earliest"])
    def test_bad_version(self, version):
        errors = shape_errors({**CONFLUENT, "version": version}, "w")
        assert any("w.version" in e for e in errors)

    @pytest.mark.parametrize("version", ["latest", 1, 42])
    def test_good_version(self, version):
        assert shape_errors({**CONFLUENT, "version": version}, "w") == []

    @pytest.mark.parametrize("mode", ["BACKWARD_ALL", "FULL_ALL", "DISABLED"])
    def test_glue_only_modes_refused_on_confluent(self, mode):
        errors = shape_errors({**CONFLUENT, "compatibility": mode}, "w")
        assert any("w.compatibility: confluent accepts" in e for e in errors)

    @pytest.mark.parametrize("mode", ["BACKWARD_TRANSITIVE", "FORWARD_TRANSITIVE"])
    def test_confluent_only_modes_refused_on_glue(self, mode):
        errors = shape_errors({**GLUE, "compatibility": mode}, "w")
        assert any("w.compatibility: glue accepts" in e for e in errors)

    @pytest.mark.parametrize("mode", ["NONE", "BACKWARD", "FORWARD", "FULL"])
    def test_shared_modes_accepted_on_both(self, mode):
        assert shape_errors({**CONFLUENT, "compatibility": mode}, "w") == []
        assert shape_errors({**GLUE, "compatibility": mode}, "w") == []

    @pytest.mark.parametrize(
        "auth",
        [{}, {"secretRef": 3}, {"secretRef": "env://X", "user": "u"}, "env://X", ["env://X"]],
    )
    def test_bad_auth_shape(self, auth):
        errors = shape_errors({**CONFLUENT, "auth": auth}, "w")
        assert any("w.auth" in e for e in errors)

    def test_good_auth_shape(self):
        assert shape_errors({**CONFLUENT, "auth": {"secretRef": "env://X"}}, "w") == []


# ---------------------------------------------------- the three spellings


class TestStreamRegistrySpelling:
    def test_consumed_from_stream_registry(self):
        (subject,) = consumed_subjects(_stream_contract(CONFLUENT))
        assert subject.role == "consumed"
        assert subject.topic == "orders"
        assert subject.format == "avro-confluent"
        assert subject.owner == "b1"
        assert subject.ref.origin == "stream.registry"
        assert subject.ref.where == "builds[0].properties.stream.registry"
        assert subject.ref.subject == "orders-value"
        assert [c["name"] for c in subject.columns] == ["id"]

    def test_glue_stream_registry(self):
        (subject,) = consumed_subjects(_stream_contract(GLUE, fmt="json"))
        assert subject.ref.kind == "glue" and subject.format == "json"

    def test_malformed_registry_raises(self):
        with pytest.raises(RegistryShapeError):
            consumed_subjects(_stream_contract({"kind": "confluent", "url": "nope"}))

    def test_build_without_stream_is_skipped(self):
        assert consumed_subjects({"builds": [{"id": "x", "properties": {"sql": "select 1"}}]}) == []

    def test_non_mapping_builds_are_skipped(self):
        assert consumed_subjects({"builds": ["x", None, 3]}) == []

    def test_stream_without_topic_is_skipped(self):
        contract = _stream_contract(CONFLUENT)
        del contract["builds"][0]["properties"]["stream"]["topic"]
        assert consumed_subjects(contract) == []

    def test_fixture_contract_resolves(self):
        text = (ROOT / "tests/fixtures/packaging/aws_streaming_iceberg_077.yaml").read_text()
        contract = yaml.safe_load(text)
        (subject,) = consumed_subjects(contract)
        assert subject.ref.kind == "confluent"
        assert subject.ref.url == "https://schema-registry.example.internal"
        assert subject.ref.subject == "commerce.orders-value"
        assert subject.ref.secret_ref == "env://SCHEMA_REGISTRY_AUTH"  # pragma: allowlist secret
        assert subject.topic == "commerce.orders"
        assert subject.owner == "ingest-orders"
        assert [c["name"] for c in subject.columns][0] == "order_id"


class TestFlinkSpelling:
    def test_from_flink_url(self):
        ref = from_flink_url("http://registry:8081", topic="t", where="w")
        assert (ref.kind, ref.url, ref.subject, ref.origin) == (
            "confluent",
            "http://registry:8081",
            "t-value",
            "flink.schemaRegistryUrl",
        )
        assert ref.secret_ref is None

    def test_consumed_from_flink_url_for_avro_confluent(self):
        contract = _stream_contract(flink_url="https://registry.example", topic="evt")
        (subject,) = consumed_subjects(contract)
        assert subject.ref.url == "https://registry.example"
        assert subject.ref.subject == "evt-value"
        assert subject.ref.where.endswith("parameters.flink.source.schemaRegistryUrl")

    def test_flink_url_ignored_for_other_formats(self):
        contract = _stream_contract(fmt="json", flink_url="https://registry.example")
        assert consumed_subjects(contract) == []

    def test_stream_registry_beats_flink_url(self):
        contract = _stream_contract(CONFLUENT, flink_url="https://other.example")
        (subject,) = consumed_subjects(contract)
        assert subject.ref.url == "https://sr.example.com"


class TestFlinkCompilerPin:
    """The resolver reads the very block the Flink compiler requires."""

    @pytest.fixture
    def flink(self):
        from fluid_build.streaming.compiler import managed_config

        contract = yaml.safe_load((ROOT / "examples/flink-iceberg/contract.fluid.yaml").read_text())
        stream = contract["builds"][0]["properties"]["stream"]
        stream["format"] = "avro-confluent"
        return contract, managed_config(contract["builds"][0]), stream

    def test_compiler_still_requires_the_url(self, flink):
        from fluid_build.streaming.compiler import compile_contract

        contract, cfg, _ = flink
        cfg["source"].pop("schemaRegistryUrl", None)
        with pytest.raises(
            ValueError,
            match="An avro-confluent value or key requires source.schemaRegistryUrl",
        ):
            compile_contract(contract)

    def test_resolver_reads_the_url_the_compiler_emits(self, flink, monkeypatch):
        from fluid_build.streaming.compiler import compile_contract

        contract, cfg, stream = flink
        cfg["source"]["schemaRegistryUrl"] = "https://registry.example"
        (artifact,) = compile_contract(contract)
        assert "'avro-confluent.url' = 'https://registry.example'" in artifact.sql
        (subject,) = consumed_subjects(contract)
        assert subject.ref.url == "https://registry.example"
        assert subject.ref.subject == f"{stream['topic']}-value"
        assert subject.ref.kind == "confluent"


class TestKafkaConnectSpelling:
    def test_from_kafka_connect(self):
        ref = from_kafka_connect(
            {"url": "http://sr:8081", "auth": {"secretRef": "env://K"}}, where="w"
        )
        assert ref is not None
        assert (ref.kind, ref.url, ref.subject, ref.secret_ref, ref.origin) == (
            "confluent",
            "http://sr:8081",
            None,
            "env://K",
            "kafka-connect.schema_registry",
        )

    def test_no_url_means_no_ref(self):
        assert from_kafka_connect({}, where="w") is None
        assert from_kafka_connect({"url": ""}, where="w") is None

    def test_connector_registries(self):
        contract = {
            "builds": [
                {
                    "id": "a",
                    "properties": {"kafka-connect": {"schema_registry": {"url": "http://sr:8081"}}},
                },
                {"id": "b", "properties": {"kafka-connect": {"schema_registry": {}}}},
                {"id": "c", "properties": {}},
            ]
        }
        (ref,) = connector_registries(contract)
        assert ref.where == "builds[0].properties.kafka-connect.schema_registry"

    def test_connector_blocks_are_not_subjects(self):
        contract = {
            "builds": [
                {
                    "id": "a",
                    "properties": {"kafka-connect": {"schema_registry": {"url": "http://sr:8081"}}},
                }
            ]
        }
        assert consumed_subjects(contract) == []
        assert produced_subjects(contract) == []

    def test_runner_still_emits_the_same_avro_converter_config(self):
        from fluid_build.build_runners.kafka_connect.schema_registry import avro_converter_config

        contract = {
            "builds": [
                {
                    "id": "a",
                    "properties": {"kafka-connect": {"schema_registry": {"url": "http://sr:8081"}}},
                }
            ]
        }
        (ref,) = connector_registries(contract)
        runner_url = contract["builds"][0]["properties"]["kafka-connect"]["schema_registry"]["url"]
        assert ref.url == runner_url
        config = avro_converter_config(ref.url)
        assert config == {
            "key.converter": "io.confluent.connect.avro.AvroConverter",
            "key.converter.schema.registry.url": runner_url,
            "value.converter": "io.confluent.connect.avro.AvroConverter",
            "value.converter.schema.registry.url": runner_url,
        }

    def test_resolution_does_not_mutate_the_contract(self):
        contract = {
            "builds": [
                {
                    "id": "a",
                    "properties": {"kafka-connect": {"schema_registry": {"url": "http://sr:8081"}}},
                }
            ]
        }
        before = copy.deepcopy(contract)
        registry_refs(contract)
        assert contract == before


# ------------------------------------------------------------- produced subjects


class TestProducedSubjects:
    def test_kafka_topic_binding_predicate(self):
        assert kafka_topic_binding({"platform": "kafka"})
        assert kafka_topic_binding({"platform": "KAFKA", "format": "x"})
        assert kafka_topic_binding({"platform": "confluent", "format": "kafka_topic"})
        assert not kafka_topic_binding({"platform": "confluent", "format": "iceberg"})
        assert not kafka_topic_binding({"platform": "aws"})
        assert not kafka_topic_binding({})

    def test_kafka_expose(self):
        (subject,) = produced_subjects(_produced_contract(GLUE))
        assert subject.role == "produced"
        assert subject.topic == "evt"
        assert subject.format == "avro"
        assert subject.owner == "events"
        assert subject.ref.where == "exposes[0].binding.properties.registry"
        assert subject.ref.subject == "evt-value"
        assert [c["name"] for c in subject.columns] == ["id"]

    def test_confluent_platform_kafka_topic(self):
        contract = _produced_contract(CONFLUENT, platform="confluent")
        (subject,) = produced_subjects(contract)
        assert subject.ref.kind == "confluent"

    def test_topic_falls_back_to_expose_id(self):
        contract = _produced_contract(GLUE, location={})
        (subject,) = produced_subjects(contract)
        assert subject.topic == "events"
        assert subject.ref.subject == "events-value"

    def test_expose_without_registry_is_not_a_subject(self):
        contract = _produced_contract(GLUE)
        del contract["exposes"][0]["binding"]["properties"]["registry"]
        assert produced_subjects(contract) == []

    def test_registry_on_non_kafka_expose_is_not_produced(self):
        contract = _produced_contract(GLUE, platform="aws", fmt="iceberg")
        assert produced_subjects(contract) == []

    def test_malformed_raises(self):
        with pytest.raises(RegistryShapeError):
            produced_subjects(_produced_contract({"kind": "glue", "url": "https://x"}))

    def test_missing_serialization_gives_empty_format(self):
        contract = _produced_contract(GLUE)
        del contract["exposes"][0]["binding"]["serialization"]
        (subject,) = produced_subjects(contract)
        assert subject.format == ""


class TestMalformedProduced:
    def test_valid_is_empty(self):
        assert malformed_produced(_produced_contract(GLUE)) == []

    def test_shape_messages(self):
        errors = malformed_produced(_produced_contract({"kind": "confluent", "url": "bad"}))
        assert errors and errors[0].startswith("exposes[0].binding.properties.registry.url")

    def test_registry_on_non_kafka_expose_is_a_warning_not_an_error(self):
        contract = _produced_contract(GLUE, platform="aws", fmt="iceberg")
        assert malformed_produced(contract) == []
        (msg,) = stray_registries(contract)
        assert "is ignored" in msg and "Kafka topic expose" in msg

    def test_no_registry_no_message(self):
        assert malformed_produced({"exposes": [{"binding": {"platform": "aws"}}]}) == []
        assert malformed_produced({}) == []


def test_registry_refs_order_consumed_produced_connector():
    contract = {
        **_stream_contract(CONFLUENT),
        **_produced_contract(GLUE),
    }
    contract["builds"].append(
        {
            "id": "kc",
            "properties": {"kafka-connect": {"schema_registry": {"url": "http://sr:8081"}}},
        }
    )
    refs = registry_refs(contract)
    assert [r.origin for r in refs] == [
        "stream.registry",
        "binding.properties.registry",
        "kafka-connect.schema_registry",
    ]
    assert all(isinstance(r, RegistryRef) for r in refs)
