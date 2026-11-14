# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# tests/conftest.py
"""
Pytest configuration and shared fixtures for FLUID tests.
"""

import argparse
import logging
from unittest.mock import MagicMock, Mock, patch

import pytest


# ── Root-logger isolation: no test may leak global logging state ──────
#
# Several production entry points (structured_logging.configure_structured_logging,
# structured_logging.configure_structured_logging, the CLI's main()) replace the ROOT logger's
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


@pytest.fixture
def deterministic_welcome_scan(monkeypatch):
    """Make ``run_welcome_scan`` return every probe's result, however slow the runner.

    The scan runs its probes in threads and discards any result that misses its
    1.0s wall-clock budget: right for a CLI prompt, wrong for a test that asserts
    on what a probe reports (workspace products, sample data, ...). On a loaded
    runner (coverage, ``-n auto``) a probe can overrun, its field keeps the
    "nothing detected" default, and the mode picker falls back to ``ai`` where
    the test expects ``from_product`` or ``refine``.

    The budget is lifted so nothing is dropped. The scan still returns as soon
    as every probe has finished, so wall-clock assertions are unaffected. The dbt
    probe is pointed at "no dbt installed" so the result does not depend on the
    host's dbt; tests of the dbt probe patch ``_resolve_dbt_executable`` themselves.

    Opt in with ``pytestmark = pytest.mark.usefixtures("deterministic_welcome_scan")``.
    """
    monkeypatch.setattr("fluid_build.cli._welcome_scan._TOTAL_TIMEOUT_S", 60.0)
    monkeypatch.setattr(
        "fluid_build.build_runners.dbt.runner._resolve_dbt_executable", lambda: None
    )


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


@pytest.fixture(scope="session")
def _empty_fluid_home(tmp_path_factory):
    return tmp_path_factory.mktemp("no-fluid-home")


@pytest.fixture(autouse=True)
def _hermetic_saved_ai_config(monkeypatch, _empty_fluid_home):
    """No test reads the developer's saved ``fluid ai setup`` choice.

    Provider selection and ``fluid doctor`` read it through
    ``_ai_config_shared.load_ai_config``: the ``llm:`` section of the unified
    ``~/.fluid/config.yaml``, then ``~/.fluid/ai_config.json``. A contributor's
    own saved provider would change what a test resolves, as the OS keyring
    would without ``_hermetic_keyring``. Both default paths point into a
    per-test directory that does not exist, so nothing one test saves reaches
    another. A test that wants a saved choice patches ``load_ai_config`` or
    ``_CONFIG_FILE`` itself, or sets ``FLUID_CONFIG`` / ``FLUID_HOME``, which
    ``unified_config_path`` reads before the home default.
    """
    import os
    import sys
    import uuid

    home = _empty_fluid_home / uuid.uuid4().hex
    config_file = home / "ai_config.json"
    monkeypatch.setattr("fluid_build._ai_config_shared._CONFIG_FILE", config_file)
    ai_setup = sys.modules.get("fluid_build.cli.ai_setup")
    if ai_setup is not None:
        # The same object: ``ai_setup`` re-exports the constant, and its write
        # path reads it there.
        monkeypatch.setattr(ai_setup, "_CONFIG_FILE", config_file)
    if not (os.environ.get("FLUID_CONFIG") or os.environ.get("FLUID_HOME")):
        monkeypatch.setenv("FLUID_CONFIG", str(home / "config.yaml"))


# What interactive setup writes straight into ``os.environ``: ``set_session_env``
# exports the provider key it found (``PROVIDER_ENV_VARS``), and a saved Ollama
# choice sets ``OLLAMA_HOST``. Monkeypatch cannot undo a direct assignment.
_SESSION_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GITHUB_API_KEY",
    "OLLAMA_HOST",
)


@pytest.fixture(autouse=True)
def _session_env_is_restored(monkeypatch):
    """No test leaves a provider key or ``OLLAMA_HOST`` behind for the next one.

    A key one test exports (the import tests' canary, for one) made a later
    test in the same worker select that provider: interactive setup reads the
    key variables from ``os.environ``. The values are put back as the test found
    them. Requesting ``monkeypatch`` tears this down first, so what monkeypatch
    set is then undone by monkeypatch itself.
    """
    import os

    found = {name: os.environ.get(name) for name in _SESSION_ENV_VARS}
    yield
    for name, value in found.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def no_llm_key_variables(monkeypatch):
    """No provider key variable or LLM selector is set.

    Provider selection reads the key variables (``OPENAI_API_KEY``,
    ``ANTHROPIC_API_KEY``, ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY``,
    ``OLLAMA_HOST``) and the named selectors from the environment. A test that
    needs none of them set asks for this fixture.
    """
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OLLAMA_HOST",
        "FLUID_LLM_PROVIDER",
        "FLUID_FORGE_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)


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
        "tests/build_runners/test_deployment_host_policy_real_pin_hook.py",
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


# ── Catalog registrar registry isolation: no test may leak a registrar ──
#
# ``fluid_build.build_runners._catalog._REGISTRY`` is a process-global dict
# with a public writer (``register_registrar``) and no un-register. It is the
# FIRST thing both dispatchers consult — ``register_all`` and
# ``register_all_payload`` return whatever is in it before they ever look at
# env vars, ``target_configs`` or the backend spec. So membership in this dict
# IS the definition of "this catalog target is configured", and it outranks
# every knob a test can set.
#
# One test seeding it and forgetting to clean up therefore reconfigures every
# later test in the same worker. That is not hypothetical: a test that seeded
# an OpenMetadata registrar pointing at the respx placeholder host
# ``https://openmetadata.test`` left it behind, and
# ``test_unconfigured_publish_names_the_missing_setting`` — which configures
# nothing and asserts the operator is told which setting is missing — resolved
# that leaked registrar instead, issued a real HTTP PUT and failed with
# ``[Errno 8] nodename nor servname provided, or not known``. The respx router
# that made the host safe was function-scoped and had already been torn down.
# It only showed up under some pytest-randomly orderings, because alphabetical
# collection happens to run the victim first.
#
# Every cleanup for this registry in the suite is hand-written per test
# (``orch._REGISTRY.pop(...)`` in a finally), so the invariant held only by
# author discipline. Snapshot and restore around every test and the leak class
# is dead regardless of which test forgets. Costs nothing for the vast
# majority of tests, which never import the module.
@pytest.fixture(autouse=True)
def _isolate_catalog_registrar_registry():
    import sys

    module_name = "fluid_build.build_runners._catalog"
    module = sys.modules.get(module_name)
    # None means "not imported yet"; the registry is empty at import time, so
    # a test that imports it mid-run must leave it empty again.
    before = dict(module._REGISTRY) if module is not None else None

    yield

    module = sys.modules.get(module_name)
    if module is None:  # pragma: no cover — nothing could have been seeded
        return
    try:
        module._REGISTRY.clear()
        module._REGISTRY.update(before or {})
    except Exception:  # pragma: no cover — never fail teardown
        pass


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
        fakesnow_server,
        fakesnow_server_target,
        moto_glue_client,
    )
except ImportError:
    pass
