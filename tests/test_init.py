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

"""Comprehensive tests for fluid_build.cli.init — mode handlers, helpers, scanners."""

import argparse
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.schema_manager import FluidSchemaManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**overrides):
    defaults = dict(
        name=None,
        quickstart=False,
        blank=False,
        template=None,
        provider="local",
        use_case=None,
        no_run=False,
        no_dag=False,
        dry_run=False,
        yes=True,
        target_dir=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.fixture
def logger():
    return logging.getLogger("test_init")


# ===========================================================================
# demo_mode
# ===========================================================================


class TestDemoMode:
    @patch("fluid_build.cli.init.show_success_message")
    @patch("fluid_build.cli.init.run_local_pipeline")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_happy_path(
        self,
        mock_copy,
        _mock_data,
        _mock_db,
        _mock_run,
        _mock_success,
        tmp_path,
        logger,
        monkeypatch,
    ):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="qs-project", no_run=True, no_dag=True)
        result = demo_mode(args, logger)
        assert result == 0
        mock_copy.assert_called_once()

    @patch("fluid_build.cli.init.copy_template", return_value=False)
    def test_copy_template_fails_returns_1(self, _mock_copy, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="qs-fail", no_run=True, no_dag=True)
        result = demo_mode(args, logger)
        assert result == 1

    def test_dry_run_returns_0(self, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="qs-dry", dry_run=True)
        result = demo_mode(args, logger)
        assert result == 0

    def test_existing_nonempty_dir_returns_1(self, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import demo_mode

        # demo_mode resolves the project dir relative to the current
        # working directory after slugifying args.name, so the existing dir
        # needs to live there with the matching slug name.
        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "existing-project"
        existing.mkdir()
        (existing / "some_file.txt").write_text("content")
        args = _make_args(name="existing-project")
        result = demo_mode(args, logger)
        assert result == 1

    @patch("fluid_build.cli.init.show_success_message")
    @patch("fluid_build.cli.init.run_local_pipeline")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_auto_name_my_first_product(
        self,
        _mock_copy,
        _mock_data,
        _mock_db,
        _mock_run,
        _mock_success,
        tmp_path,
        logger,
        monkeypatch,
    ):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(no_run=True, no_dag=True)
        result = demo_mode(args, logger)
        assert result == 0

    @patch("fluid_build.cli.init.show_success_message")
    @patch("fluid_build.cli.init.run_local_pipeline")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_pipeline_runs_when_no_run_false(
        self,
        _mock_copy,
        _mock_data,
        _mock_db,
        mock_run_pipeline,
        _mock_success,
        tmp_path,
        logger,
        monkeypatch,
    ):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="run-test", no_run=False, no_dag=True)
        demo_mode(args, logger)
        mock_run_pipeline.assert_called_once()

    @patch("fluid_build.cli.init.show_success_message")
    @patch("fluid_build.cli.init.run_local_pipeline")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_no_run_skips_pipeline(
        self,
        _mock_copy,
        _mock_data,
        _mock_db,
        mock_run_pipeline,
        _mock_success,
        tmp_path,
        logger,
        monkeypatch,
    ):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="no-run-test", no_run=True, no_dag=True)
        demo_mode(args, logger)
        mock_run_pipeline.assert_not_called()

    @patch("fluid_build.cli.init.copy_template", side_effect=RuntimeError("boom"))
    def test_exception_returns_1(self, _mock_copy, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="qs-exc", no_run=True, no_dag=True)
        result = demo_mode(args, logger)
        assert result == 1

    @patch("fluid_build.cli.init.show_success_message")
    @patch("fluid_build.cli.init.run_local_pipeline")
    @patch("fluid_build.cli.init.init_local_db")
    @patch("fluid_build.cli.init.copy_sample_data")
    @patch("fluid_build.cli.init.generate_dag_for_project", return_value=True)
    @patch("fluid_build.cli.init.should_generate_dag", return_value=True)
    def test_dag_generated_when_contract_exists(
        self,
        _mock_should,
        _mock_dag,
        _mock_data,
        _mock_db,
        _mock_run,
        _mock_success,
        tmp_path,
        logger,
        monkeypatch,
    ):
        from fluid_build.cli.init import demo_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="dag-project", no_run=True, no_dag=False)

        def _create_contract(project_dir, template, lgr):
            project_dir.mkdir(parents=True, exist_ok=True)
            (project_dir / "contract.fluid.yaml").write_text("name: test\n")
            return True

        with patch("fluid_build.cli.init.copy_template", side_effect=_create_contract):
            with patch("yaml.safe_load", return_value={"name": "test", "orchestration": {}}):
                result = demo_mode(args, logger)
        assert result == 0


# ===========================================================================
# blank_mode
# ===========================================================================


class TestBlankMode:
    """Slice UX-F rewrite: blank_mode goes directly through
    build_minimal_contract + write_contract + ReceiptBuilder.  It no
    longer delegates to product_new_run.  See slice UX-F in the plan
    file for the rationale (unify init --blank with forge --blank)."""

    def test_non_empty_existing_directory_returns_1(self, tmp_path, logger, monkeypatch):
        """A pre-existing non-empty dir blocks the scaffold with exit 1."""
        from fluid_build.cli.init import blank_mode

        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "blank-existing"
        existing.mkdir()
        (existing / "some-existing-file.txt").write_text("hands off")
        args = _make_args(name="blank-existing")
        result = blank_mode(args, logger)
        assert result == 1

    def test_empty_existing_directory_is_accepted(self, tmp_path, logger, monkeypatch):
        """An empty stub dir is fine — blank_mode populates it."""
        from fluid_build.cli.init import blank_mode

        monkeypatch.chdir(tmp_path)
        existing = tmp_path / "blank-empty"
        existing.mkdir()
        args = _make_args(name="blank-empty", dry_run=False)
        result = blank_mode(args, logger)
        assert result == 0
        assert (existing / "contract.fluid.yaml").exists()

    def test_creates_v072_yaml_contract_via_build_minimal_contract(
        self, tmp_path, logger, monkeypatch
    ):
        """blank_mode writes a v0.7.2 YAML contract through
        build_minimal_contract + write_contract.  The result has
        metadata.provenance and the new shape (top-level domain,
        metadata.layer: Bronze, SQL embedded-logic build)."""
        import yaml as _yaml

        from fluid_build.cli.init import blank_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="blank-new-project", provider="local", dry_run=False)
        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = blank_mode(args, logger)
        assert result == 0

        contract = tmp_path / "blank-new-project" / "contract.fluid.yaml"
        assert contract.exists()
        doc = _yaml.safe_load(contract.read_text())
        # New v0.7.2 shape
        assert doc["id"] == "blank-new-project"
        assert doc["domain"] == "analytics"  # top-level, not metadata.domain
        assert doc["metadata"]["layer"] == "Bronze"
        assert "provenance" in doc["metadata"]
        assert doc["builds"][0]["pattern"] == "embedded-logic"

    def test_writes_forge_receipt_inside_product(self, tmp_path, logger, monkeypatch):
        """Slice UX-F: blank_mode also writes .fluid/forge-receipt.json
        inside the new product so fluid status finds it."""
        from fluid_build.cli.artifact_paths import product_forge_receipt_path
        from fluid_build.cli.init import blank_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="blank-receipt-check", provider="local", dry_run=False)
        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            blank_mode(args, logger)

        receipt = product_forge_receipt_path(tmp_path / "blank-receipt-check")
        assert receipt.is_file()

    def test_does_not_delegate_to_product_new_run(self, tmp_path, logger, monkeypatch):
        """Regression guard against the old delegation path: slice UX-F
        removed the call to product_new.run entirely."""
        from fluid_build.cli.init import blank_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(name="blank-no-delegate", provider="local", dry_run=False)
        mock_run = MagicMock(return_value=0)
        mock_mod = MagicMock()
        mock_mod.run = mock_run
        monkeypatch.delitem(sys.modules, "fluid_build.cli.product_new", raising=False)
        with patch.dict("sys.modules", {"fluid_build.cli.product_new": mock_mod}):
            result = blank_mode(args, logger)
        assert result == 0
        # product_new.run MUST NOT be called — UX-F removed that path
        mock_run.assert_not_called()

    def test_default_name_my_project(self, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import blank_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(provider="local", dry_run=False)
        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = blank_mode(args, logger)
        assert result == 0
        assert (tmp_path / "my-project" / "contract.fluid.yaml").exists()


# ===========================================================================
# template_mode
# ===========================================================================


class TestTemplateMode:
    @patch("fluid_build.cli.init.copy_template", return_value=True)
    def test_copy_template_success(self, _mock_copy, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import template_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(template="customer-360", name="my-c360", provider="local")
        result = template_mode(args, logger)
        assert result == 0

    @patch("fluid_build.cli.init.copy_template", return_value=False)
    def test_copy_template_failure(self, _mock_copy, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import template_mode

        monkeypatch.chdir(tmp_path)
        args = _make_args(template="bad-tmpl", name="proj", provider="local")
        result = template_mode(args, logger)
        assert result == 1


# ===========================================================================
# copy_template
# ===========================================================================


class TestCopyTemplate:
    def test_missing_template_returns_false(self, tmp_path, logger):
        from fluid_build.cli.init import copy_template

        result = copy_template(tmp_path, "nonexistent-template-xyz-999", logger)
        assert result is False

    def test_copies_files_from_template(self, tmp_path, logger):
        import shutil

        from fluid_build.cli.init import copy_template

        cli_dir = Path(__file__).parent.parent / "fluid_build" / "cli"
        templates_dir = cli_dir.parent / "templates" / "test-tmpl-pytest"
        templates_dir.mkdir(parents=True, exist_ok=True)
        (templates_dir / "contract.fluid.yaml").write_text("name: test-template\n")

        project_dir = tmp_path / "output"
        project_dir.mkdir()

        try:
            result = copy_template(project_dir, "test-tmpl-pytest", logger)
            assert result is True
            assert (project_dir / "contract.fluid.yaml").exists()
        finally:
            shutil.rmtree(str(templates_dir))

    def test_copies_subdirectory(self, tmp_path, logger):
        import shutil

        from fluid_build.cli.init import copy_template

        cli_dir = Path(__file__).parent.parent / "fluid_build" / "cli"
        templates_dir = cli_dir.parent / "templates" / "test-tmpl-subdir"
        templates_dir.mkdir(parents=True, exist_ok=True)
        sub = templates_dir / "data"
        sub.mkdir()
        (sub / "sample.csv").write_text("a,b\n1,2\n")

        project_dir = tmp_path / "output2"
        project_dir.mkdir()

        try:
            result = copy_template(project_dir, "test-tmpl-subdir", logger)
            assert result is True
            assert (project_dir / "data" / "sample.csv").exists()
        finally:
            shutil.rmtree(str(templates_dir))

    def test_copy_exception_returns_false(self, tmp_path, logger):
        import shutil

        from fluid_build.cli.init import copy_template

        cli_dir = Path(__file__).parent.parent / "fluid_build" / "cli"
        templates_dir = cli_dir.parent / "templates" / "test-tmpl-perm"
        templates_dir.mkdir(parents=True, exist_ok=True)
        (templates_dir / "contract.fluid.yaml").write_text("name: x\n")

        project_dir = tmp_path / "output3"
        project_dir.mkdir()

        try:
            with patch("shutil.copy2", side_effect=PermissionError("no write")):
                result = copy_template(project_dir, "test-tmpl-perm", logger)
            assert result is False
        finally:
            shutil.rmtree(str(templates_dir))


# ===========================================================================
# run_local_pipeline
# ===========================================================================


class TestRunLocalPipeline:
    def test_no_rich_exits_early(self, tmp_path, logger):
        from fluid_build.cli.init import run_local_pipeline

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            result = run_local_pipeline(tmp_path, logger)
        assert result is None

    def test_apply_run_called_on_success(self, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import run_local_pipeline

        (tmp_path / "contract.fluid.yaml").write_text("name: test\n")
        mock_apply = MagicMock(return_value=0)
        mock_apply_mod = MagicMock()
        mock_apply_mod.run = mock_apply
        monkeypatch.delitem(sys.modules, "fluid_build.cli.apply", raising=False)
        with patch.dict("sys.modules", {"fluid_build.cli.apply": mock_apply_mod}):
            with patch("fluid_build.cli.init.RICH_AVAILABLE", True):
                with patch("fluid_build.cli.init.console") as mock_con:
                    mock_con.print = MagicMock()
                    run_local_pipeline(tmp_path, logger)
        mock_apply.assert_called_once()

    def test_exception_handled_gracefully(self, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import run_local_pipeline

        mock_apply_mod = MagicMock()
        mock_apply_mod.run.side_effect = RuntimeError("apply failed")
        monkeypatch.delitem(sys.modules, "fluid_build.cli.apply", raising=False)
        with patch.dict("sys.modules", {"fluid_build.cli.apply": mock_apply_mod}):
            with patch("fluid_build.cli.init.RICH_AVAILABLE", True):
                with patch("fluid_build.cli.init.console"):
                    # Should not raise
                    run_local_pipeline(tmp_path, logger)


# ===========================================================================
# show_success_message
# ===========================================================================


class TestShowSuccessMessage:
    def test_no_rich_prints_basic(self, tmp_path, logger):
        from fluid_build.cli.init import show_success_message

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            with patch("fluid_build.cli.init._mark_first_run_complete"):
                with patch("fluid_build.cli.init.cprint") as mock_cprint:
                    show_success_message(tmp_path, "local", logger)
        mock_cprint.assert_called()

    def test_no_rich_with_dag_prints_dag_message(self, tmp_path, logger):
        from fluid_build.cli.init import show_success_message

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            with patch("fluid_build.cli.init._mark_first_run_complete"):
                with patch("fluid_build.cli.init.cprint") as mock_cprint:
                    show_success_message(tmp_path, "local", logger, has_dag=True)
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        assert "DAG" in calls

    def test_rich_local_provider_with_output_files(self, tmp_path, logger):
        from fluid_build.cli.init import show_success_message

        (tmp_path / "output").mkdir()
        (tmp_path / "output" / "results.csv").write_text("a,b\n1,2\n")
        (tmp_path / ".fluid").mkdir()
        (tmp_path / ".fluid" / "db.duckdb").write_text("")

        with patch("fluid_build.cli.init.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init._mark_first_run_complete"):
                with patch("fluid_build.cli.init.console") as mock_con:
                    mock_con.print = MagicMock()
                    show_success_message(tmp_path, "local", logger)
        mock_con.print.assert_called()

    def test_rich_cloud_provider_shows_plan_step(self, tmp_path, logger):
        from fluid_build.cli.init import show_success_message

        with patch("fluid_build.cli.init.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init._mark_first_run_complete"):
                with patch("fluid_build.cli.init.console") as mock_con:
                    mock_con.print = MagicMock()
                    show_success_message(tmp_path, "gcp", logger)
        calls = " ".join(str(c) for c in mock_con.print.call_args_list)
        assert "gcp" in calls

    def test_rich_with_dag_shows_dag_files(self, tmp_path, logger):
        from fluid_build.cli.init import show_success_message

        dag_dir = tmp_path / "dags"
        dag_dir.mkdir()
        (dag_dir / "my_product_dag.py").write_text("# dag")

        with patch("fluid_build.cli.init.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init._mark_first_run_complete"):
                with patch("fluid_build.cli.init.console") as mock_con:
                    mock_con.print = MagicMock()
                    show_success_message(tmp_path, "local", logger, has_dag=True)
        calls = " ".join(str(c) for c in mock_con.print.call_args_list)
        assert "my_product_dag.py" in calls


# ===========================================================================
# copy_sample_data
# ===========================================================================


class TestCopySampleData:
    def test_no_data_dir_does_not_raise(self, tmp_path, logger):
        from fluid_build.cli.init import copy_sample_data

        copy_sample_data(tmp_path, "customer-360", logger)

    def test_with_csv_files_prints_count(self, tmp_path, logger):
        from fluid_build.cli.init import copy_sample_data

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "customers.csv").write_text("id,name\n1,Alice\n")
        (data_dir / "orders.csv").write_text("id,amount\n1,100\n")

        with patch("fluid_build.cli.init.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init.console") as mock_con:
                mock_con.print = MagicMock()
                copy_sample_data(tmp_path, "customer-360", logger)
        mock_con.print.assert_called()


# ===========================================================================
# init_local_db
# ===========================================================================


class TestInitLocalDb:
    def test_skips_non_local_provider(self, tmp_path, logger):
        from fluid_build.cli.init import init_local_db

        init_local_db(tmp_path, "gcp", logger)
        assert not (tmp_path / ".fluid").exists()

    def test_duckdb_not_installed_no_raise(self, tmp_path, logger):
        from fluid_build.cli.init import init_local_db

        with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
            with patch.dict("sys.modules", {"duckdb": None}):
                init_local_db(tmp_path, "local", logger)

    def test_duckdb_available_creates_db_dir(self, tmp_path, logger, monkeypatch):
        from fluid_build.cli.init import init_local_db

        mock_conn = MagicMock()
        mock_duckdb = MagicMock()
        mock_duckdb.connect.return_value = mock_conn
        monkeypatch.delitem(sys.modules, "duckdb", raising=False)
        with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
            with patch("fluid_build.cli.init.RICH_AVAILABLE", False):
                init_local_db(tmp_path, "local", logger)
        mock_duckdb.connect.assert_called_once()
        mock_conn.close.assert_called_once()


# ===========================================================================
# apply_governance_policies
# ===========================================================================


class TestApplyGovernancePolicies:
    def test_no_rich_returns_contracts_unchanged(self, logger):
        from fluid_build.cli.init_scan import apply_governance_policies

        contracts = [{"name": "c1"}]
        results = {"sensitive_columns": [{"col": "email"}]}
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", False):
            out = apply_governance_policies(contracts, results, logger)
        assert out == contracts

    def test_no_sensitive_returns_unchanged(self, logger):
        from fluid_build.cli.init_scan import apply_governance_policies

        contracts = [{"name": "c1"}]
        results = {"sensitive_columns": []}
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            out = apply_governance_policies(contracts, results, logger)
        assert out == contracts

    def test_applies_masking_rules_when_user_confirms(self, logger):
        from fluid_build.cli.init_scan import apply_governance_policies

        # 0.7.2 shape: ``exposes[*]`` with ``exposeId``.
        contracts = [
            {
                "name": "c1",
                "exposes": [{"exposeId": "users", "contract": {"schema": []}}],
            }
        ]
        results = {
            "sensitive_columns": [
                {
                    "model": "users",
                    "column": "email",
                    "type": "EMAIL",
                    "confidence": 0.85,
                }
            ],
            "metadata": {"target_database": ""},
        }
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init_scan.console") as mock_con:
                mock_con.print = MagicMock()
                with patch("fluid_build.cli.init_scan.Confirm") as mock_confirm:
                    mock_confirm.ask.return_value = True
                    out = apply_governance_policies(contracts, results, logger)
        assert "policy" in out[0]["exposes"][0]
        masking = out[0]["exposes"][0]["policy"]["masking"]
        assert masking[0]["column"] == "email"

    def test_user_declines_governance_unchanged(self, logger):
        from fluid_build.cli.init_scan import apply_governance_policies

        contracts = [{"name": "c1", "exposes": [{"exposeId": "users"}]}]
        results = {
            "sensitive_columns": [
                {"model": "users", "column": "email", "type": "EMAIL", "confidence": 0.85}
            ],
            "metadata": {},
        }
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init_scan.console"):
                with patch("fluid_build.cli.init_scan.Confirm") as mock_confirm:
                    mock_confirm.ask.return_value = False
                    out = apply_governance_policies(contracts, results, logger)
        assert out == contracts

    def test_high_confidence_uses_sha256(self, logger):
        from fluid_build.cli.init_scan import apply_governance_policies

        contracts = [{"name": "c1", "exposes": [{"exposeId": "payments"}]}]
        results = {
            "sensitive_columns": [
                {
                    "model": "payments",
                    "column": "cc_number",
                    "type": "CREDIT_CARD",
                    "confidence": 0.95,
                }
            ],
            "metadata": {},
        }
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init_scan.console") as mock_con:
                mock_con.print = MagicMock()
                with patch("fluid_build.cli.init_scan.Confirm") as mock_confirm:
                    mock_confirm.ask.return_value = True
                    out = apply_governance_policies(contracts, results, logger)
        masking = out[0]["exposes"][0]["policy"]["masking"]
        assert masking[0]["method"] == "SHA256"

    def test_logs_warning_for_legacy_produces_only_contract(self):
        """Regression guard: callers that still emit the legacy ``produces[]``
        shape must get a loud warning rather than a silent governance skip."""
        from fluid_build.cli.init_scan import apply_governance_policies

        mock_logger = MagicMock()
        contracts = [{"name": "legacy-c1", "produces": [{"name": "orders"}]}]
        results = {
            "sensitive_columns": [
                {"model": "orders", "column": "email", "type": "EMAIL", "confidence": 0.9}
            ],
            "metadata": {},
        }
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init_scan.console"):
                with patch("fluid_build.cli.init_scan.Confirm") as mock_confirm:
                    mock_confirm.ask.return_value = True
                    out = apply_governance_policies(contracts, results, mock_logger)

        mock_logger.warning.assert_called_once()
        warning_args = mock_logger.warning.call_args
        assert "legacy 'produces[]'" in warning_args[0][0]
        assert "legacy-c1" in warning_args[0]
        # Contract unchanged — no policy added to the legacy produces entry.
        assert "policy" not in out[0]["produces"][0]


class TestShowMigrationSummary:
    def test_no_rich_prints_count(self, logger):
        from fluid_build.cli.init_scan import show_migration_summary

        contracts = [{"name": "c1"}, {"name": "c2"}]
        results = {}
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", False):
            with patch("fluid_build.cli.init_scan.cprint") as mock_cprint:
                show_migration_summary(contracts, results, logger)
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        assert "2" in calls

    def test_rich_shows_contract_details(self, logger):
        from fluid_build.cli.init_scan import show_migration_summary

        contracts = [
            {
                "name": "analytics",
                "fluidVersion": FluidSchemaManager.latest_bundled_version(),
                "exposes": [
                    {"exposeId": "m1", "binding": {"platform": "gcp"}},
                    {"exposeId": "m2", "binding": {"platform": "gcp"}},
                ],
            }
        ]
        results = {}
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init_scan.console") as mock_con:
                mock_con.print = MagicMock()
                show_migration_summary(contracts, results, logger)
        mock_con.print.assert_called()

    def test_rich_shows_gdpr_flag_when_sovereignty(self, logger):
        from fluid_build.cli.init_scan import show_migration_summary

        contracts = [
            {
                "name": "eu-data",
                "fluidVersion": FluidSchemaManager.latest_bundled_version(),
                "exposes": [],
                "sovereignty": {"jurisdiction": "EU"},
            }
        ]
        results = {}
        with patch("fluid_build.cli.init_scan.RICH_AVAILABLE", True):
            with patch("fluid_build.cli.init_scan.console") as mock_con:
                mock_con.print = MagicMock()
                show_migration_summary(contracts, results, logger)
        calls = " ".join(str(c) for c in mock_con.print.call_args_list)
        assert "GDPR" in calls


# ===========================================================================
# run() top-level routing
# ===========================================================================


class TestRunRouting:
    @patch("fluid_build.cli.init._ai_mode", return_value=0)
    @patch("fluid_build.cli.init.detect_mode", return_value="ai")
    def test_routes_ai(self, _mock_detect, _mock_ai, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(), logger) == 0

    @patch("fluid_build.cli.init.blank_mode", return_value=0)
    @patch("fluid_build.cli.init.detect_mode", return_value="blank")
    def test_routes_blank(self, _mock_detect, _mock_blank, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(blank=True), logger) == 0

    @patch("fluid_build.cli.init.template_mode", return_value=0)
    @patch("fluid_build.cli.init.detect_mode", return_value="template")
    def test_routes_template(self, _mock_detect, _mock_tmpl, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(template="customer-360"), logger) == 0

    @patch("fluid_build.cli.init.detect_mode", return_value=None)
    def test_none_mode_returns_1(self, _mock_detect, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(), logger) == 1

    @patch("fluid_build.cli.init.detect_mode", return_value="unknown-mode")
    def test_unknown_mode_returns_1(self, _mock_detect, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(), logger) == 1

    @patch("fluid_build.cli.init.detect_mode", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_returns_130(self, _mock_detect, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(), logger) == 130

    @patch("fluid_build.cli.init.detect_mode", side_effect=RuntimeError("boom"))
    def test_exception_returns_1(self, _mock_detect, logger):
        from fluid_build.cli.init import run

        assert run(_make_args(), logger) == 1
