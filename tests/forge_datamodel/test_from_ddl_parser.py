# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import pytest

from fluid_build.forge_datamodel.from_ddl.parser import DDLParser


@pytest.mark.xfail(
    strict=False,
    reason="emitter behavior shift with v0.7.3 default; lands in PR-3 (build_runners + matching emitter update)",
)
def test_fallback_parser_handles_basic_create_table():
    ddl = """
    CREATE TABLE orders (
        order_id VARCHAR(64) PRIMARY KEY,
        customer_id VARCHAR(64) NOT NULL,
        order_total DECIMAL(18,2)
    );
    """
    parser = DDLParser()
    tables = parser.parse_ddl_content(ddl)
    assert len(tables) == 1
    table = tables[0]
    assert table.name == "orders"
    assert table.primary_keys == ["order_id"]
    assert [column.name for column in table.columns] == [
        "order_id",
        "customer_id",
        "order_total",
    ]


def test_fallback_parser_handles_table_level_primary_key():
    ddl = """
    CREATE TABLE customers (
        customer_id VARCHAR(64),
        customer_name STRING,
        PRIMARY KEY (customer_id)
    );
    """
    parser = DDLParser()
    tables = parser.parse_ddl_content(ddl)
    assert tables[0].primary_keys == ["customer_id"]


def test_fallback_parser_handles_snowflake_get_ddl_create_or_replace():
    ddl = """
    create or replace TABLE "BIZ_LAB"."SEEDED"."ACCOUNT" (
        "ACCOUNT_ID" VARCHAR(16777216) NOT NULL,
        "ACCOUNT_NAME" VARCHAR(16777216),
        "OPENED_AT" TIMESTAMP_NTZ(9),
        primary key ("ACCOUNT_ID")
    );
    """
    parser = DDLParser()
    tables = parser.parse_ddl_content(ddl, dialect="snowflake")
    assert len(tables) == 1
    assert tables[0].name == "ACCOUNT"
    assert tables[0].primary_keys == ["ACCOUNT_ID"]
    assert [column.name for column in tables[0].columns] == [
        "ACCOUNT_ID",
        "ACCOUNT_NAME",
        "OPENED_AT",
    ]
