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

"""Tests for providers/aws/actions/glue.py — pure helpers and validation paths."""

import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


# ── boto3 mock helper ────────────────────────────────────────────────
@contextmanager
def _patch_boto():
    """Inject fake boto3/botocore into sys.modules so the module can be imported."""
    mock_boto3 = MagicMock()
    mock_botocore = MagicMock()
    mock_exceptions = MagicMock()
    with patch.dict(
        sys.modules,
        {
            "boto3": mock_boto3,
            "botocore": mock_botocore,
            "botocore.exceptions": mock_exceptions,
        },
    ):
        yield mock_boto3, mock_exceptions


from fluid_build.providers.aws.actions.glue import (
    _columns_equal,
    _get_iceberg_table_parameters,
    _get_input_format,
    _get_output_format,
    _get_serde_lib,
)


# ── _get_input_format ────────────────────────────────────────────────
class TestGetInputFormat:
    @pytest.mark.parametrize(
        "fmt,expected_contains",
        [
            ("parquet", "ParquetInputFormat"),
            ("orc", "OrcInputFormat"),
            ("avro", "AvroContainerInputFormat"),
            ("csv", "TextInputFormat"),
            ("json", "TextInputFormat"),
        ],
    )
    def test_known_formats(self, fmt, expected_contains):
        assert expected_contains in _get_input_format(fmt)

    def test_unknown_returns_parquet(self):
        assert "Parquet" in _get_input_format("unknown_fmt")

    def test_case_insensitive(self):
        assert _get_input_format("PARQUET") == _get_input_format("parquet")


# ── _get_output_format ───────────────────────────────────────────────
class TestGetOutputFormat:
    @pytest.mark.parametrize(
        "fmt,expected_contains",
        [
            ("parquet", "ParquetOutputFormat"),
            ("orc", "OrcOutputFormat"),
            ("avro", "AvroContainerOutputFormat"),
            ("csv", "HiveIgnoreKeyTextOutputFormat"),
            ("json", "HiveIgnoreKeyTextOutputFormat"),
        ],
    )
    def test_known_formats(self, fmt, expected_contains):
        assert expected_contains in _get_output_format(fmt)

    def test_unknown_returns_parquet(self):
        assert "Parquet" in _get_output_format("nope")


# ── _get_serde_lib ───────────────────────────────────────────────────
class TestGetSerdeLib:
    @pytest.mark.parametrize(
        "fmt,expected_contains",
        [
            ("parquet", "ParquetHiveSerDe"),
            ("orc", "OrcSerde"),
            ("avro", "AvroSerDe"),
            ("csv", "LazySimpleSerDe"),
            ("json", "JsonSerDe"),
        ],
    )
    def test_known_formats(self, fmt, expected_contains):
        assert expected_contains in _get_serde_lib(fmt)

    def test_unknown_returns_parquet(self):
        assert "Parquet" in _get_serde_lib("xml")


# ── _columns_equal ───────────────────────────────────────────────────
class TestColumnsEqual:
    def test_equal(self):
        a = [{"Name": "id", "Type": "int"}, {"Name": "name", "Type": "string"}]
        b = [{"Name": "id", "Type": "int"}, {"Name": "name", "Type": "string"}]
        assert _columns_equal(a, b) is True

    def test_different_lengths(self):
        assert _columns_equal([{"Name": "id", "Type": "int"}], []) is False

    def test_different_name(self):
        a = [{"Name": "id", "Type": "int"}]
        b = [{"Name": "user_id", "Type": "int"}]
        assert _columns_equal(a, b) is False

    def test_different_type(self):
        a = [{"Name": "id", "Type": "int"}]
        b = [{"Name": "id", "Type": "bigint"}]
        assert _columns_equal(a, b) is False

    def test_case_insensitive_type(self):
        a = [{"Name": "id", "Type": "INT"}]
        b = [{"Name": "id", "Type": "int"}]
        assert _columns_equal(a, b) is True

    def test_case_insensitive_name(self):
        a = [{"Name": "ID", "Type": "int"}]
        b = [{"Name": "id", "Type": "int"}]
        assert _columns_equal(a, b) is True

    def test_empty_both(self):
        assert _columns_equal([], []) is True


# ── _get_iceberg_table_parameters ────────────────────────────────────
class TestGetIcebergTableParameters:
    def test_defaults(self):
        params = _get_iceberg_table_parameters({})
        assert params["table_type"] == "ICEBERG"
        assert params["format-version"] == "2"
        assert params["write.format.default"] == "parquet"

    def test_custom_version(self):
        params = _get_iceberg_table_parameters({"writeVersion": 1})
        assert params["format-version"] == "1"

    def test_custom_file_format(self):
        params = _get_iceberg_table_parameters({"fileFormat": "orc"})
        assert params["write.format.default"] == "orc"

    def test_custom_properties(self):
        params = _get_iceberg_table_parameters(
            {"properties": {"write.metadata.compression-codec": "gzip", "count": 5}}
        )
        assert params["write.metadata.compression-codec"] == "gzip"
        assert params["count"] == "5"

    def test_properties_empty(self):
        params = _get_iceberg_table_parameters({"properties": {}})
        assert len(params) == 3


# ── Validation-path tests (need boto3 mocking) ──────────────────────
class TestEnsureDatabaseValidation:
    def test_missing_database(self):
        with _patch_boto():
            from fluid_build.providers.aws.actions.glue import ensure_database

            result = ensure_database({})
            assert result["status"] == "error"
            assert "database" in result["error"].lower() or "required" in result["error"].lower()
            assert result["changed"] is False


class TestEnsureTableValidation:
    def test_missing_database_and_table(self):
        with _patch_boto():
            from fluid_build.providers.aws.actions.glue import ensure_table

            result = ensure_table({})
            assert result["status"] == "error"
            assert "required" in result["error"].lower()

    def test_missing_table_only(self):
        with _patch_boto():
            from fluid_build.providers.aws.actions.glue import ensure_table

            result = ensure_table({"database": "db"})
            assert result["status"] == "error"


class TestEnsureCrawlerValidation:
    def test_missing_fields(self):
        with _patch_boto():
            from fluid_build.providers.aws.actions.glue import ensure_crawler

            result = ensure_crawler({"name": "c1"})
            assert result["status"] == "error"
            assert "required" in result["error"].lower()


class TestRunCrawlerValidation:
    def test_missing_name(self):
        with _patch_boto():
            from fluid_build.providers.aws.actions.glue import run_crawler

            result = run_crawler({})
            assert result["status"] == "error"


class TestEnsureIcebergTableValidation:
    def test_missing_database_and_table(self):
        with _patch_boto():
            from fluid_build.providers.aws.actions.glue import ensure_iceberg_table

            result = ensure_iceberg_table({})
            assert result["status"] == "error"
            assert "required" in result["error"].lower()
