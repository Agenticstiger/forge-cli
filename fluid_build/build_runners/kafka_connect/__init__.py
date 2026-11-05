# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Kafka Connect acquisition runner.

Engine name: ``kafka-connect``. Lane: streaming + at-least-once / exactly-once
sources and sinks. Drives a Kafka Connect cluster via the REST API
(``/connectors``).

Supported source kinds (extensible via ``connector_class`` override):
- ``jdbc`` — Confluent JDBC source (Postgres / MySQL / SQL Server / Oracle).
- ``s3`` — Confluent S3 source.
- ``salesforce`` — Confluent Salesforce source.
- ``mongodb`` — MongoDB source connector.

Supported sink kinds (set via ``properties.kafka-connect.sink_connector_class``):
- JDBC sink, S3 sink, Snowflake sink, Iceberg sink.
"""

from __future__ import annotations

from .runner import KafkaConnectRunner, execute_kafka_connect_build

__all__ = ["KafkaConnectRunner", "execute_kafka_connect_build"]
