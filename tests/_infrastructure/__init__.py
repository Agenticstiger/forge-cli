# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared test infrastructure for the source-aligned acquisition test matrix.

Modules:
- ``testcontainers_fixtures`` — Postgres / MySQL / MongoDB / MinIO / Kafka pytest fixtures.
- ``respx_fixtures``         — REST-mock fixtures for Airbyte API, Kafka Connect REST, DataHub, OpenMetadata, Unity Catalog, Snowflake Horizon.
- ``cosign_mock``            — Sigstore signature simulation without requiring a real key infrastructure.
- ``k8s_harness``            — minikube / kind detection + skip-if-not-available helpers.

Every engine's test suite imports from this package. Fixtures are session-scoped where they wrap a long-lived container, function-scoped where state must reset between tests.
"""
