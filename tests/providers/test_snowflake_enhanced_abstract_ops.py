# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ``SnowflakeProviderEnhanced._execute_action`` abstract-op
dispatch (Phase 6F, round 2).

The first Phase 6F commit added abstract-op handlers to
``providers/snowflake/snowflake.py::SnowflakeProvider``, which turned
out to be dead code — ``providers/snowflake/__init__.py`` aliases
``SnowflakeProviderEnhanced`` as the public ``SnowflakeProvider``, so
``_execute_action`` in ``snowflake.py`` is never reached in apply.

This file tests the real production dispatcher in
``provider_enhanced.py``, which routes:

- ``provisionDataset`` → ensure_database + ensure_schema + ensure_table
- ``registerSchema``  → ensure_database + ensure_schema (no table)
- ``createView``      → ensure_view with params.sql
- ``grantAccess``     → sf.grant.privilege (or sf.grant.role if type=role)
- ``revokeAccess``    → skipped (not yet implemented in grants actions)
- ``scheduleTask``    → skipped with engine= in reason (Path-A / Path-B)
- ``updatePolicy``    → skipped (handled by stage-8 policy-apply)
- ``publishEvent``    → skipped (no Snowflake primitive)
- ``custom``          → sf.sql.execute on params.sql (fails loud if absent)

All tests stub the service-action handlers (``_execute_database_action``,
etc.) so no real Snowflake calls are made.
"""

from __future__ import annotations

import logging
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from fluid_build.providers.snowflake import SnowflakeProvider  # alias for Enhanced

# -----------------------------------------------------------------------------
# Provider harness
# -----------------------------------------------------------------------------


def _make_provider():
    """Construct SnowflakeProviderEnhanced without running __init__.

    ``__init__`` wants a connection pool and a credential resolver that
    would attempt to authenticate — using ``__new__`` bypasses both so
    tests can exercise pure dispatcher logic.
    """
    p = SnowflakeProvider.__new__(SnowflakeProvider)
    # ``warn_kv`` in the unknown-op branch reads ``self.logger``; provide
    # a real-looking logger so the warning path doesn't explode when
    # tested.
    p.logger = logging.getLogger("test_enhanced_dispatch")
    # Stub every service handler to a MagicMock returning a known shape.
    # Individual tests override them to assert specific calls.
    p._execute_database_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_schema_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_table_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_view_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_stream_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_task_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_procedure_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_udf_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_grant_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_share_action = MagicMock(return_value={"status": "success", "changed": True})
    p._execute_sql_action = MagicMock(return_value={"status": "success", "changed": True})
    return p


# -----------------------------------------------------------------------------
# _execute_action top-level dispatch
# -----------------------------------------------------------------------------


class TestDispatchPriority:
    def test_abstract_op_routes_to_abstract_handler_not_sf_prefix(self):
        """An abstract op like ``provisionDataset`` must NOT fall
        through to the ``sf.*`` prefix dispatch (which would hit
        ``unknown_action_op`` because ``provisionDataset`` doesn't
        start with ``sf.``)."""
        p = _make_provider()
        # Include columns so ensure_table is emitted — without them the
        # handler skips ensure_table and defers table creation to dbt
        # (documented behavior for reference-style contracts).
        action = {
            "op": "provisionDataset",
            "id": "a1",
            "params": {
                "binding": {
                    "location": {
                        "database": "D",
                        "schema": "S",
                        "table": "T",
                    }
                },
                "table": {"columns": [{"name": "id", "type": "NUMBER", "required": True}]},
            },
        }
        result = p._execute_action(action)
        assert result["status"] == "success"
        assert result["op"] == "provisionDataset"
        # provisionDataset with explicit columns must call all three.
        p._execute_database_action.assert_called_once()
        p._execute_schema_action.assert_called_once()
        p._execute_table_action.assert_called_once()

    def test_native_sf_op_still_routes_to_service_handler(self):
        """Native ``sf.*`` ops must continue to work — abstract-op
        dispatch layers on top, it does NOT replace native dispatch."""
        p = _make_provider()
        action = {"op": "sf.database.ensure", "id": "n1", "params": {"name": "D"}}
        result = p._execute_action(action)
        assert result["status"] == "success"
        p._execute_database_action.assert_called_once_with(action)

    def test_unknown_op_still_emits_warning_and_returns_skipped(self):
        """Genuinely unknown ops (no abstract match, no sf.* prefix)
        must still hit the ``unknown_action_op`` warning branch — we're
        adding a new dispatch mode, not removing the safety net."""
        p = _make_provider()
        # Spy the warn_kv method
        p.warn_kv = MagicMock()
        result = p._execute_action({"op": "wat_is_this", "id": "u1"})
        assert result["status"] == "skipped"
        assert "Unknown operation" in result["reason"]
        p.warn_kv.assert_called_once()
        assert p.warn_kv.call_args.kwargs["event"] == "unknown_action_op"

    def test_action_without_op_raises(self):
        p = _make_provider()
        with pytest.raises(Exception, match="missing required 'op'"):
            p._execute_action({"id": "nope"})


# -----------------------------------------------------------------------------
# provisionDataset
# -----------------------------------------------------------------------------


class TestProvisionDataset:
    def test_full_provisioning_runs_db_schema_table(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {
                    "binding": {"location": {"database": "DB", "schema": "SCH", "table": "TBL"}},
                    "table": {"columns": [{"name": "id", "type": "NUMBER", "required": True}]},
                },
            }
        )
        assert result["status"] == "success"
        assert len(result["sub_results"]) == 3
        # Validate the sub-action shape: op + flattened fields (not params.*)
        db_sub = p._execute_database_action.call_args.args[0]
        assert db_sub["op"] == "sf.database.ensure"
        assert db_sub["database"] == "DB"
        sc_sub = p._execute_schema_action.call_args.args[0]
        assert sc_sub["op"] == "sf.schema.ensure"
        assert sc_sub["database"] == "DB" and sc_sub["schema"] == "SCH"
        tb_sub = p._execute_table_action.call_args.args[0]
        assert tb_sub["op"] == "sf.table.ensure"
        assert tb_sub["table"] == "TBL"
        assert tb_sub["columns"] == [{"name": "id", "type": "NUMBER", "required": True}]

    def test_full_provisioning_without_columns_defers_table_creation(self):
        """Reference-style contracts (hybrid-reference) don't carry
        column specs; dbt creates the table during stage-7 build.
        Apply must emit a skipped table sub-result so the operator has
        evidence of the deferral."""
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {"binding": {"location": {"database": "D", "schema": "S", "table": "T"}}},
            }
        )
        assert result["status"] == "success"
        # 3 sub-results: db, schema, deferred-table
        assert len(result["sub_results"]) == 3
        p._execute_table_action.assert_not_called()
        assert result["sub_results"][2]["status"] == "skipped"
        assert "deferring" in result["sub_results"][2]["reason"]

    def test_schema_level_skips_table_when_location_has_no_table(self):
        """When binding.location omits the table entirely, there's no
        deferred-table record either — the provisioning is explicitly
        schema-level and the sub_results list is exactly two entries."""
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {"binding": {"location": {"database": "DB", "schema": "SCH"}}},
            }
        )
        assert result["status"] == "success"
        assert len(result["sub_results"]) == 2
        p._execute_table_action.assert_not_called()

    def test_missing_database_errors_early(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {"binding": {"location": {"schema": "SCH"}}},
            }
        )
        assert result["status"] == "error"
        assert "database" in result["reason"]
        # Nothing was dispatched.
        p._execute_database_action.assert_not_called()

    def test_missing_schema_errors_early(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {"binding": {"location": {"database": "DB"}}},
            }
        )
        assert result["status"] == "error"
        assert "schema" in result["reason"]

    def test_db_ensure_error_halts_chain(self):
        p = _make_provider()
        p._execute_database_action.return_value = {
            "status": "error",
            "reason": "denied",
            "changed": False,
        }
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {
                    "binding": {"location": {"database": "DB", "schema": "SCH", "table": "T"}}
                },
            }
        )
        assert result["status"] == "error"
        p._execute_schema_action.assert_not_called()
        p._execute_table_action.assert_not_called()

    def test_schema_ensure_error_halts_table_step(self):
        p = _make_provider()
        p._execute_schema_action.return_value = {
            "status": "error",
            "reason": "grant",
            "changed": False,
        }
        result = p._execute_action(
            {
                "op": "provisionDataset",
                "id": "x",
                "params": {
                    "binding": {"location": {"database": "DB", "schema": "SCH", "table": "T"}}
                },
            }
        )
        assert result["status"] == "error"
        p._execute_table_action.assert_not_called()


# -----------------------------------------------------------------------------
# registerSchema
# -----------------------------------------------------------------------------


class TestRegisterSchema:
    def test_runs_db_plus_schema_only(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "registerSchema",
                "id": "r",
                "params": {"binding": {"location": {"database": "DB", "schema": "SCH"}}},
            }
        )
        assert result["status"] == "success"
        assert len(result["sub_results"]) == 2
        p._execute_table_action.assert_not_called()

    def test_missing_database_errors(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "registerSchema",
                "id": "r",
                "params": {"binding": {"location": {"schema": "SCH"}}},
            }
        )
        assert result["status"] == "error"


# -----------------------------------------------------------------------------
# createView
# -----------------------------------------------------------------------------


class TestCreateView:
    def test_dispatches_to_view_ensure_with_sql(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "createView",
                "id": "v",
                "params": {
                    "binding": {"location": {"database": "DB", "schema": "SCH", "table": "MY_V"}},
                    "sql": "SELECT * FROM base",
                },
            }
        )
        assert result["status"] == "success"
        sub_action = p._execute_view_action.call_args.args[0]
        assert sub_action["op"] == "sf.view.ensure"
        # ensure_view reads action["query"] at the top level — not params.sql
        assert sub_action["query"] == "SELECT * FROM base"
        assert sub_action["name"] == "MY_V"

    def test_accepts_nested_view_sql(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "createView",
                "id": "v",
                "params": {
                    "binding": {"location": {"database": "DB", "schema": "SCH", "table": "V"}},
                    "view": {"sql": "SELECT 1"},
                },
            }
        )
        assert result["status"] == "success"
        assert p._execute_view_action.call_args.args[0]["query"] == "SELECT 1"

    def test_missing_sql_errors(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "createView",
                "id": "v",
                "params": {
                    "binding": {"location": {"database": "DB", "schema": "SCH", "table": "V"}}
                },
            }
        )
        assert result["status"] == "error"
        assert "sql" in result["reason"]


# -----------------------------------------------------------------------------
# grantAccess / revokeAccess
# -----------------------------------------------------------------------------


class TestGrantRevoke:
    def test_grant_privilege_default_type(self):
        p = _make_provider()
        result = p._execute_action(
            {"op": "grantAccess", "id": "g", "params": {"privilege": "SELECT"}}
        )
        assert result["status"] == "success"
        assert p._execute_grant_action.call_args.args[0]["op"] == "sf.grant.privilege"

    def test_grant_role_when_type_role(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "grantAccess",
                "id": "g",
                "params": {"type": "role", "role": "ANALYST"},
            }
        )
        assert result["status"] == "success"
        assert p._execute_grant_action.call_args.args[0]["op"] == "sf.grant.role"

    def test_revoke_access_returns_skipped_not_unknown(self):
        """Revoke isn't a pipeline halt — it must return ``skipped``
        with a clear reason so audit logs catch the gap without
        breaking apply."""
        p = _make_provider()
        result = p._execute_action({"op": "revokeAccess", "id": "r", "params": {}})
        assert result["status"] == "skipped"
        assert "not yet implemented" in result["reason"]


# -----------------------------------------------------------------------------
# scheduleTask / updatePolicy / publishEvent
# -----------------------------------------------------------------------------


class TestDeferredOps:
    def test_schedule_task_skipped_with_engine_in_reason(self):
        """scheduleTask was the #1 source of unknown_action_op warnings
        in the A2 run that motivated this fix. Must return skipped with
        the engine name surfaced so the operator knows which scheduling
        path (Path-A / Path-B) is responsible."""
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "scheduleTask",
                "id": "t",
                "params": {"engine": "snowflake_tasks"},
            }
        )
        assert result["status"] == "skipped"
        assert "snowflake_tasks" in result["reason"]

    def test_schedule_task_with_nested_orchestration_engine(self):
        p = _make_provider()
        result = p._execute_action(
            {
                "op": "scheduleTask",
                "id": "t",
                "params": {"orchestration": {"engine": "airflow"}},
            }
        )
        assert result["status"] == "skipped"
        assert "airflow" in result["reason"]

    def test_update_policy_skipped_pointing_at_stage_8(self):
        p = _make_provider()
        result = p._execute_action({"op": "updatePolicy", "id": "p", "params": {}})
        assert result["status"] == "skipped"
        assert "policy-apply" in result["reason"]

    def test_publish_event_skipped_silently(self):
        """publishEvent has no Snowflake primitive; returning skipped
        without warning is the correct behavior — contracts with this
        op target non-Snowflake consumers (e.g. BigQuery / EventBridge).
        """
        p = _make_provider()
        result = p._execute_action({"op": "publishEvent", "id": "e", "params": {}})
        assert result["status"] == "skipped"
        assert "no Snowflake primitive" in result["reason"]


# -----------------------------------------------------------------------------
# custom
# -----------------------------------------------------------------------------


class TestCustomOp:
    def test_custom_dispatches_to_sql_execute(self):
        p = _make_provider()
        result = p._execute_action(
            {"op": "custom", "id": "c", "params": {"sql": "ALTER WAREHOUSE x RESUME"}}
        )
        assert result["status"] == "success"
        sub = p._execute_sql_action.call_args.args[0]
        assert sub["op"] == "sf.sql.execute"
        assert sub["params"]["sql"] == "ALTER WAREHOUSE x RESUME"

    def test_custom_without_sql_errors(self):
        p = _make_provider()
        result = p._execute_action({"op": "custom", "id": "c", "params": {}})
        assert result["status"] == "error"
        assert "params.sql" in result["reason"]


# -----------------------------------------------------------------------------
# _aggregate_sub_status — helper semantics
# -----------------------------------------------------------------------------


class TestAggregateSubStatus:
    def test_empty_is_skipped(self):
        assert SnowflakeProvider._aggregate_sub_status([]) == "skipped"

    def test_all_success_is_success(self):
        rs = [{"status": "success"}, {"status": "success"}]
        assert SnowflakeProvider._aggregate_sub_status(rs) == "success"

    def test_any_error_is_error(self):
        rs = [{"status": "success"}, {"status": "error"}]
        assert SnowflakeProvider._aggregate_sub_status(rs) == "error"

    def test_all_skipped_is_skipped(self):
        rs = [{"status": "skipped"}, {"status": "skipped"}]
        assert SnowflakeProvider._aggregate_sub_status(rs) == "skipped"

    def test_success_plus_skipped_is_success(self):
        rs = [{"status": "skipped"}, {"status": "success"}]
        assert SnowflakeProvider._aggregate_sub_status(rs) == "success"
