# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Confluent Schema Registry HTTP client + Avro-converter config builder.

The runner uses this in two places:

1. **At validate-artifacts time** — verifies the Schema Registry the
   contract points at is reachable and the listed subjects exist.
2. **At connector-config emission** — builds the ``key.converter`` /
   ``value.converter`` properties so a Kafka Connect source/sink
   serializes records via Avro + the registered subject IDs.

The client is intentionally minimal — register / fetch by ID / fetch
by subject — because we don't need a full Schema Registry SDK for the
runner's responsibilities. ``httpx`` is the only dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class RegisteredSchema:
    subject: str
    version: int
    schema_id: int
    schema: Dict[str, Any]


class SchemaRegistryClient:
    """Thin HTTP client over the Confluent Schema Registry REST API.

    Compatible with Confluent SR, Apicurio (with compat=ccompat), and
    Redpanda's built-in registry — all expose the same v1 surface.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0):
        # SSRF guard — schema-registry URL is operator-supplied via
        # contract.fluid.yaml. allow_private=True for typical
        # cluster-internal registries; the hook still validates scheme
        # + DNS-pins the connection.
        from fluid_build.util.safe_http import safe_httpx_client

        self._client = safe_httpx_client(
            base_url=base_url,
            timeout=timeout_seconds,
            allow_private=True,
        )

    def close(self) -> None:
        self._client.close()

    def list_subjects(self) -> List[str]:
        r = self._client.get("/subjects")
        r.raise_for_status()
        return list(r.json())

    def register_schema(
        self,
        subject: str,
        schema: Dict[str, Any],
        *,
        schema_type: str = "AVRO",
    ) -> int:
        """Register an Avro schema under a subject and return the schema ID.

        Idempotent in the sense that registering the *same* schema bytes
        for the *same* subject returns the existing ID.
        """
        body = {
            "schema": json.dumps(schema, sort_keys=True),
            "schemaType": schema_type,
        }
        r = self._client.post(
            f"/subjects/{subject}/versions",
            headers={"Content-Type": "application/vnd.schemaregistry.v1+json"},
            json=body,
        )
        r.raise_for_status()
        return int(r.json()["id"])

    def get_latest(self, subject: str) -> Optional[RegisteredSchema]:
        r = self._client.get(f"/subjects/{subject}/versions/latest")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.json()
        return RegisteredSchema(
            subject=body["subject"],
            version=int(body["version"]),
            schema_id=int(body["id"]),
            schema=json.loads(body["schema"]),
        )

    def get_by_id(self, schema_id: int) -> Dict[str, Any]:
        r = self._client.get(f"/schemas/ids/{schema_id}")
        r.raise_for_status()
        return json.loads(r.json()["schema"])


def avro_converter_config(schema_registry_url: str) -> Dict[str, str]:
    """Return Connect-compatible ``key.converter`` / ``value.converter`` config.

    These properties tell Kafka Connect to deserialize keys and values
    via Avro using the given Schema Registry. Used by emitted connector
    JSON for Avro-mode acquisition contracts.
    """
    return {
        "key.converter": "io.confluent.connect.avro.AvroConverter",
        "key.converter.schema.registry.url": schema_registry_url,
        "value.converter": "io.confluent.connect.avro.AvroConverter",
        "value.converter.schema.registry.url": schema_registry_url,
    }
