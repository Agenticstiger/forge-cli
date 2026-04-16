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

"""Tests for verify.py drift severity assessment and Snowflake config utilities."""

import os
from unittest.mock import patch

import pytest

from fluid_build.cli.verify import assess_drift_severity
from fluid_build.providers.snowflake.util.config import (
    _get_connection_params_legacy,
    resolve_account_and_warehouse,
)


class TestAssessDriftSeverity:
    def test_all_clear(self):
        result = assess_drift_severity([], [], [], [], region_match=True)
        assert result["level"] == "SUCCESS"
        assert result["impact"] == "NONE"
        assert result["actions"] == []

    def test_missing_fields_critical(self):
        result = assess_drift_severity(["col1"], [], [], [], region_match=True)
        assert result["level"] == "CRITICAL"
        assert result["impact"] == "HIGH"
        assert any("missing fields" in a.lower() for a in result["actions"])

    def test_type_mismatches_critical(self):
        result = assess_drift_severity([], [], ["col1"], [], region_match=True)
        assert result["level"] == "CRITICAL"
        assert any("type mismatch" in a.lower() for a in result["actions"])

    def test_region_mismatch_critical(self):
        result = assess_drift_severity([], [], [], [], region_match=False)
        assert result["level"] == "CRITICAL"
        assert any("region" in a.lower() for a in result["actions"])

    def test_mode_mismatches_warning(self):
        result = assess_drift_severity([], [], [], ["col1"], region_match=True)
        assert result["level"] == "WARNING"
        assert result["impact"] == "MEDIUM"

    def test_extra_fields_info(self):
        result = assess_drift_severity([], ["extra_col"], [], [], region_match=True)
        assert result["level"] == "INFO"
        assert result["impact"] == "LOW"

    def test_critical_over_warning(self):
        """Multiple issues should return highest severity."""
        result = assess_drift_severity(
            ["missing"], ["extra"], ["type_mismatch"], ["mode"], region_match=True
        )
        assert result["level"] == "CRITICAL"

    def test_all_critical_actions_combined(self):
        result = assess_drift_severity(["m1"], [], ["t1"], [], region_match=False)
        assert len(result["actions"]) == 3


class TestResolveAccountAndWarehouse:
    def test_explicit_params(self):
        account, warehouse = resolve_account_and_warehouse("my_account", "my_wh")
        assert account == "my_account"
        assert warehouse == "my_wh"

    def test_env_var_fallback(self):
        """Environment variables beat stored credentials for env-specific runs."""
        with patch.dict(
            os.environ,
            {"SNOWFLAKE_ACCOUNT": "env_account", "SNOWFLAKE_WAREHOUSE": "env_wh"},
            clear=True,
        ):
            with patch(
                "fluid_build.providers.snowflake.util.config._resolve_with_adapter",
                return_value=(None, None),
            ):
                account, warehouse = resolve_account_and_warehouse()
                assert account == "env_account"
                assert warehouse == "env_wh"

    def test_no_account_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "fluid_build.providers.snowflake.util.config._resolve_with_adapter",
                return_value=(None, None),
            ):
                with pytest.raises(ValueError, match="Snowflake account not specified"):
                    resolve_account_and_warehouse()


class TestGetConnectionParamsLegacy:
    def test_basic_with_password(self):
        with patch.dict(os.environ, {"SNOWFLAKE_USER": "user1"}, clear=True):
            params = _get_connection_params_legacy("acc", "wh", password="secret")
            assert params["account"] == "acc"
            assert params["warehouse"] == "wh"
            assert params["user"] == "user1"
            assert params["password"] == "secret"

    def test_explicit_user(self):
        params = _get_connection_params_legacy("acc", "wh", user="explicit_user", password="p")
        assert params["user"] == "explicit_user"

    def test_env_password(self):
        with patch.dict(
            os.environ, {"SNOWFLAKE_USER": "u", "SNOWFLAKE_PASSWORD": "envpw"}, clear=True
        ):
            params = _get_connection_params_legacy("acc", "wh")
            assert params["password"] == "envpw"

    def test_no_user_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="Snowflake user not specified"):
                _get_connection_params_legacy("acc", "wh")

    def test_database_and_schema(self):
        params = _get_connection_params_legacy(
            "acc", "wh", database="db", schema="sch", user="u", password="p"
        )
        assert params["database"] == "db"
        assert params["schema"] == "sch"

    def test_token_auth(self):
        with patch.dict(os.environ, {"SNOWFLAKE_USER": "u"}, clear=True):
            params = _get_connection_params_legacy("acc", "wh", token="tok123")
            assert params["token"] == "tok123"

    def test_authenticator_kwarg(self):
        with patch.dict(os.environ, {"SNOWFLAKE_USER": "u"}, clear=True):
            params = _get_connection_params_legacy("acc", "wh", authenticator="externalbrowser")
            assert params["authenticator"] == "externalbrowser"

    def test_env_authenticator(self):
        with patch.dict(
            os.environ, {"SNOWFLAKE_USER": "u", "SNOWFLAKE_AUTHENTICATOR": "okta"}, clear=True
        ):
            params = _get_connection_params_legacy("acc", "wh")
            assert params["authenticator"] == "okta"

    def test_default_externalbrowser(self):
        with patch.dict(os.environ, {"SNOWFLAKE_USER": "u"}, clear=True):
            params = _get_connection_params_legacy("acc", "wh")
            assert params["authenticator"] == "externalbrowser"

    def test_optional_params(self):
        with patch.dict(os.environ, {"SNOWFLAKE_USER": "u"}, clear=True):
            params = _get_connection_params_legacy(
                "acc", "wh", role="ADMIN", application="myapp", password="p"
            )
            assert params["role"] == "ADMIN"
            assert params["application"] == "myapp"

    def test_private_key_auth(self):
        with patch.dict(os.environ, {"SNOWFLAKE_USER": "u"}, clear=True):
            params = _get_connection_params_legacy("acc", "wh", private_key="key_data")
            assert params["private_key"] == "key_data"
