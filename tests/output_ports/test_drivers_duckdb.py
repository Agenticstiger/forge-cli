# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Real-engine integration tests for the DuckDB driver.

These tests exercise the full path from a CSV fixture through the
driver's ``CREATE OR REPLACE VIEW`` setup all the way to a
parameterised query result. No cloud creds required — the entire
suite runs against an in-memory DuckDB.
"""

from __future__ import annotations

from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp.drivers import build_driver
from fluid_build.output_ports.mcp.drivers.duckdb import DuckDBDriver
from fluid_build.output_ports.mcp.query_compiler import compile_semantic_query

from ._fixtures import make_expose, write_customer_csv


def _expose_for_csv(csv_path: Path):
    return make_expose(
        semantics={
            "name": "customer_profiles",
            "measures": [
                {"name": "customer_count", "agg": "count_distinct", "expr": "customer_id"},
                {"name": "total_ltv_usd", "agg": "sum", "expr": "lifetime_value_usd"},
            ],
            "dimensions": [
                {"name": "signup_date", "type": "time"},
            ],
            "metrics": [
                {"name": "active_customers", "type": "simple", "measure": "customer_count"},
            ],
        },
        binding={
            "platform": "local",
            "format": "csv",
            "location": {
                "path": str(csv_path),
                "table": "customer_profiles",
            },
        },
    )


def _build_duckdb_driver(tmp_path):
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = _expose_for_csv(csv_path)
    return build_driver(expose=expose, contract={"exposes": [expose]}), expose


def test_descriptor_returns_table_reference(tmp_path):
    driver, _ = _build_duckdb_driver(tmp_path)
    descriptor = driver.descriptor()
    assert descriptor.platform == "local"
    assert descriptor.dialect == "duckdb"
    assert descriptor.table_reference == "customer_profiles"


def test_health_check_succeeds(tmp_path):
    driver, _ = _build_duckdb_driver(tmp_path)
    health = driver.health_check()
    assert health["status"] == "ok"
    assert "latency_ms" in health


def test_sample_returns_three_rows(tmp_path):
    driver, _ = _build_duckdb_driver(tmp_path)
    result = driver.sample(limit=10)
    assert result.columns
    assert len(result.rows) == 3
    assert result.truncated is False
    customer_ids = sorted(row["customer_id"] for row in result.rows)
    assert customer_ids == ["C0001", "C0002", "C0003"]


def test_sample_truncated_when_at_cap(tmp_path):
    driver, _ = _build_duckdb_driver(tmp_path)
    result = driver.sample(limit=2)
    assert len(result.rows) == 2
    assert result.truncated is True


def test_query_with_metric_and_dimension(tmp_path):
    driver, expose = _build_duckdb_driver(tmp_path)
    descriptor = driver.descriptor()
    compiled = compile_semantic_query(
        expose=expose,
        metric="active_customers",
        dimensions=["signup_date"],
        limit=10,
        table_reference=descriptor.table_reference,
    )
    rendered = compiled.render_sql_for_dialect(descriptor.dialect)
    result = driver.query(sql=rendered, params=compiled.params, projection=compiled.columns)
    assert "customer_count" in result.columns
    assert all("signup_date" in row for row in result.rows)


def test_query_with_filter_uses_parameter_binding(tmp_path):
    driver, expose = _build_duckdb_driver(tmp_path)
    descriptor = driver.descriptor()
    compiled = compile_semantic_query(
        expose=expose,
        measure="total_ltv_usd",
        dimensions=[],
        filters={"signup_date": "2024-02-10"},
        limit=10,
        table_reference=descriptor.table_reference,
    )
    rendered = compiled.render_sql_for_dialect(descriptor.dialect)
    result = driver.query(sql=rendered, params=compiled.params, projection=compiled.columns)
    assert len(result.rows) == 1
    assert result.rows[0]["total_ltv_usd"] == 850.0


def test_column_restriction_drops_email(tmp_path):
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = _expose_for_csv(csv_path)
    expose.setdefault("policy", {}).setdefault("authz", {})["columnRestrictions"] = [
        {"principal": "*", "columns": ["email"], "access": "deny"}
    ]
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    result = driver.sample(limit=10)
    assert "email" not in result.columns
    for row in result.rows:
        assert "email" not in row


def test_privacy_masking_drops_column_in_phase_1(tmp_path):
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = _expose_for_csv(csv_path)
    expose.setdefault("policy", {}).setdefault("privacy", {})["masking"] = [
        {"column": "lifetime_value_usd", "strategy": "hash"}
    ]
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    result = driver.sample(limit=10)
    assert "lifetime_value_usd" not in result.columns


def test_unsupported_binding_raises(tmp_path):
    expose = make_expose(
        binding={
            "platform": "wonderland",
            "format": "magic_table",
            "location": {"name": "x"},
        }
    )
    from fluid_build.output_ports.mcp.drivers.base import UnsupportedBindingError

    with pytest.raises(UnsupportedBindingError, match="No driver registered"):
        build_driver(expose=expose, contract={"exposes": [expose]})
