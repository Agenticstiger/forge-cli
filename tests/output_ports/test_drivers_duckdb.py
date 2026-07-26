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
    result = driver.query(compiled=compiled)
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
    result = driver.query(compiled=compiled)
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


# ---------------------------------------------------------------------
# Handler-wiring regression tests (keyless, run in CI).
#
# These drive the actual MCP tool handlers (``_handlers.tool_query`` /
# ``tool_query_sql``) end-to-end against DuckDB — handler → compiler →
# driver. The tests ABOVE call the compiler and the driver DIRECTLY,
# which is why a long-standing wiring bug went unnoticed for a full
# release: the handler called ``compile_semantic_query`` /
# ``compile_free_form_sql`` with ``arguments=`` / ``descriptor=``
# kwargs they never accepted, and called ``driver.query`` with
# ``compiled=`` against a ``(sql, params, projection)`` signature — so
# every real ``query`` / ``query_sql`` MCP call raised TypeError before
# reaching the engine, while these driver-level tests stayed green.
# Only the creds-gated Snowflake integration tests (which CI skips)
# exercised the handler path. These keyless tests close that gap.
# ---------------------------------------------------------------------


def _build_session_state(tmp_path):
    """A ``SessionState`` bound to the keyless DuckDB/CSV expose, used
    to drive the tool handlers end-to-end."""
    import logging

    from fluid_build.output_ports.mcp.policy import OutputPortPolicy
    from fluid_build.output_ports.mcp.server import SessionState

    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = _expose_for_csv(csv_path)
    return SessionState(
        contract={"exposes": [expose]},
        expose=expose,
        # The fixture CSV lives under tmp_path, not cwd; ``--readable-paths``
        # is enforced by the driver now, so name the real data directory.
        policy=OutputPortPolicy.from_contract_and_flags(
            expose=expose, readable_paths=(tmp_path.resolve(),)
        ),
        logger=logging.getLogger("test.output_port.handlers"),
    )


def test_tool_query_handler_executes_end_to_end(tmp_path):
    """``tool_query`` must call ``compile_semantic_query`` + ``driver.query``
    with the signatures they actually expose (regression for the
    TypeError-on-every-query wiring bug)."""
    from fluid_build.output_ports.mcp import _handlers

    state = _build_session_state(tmp_path)
    payload = _handlers.tool_query(
        state,
        {"measure": "customer_count", "dimensions": ["signup_date"], "limit": 10},
    )
    assert payload["rowCount"] >= 1
    assert "customer_count" in payload["columns"]
    assert "GROUP BY" in payload["compiled"]["sql"].upper()


def test_tool_query_handler_defaults_missing_limit(tmp_path):
    """A missing ``limit`` (the tool schema declares no default) falls
    back to the server cap instead of crashing the compiler, which
    rejects ``None``."""
    from fluid_build.output_ports.mcp import _handlers

    state = _build_session_state(tmp_path)
    payload = _handlers.tool_query(state, {"measure": "customer_count"})
    assert payload["rowCount"] >= 1
    assert "customer_count" in payload["columns"]


def test_tool_query_sql_handler_executes_end_to_end(tmp_path):
    """Same regression as ``tool_query`` but for the free-form
    ``query_sql`` handler + ``compile_free_form_sql``."""
    from fluid_build.output_ports.mcp import _handlers

    state = _build_session_state(tmp_path)
    payload = _handlers.tool_query_sql(state, {"sql": "SELECT customer_id FROM customer_profiles"})
    assert payload["rowCount"] == 3
    assert "customer_id" in payload["columns"]


def test_tool_query_sql_handler_blocks_pii_alias_bypass(tmp_path):
    """Security: a free-form SELECT that aliases a PII column to dodge
    the row-level redactor (``SELECT email AS not_email``) is rejected
    at compile time — the handler feeds the union of restricted + PII
    columns into ``compile_free_form_sql``."""
    from fluid_build.output_ports.mcp import _handlers

    state = _build_session_state(tmp_path)
    with pytest.raises(ValueError, match="email"):
        _handlers.tool_query_sql(state, {"sql": "SELECT email AS not_email FROM customer_profiles"})


# ---------------------------------------------------------------------
# --readable-paths confinement for binding.location.dbFile.
#
# Regression for a read-side sandbox escape: `path` and `attach` were
# gated against the allowlist but `dbFile` was opened raw, so a served
# contract could set `dbFile` to any host path and the driver would
# read it (read_only=True stops writes, not reads). `dbFile` now flows
# through the same _path_is_under gate as `path`/`attach`.
# ---------------------------------------------------------------------
def _expose_with_db_file(db_file: str):
    return make_expose(
        binding={
            "platform": "local",
            "format": "other",
            "location": {"table": "customer_profiles", "dbFile": db_file},
        }
    )


def test_db_file_outside_readable_paths_is_rejected(tmp_path):
    from fluid_build.output_ports.mcp.drivers.base import UnsupportedBindingError

    outside = tmp_path / "outside" / "secret.duckdb"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"")  # a real file on disk, still outside the allowlist
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    expose = _expose_with_db_file(str(outside))
    with pytest.raises(UnsupportedBindingError, match="dbFile.*outside --readable-paths"):
        DuckDBDriver(
            expose=expose,
            contract={"exposes": [expose]},
            readable_paths=(allowed.resolve(),),
        )


def test_db_file_inside_readable_paths_is_accepted(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    inside = allowed / "ok.duckdb"
    inside.write_bytes(b"")

    expose = _expose_with_db_file(str(inside))
    driver = DuckDBDriver(
        expose=expose,
        contract={"exposes": [expose]},
        readable_paths=(allowed.resolve(),),
    )
    # Resolved + retained as the connection target (not silently dropped).
    assert driver._db_file == inside.resolve()


def test_db_file_memory_sentinel_is_passed_through(tmp_path):
    expose = _expose_with_db_file(":memory:")
    driver = DuckDBDriver(
        expose=expose,
        contract={"exposes": [expose]},
        readable_paths=(tmp_path.resolve(),),
    )
    # ':memory:' is not a file — no allowlist gate, connects in-memory.
    assert driver._db_file is None


def test_db_file_unrestricted_when_no_readable_paths(tmp_path):
    # No --readable-paths configured ⇒ no confinement to enforce (parity with
    # how `path`/`attach` behave: the gate only applies when roots are set).
    anywhere = tmp_path / "anywhere.duckdb"
    anywhere.write_bytes(b"")
    expose = _expose_with_db_file(str(anywhere))
    driver = DuckDBDriver(expose=expose, contract={"exposes": [expose]})
    assert driver._db_file == anywhere.resolve()
