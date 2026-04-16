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

"""Tests for runtimes/airflow_provider_actions.py: AirflowDAGGenerator."""

import logging
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.runtimes.airflow_provider_actions import AirflowDAGGenerator

LOG = logging.getLogger("test_airflow_actions")


def _make_action(
    action_id="act-1",
    action_type_val="provisionDataset",
    provider="gcp",
    params=None,
    depends_on=None,
    description=None,
):
    """Create a mock action object."""
    from fluid_build.forge.core.provider_actions import ActionType

    action = MagicMock()
    action.action_id = action_id
    action.action_type = ActionType(action_type_val)
    action.provider = provider
    action.params = params or {}
    action.depends_on = depends_on or []
    action.description = description
    return action


# ---------------------------------------------------------------------------
# AirflowDAGGenerator
# ---------------------------------------------------------------------------


class TestAirflowDAGGenerator:
    def test_init_default_logger(self):
        gen = AirflowDAGGenerator()
        assert gen.logger is not None

    def test_init_custom_logger(self):
        gen = AirflowDAGGenerator(logger=LOG)
        assert gen.logger is LOG

    def test_action_operator_map(self):
        assert "provisionDataset" in AirflowDAGGenerator.ACTION_OPERATOR_MAP
        assert "scheduleTask" in AirflowDAGGenerator.ACTION_OPERATOR_MAP
        assert "grantAccess" in AirflowDAGGenerator.ACTION_OPERATOR_MAP
        assert "registerSchema" in AirflowDAGGenerator.ACTION_OPERATOR_MAP
        assert "createView" in AirflowDAGGenerator.ACTION_OPERATOR_MAP

    @patch("fluid_build.forge.core.provider_actions.ProviderActionParser")
    def test_generate_dag_no_actions(self, mock_parser_cls):
        mock_parser = MagicMock()
        mock_parser.parse.return_value = []
        mock_parser_cls.return_value = mock_parser

        gen = AirflowDAGGenerator(logger=LOG)
        contract = {"id": "test.product", "name": "Test"}
        dag_code = gen.generate_dag(contract)
        assert "Empty Airflow DAG" in dag_code

    @patch("fluid_build.forge.core.provider_actions.ProviderActionParser")
    def test_generate_dag_with_actions(self, mock_parser_cls):
        actions = [
            _make_action(
                action_id="provision-dataset",
                action_type_val="provisionDataset",
                provider="gcp",
                params={
                    "exposeId": "orders",
                    "binding": {"location": {"project": "proj", "dataset": "ds"}},
                },
            ),
            _make_action(
                action_id="schedule-build",
                action_type_val="scheduleTask",
                params={"engine": "dbt", "script": "orders_model", "buildId": "b1"},
                depends_on=["provision-dataset"],
            ),
        ]
        mock_parser = MagicMock()
        mock_parser.parse.return_value = actions
        mock_parser_cls.return_value = mock_parser

        gen = AirflowDAGGenerator(logger=LOG)
        contract = {"id": "test.product", "name": "Test Product", "domain": "sales"}
        dag_code = gen.generate_dag(contract)

        assert "dag_id" in dag_code
        assert "test_product" in dag_code
        assert "BashOperator" in dag_code

    @patch("fluid_build.forge.core.provider_actions.ProviderActionParser")
    def test_generate_dag_custom_id_and_schedule(self, mock_parser_cls):
        actions = [_make_action()]
        mock_parser = MagicMock()
        mock_parser.parse.return_value = actions
        mock_parser_cls.return_value = mock_parser

        gen = AirflowDAGGenerator(logger=LOG)
        contract = {"id": "test"}
        dag_code = gen.generate_dag(contract, dag_id="custom_dag", schedule="@hourly")
        assert "custom_dag" in dag_code
        assert "@hourly" in dag_code

    @patch("fluid_build.forge.core.provider_actions.ProviderActionParser")
    def test_generate_dag_write_to_file(self, mock_parser_cls):
        actions = [_make_action()]
        mock_parser = MagicMock()
        mock_parser.parse.return_value = actions
        mock_parser_cls.return_value = mock_parser

        gen = AirflowDAGGenerator(logger=LOG)
        contract = {"id": "test"}

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            tmp_path = f.name

        try:
            dag_code = gen.generate_dag(contract, output_path=tmp_path)
            assert os.path.exists(tmp_path)
            with open(tmp_path) as f:
                content = f.read()
            assert "dag_id" in content
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# _generate_task variants
# ---------------------------------------------------------------------------


class TestGenerateTaskVariants:
    def setup_method(self):
        self.gen = AirflowDAGGenerator(logger=LOG)

    def test_provision_gcp(self):
        action = _make_action(
            action_type_val="provisionDataset",
            provider="gcp",
            params={
                "exposeId": "orders",
                "binding": {"location": {"project": "p1", "dataset": "d1"}},
            },
        )
        code = self.gen._generate_provision_task(action, "provision_orders")
        assert "bq mk" in code
        assert "p1" in code
        assert "d1" in code

    def test_provision_aws(self):
        action = _make_action(
            action_type_val="provisionDataset",
            provider="aws",
            params={"exposeId": "orders"},
        )
        code = self.gen._generate_provision_task(action, "provision_orders")
        assert "aws s3" in code

    def test_provision_other(self):
        action = _make_action(
            action_type_val="provisionDataset",
            provider="local",
            params={"exposeId": "orders"},
        )
        code = self.gen._generate_provision_task(action, "provision_orders")
        assert "Provision orders" in code

    def test_schedule_dbt(self):
        action = _make_action(
            action_type_val="scheduleTask",
            params={"engine": "dbt", "script": "model", "buildId": "b1"},
        )
        code = self.gen._generate_schedule_task(action, "schedule_b1")
        assert "dbt run" in code

    def test_schedule_sql(self):
        action = _make_action(
            action_type_val="scheduleTask",
            params={"engine": "sql", "script": "SELECT 1"},
        )
        code = self.gen._generate_schedule_task(action, "schedule_sql")
        assert "SQL" in code

    def test_schedule_other(self):
        action = _make_action(
            action_type_val="scheduleTask",
            params={"engine": "spark", "script": "spark_job.py"},
        )
        code = self.gen._generate_schedule_task(action, "schedule_spark")
        assert "spark_job.py" in code

    def test_grant_task(self):
        action = _make_action(
            action_type_val="grantAccess",
            params={"principal": "user@test.com", "role": "editor", "exposeId": "orders"},
        )
        code = self.gen._generate_grant_task(action, "grant_access")
        assert "Grant editor" in code
        assert "user@test.com" in code

    def test_register_schema(self):
        action = _make_action(
            action_type_val="registerSchema",
            params={"schemaName": "orders_schema"},
        )
        code = self.gen._generate_register_schema_task(action, "register_schema")
        assert "orders_schema" in code

    def test_create_view(self):
        action = _make_action(
            action_type_val="createView",
            params={"viewName": "orders_view"},
        )
        code = self.gen._generate_create_view_task(action, "create_view")
        assert "orders_view" in code

    def test_generic_task(self):
        action = _make_action(
            action_type_val="custom",
            description="Custom operation",
        )
        code = self.gen._generate_generic_task(action, "custom_task")
        assert "Custom operation" in code

    def test_generic_task_no_description(self):
        action = _make_action(action_type_val="custom", description=None)
        code = self.gen._generate_generic_task(action, "custom_task")
        assert "custom" in code


# ---------------------------------------------------------------------------
# _generate_dependencies
# ---------------------------------------------------------------------------


class TestGenerateDependencies:
    def test_no_dependencies(self):
        gen = AirflowDAGGenerator(logger=LOG)
        actions = [_make_action(action_id="a1", depends_on=[])]
        code = gen._generate_dependencies(actions)
        assert "No dependencies specified" in code

    def test_with_dependencies(self):
        gen = AirflowDAGGenerator(logger=LOG)
        actions = [
            _make_action(action_id="a1", depends_on=[]),
            _make_action(action_id="a2", depends_on=["a1"]),
        ]
        code = gen._generate_dependencies(actions)
        assert "a1 >> a2" in code


# ---------------------------------------------------------------------------
# _generate_empty_dag
# ---------------------------------------------------------------------------


class TestGenerateEmptyDag:
    def test_empty_dag_defaults(self):
        gen = AirflowDAGGenerator(logger=LOG)
        contract = {"id": "my.product"}
        code = gen._generate_empty_dag(contract, None, None)
        assert "my_product" in code
        assert "@daily" in code

    def test_empty_dag_custom(self):
        gen = AirflowDAGGenerator(logger=LOG)
        contract = {"id": "test"}
        code = gen._generate_empty_dag(contract, "custom_id", "@hourly")
        assert "custom_id" in code
        assert "@hourly" in code


# ---------------------------------------------------------------------------
# _generate_dag_header
# ---------------------------------------------------------------------------


class TestGenerateDagHeader:
    def test_header(self):
        gen = AirflowDAGGenerator(logger=LOG)
        contract = {
            "name": "Test Product",
            "description": "A test",
            "domain": "analytics",
            "fluidVersion": "0.7.1",
        }
        header = gen._generate_dag_header("my_dag", "@daily", contract)
        assert "my_dag" in header
        assert "@daily" in header
        assert "Test Product" in header
        assert "analytics" in header
