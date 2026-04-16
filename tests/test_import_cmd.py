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

"""Tests for fluid_build.cli.import_cmd — `fluid import` migration command.

These tests were previously part of test_init.py and test_init_extra_branches.py
under the name ``fluid init --scan``. The functionality moved verbatim into
``fluid import`` so tests were re-homed here with patched import paths.
"""

import argparse
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.schema_manager import FluidSchemaManager


def _make_args(**overrides):
    defaults = dict(
        provider="local",
        target_dir=None,
        yes=True,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def logger():
    return logging.getLogger("test_import_cmd")


# ===========================================================================
# import_cmd.run — entry point (formerly scan_mode)
# ===========================================================================


class TestImportCmdRun:
    def test_no_project_detected_returns_1(self, logger):
        from fluid_build.cli.import_cmd import run

        args = _make_args(provider="local")
        with patch("fluid_build.cli.import_cmd.detect_project_type", return_value=None):
            result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.import_cmd.show_migration_summary")
    @patch("fluid_build.cli.import_cmd.generate_contracts_from_scan")
    @patch("fluid_build.cli.import_cmd.show_scan_results")
    def test_scan_success_no_sensitive(
        self, _mock_results, mock_gen, _mock_summary, tmp_path, logger, monkeypatch
    ):
        from fluid_build.cli.import_cmd import run

        monkeypatch.chdir(tmp_path)
        args = _make_args(provider="local")
        mock_detector = MagicMock()
        mock_detector.scan.return_value = {
            "project_type": "dbt",
            "metadata": {},
            "models": [],
            "sensitive_columns": [],
        }
        mock_gen.return_value = [
            {"name": "c1", "version": FluidSchemaManager.latest_bundled_version()}
        ]

        with patch("fluid_build.cli.import_cmd.detect_project_type", return_value=mock_detector):
            with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
                result = run(args, logger)
        assert result == 0

    @patch(
        "fluid_build.cli.import_cmd.detect_project_type", side_effect=RuntimeError("scan boom")
    )
    def test_exception_returns_1(self, _mock_detect, logger):
        from fluid_build.cli.import_cmd import run

        args = _make_args()
        result = run(args, logger)
        assert result == 1

    def test_scan_zero_model_dbt_fails_without_writing_contract(
        self, tmp_path, logger, monkeypatch
    ):
        from fluid_build.cli.import_cmd import run

        monkeypatch.chdir(tmp_path)
        args = _make_args(provider="local")
        mock_detector = MagicMock()
        mock_detector.scan.return_value = {
            "project_type": "dbt",
            "metadata": {"project_name": "empty-dbt", "target_platform": "duckdb"},
            "models": [],
            "sensitive_columns": [],
        }

        with patch(
            "fluid_build.cli.import_cmd.detect_project_type", return_value=mock_detector
        ):
            with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
                result = run(args, logger)

        assert result == 1
        assert list(tmp_path.glob("*.fluid.yaml")) == []

    @patch("fluid_build.cli.import_cmd.show_migration_summary")
    @patch("fluid_build.cli.import_cmd.apply_governance_policies")
    @patch("fluid_build.cli.import_cmd.generate_contracts_from_scan")
    @patch("fluid_build.cli.import_cmd.show_scan_results")
    def test_scan_with_sensitive_columns_calls_governance(
        self,
        _mock_show,
        mock_gen,
        mock_governance,
        _mock_summary,
        tmp_path,
        logger,
        monkeypatch,
    ):
        from fluid_build.cli.import_cmd import run

        monkeypatch.chdir(tmp_path)
        args = _make_args(provider="local")
        mock_detector = MagicMock()
        mock_detector.scan.return_value = {
            "project_type": "sql",
            "metadata": {},
            "files": [],
            "sensitive_columns": [{"col": "email", "type": "EMAIL"}],
        }
        mock_gen.return_value = [{"name": "sql-import"}]
        mock_governance.return_value = [{"name": "sql-import"}]

        with patch(
            "fluid_build.cli.import_cmd.detect_project_type", return_value=mock_detector
        ):
            with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
                result = run(args, logger)
        assert result == 0
        mock_governance.assert_called_once()

    def test_bad_target_dir_returns_1(self, tmp_path, logger):
        """``--dir`` pointing to a non-existent path should fail fast."""
        from fluid_build.cli.import_cmd import run

        args = _make_args(target_dir=str(tmp_path / "does-not-exist"))
        result = run(args, logger)
        assert result == 1


# ===========================================================================
# show_scan_results
# ===========================================================================


class TestShowScanResults:
    def test_no_rich_prints_project_type(self):
        from fluid_build.cli.import_cmd import show_scan_results

        results = {"project_type": "dbt", "metadata": {}, "sensitive_columns": []}
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
            with patch("fluid_build.cli.import_cmd.cprint") as mock_cprint:
                show_scan_results(results)
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        assert "dbt" in calls

    def test_rich_dbt_type_with_metadata(self):
        from fluid_build.cli.import_cmd import show_scan_results

        results = {
            "project_type": "dbt",
            "metadata": {
                "project_name": "myproj",
                "target_platform": "gcp",
                "target_database": "",
            },
            "models": [{"name": "m1"}, {"name": "m2"}],
            "sensitive_columns": [],
        }
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.import_cmd.console") as mock_con:
                mock_con.print = MagicMock()
                show_scan_results(results)
        mock_con.print.assert_called()

    def test_rich_terraform_type(self):
        from fluid_build.cli.import_cmd import show_scan_results

        results = {
            "project_type": "terraform",
            "metadata": {"files_count": 3, "target_platform": "aws"},
            "sensitive_columns": [],
        }
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.import_cmd.console") as mock_con:
                mock_con.print = MagicMock()
                show_scan_results(results)
        mock_con.print.assert_called()

    def test_rich_sql_type(self):
        from fluid_build.cli.import_cmd import show_scan_results

        results = {
            "project_type": "sql",
            "metadata": {"files_count": 5},
            "sensitive_columns": [],
        }
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.import_cmd.console") as mock_con:
                mock_con.print = MagicMock()
                show_scan_results(results)
        mock_con.print.assert_called()

    def test_sensitive_columns_rendered_in_table(self):
        from fluid_build.cli.import_cmd import show_scan_results

        results = {
            "project_type": "dbt",
            "metadata": {},
            "models": [],
            "sensitive_columns": [
                {
                    "model": "users",
                    "column": "email",
                    "type": "EMAIL",
                    "confidence": 0.85,
                    "method": "heuristic",
                },
                {
                    "model": "orders",
                    "column": "ssn",
                    "type": "SSN",
                    "confidence": 0.95,
                    "method": "heuristic",
                },
            ],
        }
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.import_cmd.console") as mock_con:
                with patch("fluid_build.cli.import_cmd.Table") as mock_table_cls:
                    mock_table = MagicMock()
                    mock_table_cls.return_value = mock_table
                    mock_con.print = MagicMock()
                    show_scan_results(results)
        mock_table.add_row.assert_called()

    def test_eu_database_shows_gdpr_hint(self):
        from fluid_build.cli.import_cmd import show_scan_results

        results = {
            "project_type": "dbt",
            "metadata": {
                "project_name": "eu_proj",
                "target_platform": "gcp",
                "target_database": "eu-west-db",
            },
            "models": [],
            "sensitive_columns": [],
        }
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.import_cmd.console") as mock_con:
                mock_con.print = MagicMock()
                show_scan_results(results)
        calls = " ".join(str(c) for c in mock_con.print.call_args_list)
        assert "EU" in calls or "GDPR" in calls

    def test_many_sensitive_columns_truncated(self):
        from fluid_build.cli.import_cmd import show_scan_results

        sensitive = [
            {
                "model": "m",
                "column": f"col_{i}",
                "type": "EMAIL",
                "confidence": 0.8,
                "method": "h",
            }
            for i in range(15)
        ]
        results = {
            "project_type": "dbt",
            "metadata": {},
            "models": [],
            "sensitive_columns": sensitive,
        }
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.import_cmd.console") as mock_con:
                with patch("fluid_build.cli.import_cmd.Table") as mock_table_cls:
                    mock_table = MagicMock()
                    mock_table_cls.return_value = mock_table
                    mock_con.print = MagicMock()
                    show_scan_results(results)
        # Only 10 rows shown
        assert mock_table.add_row.call_count == 10


# ===========================================================================
# detect_project_type
# ===========================================================================


class TestDetectProjectType:
    def test_detects_dbt(self, tmp_path):
        from fluid_build.cli.import_cmd import DbtDetector, detect_project_type

        (tmp_path / "dbt_project.yml").write_text("name: myproject\n")
        detector = detect_project_type(tmp_path)
        assert isinstance(detector, DbtDetector)

    def test_detects_terraform(self, tmp_path):
        from fluid_build.cli.import_cmd import TerraformDetector, detect_project_type

        (tmp_path / "main.tf").write_text("resource {}")
        detector = detect_project_type(tmp_path)
        assert isinstance(detector, TerraformDetector)

    def test_detects_sql(self, tmp_path):
        from fluid_build.cli.import_cmd import SqlFileDetector, detect_project_type

        (tmp_path / "query.sql").write_text("SELECT 1")
        detector = detect_project_type(tmp_path)
        assert isinstance(detector, SqlFileDetector)

    def test_returns_none_when_nothing_found(self, tmp_path):
        from fluid_build.cli.import_cmd import detect_project_type

        detector = detect_project_type(tmp_path)
        assert detector is None

    def test_dbt_takes_priority_over_sql(self, tmp_path):
        from fluid_build.cli.import_cmd import DbtDetector, detect_project_type

        (tmp_path / "dbt_project.yml").write_text("name: x\n")
        (tmp_path / "model.sql").write_text("SELECT 1")
        detector = detect_project_type(tmp_path)
        assert isinstance(detector, DbtDetector)


# ===========================================================================
# DbtDetector
# ===========================================================================


class TestDbtDetector:
    def test_can_detect_true(self, tmp_path):
        from fluid_build.cli.import_cmd import DbtDetector

        (tmp_path / "dbt_project.yml").write_text("name: x\n")
        assert DbtDetector().can_detect(tmp_path) is True

    def test_can_detect_false(self, tmp_path):
        from fluid_build.cli.import_cmd import DbtDetector

        assert DbtDetector().can_detect(tmp_path) is False

    def test_parse_model_extracts_columns(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import DbtDetector

        sql_file = tmp_path / "orders.sql"
        sql_file.write_text("SELECT id, name, amount FROM raw.orders")
        model = DbtDetector()._parse_model(sql_file, logger)
        assert model is not None
        assert model["name"] == "orders"
        assert any(c["name"] == "amount" for c in model["columns"])

    def test_parse_model_with_table_materialization(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import DbtDetector

        sql_file = tmp_path / "facts.sql"
        sql_file.write_text("{{ config(materialized='table') }}\nSELECT id FROM raw.facts")
        model = DbtDetector()._parse_model(sql_file, logger)
        assert model["materialization"] == "table"

    def test_parse_model_with_incremental_materialization(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import DbtDetector

        sql_file = tmp_path / "inc.sql"
        sql_file.write_text("{{ config(materialized='incremental') }}\nSELECT id FROM t")
        model = DbtDetector()._parse_model(sql_file, logger)
        assert model["materialization"] == "incremental"

    def test_parse_model_returns_none_on_missing_file(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import DbtDetector

        non_existent = tmp_path / "nope.sql"
        model = DbtDetector()._parse_model(non_existent, logger)
        assert model is None

    def test_detect_pii_finds_email(self):
        from fluid_build.cli.import_cmd import DbtDetector

        models = [{"name": "users", "columns": [{"name": "email_address"}, {"name": "user_id"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert any(f["type"] == "EMAIL" for f in findings)

    def test_detect_pii_finds_phone(self):
        from fluid_build.cli.import_cmd import DbtDetector

        models = [{"name": "contacts", "columns": [{"name": "phone_number"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert any(f["type"] == "PHONE" for f in findings)

    def test_detect_pii_finds_credit_card(self):
        from fluid_build.cli.import_cmd import DbtDetector

        models = [{"name": "payments", "columns": [{"name": "credit_card_num"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert any(f["type"] == "CREDIT_CARD" for f in findings)

    def test_detect_pii_finds_ssn(self):
        from fluid_build.cli.import_cmd import DbtDetector

        models = [{"name": "hr", "columns": [{"name": "social_security_number"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert any(f["type"] == "SSN" for f in findings)

    def test_detect_pii_finds_name(self):
        from fluid_build.cli.import_cmd import DbtDetector

        models = [{"name": "people", "columns": [{"name": "first_name"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert any(f["type"] == "NAME" for f in findings)

    def test_detect_pii_no_pii(self):
        from fluid_build.cli.import_cmd import DbtDetector

        models = [{"name": "metrics", "columns": [{"name": "revenue"}, {"name": "count"}]}]
        findings = DbtDetector()._detect_pii(models)
        assert findings == []

    def test_scan_parses_project_name(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import DbtDetector

        (tmp_path / "dbt_project.yml").write_text("name: analytics\nversion: 1.0.0\n")
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
            results = DbtDetector().scan(tmp_path, logger)
        assert results["project_type"] == "dbt"
        assert results["metadata"]["project_name"] == "analytics"


# ===========================================================================
# TerraformDetector
# ===========================================================================


class TestTerraformDetector:
    def test_can_detect_true(self, tmp_path):
        from fluid_build.cli.import_cmd import TerraformDetector

        (tmp_path / "main.tf").write_text("resource {}")
        assert TerraformDetector().can_detect(tmp_path) is True

    def test_can_detect_false(self, tmp_path):
        from fluid_build.cli.import_cmd import TerraformDetector

        assert TerraformDetector().can_detect(tmp_path) is False

    def test_scan_detects_gcp(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import TerraformDetector

        (tmp_path / "main.tf").write_text('resource "google_bigquery_dataset" "ds" {}')
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
            results = TerraformDetector().scan(tmp_path, logger)
        assert results["metadata"].get("target_platform") == "gcp"

    def test_scan_detects_snowflake(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import TerraformDetector

        (tmp_path / "main.tf").write_text('resource "snowflake_database" "db" {}')
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
            results = TerraformDetector().scan(tmp_path, logger)
        assert results["metadata"].get("target_platform") == "snowflake"

    def test_scan_returns_files_count(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import TerraformDetector

        (tmp_path / "main.tf").write_text("resource {}")
        (tmp_path / "variables.tf").write_text("variable x {}")
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
            results = TerraformDetector().scan(tmp_path, logger)
        assert results["metadata"]["files_count"] == 2


# ===========================================================================
# SqlFileDetector
# ===========================================================================


class TestSqlFileDetector:
    def test_can_detect_true(self, tmp_path):
        from fluid_build.cli.import_cmd import SqlFileDetector

        (tmp_path / "query.sql").write_text("SELECT 1")
        assert SqlFileDetector().can_detect(tmp_path) is True

    def test_can_detect_false_no_sql(self, tmp_path):
        from fluid_build.cli.import_cmd import SqlFileDetector

        assert SqlFileDetector().can_detect(tmp_path) is False

    def test_can_detect_false_when_dbt_exists(self, tmp_path):
        from fluid_build.cli.import_cmd import SqlFileDetector

        (tmp_path / "query.sql").write_text("SELECT 1")
        (tmp_path / "dbt_project.yml").write_text("name: x\n")
        assert SqlFileDetector().can_detect(tmp_path) is False

    def test_scan_lists_files(self, tmp_path, logger):
        from fluid_build.cli.import_cmd import SqlFileDetector

        (tmp_path / "a.sql").write_text("SELECT 1")
        (tmp_path / "b.sql").write_text("SELECT 2")
        with patch("fluid_build.cli.import_cmd.RICH_AVAILABLE", False):
            results = SqlFileDetector().scan(tmp_path, logger)
        assert results["project_type"] == "sql"
        assert results["metadata"]["files_count"] == 2


# ===========================================================================
# _safe_yaml_load — file-size cap
# ===========================================================================


class TestSafeYamlLoad:
    def test_loads_small_file(self, tmp_path):
        from fluid_build.cli.import_cmd import _safe_yaml_load

        f = tmp_path / "small.yml"
        f.write_text("name: ok\nversion: 1.0\n")
        result = _safe_yaml_load(f)
        assert result == {"name": "ok", "version": 1.0}

    def test_refuses_oversize_file(self, tmp_path):
        from fluid_build.cli.import_cmd import _safe_yaml_load

        f = tmp_path / "huge.yml"
        f.write_text("name: " + "x" * 200)  # ~200 bytes
        with pytest.raises(ValueError, match="exceeds"):
            _safe_yaml_load(f, max_bytes=100)

    def test_raises_value_error_for_missing_file(self, tmp_path):
        from fluid_build.cli.import_cmd import _safe_yaml_load

        with pytest.raises(ValueError, match="Cannot stat"):
            _safe_yaml_load(tmp_path / "nope.yml")

    def test_empty_file_returns_none(self, tmp_path):
        from fluid_build.cli.import_cmd import _safe_yaml_load

        f = tmp_path / "empty.yml"
        f.write_text("")
        assert _safe_yaml_load(f) is None


# ===========================================================================
# _safe_contract_filename — path-traversal guard
# ===========================================================================


class TestSafeContractFilename:
    def test_strips_parent_directory_components(self):
        from fluid_build.cli.import_cmd import _safe_contract_filename

        # A malicious dbt model named ../../evil must not be able to
        # escape the output directory.
        result = _safe_contract_filename("../../evil", 0)
        assert ".." not in result
        assert "/" not in result

    def test_normal_name_slugified(self):
        from fluid_build.cli.import_cmd import _safe_contract_filename

        result = _safe_contract_filename("customer_analytics", 0)
        assert result  # non-empty
        assert "/" not in result
        assert ".." not in result

    def test_empty_name_falls_back_to_index(self):
        from fluid_build.cli.import_cmd import _safe_contract_filename

        result = _safe_contract_filename("", 3)
        assert result == "contract-3"

    def test_absolute_path_stripped(self):
        from fluid_build.cli.import_cmd import _safe_contract_filename

        result = _safe_contract_filename("/etc/passwd", 0)
        assert "/" not in result
        assert "etc" not in result or result == "passwd"  # Path().name → "passwd", then slugified


# ===========================================================================
# register() — argparse wiring
# ===========================================================================


class TestRegister:
    def test_register_adds_import_subcommand(self):
        from fluid_build.cli.import_cmd import register

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)

        # Parse a known set of args to confirm the command is registered.
        args = parser.parse_args(["import", "--provider", "snowflake", "--yes"])
        assert args.cmd == "import"
        assert args.provider == "snowflake"
        assert args.yes is True
