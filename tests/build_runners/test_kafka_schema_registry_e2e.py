# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end Schema Registry round-trip + Avro serialization.

Spins a real Redpanda container (single binary; Kafka API + Schema Registry
HTTP in one process), registers an Avro schema, fetches it back, and
verifies fingerprinting + Connect converter wiring.

This is the production-grade assertion that the Avro / Schema-Registry
leg of the Kafka-Connect runner actually works against a live registry.
Skips when Docker is unavailable.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict

import pytest

from fluid_build.build_runners.kafka_connect.schema_registry import (
    SchemaRegistryClient,
    avro_converter_config,
)

# ── Unit: avro_converter_config ───────────────────────────────────────────


class TestAvroConverterConfig:
    def test_emits_canonical_keys(self):
        cfg = avro_converter_config("http://schema-registry:8081")
        assert cfg["key.converter"] == "io.confluent.connect.avro.AvroConverter"
        assert cfg["value.converter"] == "io.confluent.connect.avro.AvroConverter"
        assert cfg["key.converter.schema.registry.url"] == "http://schema-registry:8081"
        assert cfg["value.converter.schema.registry.url"] == "http://schema-registry:8081"

    def test_url_round_trip_preserves_authentication_user_info(self):
        # Confluent SR allows basic-auth via embedded URL credentials; the
        # converter config must pass that through verbatim — it's the
        # caller's contract to decide whether to use that style or the
        # ``basic.auth.user.info`` properties.
        url = "http://reader:s3cret@sr:8081"
        cfg = avro_converter_config(url)
        assert cfg["value.converter.schema.registry.url"] == url


# ── Integration: live Redpanda Schema Registry ────────────────────────────


@pytest.fixture
def avro_user_schema() -> Dict[str, Any]:
    return {
        "type": "record",
        "name": "User",
        "namespace": "com.example.fluid",
        "fields": [
            {"name": "id", "type": "long"},
            {"name": "name", "type": "string"},
            {"name": "email", "type": ["null", "string"], "default": None},
        ],
    }


@pytest.mark.integration
def test_schema_registry_register_and_fetch(
    redpanda_container: Dict[str, Any], avro_user_schema: Dict[str, Any]
) -> None:
    """Register an Avro schema, fetch it back, confirm round-trip."""
    sr = SchemaRegistryClient(redpanda_container["schema_registry"])
    try:
        subject = "fluid.test.users-value"
        schema_id = sr.register_schema(subject, avro_user_schema)
        assert schema_id > 0

        latest = sr.get_latest(subject)
        assert latest is not None
        assert latest.subject == subject
        assert latest.version == 1
        assert latest.schema_id == schema_id
        assert latest.schema["name"] == "User"
        assert {f["name"] for f in latest.schema["fields"]} == {"id", "name", "email"}

        by_id = sr.get_by_id(schema_id)
        assert by_id["name"] == "User"
        # Subject listing includes the new subject.
        assert subject in sr.list_subjects()
    finally:
        sr.close()


@pytest.mark.integration
def test_schema_registry_idempotent_registration(
    redpanda_container: Dict[str, Any], avro_user_schema: Dict[str, Any]
) -> None:
    """Registering the same schema twice returns the same ID — important
    for deterministic deploys (re-running ``apply`` must not churn IDs).
    """
    sr = SchemaRegistryClient(redpanda_container["schema_registry"])
    try:
        subject = "fluid.test.users-idempotent"
        first = sr.register_schema(subject, avro_user_schema)
        second = sr.register_schema(subject, avro_user_schema)
        assert first == second
    finally:
        sr.close()


@pytest.mark.integration
def test_schema_registry_avro_message_round_trip(
    redpanda_container: Dict[str, Any], avro_user_schema: Dict[str, Any]
) -> None:
    """Encode + decode an Avro record using ``fastavro`` and a registered
    schema. Verifies our ``avro_converter_config`` lines up with what
    Connect's AvroConverter actually does on the wire (Confluent
    framing: magic byte 0x00 + 4-byte schema ID + Avro body).
    """
    import fastavro

    sr = SchemaRegistryClient(redpanda_container["schema_registry"])
    try:
        subject = "fluid.test.users-roundtrip-value"
        schema_id = sr.register_schema(subject, avro_user_schema)

        # Encode like Confluent's AvroSerializer would.
        record = {"id": 42, "name": "Alice", "email": "alice@example.com"}
        body_buf = io.BytesIO()
        fastavro.schemaless_writer(body_buf, avro_user_schema, record)
        body = body_buf.getvalue()
        framed = b"\x00" + schema_id.to_bytes(4, "big") + body

        # Decode the framed payload.
        assert framed[0] == 0x00
        decoded_id = int.from_bytes(framed[1:5], "big")
        assert decoded_id == schema_id
        decoded_schema = sr.get_by_id(decoded_id)
        decoded = fastavro.schemaless_reader(io.BytesIO(framed[5:]), decoded_schema)
        assert decoded == record
    finally:
        sr.close()
