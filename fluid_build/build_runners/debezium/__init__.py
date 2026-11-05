# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Debezium CDC acquisition runner.

Engine name: ``debezium``. Lane: best-in-class CDC for Postgres / MySQL /
MongoDB / SQL Server / Oracle. Capabilities: ``cdc``, ``streaming``,
``at_least_once``, ``schema_discovery``.

Two execution modes:
- **Kafka Connect** (``deployment.mode = bring-your-own | managed``): the
  most common path. Debezium ships as a set of Kafka Connect source
  connector classes; the runner POSTs the connector config to a Kafka
  Connect REST endpoint.
- **Debezium Server** (``deployment.mode = embedded``): a standalone
  Quarkus-based Debezium runtime that emits change events directly to
  destinations (Iceberg, S3, Pulsar, Kinesis…). The runner generates an
  ``application.properties`` file and shells out to the
  ``debezium-server`` binary.
"""

from __future__ import annotations

from .runner import DebeziumRunner, execute_debezium_build

__all__ = ["DebeziumRunner", "execute_debezium_build"]
