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
import logging
from unittest.mock import MagicMock, Mock, patch

import pytest


# ── Session-level self-heal: scrub poisoned personal-memory ───────────
#
# A prior bug let a ``MagicMock`` repr leak into
# ``~/.fluid/personal-memory.json``; it then re-rendered in every
# subsequent forge run's streaming preview.  This session-level fixture
# strips poisoned values once per pytest invocation so contributor
# laptops self-heal without anyone having to think about it.  Idempotent
# (no-op when the file is clean or absent).
@pytest.fixture(scope="session", autouse=True)
def _self_heal_personal_memory():
    try:
        from fluid_build.cli.forge_copilot_personal_memory import (
            _sanitize_existing_personal_memory,
        )

        _sanitize_existing_personal_memory()
    except Exception:  # pragma: no cover — defensive, never fail tests
        pass
    yield


# ── Root-logger isolation: no test may leak global logging state ──────
#
# Several production entry points (structured_logging.configure_structured_logging,
# logging_utils.setup_logger, the CLI's main()) replace the ROOT logger's
# handlers wholesale.  A test that exercises them used to leak that
# configuration into every later test on the same worker — including a
# StreamHandler bound to the (soon closed) captured stderr.  Combined with
# the mcp SDK's per-message warning recording, a leaked utcnow-warning
# formatter produced an unbounded warning-amplification loop that hung CI
# workers for the full pytest-timeout budget (the "[gwN] node down" flake).
# Snapshot and restore root handlers/level/filters around every test so the
# leak class is dead regardless of which test forgets to clean up.
@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_filters = list(root.filters)
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    root.filters[:] = saved_filters


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


@pytest.fixture(autouse=True)
def _stop_leaked_monitoring_threads():
    """Drain every MonitoringSystem's background worker threads after each
    test.

    ``fluid_build.forge.core.monitoring.MonitoringSystem`` starts FOUR
    daemon workers (metric/log/alert processors + aggregator) in its
    ``__init__`` and only stops them on an explicit ``shutdown()``. Tests
    that construct instances directly — and never shut them down — leak
    4 threads each. Across the full suite that compounds into thousands of
    threads, each reserving ~8MB of virtual stack, which exhausts virtual
    address space and gets the process OOM-killed on Linux (this is what
    silently hung the 3.13/3.14 CI jobs at ~95% — low RSS, ~11GB VSZ,
    SIGKILL). Draining per-test keeps the live-thread count flat. Only
    acts when monitoring was actually imported during the test, so it
    costs nothing for the vast majority that never touch it.
    """
    import sys

    yield
    module = sys.modules.get("fluid_build.forge.core.monitoring")
    if module is not None:
        try:
            module._shutdown_all_monitors()
        except Exception:  # pragma: no cover — never fail teardown
            pass


@pytest.fixture(autouse=True)
def _hermetic_keyring():
    """Swap the OS keyring for a fresh in-memory backend in EVERY test.

    A unit test must never touch the real system keyring, for two
    reasons that bit us in CI:

    * **It can BLOCK INDEFINITELY.** On macOS the real backend's
      ``SecItemCopyMatching`` hangs on a locked/headless keychain — it
      *blocks*, it does not raise, so the ``try/except KeyringError`` in
      ``KeyringCredentialStore.get_credential`` cannot save us. Several
      code paths probe the keyring for a saved LLM key
      (``resolve_llm_config`` → ``_infer_provider_from_keyring``), so any
      test that exercises the copilot/judge stack could wedge. This is
      exactly what silently hung the 3.13/3.14 CI jobs for ~10 min until
      the runner cancelled them — and only intermittently, because
      ``pytest-randomly`` has to order such a test *before* any test that
      sets a provider env var (which would short-circuit the probe).
    * **It is non-deterministic.** A key saved on the contributor's real
      keychain would leak into ``_infer_provider_from_keyring`` and
      change behaviour between machines.

    A *fresh* in-memory backend per test makes keyring reads instant,
    credential-free, and isolated — no cross-test leakage. We use
    keyring's documented extension point (``set_keyring`` + a
    ``KeyringBackend`` subclass), the same isolation pattern keyring's
    own test suite and downstream tools (twine, poetry) use. Tests that
    patch ``...keyring_store.keyring`` directly are unaffected (they
    replace a different reference); tests that genuinely save+load a key
    get a working in-memory round-trip.
    """
    try:
        import keyring
        from keyring.backend import KeyringBackend
    except Exception:  # pragma: no cover — keyring not installed: nothing to block on
        yield
        return

    class _InMemoryKeyring(KeyringBackend):
        # ``priority`` is keyring's required backend-ranking attribute;
        # irrelevant here since we install this backend explicitly.
        priority = 1  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()
            self._store: dict = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    previous = keyring.get_keyring()
    keyring.set_keyring(_InMemoryKeyring())
    try:
        yield
    finally:
        keyring.set_keyring(previous)


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
    # Files that exercise the SSRF guard end-to-end and therefore need the
    # REAL implementation (they stub ``socket.getaddrinfo`` themselves to
    # stay offline). Detection by path keeps the policy local.
    _real_guard_files = (
        "tests/util/test_safe_http.py",
        "tests/test_forge_web_tools.py",
    )
    if any(marker in fspath for marker in _real_guard_files):
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
