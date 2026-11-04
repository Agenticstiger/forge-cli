# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared test infrastructure for the source-aligned acquisition test matrix.

Modules:
- ``testcontainers_fixtures`` — Postgres / MySQL / MongoDB / MinIO / Kafka pytest fixtures.
- ``respx_fixtures``         — REST-mock fixtures for Airbyte API, Kafka Connect REST, DataHub, OpenMetadata, Unity Catalog, Snowflake Horizon.
- ``cosign_mock``            — Sigstore signature simulation without requiring a real key infrastructure.
- ``k8s_harness``            — minikube / kind detection + skip-if-not-available helpers.

Every engine's test suite imports from this package. Fixtures are session-scoped where they wrap a long-lived container, function-scoped where state must reset between tests.
"""
