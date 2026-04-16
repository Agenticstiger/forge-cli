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

"""Tests for the pluggable scheduler engine framework.

Covers the registry, base class, validation types, and built-in
scheduler auto-discovery.
"""

from __future__ import annotations

import pytest

from fluid_build.engines.base import Severity, ValidationIssue
from fluid_build.schedulers.base import ScheduleEngine, ScheduleGenerationResult, ScheduleIntent
from fluid_build.schedulers.registry import (
    _reset_registry,
    get_scheduler,
    has_scheduler,
    list_schedulers,
    list_schedulers_for_platform,
    register_scheduler,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the scheduler registry before and after each test."""
    _reset_registry()
    yield
    _reset_registry()


class _DummyScheduler(ScheduleEngine):
    """Minimal scheduler for testing."""

    name = "dummy"
    supported_platforms = None

    def generate(self, contract, **kwargs) -> ScheduleGenerationResult:
        return {"dummy_dag.py": "# dummy"}

    def validate(self, contract):
        return []


class _GcpOnlyScheduler(ScheduleEngine):
    """Scheduler restricted to GCP."""

    name = "gcp_only"
    supported_platforms = ("gcp",)

    def generate(self, contract, **kwargs) -> ScheduleGenerationResult:
        return {}

    def validate(self, contract):
        return []


# ---------------------------------------------------------------------------
# ScheduleIntent tests
# ---------------------------------------------------------------------------


class TestScheduleIntent:
    def test_defaults(self):
        intent = ScheduleIntent()
        assert intent.schedule == "0 2 * * *"
        assert intent.timezone == "UTC"
        assert intent.tasks == []
        assert intent.provider is None
        assert intent.provider_config == {}

    def test_construction(self):
        intent = ScheduleIntent(
            schedule="@daily",
            timezone="US/Eastern",
            tasks=[{"taskId": "t1", "type": "provider_action"}],
            provider="gcp",
            provider_config={"project": "my-proj"},
        )
        assert intent.schedule == "@daily"
        assert intent.provider == "gcp"
        assert len(intent.tasks) == 1


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestSchedulerRegistry:
    def test_register_and_get(self):
        register_scheduler(_DummyScheduler)
        scheduler = get_scheduler("dummy")
        assert scheduler is not None
        assert scheduler.name == "dummy"

    def test_get_unknown_returns_none(self):
        assert get_scheduler("nonexistent") is None

    def test_has_scheduler(self):
        register_scheduler(_DummyScheduler)
        assert has_scheduler("dummy")
        assert not has_scheduler("nonexistent")

    def test_list_schedulers(self):
        register_scheduler(_DummyScheduler)
        register_scheduler(_GcpOnlyScheduler)
        names = list_schedulers()
        assert names == ["dummy", "gcp_only"]

    def test_list_schedulers_for_platform(self):
        register_scheduler(_DummyScheduler)
        register_scheduler(_GcpOnlyScheduler)

        # GCP platform should include both (dummy is platform-agnostic)
        gcp_schedulers = list_schedulers_for_platform("gcp")
        assert "dummy" in gcp_schedulers
        assert "gcp_only" in gcp_schedulers

        # AWS platform should include only dummy (platform-agnostic)
        aws_schedulers = list_schedulers_for_platform("aws")
        assert "dummy" in aws_schedulers
        assert "gcp_only" not in aws_schedulers

    def test_register_empty_name_raises(self):
        class _NoName(ScheduleEngine):
            name = ""

            def generate(self, contract, **kwargs):
                return {}

            def validate(self, contract):
                return []

        with pytest.raises(ValueError, match="non-empty 'name'"):
            register_scheduler(_NoName)

    def test_override_registration(self):
        register_scheduler(_DummyScheduler)

        class _DummyV2(ScheduleEngine):
            name = "dummy"
            supported_platforms = ("aws",)

            def generate(self, contract, **kwargs):
                return {"v2.py": "# v2"}

            def validate(self, contract):
                return []

        register_scheduler(_DummyV2)
        scheduler = get_scheduler("dummy")
        assert scheduler is not None
        files = scheduler.generate({})
        assert "v2.py" in files

    def test_reset_registry(self):
        register_scheduler(_DummyScheduler)
        assert has_scheduler("dummy")
        _reset_registry()
        assert not has_scheduler("dummy")


# ---------------------------------------------------------------------------
# Built-in scheduler auto-discovery tests
# ---------------------------------------------------------------------------


class TestBuiltinSchedulers:
    """Test that built-in schedulers register via @register_scheduler.

    Uses register_scheduler directly since the autouse fixture clears
    the registry and Python caches modules (so re-import won't re-run
    the decorator).
    """

    def test_airflow_registers(self):
        from fluid_build.schedulers.airflow import AirflowScheduler

        register_scheduler(AirflowScheduler)
        assert has_scheduler("airflow")
        scheduler = get_scheduler("airflow")
        assert scheduler.name == "airflow"

    def test_dagster_registers(self):
        from fluid_build.schedulers.dagster import DagsterScheduler

        register_scheduler(DagsterScheduler)
        assert has_scheduler("dagster")
        scheduler = get_scheduler("dagster")
        assert scheduler.name == "dagster"

    def test_prefect_registers(self):
        from fluid_build.schedulers.prefect import PrefectScheduler

        register_scheduler(PrefectScheduler)
        assert has_scheduler("prefect")
        scheduler = get_scheduler("prefect")
        assert scheduler.name == "prefect"


# ---------------------------------------------------------------------------
# Validation issue reuse tests
# ---------------------------------------------------------------------------


class TestValidationIssueReuse:
    """Verify we properly reuse ValidationIssue from engines."""

    def test_validation_issue_str(self):
        issue = ValidationIssue(
            message="Missing orchestration",
            severity=Severity.ERROR,
            field="orchestration",
        )
        assert "error" in str(issue)
        assert "Missing orchestration" in str(issue)
        assert "[orchestration]" in str(issue)
