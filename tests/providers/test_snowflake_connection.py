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

"""Unit tests for Snowflake session initialization hardening."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import fluid_build.providers.snowflake.connection as connection_mod
from fluid_build.providers.snowflake.connection import SnowflakeConnection
from fluid_build.providers.snowflake.types import ProviderOptions


class _CursorStub:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):
        self._conn.statements.append(sql)
        if self._conn.fail_on == sql:
            raise RuntimeError("boom")


class _ConnectionStub:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.statements = []
        self.closed = False

    def cursor(self):
        return _CursorStub(self)

    def close(self):
        self.closed = True


def _opts(**overrides):
    values = {
        "account": "xy12345",
        "user": "svc_forge",
        "password": "hunter2",
        "role": "TRANSFORMER",
        "warehouse": "TRANSFORM_WH",
        "database": "ANALYTICS",
        "schema": "CURATED",
    }
    values.update(overrides)
    return ProviderOptions(**values)


def test_connect_initializes_session_with_explicit_use_order(monkeypatch):
    conn = _ConnectionStub()

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    result = SnowflakeConnection(_opts())._connect()

    assert result is conn
    assert conn.statements == [
        "USE ROLE TRANSFORMER",
        "USE WAREHOUSE TRANSFORM_WH",
        "USE DATABASE ANALYTICS",
        "USE SCHEMA CURATED",
    ]


def test_connect_closes_connection_when_session_init_statement_fails(monkeypatch):
    conn = _ConnectionStub(fail_on="USE DATABASE ANALYTICS")

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    with pytest.raises(RuntimeError, match="Snowflake session initialization failed"):
        SnowflakeConnection(_opts())._connect()

    assert conn.closed is True
    assert conn.statements == [
        "USE ROLE TRANSFORMER",
        "USE WAREHOUSE TRANSFORM_WH",
        "USE DATABASE ANALYTICS",
    ]


def test_connect_fails_fast_on_invalid_configured_session_identifier(monkeypatch):
    conn = _ConnectionStub()

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    with pytest.raises(RuntimeError, match="Invalid Snowflake warehouse configured"):
        SnowflakeConnection(_opts(warehouse="BAD WAREHOUSE"))._connect()

    assert conn.closed is True
    assert conn.statements == []


def test_connect_accepts_dotted_database_and_schema(monkeypatch):
    """Regression: Snowflake accepts dotted ``DB.SCHEMA`` identifiers in
    ``USE`` statements, so the session initializer must not reject them."""
    conn = _ConnectionStub()

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    result = SnowflakeConnection(
        _opts(database="ANALYTICS.RAW", schema="STAGING.INBOUND")
    )._connect()

    assert result is conn
    assert conn.statements == [
        "USE ROLE TRANSFORMER",
        "USE WAREHOUSE TRANSFORM_WH",
        "USE DATABASE ANALYTICS.RAW",
        "USE SCHEMA STAGING.INBOUND",
    ]


def test_connect_skips_schema_initialization_without_database(monkeypatch):
    """Schema initialization requires a current database in Snowflake."""
    conn = _ConnectionStub()

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    result = SnowflakeConnection(_opts(database=None, schema="PUBLIC"))._connect()

    assert result is conn
    assert conn.statements == [
        "USE ROLE TRANSFORMER",
        "USE WAREHOUSE TRANSFORM_WH",
    ]


def test_connect_rejects_dotted_identifier_with_invalid_segment(monkeypatch):
    """Each segment of a dotted identifier must still pass validate_ident —
    an empty segment (``ANALYTICS..RAW``) or an injection-like segment is
    rejected and the connection closed."""
    conn = _ConnectionStub()

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    with pytest.raises(RuntimeError, match="Invalid Snowflake database configured"):
        SnowflakeConnection(_opts(database="ANALYTICS..RAW"))._connect()

    assert conn.closed is True


def test_connect_rejects_quoted_segment_in_dotted_identifier(monkeypatch):
    """A dotted identifier segment containing a quote or injection metacharacter
    must be rejected — the relaxation must not open an injection surface."""
    conn = _ConnectionStub()

    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kwargs: conn)),
    )

    with pytest.raises(RuntimeError, match="Invalid Snowflake schema configured"):
        SnowflakeConnection(_opts(schema='PUBLIC."EVIL; DROP"'))._connect()

    assert conn.closed is True
