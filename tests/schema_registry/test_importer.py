# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Seeding ``properties.stream`` from a subject, and the market connectors that call it."""

from __future__ import annotations

import logging

import pytest

from fluid_build.schema_registry.clients import RegistryError, SubjectVersion
from fluid_build.schema_registry.importer import ref_for, stream_from_subject
from fluid_build.schema_registry.reference import RegistryShapeError
from fluid_build.schema_registry.schemas import (
    UnsupportedSchema,
    avro_schema,
    json_schema,
    schema_text,
)

pytestmark = pytest.mark.unit

COLUMNS = [
    {"name": "id", "type": "string", "required": True},
    {"name": "n", "type": "long", "required": False},
]


class Fake:
    def __init__(self, schema, data_format="AVRO"):
        self.sv = SubjectVersion("s", 4, data_format, schema)
        self.calls = []
        self.closed = False

    def get_version(self, *args):
        self.calls.append(args)
        return self.sv

    def close(self):
        self.closed = True


def avro():
    return schema_text(avro_schema(COLUMNS, name="orders"))


class TestRefFor:
    def test_confluent(self):
        ref = ref_for("confluent", "orders-value", url="https://sr.example.com", version=2)
        assert (ref.kind, ref.url, ref.subject, ref.version, ref.origin) == (
            "confluent",
            "https://sr.example.com",
            "orders-value",
            2,
            "import",
        )

    def test_glue(self):
        ref = ref_for("glue", "orders-value", registry_name="commerce")
        assert ref.glue_registry == "commerce" and ref.version == "latest"

    def test_bad_input_is_a_shape_error(self):
        with pytest.raises(RegistryShapeError):
            ref_for("confluent", "orders-value")  # no url


class TestStreamFromSubject:
    def test_avro_confluent(self):
        client = Fake(avro())
        ref = ref_for("confluent", "orders-value", url="https://sr.example.com")
        block = stream_from_subject(ref, environ={}, client=client)
        assert client.calls == [("orders-value", "latest")]
        assert block == {
            "kind": "kafka",
            "topic": "orders",
            "format": "avro-confluent",
            "schema": COLUMNS,
            "registry": {"kind": "confluent", "url": "https://sr.example.com"},
        }

    def test_json_subject(self):
        client = Fake(schema_text(json_schema(COLUMNS, title="o")), "JSON")
        ref = ref_for("confluent", "orders-value", url="https://sr.example.com")
        block = stream_from_subject(ref, environ={}, client=client)
        assert block["format"] == "json"
        assert [c["name"] for c in block["schema"]] == ["id", "n"]

    def test_a_non_default_subject_is_kept_in_the_registry_block(self):
        ref = ref_for("confluent", "custom", url="https://sr.example.com")
        block = stream_from_subject(ref, environ={}, client=Fake(avro()))
        assert block["topic"] == "custom"
        assert block["registry"]["subject"] == "custom"

    def test_glue_block_carries_the_registry_name(self):
        client = Fake(avro())
        ref = ref_for("glue", "orders-value", registry_name="commerce", version=3)
        block = stream_from_subject(ref, environ={}, client=client)
        assert client.calls == [("commerce", "orders-value", 3)]
        assert block["registry"] == {"kind": "glue", "registryName": "commerce"}

    def test_glue_default_registry_names_nothing(self):
        ref = ref_for("glue", "orders-value")
        block = stream_from_subject(ref, environ={}, client=Fake(avro()))
        assert block["registry"] == {"kind": "glue"}

    def test_link_local_is_refused_before_any_read(self):
        ref = ref_for("confluent", "s-value", url="http://169.254.169.254")
        client = Fake(avro())
        with pytest.raises(RegistryError, match="link-local"):
            stream_from_subject(ref, environ={}, client=client)
        assert client.calls == []

    def test_an_unsupported_schema_raises(self):
        client = Fake("{}", "PROTOBUF")
        ref = ref_for("confluent", "s-value", url="https://sr.example.com")
        with pytest.raises(UnsupportedSchema, match="PROTOBUF"):
            stream_from_subject(ref, environ={}, client=client)

    def test_a_non_json_schema_text_is_a_registry_error(self):
        client = Fake("syntax = 'proto3';", "PROTOBUF")
        ref = ref_for("confluent", "s-value", url="https://sr.example.com")
        with pytest.raises(RegistryError, match="not JSON"):
            stream_from_subject(ref, environ={}, client=client)

    def test_seeded_stream_round_trips_through_consumed_subjects(self):
        from fluid_build.schema_registry.reference import consumed_subjects

        ref = ref_for("confluent", "orders-value", url="https://sr.example.com")
        block = stream_from_subject(ref, environ={}, client=Fake(avro()))
        contract = {"builds": [{"id": "b", "properties": {"stream": block}}]}
        (subject,) = consumed_subjects(contract)
        assert subject.ref.url == "https://sr.example.com"
        assert subject.ref.subject == "orders-value" and subject.topic == "orders"


class TestMarketConnectors:
    def test_glue_connector_stream_block(self):
        from fluid_build.cli.market_catalogs.glue_schema_reg import GlueSchemaRegistryImporter

        calls = []

        class Boto:
            def get_schema_version(self, **kwargs):
                calls.append(kwargs)
                return {
                    "VersionNumber": 2,
                    "DataFormat": "AVRO",
                    "SchemaDefinition": avro(),
                    "Status": "AVAILABLE",
                }

        importer = GlueSchemaRegistryImporter("commerce", client=Boto())
        block = importer.stream_block("orders-value", 2)
        assert calls[0]["SchemaId"] == {"RegistryName": "commerce", "SchemaName": "orders-value"}
        assert calls[0]["SchemaVersionNumber"] == {"VersionNumber": 2}
        assert block["registry"]["registryName"] == "commerce"

    def test_glue_connector_lists_schemas_in_the_default_registry(self):
        from fluid_build.cli.market_catalogs.glue_schema_reg import GlueSchemaRegistryImporter

        seen = []

        class Boto:
            def list_schemas(self, **kwargs):
                seen.append(kwargs)
                return {"Schemas": [{"SchemaName": "x"}]}

        assert GlueSchemaRegistryImporter(client=Boto()).schemas() == ["x"]
        assert seen[0]["RegistryId"] == {"RegistryName": "default-registry"}

    def test_confluent_connector_stream_block(self, monkeypatch):
        from fluid_build.cli.market_catalogs.confluent_schema_reg import (
            ConfluentSchemaRegistryConnector,
        )
        from fluid_build.schema_registry import clients

        fake = Fake(avro())
        built = []

        def factory(url, **kw):
            built.append(url)
            return fake

        monkeypatch.setattr(clients, "ConfluentRegistry", factory)
        connector = ConfluentSchemaRegistryConnector(
            {"url": "https://sr.example.com"}, logging.getLogger("test")
        )
        block = connector.stream_block("orders-value")
        assert built == ["https://sr.example.com"] and fake.closed
        assert block["registry"]["url"] == "https://sr.example.com"
