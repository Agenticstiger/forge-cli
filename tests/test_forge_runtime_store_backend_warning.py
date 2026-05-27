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

"""Tests for the staged-coordinator activation warning.

MEMORY-E2E-A finding #53: when ``FLUID_STORE_BACKEND`` is set to a
non-file value (postgres / sqlite / vector) but the staged
coordinator stays inactive — the default for CSV-only forge inputs
— the configured backend silently does nothing. The fix logs a
WARNING once per process explaining the gap and pointing the
operator at ``FLUID_FORGE_STAGED_COPILOT=1`` as the activation
switch.
"""

from __future__ import annotations

import logging

import pytest

from fluid_build.cli.forge_copilot_discovery import DiscoveryReport
from fluid_build.cli.forge_copilot_runtime import (
    _maybe_warn_inactive_staged_coordinator,
    _reset_staged_copilot_warning,
    _should_use_staged_copilot,
)


@pytest.fixture(autouse=True)
def _reset_warning_latch():
    """Each test sees a fresh once-per-process latch."""
    _reset_staged_copilot_warning()
    yield
    _reset_staged_copilot_warning()


# ── _should_use_staged_copilot returns False for plain CSV-only context ─


class TestShouldUseStagedCopilot:
    def test_returns_false_for_empty_context_and_no_data_models(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)
        ctx: dict = {}
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        assert _should_use_staged_copilot(ctx, report) is False

    def test_returns_true_when_env_explicitly_set(self, monkeypatch):
        monkeypatch.setenv("FLUID_FORGE_STAGED_COPILOT", "1")
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        assert _should_use_staged_copilot({}, report) is True

    def test_returns_true_when_data_model_paths_in_context(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        ctx = {"data_model_paths": ["model.sql"]}
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        assert _should_use_staged_copilot(ctx, report) is True

    def test_returns_true_when_discovery_has_user_data_models(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        report = DiscoveryReport(
            workspace_roots=["/tmp/x"],
            user_data_models=[{"path": "ddl.sql"}],
        )
        assert _should_use_staged_copilot({}, report) is True


# ── Warning fires once when backend set but coordinator off ───────────


class TestInactiveCoordinatorWarning:
    def test_warning_fires_when_postgres_set_and_coordinator_off(self, monkeypatch, caplog):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        report = DiscoveryReport(workspace_roots=["/tmp/x"])

        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            result = _should_use_staged_copilot({}, report)
        assert result is False
        # Warning must surface the backend name AND the activation
        # hint so operators can act on it.
        text = caplog.text
        assert "postgres" in text
        assert "FLUID_FORGE_STAGED_COPILOT" in text

    def test_warning_fires_for_sqlite_backend(self, monkeypatch, caplog):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        report = DiscoveryReport(workspace_roots=["/tmp/x"])

        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
        assert "sqlite" in caplog.text

    def test_warning_fires_for_vector_backend(self, monkeypatch, caplog):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        monkeypatch.setenv("FLUID_STORE_BACKEND", "vector")
        report = DiscoveryReport(workspace_roots=["/tmp/x"])

        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
        assert "vector" in caplog.text

    def test_warning_fires_only_once_per_process(self, monkeypatch, caplog):
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        report = DiscoveryReport(workspace_roots=["/tmp/x"])

        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
            _should_use_staged_copilot({}, report)
            _should_use_staged_copilot({}, report)
        # Count by event message — repeated calls must not spam the log.
        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "staged coordinator" in record.getMessage().lower()
        ]
        assert len(warnings) == 1


# ── No warning when backend is default (file) or staged coordinator ON ─


class TestNoWarningCases:
    def test_no_warning_when_backend_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
        assert "staged coordinator" not in caplog.text.lower()

    def test_no_warning_when_backend_is_file(self, monkeypatch, caplog):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
        assert "staged coordinator" not in caplog.text.lower()

    def test_no_warning_when_backend_is_null(self, monkeypatch, caplog):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
        assert "staged coordinator" not in caplog.text.lower()

    def test_no_warning_when_staged_coordinator_active(self, monkeypatch, caplog):
        # Backend is postgres BUT the env flag activates the
        # coordinator — the warning is suppressed because the
        # coordinator IS running and the backend WILL see traffic.
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        monkeypatch.setenv("FLUID_FORGE_STAGED_COPILOT", "1")
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({}, report)
        assert "staged coordinator" not in caplog.text.lower()

    def test_no_warning_when_data_model_paths_present(self, monkeypatch, caplog):
        # Data-model context → staged coordinator activates → no need
        # for the warning.
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        monkeypatch.delenv("FLUID_FORGE_STAGED_COPILOT", raising=False)
        report = DiscoveryReport(workspace_roots=["/tmp/x"])
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _should_use_staged_copilot({"data_model_paths": ["x.sql"]}, report)
        assert "staged coordinator" not in caplog.text.lower()


# ── Direct helper invocation (defensive surface) ──────────────────────


class TestDirectHelperInvocation:
    def test_helper_is_idempotent_after_warning_fires(self, monkeypatch, caplog):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _maybe_warn_inactive_staged_coordinator()
            _maybe_warn_inactive_staged_coordinator()
            _maybe_warn_inactive_staged_coordinator()
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1

    def test_helper_no_op_when_env_unset(self, monkeypatch, caplog):
        monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)
        with caplog.at_level(logging.WARNING, logger="fluid.cli.forge_copilot"):
            _maybe_warn_inactive_staged_coordinator()
        assert "staged coordinator" not in caplog.text.lower()
