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

"""Optional live smoke test for Snowflake governance hardening."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

try:
    import snowflake.connector  # noqa: F401

    HAS_SNOWFLAKE = True
except ImportError:
    HAS_SNOWFLAKE = False

from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.snowflake.connection import SnowflakeConnection
from fluid_build.providers.snowflake.governance import UnifiedGovernanceApplicator
from fluid_build.providers.snowflake.types import ProviderOptions

REQUIRED_ENV_VARS = [
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
    "FLUID_TEST_SNOWFLAKE_DATABASE",
]
MISSING_ENV_VARS = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]

pytestmark = [
    pytest.mark.integration,
    pytest.mark.snowflake,
    pytest.mark.skipif(
        not HAS_SNOWFLAKE or MISSING_ENV_VARS,
        reason=(
            "Snowflake live test requires snowflake-connector-python and env vars: "
            + ", ".join(REQUIRED_ENV_VARS)
        ),
    ),
]


class _TrackingCursor:
    """Cursor wrapper that counts execute calls while proxying to Snowflake."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.connection = cursor.connection
        self.execute_calls = 0

    def execute(self, sql, params=None):
        self.execute_calls += 1
        if params is None:
            return self._cursor.execute(sql)
        return self._cursor.execute(sql, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        return self._cursor.close()


def _provider_options(database: str | None = None) -> ProviderOptions:
    return ProviderOptions(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.getenv("SNOWFLAKE_ROLE"),
        database=database,
        schema=None,
    )


def _minimal_contract(database: str, schema: str, table: str) -> dict:
    return {
        "exposes": [
            {
                "binding": {
                    "location": {
                        "database": database,
                        "schema": schema,
                        "table": table,
                    },
                    "properties": {},
                },
                "contract": {
                    "schema": [
                        {"name": "ID", "type": "INTEGER", "required": True},
                        {"name": "EMAIL", "type": "STRING"},
                    ]
                },
            }
        ]
    }


def test_snowflake_governance_live_smoke():
    database = os.environ["FLUID_TEST_SNOWFLAKE_DATABASE"]
    schema = f"FLUID_SMOKE_{uuid4().hex[:8].upper()}"
    table = "GOVERNANCE_SMOKE"

    with SnowflakeConnection(_provider_options(database=database)) as conn:
        try:
            with conn._conn.cursor() as safe_raw_cursor:
                safe_cursor = _TrackingCursor(safe_raw_cursor)
                safe_app = UnifiedGovernanceApplicator(
                    safe_cursor,
                    _minimal_contract(database, schema, table),
                    dry_run=False,
                )
                result = safe_app.apply_all()

                assert result["status"] == "success"
                assert safe_cursor.execute_calls > 0

                safe_cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_CATALOG = %s
                      AND TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                    """,
                    (database, schema, table),
                )
                count = safe_cursor.fetchone()[0]
                assert count == 1

            with conn._conn.cursor() as invalid_raw_cursor:
                invalid_cursor = _TrackingCursor(invalid_raw_cursor)
                invalid_app = UnifiedGovernanceApplicator(
                    invalid_cursor,
                    _minimal_contract(database, "BAD;DROP_SCHEMA", table),
                    dry_run=False,
                )
                invalid_result = invalid_app.apply_all()

                assert invalid_result["status"] == "error"
                assert "Invalid SQL identifier" in invalid_result["error"]
                assert invalid_cursor.execute_calls == 0
        finally:
            with conn._conn.cursor() as cleanup_cursor:
                cleanup_cursor.execute(
                    f"DROP SCHEMA IF EXISTS {validate_ident(database)}.{validate_ident(schema)} CASCADE"
                )
