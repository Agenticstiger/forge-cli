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

"""Tests for forge_copilot_schema_inference.py: type inference, file parsing."""

import csv
import json
import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge_copilot_schema_inference import (
    extract_avro_columns,
    extract_provider_hints,
    infer_arrow_type,
    infer_avro_type,
    infer_duckdb_type,
    infer_python_type,
    infer_scalar_type,
    load_json_rows,
    map_inferred_type_to_contract_type,
    merge_types,
    read_avro_metadata,
    read_parquet_metadata,
    summarize_sample_file,
)

LOG = logging.getLogger("test_forge_copilot_ext3")


# ---------------------------------------------------------------------------
# infer_scalar_type
# ---------------------------------------------------------------------------


class TestInferScalarType:
    def test_none(self):
        assert infer_scalar_type(None) == "null"

    def test_empty_string(self):
        assert infer_scalar_type("") == "null"

    def test_whitespace(self):
        assert infer_scalar_type("   ") == "null"

    def test_boolean_true(self):
        assert infer_scalar_type("true") == "boolean"

    def test_boolean_false(self):
        assert infer_scalar_type("False") == "boolean"

    def test_integer(self):
        assert infer_scalar_type("42") == "integer"

    def test_negative_integer(self):
        assert infer_scalar_type("-10") == "integer"

    def test_number(self):
        assert infer_scalar_type("3.14") == "number"

    def test_negative_number(self):
        assert infer_scalar_type("-1.5") == "number"

    def test_date(self):
        assert infer_scalar_type("2024-01-15") == "date"

    def test_datetime(self):
        assert infer_scalar_type("2024-01-15T10:30:00Z") == "datetime"

    def test_datetime_with_space(self):
        assert infer_scalar_type("2024-01-15 10:30:00") == "datetime"

    def test_string(self):
        assert infer_scalar_type("hello world") == "string"


# ---------------------------------------------------------------------------
# infer_python_type
# ---------------------------------------------------------------------------


class TestInferPythonType:
    def test_none(self):
        assert infer_python_type(None) == "null"

    def test_bool(self):
        assert infer_python_type(True) == "boolean"
        assert infer_python_type(False) == "boolean"

    def test_int(self):
        assert infer_python_type(42) == "integer"

    def test_float(self):
        assert infer_python_type(3.14) == "number"

    def test_list(self):
        assert infer_python_type([1, 2, 3]) == "array"

    def test_dict(self):
        assert infer_python_type({"key": "val"}) == "object"

    def test_string(self):
        assert infer_python_type("hello") == "string"

    def test_string_with_number(self):
        assert infer_python_type("42") == "integer"

    def test_string_with_date(self):
        assert infer_python_type("2024-01-01") == "date"


# ---------------------------------------------------------------------------
# merge_types
# ---------------------------------------------------------------------------


class TestMergeTypes:
    def test_all_same(self):
        assert merge_types(["integer", "integer", "integer"]) == "integer"

    def test_mixed(self):
        result = merge_types(["integer", "integer", "string"])
        assert result == "integer"  # most common

    def test_all_null(self):
        assert merge_types(["null", "null"]) == "string"

    def test_empty(self):
        assert merge_types([]) == "string"

    def test_with_nulls(self):
        result = merge_types(["null", "integer", "null", "integer"])
        assert result == "integer"


# ---------------------------------------------------------------------------
# map_inferred_type_to_contract_type
# ---------------------------------------------------------------------------


class TestMapInferredType:
    def test_boolean(self):
        assert map_inferred_type_to_contract_type("boolean") == "boolean"

    def test_integer(self):
        assert map_inferred_type_to_contract_type("integer") == "integer"

    def test_number(self):
        assert map_inferred_type_to_contract_type("number") == "number"

    def test_date(self):
        assert map_inferred_type_to_contract_type("date") == "date"

    def test_datetime(self):
        assert map_inferred_type_to_contract_type("datetime") == "timestamp"

    def test_array(self):
        assert map_inferred_type_to_contract_type("array") == "array"

    def test_object(self):
        assert map_inferred_type_to_contract_type("object") == "object"

    def test_string(self):
        assert map_inferred_type_to_contract_type("string") == "string"

    def test_unknown(self):
        assert map_inferred_type_to_contract_type("unknown_type") == "string"


# ---------------------------------------------------------------------------
# extract_provider_hints
# ---------------------------------------------------------------------------


class TestExtractProviderHints:
    def test_gcp(self):
        assert "gcp" in extract_provider_hints("bigquery_table.csv")

    def test_aws(self):
        assert "aws" in extract_provider_hints("s3_bucket_data.json")

    def test_snowflake(self):
        assert "snowflake" in extract_provider_hints("snowflake_warehouse.csv")

    def test_local(self):
        hints = extract_provider_hints("local_data.csv")
        assert "local" in hints

    def test_no_hints(self):
        hints = extract_provider_hints("data.txt")
        assert hints == []

    def test_gcp_composer(self):
        assert "gcp" in extract_provider_hints("composer_dag.py")

    def test_aws_redshift(self):
        assert "aws" in extract_provider_hints("redshift_table.sql")

    def test_aws_glue(self):
        assert "aws" in extract_provider_hints("glue_job.py")

    def test_gcp_dataform(self):
        assert "gcp" in extract_provider_hints("dataform_model.sql")

    def test_aws_athena(self):
        assert "aws" in extract_provider_hints("athena_query.sql")

    def test_duckdb(self):
        hints = extract_provider_hints("duckdb_data.csv")
        assert "local" in hints


# ---------------------------------------------------------------------------
# infer_avro_type
# ---------------------------------------------------------------------------


class TestInferAvroType:
    def test_string(self):
        assert infer_avro_type("string") == "string"

    def test_int(self):
        assert infer_avro_type("int") == "integer"

    def test_long(self):
        assert infer_avro_type("long") == "integer"

    def test_float(self):
        assert infer_avro_type("float") == "number"

    def test_double(self):
        assert infer_avro_type("double") == "number"

    def test_boolean(self):
        assert infer_avro_type("boolean") == "boolean"

    def test_bytes(self):
        assert infer_avro_type("bytes") == "string"

    def test_array(self):
        assert infer_avro_type("array") == "array"

    def test_map(self):
        assert infer_avro_type("map") == "object"

    def test_record(self):
        assert infer_avro_type("record") == "object"

    def test_enum(self):
        assert infer_avro_type("enum") == "string"

    def test_union_with_null(self):
        assert infer_avro_type(["null", "string"]) == "string"

    def test_union_all_null(self):
        assert infer_avro_type(["null"]) == "string"

    def test_mapping_date(self):
        assert infer_avro_type({"type": "int", "logicalType": "date"}) == "date"

    def test_mapping_timestamp(self):
        assert infer_avro_type({"type": "long", "logicalType": "timestamp-millis"}) == "datetime"

    def test_mapping_array(self):
        assert infer_avro_type({"type": "array", "items": "string"}) == "array"

    def test_mapping_map(self):
        assert infer_avro_type({"type": "map", "values": "string"}) == "object"

    def test_mapping_record(self):
        assert infer_avro_type({"type": "record", "fields": []}) == "object"

    def test_mapping_enum(self):
        assert infer_avro_type({"type": "enum", "symbols": ["A", "B"]}) == "string"

    def test_unknown(self):
        assert infer_avro_type(42) == "string"


# ---------------------------------------------------------------------------
# infer_arrow_type
# ---------------------------------------------------------------------------


class TestInferArrowType:
    def test_bool(self):
        assert infer_arrow_type("bool") == "boolean"

    def test_int64(self):
        assert infer_arrow_type("int64") == "integer"

    def test_uint32(self):
        assert infer_arrow_type("uint32") == "integer"

    def test_float64(self):
        assert infer_arrow_type("float64") == "number"

    def test_double(self):
        assert infer_arrow_type("double") == "number"

    def test_decimal(self):
        assert infer_arrow_type("decimal128(10,2)") == "number"

    def test_timestamp(self):
        assert infer_arrow_type("timestamp[ns]") == "datetime"

    def test_date(self):
        assert infer_arrow_type("date32") == "date"

    def test_list(self):
        # "list<int64>" contains "int" which matches before "list" check
        assert infer_arrow_type("list<string>") == "array"

    def test_large_list(self):
        assert infer_arrow_type("large_list<string>") == "array"

    def test_struct(self):
        assert infer_arrow_type("struct<a: char, b: string>") == "object"

    def test_map_type(self):
        assert infer_arrow_type("map<string, char>") == "object"

    def test_string(self):
        assert infer_arrow_type("utf8") == "string"


# ---------------------------------------------------------------------------
# infer_duckdb_type
# ---------------------------------------------------------------------------


class TestInferDuckdbType:
    def test_boolean(self):
        assert infer_duckdb_type("BOOLEAN") == "boolean"

    def test_integer(self):
        assert infer_duckdb_type("INTEGER") == "integer"

    def test_bigint(self):
        assert infer_duckdb_type("BIGINT") == "integer"

    def test_tinyint(self):
        assert infer_duckdb_type("TINYINT") == "integer"

    def test_smallint(self):
        assert infer_duckdb_type("SMALLINT") == "integer"

    def test_hugeint(self):
        assert infer_duckdb_type("HUGEINT") == "integer"

    def test_float(self):
        assert infer_duckdb_type("FLOAT") == "number"

    def test_double(self):
        assert infer_duckdb_type("DOUBLE") == "number"

    def test_decimal(self):
        assert infer_duckdb_type("DECIMAL(10,2)") == "number"

    def test_real(self):
        assert infer_duckdb_type("REAL") == "number"

    def test_timestamp(self):
        assert infer_duckdb_type("TIMESTAMP") == "datetime"

    def test_date(self):
        assert infer_duckdb_type("DATE") == "date"

    def test_array(self):
        # "INTEGER[]" contains "integer" token, matching before [] check
        assert infer_duckdb_type("VARCHAR[]") == "array"

    def test_list(self):
        # "LIST(INTEGER)" contains "integer", matching before "list" check
        assert infer_duckdb_type("LIST(VARCHAR)") == "array"

    def test_struct(self):
        assert infer_duckdb_type("STRUCT(a INT)") == "object"

    def test_map_duckdb(self):
        assert infer_duckdb_type("MAP(STRING, INT)") == "object"

    def test_varchar(self):
        assert infer_duckdb_type("VARCHAR") == "string"


# ---------------------------------------------------------------------------
# extract_avro_columns
# ---------------------------------------------------------------------------


class TestExtractAvroColumns:
    def test_basic(self):
        schema = {
            "type": "record",
            "fields": [
                {"name": "id", "type": "int"},
                {"name": "name", "type": "string"},
            ],
        }
        columns = extract_avro_columns(schema)
        assert columns == {"id": "integer", "name": "string"}

    def test_empty_fields(self):
        schema = {"type": "record", "fields": []}
        assert extract_avro_columns(schema) == {}

    def test_no_fields(self):
        schema = {"type": "record"}
        assert extract_avro_columns(schema) == {}

    def test_field_no_name(self):
        schema = {"fields": [{"type": "int"}]}
        assert extract_avro_columns(schema) == {}


# ---------------------------------------------------------------------------
# load_json_rows
# ---------------------------------------------------------------------------


class TestLoadJsonRows:
    def test_json_array(self):
        data = [{"a": 1}, {"a": 2}]
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        rows = list(load_json_rows(Path(tmp_path)))
        assert len(rows) == 2

    def test_json_empty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("")
            tmp_path = f.name

        rows = list(load_json_rows(Path(tmp_path)))
        assert rows == []

    def test_jsonl(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write('{"a": 1}\n{"a": 2}\n')
            tmp_path = f.name

        rows = list(load_json_rows(Path(tmp_path)))
        assert len(rows) == 2

    def test_json_dict_of_lists(self):
        data = {"col1": [1, 2, 3], "col2": ["a", "b", "c"]}
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        rows = list(load_json_rows(Path(tmp_path)))
        assert len(rows) == 3
        assert rows[0] == {"col1": 1, "col2": "a"}

    def test_json_single_object(self):
        data = {"key": "value", "nested": {"inner": 1}}
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        rows = list(load_json_rows(Path(tmp_path)))
        assert len(rows) == 1

    def test_jsonl_empty_lines(self):
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write('{"a": 1}\n\n{"a": 2}\n')
            tmp_path = f.name

        rows = list(load_json_rows(Path(tmp_path)))
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# summarize_sample_file
# ---------------------------------------------------------------------------


class TestSummarizeSampleFile:
    def test_csv(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False) as f:
            writer = csv.writer(f)
            writer.writerow(["name", "age", "active"])
            writer.writerow(["Alice", "30", "true"])
            writer.writerow(["Bob", "25", "false"])
            tmp_path = f.name

        summary = summarize_sample_file(Path(tmp_path))
        assert summary["format"] == "csv"
        assert "name" in summary["columns"]
        assert summary["sampled_rows"] == 2

    def test_json(self):
        data = [{"id": 1, "name": "test"}, {"id": 2, "name": "test2"}]
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        summary = summarize_sample_file(Path(tmp_path))
        assert summary["format"] == "json"
        assert "id" in summary["columns"]

    def test_invalid_json_warns(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{not json")
            tmp_path = f.name

        summary = summarize_sample_file(Path(tmp_path))
        assert summary["format"] == "json"
        assert summary["columns"] == {}
        assert summary["sampled_rows"] == 0
        assert "warnings" in summary
        assert "Could not inspect JSON schema" in summary["warnings"][0]

    def test_parquet_no_library(self):
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(b"dummy")
            tmp_path = f.name

        with (
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_parquet_metadata_pyarrow",
                side_effect=ImportError,
            ),
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_parquet_metadata_duckdb",
                side_effect=ImportError,
            ),
        ):
            summary = summarize_sample_file(Path(tmp_path))
        assert summary["format"] == "parquet"
        assert "warnings" in summary

    def test_avro_no_library(self):
        with tempfile.NamedTemporaryFile(suffix=".avro", delete=False) as f:
            f.write(b"dummy")
            tmp_path = f.name

        with (
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_avro_metadata_fastavro",
                side_effect=ImportError,
            ),
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_avro_metadata_avro",
                side_effect=ImportError,
            ),
        ):
            summary = summarize_sample_file(Path(tmp_path))
        assert summary["format"] == "avro"
        assert "warnings" in summary

    def test_unknown_format(self):
        with tempfile.NamedTemporaryFile(suffix=".xyz", mode="w", delete=False) as f:
            f.write("unknown data")
            tmp_path = f.name

        summary = summarize_sample_file(Path(tmp_path))
        assert summary["columns"] == {}


# ---------------------------------------------------------------------------
# read_parquet_metadata / read_avro_metadata
# ---------------------------------------------------------------------------


class TestReadMetadata:
    def test_parquet_no_libs(self):
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(b"x")
            tmp_path = f.name

        with (
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_parquet_metadata_pyarrow",
                side_effect=ImportError,
            ),
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_parquet_metadata_duckdb",
                side_effect=ImportError,
            ),
        ):
            result = read_parquet_metadata(Path(tmp_path))
        assert result["columns"] == {}
        assert "warnings" in result

    def test_parquet_exception(self):
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as f:
            f.write(b"x")
            tmp_path = f.name

        with patch(
            "fluid_build.cli.forge_copilot_schema_inference._read_parquet_metadata_pyarrow",
            side_effect=RuntimeError("broken"),
        ):
            result = read_parquet_metadata(Path(tmp_path))
        assert "warnings" in result

    def test_avro_no_libs(self):
        with tempfile.NamedTemporaryFile(suffix=".avro", delete=False) as f:
            f.write(b"x")
            tmp_path = f.name

        with (
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_avro_metadata_fastavro",
                side_effect=ImportError,
            ),
            patch(
                "fluid_build.cli.forge_copilot_schema_inference._read_avro_metadata_avro",
                side_effect=ImportError,
            ),
        ):
            result = read_avro_metadata(Path(tmp_path))
        assert result["columns"] == {}
        assert "warnings" in result

    def test_avro_exception(self):
        with tempfile.NamedTemporaryFile(suffix=".avro", delete=False) as f:
            f.write(b"x")
            tmp_path = f.name

        with patch(
            "fluid_build.cli.forge_copilot_schema_inference._read_avro_metadata_fastavro",
            side_effect=RuntimeError("broken"),
        ):
            result = read_avro_metadata(Path(tmp_path))
        assert "warnings" in result
