# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Mocked unit tests for the new Postgres + Athena drivers.

Live Postgres tests (against the dockerized container in
``examples/mcp-output-port-docker/docker-compose.yml``) live in the
e2e script — these tests pin the in-process contract: registry
binding, descriptor shape, parameter rewrite, identifier validation,
and connection-options layering. They run on every CI cycle without
any cloud creds or Docker.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.output_ports.mcp.drivers import (
    AthenaDriver,
    PostgresDriver,
    UnsupportedBindingError,
    build_driver,
    supported_keys,
)

# ---------------------------------------------------------------------
# Registry — both drivers reachable via build_driver
# ---------------------------------------------------------------------


def test_registry_includes_postgres_and_athena_keys():
    keys = set(supported_keys())
    assert ("postgres", "postgres_table") in keys
    assert ("postgres", "table") in keys
    assert ("aws", "athena_table") in keys
    assert ("aws", "glue_table") in keys


def test_build_driver_resolves_postgres():
    expose = {
        "exposeId": "demo",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {"database": "db", "schema": "public", "table": "t"},
        },
    }
    driver = build_driver(expose=expose, contract={})
    assert isinstance(driver, PostgresDriver)


def test_build_driver_resolves_athena():
    expose = {
        "exposeId": "demo",
        "binding": {
            "platform": "aws",
            "format": "athena_table",
            "location": {"database": "demo_db", "table": "demo_table"},
        },
    }
    driver = build_driver(expose=expose, contract={})
    assert isinstance(driver, AthenaDriver)


# ---------------------------------------------------------------------
# Postgres driver — descriptor + parameter rewrite + lifecycle
# ---------------------------------------------------------------------


def _pg_expose():
    return {
        "exposeId": "demo",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {"database": "appdb", "schema": "public", "table": "users"},
        },
    }


def test_postgres_descriptor_renders_qualified_table_reference():
    driver = PostgresDriver(expose=_pg_expose(), contract={})
    descriptor = driver.descriptor()
    assert descriptor.platform == "postgres"
    assert descriptor.dialect == "postgres"
    assert descriptor.table_reference == "appdb.public.users"
    assert descriptor.capabilities["sample"] is True


def test_postgres_rejects_non_postgres_platform():
    expose = {
        "exposeId": "demo",
        "binding": {
            "platform": "snowflake",
            "format": "postgres_table",
            "location": {"database": "x", "schema": "y", "table": "z"},
        },
    }
    with pytest.raises(UnsupportedBindingError, match="postgres"):
        PostgresDriver(expose=expose, contract={})


def test_postgres_rejects_invalid_identifier():
    expose = {
        "exposeId": "demo",
        "binding": {
            "platform": "postgres",
            "format": "postgres_table",
            "location": {
                "database": "appdb",
                "schema": "public",
                "table": "drop;table",  # injection attempt
            },
        },
    }
    with pytest.raises(Exception):  # SQL safety raises a typed error
        PostgresDriver(expose=expose, contract={})


def test_postgres_execute_rewrites_named_placeholders_to_psycopg_form():
    """Compiler emits ``:p_<index>``; driver must rewrite to
    ``%(p_<index>)s`` for psycopg's named-parameter binding."""
    driver = PostgresDriver(expose=_pg_expose(), contract={})
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda self: self
    fake_cursor.__exit__ = lambda self, *a: None
    fake_cursor.description = [MagicMock(name="customer_id")]
    fake_cursor.description[0].name = "customer_id"
    fake_cursor.fetchall.return_value = [("C1",)]
    fake_conn.cursor.return_value = fake_cursor
    driver._connection = fake_conn

    sql_in = 'SELECT customer_id FROM "public"."users" WHERE segment = :p_0'
    result = driver.execute(sql=sql_in, params=("smb",))

    # The cursor should have been called with the rewritten SQL.
    called_sql = fake_cursor.execute.call_args_list[-1].args[0]
    assert "%(p_0)s" in called_sql
    assert ":p_0" not in called_sql
    assert result.columns == ("customer_id",)
    assert result.rows == ({"customer_id": "C1"},)


def test_postgres_a_literal_percent_survives_pyformat_binding():
    """psycopg reads a statement with bound parameters as a pyformat string, so a ``%``
    in a LIKE pattern is doubled; with no parameters it is left as written."""
    driver = PostgresDriver(expose=_pg_expose(), contract={})
    fake_conn = MagicMock()
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda self: self
    fake_cursor.__exit__ = lambda self, *a: None
    fake_cursor.description = []
    fake_cursor.fetchall.return_value = []
    fake_conn.cursor.return_value = fake_cursor
    driver._connection = fake_conn

    sql = "SELECT count(*) FROM t WHERE (region LIKE 'EU%') AND (segment = :p_0)"
    driver.execute(sql=sql, params=("smb",))
    called_sql, bound = fake_cursor.execute.call_args_list[-1].args
    assert "LIKE 'EU%%'" in called_sql and bound == {"p_0": "smb"}
    assert called_sql % {"p_0": "'smb'"} == sql.replace(":p_0", "'smb'")
    driver.execute(sql="SELECT count(*) FROM t WHERE region LIKE 'EU%'", params=())
    assert fake_cursor.execute.call_args_list[-1].args == (
        "SELECT count(*) FROM t WHERE region LIKE 'EU%'",
    )


def test_postgres_close_is_idempotent():
    driver = PostgresDriver(expose=_pg_expose(), contract={})
    fake_conn = MagicMock()
    driver._connection = fake_conn
    driver.close()
    driver.close()  # second call must not raise
    fake_conn.close.assert_called_once()


# ---------------------------------------------------------------------
# Athena driver — descriptor, parameter rewrite, region resolution
# ---------------------------------------------------------------------


def _athena_expose():
    return {
        "exposeId": "demo",
        "binding": {
            "platform": "aws",
            "format": "athena_table",
            "location": {"database": "analytics", "table": "events"},
        },
    }


def test_athena_descriptor_uses_quoted_table_reference():
    driver = AthenaDriver(expose=_athena_expose(), contract={})
    descriptor = driver.descriptor()
    assert descriptor.platform == "aws"
    assert descriptor.format == "athena_table"
    assert descriptor.table_reference == '"analytics"."events"'
    assert descriptor.dialect == "athena"


def test_athena_rejects_non_aws_platform():
    expose = {
        "exposeId": "demo",
        "binding": {
            "platform": "gcp",
            "format": "athena_table",
            "location": {"database": "x", "table": "y"},
        },
    }
    with pytest.raises(UnsupportedBindingError, match="aws"):
        AthenaDriver(expose=expose, contract={})


def test_athena_resolves_region_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    driver = AthenaDriver(expose=_athena_expose(), contract={})
    assert driver._region == "eu-west-1"


def test_athena_execute_substitutes_named_to_positional_placeholders():
    driver = AthenaDriver(expose=_athena_expose(), contract={})
    fake_client = MagicMock()
    fake_client.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    fake_client.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    fake_client.get_query_results.return_value = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "customer_id"}]},
                {"Data": [{"VarCharValue": "C1"}]},
            ],
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "customer_id"}]},
        }
    }
    driver._client = fake_client

    sql_in = 'SELECT customer_id FROM "analytics"."events" WHERE region = :p_0'
    result = driver.execute(sql=sql_in, params=("us-east",))

    called_kwargs = fake_client.start_query_execution.call_args.kwargs
    assert "?" in called_kwargs["QueryString"]
    assert ":p_0" not in called_kwargs["QueryString"]
    assert called_kwargs["ExecutionParameters"] == ["us-east"]
    assert result.rows == ({"customer_id": "C1"},)


def test_athena_health_check_pings_workgroup_listing():
    driver = AthenaDriver(expose=_athena_expose(), contract={})
    fake_client = MagicMock()
    fake_client.list_work_groups.return_value = {"WorkGroups": []}
    driver._client = fake_client
    health = driver.health_check()
    assert health["status"] == "ok"
    assert health["engine"] == "athena"
    fake_client.list_work_groups.assert_called_once()


def test_postgres_runs_each_request_in_a_read_only_transaction_it_rolls_back():
    """In autocommit neither the connection's read-only flag nor a SET LOCAL reaches the
    next statement. Each request is its own read-only transaction instead, rolled back
    so no session setting a statement changes outlives it."""
    driver = PostgresDriver(expose=_pg_expose(), contract={})
    events = []
    fake_cursor = MagicMock()
    fake_cursor.__enter__ = lambda self: self
    fake_cursor.__exit__ = lambda self, *a: None
    fake_cursor.description = []
    fake_cursor.fetchall.return_value = []
    fake_cursor.execute.side_effect = lambda statement, *a: events.append(statement)
    transaction = MagicMock()
    transaction.__enter__ = lambda self: events.append("BEGIN")
    transaction.__exit__ = lambda self, *a: events.append("ROLLBACK")
    fake_conn = MagicMock()
    fake_conn.transaction.return_value = transaction
    fake_conn.cursor.return_value = fake_cursor
    driver._connection = fake_conn

    driver.execute(sql="SELECT id FROM t", params=(), timeout_seconds=2)

    fake_conn.transaction.assert_called_once_with(force_rollback=True)
    assert events == [
        "BEGIN",
        "SET TRANSACTION READ ONLY",
        "SET LOCAL statement_timeout = 2000",
        "SELECT id FROM t",
        "ROLLBACK",
    ]


def _connecting(monkeypatch, role_row, *, filtered):
    """A PostgresDriver whose psycopg connects to a fake server; the role query answers
    ``role_row`` (``rolsuper``, the privileged roles it is a member of)."""
    import sys
    import types

    cursor = MagicMock()
    cursor.__enter__ = lambda self: self
    cursor.__exit__ = lambda self, *a: None
    cursor.fetchall.return_value = [role_row]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    monkeypatch.setitem(
        sys.modules, "psycopg", types.SimpleNamespace(connect=lambda **_: connection)
    )
    expose = _pg_expose()
    if filtered:
        expose["policy"] = {
            "authz": {"rowFilters": [{"principal": "group:eu@x.example", "where": "id > 0"}]}
        }
    return PostgresDriver(expose=expose, contract={}), connection


@pytest.mark.parametrize(
    ("role_row", "named"),
    [((True, []), "superuser"), ((False, ["pg_read_server_files"]), "pg_read_server_files")],
    ids=["superuser", "reads-server-files"],
)
def test_postgres_refuses_to_serve_row_filters_as_a_privileged_role(monkeypatch, role_row, named):
    """Such a role can read a table's files and the write-ahead log, past any filter."""
    from fluid_build.output_ports.mcp.query_compiler import RowFilterUnavailable

    driver, connection = _connecting(monkeypatch, role_row, filtered=True)
    with pytest.raises(RowFilterUnavailable, match=named):
        driver._get_connection()
    connection.close.assert_called_once()
    assert driver._connection is None


def test_postgres_serves_row_filters_as_a_role_that_reads_only_tables(monkeypatch):
    driver, connection = _connecting(monkeypatch, (False, []), filtered=True)
    assert driver._get_connection() is connection


def test_postgres_warns_about_a_privileged_role_on_an_expose_without_row_filters(
    monkeypatch, caplog
):
    driver, connection = _connecting(monkeypatch, (False, ["pg_monitor"]), filtered=False)
    with caplog.at_level("WARNING"):
        assert driver._get_connection() is connection
    assert "postgres_privileged_role" in caplog.text and "pg_monitor" in caplog.text
