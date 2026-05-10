# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""ensure_database / ensure_schema bootstrap behaviour against fresh accounts.

The Snowflake provider must be able to CREATE DATABASE and CREATE SCHEMA on
a Snowflake account where neither yet exists. The chicken-and-egg trap: the
Snowflake connector validates ``database`` / ``schema`` set in session params
during connect(), so a connect-with-database call against a non-existent
database fails BEFORE any DDL can run.

These tests pin the fix: ensure_database connects WITHOUT database/schema,
ensure_schema connects WITH database but WITHOUT schema. Both honour
operator overrides via env-var fallbacks normally for downstream actions.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers.snowflake.actions.database import ensure_database
from fluid_build.providers.snowflake.actions.schema import ensure_schema


class _FakeConnFactory:
    """Records the kwargs passed to SnowflakeConnection(__init__) and returns
    a stub that supports SHOW + CREATE without hitting the wire.
    """

    def __init__(self, *, db_exists: bool = False, schema_exists: bool = False):
        self.captured: dict = {}
        self._db_exists = db_exists
        self._schema_exists = schema_exists
        self.executed_sql: list = []

    def __call__(self, **kwargs):
        # Capture connect params and return a context manager.
        self.captured.update(kwargs)
        outer = self

        class _Conn:
            def __enter__(_inner):
                return _inner

            def __exit__(_inner, *exc):
                return False

            def execute(_inner, sql):
                outer.executed_sql.append(sql)
                if sql.startswith("SHOW DATABASES"):
                    return [("NEW_DB",)] if outer._db_exists else []
                if sql.startswith("SHOW SCHEMAS"):
                    return [("NEW_SCHEMA",)] if outer._schema_exists else []
                return None

        return _Conn()


@pytest.fixture
def provider_stub() -> MagicMock:
    p = MagicMock()
    p.warehouse = "WH1"
    p._kwargs = {
        "user": "alice",
        "password": "s3cret",
        # Explicitly include schema in _kwargs to confirm the strip works
        # even when the contract / call site forwarded a schema in.
        "schema": "TARGET_SCHEMA",
    }
    return p


# ── ensure_database ────────────────────────────────────────────────────


class TestEnsureDatabaseBootstrap:
    def test_connects_without_database_and_schema(
        self, monkeypatch: pytest.MonkeyPatch, provider_stub
    ):
        # Even with SNOWFLAKE_DATABASE / SNOWFLAKE_SCHEMA set in the env
        # (which the resolver would otherwise pick up), ensure_database
        # must NOT pass them to connect() — the database we're CREATING
        # doesn't exist yet, and connect-with-missing-db fails session-init.
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "FROM_ENV_DB")
        monkeypatch.setenv("SNOWFLAKE_SCHEMA", "FROM_ENV_SCHEMA")
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ACCT")
        monkeypatch.setenv("SNOWFLAKE_USER", "alice")

        factory = _FakeConnFactory(db_exists=False)
        with patch(
            "fluid_build.providers.snowflake.actions.database.SnowflakeConnection",
            factory,
        ):
            result = ensure_database(
                {
                    "op": "ensure_database",
                    "database": "NEW_DB",
                    "account": "ACCT",
                },
                provider_stub,
            )

        # Critical: neither was passed to connect(). This is THE fix.
        assert "database" not in factory.captured, (
            f"connect() received database={factory.captured.get('database')!r}; chicken-and-egg risk"
        )
        assert "schema" not in factory.captured, (
            f"connect() received schema={factory.captured.get('schema')!r}; chicken-and-egg risk"
        )

        # Sanity: the SHOW + CREATE actually ran.
        assert any("SHOW DATABASES" in s for s in factory.executed_sql)
        assert any("CREATE" in s and "DATABASE" in s for s in factory.executed_sql)
        assert result["status"] == "changed"
        assert result["changed"] is True

    def test_idempotent_when_database_already_exists(
        self, monkeypatch: pytest.MonkeyPatch, provider_stub
    ):
        # When SHOW returns a row, ensure_database is a no-op (no CREATE).
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ACCT")
        monkeypatch.setenv("SNOWFLAKE_USER", "alice")
        factory = _FakeConnFactory(db_exists=True)
        with patch(
            "fluid_build.providers.snowflake.actions.database.SnowflakeConnection",
            factory,
        ):
            result = ensure_database(
                {
                    "op": "ensure_database",
                    "database": "EXISTING_DB",
                    "account": "ACCT",
                },
                provider_stub,
            )

        # Same strip applies — connect() still gets no database/schema.
        assert "database" not in factory.captured
        assert "schema" not in factory.captured
        # No CREATE ran.
        assert not any("CREATE" in s for s in factory.executed_sql)
        assert result["status"] == "ok"
        assert result["changed"] is False


# ── ensure_schema ──────────────────────────────────────────────────────


class TestEnsureSchemaBootstrap:
    def test_connects_with_database_but_without_schema(
        self, monkeypatch: pytest.MonkeyPatch, provider_stub
    ):
        # ensure_schema runs AFTER ensure_database so the database exists
        # and CAN be set on the connection. The schema we're CREATING does
        # NOT exist yet, so it must be stripped from connect().
        monkeypatch.setenv("SNOWFLAKE_SCHEMA", "FROM_ENV_SCHEMA")
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "ACCT")
        monkeypatch.setenv("SNOWFLAKE_USER", "alice")

        factory = _FakeConnFactory(schema_exists=False)
        with patch(
            "fluid_build.providers.snowflake.actions.schema.SnowflakeConnection",
            factory,
        ):
            result = ensure_schema(
                {
                    "op": "ensure_schema",
                    "database": "EXISTING_DB",
                    "schema": "NEW_SCHEMA",
                    "account": "ACCT",
                },
                provider_stub,
            )

        # database SHOULD be present (it exists, we want USE DATABASE).
        assert factory.captured.get("database") == "EXISTING_DB"
        # schema MUST NOT be present.
        assert "schema" not in factory.captured, (
            f"connect() received schema={factory.captured.get('schema')!r}; chicken-and-egg risk"
        )

        assert any("SHOW SCHEMAS" in s for s in factory.executed_sql)
        assert any("CREATE" in s and "SCHEMA" in s for s in factory.executed_sql)
        assert result["status"] == "changed"
