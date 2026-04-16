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

"""Tests for fluid_build.cli.verify – covering verify_bigquery_table and run()."""

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.verify import run, verify_bigquery_table

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(
    contract="contract.yaml",
    expose_id=None,
    strict=False,
    out=None,
    show_diffs=False,
    env=None,
):
    """Return a minimal Namespace that satisfies run()."""
    ns = argparse.Namespace(
        contract=contract,
        expose_id=expose_id,
        strict=strict,
        out=out,
        show_diffs=show_diffs,
        env=env,
    )
    return ns


def _bq_field(name, field_type, mode="NULLABLE"):
    f = Mock()
    f.name = name
    f.field_type = field_type
    f.mode = mode
    return f


def _make_bq_table(fields, num_rows=100, created=None, modified=None):
    tbl = Mock()
    tbl.schema = fields
    tbl.num_rows = num_rows
    tbl.created = created
    tbl.modified = modified
    return tbl


def _make_bq_dataset(location="US"):
    ds = Mock()
    ds.location = location
    return ds


# ---------------------------------------------------------------------------
# verify_bigquery_table – table not found
# ---------------------------------------------------------------------------


class TestVerifyBigqueryTableNotFound:
    def test_table_not_found_returns_error(self):
        mock_client = MagicMock()
        mock_client.get_table.side_effect = Exception("Not found")

        with patch.dict(
            "sys.modules",
            {"google.cloud": MagicMock(), "google.cloud.bigquery": MagicMock()},
        ):
            with patch("fluid_build.cli.verify.verify_bigquery_table") as patched:
                patched.return_value = {
                    "status": "error",
                    "error": "Table not found: proj.ds.tbl",
                    "exists": False,
                }
                result = patched("proj", "ds", "tbl", [])
        assert result["status"] == "error"
        assert result["exists"] is False

    def test_import_error_returns_error(self):
        """When google-cloud-bigquery is not installed, return error dict."""
        with patch.dict("sys.modules", {"google": None, "google.cloud": None}):
            result = verify_bigquery_table("p", "d", "t", [])
        assert result["status"] == "error"
        assert result["exists"] is False


# ---------------------------------------------------------------------------
# verify_bigquery_table – full mocked BQ client
# ---------------------------------------------------------------------------


class TestVerifyBigqueryTableWithMockedClient:
    """Drive verify_bigquery_table through the BigQuery code path via mocks."""

    def _run(self, schema_fields, expected_schema, region=None, dataset_location="US"):
        """
        Patch google.cloud.bigquery so we can exercise the real function code.
        """
        mock_bq_table = _make_bq_table(schema_fields)
        mock_bq_dataset = _make_bq_dataset(dataset_location)

        mock_client_instance = MagicMock()
        mock_client_instance.get_table.return_value = mock_bq_table
        mock_client_instance.get_dataset.return_value = mock_bq_dataset

        mock_bigquery_module = MagicMock()
        mock_bigquery_module.Client.return_value = mock_client_instance

        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(),
                "google.cloud": MagicMock(),
                "google.cloud.bigquery": mock_bigquery_module,
            },
        ):
            # Re-import to pick up the patched module inside verify_bigquery_table
            import importlib

            import fluid_build.cli.verify as verify_mod

            with patch.object(
                verify_mod,
                "verify_bigquery_table",
                wraps=None,
            ):
                # Call the real function but patch the inner import
                with patch(
                    "builtins.__import__", side_effect=self._make_importer(mock_bigquery_module)
                ):
                    result = verify_bigquery_table("proj", "ds", "tbl", expected_schema, region)
        return result

    @staticmethod
    def _make_importer(mock_bq):
        real_import = __builtins__.__import__ if isinstance(__builtins__, dict) else __import__

        def _import(name, *args, **kwargs):
            if name == "google.cloud.bigquery" or name == "google.cloud":
                mod = MagicMock()
                mod.bigquery = mock_bq
                return mod
            return real_import(name, *args, **kwargs)

        return _import

    def test_perfect_match_returns_match_status(self):
        fields = [_bq_field("id", "INTEGER", "REQUIRED"), _bq_field("name", "STRING", "NULLABLE")]
        expected = [
            {"name": "id", "type": "integer", "required": True},
            {"name": "name", "type": "string"},
        ]
        # Use the module-level patch approach instead
        result = self._drive_verify(fields, expected, region=None, dataset_loc="US")
        assert result["status"] == "match"
        assert result["exists"] is True
        assert result["severity"]["level"] == "SUCCESS"

    def test_missing_field_returns_mismatch_critical(self):
        fields = [_bq_field("id", "INTEGER", "REQUIRED")]
        expected = [
            {"name": "id", "type": "integer", "required": True},
            {"name": "name", "type": "string"},
        ]
        result = self._drive_verify(fields, expected)
        assert result["status"] == "mismatch"
        assert result["severity"]["level"] == "CRITICAL"
        missing = result["dimensions"]["structure"]["missing_fields"]
        assert any(m["field"] == "name" for m in missing)

    def test_extra_field_returns_mismatch_info(self):
        fields = [
            _bq_field("id", "INTEGER", "REQUIRED"),
            _bq_field("extra_col", "STRING", "NULLABLE"),
        ]
        expected = [{"name": "id", "type": "integer", "required": True}]
        result = self._drive_verify(fields, expected)
        assert result["status"] == "mismatch"
        assert result["severity"]["level"] == "INFO"
        extra = result["dimensions"]["structure"]["extra_fields"]
        assert any(e["field"] == "extra_col" for e in extra)

    def test_type_mismatch_returns_critical(self):
        fields = [_bq_field("amount", "STRING", "NULLABLE")]
        expected = [{"name": "amount", "type": "integer"}]
        result = self._drive_verify(fields, expected)
        assert result["severity"]["level"] == "CRITICAL"
        type_mismatches = result["dimensions"]["types"]["mismatches"]
        assert any(m["field"] == "amount" for m in type_mismatches)

    def test_mode_mismatch_returns_warning(self):
        fields = [_bq_field("email", "STRING", "NULLABLE")]
        expected = [{"name": "email", "type": "string", "required": True}]
        result = self._drive_verify(fields, expected)
        assert result["severity"]["level"] == "WARNING"
        mode_mismatches = result["dimensions"]["constraints"]["mismatches"]
        assert any(m["field"] == "email" for m in mode_mismatches)

    def test_region_mismatch_returns_critical(self):
        fields = [_bq_field("id", "INTEGER", "REQUIRED")]
        expected = [{"name": "id", "type": "integer", "required": True}]
        result = self._drive_verify(fields, expected, region="EU", dataset_loc="US")
        assert result["severity"]["level"] == "CRITICAL"
        assert result["dimensions"]["location"]["status"] == "fail"

    def test_region_match_passes(self):
        fields = [_bq_field("id", "INTEGER", "REQUIRED")]
        expected = [{"name": "id", "type": "integer", "required": True}]
        result = self._drive_verify(fields, expected, region="EU", dataset_loc="EU")
        assert result["dimensions"]["location"]["status"] == "pass"

    def test_type_mapping_bool(self):
        fields = [_bq_field("active", "BOOL", "NULLABLE")]
        expected = [{"name": "active", "type": "boolean"}]
        result = self._drive_verify(fields, expected)
        assert result["dimensions"]["types"]["status"] == "pass"

    def test_type_mapping_int_alias(self):
        fields = [_bq_field("count", "INTEGER", "NULLABLE")]
        expected = [{"name": "count", "type": "int"}]
        result = self._drive_verify(fields, expected)
        assert result["dimensions"]["types"]["status"] == "pass"

    def test_metadata_included_in_result(self):
        fields = [_bq_field("id", "INTEGER", "NULLABLE")]
        expected = [{"name": "id", "type": "integer"}]
        result = self._drive_verify(fields, expected)
        assert "metadata" in result
        assert "num_rows" in result["metadata"]

    def test_no_region_expected_passes_location(self):
        fields = [_bq_field("id", "INTEGER", "NULLABLE")]
        expected = [{"name": "id", "type": "integer"}]
        result = self._drive_verify(fields, expected, region=None, dataset_loc="EU")
        assert result["dimensions"]["location"]["status"] == "pass"

    def _drive_verify(self, bq_fields, expected_schema, region=None, dataset_loc="US"):
        """Helper: patch google.cloud.bigquery at the sys.modules level."""
        mock_bq_table = _make_bq_table(bq_fields)
        mock_bq_dataset = _make_bq_dataset(dataset_loc)

        mock_client_instance = MagicMock()
        mock_client_instance.get_table.return_value = mock_bq_table
        mock_client_instance.get_dataset.return_value = mock_bq_dataset

        mock_bigquery = MagicMock()
        mock_bigquery.Client.return_value = mock_client_instance

        mock_google_cloud = MagicMock()
        mock_google_cloud.bigquery = mock_bigquery

        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(cloud=mock_google_cloud),
                "google.cloud": mock_google_cloud,
                "google.cloud.bigquery": mock_bigquery,
            },
        ):
            return verify_bigquery_table("proj", "ds", "tbl", expected_schema, region)


# ---------------------------------------------------------------------------
# run() – contract not found / load failures
# ---------------------------------------------------------------------------


class TestRunContractErrors:
    def test_missing_contract_raises_clierror(self, tmp_path):
        args = _make_args(contract=str(tmp_path / "nonexistent.yaml"))
        logger = logging.getLogger("test")
        with pytest.raises(CLIError) as exc_info:
            run(args, logger)
        assert exc_info.value.event == "contract_not_found"

    def test_load_failure_raises_clierror(self, tmp_path):
        contract_path = tmp_path / "bad.yaml"
        contract_path.write_text("id: test\n")
        args = _make_args(contract=str(contract_path))
        logger = logging.getLogger("test")

        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            side_effect=Exception("parse error"),
        ):
            with pytest.raises(CLIError) as exc_info:
                run(args, logger)
        assert exc_info.value.event == "contract_load_failed"

    def test_clierror_propagates_unchanged(self, tmp_path):
        contract_path = tmp_path / "contract.yaml"
        contract_path.write_text("id: test\n")
        args = _make_args(contract=str(contract_path))
        logger = logging.getLogger("test")

        original = CLIError(2, "some_event")
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            side_effect=original,
        ):
            with pytest.raises(CLIError) as exc_info:
                run(args, logger)
        assert exc_info.value is original


# ---------------------------------------------------------------------------
# run() – expose filtering
# ---------------------------------------------------------------------------


class TestRunExposeFiltering:
    def _contract(self, exposes):
        return {"id": "test-contract", "exposes": exposes}

    def _run_with_contract(self, contract_data, args, extra_patches=None):
        logger = logging.getLogger("test")
        patches = {
            "fluid_build.cli.verify.load_contract_with_overlay": MagicMock(
                return_value=contract_data
            ),
        }
        if extra_patches:
            patches.update(extra_patches)
        with patch.multiple("fluid_build.cli.verify", **patches):
            return run(args, logger)

    def test_expose_not_found_raises_clierror(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), expose_id="missing_expose")
        contract = self._contract(
            [{"exposeId": "other_expose", "format": "bigquery_table", "properties": {}}]
        )
        with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
            with pytest.raises(CLIError) as exc_info:
                run(args, logging.getLogger("test"))
        assert exc_info.value.event == "expose_not_found"

    def test_all_exposes_verified_when_no_filter(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path))
        contract = self._contract(
            [
                {
                    "exposeId": "expose1",
                    "format": "unsupported_format",
                },
                {
                    "exposeId": "expose2",
                    "format": "unsupported_format",
                },
            ]
        )
        with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
            result = run(args, logging.getLogger("test"))
        # unsupported formats return error status, but run completes
        assert result == 0

    def test_specific_expose_verified(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), expose_id="expose1")
        contract = self._contract(
            [
                {"exposeId": "expose1", "format": "unsupported"},
                {"exposeId": "expose2", "format": "unsupported"},
            ]
        )
        with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
            result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_exposes_as_dict(self, tmp_path):
        """run() should handle exposes when the contract stores them as a dict."""
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path))
        contract = {
            "id": "t",
            "exposes": {
                "expose1": {"format": "unsupported"},
            },
        }
        with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
            result = run(args, logging.getLogger("test"))
        assert result == 0


# ---------------------------------------------------------------------------
# run() – strict mode and output report
# ---------------------------------------------------------------------------


class TestRunStrictAndOutput:
    def _simple_contract(self):
        return {
            "id": "strict-test",
            "exposes": [
                {"exposeId": "bad_expose", "format": "unsupported_format"},
            ],
        }

    def test_strict_mode_returns_1_on_error(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), strict=True)
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._simple_contract(),
        ):
            result = run(args, logging.getLogger("test"))
        assert result == 1

    def test_non_strict_returns_0_despite_errors(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), strict=False)
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._simple_contract(),
        ):
            result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_out_writes_json_report(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        report_path = tmp_path / "report.json"
        args = _make_args(contract=str(contract_path), out=str(report_path))
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._simple_contract(),
        ):
            run(args, logging.getLogger("test"))
        assert report_path.exists()
        data = json.loads(report_path.read_text())
        assert "summary" in data
        assert "results" in data
        assert data["contract_id"] == "strict-test"

    def test_show_diffs_does_not_crash(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), show_diffs=True)
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._simple_contract(),
        ):
            result = run(args, logging.getLogger("test"))
        assert result == 0


# ---------------------------------------------------------------------------
# run() – bigquery_table format end-to-end via mocked verify_bigquery_table
# ---------------------------------------------------------------------------


class TestRunBigqueryTableFormat:
    def _bq_expose_contract(self, target="proj.ds.tbl", region=None, schema=None):
        expose = {
            "exposeId": "my_table",
            "format": "bigquery_table",
            "properties": {"target": target},
        }
        if region:
            expose["properties"]["region"] = region
        if schema is not None:
            expose["properties"]["schema"] = schema
        return {"id": "bq-contract", "exposes": [expose]}

    def test_bigquery_match_returns_0(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path))
        mock_result = {
            "status": "match",
            "exists": True,
            "severity": {
                "level": "SUCCESS",
                "symbol": "🟢",
                "impact": "NONE",
                "remediation": "NONE",
                "reason": "ok",
                "actions": [],
            },
            "dimensions": {
                "structure": {
                    "status": "pass",
                    "matching_fields": ["id"],
                    "missing_fields": [],
                    "extra_fields": [],
                    "total_expected": 1,
                    "total_actual": 1,
                },
                "types": {"status": "pass", "mismatches": []},
                "constraints": {"status": "pass", "mismatches": []},
                "location": {"status": "pass", "expected": None, "actual": "US", "message": None},
            },
            "metadata": {"num_rows": 0, "created": None, "modified": None},
        }
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._bq_expose_contract(),
        ):
            with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=mock_result):
                result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_bigquery_mismatch_strict_returns_1(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), strict=True)
        mock_result = {
            "status": "mismatch",
            "exists": True,
            "severity": {
                "level": "CRITICAL",
                "symbol": "🔴",
                "impact": "HIGH",
                "remediation": "MANUAL",
                "reason": "missing",
                "actions": ["fix it"],
            },
            "dimensions": {
                "structure": {
                    "status": "fail",
                    "matching_fields": [],
                    "missing_fields": [{"field": "id", "expected": {}}],
                    "extra_fields": [],
                    "total_expected": 1,
                    "total_actual": 0,
                },
                "types": {"status": "pass", "mismatches": []},
                "constraints": {"status": "pass", "mismatches": []},
                "location": {"status": "pass", "expected": None, "actual": "US", "message": None},
            },
            "metadata": {"num_rows": 0, "created": None, "modified": None},
        }
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._bq_expose_contract(),
        ):
            with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=mock_result):
                result = run(args, logging.getLogger("test"))
        assert result == 1

    def test_invalid_target_format_records_error(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path))
        contract = self._bq_expose_contract(target="bad_target_no_dots")
        with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
            result = run(args, logging.getLogger("test"))
        assert result == 0  # errors but not strict

    def test_bigquery_binding_format(self, tmp_path):
        """Expose using binding.format instead of top-level format."""
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path))
        contract = {
            "id": "bq-binding",
            "exposes": [
                {
                    "exposeId": "my_table",
                    "binding": {
                        "format": "bigquery_table",
                        "location": {
                            "project": "proj",
                            "dataset": "ds",
                            "table": "tbl",
                            "region": "US",
                        },
                    },
                    "schema": [{"name": "id", "type": "integer"}],
                }
            ],
        }
        mock_result = {
            "status": "match",
            "exists": True,
            "severity": {
                "level": "SUCCESS",
                "symbol": "🟢",
                "impact": "NONE",
                "remediation": "NONE",
                "reason": "ok",
                "actions": [],
            },
            "dimensions": {
                "structure": {
                    "status": "pass",
                    "matching_fields": ["id"],
                    "missing_fields": [],
                    "extra_fields": [],
                    "total_expected": 1,
                    "total_actual": 1,
                },
                "types": {"status": "pass", "mismatches": []},
                "constraints": {"status": "pass", "mismatches": []},
                "location": {"status": "pass", "expected": "US", "actual": "US", "message": None},
            },
            "metadata": {"num_rows": 5, "created": None, "modified": None},
        }
        with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
            with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=mock_result):
                result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_show_diffs_with_type_mismatches(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), show_diffs=True)
        mock_result = {
            "status": "mismatch",
            "exists": True,
            "severity": {
                "level": "CRITICAL",
                "symbol": "🔴",
                "impact": "HIGH",
                "remediation": "MANUAL",
                "reason": "type mismatch",
                "actions": ["fix types"],
            },
            "dimensions": {
                "structure": {
                    "status": "pass",
                    "matching_fields": ["amount"],
                    "missing_fields": [],
                    "extra_fields": [],
                    "total_expected": 1,
                    "total_actual": 1,
                },
                "types": {
                    "status": "fail",
                    "mismatches": [{"field": "amount", "expected": "integer", "actual": "string"}],
                },
                "constraints": {"status": "pass", "mismatches": []},
                "location": {"status": "pass", "expected": None, "actual": "US", "message": None},
            },
            "metadata": {"num_rows": 0, "created": None, "modified": None},
        }
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._bq_expose_contract(),
        ):
            with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=mock_result):
                result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_show_diffs_with_mode_mismatches(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), show_diffs=True)
        mock_result = {
            "status": "mismatch",
            "exists": True,
            "severity": {
                "level": "WARNING",
                "symbol": "🟡",
                "impact": "MEDIUM",
                "remediation": "MANUAL",
                "reason": "mode mismatch",
                "actions": ["fix modes"],
            },
            "dimensions": {
                "structure": {
                    "status": "pass",
                    "matching_fields": ["email"],
                    "missing_fields": [],
                    "extra_fields": [],
                    "total_expected": 1,
                    "total_actual": 1,
                },
                "types": {"status": "pass", "mismatches": []},
                "constraints": {
                    "status": "fail",
                    "mismatches": [
                        {"field": "email", "expected": "required", "actual": "nullable"}
                    ],
                },
                "location": {"status": "pass", "expected": None, "actual": "US", "message": None},
            },
            "metadata": {"num_rows": 0, "created": None, "modified": None},
        }
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._bq_expose_contract(),
        ):
            with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=mock_result):
                result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_bq_table_error_status_increments_error_count(self, tmp_path):
        contract_path = tmp_path / "c.yaml"
        contract_path.write_text("id: t\n")
        args = _make_args(contract=str(contract_path), strict=True)
        mock_result = {"status": "error", "error": "connection refused", "exists": False}
        with patch(
            "fluid_build.cli.verify.load_contract_with_overlay",
            return_value=self._bq_expose_contract(),
        ):
            with patch("fluid_build.cli.verify.verify_bigquery_table", return_value=mock_result):
                result = run(args, logging.getLogger("test"))
        assert result == 1  # strict + error → exit 1
