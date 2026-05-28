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

"""Snowflake driver tests — mocked unit tests + opt-in real-engine
integration.

Two test classes:

* :class:`TestSnowflakeDriverMocked` — fully mocked. Exercises every
  driver code path (descriptor, execute, health_check, parameter
  rewrite) without needing Snowflake credentials. Always runs in CI.
* :class:`TestSnowflakeDriverIntegration` — opt-in. Skips unless
  ``SNOWFLAKE_ACCOUNT`` (and the rest of the connector env) is set.
  The integration suite runs the full ``fluid mcp output-port serve``
  surface against a real Snowflake table — the canonical
  ``snowflake-biz-lab`` smoke table.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.output_ports.mcp.drivers.base import UnsupportedBindingError
from fluid_build.output_ports.mcp.drivers.snowflake import SnowflakeDriver

# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------

SNOWFLAKE_ENV_KEYS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_PRIVATE_KEY_PATH",
)

INTEGRATION_REASON = (
    "SNOWFLAKE_ACCOUNT (and creds) not set — skipping live Snowflake "
    "integration test. Set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER and one of "
    "SNOWFLAKE_PASSWORD / SNOWFLAKE_PRIVATE_KEY_PATH to enable."
)


def _has_snowflake_creds() -> bool:
    if not os.environ.get("SNOWFLAKE_ACCOUNT"):
        return False
    if not os.environ.get("SNOWFLAKE_USER"):
        return False
    if not (os.environ.get("SNOWFLAKE_PASSWORD") or os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")):
        return False
    return True


def _make_expose(
    *,
    database: str = "ANALYTICS_PROD",
    schema: str = "CUSTOMER",
    table: str = "PROFILES",
    column_restrictions: List[Dict[str, Any]] = None,
    semantics: Dict[str, Any] = None,
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "customer_profiles",
        "kind": "table",
        "contract": {
            "schema": [
                {"name": "CUSTOMER_ID", "type": "STRING", "required": True},
                {"name": "EMAIL", "type": "STRING", "sensitivity": "pii"},
                {"name": "SIGNUP_DATE", "type": "DATE"},
            ],
        },
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {
                "database": database,
                "schema": schema,
                "table": table,
            },
        },
    }
    if semantics is not None:
        expose["semantics"] = semantics
    if column_restrictions is not None:
        expose.setdefault("policy", {}).setdefault("authz", {})[
            "columnRestrictions"
        ] = column_restrictions
    return expose


# ---------------------------------------------------------------------
# Mocked unit tests — always run, no creds needed
# ---------------------------------------------------------------------


class TestSnowflakeDriverMocked:
    def test_descriptor_returns_qualified_table_reference(self):
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        descriptor = driver.descriptor()
        assert descriptor.platform == "snowflake"
        assert descriptor.format == "snowflake_table"
        assert descriptor.dialect == "snowflake"
        assert descriptor.table_reference == "ANALYTICS_PROD.CUSTOMER.PROFILES"

    def test_descriptor_capabilities_advertised(self):
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        capabilities = driver.descriptor().capabilities
        assert capabilities["describe"] is True
        assert capabilities["sample"] is True
        assert capabilities["query"] is True
        assert capabilities["query_sql"] is True

    def test_unsupported_platform_raises(self):
        expose = _make_expose()
        expose["binding"]["platform"] = "gcp"
        with pytest.raises(UnsupportedBindingError, match="snowflake"):
            SnowflakeDriver(expose=expose, contract={})

    def test_unsupported_format_raises(self):
        expose = _make_expose()
        expose["binding"]["format"] = "snowflake_view"
        with pytest.raises(UnsupportedBindingError, match="snowflake_table"):
            SnowflakeDriver(expose=expose, contract={})

    def test_missing_database_raises(self):
        expose = _make_expose()
        del expose["binding"]["location"]["database"]
        with pytest.raises(UnsupportedBindingError, match="database"):
            SnowflakeDriver(expose=expose, contract={})

    def test_unsafe_identifier_rejected(self):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            SnowflakeDriver(expose=_make_expose(table="PROFILES; DROP TABLE x"), contract={})

    def test_execute_rewrites_named_placeholders(self):
        """Compiler emits ``:p_0``; Snowflake DB-API expects
        ``%(p_0)s``. The driver MUST rewrite before binding."""
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        connection, cursor = self._patch_connection(driver)
        cursor.description = [type("Col", (), {"name": "CUSTOMER_ID"})()]
        cursor.fetchall.return_value = [("C0001",)]
        sql_with_named = (
            "SELECT CUSTOMER_ID FROM ANALYTICS_PROD.CUSTOMER.PROFILES WHERE EMAIL = :p_0"
        )
        result = driver.execute(sql=sql_with_named, params=("alice@example.com",))
        # Cursor must see %(p_0)s, not :p_0.
        executed_sql = cursor.execute.call_args.args[0]
        assert "%(p_0)s" in executed_sql
        assert ":p_0" not in executed_sql
        # Binding dict aligns with the placeholder names.
        executed_params = cursor.execute.call_args.args[1]
        assert executed_params == {"p_0": "alice@example.com"}
        assert result.columns == ("CUSTOMER_ID",)
        assert result.rows == ({"CUSTOMER_ID": "C0001"},)

    def test_execute_blocks_injection_marker_at_driver(self):
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        self._patch_connection(driver)
        with pytest.raises(ValueError, match="injection marker"):
            driver.execute(sql="SELECT 1; DROP TABLE x", params=())

    def test_execute_blocks_banned_body_keyword_at_driver(self):
        """Defence-in-depth: even if the compiler regressed and let
        a UNION through, the driver guard must reject it."""
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        self._patch_connection(driver)
        with pytest.raises(ValueError, match="banned keyword"):
            driver.execute(
                sql="SELECT 1 FROM x UNION ALL SELECT secret FROM y",
                params=(),
            )

    def test_health_check_reports_ok(self):
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        connection, cursor = self._patch_connection(driver)
        cursor.fetchall.return_value = [(1,)]
        result = driver.health_check()
        assert result["status"] == "ok"
        assert result["engine"] == "snowflake"
        assert "latency_ms" in result

    def test_health_check_reports_unavailable_on_error(self):
        driver = SnowflakeDriver(expose=_make_expose(), contract={})
        connection, cursor = self._patch_connection(driver)
        cursor.execute.side_effect = RuntimeError("network unreachable")
        result = driver.health_check()
        assert result["status"] == "unavailable"
        assert "network unreachable" in result["detail"]

    def test_restricted_columns_drop_via_project(self):
        driver = SnowflakeDriver(
            expose=_make_expose(
                column_restrictions=[{"principal": "*", "columns": ["EMAIL"], "access": "deny"}]
            ),
            contract={},
        )
        rows = [{"CUSTOMER_ID": "C1", "EMAIL": "x@y", "SIGNUP_DATE": "2024-01-01"}]
        visible_columns, masked_rows = driver.project(rows)
        assert "EMAIL" not in visible_columns
        assert all("EMAIL" not in row for row in masked_rows)

    @staticmethod
    def _patch_connection(driver: SnowflakeDriver):
        cursor = MagicMock(name="cursor")
        cursor.description = []
        cursor.fetchall.return_value = []
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        connection = MagicMock(name="connection")
        connection.cursor.return_value = cursor
        driver._connection = connection
        return connection, cursor


# ---------------------------------------------------------------------
# Real-engine integration — opt-in, skips without creds
# ---------------------------------------------------------------------


@pytest.mark.skipif(not _has_snowflake_creds(), reason=INTEGRATION_REASON)
class TestSnowflakeDriverIntegration:
    """Drives the full ``fluid mcp output-port serve`` stack against a
    real Snowflake account.

    Binds to ``TELCO_STAGE_LOAD.PARTY`` — a stable 200-row seeded
    table from the snowflake-biz-lab fixtures. Read-only against
    Snowflake; the seed step is owned by snowflake-biz-lab.

    The integration suite runs four assertions:

    1. ``describe`` returns the Snowflake-bound engine metadata.
    2. ``sample`` returns at least one row with the expected columns.
    3. ``query`` runs a ``count`` measure and returns a non-zero
       row count.
    4. ``query`` with a categorical dimension groups correctly.
    """

    EXPOSE_ID = "telco_party"
    TABLE_NAME = "PARTY"
    SCHEMA_OVERRIDE = "TELCO_STAGE_LOAD"

    def test_describe_returns_snowflake_metadata(self, tmp_path: Path):
        contract = self._render_contract(tmp_path)
        responses = self._run_server(
            contract,
            [
                self._init_message(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "describe", "arguments": {}},
                },
            ],
        )
        describe = json.loads(responses[2]["result"]["content"][0]["text"])
        # The describe tool emits the bound-engine metadata under the
        # ``binding`` key (see _handlers.tool_describe), not ``engine``.
        assert describe["binding"]["platform"] == "snowflake"
        assert describe["binding"]["format"] == "snowflake_table"
        assert describe["binding"]["dialect"] == "snowflake"
        assert describe["exposeId"] == self.EXPOSE_ID
        assert describe["binding"]["tableReference"].endswith(f".{self.TABLE_NAME}")

    def test_sample_returns_real_rows(self, tmp_path: Path):
        contract = self._render_contract(tmp_path)
        responses = self._run_server(
            contract,
            [
                self._init_message(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "sample", "arguments": {"limit": 3}},
                },
            ],
        )
        assert "result" in responses[2], responses[2].get("error")
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        assert payload["rowCount"] >= 1, payload
        # Snowflake column names come back uppercase by default.
        assert any(col.upper() == "PARTY_ID" for col in payload["columns"])

    def test_query_with_count_measure(self, tmp_path: Path):
        contract = self._render_contract(tmp_path)
        responses = self._run_server(
            contract,
            [
                self._init_message(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "query",
                        "arguments": {
                            "measure": "row_count",
                            "dimensions": [],
                            "limit": 1,
                        },
                    },
                },
            ],
        )
        assert "result" in responses[2], responses[2].get("error")
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        assert payload["rowCount"] == 1
        # A count over a 200-row table must return a positive integer
        # under the row_count alias (engine echoes the alias).
        row = payload["rows"][0]
        count_value = next(iter(row.values()))
        assert isinstance(count_value, (int, float))
        assert count_value > 0

    def test_query_grouped_by_categorical_dimension(self, tmp_path: Path):
        contract = self._render_contract(tmp_path)
        responses = self._run_server(
            contract,
            [
                self._init_message(),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "query",
                        "arguments": {
                            "measure": "row_count",
                            "dimensions": ["party_type"],
                            "limit": 50,
                        },
                    },
                },
            ],
        )
        assert "result" in responses[2], responses[2].get("error")
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        assert payload["rowCount"] >= 1
        # Query results nest the rendered SQL under compiled.sql
        # (see _handlers._serialize_query_result), not a flat compiledSql.
        compiled = payload["compiled"]["sql"]
        assert "GROUP BY" in compiled.upper()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _init_message() -> Dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                # ``model`` is REQUIRED — the gateway's fail-closed
                # agentPolicy reads caller identity from clientInfo.model
                # (_bind_caller_identity_from_context). Without it every
                # tools/call is denied as "missing-model-identity" before
                # any Snowflake round-trip. The rendered contract sets no
                # allowedModels, so any non-empty value passes the gate.
                "clientInfo": {
                    "name": "snowflake-integration",
                    "version": "1.0.0",
                    "model": "snowflake-integration-test",
                },
                "capabilities": {},
            },
        }

    def _render_contract(self, tmp_path: Path) -> Path:
        database = os.environ["SNOWFLAKE_DATABASE"]
        schema = self.SCHEMA_OVERRIDE
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(
            f"""fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.telco.snowflake_integration_v1
name: Snowflake integration
metadata:
  layer: Bronze
  owner:
    team: telco-data-platform
    email: data-platform@example.com
  businessContext:
    domain: Telco
exposes:
  - exposeId: {self.EXPOSE_ID}
    kind: table
    binding:
      platform: snowflake
      format: snowflake_table
      location:
        database: {database}
        schema: {schema}
        table: {self.TABLE_NAME}
    contract:
      schema:
        - name: party_id
          type: STRING
          required: true
        - name: party_type
          type: STRING
        - name: status
          type: STRING
        - name: created_at
          type: TIMESTAMP
        - name: updated_at
          type: TIMESTAMP
    semantics:
      name: party
      measures:
        - name: row_count
          agg: count
          expr: party_id
      dimensions:
        - name: party_type
          type: categorical
        - name: status
          type: categorical
""",
            encoding="utf-8",
        )
        return path

    def _run_server(
        self, contract: Path, messages: List[Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        env = os.environ.copy()
        env["FLUID_QUIET"] = "1"
        env["FLUID_NONINTERACTIVE"] = "1"
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "fluid_build",
                "mcp",
                "output-port",
                "serve",
                str(contract),
                "--expose-id",
                self.EXPOSE_ID,
                "--max-sample-rows",
                "5",
            ],
            cwd=str(contract.parent),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        # Only requests (those carrying "id") get responses; the
        # ``initialized`` notification does not.
        expected_ids = {m["id"] for m in messages if "id" in m}
        responses: Dict[int, Dict[str, Any]] = {}

        # Hard wall-clock cap. A wedged server would otherwise block the
        # readline() below forever (pytest has no per-test timeout here);
        # killing the process closes stdout so readline() returns "".
        watchdog = threading.Timer(120, proc.kill)
        watchdog.start()
        try:
            # Pipe every request and FLUSH — but keep stdin OPEN. Closing
            # stdin is an EOF that tears down the server's read stream and
            # races any in-flight (executor-backed) response still being
            # written to stdout. That race is exactly what used to crash
            # the server with anyio.ClosedResourceError → rc=1 on the
            # `describe` call (which round-trips through the thread pool).
            for message in messages:
                proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            # Drain until every request id has a response (or stdout EOFs
            # because the server exited / the watchdog killed it).
            while expected_ids - responses.keys():
                line = proc.stdout.readline()
                if line == "":
                    break
                candidate = line.strip()
                if not candidate.startswith("{"):
                    continue
                response = json.loads(candidate)
                if "id" in response:
                    responses[response["id"]] = response
        finally:
            watchdog.cancel()

        # All responses collected — let communicate() send the EOF
        # (it flushes + closes stdin, swallowing BrokenPipeError) so the
        # server unwinds its lifespan and exits 0, then drains any
        # trailing stdout/stderr. We must NOT close stdin ourselves
        # first: communicate() re-flushes stdin and would raise
        # ValueError("I/O operation on closed file") on an already-closed
        # handle.
        try:
            _, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()

        missing = expected_ids - responses.keys()
        assert not missing, (
            f"no response for request ids {sorted(missing)} "
            f"(rc={proc.returncode})\nstderr tail: {stderr[-1500:]}"
        )
        assert (
            proc.returncode == 0
        ), f"server exited with rc={proc.returncode}\nstderr tail: {stderr[-1500:]}"
        return responses
