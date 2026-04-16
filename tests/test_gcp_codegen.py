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

"""Tests for GCP codegen: dagster.py and prefect.py — pure code generators."""

import pytest

from fluid_build.providers.gcp.codegen.dagster import (
    _convert_schedule as dagster_schedule,
)
from fluid_build.providers.gcp.codegen.dagster import (
    _generate_header as dagster_header,
)
from fluid_build.providers.gcp.codegen.dagster import (
    _generate_imports as dagster_imports,
)
from fluid_build.providers.gcp.codegen.dagster import (
    _generate_job as dagster_job,
)
from fluid_build.providers.gcp.codegen.dagster import (
    _generate_resources as dagster_resources,
)
from fluid_build.providers.gcp.codegen.dagster import (
    _generate_single_op,
    generate_dagster_pipeline,
)
from fluid_build.providers.gcp.codegen.dagster import (
    _sanitize_name as dagster_sanitize,
)
from fluid_build.providers.gcp.codegen.prefect import (
    _convert_schedule as prefect_schedule,
)
from fluid_build.providers.gcp.codegen.prefect import (
    _generate_config,
    _generate_deployment,
    _generate_single_task,
    generate_prefect_flow,
)
from fluid_build.providers.gcp.codegen.prefect import (
    _generate_flow as prefect_flow,
)
from fluid_build.providers.gcp.codegen.prefect import (
    _generate_header as prefect_header,
)
from fluid_build.providers.gcp.codegen.prefect import (
    _generate_imports as prefect_imports,
)
from fluid_build.providers.gcp.codegen.prefect import (
    _sanitize_name as prefect_sanitize,
)

# ═══════════════════════════════════════════════════════════════════
# Dagster codegen
# ═══════════════════════════════════════════════════════════════════


class TestDagsterSanitizeName:
    def test_clean(self):
        assert dagster_sanitize("my_pipeline") == "my_pipeline"

    def test_dashes(self):
        assert dagster_sanitize("my-pipe") == "my_pipe"

    def test_dots(self):
        assert dagster_sanitize("org.proj") == "org_proj"

    def test_spaces(self):
        assert dagster_sanitize("my pipe") == "my_pipe"


class TestDagsterConvertSchedule:
    def test_hourly(self):
        assert dagster_schedule("@hourly") == "0 * * * *"

    def test_daily(self):
        assert dagster_schedule("@daily") == "0 0 * * *"

    def test_weekly(self):
        assert dagster_schedule("@weekly") == "0 0 * * 0"

    def test_monthly(self):
        assert dagster_schedule("@monthly") == "0 0 1 * *"

    def test_cron_passthrough(self):
        assert dagster_schedule("0 3 * * *") == "0 3 * * *"

    def test_unknown_defaults_daily(self):
        assert dagster_schedule("every_5_min") == "0 2 * * *"


class TestDagsterHeader:
    def test_contains_metadata(self):
        h = dagster_header("cid", "name", "daily", "UTC", "proj", "us-c1")
        assert "cid" in h
        assert "name" in h
        assert "DO NOT EDIT" in h


class TestDagsterImports:
    def test_contains_dagster(self):
        imp = dagster_imports()
        assert "from dagster import" in imp
        assert "bigquery" in imp


class TestDagsterResources:
    def test_contains_project(self):
        res = dagster_resources("my-proj", "us-central1")
        assert "my-proj" in res
        assert "us-central1" in res


class TestGenerateSingleOp:
    def test_bigquery_create_dataset(self):
        task = {
            "taskId": "create_ds",
            "action": "gcp.bigquery.create_dataset",
            "params": {"dataset_id": "analytics"},
        }
        result = _generate_single_op(task, "proj", "us-c1")
        assert "analytics" in result
        assert "bigquery_client" in result

    def test_gcs_create_bucket(self):
        task = {
            "taskId": "make_bucket",
            "action": "gcp.gcs.create_bucket",
            "params": {"bucket": "my-bucket"},
        }
        result = _generate_single_op(task, "proj", "us-c1")
        assert "my-bucket" in result

    def test_pubsub_create_topic(self):
        task = {
            "taskId": "make_topic",
            "action": "gcp.pubsub.create_topic",
            "params": {"topic": "events"},
        }
        result = _generate_single_op(task, "proj", "us-c1")
        assert "events" in result

    def test_unknown_service_fallback(self):
        task = {"taskId": "custom", "action": "gcp.custom.do_thing", "params": {}}
        result = _generate_single_op(task, "proj", "us-c1")
        assert "Generic op" in result

    def test_short_action_fallback(self):
        task = {"taskId": "short", "action": "custom", "params": {}}
        result = _generate_single_op(task, "proj", "us-c1")
        assert "Generic op" in result

    def test_dependencies_in_ins(self):
        task = {
            "taskId": "t2",
            "action": "gcp.bigquery.create_dataset",
            "params": {"dataset_id": "ds"},
            "dependsOn": ["t1"],
        }
        result = _generate_single_op(task, "proj", "us-c1")
        assert "dep_t1" in result


class TestDagsterJob:
    def test_basic_job(self):
        tasks = [{"taskId": "t1"}, {"taskId": "t2", "dependsOn": ["t1"]}]
        result = dagster_job("my-pipeline", tasks, "@daily", "UTC")
        assert "my_pipeline" in result  # sanitized name
        assert "ScheduleDefinition" in result


class TestGenerateDagsterPipeline:
    def test_missing_orchestration_raises(self):
        with pytest.raises(ValueError, match="orchestration"):
            generate_dagster_pipeline({}, "proj", "us-c1")

    def test_empty_tasks_raises(self):
        with pytest.raises(ValueError):
            generate_dagster_pipeline({"orchestration": {"tasks": []}}, "p", "r")

    def test_full_pipeline(self):
        contract = {
            "id": "test-pipe",
            "name": "Test Pipeline",
            "orchestration": {
                "schedule": "@daily",
                "tasks": [
                    {
                        "taskId": "ds",
                        "type": "provider_action",
                        "action": "gcp.bigquery.create_dataset",
                        "params": {"dataset_id": "analytics"},
                    },
                ],
            },
        }
        result = generate_dagster_pipeline(contract, "my-proj", "us-c1")
        assert "test-pipe" in result or "test_pipe" in result
        assert "analytics" in result


# ═══════════════════════════════════════════════════════════════════
# Prefect codegen
# ═══════════════════════════════════════════════════════════════════


class TestPrefectSanitizeName:
    def test_clean(self):
        assert prefect_sanitize("my_flow") == "my_flow"

    def test_dashes(self):
        assert prefect_sanitize("my-flow") == "my_flow"


class TestPrefectConvertSchedule:
    def test_hourly(self):
        assert prefect_schedule("@hourly") == "0 * * * *"

    def test_daily(self):
        assert prefect_schedule("@daily") == "0 0 * * *"

    def test_cron_passthrough(self):
        assert prefect_schedule("0 3 * * *") == "0 3 * * *"

    def test_unknown_defaults(self):
        assert prefect_schedule("unknown") == "0 2 * * *"


class TestPrefectHeader:
    def test_contains_metadata(self):
        h = prefect_header("cid", "name", "daily", "UTC", "proj", "us-c1")
        assert "cid" in h
        assert "DO NOT EDIT" in h


class TestPrefectImports:
    def test_contains_prefect(self):
        imp = prefect_imports()
        assert "from prefect import" in imp


class TestPrefectConfig:
    def test_contains_project(self):
        cfg = _generate_config("my-proj", "us-c1")
        assert "my-proj" in cfg
        assert "us-c1" in cfg


class TestGenerateSingleTask:
    def test_bigquery_task(self):
        task = {
            "taskId": "create_ds",
            "action": "gcp.bigquery.create_dataset",
            "params": {"dataset_id": "analytics"},
        }
        result = _generate_single_task(task, "proj", "us-c1")
        assert "analytics" in result
        assert "@task" in result

    def test_gcs_task(self):
        task = {
            "taskId": "make_bucket",
            "action": "gcp.gcs.ensure_bucket",
            "params": {"bucket": "my-bucket"},
        }
        result = _generate_single_task(task, "proj", "us-c1")
        assert "my-bucket" in result

    def test_pubsub_task(self):
        task = {
            "taskId": "make_topic",
            "action": "gcp.pubsub.create_topic",
            "params": {"topic": "events"},
        }
        result = _generate_single_task(task, "proj", "us-c1")
        assert "events" in result

    def test_unknown_service_fallback(self):
        task = {"taskId": "custom", "action": "gcp.custom.foo", "params": {}}
        result = _generate_single_task(task, "proj", "us-c1")
        assert "Generic task" in result


class TestPrefectFlow:
    def test_basic_flow(self):
        tasks = [{"taskId": "t1"}, {"taskId": "t2", "dependsOn": ["t1"]}]
        result = prefect_flow("my-flow", "My Flow", tasks)
        assert "my_flow" in result
        assert "@flow" in result


class TestPrefectDeployment:
    def test_basic_deployment(self):
        result = _generate_deployment("my-flow", "My Flow", "@daily", "UTC")
        assert "Deployment" in result
        assert "my_flow" in result


class TestGeneratePrefectFlow:
    def test_missing_orchestration_raises(self):
        with pytest.raises(ValueError, match="orchestration"):
            generate_prefect_flow({}, "proj", "us-c1")

    def test_full_flow(self):
        contract = {
            "id": "test-flow",
            "name": "Test Flow",
            "orchestration": {
                "schedule": "@daily",
                "tasks": [
                    {
                        "taskId": "ds",
                        "type": "provider_action",
                        "action": "gcp.bigquery.create_dataset",
                        "params": {"dataset_id": "analytics"},
                    },
                ],
            },
        }
        result = generate_prefect_flow(contract, "my-proj", "us-c1")
        assert "analytics" in result
        assert "Deployment" in result
