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

# tests/conftest.py
"""
Pytest configuration and shared fixtures for FLUID tests.
"""

import argparse
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture
def mock_logger():
    """Provide a mock logger for testing."""
    logger = MagicMock()
    logger.info = Mock()
    logger.debug = Mock()
    logger.warning = Mock()
    logger.error = Mock()
    logger.exception = Mock()
    return logger


@pytest.fixture
def make_cli_args():
    """Factory fixture: build an argparse.Namespace with sensible defaults.

    Usage::

        def test_something(make_cli_args):
            args = make_cli_args(contract="my.yaml", dry_run=True)
    """

    def _factory(**overrides):
        return argparse.Namespace(**overrides)

    return _factory


@pytest.fixture
def sample_contract():
    """Provide a sample FLUID contract for testing."""
    return {
        "id": "test-product",
        "version": "0.5.7",
        "name": "Test Data Product",
        "description": "A test data product",
        "exposes": [
            {
                "id": "customers",
                "name": "Customer Data",
                "location": {
                    "format": "bigquery_table",
                    "properties": {
                        "project": "test-project",
                        "dataset": "analytics",
                        "table": "customers",
                    },
                },
                "schema": {
                    "columns": [
                        {"name": "customer_id", "type": "STRING"},
                        {"name": "email", "type": "STRING"},
                        {"name": "created_at", "type": "TIMESTAMP"},
                    ]
                },
            }
        ],
        "consumes": [],
        "accessPolicy": {
            "rules": [
                {
                    "role": "roles/bigquery.dataViewer",
                    "members": ["group:analytics-team@example.com"],
                }
            ]
        },
    }


@pytest.fixture
def sample_aws_contract():
    """Provide a sample AWS-specific contract."""
    return {
        "id": "aws-product",
        "version": "0.5.7",
        "name": "AWS Data Product",
        "exposes": [
            {
                "id": "raw-data",
                "location": {
                    "format": "s3",
                    "properties": {"bucket": "my-data-bucket", "prefix": "raw/"},
                },
            }
        ],
    }


@pytest.fixture
def sample_plan():
    """Provide a sample execution plan."""
    return {
        "provider": "gcp",
        "actions": [
            {
                "id": "action_1",
                "op": "bigquery.ensure_dataset",
                "dataset": "analytics",
                "location": "us",
            },
            {
                "id": "action_2",
                "op": "bigquery.ensure_table",
                "dataset": "analytics",
                "table": "customers",
            },
        ],
    }


@pytest.fixture
def mock_boto3_client():
    """Provide a mock boto3 client."""
    with patch("boto3.client") as mock_client:
        yield mock_client


@pytest.fixture
def mock_bigquery_client():
    """Provide a mock BigQuery client."""
    with patch("google.cloud.bigquery.Client") as mock_client:
        yield mock_client


@pytest.fixture(autouse=True)
def _disable_copilot_self_eval(monkeypatch):
    """Disable self-evaluation LLM calls in all tests.

    The self-evaluation feature calls ``call_llm`` a second time after
    generation succeeds.  This interferes with tests that mock
    ``call_llm`` with exact side-effect counts.
    """
    monkeypatch.setenv("FLUID_COPILOT_SELF_EVAL", "0")


def _noop_assert_safe_url(url, *, allow_private=False):  # noqa: ARG001
    """Replacement for safe_http.assert_safe_url used in unit tests.

    Returns a sentinel ``(hostname, pinned_ip)`` so callers that unpack
    the tuple keep working without doing a real DNS lookup. Synthetic
    test hostnames like ``airbyte.test`` / ``databricks.test`` /
    ``kafka-connect.test`` are NOT in any DNS, so the real function
    would raise UnsafeURLError on every test that constructs a
    safe_httpx_client.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.hostname or "test", "127.0.0.1"


def _noop_pin_hook_factory(*, allow_private: bool = False):  # noqa: ARG001
    def _hook(request) -> None:  # noqa: ARG001
        return None

    return _hook


@pytest.fixture(autouse=True)
def _disable_ssrf_guard_for_unit_tests(request):
    """Replace the SSRF guard primitives with no-ops for the unit-test
    suite so respx-mocked synthetic hostnames work without real DNS.

    The guard itself is tested explicitly in ``tests/util/test_safe_http.py``
    — we DON'T noop there. Detection by path so the policy stays local
    to one file even if new tests land for the guard later.
    """
    fspath = str(getattr(request.node, "fspath", "") or "")
    if "tests/util/test_safe_http.py" in fspath:
        # The SSRF guard's own tests need the real implementation.
        yield
        return

    with (
        patch(
            "fluid_build.util.safe_http.assert_safe_url",
            side_effect=_noop_assert_safe_url,
        ),
        patch(
            "fluid_build.util.safe_http._make_request_pin_hook",
            side_effect=_noop_pin_hook_factory,
        ),
    ):
        yield


# ── Source-aligned acquisition test infrastructure (Slice A) ────────────
#
# Re-export the shared fixtures from tests/_infrastructure/ so individual
# test files can pull them by name without an explicit import. Lazy import
# guarded against missing optional dependencies (testcontainers, respx).

try:  # pragma: no cover — exercised at collection time
    from tests._infrastructure.respx_fixtures import (  # noqa: F401
        airbyte_mock,
        datahub_mock,
        glue_mock,
        kafka_connect_mock,
        marquez_mock,
        openmetadata_mock,
        snowflake_horizon_mock,
        unity_mock,
    )
except ImportError:
    pass

try:  # pragma: no cover
    from tests._infrastructure.testcontainers_fixtures import (  # noqa: F401
        minio_container,
        mongodb_container,
        mysql_container,
        postgres_container,
        redpanda_container,
        seeded_postgres,
    )
except ImportError:
    pass

# Keyless emulator fixtures (moto / fakesnow) for the `emulated`-marked
# provider integration tests. Optional dependency: the `test-emulators`
# extra. Absent it, the import is skipped and the emulator tests skip.
try:  # pragma: no cover — exercised at collection time
    from tests._infrastructure.emulator_fixtures import (  # noqa: F401
        bigquery_emulator_client,
        fakesnow_patch,
        moto_glue_client,
    )
except ImportError:
    pass
