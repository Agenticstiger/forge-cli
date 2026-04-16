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

"""Tests for pure logic in cli/init.py: should_generate_dag, create_basic_dag, create_dags_readme."""

import logging

from fluid_build.cli.init import create_basic_dag, create_dags_readme, should_generate_dag


class TestShouldGenerateDag:
    def test_explicit_orchestration(self):
        assert should_generate_dag({"orchestration": {"schedule": "@daily"}}) is True

    def test_orchestrated_template_customer_360(self):
        assert should_generate_dag({}, template="customer-360") is True

    def test_orchestrated_template_sales_analytics(self):
        assert should_generate_dag({}, template="sales-analytics") is True

    def test_orchestrated_template_ml_features(self):
        assert should_generate_dag({}, template="ml-features") is True

    def test_orchestrated_template_data_quality(self):
        assert should_generate_dag({}, template="data-quality") is True

    def test_non_orchestrated_template(self):
        assert should_generate_dag({}, template="analytics-basic") is False

    def test_multiple_provider_actions(self):
        contract = {"binding": {"providerActions": [{"op": "a"}, {"op": "b"}]}}
        assert should_generate_dag(contract) is True

    def test_single_provider_action(self):
        contract = {"binding": {"providerActions": [{"op": "a"}]}}
        assert should_generate_dag(contract) is False

    def test_empty_contract(self):
        assert should_generate_dag({}) is False

    def test_no_template_no_actions(self):
        assert should_generate_dag({"name": "test"}) is False


class TestCreateBasicDag:
    def test_generates_dag_file(self, tmp_path):
        contract = {
            "name": "my-product",
            "orchestration": {"schedule": "@hourly", "retries": 2, "retry_delay": "10m"},
        }
        logger = logging.getLogger("test")
        create_basic_dag(tmp_path, contract, logger)
        dag_dir = tmp_path / "dags"
        assert dag_dir.exists()
        dag_file = dag_dir / "my_product_dag.py"
        assert dag_file.exists()
        content = dag_file.read_text()
        assert "my_product" in content
        assert "@hourly" in content
        assert "'retries': 2" in content

    def test_default_schedule(self, tmp_path):
        contract = {"name": "simple"}
        logger = logging.getLogger("test")
        create_basic_dag(tmp_path, contract, logger)
        dag_file = tmp_path / "dags" / "simple_dag.py"
        content = dag_file.read_text()
        assert "@daily" in content

    def test_dag_has_airflow_imports(self, tmp_path):
        contract = {"name": "test"}
        logger = logging.getLogger("test")
        create_basic_dag(tmp_path, contract, logger)
        content = (tmp_path / "dags" / "test_dag.py").read_text()
        assert "from airflow import DAG" in content
        assert "BashOperator" in content

    def test_dag_task_chain(self, tmp_path):
        contract = {"name": "test"}
        logger = logging.getLogger("test")
        create_basic_dag(tmp_path, contract, logger)
        content = (tmp_path / "dags" / "test_dag.py").read_text()
        assert "validate >> plan >> apply" in content


class TestCreateDagsReadme:
    def test_generates_readme(self, tmp_path):
        dag_dir = tmp_path / "dags"
        dag_dir.mkdir()
        create_dags_readme(dag_dir, "my_product", "@daily", "my_product_dag.py")
        readme = (dag_dir / "README.md").read_text()
        assert "my_product" in readme
        assert "@daily" in readme
        assert "my_product_dag.py" in readme

    def test_readme_has_sections(self, tmp_path):
        dag_dir = tmp_path / "dags"
        dag_dir.mkdir()
        create_dags_readme(dag_dir, "test", "@hourly", "test_dag.py")
        readme = (dag_dir / "README.md").read_text()
        assert "## Usage" in readme or "Usage" in readme
        assert "## Customization" in readme or "Customization" in readme
