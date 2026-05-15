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

# tests/providers/test_snowflake_sql_allowlist.py
"""Statement-body allowlisting for the arbitrary-SQL Snowflake action surfaces.

Covers the D1/D2/D3 hardening:

* D1 — ``actions/sql.py::execute_sql`` (``custom`` surface)
* D2 — ``actions/task.py::ensure_task`` (``task_body`` surface + SCHEDULE quoting)
* D3 — ``actions/view.py::ensure_view`` / ``ensure_materialized_view``
       (``view_body`` surface)

The allowlist is implemented in ``fluid_build.providers._sql_safety.
parse_and_allowlist_sql`` on top of the borrowed sqlglot parser.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers._sql_safety import (
    SqlAllowlistError,
    parse_and_allowlist_sql,
    quote_string_literal,
)
from fluid_build.providers.snowflake.actions import sql as sql_action
from fluid_build.providers.snowflake.actions import task as task_action
from fluid_build.providers.snowflake.actions import view as view_action

pytestmark = pytest.mark.unit


# ── Helpers ────────────────────────────────────────────────────────────


def _provider() -> MagicMock:
    """A provider double exposing only what the action functions touch."""
    prov = MagicMock()
    prov.warehouse = "WH"
    prov._kwargs = {}
    return prov


@contextmanager
def _patched_connection(module):
    """Patch ``SnowflakeConnection`` + ``get_connection_params`` on a module.

    Yields the connection mock so a test can assert on the SQL handed to
    ``execute`` / ``executescript`` without a live warehouse.
    """
    conn = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    with (
        patch.object(module, "SnowflakeConnection", return_value=ctx),
        patch.object(module, "get_connection_params", return_value={}),
    ):
        yield conn


# ── parse_and_allowlist_sql — direct unit coverage ─────────────────────


class TestCustomSurfaceAllowlist:
    """The ``custom`` surface (D1) permits pipeline DDL/DML, rejects the rest."""

    @pytest.mark.parametrize(
        "stmt",
        [
            "CREATE TABLE t (a INT)",
            "CREATE OR REPLACE TABLE t (a INT)",
            "CREATE OR REPLACE VIEW v AS SELECT 1",
            "CREATE SCHEMA s",
            "ALTER TABLE t ADD COLUMN c INT",
            "DROP TABLE t",
            "INSERT INTO t SELECT * FROM s",
            "UPDATE t SET a = 1",
            "DELETE FROM t WHERE a = 1",
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1",
            "COPY INTO t FROM @stage",
            "TRUNCATE TABLE t",
            "GRANT SELECT ON t TO ROLE analyst",
            "SELECT 1",
        ],
    )
    def test_allows_legitimate_pipeline_statements(self, stmt):
        result = parse_and_allowlist_sql(stmt, surface="custom")
        assert len(result) == 1

    @pytest.mark.parametrize(
        "stmt",
        [
            "DROP ROLE analyst",
            "CREATE USER bob PASSWORD = 'x'",
            "CREATE WAREHOUSE w",
            "DROP DATABASE prod",
            "ALTER ACCOUNT SET FOO = 1",
            "USE ROLE accountadmin",
            "GRANT ROLE admin TO ROLE public",
            "CREATE ROLE superuser",
            "EXECUTE IMMEDIATE 'DROP TABLE t'",
        ],
    )
    def test_rejects_account_and_role_level_statements(self, stmt):
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql(stmt, surface="custom")

    def test_multi_statement_allowed_when_each_is_on_allowlist(self):
        # execute_sql legitimately supports multi-statement scripts; each
        # statement is still individually allowlisted.
        result = parse_and_allowlist_sql(
            "CREATE TABLE t (a INT); INSERT INTO t VALUES (1)", surface="custom"
        )
        assert len(result) == 2

    def test_multi_statement_rejected_if_any_statement_is_disallowed(self):
        # The classic injection payload: a benign statement followed by a
        # privilege-escalation statement. The DROP ROLE must sink the batch.
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql("SELECT 1; DROP ROLE analyst", surface="custom")

    @pytest.mark.parametrize("payload", ["", "   ", "-- only a comment", "/* x */"])
    def test_rejects_empty_or_comment_only(self, payload):
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql(payload, surface="custom")

    def test_rejects_non_string(self):
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql(None, surface="custom")  # type: ignore[arg-type]

    def test_error_message_does_not_echo_sql_body(self):
        secret = "DROP ROLE secret_role_name_abc123"
        with pytest.raises(SqlAllowlistError) as exc:
            parse_and_allowlist_sql(secret, surface="custom")
        # Only a structural label leaks, never the raw identifier.
        assert "secret_role_name_abc123" not in str(exc.value)


class TestTaskBodyAllowlist:
    """The ``task_body`` surface (D2) — a single CALL/DML/SELECT statement."""

    @pytest.mark.parametrize(
        "stmt",
        [
            "CALL db.sch.refresh_proc()",
            "INSERT INTO t SELECT * FROM s",
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1",
            "UPDATE t SET a = 1",
            "DELETE FROM t WHERE a = 1",
            "SELECT 1",
        ],
    )
    def test_allows_single_task_body_statements(self, stmt):
        assert parse_and_allowlist_sql(stmt, surface="task_body")

    def test_rejects_multi_statement_task_body(self):
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql("INSERT INTO t VALUES (1); DROP TABLE x", surface="task_body")

    @pytest.mark.parametrize(
        "stmt",
        [
            "CREATE TABLE t (a INT)",
            "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN c INT",
            "GRANT SELECT ON t TO ROLE r",
            "EXECUTE IMMEDIATE 'x'",
        ],
    )
    def test_rejects_ddl_and_opaque_statements_in_task_body(self, stmt):
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql(stmt, surface="task_body")


class TestViewBodyAllowlist:
    """The ``view_body`` surface (D3) — a single SELECT, CTEs/UNION allowed."""

    @pytest.mark.parametrize(
        "stmt",
        [
            "SELECT a, b FROM t",
            "WITH c AS (SELECT 1) SELECT * FROM c",
            "SELECT 1 UNION SELECT 2",
            "SELECT * FROM (SELECT 1) x",
        ],
    )
    def test_allows_select_view_bodies(self, stmt):
        assert parse_and_allowlist_sql(stmt, surface="view_body")

    @pytest.mark.parametrize(
        "stmt",
        [
            "DROP TABLE t",
            "INSERT INTO t VALUES (1)",
            "CREATE TABLE t (a INT)",
            "CALL p()",
            "SELECT 1; DROP TABLE t",
            "SELECT 1; SELECT 2",
        ],
    )
    def test_rejects_non_select_or_multi_statement_view_bodies(self, stmt):
        with pytest.raises(SqlAllowlistError):
            parse_and_allowlist_sql(stmt, surface="view_body")


# ── D1 — execute_sql wiring ────────────────────────────────────────────


class TestExecuteSqlAction:
    def test_rejected_payload_never_opens_a_connection(self):
        prov = _provider()
        with patch.object(sql_action, "SnowflakeConnection") as conn_cls:
            with pytest.raises(SqlAllowlistError):
                sql_action.execute_sql(
                    {"sql": "DROP ROLE analyst", "account": "a", "op": "sf.sql.execute"},
                    prov,
                )
        conn_cls.assert_not_called()
        # Rejection is recorded on the structured error log.
        assert any(
            c.kwargs.get("event") == "execute_sql_rejected" for c in prov.err_kv.call_args_list
        )

    def test_allowed_payload_executes_and_emits_audit_log(self):
        prov = _provider()
        with _patched_connection(sql_action) as conn:
            result = sql_action.execute_sql(
                {
                    "sql": "CREATE TABLE t (a INT)",
                    "account": "a",
                    "op": "sf.sql.execute",
                },
                prov,
            )
        assert result["changed"] is True
        conn.execute.assert_called_once()
        # The audit event fires for every allowed custom-SQL execution.
        audit = [
            c for c in prov.info_kv.call_args_list if c.kwargs.get("event") == "custom_sql_allowed"
        ]
        assert len(audit) == 1
        assert audit[0].kwargs["statement_count"] == 1

    def test_multi_statement_script_uses_executescript(self):
        prov = _provider()
        with _patched_connection(sql_action) as conn:
            sql_action.execute_sql(
                {
                    "sql": "CREATE TABLE t (a INT); INSERT INTO t VALUES (1); SELECT 1",
                    "account": "a",
                    "op": "sf.sql.execute",
                },
                prov,
            )
        conn.executescript.assert_called_once()


# ── D2 — ensure_task wiring ────────────────────────────────────────────


class TestEnsureTaskAction:
    def _base_action(self, sql_body: str) -> dict:
        return {
            "sql": sql_body,
            "account": "a",
            "database": "DB",
            "schema": "SCH",
            "name": "my_task",
            "op": "sf.task.ensure",
        }

    def test_rejected_task_body_never_opens_a_connection(self):
        prov = _provider()
        with patch.object(task_action, "SnowflakeConnection") as conn_cls:
            with pytest.raises(SqlAllowlistError):
                task_action.ensure_task(
                    self._base_action("INSERT INTO t VALUES (1); DROP TABLE x"), prov
                )
        conn_cls.assert_not_called()
        assert any(
            c.kwargs.get("event") == "ensure_task_rejected" for c in prov.err_kv.call_args_list
        )

    def test_allowed_task_body_executes(self):
        prov = _provider()
        action = self._base_action("CALL DB.SCH.refresh_proc()")
        action["schedule"] = "5 MINUTE"
        with _patched_connection(task_action) as conn:
            result = task_action.ensure_task(action, prov)
        assert result["changed"] is True
        conn.execute.assert_called_once()

    def test_schedule_literal_is_quoted_against_injection(self):
        # A crafted schedule value tries to break out of the literal and append
        # an extra statement. quote_string_literal must double the quote so the
        # whole thing stays inside the SCHEDULE = '...' literal.
        prov = _provider()
        evil = "1 MINUTE'; DROP TABLE secrets; --"
        action = self._base_action("CALL DB.SCH.refresh_proc()")
        action["schedule"] = evil
        with _patched_connection(task_action) as conn:
            task_action.ensure_task(action, prov)
        emitted = conn.execute.call_args.args[0]
        # The doubled-quote form appears; no bare single quote escapes.
        assert quote_string_literal(evil) in emitted
        assert "''; DROP TABLE secrets" in emitted


# ── D3 — ensure_view / ensure_materialized_view wiring ─────────────────


class TestEnsureViewAction:
    def _base_action(self, query: str) -> dict:
        return {
            "query": query,
            "account": "a",
            "database": "DB",
            "schema": "SCH",
            "name": "my_view",
            "op": "sf.view.ensure",
        }

    def test_rejected_view_body_never_opens_a_connection(self):
        prov = _provider()
        with patch.object(view_action, "SnowflakeConnection") as conn_cls:
            with pytest.raises(SqlAllowlistError):
                view_action.ensure_view(self._base_action("SELECT 1; DROP TABLE t"), prov)
        conn_cls.assert_not_called()
        assert any(
            c.kwargs.get("event") == "ensure_view_rejected" for c in prov.err_kv.call_args_list
        )

    def test_ddl_view_body_is_rejected(self):
        prov = _provider()
        with patch.object(view_action, "SnowflakeConnection"):
            with pytest.raises(SqlAllowlistError):
                view_action.ensure_view(self._base_action("DROP TABLE t"), prov)

    def test_allowed_select_view_body_executes(self):
        prov = _provider()
        with _patched_connection(view_action) as conn:
            result = view_action.ensure_view(
                self._base_action("SELECT a, b FROM t WHERE a > 0"), prov
            )
        assert result["changed"] is True
        conn.execute.assert_called_once()

    def test_materialized_view_body_is_also_allowlisted(self):
        prov = _provider()
        action = self._base_action("SELECT 1; DROP TABLE t")
        action["op"] = "sf.view.materialized.ensure"
        with patch.object(view_action, "SnowflakeConnection") as conn_cls:
            with pytest.raises(SqlAllowlistError):
                view_action.ensure_materialized_view(action, prov)
        conn_cls.assert_not_called()
        assert any(
            c.kwargs.get("event") == "ensure_materialized_view_rejected"
            for c in prov.err_kv.call_args_list
        )

    def test_materialized_view_allows_select(self):
        prov = _provider()
        action = self._base_action("SELECT a FROM t")
        action["op"] = "sf.view.materialized.ensure"
        with _patched_connection(view_action) as conn:
            result = view_action.ensure_materialized_view(action, prov)
        assert result["changed"] is True
        conn.execute.assert_called_once()
