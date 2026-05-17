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

"""Keyless Snowflake happy-path integration test (fakesnow emulator).

The keyless sibling of ``test_snowflake_live_happy_path.py``: it drives
forge-cli's own ``SnowflakeConnection`` wrapper — connect, DDL, DML, query —
against the in-process ``fakesnow`` emulator (embedded DuckDB) instead of a real
Snowflake account. Runs on every PR, including from forks, with zero credentials.

fakesnow accepts a more liberal SQL dialect than Snowflake, so this test asserts
the forge-cli connection codepath *executes*; ``test_snowflake_live_happy_path.py``
remains the authority on Snowflake SQL correctness.
"""

from __future__ import annotations

import uuid

import pytest

from tests._infrastructure.emulator_fixtures import requires_fakesnow

pytestmark = [pytest.mark.integration, pytest.mark.emulated, requires_fakesnow()]


def test_snowflake_connection_emulated_happy_path(fakesnow_patch) -> None:
    """forge-cli SnowflakeConnection: connect -> create -> insert -> select."""
    from fluid_build.providers.snowflake.connection import SnowflakeConnection

    database = f"FORGE_EMU_DB_{uuid.uuid4().hex[:8].upper()}"
    schema = "PUBLIC"

    # No database/schema on the options, so SnowflakeConnection._initialize_session
    # issues no USE statements before the database exists; the test creates the
    # objects explicitly through the same connection wrapper.
    with SnowflakeConnection(account="forge-emulated", user="forge") as conn:
        conn.execute(f"CREATE DATABASE {database}")
        conn.execute(f"CREATE SCHEMA {database}.{schema}")
        conn.execute(
            f'CREATE TABLE {database}.{schema}."SMOKE_TABLE" '
            '("ID" NUMBER(38,0) NOT NULL, "MESSAGE" VARCHAR, "CREATED_AT" TIMESTAMP_NTZ)'
        )
        conn.execute(
            f'INSERT INTO {database}.{schema}."SMOKE_TABLE" ' "SELECT 1, 'ok', CURRENT_TIMESTAMP()"
        )

        rows = conn.execute(f'SELECT "ID", "MESSAGE" FROM {database}.{schema}."SMOKE_TABLE"')
        assert rows is not None and len(rows) == 1
        assert int(rows[0][0]) == 1
        assert rows[0][1] == "ok"

    # No teardown: fakesnow.patch() discards the embedded DuckDB on context
    # exit, so the emulated database does not outlive the test.
