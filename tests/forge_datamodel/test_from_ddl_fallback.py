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

"""Coverage for the regex DDL fallback in ``forge_datamodel/from_ddl/parser.py``.

``DDLParser.parse_ddl_content`` is sqlglot-first: when sqlglot is installed
(it is, in the dev/CI environment) every well-formed ``CREATE TABLE`` is
handled by ``_parse_with_sqlglot`` and the hand-rolled regex fallback
(``_parse_with_fallback`` -> ``_parse_table_definition`` ->
``_parse_column_definition``) is never reached — which is why the existing
``test_from_ddl_parser.py`` tests, despite their names, exercise the sqlglot
path.

That regex fallback is real production behavior on hosts where sqlglot is
absent or where ``sqlglot.parse`` raises. These tests force it by stubbing
the sqlglot branch to return no tables — exactly the ``[]`` that
``_parse_with_sqlglot`` yields when ``import sqlglot`` fails or the parse
throws — so the fallback chain is genuinely covered.
"""

from __future__ import annotations

import pytest

from fluid_build.forge_datamodel.from_ddl.parser import (
    DDLParser,
    ParsedDDL,
    infer_sqlglot_dialect,
    parse_ddl_text,
)


def _fallback_parser(monkeypatch) -> DDLParser:
    """A ``DDLParser`` pinned to the regex fallback path.

    Stubbing ``_parse_with_sqlglot`` to return ``[]`` reproduces the
    sqlglot-unavailable / sqlglot-parse-failed branch without uninstalling
    the package, so ``parse_ddl_content`` falls through to the regex parser.
    """
    parser = DDLParser()
    monkeypatch.setattr(parser, "_parse_with_sqlglot", lambda *a, **k: [])
    return parser


class TestFallbackColumnParsing:
    """``_parse_column_definition`` — column name, type, qualifiers, flags."""

    def test_basic_columns_with_inline_primary_key(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE orders (
                order_id VARCHAR(64) PRIMARY KEY,
                customer_id VARCHAR(64) NOT NULL,
                order_total DECIMAL(18,2)
            );
            """
        )
        assert len(tables) == 1
        table = tables[0]
        assert table.name == "orders"
        assert [c.name for c in table.columns] == ["order_id", "customer_id", "order_total"]
        assert table.primary_keys == ["order_id"]

        by_name = {c.name: c for c in table.columns}
        assert by_name["order_id"].primary_key is True
        assert by_name["customer_id"].nullable is False
        assert by_name["order_total"].nullable is True
        assert by_name["order_total"].primary_key is False

    def test_varchar_length_qualifier(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE labels (
                label VARCHAR(128),
                code CHAR(3)
            );
            """
        )
        cols = {c.name: c for c in tables[0].columns}
        assert cols["label"].logical_type == "VARCHAR"
        assert cols["label"].qualifiers["length"] == 128
        assert cols["code"].qualifiers["length"] == 3

    def test_decimal_precision_and_scale_qualifiers(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE metrics (
                amount DECIMAL(18,4),
                ratio NUMERIC(9)
            );
            """
        )
        cols = {c.name: c for c in tables[0].columns}
        assert cols["amount"].qualifiers["precision"] == 18
        assert cols["amount"].qualifiers["scale"] == 4
        assert cols["ratio"].qualifiers["precision"] == 9
        assert "scale" not in cols["ratio"].qualifiers

    def test_array_nested_type_qualifier(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE events (
                event_id VARCHAR(32),
                tags ARRAY<STRING>
            );
            """
        )
        tags = {c.name: c for c in tables[0].columns}["tags"]
        assert tags.logical_type == "ARRAY"
        assert tags.qualifiers["nested_type"] == "STRING"

    def test_inline_dash_comment_is_captured(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE accounts (
                account_id VARCHAR(32),  -- the natural key
                status STRING
            );
            """
        )
        cols = {c.name: c for c in tables[0].columns}
        assert cols["account_id"].comment == "the natural key"
        assert cols["status"].comment is None

    def test_options_description_becomes_column_comment(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            "CREATE TABLE t (notes STRING OPTIONS(description='free-form notes'));"
        )
        assert tables[0].columns[0].comment == "free-form notes"


class TestFallbackTableLevelKeys:
    """``_parse_table_definition`` — table-level PRIMARY KEY clauses."""

    def test_table_level_primary_key(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE customers (
                customer_id VARCHAR(64),
                customer_name STRING,
                PRIMARY KEY (customer_id)
            );
            """
        )
        assert tables[0].primary_keys == ["customer_id"]

    def test_named_constraint_composite_primary_key(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE line_items (
                order_id VARCHAR(64),
                line_no VARCHAR(8),
                CONSTRAINT pk_line_items PRIMARY KEY (order_id, line_no)
            );
            """
        )
        assert tables[0].primary_keys == ["order_id", "line_no"]
        # The CONSTRAINT line is not mistaken for a column.
        assert [c.name for c in tables[0].columns] == ["order_id", "line_no"]


class TestFallbackTableShapes:
    """``_parse_with_fallback`` — table discovery, naming, multi-statement."""

    def test_partition_and_cluster_lines_are_skipped(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE t (
                id VARCHAR(16),
                CLUSTER BY (id)
            );
            """
        )
        assert [c.name for c in tables[0].columns] == ["id"]

    def test_schema_qualified_name_reduced_to_simple(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            'CREATE TABLE "WAREHOUSE"."PUBLIC"."SALES" (sale_id VARCHAR(32));'
        )
        assert tables[0].name == "SALES"

    def test_create_or_replace_transient_table(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content("CREATE OR REPLACE TRANSIENT TABLE staging (raw STRING);")
        assert tables[0].name == "staging"

    def test_multiple_tables_in_one_script(self, monkeypatch):
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_content(
            """
            CREATE TABLE a (
                a_id VARCHAR(8)
            );
            CREATE TABLE b (
                b_id VARCHAR(8),
                label STRING
            );
            """
        )
        assert [t.name for t in tables] == ["a", "b"]
        assert [c.name for c in tables[1].columns] == ["b_id", "label"]

    def test_parse_ddl_file_reads_from_disk(self, tmp_path, monkeypatch):
        ddl_path = tmp_path / "schema.sql"
        ddl_path.write_text("CREATE TABLE disk_tbl (id VARCHAR(8));", encoding="utf-8")
        parser = _fallback_parser(monkeypatch)
        tables = parser.parse_ddl_file(str(ddl_path))
        assert [t.name for t in tables] == ["disk_tbl"]


class TestInferSqlglotDialect:
    """``infer_sqlglot_dialect`` — forge source-type -> sqlglot dialect."""

    @pytest.mark.parametrize(
        ("source_type", "expected"),
        [
            ("bigquery", "bigquery"),
            ("snowflake", "snowflake"),
            ("postgres", "postgres"),
            ("postgresql", "postgres"),
            ("mysql", "mysql"),
            ("oracle", "oracle"),
            ("Snowflake", "snowflake"),  # case-insensitive
        ],
    )
    def test_known_source_types_map(self, source_type, expected):
        assert infer_sqlglot_dialect(source_type) == expected

    def test_unknown_source_type_returns_none(self):
        assert infer_sqlglot_dialect("teradata") is None

    def test_empty_source_type_returns_none(self):
        assert infer_sqlglot_dialect(None) is None
        assert infer_sqlglot_dialect("") is None


class TestParseDdlText:
    """``parse_ddl_text`` — the ``ParsedDDL`` convenience wrapper."""

    def test_returns_parsed_ddl_wrapper(self):
        result = parse_ddl_text("CREATE TABLE t (id VARCHAR(8));", dialect="snowflake")
        assert isinstance(result, ParsedDDL)
        assert result.dialect == "snowflake"
        assert [t.name for t in result.tables] == ["t"]
