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

"""Tests for providers/aws/util/ddl.py — Athena + Redshift DDL generation."""

import pytest

from fluid_build.providers.aws.util.ddl import (
    _escape_sql,
    _get_stored_as_format,
    extract_partition_columns,
    generate_athena_ddl,
    generate_redshift_ddl,
    map_fluid_type_to_athena,
    map_fluid_type_to_redshift,
    schema_to_glue_columns,
    schema_to_redshift_columns,
)


# ── map_fluid_type_to_athena ─────────────────────────────────────────
class TestMapFluidTypeToAthena:
    @pytest.mark.parametrize(
        "fluid,expected",
        [
            ("string", "string"),
            ("str", "string"),
            ("text", "string"),
            ("integer", "bigint"),
            ("int", "bigint"),
            ("int32", "int"),
            ("int64", "bigint"),
            ("long", "bigint"),
            ("float", "double"),
            ("float32", "float"),
            ("float64", "double"),
            ("double", "double"),
            ("boolean", "boolean"),
            ("bool", "boolean"),
            ("timestamp", "timestamp"),
            ("datetime", "timestamp"),
            ("date", "date"),
            ("binary", "binary"),
            ("bytes", "binary"),
        ],
    )
    def test_simple_types(self, fluid, expected):
        assert map_fluid_type_to_athena(fluid) == expected

    def test_unknown_defaults_string(self):
        assert map_fluid_type_to_athena("foobar") == "string"

    def test_decimal_passthrough(self):
        assert map_fluid_type_to_athena("decimal(10,2)") == "decimal(10,2)"

    def test_varchar_passthrough(self):
        assert map_fluid_type_to_athena("varchar(255)") == "varchar(255)"

    def test_array_passthrough(self):
        assert map_fluid_type_to_athena("array<string>") == "array<string>"

    def test_map_passthrough(self):
        assert map_fluid_type_to_athena("map<string,int>") == "map<string,int>"

    def test_struct_passthrough(self):
        assert map_fluid_type_to_athena("struct<a:int>") == "struct<a:int>"

    def test_case_insensitive(self):
        assert map_fluid_type_to_athena("STRING") == "string"
        assert map_fluid_type_to_athena("DECIMAL(5,2)") == "decimal(5,2)"


# ── map_fluid_type_to_redshift ───────────────────────────────────────
class TestMapFluidTypeToRedshift:
    @pytest.mark.parametrize(
        "fluid,expected",
        [
            ("string", "VARCHAR(65535)"),
            ("str", "VARCHAR(65535)"),
            ("text", "VARCHAR(65535)"),
            ("integer", "BIGINT"),
            ("int", "BIGINT"),
            ("int32", "INTEGER"),
            ("int64", "BIGINT"),
            ("long", "BIGINT"),
            ("float", "DOUBLE PRECISION"),
            ("float32", "REAL"),
            ("float64", "DOUBLE PRECISION"),
            ("double", "DOUBLE PRECISION"),
            ("boolean", "BOOLEAN"),
            ("bool", "BOOLEAN"),
            ("timestamp", "TIMESTAMP"),
            ("datetime", "TIMESTAMP"),
            ("date", "DATE"),
            ("binary", "VARBYTE(65535)"),
            ("bytes", "VARBYTE(65535)"),
        ],
    )
    def test_simple_types(self, fluid, expected):
        assert map_fluid_type_to_redshift(fluid) == expected

    def test_unknown_defaults_varchar(self):
        assert map_fluid_type_to_redshift("foobar") == "VARCHAR(65535)"

    def test_decimal_passthrough_upper(self):
        assert map_fluid_type_to_redshift("decimal(18,6)") == "DECIMAL(18,6)"

    def test_varchar_passthrough_upper(self):
        assert map_fluid_type_to_redshift("varchar(128)") == "VARCHAR(128)"


# ── _get_stored_as_format ────────────────────────────────────────────
class TestGetStoredAsFormat:
    @pytest.mark.parametrize(
        "fmt,expected",
        [
            ("parquet", "PARQUET"),
            ("orc", "ORC"),
            ("avro", "AVRO"),
            ("csv", "TEXTFILE"),
            ("json", "TEXTFILE"),
            ("PARQUET", "PARQUET"),
        ],
    )
    def test_known(self, fmt, expected):
        assert _get_stored_as_format(fmt) == expected

    def test_unknown(self):
        assert _get_stored_as_format("delta") == "PARQUET"


# ── _escape_sql ──────────────────────────────────────────────────────
class TestEscapeSql:
    def test_single_quotes(self):
        assert _escape_sql("it's a test") == "it''s a test"

    def test_no_escape_needed(self):
        assert _escape_sql("hello") == "hello"


# ── schema_to_glue_columns ──────────────────────────────────────────
class TestSchemaToGlueColumns:
    def test_basic(self):
        schema = [
            {"name": "id", "type": "integer"},
            {"name": "name", "type": "string", "description": "User name"},
        ]
        cols = schema_to_glue_columns(schema)
        assert len(cols) == 2
        assert cols[0] == {"Name": "id", "Type": "bigint"}
        assert cols[1]["Comment"] == "User name"

    def test_no_description(self):
        cols = schema_to_glue_columns([{"name": "x", "type": "boolean"}])
        assert "Comment" not in cols[0]


# ── schema_to_redshift_columns ───────────────────────────────────────
class TestSchemaToRedshiftColumns:
    def test_basic(self):
        schema = [
            {"name": "id", "type": "integer", "required": True},
            {"name": "ts", "type": "timestamp"},
        ]
        cols = schema_to_redshift_columns(schema)
        assert cols[0]["NotNull"] is True
        assert cols[0]["Encode"] == "AZ64"
        assert cols[1]["Encode"] == "AZ64"

    def test_varchar_gets_lzo(self):
        cols = schema_to_redshift_columns([{"name": "s", "type": "string"}])
        assert cols[0]["Encode"] == "LZO"

    def test_no_required_no_notnull(self):
        cols = schema_to_redshift_columns([{"name": "x", "type": "boolean"}])
        assert "NotNull" not in cols[0]


# ── generate_athena_ddl ─────────────────────────────────────────────
class TestGenerateAthenaDDL:
    def test_basic(self):
        cols = [{"Name": "id", "Type": "bigint"}]
        ddl = generate_athena_ddl("mydb", "mytable", cols, "s3://bucket/path/")
        assert "CREATE EXTERNAL TABLE" in ddl
        assert "`mydb`.`mytable`" in ddl
        assert "`id` bigint" in ddl
        assert "STORED AS PARQUET" in ddl
        assert "LOCATION 's3://bucket/path/'" in ddl
        assert ddl.endswith(";")

    def test_column_comment(self):
        cols = [{"Name": "x", "Type": "string", "Comment": "it's a test"}]
        ddl = generate_athena_ddl("db", "t", cols, "s3://b/")
        assert "COMMENT 'it''s a test'" in ddl

    def test_partitions(self):
        cols = [{"Name": "id", "Type": "bigint"}]
        parts = [{"Name": "dt", "Type": "date"}]
        ddl = generate_athena_ddl("db", "t", cols, "s3://b/", partition_columns=parts)
        assert "PARTITIONED BY" in ddl
        assert "`dt` date" in ddl

    def test_file_format(self):
        ddl = generate_athena_ddl("db", "t", [], "s3://b/", file_format="orc")
        assert "STORED AS ORC" in ddl

    def test_table_properties(self):
        ddl = generate_athena_ddl("db", "t", [], "s3://b/", table_properties={"k": "v"})
        assert "TBLPROPERTIES" in ddl
        assert "'k'='v'" in ddl


# ── generate_redshift_ddl ────────────────────────────────────────────
class TestGenerateRedshiftDDL:
    def test_basic(self):
        cols = [{"Name": "id", "Type": "BIGINT"}]
        ddl = generate_redshift_ddl("public", "users", cols)
        assert "CREATE TABLE IF NOT EXISTS public.users" in ddl
        assert "id BIGINT" in ddl
        assert "DISTSTYLE" not in ddl  # AUTO is default → omitted

    def test_not_null_and_encode(self):
        cols = [{"Name": "id", "Type": "BIGINT", "NotNull": True, "Encode": "AZ64"}]
        ddl = generate_redshift_ddl("s", "t", cols)
        assert "NOT NULL" in ddl
        assert "ENCODE AZ64" in ddl

    def test_distribution_style(self):
        ddl = generate_redshift_ddl("s", "t", [], distribution_style="KEY")
        assert "DISTSTYLE KEY" in ddl

    def test_sort_key(self):
        ddl = generate_redshift_ddl("s", "t", [], sort_key=["ts", "id"])
        assert "SORTKEY (ts, id)" in ddl


# ── extract_partition_columns ────────────────────────────────────────
class TestExtractPartitionColumns:
    def test_no_partitions(self):
        schema = [{"name": "id", "type": "integer"}, {"name": "val", "type": "string"}]
        data, parts = extract_partition_columns(schema)
        assert len(data) == 2
        assert len(parts) == 0

    def test_with_partitions(self):
        schema = [
            {"name": "id", "type": "integer"},
            {"name": "dt", "type": "date"},
            {"name": "region", "type": "string", "description": "AWS region"},
        ]
        data, parts = extract_partition_columns(schema, partition_keys=["dt", "region"])
        assert len(data) == 1
        assert data[0]["Name"] == "id"
        assert len(parts) == 2
        names = {p["Name"] for p in parts}
        assert names == {"dt", "region"}
        # Check description carried through for partition col
        region_part = next(p for p in parts if p["Name"] == "region")
        assert region_part["Comment"] == "AWS region"
