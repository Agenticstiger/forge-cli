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

"""Optional live Snowflake happy-path test for auth, plan, apply, and verify."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

try:
    import snowflake.connector  # noqa: F401

    HAS_SNOWFLAKE = True
except ImportError:
    HAS_SNOWFLAKE = False

from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.snowflake.connection import SnowflakeConnection
from fluid_build.providers.snowflake.types import ProviderOptions

REQUIRED_ENV_VARS = [
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_WAREHOUSE",
]
MISSING_ENV_VARS = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
HAS_KEY_PAIR_AUTH = bool(os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"))
HAS_OAUTH_AUTH = bool(os.getenv("SNOWFLAKE_OAUTH_TOKEN"))
HAS_SECURE_AUTH = HAS_KEY_PAIR_AUTH or HAS_OAUTH_AUTH

pytestmark = [
    pytest.mark.integration,
    pytest.mark.snowflake,
    pytest.mark.skipif(
        not HAS_SNOWFLAKE or MISSING_ENV_VARS or not HAS_SECURE_AUTH,
        reason=(
            "Snowflake live happy-path test requires snowflake-connector-python and env vars: "
            + ", ".join(REQUIRED_ENV_VARS)
            + " plus secure auth via SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_OAUTH_TOKEN"
        ),
    ),
]


def _bootstrap_options() -> ProviderOptions:
    options = ProviderOptions(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
        role=os.getenv("SNOWFLAKE_ROLE"),
    )
    if os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"):
        options.private_key_path = os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]
        options.private_key_passphrase = os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    elif os.getenv("SNOWFLAKE_OAUTH_TOKEN"):
        options.oauth_token = os.environ["SNOWFLAKE_OAUTH_TOKEN"]
    else:
        raise AssertionError(
            "Live Snowflake test requires secure auth via "
            "SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_OAUTH_TOKEN"
        )
    return options


def _write_contract(path: Path) -> None:
    path.write_text(
        """
fluidVersion: "0.7.1"
kind: "DataProduct"
id: "silver.community.live_snowflake_happy_path_v1"
name: "Live Snowflake Happy Path"
description: "Disposable contract used by the gated Snowflake acceptance test."
domain: "community"

metadata:
  layer: "Silver"
  owner:
    team: "qa"
    email: "qa@example.com"

builds:
  - id: "seed_smoke_table"
    pattern: "embedded-logic"
    engine: "sql"
    properties:
      sql: |
        CREATE OR REPLACE TABLE "{{ env.SNOWFLAKE_DATABASE }}"."{{ env.SNOWFLAKE_SCHEMA }}"."SMOKE_TABLE" (
          "ID" NUMBER(38,0) NOT NULL,
          "MESSAGE" VARCHAR,
          "CREATED_AT" TIMESTAMP_NTZ
        );

        INSERT INTO "{{ env.SNOWFLAKE_DATABASE }}"."{{ env.SNOWFLAKE_SCHEMA }}"."SMOKE_TABLE"
        SELECT 1, 'ok', CURRENT_TIMESTAMP();
    execution:
      trigger:
        type: "manual"
      runtime:
        platform: "snowflake"
        resources:
          warehouse: "{{ env.SNOWFLAKE_WAREHOUSE }}"
          database: "{{ env.SNOWFLAKE_DATABASE }}"
          schema: "{{ env.SNOWFLAKE_SCHEMA }}"

exposes:
  - exposeId: "smoke_table"
    kind: "table"
    binding:
      platform: "snowflake"
      format: "snowflake_table"
      location:
        account: "{{ env.SNOWFLAKE_ACCOUNT }}"
        database: "{{ env.SNOWFLAKE_DATABASE }}"
        schema: "{{ env.SNOWFLAKE_SCHEMA }}"
        table: "SMOKE_TABLE"
    contract:
      schema:
        - name: "ID"
          type: "INTEGER"
          required: true
        - name: "MESSAGE"
          type: "STRING"
        - name: "CREATED_AT"
          type: "TIMESTAMP"
""".strip()
        + "\n"
    )


def _run_cli(
    args: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "fluid_build", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _build_secure_cli_env(*, warehouse: str, database: str, schema: str) -> dict[str, str]:
    env = {
        **os.environ,
        "SNOWFLAKE_ACCOUNT": os.environ["SNOWFLAKE_ACCOUNT"],
        "SNOWFLAKE_USER": os.environ["SNOWFLAKE_USER"],
        "SNOWFLAKE_WAREHOUSE": warehouse,
        "SNOWFLAKE_DATABASE": database,
        "SNOWFLAKE_SCHEMA": schema,
    }
    env.pop("SNOWFLAKE_PASSWORD", None)
    env.pop("SF_PASSWORD", None)

    if os.getenv("SNOWFLAKE_ROLE"):
        env["SNOWFLAKE_ROLE"] = os.environ["SNOWFLAKE_ROLE"]
    if os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"):
        env["SNOWFLAKE_PRIVATE_KEY_PATH"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]
        if os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            env["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
    elif os.getenv("SNOWFLAKE_OAUTH_TOKEN"):
        env["SNOWFLAKE_OAUTH_TOKEN"] = os.environ["SNOWFLAKE_OAUTH_TOKEN"]
    else:
        raise AssertionError(
            "Live Snowflake test requires secure auth via "
            "SNOWFLAKE_PRIVATE_KEY_PATH or SNOWFLAKE_OAUTH_TOKEN"
        )
    return env


def test_snowflake_live_happy_path(tmp_path: Path):
    suffix = uuid4().hex[:8].upper()
    warehouse = f"FLUID_HP_WH_{suffix}"
    database = f"FLUID_HP_DB_{suffix}"
    schema = f"HP_{suffix}"
    contract_file = tmp_path / "contract.fluid.yaml"
    plan_file = tmp_path / "runtime" / "plan.json"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    _write_contract(contract_file)

    cli_env = _build_secure_cli_env(warehouse=warehouse, database=database, schema=schema)

    with SnowflakeConnection(_bootstrap_options()) as conn:
        conn.execute(
            f"CREATE WAREHOUSE IF NOT EXISTS {validate_ident(warehouse)} "
            "WAREHOUSE_SIZE = 'XSMALL' AUTO_SUSPEND = 60 AUTO_RESUME = TRUE INITIALLY_SUSPENDED = TRUE"
        )

        try:
            auth_result = _run_cli(["auth", "status", "snowflake"], cwd=tmp_path, env=cli_env)
            assert auth_result.returncode == 0, auth_result.stdout + auth_result.stderr

            validate_result = _run_cli(["validate", str(contract_file)], cwd=tmp_path, env=cli_env)
            assert validate_result.returncode == 0, validate_result.stdout + validate_result.stderr

            plan_result = _run_cli(
                ["plan", str(contract_file), "--out", str(plan_file)],
                cwd=tmp_path,
                env=cli_env,
            )
            assert plan_result.returncode == 0, plan_result.stdout + plan_result.stderr
            assert plan_file.exists()

            apply_result = _run_cli(
                ["apply", str(contract_file), "--yes"],
                cwd=tmp_path,
                env=cli_env,
            )
            assert apply_result.returncode == 0, apply_result.stdout + apply_result.stderr

            verify_result = _run_cli(
                ["verify", str(contract_file), "--strict"],
                cwd=tmp_path,
                env=cli_env,
            )
            assert verify_result.returncode == 0, verify_result.stdout + verify_result.stderr

            rows = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM {validate_ident(database)}.INFORMATION_SCHEMA.TABLES
                WHERE TABLE_CATALOG = %s
                  AND TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                """,
                [database, schema, "SMOKE_TABLE"],
            )
            assert rows and rows[0][0] == 1
        finally:
            conn.execute(f"DROP DATABASE IF EXISTS {validate_ident(database)}")
            conn.execute(f"DROP WAREHOUSE IF EXISTS {validate_ident(warehouse)}")

            warehouse_rows = conn.execute(f"SHOW WAREHOUSES LIKE '{warehouse}'")
            database_rows = conn.execute(f"SHOW DATABASES LIKE '{database}'")
            assert not warehouse_rows
            assert not database_rows
