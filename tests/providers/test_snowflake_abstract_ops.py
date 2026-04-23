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

"""Tests for Phase 6F — Snowflake provider's abstract-op translators.

Adversarial bias: every test locks the contract between FLUID 0.7.1's
abstract ``ActionType`` enum (provisionDataset / grantAccess / etc.)
and Snowflake-native DDL ops (ensure_database / ensure_schema /
apply_security / etc.). Breaking this contract is the ``unknown_action_op``
silent-no-op bug this phase fixes — plan.json carries abstract ops, the
Snowflake dispatcher speaks native only, every action becomes a
no-op warning and the pipeline reports SUCCESS with 0 DDL.

Native handlers are MOCKED (patched on the provider instance) — we
don't need a real Snowflake connection to verify the translator's
dispatch logic. Real-connection coverage lives in the live_happy_path
test files (marked @pytest.mark.snowflake).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


def _make_provider():
    """Build a SnowflakeProvider with mocked connection + native handlers.

    The translator tests exercise the ``_execute_action`` dispatch layer
    + the abstract-op handlers. Native handlers are replaced with mocks
    that record call args and return a success dict — enough to verify
    the translator passes the right params into the right native.
    """
    # Lazy import — Snowflake provider module has some import-time
    # side effects; keep imports inside the test helper so collection
    # doesn't trip on missing backend deps.
    from fluid_build.providers.snowflake.snowflake import SnowflakeProvider

    opts = MagicMock()
    opts.account = "NCBHNHQ-XP48604"
    opts.user = "ci"
    opts.warehouse = "COMPUTE_WH"
    opts.database = "TELCO_LAB"
    opts.schema = "PUBLIC"
    opts.role = "ACCOUNTADMIN"
    opts.environment = "test"

    # Construct without running __init__'s connection setup.
    provider = SnowflakeProvider.__new__(SnowflakeProvider)
    provider.options = opts
    provider.info_kv = MagicMock()
    provider.debug_kv = MagicMock()
    provider.warn_kv = MagicMock()
    provider.err_kv = MagicMock()

    # Stub every native handler to a recording mock. Each returns a
    # success dict — the tests assert the translator called the right
    # one with the right flattened params.
    provider._ensure_database = MagicMock(
        return_value={"op": "ensure_database", "status": "success", "action": "created"}
    )
    provider._ensure_schema = MagicMock(
        return_value={"op": "ensure_schema", "status": "success", "action": "created"}
    )
    provider._ensure_table = MagicMock(
        return_value={"op": "ensure_table", "status": "success", "action": "created"}
    )
    provider._ensure_view = MagicMock(
        return_value={"op": "ensure_view", "status": "success", "action": "created"}
    )
    provider._apply_security = MagicMock(return_value={"op": "apply_security", "status": "success"})
    provider._execute_sql = MagicMock(return_value={"op": "execute_sql", "status": "success"})

    return provider


# ---------------------------------------------------------------------------
# Dispatcher — abstract ops reach their handler; unknown ops error
# ---------------------------------------------------------------------------


class TestExecuteActionDispatcher:
    """``_execute_action`` routes abstract ops to abstract_handlers first,
    then native_handlers. Unknown ops return a dedicated error result."""

    def test_abstract_op_routes_to_abstract_handler(self):
        provider = _make_provider()
        action = {
            "op": "provisionDataset",
            "params": {
                "binding": {
                    "location": {
                        "database": "TELCO_LAB",
                        "schema": "SILVER",
                    }
                }
            },
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "provisionDataset"
        # Abstract handler delegated to two native handlers in order.
        assert provider._ensure_database.call_count == 1
        assert provider._ensure_schema.call_count == 1

    def test_native_op_still_routes_to_native_handler(self):
        """Back-compat: native ops keep working — translator is purely
        additive for abstract ops, never intercepts a native op."""
        provider = _make_provider()
        action = {"op": "ensure_database", "database": "TELCO_LAB"}
        result = provider._execute_action(action, context={})
        assert result["op"] == "ensure_database"
        assert provider._ensure_database.call_count == 1

    def test_unknown_op_returns_error(self):
        provider = _make_provider()
        action = {"op": "completelyInventedOp"}
        result = provider._execute_action(action, context={})
        assert result["status"] == "error"
        assert "Unknown operation" in result["error"]


# ---------------------------------------------------------------------------
# provisionDataset → ensure_database + ensure_schema (+ ensure_table)
# ---------------------------------------------------------------------------


class TestProvisionDataset:
    """provisionDataset is the most-common abstract op (every contract
    with an exposed port emits it). Must decompose correctly into the
    native ops the Snowflake dispatcher already handles."""

    def test_schema_level_provisioning_no_table(self):
        """When binding.location has database + schema but no table,
        only ensure_database + ensure_schema fire. ensure_table is
        skipped silently."""
        provider = _make_provider()
        action = {
            "op": "provisionDataset",
            "params": {"binding": {"location": {"database": "TELCO_LAB", "schema": "SILVER"}}},
        }
        result = provider._execute_action(action, context={})
        assert result["status"] == "success"
        assert result["database"] == "TELCO_LAB"
        assert result["schema"] == "SILVER"
        assert result["table"] is None
        assert len(result["sub_results"]) == 2
        assert provider._ensure_table.call_count == 0

    def test_table_level_provisioning_runs_all_three(self):
        """When binding.location.table is present, all three native
        ops fire in order (database → schema → table)."""
        provider = _make_provider()
        action = {
            "op": "provisionDataset",
            "params": {
                "binding": {
                    "location": {
                        "database": "TELCO_LAB",
                        "schema": "SILVER",
                        "table": "CUSTOMERS",
                    }
                },
                "schema": {"columns": [{"name": "id", "type": "STRING"}]},
            },
        }
        result = provider._execute_action(action, context={})
        assert result["status"] == "success"
        assert result["table"] == "CUSTOMERS"
        assert len(result["sub_results"]) == 3
        # ensure_table got the columns array from params.schema.columns.
        table_call_kwargs = provider._ensure_table.call_args.args[0]
        assert table_call_kwargs["table"] == "CUSTOMERS"
        assert table_call_kwargs["columns"] == [{"name": "id", "type": "STRING"}]

    def test_database_from_options_when_binding_omits_it(self):
        """binding.location.database is optional; falls back to the
        provider's options.database — matches Snowflake's convention of
        account-level default database."""
        provider = _make_provider()
        action = {
            "op": "provisionDataset",
            "params": {"binding": {"location": {"schema": "SILVER"}}},
        }
        result = provider._execute_action(action, context={})
        assert result["database"] == "TELCO_LAB"  # from _make_provider options

    def test_missing_schema_errors_early(self):
        """Schema is mandatory — no fallback. Return error before any
        native handler fires."""
        provider = _make_provider()
        action = {
            "op": "provisionDataset",
            "params": {"binding": {"location": {"database": "TELCO_LAB"}}},
        }
        result = provider._execute_action(action, context={})
        assert result["status"] == "error"
        assert "schema required" in result["error"]
        assert provider._ensure_database.call_count == 0

    def test_missing_database_and_no_option_errors(self):
        """Database required when provider has no default."""
        provider = _make_provider()
        provider.options.database = None
        action = {
            "op": "provisionDataset",
            "params": {"binding": {"location": {"schema": "SILVER"}}},
        }
        result = provider._execute_action(action, context={})
        assert result["status"] == "error"
        assert "database required" in result["error"]

    def test_ensure_database_error_halts_the_chain(self):
        """If the first native step fails, subsequent steps are NOT
        attempted — the overall result carries the sub-error."""
        provider = _make_provider()
        provider._ensure_database.return_value = {
            "op": "ensure_database",
            "status": "error",
            "error": "auth failed",
        }
        action = {
            "op": "provisionDataset",
            "params": {"binding": {"location": {"database": "X", "schema": "Y"}}},
        }
        result = provider._execute_action(action, context={})
        assert result["status"] == "error"
        assert "auth failed" in result["error"]
        assert provider._ensure_schema.call_count == 0


# ---------------------------------------------------------------------------
# grantAccess / revokeAccess → apply_security
# ---------------------------------------------------------------------------


class TestGrantRevokeAccess:
    def test_grant_access_dispatches_to_apply_security_grant_mode(self):
        provider = _make_provider()
        action = {
            "op": "grantAccess",
            "params": {
                "principal": "analytics-team@company.com",
                "role": "reader",
                "binding": {
                    "location": {
                        "database": "TELCO_LAB",
                        "schema": "SILVER",
                        "table": "CUSTOMERS",
                    }
                },
            },
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "grantAccess"
        assert result["status"] == "success"
        native_args = provider._apply_security.call_args.args[0]
        assert native_args["mode"] == "grant"
        assert native_args["principal"] == "analytics-team@company.com"
        assert native_args["role"] == "reader"
        assert native_args["target"] == "CUSTOMERS"

    def test_revoke_access_dispatches_to_apply_security_revoke_mode(self):
        provider = _make_provider()
        action = {
            "op": "revokeAccess",
            "params": {
                "principal": "deprecated-team@company.com",
                "role": "reader",
                "binding": {"location": {"database": "TELCO_LAB", "schema": "SILVER"}},
            },
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "revokeAccess"
        native_args = provider._apply_security.call_args.args[0]
        assert native_args["mode"] == "revoke"


# ---------------------------------------------------------------------------
# createView / registerSchema → existing natives
# ---------------------------------------------------------------------------


class TestCreateViewAndRegisterSchema:
    def test_create_view_dispatches_to_ensure_view_with_sql(self):
        provider = _make_provider()
        action = {
            "op": "createView",
            "params": {
                "view_sql": "SELECT * FROM bronze.orders",
                "binding": {
                    "location": {
                        "database": "TELCO_LAB",
                        "schema": "SILVER",
                        "view": "ORDERS_V",
                    }
                },
            },
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "createView"
        native_args = provider._ensure_view.call_args.args[0]
        assert native_args["view"] == "ORDERS_V"
        assert native_args["sql"] == "SELECT * FROM bronze.orders"

    def test_create_view_accepts_sql_alias_for_view_sql(self):
        """Param shape historically varied — accept both 'sql' and 'view_sql'."""
        provider = _make_provider()
        action = {
            "op": "createView",
            "params": {
                "sql": "SELECT 1",
                "binding": {
                    "location": {
                        "database": "X",
                        "schema": "Y",
                        "view": "V",
                    }
                },
            },
        }
        result = provider._execute_action(action, context={})
        native_args = provider._ensure_view.call_args.args[0]
        assert native_args["sql"] == "SELECT 1"

    def test_register_schema_runs_database_plus_schema_only(self):
        """registerSchema is strictly schema-level provisioning —
        skips ensure_table even when params include table info."""
        provider = _make_provider()
        action = {
            "op": "registerSchema",
            "params": {"binding": {"location": {"database": "X", "schema": "Y"}}},
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "registerSchema"
        assert result["status"] == "success"
        assert provider._ensure_database.call_count == 1
        assert provider._ensure_schema.call_count == 1
        assert provider._ensure_table.call_count == 0


# ---------------------------------------------------------------------------
# scheduleTask / updatePolicy / publishEvent — skipped / deferred ops
# ---------------------------------------------------------------------------


class TestSkippedAndDeferredOps:
    """Three abstract ops don't have a full Snowflake translation today —
    scheduleTask (Snowflake Tasks DDL is future work), updatePolicy
    (masking/RAP updates route through apply_security), publishEvent
    (event bus is a cross-provider concern). Each returns status=skipped
    with a reason instead of erroring, so the overall apply doesn't
    hard-fail on an unimplemented-but-safe op."""

    def test_schedule_task_skipped_with_engine_in_reason(self):
        provider = _make_provider()
        action = {
            "op": "scheduleTask",
            "params": {"buildId": "dv2_refresh", "engine": "dbt"},
        }
        result = provider._execute_action(action, context={})
        assert result["status"] == "skipped"
        assert "engine='dbt'" in result["reason"]
        assert result["build_id"] == "dv2_refresh"
        # No native handlers fired.
        provider._ensure_database.assert_not_called()
        provider._execute_sql.assert_not_called()

    def test_publish_event_skipped_silently(self):
        provider = _make_provider()
        action = {"op": "publishEvent", "params": {"topic": "orders.created"}}
        result = provider._execute_action(action, context={})
        assert result["status"] == "skipped"
        # No native handlers fired — publishEvent is entirely a catalog/
        # event-bus concern, not a Snowflake concern.
        provider._ensure_database.assert_not_called()

    def test_update_policy_routes_to_apply_security(self):
        provider = _make_provider()
        action = {
            "op": "updatePolicy",
            "params": {
                "policy": "PII_MASKING",
                "binding": {"location": {"database": "X", "schema": "Y", "table": "T"}},
            },
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "updatePolicy"
        native_args = provider._apply_security.call_args.args[0]
        assert native_args["mode"] == "policy_update"


# ---------------------------------------------------------------------------
# custom → execute_sql (ad-hoc escape hatch)
# ---------------------------------------------------------------------------


class TestCustomOp:
    def test_custom_dispatches_to_execute_sql(self):
        provider = _make_provider()
        action = {
            "op": "custom",
            "params": {"sql": "ALTER SESSION SET TIMEZONE = 'UTC'"},
        }
        result = provider._execute_action(action, context={})
        assert result["op"] == "custom"
        native_args = provider._execute_sql.call_args.args[0]
        assert native_args["sql"] == "ALTER SESSION SET TIMEZONE = 'UTC'"

    def test_custom_without_sql_errors(self):
        """custom requires params.sql — any other param shape is an error."""
        provider = _make_provider()
        action = {"op": "custom", "params": {}}
        result = provider._execute_action(action, context={})
        assert result["status"] == "error"
        assert "requires params.sql" in result["error"]
        provider._execute_sql.assert_not_called()


# ---------------------------------------------------------------------------
# Status roll-up helper — behaviour guaranteed on edge cases
# ---------------------------------------------------------------------------


class TestAggregateSubStatus:
    """``_aggregate_sub_status`` rolls a list of sub-op results into a
    single overall status. Used by every abstract-op handler that fans
    out to multiple native ops."""

    def test_any_error_yields_error(self):
        provider = _make_provider()
        assert (
            provider._aggregate_sub_status([{"status": "success"}, {"status": "error"}]) == "error"
        )

    def test_all_skipped_yields_skipped(self):
        provider = _make_provider()
        assert (
            provider._aggregate_sub_status([{"status": "skipped"}, {"status": "skipped"}])
            == "skipped"
        )

    def test_success_plus_skipped_yields_success(self):
        """Skipped is not a failure — mixed success+skipped rolls to
        success (one native succeeded, another was a no-op)."""
        provider = _make_provider()
        assert (
            provider._aggregate_sub_status([{"status": "success"}, {"status": "skipped"}])
            == "success"
        )

    def test_empty_list_yields_skipped(self):
        """No sub-ops → overall skipped (nothing happened, nothing failed)."""
        provider = _make_provider()
        assert provider._aggregate_sub_status([]) == "skipped"
