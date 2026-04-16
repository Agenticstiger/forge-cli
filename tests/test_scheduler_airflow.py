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

"""Tests for the Airflow schedule engine."""

from __future__ import annotations

import pytest

from fluid_build.schedulers.airflow import AirflowScheduler


@pytest.fixture
def scheduler():
    return AirflowScheduler()


@pytest.fixture
def gcp_contract():
    return {
        "id": "analytics.customer_360",
        "name": "Customer 360 Analytics",
        "provider": "gcp",
        "orchestration": {
            "schedule": "0 2 * * *",
            "timezone": "US/Eastern",
            "engine": "airflow",
            "tasks": [
                {
                    "taskId": "create_dataset",
                    "type": "provider_action",
                    "action": "gcp.bigquery.create_dataset",
                    "params": {"dataset_id": "customer_360", "location": "US"},
                },
                {
                    "taskId": "run_transform",
                    "type": "provider_action",
                    "action": "gcp.bigquery.query",
                    "params": {"query": "SELECT * FROM raw.customers"},
                    "dependsOn": ["create_dataset"],
                },
            ],
        },
    }


@pytest.fixture
def aws_contract():
    return {
        "id": "etl.sales_pipeline",
        "name": "Sales ETL Pipeline",
        "provider": "aws",
        "orchestration": {
            "schedule": "@daily",
            "timezone": "UTC",
            "engine": "airflow",
            "tasks": [
                {
                    "taskId": "run_glue_job",
                    "type": "provider_action",
                    "action": "aws.glue.run_job",
                    "params": {"job_name": "sales_transform"},
                },
            ],
        },
    }


@pytest.fixture
def snowflake_contract():
    return {
        "id": "warehouse.dim_customer",
        "name": "Customer Dimension",
        "provider": "snowflake",
        "orchestration": {
            "schedule": "0 3 * * *",
            "timezone": "UTC",
            "engine": "airflow",
            "tasks": [
                {
                    "taskId": "load_customers",
                    "type": "provider_action",
                    "action": "snowflake.sql.execute_sql",
                    "params": {"sql": "INSERT INTO dim.customers SELECT * FROM stg.customers"},
                },
            ],
        },
    }


class TestAirflowSchedulerValidation:
    def test_valid_contract(self, scheduler, gcp_contract):
        issues = scheduler.validate(gcp_contract)
        assert len(issues) == 0

    def test_missing_orchestration(self, scheduler):
        issues = scheduler.validate({"id": "test"})
        assert len(issues) == 1
        assert issues[0].severity.value == "error"
        assert "orchestration" in issues[0].message.lower()

    def test_empty_tasks_warning(self, scheduler):
        contract = {
            "orchestration": {"schedule": "@daily", "tasks": []},
        }
        issues = scheduler.validate(contract)
        warnings = [i for i in issues if i.severity.value == "warning"]
        assert len(warnings) >= 1

    def test_duplicate_task_ids(self, scheduler):
        contract = {
            "orchestration": {
                "tasks": [
                    {"taskId": "dup", "type": "provider_action", "action": "test"},
                    {"taskId": "dup", "type": "provider_action", "action": "test2"},
                ],
            },
        }
        issues = scheduler.validate(contract)
        errors = [i for i in issues if i.severity.value == "error"]
        assert any("uplicate" in i.message for i in errors)

    def test_missing_dependency(self, scheduler):
        contract = {
            "orchestration": {
                "tasks": [
                    {
                        "taskId": "t1",
                        "type": "provider_action",
                        "action": "test",
                        "dependsOn": ["nonexistent"],
                    },
                ],
            },
        }
        issues = scheduler.validate(contract)
        errors = [i for i in issues if i.severity.value == "error"]
        assert any("non-existent" in i.message for i in errors)


class TestAirflowSchedulerGeneration:
    def test_gcp_generates_dag(self, scheduler, gcp_contract):
        files = scheduler.generate(
            gcp_contract,
            provider="gcp",
            provider_config={"project": "my-proj", "region": "us-central1"},
        )
        assert len(files) == 1
        filename = list(files.keys())[0]
        assert filename.endswith("_dag.py")
        content = list(files.values())[0]

        assert "from airflow import DAG" in content
        assert "BigQuery" in content
        assert "my-proj" in content
        assert "create_dataset" in content
        assert "run_transform" in content
        assert "0 2 * * *" in content

    def test_aws_generates_dag(self, scheduler, aws_contract):
        files = scheduler.generate(
            aws_contract,
            provider="aws",
            provider_config={"region": "us-east-1"},
        )
        assert len(files) == 1
        content = list(files.values())[0]

        assert "from airflow import DAG" in content
        assert "GlueJobOperator" in content
        assert "run_glue_job" in content

    def test_snowflake_generates_dag(self, scheduler, snowflake_contract):
        files = scheduler.generate(
            snowflake_contract,
            provider="snowflake",
            provider_config={"connection_id": "sf_conn"},
        )
        assert len(files) == 1
        content = list(files.values())[0]

        assert "from airflow import DAG" in content
        assert "SnowflakeOperator" in content
        assert "load_customers" in content
        assert "sf_conn" in content

    def test_generic_provider_fallback(self, scheduler, gcp_contract):
        # Without specifying provider, should still generate
        files = scheduler.generate(gcp_contract)
        assert len(files) == 1
        content = list(files.values())[0]
        assert "from airflow import DAG" in content

    def test_task_dependencies_in_output(self, scheduler, gcp_contract):
        files = scheduler.generate(gcp_contract, provider="gcp")
        content = list(files.values())[0]
        assert "create_dataset >> run_transform" in content

    def test_attributes(self, scheduler):
        assert scheduler.name == "airflow"
        assert scheduler.supported_platforms is None


class TestDagsterScheduler:
    def test_generates_pipeline(self, gcp_contract):
        from fluid_build.schedulers.dagster import DagsterScheduler

        scheduler = DagsterScheduler()
        files = scheduler.generate(
            gcp_contract,
            provider="gcp",
            provider_config={"project": "my-proj"},
        )
        assert len(files) == 1
        filename = list(files.keys())[0]
        assert filename.endswith("_pipeline.py")
        content = list(files.values())[0]

        assert "from dagster import" in content
        assert "ScheduleDefinition" in content
        assert "create_dataset" in content

    def test_attributes(self):
        from fluid_build.schedulers.dagster import DagsterScheduler

        s = DagsterScheduler()
        assert s.name == "dagster"


class TestPrefectScheduler:
    def test_generates_flow(self, gcp_contract):
        from fluid_build.schedulers.prefect import PrefectScheduler

        scheduler = PrefectScheduler()
        files = scheduler.generate(
            gcp_contract,
            provider="gcp",
            provider_config={"project": "my-proj"},
        )
        assert len(files) == 1
        filename = list(files.keys())[0]
        assert filename.endswith("_flow.py")
        content = list(files.values())[0]

        assert "from prefect import flow, task" in content
        assert "CronSchedule" in content
        assert "create_dataset" in content

    def test_attributes(self):
        from fluid_build.schedulers.prefect import PrefectScheduler

        s = PrefectScheduler()
        assert s.name == "prefect"
