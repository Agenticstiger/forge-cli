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

"""Tests for Snowflake naming utilities and type definitions."""

import pytest

from fluid_build.providers.snowflake.types import (
    AuthenticationType,
    DeploymentStrategy,
    OperationResult,
    SnowflakeIdentifier,
    TableColumn,
    ValidationResult,
)
from fluid_build.providers.snowflake.util.names import (
    build_qualified_name,
    normalize_column_name,
    normalize_database_name,
    normalize_schema_name,
    normalize_table_name,
    quote_identifier,
)


class TestNormalizeDatabaseName:
    def test_basic_name(self):
        assert normalize_database_name("my_db") == "MY_DB"

    def test_hyphens_replaced(self):
        assert normalize_database_name("my-data-db") == "MY_DATA_DB"

    def test_special_chars_removed(self):
        assert normalize_database_name("my.db!@#$") == "MYDB"

    def test_starts_with_digit_gets_underscore(self):
        assert normalize_database_name("123db") == "_123DB"

    def test_truncated_to_255(self):
        long_name = "A" * 300
        result = normalize_database_name(long_name)
        assert len(result) == 255

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="cannot be empty"):
            normalize_database_name("")

    def test_all_invalid_chars_raises(self):
        with pytest.raises(ValueError, match="Invalid database name"):
            normalize_database_name("!@#$%^&*()")

    def test_uppercase_preserved(self):
        assert normalize_database_name("ALREADY_UPPER") == "ALREADY_UPPER"


class TestNormalizeWrappers:
    def test_schema_name(self):
        assert normalize_schema_name("my-schema") == "MY_SCHEMA"

    def test_table_name(self):
        assert normalize_table_name("my-table") == "MY_TABLE"

    def test_column_name(self):
        assert normalize_column_name("my-col") == "MY_COL"


class TestQuoteIdentifier:
    def test_already_quoted(self):
        assert quote_identifier('"FOO"') == '"FOO"'

    def test_uppercase_no_quoting(self):
        assert quote_identifier("FOO") == "FOO"

    def test_mixed_case_quoted(self):
        assert quote_identifier("Foo") == '"Foo"'

    def test_starts_with_underscore_quoted(self):
        result = quote_identifier("_FOO")
        assert result == '"_FOO"'

    def test_special_chars_quoted(self):
        result = quote_identifier("FOO BAR")
        assert result == '"FOO BAR"'

    def test_internal_double_quotes_escaped(self):
        result = quote_identifier('foo"bar')
        assert result == '"foo""bar"'


class TestBuildQualifiedName:
    def test_full_qualified(self):
        result = build_qualified_name("DB", "SCHEMA", "TABLE")
        assert result == "DB.SCHEMA.TABLE"

    def test_schema_and_table(self):
        result = build_qualified_name(schema="SCHEMA", name="TABLE")
        assert result == "SCHEMA.TABLE"

    def test_name_only(self):
        result = build_qualified_name(name="TABLE")
        assert result == "TABLE"

    def test_empty(self):
        result = build_qualified_name()
        assert result == ""

    def test_mixed_case_gets_quoted(self):
        result = build_qualified_name("MyDb", "MySchema", "MyTable")
        assert '"MyDb"' in result


class TestSnowflakeIdentifier:
    def test_fqtn(self):
        ident = SnowflakeIdentifier("DB", "SCH", "TBL")
        assert ident.fqtn() == '"DB"."SCH"."TBL"'

    def test_validate_valid(self):
        ident = SnowflakeIdentifier("DB", "SCH", "TBL")
        ident.validate()  # should not raise

    def test_validate_empty_database(self):
        ident = SnowflakeIdentifier("", "SCH", "TBL")
        with pytest.raises(ValueError, match="non-empty"):
            ident.validate()

    def test_validate_invalid_chars(self):
        ident = SnowflakeIdentifier("DB/BAD", "SCH", "TBL")
        with pytest.raises(ValueError, match="Invalid Snowflake identifier"):
            ident.validate()

    def test_is_valid_identifier_special_chars(self):
        ident = SnowflakeIdentifier("DB", "SCH", "TBL")
        assert ident._is_valid_identifier("valid_name") is True
        assert ident._is_valid_identifier("bad/name") is False
        assert ident._is_valid_identifier("") is False


class TestTableColumn:
    def test_basic_ddl(self):
        col = TableColumn(name="id", type="NUMBER")
        assert col.to_ddl_fragment() == '"id" NUMBER'

    def test_not_null(self):
        col = TableColumn(name="id", type="NUMBER", nullable=False)
        assert "NOT NULL" in col.to_ddl_fragment()

    def test_default_value(self):
        col = TableColumn(name="status", type="VARCHAR", default_value="'active'")
        assert "DEFAULT 'active'" in col.to_ddl_fragment()

    def test_check_constraint(self):
        col = TableColumn(name="age", type="NUMBER", check_constraint="age > 0")
        assert "CHECK (age > 0)" in col.to_ddl_fragment()

    def test_comment_from_description(self):
        col = TableColumn(name="id", type="NUMBER", description="Primary key")
        ddl = col.to_ddl_fragment()
        assert "COMMENT 'Primary key'" in ddl

    def test_comment_escapes_quotes(self):
        col = TableColumn(name="x", type="VARCHAR", comment="it's a test")
        ddl = col.to_ddl_fragment()
        assert "it''s a test" in ddl

    def test_full_ddl(self):
        col = TableColumn(
            name="price",
            type="DECIMAL(10,2)",
            nullable=False,
            default_value="0.00",
            check_constraint="price >= 0",
            comment="Product price",
        )
        ddl = col.to_ddl_fragment()
        assert '"price" DECIMAL(10,2)' in ddl
        assert "NOT NULL" in ddl
        assert "DEFAULT 0.00" in ddl
        assert "CHECK (price >= 0)" in ddl
        assert "COMMENT 'Product price'" in ddl


class TestValidationResult:
    def test_initial_state(self):
        vr = ValidationResult(valid=True)
        assert vr.valid is True
        assert vr.warnings == []
        assert vr.errors == []

    def test_add_warning(self):
        vr = ValidationResult(valid=True)
        vr.add_warning("test warning")
        assert len(vr.warnings) == 1
        assert vr.valid is True

    def test_add_error_sets_invalid(self):
        vr = ValidationResult(valid=True)
        vr.add_error("test error")
        assert vr.valid is False
        assert len(vr.errors) == 1

    def test_add_info(self):
        vr = ValidationResult(valid=True)
        vr.add_info("test info")
        assert len(vr.info) == 1


class TestOperationResult:
    def test_creation(self):
        result = OperationResult(success=True, operation="create", duration=1.5)
        assert result.success is True
        assert result.resources_created == 0
        assert result.credits_consumed == 0.0


class TestEnums:
    def test_auth_types(self):
        assert AuthenticationType.PASSWORD.value == "password"
        assert AuthenticationType.KEY_PAIR.value == "key_pair"

    def test_deployment_strategies(self):
        assert DeploymentStrategy.BLUE_GREEN.value == "blue_green"
        assert DeploymentStrategy.CANARY.value == "canary"
