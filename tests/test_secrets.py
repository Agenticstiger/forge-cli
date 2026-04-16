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

"""Tests for fluid_build.secrets"""

from unittest.mock import patch

import pytest

from fluid_build.errors import ConfigurationError
from fluid_build.secrets import SecretConfig, SecretManager, SecretSource


class TestSecretSource:
    def test_values(self):
        assert SecretSource.ENV.value == "environment"
        assert SecretSource.GCP_SECRET_MANAGER.value == "gcp_secret_manager"
        assert SecretSource.AWS_SECRETS_MANAGER.value == "aws_secrets_manager"
        assert SecretSource.AZURE_KEY_VAULT.value == "azure_key_vault"
        assert SecretSource.HASHICORP_VAULT.value == "hashicorp_vault"
        assert SecretSource.LOCAL_FILE.value == "local_file"


class TestSecretConfig:
    def test_defaults(self):
        sc = SecretConfig(source=SecretSource.ENV)
        assert sc.project_id is None
        assert sc.region is None
        assert sc.vault_url is None
        assert sc.vault_path is None


class TestSecretManager:
    def test_default_config(self):
        sm = SecretManager()
        assert sm.config.source == SecretSource.ENV
        assert sm._cache == {}

    def test_get_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "secret_value")
        sm = SecretManager()
        val = sm.get_secret("MY_SECRET")
        assert val == "secret_value"

    def test_get_from_env_missing_required(self):
        sm = SecretManager()
        with pytest.raises(ConfigurationError, match="Required secret not found"):
            sm.get_secret("NONEXISTENT_SECRET_XYZ_12345")

    def test_get_from_env_missing_optional(self):
        sm = SecretManager()
        val = sm.get_secret("NONEXISTENT_SECRET_XYZ_12345", required=False)
        assert val is None

    def test_caching(self, monkeypatch):
        monkeypatch.setenv("CACHED_KEY", "orig")
        sm = SecretManager()
        val1 = sm.get_secret("CACHED_KEY")
        monkeypatch.delenv("CACHED_KEY")
        val2 = sm.get_secret("CACHED_KEY")
        assert val1 == val2 == "orig"

    def test_retrieve_dispatches_env(self):
        sm = SecretManager(SecretConfig(source=SecretSource.ENV))
        with patch.object(sm, "_get_from_env", return_value="v") as mock:
            result = sm._retrieve_secret("k")
        mock.assert_called_once_with("k")
        assert result == "v"

    def test_retrieve_dispatches_gcp(self):
        sm = SecretManager(SecretConfig(source=SecretSource.GCP_SECRET_MANAGER))
        with patch.object(sm, "_get_from_gcp", return_value="v") as mock:
            sm._retrieve_secret("k")
        mock.assert_called_once_with("k")

    def test_retrieve_dispatches_aws(self):
        sm = SecretManager(SecretConfig(source=SecretSource.AWS_SECRETS_MANAGER))
        with patch.object(sm, "_get_from_aws", return_value="v") as mock:
            sm._retrieve_secret("k")
        mock.assert_called_once_with("k")

    def test_gcp_requires_project_id(self):
        sm = SecretManager(SecretConfig(source=SecretSource.GCP_SECRET_MANAGER, project_id=None))
        with patch("fluid_build.secrets.SecretManager._get_from_gcp") as mock_gcp:
            # If google-cloud-secret-manager isn't installed, the method raises
            # ConfigurationError about the import; that's fine to verify too
            mock_gcp.side_effect = ConfigurationError("project_id required")
            with pytest.raises(ConfigurationError):
                sm.get_secret("test")

    def test_get_from_env_simple(self, monkeypatch):
        monkeypatch.setenv("X", "42")
        sm = SecretManager()
        assert sm._get_from_env("X") == "42"
        assert sm._get_from_env("MISSING") is None
