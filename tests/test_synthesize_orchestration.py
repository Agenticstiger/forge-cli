# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for orchestration synthesis from contract builds."""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.schedulers.synthesis import (
    _resolve_action,
    _sanitize_task_id,
    synthesize_orchestration_from_builds,
)

# ──────────────────────────────────────────────────────────────────────────────
# _sanitize_task_id
# ──────────────────────────────────────────────────────────────────────────────


class TestSanitizeTaskId:
    def test_simple_name(self):
        assert _sanitize_task_id("load_customers") == "load_customers"

    def test_dashes_to_underscores(self):
        assert _sanitize_task_id("load-customers") == "load_customers"

    def test_spaces_to_underscores(self):
        assert _sanitize_task_id("load customers") == "load_customers"

    def test_leading_digit(self):
        assert _sanitize_task_id("1_first_step") == "task_1_first_step"

    def test_empty_string(self):
        assert _sanitize_task_id("") == "task_unnamed"

    def test_special_chars(self):
        result = _sanitize_task_id("build@v2.0!")
        assert result.isidentifier() or result.replace("_", "").isalnum()


# ──────────────────────────────────────────────────────────────────────────────
# _resolve_action
# ──────────────────────────────────────────────────────────────────────────────


class TestResolveAction:
    def test_gcp_sql(self):
        assert _resolve_action("gcp", "sql") == "gcp.bigquery.query"

    def test_aws_python(self):
        assert _resolve_action("aws", "python") == "aws.glue.run_job"

    def test_snowflake_sql(self):
        assert _resolve_action("snowflake", "sql") == "snowflake.sql.execute_sql"

    def test_unknown_provider_falls_back(self):
        assert _resolve_action("databricks", "sql") == "generic.sql.run"

    def test_case_insensitive(self):
        assert _resolve_action("GCP", "SQL") == "gcp.bigquery.query"


# ──────────────────────────────────────────────────────────────────────────────
# synthesize_orchestration_from_builds
# ──────────────────────────────────────────────────────────────────────────────


class TestSynthesizeOrchestration:
    def _make_contract(self, builds, provider="gcp"):
        return {
            "fluidVersion": "0.7.2",
            "kind": "DataProduct",
            "id": "test-product",
            "name": "Test Product",
            "provider": provider,
            "builds": builds,
        }

    def test_empty_builds_returns_empty(self):
        contract = self._make_contract([])
        result = synthesize_orchestration_from_builds(contract, "airflow")
        assert result == {}

    def test_no_builds_key_returns_empty(self):
        contract = {"id": "test"}
        result = synthesize_orchestration_from_builds(contract, "airflow")
        assert result == {}

    def test_basic_sql_builds(self):
        builds = [
            {"id": "load_raw", "engine": "sql", "properties": {"sql": "SELECT 1"}},
            {"id": "transform", "engine": "sql", "properties": {"sql": "SELECT 2"}},
        ]
        result = synthesize_orchestration_from_builds(
            self._make_contract(builds), "airflow", provider="gcp"
        )

        assert result["engine"] == "airflow"
        assert result["schedule"] == "0 2 * * *"
        assert result["timezone"] == "UTC"
        assert len(result["tasks"]) == 2

        t0 = result["tasks"][0]
        assert t0["taskId"] == "load_raw"
        assert t0["action"] == "gcp.bigquery.query"
        assert t0["params"]["query"] == "SELECT 1"
        assert t0["dependsOn"] == []

        t1 = result["tasks"][1]
        assert t1["taskId"] == "transform"
        assert t1["dependsOn"] == ["load_raw"]

    def test_dependency_chain_three_steps(self):
        builds = [
            {"id": "step_a", "engine": "sql"},
            {"id": "step_b", "engine": "sql"},
            {"id": "step_c", "engine": "sql"},
        ]
        result = synthesize_orchestration_from_builds(self._make_contract(builds), "dagster")
        tasks = result["tasks"]
        assert tasks[0]["dependsOn"] == []
        assert tasks[1]["dependsOn"] == ["step_a"]
        assert tasks[2]["dependsOn"] == ["step_b"]

    def test_default_schedule(self):
        builds = [{"id": "x", "engine": "sql"}]
        result = synthesize_orchestration_from_builds(self._make_contract(builds), "prefect")
        assert result["schedule"] == "0 2 * * *"
        assert result["timezone"] == "UTC"

    def test_contract_not_mutated(self):
        contract = self._make_contract([{"id": "x", "engine": "sql"}])
        original = copy.deepcopy(contract)
        synthesize_orchestration_from_builds(contract, "airflow")
        assert contract == original
        assert "orchestration" not in contract

    def test_provider_from_contract(self):
        """Provider should be read from contract if not passed explicitly."""
        builds = [{"id": "x", "engine": "sql"}]
        contract = self._make_contract(builds, provider="aws")
        result = synthesize_orchestration_from_builds(contract, "airflow")
        assert result["tasks"][0]["action"] == "aws.athena.run_query"

    def test_build_without_id_uses_index(self):
        builds = [{"engine": "sql"}, {"engine": "python"}]
        result = synthesize_orchestration_from_builds(
            self._make_contract(builds), "airflow", provider="gcp"
        )
        assert result["tasks"][0]["taskId"] == "step_0"
        assert result["tasks"][1]["taskId"] == "step_1"

    def test_build_with_name_fallback(self):
        builds = [{"name": "My Build", "engine": "sql"}]
        result = synthesize_orchestration_from_builds(self._make_contract(builds), "airflow")
        assert result["tasks"][0]["taskId"] == "my_build"

    def test_python_build_includes_model(self):
        builds = [{"id": "ml_train", "engine": "python", "properties": {"model": "churn_model"}}]
        result = synthesize_orchestration_from_builds(
            self._make_contract(builds), "airflow", provider="aws"
        )
        assert result["tasks"][0]["params"]["model"] == "churn_model"
        assert result["tasks"][0]["action"] == "aws.glue.run_job"


# ──────────────────────────────────────────────────────────────────────────────
# Integration: _generate_schedule_artifacts with synthesis
# ──────────────────────────────────────────────────────────────────────────────


class TestGenerateScheduleArtifactsIntegration:
    @pytest.fixture(autouse=True)
    def _ensure_schedulers_registered(self):
        """Re-discover schedulers in case a previous test reset the registry."""
        import importlib

        import fluid_build.schedulers.airflow
        import fluid_build.schedulers.dagster
        import fluid_build.schedulers.prefect

        importlib.reload(fluid_build.schedulers.airflow)
        importlib.reload(fluid_build.schedulers.dagster)
        importlib.reload(fluid_build.schedulers.prefect)

    def test_generates_dag_from_builds_only_contract(self):
        """Contract with builds but no orchestration should produce DAG files."""
        from fluid_build.cli.forge_modes import _generate_schedule_artifacts

        contract = {
            "fluidVersion": "0.7.2",
            "kind": "DataProduct",
            "id": "customer-analytics",
            "name": "Customer Analytics",
            "builds": [
                {
                    "id": "raw_load",
                    "engine": "sql",
                    "properties": {"sql": "SELECT * FROM raw.customers"},
                },
                {
                    "id": "transform",
                    "engine": "sql",
                    "properties": {"sql": "SELECT id FROM staging.customers"},
                },
            ],
        }
        context = {"schedule_engine": "airflow", "provider": "gcp"}

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            files = _generate_schedule_artifacts(
                contract,
                target_dir=Path(tmp),
                context=context,
                logger=MagicMock(),
                console=None,
            )

        assert len(files) > 0, "Expected at least one DAG file"
        # All files should be under dags/
        for path in files:
            assert path.startswith("dags/"), f"Expected path under dags/, got {path}"
        # Content should mention airflow
        content = list(files.values())[0]
        assert "airflow" in content.lower() or "DAG" in content

    def test_no_schedule_engine_returns_empty(self):
        """Without schedule_engine in context, no files should be generated."""
        import tempfile
        from pathlib import Path

        from fluid_build.cli.forge_modes import _generate_schedule_artifacts

        contract = {"builds": [{"id": "x", "engine": "sql"}]}
        with tempfile.TemporaryDirectory() as tmp:
            files = _generate_schedule_artifacts(
                contract,
                target_dir=Path(tmp),
                context={},
                logger=MagicMock(),
                console=None,
            )
        assert files == {}

    def test_contract_with_orchestration_uses_it_directly(self):
        """If contract already has orchestration, synthesis is skipped."""
        import tempfile
        from pathlib import Path

        from fluid_build.cli.forge_modes import _generate_schedule_artifacts

        contract = {
            "id": "test",
            "name": "Test",
            "orchestration": {
                "engine": "airflow",
                "schedule": "0 3 * * *",
                "timezone": "UTC",
                "tasks": [
                    {
                        "taskId": "custom_task",
                        "type": "provider_action",
                        "action": "gcp.bigquery.query",
                        "params": {},
                        "dependsOn": [],
                    },
                ],
            },
        }
        context = {"schedule_engine": "airflow", "provider": "gcp"}

        with tempfile.TemporaryDirectory() as tmp:
            files = _generate_schedule_artifacts(
                contract,
                target_dir=Path(tmp),
                context=context,
                logger=MagicMock(),
                console=None,
            )

        assert len(files) > 0
