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

"""Snowflake happy-path coverage for enterprise config resolution and planning."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers._sql_safety import (
    quote_string_literal,
    validate_sql_expression_allowlist,
)
from fluid_build.providers.snowflake.plan.planner import plan_actions
from fluid_build.providers.snowflake.util.auth import get_auth_report
from fluid_build.providers.snowflake.util.config import (
    _normalize_account_identifier,
    resolve_snowflake_settings,
)


def _native_sql_contract() -> dict:
    return {
        "id": "silver.spec.customers_v1",
        "metadata": {"name": "customer-analytics"},
        "builds": [
            {
                "id": "bootstrap_customers",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {
                    "sql": (
                        'CREATE OR REPLACE TABLE "ANALYTICS"."CURATED"."CUSTOMERS" '
                        "(ID NUMBER, EMAIL VARCHAR)"
                    )
                },
                "execution": {
                    "runtime": {
                        "platform": "snowflake",
                        "resources": {"warehouse": "TRANSFORM_WH", "role": "TRANSFORMER"},
                    }
                },
            }
        ],
        "exposes": [
            {
                "id": "customers",
                "type": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "account": "{{ env.CONTRACT_ACCOUNT }}",
                        "database": "ANALYTICS",
                        "schema": "CURATED",
                        "table": "CUSTOMERS",
                    },
                    "properties": {"cluster_by": ["REGION", "CUSTOMER_SEGMENT"]},
                },
                "contract": {
                    "schema": [
                        {"name": "ID", "type": "INTEGER", "required": True},
                        {"name": "EMAIL", "type": "STRING"},
                    ]
                },
            }
        ],
    }


def _dbt_contract() -> dict:
    return {
        "id": "silver.spec.billing_history_v1",
        "metadata": {"name": "billing-history"},
        "builds": [
            {
                "id": "billing_history_model",
                "engine": "dbt",
                "repository": "./models/billing_history",
                "properties": {"model": "billing_history"},
                "execution": {
                    "runtime": {
                        "platform": "snowflake",
                        "resources": {
                            "warehouse": "TRANSFORM_WH",
                            "database": "ANALYTICS",
                            "schema": "SILVER",
                            "role": "TRANSFORMER",
                        },
                    }
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "billing_history_table",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "ANALYTICS",
                        "schema": "SILVER",
                        "table": "BILLING_HISTORY_V1",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "billing_account_id", "type": "STRING", "required": True},
                        {"name": "total_amount", "type": "DECIMAL"},
                    ]
                },
            }
        ],
    }


def test_resolve_snowflake_settings_uses_enterprise_precedence(monkeypatch):
    monkeypatch.setenv("CONTRACT_ACCOUNT", "contract-account")
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "env-account")
    monkeypatch.setenv("SNOWFLAKE_USER", "env-user")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "env-password")
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "env-wh")

    with patch(
        "fluid_build.providers.snowflake.util.config._resolve_with_adapter",
        return_value=(None, None),
    ):
        resolved = resolve_snowflake_settings(
            contract=_native_sql_contract(),
            account="explicit-account",
            warehouse="explicit-wh",
        )

    assert resolved["account"] == "explicit-account"
    assert resolved["warehouse"] == "explicit-wh"
    assert resolved["database"] == "ANALYTICS"
    assert resolved["schema"] == "CURATED"
    assert resolved["role"] == "TRANSFORMER"
    assert resolved["user"] == "env-user"
    assert resolved["password"] == "env-password"


def test_get_auth_report_surfaces_provider_readiness():
    config = {
        "account": "acme-account",
        "user": "svc_forge",
        "warehouse": "TRANSFORM_WH",
        "authenticator": "externalbrowser",
    }

    with patch(
        "fluid_build.providers.snowflake.util.auth._get_current_identity",
        side_effect=RuntimeError("connector unavailable"),
    ):
        report = get_auth_report(config)

    assert report["readiness"]["auth_ready"] is True
    assert report["readiness"]["provider_ready"] is False
    assert report["readiness"]["provider_ready_missing"] == ["database", "schema"]
    assert report["connection_test"] == "failed"


def test_plan_actions_supports_modern_native_sql_happy_path():
    actions = plan_actions(
        _native_sql_contract(),
        account="acme-account",
        warehouse="BOOTSTRAP_WH",
        database=None,
        schema="PUBLIC",
    )

    ops = [action["op"] for action in actions]
    assert "sf.database.ensure" in ops
    assert "sf.schema.ensure" in ops
    assert "sf.sql.execute" in ops
    assert "sf.table.ensure" in ops

    sql_action = next(action for action in actions if action["op"] == "sf.sql.execute")
    assert sql_action["database"] == "ANALYTICS"
    assert sql_action["schema"] == "CURATED"

    table_action = next(action for action in actions if action["op"] == "sf.table.ensure")
    assert table_action["cluster_by"] == ["REGION", "CUSTOMER_SEGMENT"]
    assert table_action["database"] == "ANALYTICS"
    assert table_action["schema"] == "CURATED"


def test_plan_actions_supports_dbt_style_contract_without_build_sql():
    actions = plan_actions(
        _dbt_contract(),
        account="acme-account",
        warehouse="BOOTSTRAP_WH",
        database=None,
        schema="PUBLIC",
    )

    ops = [action["op"] for action in actions]
    assert "sf.database.ensure" in ops
    assert "sf.schema.ensure" in ops
    assert "sf.table.ensure" in ops
    assert "sf.sql.execute" not in ops

    table_action = next(action for action in actions if action["op"] == "sf.table.ensure")
    assert table_action["database"] == "ANALYTICS"
    assert table_action["schema"] == "SILVER"
    assert table_action["table"] == "BILLING_HISTORY_V1"


def test_plan_actions_preserves_parameterized_decimal_types():
    contract = _dbt_contract()
    contract["exposes"][0]["contract"]["schema"][1]["type"] = "DECIMAL(12,2)"

    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="BOOTSTRAP_WH",
        database=None,
        schema="PUBLIC",
    )

    table_action = next(action for action in actions if action["op"] == "sf.table.ensure")
    total_amount = next(col for col in table_action["columns"] if col["name"] == "total_amount")
    assert total_amount["type"] == "DECIMAL(12,2)"


# ---------------------------------------------------------------------------
# Hardening regression tests (PR follow-up to #44)
# ---------------------------------------------------------------------------


def test_resolve_precedence_explicit_beats_contract_beats_env(monkeypatch):
    """Explicit arg must win over contract value, which wins over env var."""
    contract = {
        "binding": {
            "platform": "snowflake",
            "location": {"database": "FROM_CONTRACT"},
        },
    }
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "FROM_ENV")

    # Explicit wins
    resolved = resolve_snowflake_settings(contract=contract, database="FROM_ARG")
    assert resolved["database"] == "FROM_ARG"

    # Contract wins when explicit is absent
    resolved = resolve_snowflake_settings(contract=contract)
    assert resolved["database"] == "FROM_CONTRACT"

    # Env wins when both are absent
    resolved = resolve_snowflake_settings()
    assert resolved["database"] == "FROM_ENV"


def test_unresolved_env_template_is_preserved(monkeypatch):
    """Missing env vars must leave the placeholder intact, not blank it."""
    monkeypatch.delenv("DEFINITELY_NOT_SET_VARIABLE", raising=False)
    contract = {
        "binding": {
            "platform": "snowflake",
            "location": {"account": "{{ env.DEFINITELY_NOT_SET_VARIABLE }}"},
        },
    }
    resolved = resolve_snowflake_settings(contract=contract)
    # Unresolved placeholder is left so downstream code can fail loudly.
    assert resolved.get("account") == "{{ env.DEFINITELY_NOT_SET_VARIABLE }}"


def test_sources_never_contains_secret_keys(monkeypatch):
    """The `_sources` map must not leak provenance of secret values."""
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct123")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")
    monkeypatch.setenv("SNOWFLAKE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "phrase")

    resolved = resolve_snowflake_settings()
    sources = resolved.get("_sources", {})

    # Non-secret provenance is still recorded.
    assert sources.get("account") == "env:SNOWFLAKE_ACCOUNT"
    # Secret keys must NOT appear in sources.
    assert "password" not in sources
    assert "oauth_token" not in sources
    assert "private_key_passphrase" not in sources


def test_get_connection_params_raises_when_user_missing(monkeypatch):
    from fluid_build.providers.snowflake.util import config as config_mod

    for key in [
        "SNOWFLAKE_USER",
        "SF_USER",
        "SNOWFLAKE_ACCOUNT",
        "SF_ACCOUNT",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Ensure the credential adapter can't inject a user from a local keychain.
    monkeypatch.setattr(config_mod, "_resolve_with_adapter", lambda *a, **k: (None, None))

    import pytest

    with pytest.raises(ValueError, match="user not specified"):
        config_mod.get_connection_params(account="acct123")


def test_externalbrowser_default_requires_tty(monkeypatch):
    """With no credentials and no TTY, the resolver must raise — never hang."""
    from fluid_build.providers.snowflake.util import config as config_mod

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct123")
    monkeypatch.setenv("SNOWFLAKE_USER", "alice")
    for key in [
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_AUTHENTICATOR",
        "SNOWFLAKE_PRIVATE_KEY_PATH",
        "SNOWFLAKE_OAUTH_TOKEN",
    ]:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(config_mod.os, "isatty", lambda fd: False)

    import pytest

    with pytest.raises(ValueError, match="not a TTY"):
        config_mod.get_connection_params(account="acct123", user="alice")


def test_query_tag_session_param_is_set(monkeypatch):
    """Every connection should carry a QUERY_TAG for cost attribution."""
    from fluid_build.providers.snowflake.util import config as config_mod

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct123")
    monkeypatch.setenv("SNOWFLAKE_USER", "alice")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")
    monkeypatch.setattr(config_mod.os, "isatty", lambda fd: True)

    params = config_mod.get_connection_params(
        account="acct123",
        user="alice",
        contract={"id": "silver.spec.customers_v1"},
        environment="prod",
    )
    session_params = params.get("session_params") or {}
    tag = session_params.get("QUERY_TAG", "")
    assert tag.startswith("forge:")
    assert "silver.spec.customers_v1" in tag
    assert "prod" in tag


def test_auth_order_prefers_keypair_over_password(monkeypatch):
    """Key-pair auth must take precedence over password when both are present."""
    from fluid_build.providers.snowflake.util import config as config_mod

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct123")
    monkeypatch.setenv("SNOWFLAKE_USER", "alice")
    monkeypatch.setattr(config_mod.os, "isatty", lambda fd: True)

    params = config_mod.get_connection_params(
        account="acct123",
        user="alice",
        password="hunter2",
        private_key_path="/tmp/key.p8",
    )
    assert params.get("private_key_path") == "/tmp/key.p8"
    assert "password" not in params


def test_auth_report_strips_secret_keys_from_sources(monkeypatch):
    """`get_auth_report` must never echo secret provenance."""
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct123")
    monkeypatch.setenv("SNOWFLAKE_USER", "alice")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2")

    report = get_auth_report("acct123")
    sources = report.get("sources", {})
    assert "password" not in sources
    assert "oauth_token" not in sources
    assert "private_key_passphrase" not in sources
    # And the report itself never surfaces the raw secret values.
    report_json = str(report)
    assert "hunter2" not in report_json


def test_iter_bindings_falls_back_on_empty_binding_location():
    """Empty `binding.location: {}` should still use legacy `expose.location`."""
    contract = {
        "exposes": [
            {
                "location": {"database": "LEGACY_DB", "schema": "LEGACY_SCHEMA"},
                "binding": {"platform": "snowflake", "location": {}},
            }
        ]
    }
    resolved = resolve_snowflake_settings(contract=contract)
    assert resolved.get("database") == "LEGACY_DB"
    assert resolved.get("schema") == "LEGACY_SCHEMA"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("xy12345", "xy12345"),
        ("xy12345.us-east-1", "xy12345.us-east-1"),
        ("xy12345.us-east-1.aws", "xy12345.us-east-1"),
        ("xy12345.eu-central-1.gcp", "xy12345.eu-central-1"),
        ("xy12345.west-us-2.azure", "xy12345.west-us-2"),
        ("org-account", "org-account"),
        ("org-account.privatelink", "org-account.privatelink"),
        ("xy12345.us-east-1.privatelink", "xy12345.us-east-1.privatelink"),
        (
            "https://xy12345.us-east-1.aws.snowflakecomputing.com",
            "xy12345.us-east-1",
        ),
        (
            "https://xy12345.us-east-1.privatelink.snowflakecomputing.com",
            "xy12345.us-east-1.privatelink",
        ),
        ("https://org-account.snowflakecomputing.com", "org-account"),
        (
            "https://org-account.privatelink.snowflakecomputing.com",
            "org-account.privatelink",
        ),
        (
            "https://app-org-account.privatelink.snowflakecomputing.com/console/login",
            "org-account.privatelink",
        ),
        (
            "https://app-xy12345.us-east-1.privatelink.snowflakecomputing.com",
            "xy12345.us-east-1.privatelink",
        ),
        ("xy12345.us-east-1.aws/", "xy12345.us-east-1"),
    ],
)
def test_normalize_account_identifier_supports_matrix(raw, expected):
    assert _normalize_account_identifier(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com",
        "bad/account/name",
        "org-account.extra.segment.aws",
    ],
)
def test_normalize_account_identifier_rejects_invalid_formats(raw):
    with pytest.raises(ValueError, match="Accepted forms include|organization/account"):
        _normalize_account_identifier(raw)


def test_quote_string_literal_escapes_embedded_quotes():
    assert quote_string_literal("analyst's role") == "'analyst''s role'"


def test_validate_sql_expression_allowlist_rejects_statement_breakouts():
    with pytest.raises(ValueError, match="Invalid SQL expression"):
        validate_sql_expression_allowlist("region = 'US'; DROP TABLE users")


def _rls_contract(table: str, role: str, condition: str) -> dict:
    return {
        "security": {
            "row_level_security": [
                {
                    "table": table,
                    "role": role,
                    "condition": condition,
                }
            ]
        }
    }


def test_plan_actions_emits_safe_row_level_security_sql():
    actions = plan_actions(
        _rls_contract("orders", "ANALYST", "region = 'US'"),
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
    )

    rls_action = next(action for action in actions if action["id"] == "rls_orders_analyst")
    assert rls_action["op"] == "sf.sql.execute"
    assert "CURRENT_ROLE() = 'ANALYST'" in rls_action["sql"]
    assert "THEN region = 'US'" in rls_action["sql"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("table", "orders; DROP TABLE users"),
        ("role", "ANALYST' OR 1=1 --"),
        ("condition", "region = 'US'; DROP TABLE users"),
    ],
)
def test_plan_actions_skips_unsafe_row_level_security_entries(field, value):
    logger = MagicMock()
    policy = {
        "table": "orders",
        "role": "ANALYST",
        "condition": "region = 'US'",
    }
    policy[field] = value

    actions = plan_actions(
        {"security": {"row_level_security": [policy]}},
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
        logger=logger,
    )

    assert not any(action["id"].startswith("rls_") for action in actions)
    logger.warning.assert_called_once()


def test_plan_actions_emits_unique_policy_names_per_role_on_same_table():
    """Multiple roles on the same table must not collide on a single policy name."""
    contract = {
        "security": {
            "row_level_security": [
                {"table": "orders", "role": "ANALYST", "condition": "region = 'US'"},
                {"table": "orders", "role": "AUDITOR", "condition": "region = 'EU'"},
            ]
        }
    }
    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
    )

    rls_actions = [a for a in actions if a["id"].startswith("rls_")]
    assert {a["id"] for a in rls_actions} == {"rls_orders_analyst", "rls_orders_auditor"}
    assert "orders_rls__analyst" in rls_actions[0]["sql"]
    assert "orders_rls__auditor" in rls_actions[1]["sql"]
    # Names are distinct so neither overwrites the other on apply.
    assert "orders_rls__analyst" not in rls_actions[1]["sql"]


def test_plan_actions_emits_apply_action_when_apply_on_is_provided():
    contract = {
        "security": {
            "row_level_security": [
                {
                    "table": "orders",
                    "role": "ANALYST",
                    "condition": "CURRENT_ROLE() = 'ANALYST'",
                    "apply_on": ["region"],
                }
            ]
        }
    }
    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
    )

    create = next(a for a in actions if a["id"] == "rls_orders_analyst")
    apply_action = next(a for a in actions if a["id"] == "rls_apply_orders_analyst")
    assert create["sql"].startswith("CREATE OR REPLACE ROW ACCESS POLICY orders_rls__analyst")
    assert apply_action["sql"] == (
        "ALTER TABLE orders ADD ROW ACCESS POLICY orders_rls__analyst ON (region)"
    )


def test_plan_actions_skips_apply_when_apply_on_is_unsafe():
    logger = MagicMock()
    contract = {
        "security": {
            "row_level_security": [
                {
                    "table": "orders",
                    "role": "ANALYST",
                    "condition": "region = 'US'",
                    "apply_on": "region; DROP TABLE users",
                }
            ]
        }
    }
    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
        logger=logger,
    )

    # CREATE still emitted; APPLY is dropped because the column is unsafe.
    assert any(a["id"] == "rls_orders_analyst" for a in actions)
    assert not any(a["id"].startswith("rls_apply_") for a in actions)
    logger.warning.assert_called_once()


def test_plan_actions_emits_named_row_access_policy():
    contract = {
        "security": {
            "policies": {
                "row_access": [
                    {
                        "name": "us_only",
                        "signature": "(region VARCHAR) RETURNS BOOLEAN",
                        "condition": "CURRENT_ROLE() = 'ANALYST' AND region = 'US'",
                        "comment": "US-only access for analyst role",
                    }
                ]
            },
            "policy_applications": {
                "row_access": [
                    {"table": "orders", "policy": "us_only", "on": ["region"]},
                ]
            },
        }
    }
    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
    )

    create = next(a for a in actions if a["id"] == "row_access_policy_us_only")
    apply_action = next(a for a in actions if a["id"] == "row_access_apply_orders_us_only")
    assert create["sql"] == (
        "CREATE OR REPLACE ROW ACCESS POLICY us_only "
        "AS (region VARCHAR) RETURNS BOOLEAN -> "
        "CURRENT_ROLE() = 'ANALYST' AND region = 'US'"
    )
    assert apply_action["sql"] == ("ALTER TABLE orders ADD ROW ACCESS POLICY us_only ON (region)")


def test_plan_actions_emits_named_masking_policy_and_application():
    contract = {
        "security": {
            "policies": {
                "masking": [
                    {
                        "name": "hash_email",
                        "signature": "(val VARCHAR) RETURNS VARCHAR",
                        "body": (
                            "CASE WHEN CURRENT_ROLE() IN ('ADMIN') THEN val ELSE SHA2(val, 256) END"
                        ),
                    }
                ]
            },
            "policy_applications": {
                "masking": [
                    {"table": "customers", "column": "email", "policy": "hash_email"},
                ]
            },
        }
    }
    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
    )

    create = next(a for a in actions if a["id"] == "masking_policy_hash_email")
    apply_action = next(a for a in actions if a["id"] == "masking_apply_customers_email_hash_email")
    assert create["sql"] == (
        "CREATE OR REPLACE MASKING POLICY hash_email "
        "AS (val VARCHAR) RETURNS VARCHAR -> "
        "CASE WHEN CURRENT_ROLE() IN ('ADMIN') THEN val ELSE SHA2(val, 256) END"
    )
    assert apply_action["sql"] == (
        "ALTER TABLE customers MODIFY COLUMN email SET MASKING POLICY hash_email"
    )


@pytest.mark.parametrize(
    ("policy_kind", "field", "value"),
    [
        ("row_access", "name", "us_only; DROP TABLE x"),
        ("row_access", "condition", "1=1; DROP TABLE x"),
        ("row_access", "signature", "(val VARCHAR); DROP TABLE x"),
        ("masking", "name", "bad-name"),
        ("masking", "body", "val; DROP TABLE x"),
    ],
)
def test_plan_actions_skips_unsafe_named_policy_inputs(policy_kind, field, value):
    logger = MagicMock()
    base = {
        "row_access": {"name": "ok", "condition": "CURRENT_ROLE() = 'ANALYST'"},
        "masking": {"name": "ok", "body": "val"},
    }[policy_kind]
    bad = dict(base)
    bad[field] = value
    contract = {"security": {"policies": {policy_kind: [bad]}}}

    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
        logger=logger,
    )

    prefix = "row_access_policy_" if policy_kind == "row_access" else "masking_policy_"
    assert not any(a["id"].startswith(prefix) for a in actions)
    logger.warning.assert_called_once()


@pytest.mark.parametrize(
    ("application_kind", "field", "value"),
    [
        ("row_access", "table", "orders; DROP TABLE x"),
        ("row_access", "policy", "us_only' OR 1=1"),
        ("row_access", "on", ["region; --"]),
        ("masking", "table", "customers; --"),
        ("masking", "column", "email; --"),
        ("masking", "policy", "hash_email; --"),
    ],
)
def test_plan_actions_skips_unsafe_policy_applications(application_kind, field, value):
    logger = MagicMock()
    base = {
        "row_access": {"table": "orders", "policy": "us_only", "on": ["region"]},
        "masking": {"table": "customers", "column": "email", "policy": "hash_email"},
    }[application_kind]
    bad = dict(base)
    bad[field] = value
    contract = {"security": {"policy_applications": {application_kind: [bad]}}}

    actions = plan_actions(
        contract,
        account="acme-account",
        warehouse="TRANSFORM_WH",
        database="ANALYTICS",
        schema="PUBLIC",
        logger=logger,
    )

    prefix = "row_access_apply_" if application_kind == "row_access" else "masking_apply_"
    assert not any(a["id"].startswith(prefix) for a in actions)
    logger.warning.assert_called_once()
