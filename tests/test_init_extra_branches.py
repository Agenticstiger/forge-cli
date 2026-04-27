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

"""Branch-coverage tests for fluid_build.cli.init"""

import argparse
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.import_cmd import (
    DbtDetector,
    SqlFileDetector,
    TerraformDetector,
    detect_project_type,
)
from fluid_build.cli.init import (
    COMMAND,
    _mark_first_run_complete,
    copy_template,
    create_basic_dag,
    create_dags_readme,
    detect_mode,
    init_local_db,
    register,
    should_generate_dag,
)
from fluid_build.cli.init_scan import (
    generate_contracts_from_scan,
    show_migration_summary,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_init")


# ── Constants ────────────────────────────────────────────────────────


class TestConstants:
    def test_command(self):
        assert COMMAND == "init"


# ── register ─────────────────────────────────────────────────────────


class TestRegister:
    def test_register_adds_parser(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["init", "my-project", "--quickstart"])
        assert args.name == "my-project"
        assert args.quickstart is True


# ── _mark_first_run_complete ─────────────────────────────────────────


class TestMarkFirstRunComplete:
    def test_creates_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _mark_first_run_complete()
        assert (tmp_path / ".fluid").is_dir()

    def test_oserror_is_silent(self, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: Path("/proc/nonexistent"))
        _mark_first_run_complete()  # Should not raise


# ── detect_mode ──────────────────────────────────────────────────────


class TestDetectMode:
    def test_explicit_quickstart(self, logger):
        """--quickstart is now an alias for --template customer-360."""
        args = SimpleNamespace(quickstart=True, blank=False, template=False, name=None)
        assert detect_mode(args, logger) == "template"
        assert args.template == "customer-360"

    def test_explicit_blank(self, logger):
        args = SimpleNamespace(quickstart=False, blank=True, template=False, name=None)
        assert detect_mode(args, logger) == "blank"

    def test_explicit_template(self, logger):
        args = SimpleNamespace(quickstart=False, blank=False, template="x", name=None)
        assert detect_mode(args, logger) == "template"

    def test_existing_contract_returns_none(self, logger, tmp_path, monkeypatch):
        (tmp_path / "contract.fluid.yaml").write_text("test")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(quickstart=False, blank=False, template=False, name=None)
        assert detect_mode(args, logger) is None

    def test_dbt_project_falls_through(self, logger, tmp_path, monkeypatch):
        """dbt/terraform/sql auto-detect removed; falls through to creation menu."""
        (tmp_path / "dbt_project.yml").write_text("name: test")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(quickstart=False, blank=False, template=False, name=None)
        with patch("fluid_build.cli.init._ask_creation_mode", return_value="ai"):
            assert detect_mode(args, logger) == "ai"

    def test_terraform_falls_through(self, logger, tmp_path, monkeypatch):
        (tmp_path / "main.tf").write_text("resource {}")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(quickstart=False, blank=False, template=False, name=None)
        with patch("fluid_build.cli.init._ask_creation_mode", return_value="template"):
            assert detect_mode(args, logger) == "template"

    def test_sql_files_fall_through(self, logger, tmp_path, monkeypatch):
        (tmp_path / "query.sql").write_text("SELECT 1")
        monkeypatch.chdir(tmp_path)
        args = SimpleNamespace(quickstart=False, blank=False, template=False, name=None)
        with patch("fluid_build.cli.init._ask_creation_mode", return_value="blank"):
            assert detect_mode(args, logger) == "blank"

    def test_first_time_user_shows_menu(self, logger, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
        args = SimpleNamespace(quickstart=False, blank=False, template=False, name=None)
        with patch("fluid_build.cli.init._ask_creation_mode", return_value="ai") as m:
            assert detect_mode(args, logger) == "ai"
            m.assert_called_once()

    def test_default_shows_menu(self, logger, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".fluid").mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        args = SimpleNamespace(quickstart=False, blank=False, template=False, name=None)
        with patch("fluid_build.cli.init._ask_creation_mode", return_value="template") as m:
            assert detect_mode(args, logger) == "template"
            m.assert_called_once()


# ── should_generate_dag ─────────────────────────────────────────────


class TestShouldGenerateDag:
    def test_orchestration_present(self):
        assert should_generate_dag({"orchestration": {"schedule": "@daily"}}) is True

    def test_customer_360_template(self):
        assert should_generate_dag({}, template="customer-360") is True

    def test_sales_analytics_template(self):
        assert should_generate_dag({}, template="sales-analytics") is True

    def test_ml_features_template(self):
        assert should_generate_dag({}, template="ml-features") is True

    def test_data_quality_template(self):
        assert should_generate_dag({}, template="data-quality") is True

    def test_unknown_template(self):
        assert should_generate_dag({}, template="custom") is False

    def test_multiple_provider_actions(self):
        contract = {"binding": {"providerActions": ["a", "b"]}}
        assert should_generate_dag(contract) is True

    def test_single_provider_action(self):
        contract = {"binding": {"providerActions": ["a"]}}
        assert should_generate_dag(contract) is False

    def test_empty_contract(self):
        assert should_generate_dag({}) is False

    def test_no_template_no_orchestration(self):
        assert should_generate_dag({"binding": {}}) is False


# ── create_basic_dag ─────────────────────────────────────────────────


class TestCreateBasicDag:
    def test_creates_dag_file(self, tmp_path, logger):
        contract = {
            "name": "my-product",
            "orchestration": {"schedule": "@hourly", "retries": 2, "retry_delay": "10m"},
        }
        create_basic_dag(tmp_path, contract, logger)
        dag_file = tmp_path / "dags" / "my_product_dag.py"
        assert dag_file.exists()
        content = dag_file.read_text()
        assert "my_product" in content
        assert "@hourly" in content

    def test_defaults(self, tmp_path, logger):
        contract = {}
        create_basic_dag(tmp_path, contract, logger)
        dag_file = tmp_path / "dags" / "my_product_dag.py"
        assert dag_file.exists()
        content = dag_file.read_text()
        assert "@daily" in content


# ── create_dags_readme ───────────────────────────────────────────────


class TestCreateDagsReadme:
    def test_creates_readme(self, tmp_path):
        dag_dir = tmp_path / "dags"
        dag_dir.mkdir()
        create_dags_readme(dag_dir, "my_dag", "@daily", "my_dag_dag.py")
        readme = dag_dir / "README.md"
        assert readme.exists()
        content = readme.read_text()
        assert "my_dag" in content


# ── copy_template ────────────────────────────────────────────────────


class TestCopyTemplate:
    def test_existing_template_creates_target_dir(self, tmp_path, logger):
        project_dir = tmp_path / "smoke-test"

        result = copy_template(project_dir, "hello-world", logger)

        assert result is True
        assert project_dir.is_dir()
        assert (project_dir / ".template-meta.yaml").exists()
        assert (project_dir / "README.md").exists()
        assert (project_dir / "contract.fluid.yaml").exists()

    def test_template_not_found(self, tmp_path, logger):
        result = copy_template(tmp_path / "out", "nonexistent-xyz-9999", logger)
        assert result is False


# ── init_local_db ────────────────────────────────────────────────────


class TestInitLocalDb:
    def test_non_local_provider_skips(self, tmp_path, logger):
        init_local_db(tmp_path, "gcp", logger)
        # Should do nothing
        assert not (tmp_path / ".fluid").exists()

    @patch("fluid_build.cli.init.duckdb", create=True)
    def test_local_provider_creates_db(self, _mock_duckdb, tmp_path, logger):
        # The import is inside the function, mock it at module level after import
        with patch.dict("sys.modules", {"duckdb": MagicMock()}):
            # Re-call init_local_db directly, duckdb imported lazily
            init_local_db(tmp_path, "local", logger)

    def test_import_error_is_handled(self, tmp_path, logger):
        with patch.dict("sys.modules", {"duckdb": None}):
            # duckdb=None causes ImportError on import
            init_local_db(tmp_path, "local", logger)
            # Should not raise


# ── detect_project_type ──────────────────────────────────────────────


class TestDetectProjectType:
    def test_dbt_project(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: test")
        result = detect_project_type(tmp_path)
        assert isinstance(result, DbtDetector)

    def test_terraform_project(self, tmp_path):
        (tmp_path / "main.tf").write_text("resource {}")
        result = detect_project_type(tmp_path)
        assert isinstance(result, TerraformDetector)

    def test_sql_project(self, tmp_path):
        (tmp_path / "query.sql").write_text("SELECT 1")
        result = detect_project_type(tmp_path)
        assert isinstance(result, SqlFileDetector)

    def test_no_detection(self, tmp_path):
        result = detect_project_type(tmp_path)
        assert result is None

    def test_dbt_takes_priority_over_sql(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: test")
        (tmp_path / "query.sql").write_text("SELECT 1")
        result = detect_project_type(tmp_path)
        assert isinstance(result, DbtDetector)

    def test_sql_not_detected_with_dbt(self, tmp_path):
        """SqlFileDetector should not trigger when dbt_project.yml exists"""
        (tmp_path / "dbt_project.yml").write_text("name: test")
        (tmp_path / "query.sql").write_text("SELECT 1")
        detector = SqlFileDetector()
        assert detector.can_detect(tmp_path) is False


# ── DbtDetector ──────────────────────────────────────────────────────


class TestDbtDetector:
    def test_can_detect_yes(self, tmp_path):
        (tmp_path / "dbt_project.yml").write_text("name: test")
        assert DbtDetector().can_detect(tmp_path) is True

    def test_can_detect_no(self, tmp_path):
        assert DbtDetector().can_detect(tmp_path) is False

    def test_parse_model_basic(self, tmp_path, logger):
        sql_file = tmp_path / "model.sql"
        sql_file.write_text("SELECT id, name, email FROM customers")
        result = DbtDetector()._parse_model(sql_file, logger)
        assert result is not None
        assert result["name"] == "model"
        assert result["materialization"] == "view"

    def test_parse_model_with_config_table(self, tmp_path, logger):
        sql_file = tmp_path / "model.sql"
        sql_file.write_text("{{ config(materialized='table') }}\nSELECT id FROM t")
        result = DbtDetector()._parse_model(sql_file, logger)
        assert result["materialization"] == "table"

    def test_parse_model_with_config_incremental(self, tmp_path, logger):
        sql_file = tmp_path / "model.sql"
        sql_file.write_text("{{ config(materialized='incremental') }}\nSELECT id FROM t")
        result = DbtDetector()._parse_model(sql_file, logger)
        assert result["materialization"] == "incremental"

    def test_parse_model_exception(self, tmp_path, logger):
        sql_file = tmp_path / "nonexistent.sql"
        result = DbtDetector()._parse_model(sql_file, logger)
        assert result is None

    def test_detect_pii_email(self):
        models = [{"name": "users", "columns": [{"name": "email", "type": "str"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert len(findings) >= 1
        assert findings[0]["type"] == "EMAIL"

    def test_detect_pii_ssn(self):
        models = [{"name": "users", "columns": [{"name": "ssn", "type": "str"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert any(f["type"] == "SSN" for f in findings)

    def test_detect_pii_no_columns(self):
        models = [{"name": "users", "columns": []}]
        findings = DbtDetector()._detect_pii(models)
        assert findings == []

    def test_detect_pii_no_match(self):
        models = [{"name": "metrics", "columns": [{"name": "count", "type": "int"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert findings == []


# ── TerraformDetector ────────────────────────────────────────────────


class TestTerraformDetector:
    def test_can_detect_yes(self, tmp_path):
        (tmp_path / "main.tf").write_text("resource {}")
        assert TerraformDetector().can_detect(tmp_path) is True

    def test_can_detect_no(self, tmp_path):
        assert TerraformDetector().can_detect(tmp_path) is False

    @patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False)
    def test_scan_gcp(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "google_bigquery_dataset" "ds" {}')
        results = TerraformDetector().scan(tmp_path, logging.getLogger("t"))
        assert results["metadata"]["target_platform"] == "gcp"

    @patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False)
    def test_scan_snowflake(self, tmp_path):
        (tmp_path / "main.tf").write_text('resource "snowflake_database" "db" {}')
        results = TerraformDetector().scan(tmp_path, logging.getLogger("t"))
        assert results["metadata"]["target_platform"] == "snowflake"

    @patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False)
    def test_scan_unknown(self, tmp_path):
        (tmp_path / "main.tf").write_text("resource {}")
        results = TerraformDetector().scan(tmp_path, logging.getLogger("t"))
        assert "target_platform" not in results["metadata"]


# ── SqlFileDetector ──────────────────────────────────────────────────


class TestSqlFileDetector:
    def test_can_detect_yes(self, tmp_path):
        (tmp_path / "query.sql").write_text("SELECT 1")
        assert SqlFileDetector().can_detect(tmp_path) is True

    def test_can_detect_no(self, tmp_path):
        assert SqlFileDetector().can_detect(tmp_path) is False

    @patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False)
    def test_scan(self, tmp_path):
        (tmp_path / "q1.sql").write_text("SELECT 1")
        (tmp_path / "q2.sql").write_text("SELECT 2")
        results = SqlFileDetector().scan(tmp_path, logging.getLogger("t"))
        assert results["project_type"] == "sql"
        assert results["metadata"]["files_count"] == 2


# ── run ──────────────────────────────────────────────────────────────


class TestRun:
    @patch("fluid_build.cli.init.detect_mode", return_value=None)
    def test_none_mode_returns_1(self, _mock_dm, logger):
        args = SimpleNamespace()
        from fluid_build.cli.init import run

        assert run(args, logger) == 1

    # ``test_blank_dispatch`` / ``test_template_dispatch`` exercise the routing
    # logic in ``init.run`` — they assert that mode="blank" calls ``blank_mode``
    # and mode="template" calls ``template_mode``.
    #
    # Originally these were ``@pytest.mark.xfail(reason="import-cycle with the
    # copilot subsystem", strict=False)``. PR #53 removed the markers after they
    # began XPASSing on the author's local 3.13 run. CI then went red on Python
    # 3.14 (PR #55) and Python 3.11 (PR #56). The captured stdout revealed the
    # actual root cause is *not* an import cycle: ``init.run`` calls
    # ``_ensure_workspace`` before dispatch; ``_ensure_workspace`` interactively
    # prompts ``Prompt.ask("Workspace name", ...)`` when no workspace exists
    # and no ``--yes/--blank/--template`` flag is set; pytest's stdin capture
    # raises ``OSError``; the outer ``except Exception`` returns 1.
    #
    # The flake-by-Python-version was actually a flake-by-random-seed:
    # ``pytest-randomly`` reorders tests, and a prior test sometimes leaves a
    # workspace artifact at cwd that lets ``_ensure_workspace`` short-circuit.
    # When the random seed didn't put such a test first, the prompt fired and
    # the dispatch test failed.
    #
    # The right fix is to mock the side-effecting / interactive helpers so the
    # test stays focused on its actual subject (dispatch routing). After-mode
    # post-processing (``_mark_first_run_complete``, ``_write_init_receipt``,
    # ``print_next_steps``) and the ``_offer_first_forge`` interactive follow-up
    # are likewise mocked because the test asserts nothing against them.
    @patch("fluid_build.cli.init._offer_first_forge")
    @patch("fluid_build.cli.init.print_next_steps")
    @patch("fluid_build.cli.init._write_init_receipt")
    @patch("fluid_build.cli.init._mark_first_run_complete")
    @patch("fluid_build.cli.init._ensure_workspace")
    @patch("fluid_build.cli.init._ask_industry", return_value=None)
    @patch("fluid_build.cli.init.blank_mode", return_value=0)
    @patch("fluid_build.cli.init.detect_mode", return_value="blank")
    def test_blank_dispatch(
        self,
        _mock_dm,
        mock_bl,
        _mock_ind,
        _mock_ews,
        _mock_mfrc,
        _mock_wir,
        _mock_pns,
        _mock_offer,
        logger,
    ):
        from fluid_build.cli.init import run

        assert run(SimpleNamespace(), logger) == 0
        mock_bl.assert_called_once()

    @patch("fluid_build.cli.init._offer_first_forge")
    @patch("fluid_build.cli.init.print_next_steps")
    @patch("fluid_build.cli.init._write_init_receipt")
    @patch("fluid_build.cli.init._mark_first_run_complete")
    @patch("fluid_build.cli.init._ensure_workspace")
    @patch("fluid_build.cli.init._ask_industry", return_value=None)
    @patch("fluid_build.cli.init.template_mode", return_value=0)
    @patch("fluid_build.cli.init.detect_mode", return_value="template")
    def test_template_dispatch(
        self,
        _mock_dm,
        mock_tm,
        _mock_ind,
        _mock_ews,
        _mock_mfrc,
        _mock_wir,
        _mock_pns,
        _mock_offer,
        logger,
    ):
        from fluid_build.cli.init import run

        assert run(SimpleNamespace(), logger) == 0
        mock_tm.assert_called_once()

    @patch("fluid_build.cli.init.detect_mode", return_value="unknown_xyz")
    def test_unknown_mode_returns_1(self, _mock_dm, logger):
        from fluid_build.cli.init import run

        assert run(SimpleNamespace(), logger) == 1

    @patch("fluid_build.cli.init.detect_mode", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_returns_130(self, _mock_dm, logger):
        from fluid_build.cli.init import run

        assert run(SimpleNamespace(), logger) == 130

    @patch("fluid_build.cli.init.detect_mode", side_effect=RuntimeError("boom"))
    def test_exception_returns_1(self, _mock_dm, logger):
        from fluid_build.cli.init import run

        assert run(SimpleNamespace(), logger) == 1


# ── generate_dag_for_project ─────────────────────────────────────────


class TestGenerateDagForProject:
    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.init.create_dags_readme")
    @patch("fluid_build.cli.init.create_basic_dag")
    def test_subprocess_failure_creates_basic(self, mock_basic, _mock_readme, tmp_path, logger):
        from fluid_build.cli.init import generate_dag_for_project

        contract = {"name": "test-dag"}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = generate_dag_for_project(tmp_path, contract, logger, None)
        assert result is True
        mock_basic.assert_called_once()

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    @patch("fluid_build.cli.init.create_dags_readme")
    def test_subprocess_success(self, _mock_readme, tmp_path, logger):
        from fluid_build.cli.init import generate_dag_for_project

        contract = {"name": "ok-dag", "orchestration": {"schedule": "@hourly"}}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = generate_dag_for_project(tmp_path, contract, logger, None)
        assert result is True

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_exception_returns_false(self, tmp_path, logger):
        from fluid_build.cli.init import generate_dag_for_project

        with patch("subprocess.run", side_effect=OSError("no subprocess")):
            result = generate_dag_for_project(tmp_path, {}, logger, None)
        assert result is False


# ── generate_contracts_from_scan ─────────────────────────────────────


class TestGenerateContractsFromScan:
    """All assertions are against the FLUID 0.7.2 canonical shape:
    ``fluidVersion`` / ``kind: DataProduct`` / ``exposes[*]`` with per-expose
    ``binding.platform`` and nested ``contract.schema``."""

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_dbt_basic(self, logger):
        results = {
            "project_type": "dbt",
            "models": [
                {
                    "name": "users",
                    "columns": [{"name": "id", "type": "int"}],
                    "raw_sql": "SELECT id FROM users",
                }
            ],
            "metadata": {"project_name": "test-dbt"},
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        assert len(contracts) == 1
        assert contracts[0]["name"] == "test-dbt"
        assert contracts[0]["kind"] == "DataProduct"
        assert len(contracts[0]["exposes"]) == 1
        expose = contracts[0]["exposes"][0]
        assert expose["contract"]["schema"][0]["name"] == "id"

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_dbt_no_columns(self, logger):
        results = {
            "project_type": "dbt",
            "models": [{"name": "m1", "raw_sql": "SELECT *"}],
            "metadata": {},
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        assert len(contracts) == 1
        # 0.7.2 shape: empty schema is an empty list, not an absent field.
        assert contracts[0]["exposes"][0]["contract"]["schema"] == []

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_dbt_gcp_platform(self, logger):
        results = {
            "project_type": "dbt",
            "models": [{"name": "orders", "columns": []}],
            "metadata": {
                "target_platform": "gcp",
                "target_database": "my-proj",
                "target_schema": "ds",
            },
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        binding = contracts[0]["exposes"][0]["binding"]
        assert binding["platform"] == "gcp"
        assert binding["location"]["project"] == "my-proj"
        assert binding["location"]["dataset"] == "ds"

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_dbt_snowflake_platform(self, logger):
        results = {
            "project_type": "dbt",
            "models": [{"name": "orders", "columns": []}],
            "metadata": {"target_platform": "snowflake"},
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        assert contracts[0]["exposes"][0]["binding"]["platform"] == "snowflake"

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_dbt_unknown_platform_falls_to_local(self, logger):
        results = {
            "project_type": "dbt",
            "models": [{"name": "orders", "columns": []}],
            "metadata": {"target_platform": "oracle"},
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        assert contracts[0]["exposes"][0]["binding"]["platform"] == "local"

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_dbt_redshift_platform_keeps_warehouse_coordinates(self, logger):
        results = {
            "project_type": "dbt",
            "models": [{"name": "orders", "columns": []}],
            "metadata": {
                "target_platform": "redshift",
                "target_database": "analytics",
                "target_schema": "mart",
                "target_table": "orders",
            },
        }
        contracts = generate_contracts_from_scan(results, "aws", logger)
        binding = contracts[0]["exposes"][0]["binding"]
        assert binding["platform"] == "aws"
        assert binding["format"] == "other"
        assert binding["location"]["database"] == "analytics"
        assert binding["location"]["schema"] == "mart"
        assert binding["location"]["table"] == "orders"

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_terraform(self, logger):
        results = {
            "project_type": "terraform",
            "metadata": {"target_platform": "gcp"},
        }
        contracts = generate_contracts_from_scan(results, "local", logger)
        assert len(contracts) == 1
        assert contracts[0]["name"] == "terraform-import"
        assert contracts[0]["exposes"][0]["binding"]["platform"] == "gcp"

    @patch("fluid_build.cli.init.RICH_AVAILABLE", False)
    def test_sql(self, logger):
        results = {
            "project_type": "sql",
            "metadata": {},
        }
        contracts = generate_contracts_from_scan(results, "aws", logger)
        assert len(contracts) == 1
        assert contracts[0]["name"] == "sql-import"
        assert contracts[0]["exposes"][0]["binding"]["platform"] == "aws"


# ── show_migration_summary ───────────────────────────────────────────


class TestShowMigrationSummary:
    @patch("fluid_build.cli.init_scan.RICH_AVAILABLE", False)
    def test_non_rich_output(self, logger):
        contracts = [{"name": "test", "version": "0.7.1", "binding": {"provider": "local"}}]
        results = {}
        show_migration_summary(contracts, results, logger)  # Should not raise
