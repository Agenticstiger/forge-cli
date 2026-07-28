# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
