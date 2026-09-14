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

"""Keyless Snowflake happy-path over the real wire protocol (fakesnow server).

The over-the-wire sibling of ``test_snowflake_emulated_happy_path.py``. That
test runs under ``fakesnow.patch()``, which replaces snowflake-connector-python
in-process — the connector's own code never executes, so it proves forge-cli
calls the connector correctly and nothing below that.

Here the connector is NOT patched. fakesnow's Starlette app serves the Snowflake
HTTP wire protocol on localhost and the real connector talks to it: a real login
request, real query submission, real result-set decoding. A regression in any of
those surfaces — including one introduced by a connector upgrade — fails here and
passes in the in-process test.

Only the endpoint is redirected (``host``/``port``/``protocol``), because the
connector derives its URL from the account name and offers no override. See the
fixture note in ``tests/_infrastructure/emulator_fixtures.py``.

Still keyless, still free, still runs on every PR including forks.
``test_snowflake_live_happy_path.py`` remains the authority on Snowflake SQL.
"""

from __future__ import annotations

import uuid

import pytest

from tests._infrastructure.emulator_fixtures import requires_fakesnow_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.emulated,
    requires_fakesnow_server(),
]

# fakesnow ignores credentials entirely — it is an emulator, and the fixture
# forces the connection to loopback. The connector still requires SOME value to
# build a login request, so this is an arbitrary placeholder, not a credential.
# detect-secrets flags any `password=` literal on sight, which is the correct
# default; this is the documented way to say "checked, and it is not one".
_ANY_PASSWORD = "forge"  # pragma: allowlist secret


def test_snowflake_connection_server_happy_path(fakesnow_server_target: str) -> None:
    """SnowflakeConnection over HTTP: connect -> create -> insert -> select."""
    from fluid_build.providers.snowflake.connection import SnowflakeConnection

    database = f"FORGE_SRV_DB_{uuid.uuid4().hex[:8].upper()}"
    schema = "PUBLIC"

    with SnowflakeConnection(
        account=fakesnow_server_target, user="forge", password=_ANY_PASSWORD
    ) as conn:
        conn.execute(f"CREATE DATABASE {database}")
        conn.execute(f"CREATE SCHEMA {database}.{schema}")
        conn.execute(
            f'CREATE TABLE {database}.{schema}."SMOKE_TABLE" '
            '("ID" NUMBER(38,0) NOT NULL, "MESSAGE" VARCHAR, "CREATED_AT" TIMESTAMP_NTZ)'
        )
        conn.execute(
            f"INSERT INTO {database}.{schema}.\"SMOKE_TABLE\" SELECT 1, 'ok', CURRENT_TIMESTAMP()"
        )

        rows = conn.execute(f'SELECT "ID", "MESSAGE" FROM {database}.{schema}."SMOKE_TABLE"')
        assert rows is not None and len(rows) == 1
        assert int(rows[0][0]) == 1
        assert rows[0][1] == "ok"


def test_session_context_pinning_survives_the_wire(fakesnow_server_target: str) -> None:
    """``_initialize_session`` USE statements execute against a real endpoint.

    ``SnowflakeConnection._initialize_session`` pins ROLE / WAREHOUSE / DATABASE /
    SCHEMA with ``USE`` statements immediately after connect. Under
    ``fakesnow.patch()`` those never reach a server. Here they do, on a
    connection whose target objects already exist — which is the ordering the
    apply path relies on.
    """
    from fluid_build.providers.snowflake.connection import SnowflakeConnection

    database = f"FORGE_SRV_CTX_{uuid.uuid4().hex[:8].upper()}"

    with SnowflakeConnection(
        account=fakesnow_server_target, user="forge", password=_ANY_PASSWORD
    ) as bootstrap:
        bootstrap.execute(f"CREATE DATABASE {database}")
        bootstrap.execute(f"CREATE SCHEMA {database}.PUBLIC")

    # Re-connect WITH database/schema set, so _initialize_session issues USE.
    with SnowflakeConnection(
        account=fakesnow_server_target,
        user="forge",
        password=_ANY_PASSWORD,
        database=database,
        schema="PUBLIC",
    ) as conn:
        rows = conn.execute("SELECT CURRENT_DATABASE()")
        assert rows is not None and len(rows) == 1
        assert str(rows[0][0]).upper() == database
