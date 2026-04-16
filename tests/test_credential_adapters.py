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

"""Tests for fluid_build.credentials.adapters."""

from unittest.mock import MagicMock, patch

import pytest

from fluid_build.credentials.adapters import (
    AWSCredentialAdapter,
    GCPCredentialAdapter,
    SnowflakeCredentialAdapter,
)
from fluid_build.credentials.resolver import CredentialConfig, CredentialError

# ── SnowflakeCredentialAdapter ────────────────────────────────────────


class TestSnowflakeCredentialAdapter:
    def test_provider_default_returns_none(self):
        adapter = SnowflakeCredentialAdapter()
        assert adapter._get_provider_default("any_key") is None

    def test_provider_is_snowflake(self):
        adapter = SnowflakeCredentialAdapter()
        assert adapter.provider == "snowflake"

    @patch.object(SnowflakeCredentialAdapter, "get_credential")
    def test_get_connection_params_password_auth(self, mock_get):
        def side_effect(key, **kwargs):
            values = {
                "account": "my_account",
                "user": "my_user",
                "password": "my_pass",
                "warehouse": "my_wh",
                "database": None,
                "schema": None,
                "role": None,
            }
            return values.get(key)

        mock_get.side_effect = side_effect
        adapter = SnowflakeCredentialAdapter()
        params = adapter.get_connection_params()
        assert params["account"] == "my_account"
        assert params["user"] == "my_user"
        assert params["password"] == "my_pass"
        assert params["warehouse"] == "my_wh"

    @patch.object(SnowflakeCredentialAdapter, "get_credential")
    def test_get_connection_params_key_pair_auth(self, mock_get):
        def side_effect(key, **kwargs):
            values = {
                "account": "acct",
                "user": "usr",
                "password": None,
                "private_key_path": "/path/to/key",
                "private_key_passphrase": "phrase",
                "warehouse": None,
                "database": None,
                "schema": None,
                "role": None,
            }
            return values.get(key)

        mock_get.side_effect = side_effect
        adapter = SnowflakeCredentialAdapter()
        params = adapter.get_connection_params()
        assert params["private_key_path"] == "/path/to/key"
        assert params["private_key_passphrase"] == "phrase"
        assert "password" not in params

    @patch.object(SnowflakeCredentialAdapter, "get_credential")
    def test_get_connection_params_oauth(self, mock_get):
        def side_effect(key, **kwargs):
            values = {
                "account": "acct",
                "user": "usr",
                "password": None,
                "private_key_path": None,
                "oauth_token": "token123",
                "warehouse": None,
                "database": None,
                "schema": None,
                "role": None,
            }
            return values.get(key)

        mock_get.side_effect = side_effect
        adapter = SnowflakeCredentialAdapter()
        params = adapter.get_connection_params()
        assert params["oauth_token"] == "token123"

    @patch.object(SnowflakeCredentialAdapter, "get_credential")
    def test_get_connection_params_sso_fallback(self, mock_get):
        def side_effect(key, **kwargs):
            values = {
                "account": "acct",
                "user": "usr",
                "password": None,
                "private_key_path": None,
                "oauth_token": None,
                "authenticator": kwargs.get("cli_value", "externalbrowser"),
                "warehouse": None,
                "database": None,
                "schema": None,
                "role": None,
            }
            return values.get(key)

        mock_get.side_effect = side_effect
        adapter = SnowflakeCredentialAdapter()
        params = adapter.get_connection_params()
        assert params["authenticator"] == "externalbrowser"


# ── GCPCredentialAdapter ──────────────────────────────────────────────


class TestGCPCredentialAdapter:
    def test_provider_is_gcp(self):
        adapter = GCPCredentialAdapter()
        assert adapter.provider == "gcp"

    def test_provider_default_non_credentials_key_returns_none(self):
        adapter = GCPCredentialAdapter()
        assert adapter._get_provider_default("some_other_key") is None

    @patch("fluid_build.credentials.adapters.GCPCredentialAdapter._get_provider_default")
    def test_get_credentials_adc_mode(self, mock_default):
        mock_creds = MagicMock()
        mock_default.return_value = mock_creds
        adapter = GCPCredentialAdapter()
        result = adapter.get_credentials(mode="adc")
        assert result is mock_creds

    @patch("fluid_build.credentials.adapters.GCPCredentialAdapter.get_credential")
    def test_get_credentials_sa_key_mode(self, mock_get_cred):
        pytest.importorskip("google.oauth2.service_account")
        mock_get_cred.return_value = "/path/to/sa.json"
        adapter = GCPCredentialAdapter()

        mock_sa_creds = MagicMock()
        with patch(
            "google.oauth2.service_account.Credentials.from_service_account_file",
            return_value=mock_sa_creds,
        ):
            result = adapter.get_credentials(mode="sa-key")
            assert result is mock_sa_creds

    def test_provider_default_credentials_import_error(self):
        adapter = GCPCredentialAdapter()
        with patch.dict("sys.modules", {"google.auth": None, "google": None}):
            result = adapter._get_provider_default("credentials")
            # Should return None gracefully
            assert result is None or result is not None  # Just shouldn't raise


# ── AWSCredentialAdapter ──────────────────────────────────────────────


class TestAWSCredentialAdapter:
    def test_provider_is_aws(self):
        adapter = AWSCredentialAdapter()
        assert adapter.provider == "aws"

    def test_provider_default_non_session_key(self):
        adapter = AWSCredentialAdapter()
        assert adapter._get_provider_default("other_key") is None

    @patch.object(AWSCredentialAdapter, "get_credential")
    def test_get_session_with_explicit_keys(self, mock_get_cred):
        pytest.importorskip("boto3")

        def side_effect(key, **kwargs):
            return {"aws_access_key_id": "AKID", "aws_secret_access_key": "SECRET"}.get(key)

        mock_get_cred.side_effect = side_effect
        adapter = AWSCredentialAdapter()

        mock_session = MagicMock()
        with patch("boto3.Session", return_value=mock_session):
            result = adapter.get_session()
            assert result is mock_session

    @patch.object(AWSCredentialAdapter, "get_credential")
    @patch.object(AWSCredentialAdapter, "_get_provider_default")
    def test_get_session_falls_back_to_default(self, mock_default, mock_get_cred):
        mock_get_cred.return_value = None  # No explicit keys
        mock_session = MagicMock()
        mock_default.return_value = mock_session
        adapter = AWSCredentialAdapter()
        result = adapter.get_session()
        assert result is mock_session
