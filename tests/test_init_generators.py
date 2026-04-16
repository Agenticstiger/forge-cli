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

"""Tests for generate_contracts_from_scan (init_scan.py).

The CI/CD generator tests that used to live here were deleted alongside
``init.py``'s ``generate_cicd`` and its four platform-specific helpers
(``generate_jenkinsfile``, ``generate_github_actions``, ``generate_gitlab_ci``,
``generate_cloudbuild``) when that dead code was removed.  ``fluid
scaffold-ci`` has its own implementation and is tested separately.
"""

import logging

import pytest

from fluid_build.cli.init_scan import generate_contracts_from_scan
from fluid_build.schema_manager import FluidSchemaManager


@pytest.fixture
def logger():
    return logging.getLogger("test_init_gen")


class TestGenerateContractsFromScan:
    def test_dbt_project(self, logger):
        results = {
            "project_type": "dbt",
            "metadata": {
                "project_name": "my-dbt",
                "target_platform": "gcp",
                "target_database": "proj",
                "target_schema": "ds",
            },
            "models": [
                {
                    "name": "orders",
                    "raw_sql": "SELECT * FROM raw.orders",
                    "columns": [
                        {"name": "id", "type": "integer"},
                        {"name": "amount", "type": "float"},
                    ],
                },
                {
                    "name": "users",
                    "raw_sql": "SELECT * FROM raw.users",
                    "columns": [{"name": "user_id"}],
                },
            ],
        }
        contracts = generate_contracts_from_scan(results, "gcp", logger)
        assert len(contracts) == 1
        c = contracts[0]
        assert c["name"] == "my-dbt"
        assert c["fluidVersion"] == FluidSchemaManager.latest_bundled_version()
        assert c["kind"] == "DataProduct"
        assert len(c["exposes"]) == 2
        assert c["exposes"][0]["exposeId"] == "orders"
        # 0.7.2 canonical: binding lives per-expose.
        assert c["exposes"][0]["binding"]["platform"] == "gcp"
        assert c["exposes"][0]["binding"]["location"]["project"] == "proj"

    def test_dbt_with_local_provider(self, logger):
        """Zero-model dbt scans must fail instead of emitting invalid 0.7.2 contracts."""
        results = {
            "project_type": "dbt",
            "metadata": {"project_name": "local-dbt", "target_platform": "duckdb"},
            "models": [],
        }
        with pytest.raises(ValueError, match="requires at least one expose"):
            generate_contracts_from_scan(results, "local", logger)

        invalid_candidate = {
            "fluidVersion": FluidSchemaManager.latest_bundled_version(),
            "kind": "DataProduct",
            "id": "scan.dbt.local-dbt",
            "name": "local-dbt",
            "description": "Imported from dbt project on test",
            "domain": "imported",
            "metadata": {"owner": {"team": "data-team"}},
            "exposes": [],
        }
        validation = FluidSchemaManager().validate_contract(invalid_candidate, offline_only=True)
        assert "exposes: [] should be non-empty" in validation.errors

    def test_terraform_project(self, logger):
        results = {
            "project_type": "terraform",
            "metadata": {"target_platform": "aws"},
        }
        contracts = generate_contracts_from_scan(results, "aws", logger)
        assert len(contracts) == 1
        assert contracts[0]["name"] == "terraform-import"
        assert contracts[0]["exposes"][0]["binding"]["platform"] == "aws"

    def test_sql_project(self, logger):
        results = {
            "project_type": "sql",
            "metadata": {},
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        assert len(contracts) == 1
        assert contracts[0]["name"] == "sql-import"
        assert contracts[0]["exposes"][0]["binding"]["platform"] == "local"

    def test_dbt_schema_columns(self, logger):
        results = {
            "project_type": "dbt",
            "metadata": {"project_name": "test"},
            "models": [
                {"name": "m1", "columns": [{"name": "a", "type": "varchar"}, {"name": "b"}]},
            ],
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        schema = contracts[0]["exposes"][0]["contract"]["schema"]
        assert schema[0]["name"] == "a"
        assert schema[0]["type"] == "varchar"
        assert schema[1]["type"] == "string"  # default

    def test_dbt_limits_models(self, logger):
        results = {
            "project_type": "dbt",
            "metadata": {"project_name": "big"},
            "models": [{"name": f"m{i}"} for i in range(10)],
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        # Only first 5 models
        assert len(contracts[0]["exposes"]) == 5

    def test_dbt_redshift_target_preserves_warehouse_location(self, logger):
        results = {
            "project_type": "dbt",
            "metadata": {
                "project_name": "redshift-dbt",
                "target_platform": "redshift",
                "target_database": "analytics",
                "target_schema": "mart",
                "target_table": "orders",
            },
            "models": [{"name": "orders", "columns": []}],
        }
        contracts = generate_contracts_from_scan(results, "aws", logger)
        binding = contracts[0]["exposes"][0]["binding"]
        assert binding["platform"] == "aws"
        assert binding["format"] == "other"
        assert binding["location"] == {
            "database": "analytics",
            "schema": "mart",
            "table": "orders",
        }
