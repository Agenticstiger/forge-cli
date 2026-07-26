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

"""Snowflake-specific tests for ``fluid verify``."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from fluid_build.cli.verify import _hydrate_dotenv_into_environ, run, verify_snowflake_table


class _MockConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [
                ("ID", "NUMBER", "NO"),
                ("EMAIL", "VARCHAR", "YES"),
            ]
        if "COUNT(*)" in sql:
            return [(1,)]
        return []


def _args(contract: str, strict: bool = False):
    return argparse.Namespace(
        contract=contract,
        expose_id=None,
        strict=strict,
        out=None,
        show_diffs=False,
        env=None,
    )


def test_verify_snowflake_table_returns_match():
    with patch(
        "fluid_build.providers.snowflake.util.config.get_connection_params", return_value={}
    ):
        with patch(
            "fluid_build.providers.snowflake.connection.SnowflakeConnection", _MockConnection
        ):
            result = verify_snowflake_table(
                account="acme-account",
                warehouse="TRANSFORM_WH",
                database="ANALYTICS",
                schema="CURATED",
                table="CUSTOMERS",
                expected_schema=[
                    {"name": "ID", "type": "INTEGER", "required": True},
                    {"name": "EMAIL", "type": "STRING"},
                ],
                user="svc_forge",
                password="secret",
            )

    assert result["status"] == "match"
    assert result["severity"]["level"] == "SUCCESS"
    assert result["dimensions"]["location"]["actual"] == "ANALYTICS.CURATED"


def test_verify_snowflake_table_matches_case_insensitively():
    with patch(
        "fluid_build.providers.snowflake.util.config.get_connection_params", return_value={}
    ):
        with patch(
            "fluid_build.providers.snowflake.connection.SnowflakeConnection", _MockConnection
        ):
            result = verify_snowflake_table(
                account="acme-account",
                warehouse="TRANSFORM_WH",
                database="ANALYTICS",
                schema="CURATED",
                table="CUSTOMERS",
                expected_schema=[
                    {"name": "id", "type": "INTEGER", "required": True},
                    {"name": "email", "type": "STRING"},
                ],
                user="svc_forge",
                password="secret",
            )

    assert result["status"] == "match"
    assert result["dimensions"]["structure"]["matching_fields"] == ["id", "email"]


def test_run_routes_snowflake_table_to_verify_function(tmp_path: Path):
    contract_file = tmp_path / "contract.fluid.yaml"
    contract_file.write_text("id: snowflake.test\n")
    contract = {
        "id": "snowflake.test",
        "exposes": [
            {
                "id": "customers",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "ANALYTICS",
                        "schema": "CURATED",
                        "table": "CUSTOMERS",
                    },
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

    with patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract):
        with patch(
            "fluid_build.providers.snowflake.util.config.resolve_snowflake_settings",
            return_value={
                "account": "acme-account",
                "warehouse": "TRANSFORM_WH",
                "user": "svc_forge",
                "password": "secret",
                "schema": "CURATED",
            },
        ):
            with patch(
                "fluid_build.cli.verify.verify_snowflake_table",
                return_value={
                    "status": "match",
                    "exists": True,
                    "severity": {
                        "symbol": "🟢",
                        "level": "SUCCESS",
                        "impact": "NONE",
                        "remediation": "NONE",
                        "reason": "All checks passed",
                        "actions": [],
                    },
                    "dimensions": {
                        "structure": {
                            "status": "pass",
                            "matching_fields": ["ID", "EMAIL"],
                            "missing_fields": [],
                            "extra_fields": [],
                            "total_expected": 2,
                            "total_actual": 2,
                        },
                        "types": {"status": "pass", "mismatches": []},
                        "constraints": {"status": "pass", "mismatches": []},
                        "location": {
                            "status": "pass",
                            "expected": "ANALYTICS.CURATED",
                            "actual": "ANALYTICS.CURATED",
                            "message": None,
                        },
                    },
                    "metadata": {"num_rows": 1, "created": None, "modified": None},
                },
            ) as verify_mock:
                exit_code = run(
                    _args(str(contract_file), strict=True),
                    logger=__import__("logging").getLogger("test"),
                )

    assert exit_code == 0
    verify_mock.assert_called_once()


# ---------------------------------------------------------------------------
# SQL injection defense (PR follow-up to #44)
# ---------------------------------------------------------------------------


class _TrackingConnection:
    """Mock connection that records every SQL statement it sees."""

    statements: list = []

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        type(self).statements.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [("ID", "NUMBER", "NO")]
        if "COUNT(*)" in sql:
            return [(1,)]
        return []


def test_verify_rejects_injection_in_database_identifier():
    """A malicious database name must be rejected before any SQL runs."""
    _TrackingConnection.statements = []
    with patch(
        "fluid_build.providers.snowflake.util.config.get_connection_params", return_value={}
    ):
        with patch(
            "fluid_build.providers.snowflake.connection.SnowflakeConnection",
            _TrackingConnection,
        ):
            result = verify_snowflake_table(
                account="acme-account",
                warehouse="TRANSFORM_WH",
                database='FOO"; DROP TABLE users;--',
                schema="CURATED",
                table="CUSTOMERS",
                expected_schema=[{"name": "ID", "type": "INTEGER"}],
                user="svc_forge",
                password="secret",
            )

    assert result["status"] == "error"
    assert "Invalid SQL identifier" in result["error"]
    # Critically: no SQL was ever issued.
    assert _TrackingConnection.statements == []


def test_verify_rejects_injection_in_schema_identifier():
    _TrackingConnection.statements = []
    with patch(
        "fluid_build.providers.snowflake.util.config.get_connection_params", return_value={}
    ):
        with patch(
            "fluid_build.providers.snowflake.connection.SnowflakeConnection",
            _TrackingConnection,
        ):
            result = verify_snowflake_table(
                account="acme-account",
                warehouse="TRANSFORM_WH",
                database="ANALYTICS",
                schema="CURATED; DROP DATABASE PROD",
                table="CUSTOMERS",
                expected_schema=[{"name": "ID", "type": "INTEGER"}],
                user="svc_forge",
                password="secret",
            )

    assert result["status"] == "error"
    assert _TrackingConnection.statements == []


def test_verify_rejects_injection_in_table_identifier():
    _TrackingConnection.statements = []
    with patch(
        "fluid_build.providers.snowflake.util.config.get_connection_params", return_value={}
    ):
        with patch(
            "fluid_build.providers.snowflake.connection.SnowflakeConnection",
            _TrackingConnection,
        ):
            result = verify_snowflake_table(
                account="acme-account",
                warehouse="TRANSFORM_WH",
                database="ANALYTICS",
                schema="CURATED",
                table='CUSTOMERS" UNION SELECT * FROM SECRETS --',
                expected_schema=[{"name": "ID", "type": "INTEGER"}],
                user="svc_forge",
                password="secret",
            )

    assert result["status"] == "error"
    assert _TrackingConnection.statements == []


@pytest.fixture()
def _clean_snowflake_env(monkeypatch):
    for key in (
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_FLUID_SCHEMA",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_ROLE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def test_verify_hydrates_dotenv_into_environ(tmp_path: Path, _clean_snowflake_env) -> None:
    # Without the hydration step, `os.environ["SNOWFLAKE_DATABASE"]` stays
    # unset and `_resolve_env_templates("{{ env.SNOWFLAKE_DATABASE }}")`
    # returns the template string, which the Snowflake identifier
    # allowlist rejects. The fix loads .env files at the top of `run`.
    env_file = tmp_path / ".env"
    env_file.write_text("SNOWFLAKE_DATABASE=UNIT_TEST_DB\n")

    _hydrate_dotenv_into_environ(tmp_path, environment=None)

    assert os.environ.get("SNOWFLAKE_DATABASE") == "UNIT_TEST_DB"


def test_verify_hydration_is_noop_when_no_dotenv_present(
    tmp_path: Path, _clean_snowflake_env, caplog
) -> None:
    # No .env file on disk -> no exception, no log warnings, env untouched.
    with caplog.at_level(logging.WARNING, logger="fluid.cli.verify"):
        _hydrate_dotenv_into_environ(tmp_path, environment=None)

    assert os.environ.get("SNOWFLAKE_DATABASE") is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# --strict / --fail-on-warning exit-code split
# ---------------------------------------------------------------------------


def _drift_result(level: str, reason: str):
    """A verify result graded ``level`` with a non-'match' status."""
    return {
        "status": "mismatch",
        "exists": True,
        "severity": {
            "symbol": "🟡",
            "level": level,
            "impact": "MEDIUM",
            "remediation": "MANUAL_RECOMMENDED",
            "reason": reason,
            "actions": [],
        },
        "dimensions": {
            "structure": {
                "status": "pass",
                "matching_fields": ["ID"],
                "missing_fields": [],
                "extra_fields": [],
                "total_expected": 1,
                "total_actual": 1,
            },
            "types": {"status": "pass", "mismatches": []},
            "constraints": {
                "status": "fail",
                "mismatches": [{"field": "ID", "expected": "required", "actual": "nullable"}],
            },
            "location": {
                "status": "pass",
                "expected": "ANALYTICS.CURATED",
                "actual": "ANALYTICS.CURATED",
                "message": None,
            },
        },
        "metadata": {"num_rows": 1, "created": None, "modified": None},
    }


def _run_with_result(tmp_path: Path, result, **arg_overrides) -> int:
    contract_file = tmp_path / "contract.fluid.yaml"
    contract_file.write_text("id: snowflake.test\n")
    contract = {
        "id": "snowflake.test",
        "exposes": [
            {
                "id": "customers",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "ANALYTICS",
                        "schema": "CURATED",
                        "table": "CUSTOMERS",
                    },
                },
                "contract": {"schema": [{"name": "ID", "type": "INTEGER", "required": True}]},
            }
        ],
    }
    args = _args(str(contract_file), strict=arg_overrides.pop("strict", False))
    for key, value in arg_overrides.items():
        setattr(args, key, value)

    with (
        patch("fluid_build.cli.verify.load_contract_with_overlay", return_value=contract),
        patch(
            "fluid_build.providers.snowflake.util.config.resolve_snowflake_settings",
            return_value={
                "account": "acme-account",
                "warehouse": "TRANSFORM_WH",
                "user": "svc_forge",
                "password": "secret",
                "schema": "CURATED",
            },
        ),
        patch("fluid_build.cli.verify.verify_snowflake_table", return_value=result),
    ):
        return run(args, logger=__import__("logging").getLogger("test"))


def test_strict_downgrades_constraint_drift_to_a_warning(tmp_path: Path):
    """The documented split: --strict gates on CRITICAL drift only."""
    code = _run_with_result(
        tmp_path,
        _drift_result("WARNING", "Constraint mismatches detected (nullable vs required)"),
        strict=True,
        fail_on_warning=False,
    )
    assert code == 0


def test_fail_on_warning_gates_a_required_to_nullable_break(tmp_path: Path):
    """Regression: a CI gate had no way to fail on a break the tool itself
    calls breaking ('Mode changes are breaking'). ``--strict`` alone exits 0
    and the only documented behaviour was 'Exit with error code if any
    mismatches found', which was false."""
    code = _run_with_result(
        tmp_path,
        _drift_result("WARNING", "Constraint mismatches detected (nullable vs required)"),
        strict=True,
        fail_on_warning=True,
    )
    assert code == 1


def test_fail_on_warning_works_without_strict(tmp_path: Path):
    code = _run_with_result(
        tmp_path,
        _drift_result("WARNING", "Constraint mismatches detected (nullable vs required)"),
        strict=False,
        fail_on_warning=True,
    )
    assert code == 1


def test_downgrade_message_names_the_actual_drift_class(tmp_path: Path, capsys):
    """Regression: extra-column drift (INFO, a schema-structure mismatch) was
    reported as 'constraint-only drift', which is factually wrong."""
    code = _run_with_result(
        tmp_path,
        _drift_result("INFO", "Extra fields found in table (not in contract)"),
        strict=True,
        fail_on_warning=False,
    )
    assert code == 0
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "constraint-only drift" not in combined
    assert "Extra fields found in table" in combined


def test_fail_on_warning_defaults_off_for_callers_that_omit_it(tmp_path: Path):
    """``getattr`` default keeps programmatic callers (and older Namespaces)
    working."""
    args_result = _drift_result("WARNING", "Constraint mismatches detected")
    code = _run_with_result(tmp_path, args_result, strict=True)
    assert code == 0


def test_fail_on_warning_flag_is_registered():
    import argparse as _argparse

    from fluid_build.cli.verify import register

    parser = _argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register(sub)
    ns = parser.parse_args(["verify", "c.fluid.yaml", "--strict", "--fail-on-warning"])
    assert ns.strict is True
    assert ns.fail_on_warning is True
