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

"""Branch-coverage tests for fluid_build.secrets"""

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.errors import AuthenticationError, ConfigurationError
from fluid_build.secrets import (
    SecretConfig,
    SecretManager,
    SecretSource,
    get_secret,
    get_secret_manager,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_secrets")


# ── SecretSource enum ────────────────────────────────────────────────


class TestSecretSource:
    def test_env(self):
        assert SecretSource.ENV.value == "environment"

    def test_gcp(self):
        assert SecretSource.GCP_SECRET_MANAGER.value == "gcp_secret_manager"

    def test_aws(self):
        assert SecretSource.AWS_SECRETS_MANAGER.value == "aws_secrets_manager"

    def test_azure(self):
        assert SecretSource.AZURE_KEY_VAULT.value == "azure_key_vault"

    def test_vault(self):
        assert SecretSource.HASHICORP_VAULT.value == "hashicorp_vault"

    def test_file(self):
        assert SecretSource.LOCAL_FILE.value == "local_file"


# ── SecretConfig dataclass ───────────────────────────────────────────


class TestSecretConfig:
    def test_defaults(self):
        cfg = SecretConfig(source=SecretSource.ENV)
        assert cfg.project_id is None
        assert cfg.region is None
        assert cfg.vault_url is None
        assert cfg.vault_path is None

    def test_full(self):
        cfg = SecretConfig(
            source=SecretSource.GCP_SECRET_MANAGER,
            project_id="proj",
            region="us-east-1",
            vault_url="https://vault",
            vault_path="secret/data",
        )
        assert cfg.project_id == "proj"


# ── SecretManager ────────────────────────────────────────────────────


class TestSecretManager:
    def test_default_config(self):
        sm = SecretManager()
        assert sm.config.source == SecretSource.ENV

    def test_custom_config(self):
        cfg = SecretConfig(source=SecretSource.LOCAL_FILE)
        sm = SecretManager(cfg)
        assert sm.config.source == SecretSource.LOCAL_FILE

    # -- get_secret / caching --
    @patch.dict(os.environ, {"MY_SECRET": "val123"})
    def test_get_secret_from_env(self):
        sm = SecretManager()
        assert sm.get_secret("MY_SECRET") == "val123"

    @patch.dict(os.environ, {"MY_SECRET": "val123"})
    def test_cache_hit(self):
        sm = SecretManager()
        sm.get_secret("MY_SECRET")
        assert "MY_SECRET" in sm._cache
        # Second call should use cache
        assert sm.get_secret("MY_SECRET") == "val123"

    @patch.dict(os.environ, {}, clear=True)
    def test_required_missing_raises(self):
        sm = SecretManager()
        with pytest.raises(ConfigurationError):
            sm.get_secret("NONEXISTENT_SECRET", required=True)

    @patch.dict(os.environ, {}, clear=True)
    def test_not_required_returns_none(self):
        sm = SecretManager()
        assert sm.get_secret("NONEXISTENT_SECRET", required=False) is None

    def test_clear_cache(self):
        sm = SecretManager()
        sm._cache["key"] = "val"
        sm.clear_cache()
        assert sm._cache == {}

    # -- _get_from_env --
    @patch.dict(os.environ, {"X": "1"})
    def test_get_from_env_found(self):
        sm = SecretManager()
        assert sm._get_from_env("X") == "1"

    @patch.dict(os.environ, {}, clear=True)
    def test_get_from_env_missing(self):
        sm = SecretManager()
        assert sm._get_from_env("X") is None

    # -- _get_from_gcp --
    def test_gcp_import_error(self):
        cfg = SecretConfig(source=SecretSource.GCP_SECRET_MANAGER, project_id="proj")
        sm = SecretManager(cfg)
        with patch.dict(
            "sys.modules",
            {"google.cloud.secretmanager": None, "google.cloud": None, "google": None},
        ):
            with pytest.raises(ConfigurationError):
                sm._get_from_gcp("secret")

    def test_gcp_no_project_id(self):
        cfg = SecretConfig(source=SecretSource.GCP_SECRET_MANAGER, project_id=None)
        sm = SecretManager(cfg)
        mock_mod = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "google.cloud.secretmanager": mock_mod,
                "google.cloud": MagicMock(),
                "google": MagicMock(),
            },
        ):
            with pytest.raises(ConfigurationError):
                sm._get_from_gcp("secret")

    def test_gcp_success(self):
        cfg = SecretConfig(source=SecretSource.GCP_SECRET_MANAGER, project_id="proj")
        sm = SecretManager(cfg)
        mock_secretmanager = MagicMock()
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.payload.data.decode.return_value = "my-secret-value"
        mock_client.access_secret_version.return_value = mock_response
        mock_secretmanager.SecretManagerServiceClient.return_value = mock_client
        mock_google_cloud = MagicMock()
        mock_google_cloud.secretmanager = mock_secretmanager
        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(),
                "google.cloud": mock_google_cloud,
                "google.cloud.secretmanager": mock_secretmanager,
            },
        ):
            result = sm._get_from_gcp("test_secret")
        assert result == "my-secret-value"

    def test_gcp_exception_returns_none(self):
        cfg = SecretConfig(source=SecretSource.GCP_SECRET_MANAGER, project_id="proj")
        sm = SecretManager(cfg)
        mock_secretmanager = MagicMock()
        mock_secretmanager.SecretManagerServiceClient.return_value.access_secret_version.side_effect = RuntimeError(
            "fail"
        )
        mock_google_cloud = MagicMock()
        mock_google_cloud.secretmanager = mock_secretmanager
        with patch.dict(
            "sys.modules",
            {
                "google": MagicMock(),
                "google.cloud": mock_google_cloud,
                "google.cloud.secretmanager": mock_secretmanager,
            },
        ):
            result = sm._get_from_gcp("test_secret")
        assert result is None

    # -- _get_from_aws --
    def test_aws_import_error(self):
        cfg = SecretConfig(source=SecretSource.AWS_SECRETS_MANAGER)
        sm = SecretManager(cfg)
        with patch.dict(
            "sys.modules", {"boto3": None, "botocore": None, "botocore.exceptions": None}
        ):
            with pytest.raises(ConfigurationError):
                sm._get_from_aws("secret")

    def test_aws_string_secret(self):
        cfg = SecretConfig(source=SecretSource.AWS_SECRETS_MANAGER, region="us-west-2")
        sm = SecretManager(cfg)
        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": "aws-secret"}
        mock_boto3.client.return_value = mock_client
        mock_botocore = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "boto3": mock_boto3,
                "botocore": mock_botocore,
                "botocore.exceptions": mock_botocore.exceptions,
            },
        ):
            result = sm._get_from_aws("my-secret")
        assert result == "aws-secret"

    # -- _get_from_file --
    def test_file_not_found(self, tmp_path, monkeypatch):
        cfg = SecretConfig(source=SecretSource.LOCAL_FILE)
        sm = SecretManager(cfg)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = sm._get_from_file("missing")
        assert result is None

    def test_file_found(self, tmp_path, monkeypatch):
        cfg = SecretConfig(source=SecretSource.LOCAL_FILE)
        sm = SecretManager(cfg)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        secrets_dir = tmp_path / ".fluid" / "secrets"
        secrets_dir.mkdir(parents=True)
        (secrets_dir / "my_key").write_text("  secret_value  \n")
        result = sm._get_from_file("my_key")
        assert result == "secret_value"

    def test_file_read_error(self, tmp_path, monkeypatch):
        cfg = SecretConfig(source=SecretSource.LOCAL_FILE)
        sm = SecretManager(cfg)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        secrets_dir = tmp_path / ".fluid" / "secrets"
        secrets_dir.mkdir(parents=True)
        secret_file = secrets_dir / "bad"
        secret_file.mkdir()  # Directory, not file — read_text will fail
        result = sm._get_from_file("bad")
        assert result is None

    # -- _get_from_azure --
    def test_azure_import_error(self):
        cfg = SecretConfig(source=SecretSource.AZURE_KEY_VAULT, vault_url="https://v")
        sm = SecretManager(cfg)
        with patch.dict(
            "sys.modules",
            {
                "azure.keyvault.secrets": None,
                "azure.keyvault": None,
                "azure": None,
                "azure.identity": None,
            },
        ):
            with pytest.raises(ConfigurationError):
                sm._get_from_azure("s")

    def test_azure_no_vault_url(self):
        cfg = SecretConfig(source=SecretSource.AZURE_KEY_VAULT, vault_url=None)
        sm = SecretManager(cfg)
        mock_az = MagicMock()
        mock_id = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "azure.keyvault.secrets": mock_az,
                "azure.keyvault": MagicMock(),
                "azure": MagicMock(),
                "azure.identity": mock_id,
            },
        ):
            with pytest.raises(ConfigurationError):
                sm._get_from_azure("s")

    # -- _get_from_vault --
    def test_vault_import_error(self):
        cfg = SecretConfig(source=SecretSource.HASHICORP_VAULT, vault_url="http://v")
        sm = SecretManager(cfg)
        with patch.dict("sys.modules", {"hvac": None}):
            with pytest.raises(ConfigurationError):
                sm._get_from_vault("s")

    def test_vault_no_url(self):
        cfg = SecretConfig(source=SecretSource.HASHICORP_VAULT, vault_url=None)
        sm = SecretManager(cfg)
        mock_hvac = MagicMock()
        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            with pytest.raises(ConfigurationError):
                sm._get_from_vault("s")

    @patch.dict(os.environ, {}, clear=True)
    def test_vault_no_token(self):
        cfg = SecretConfig(source=SecretSource.HASHICORP_VAULT, vault_url="http://v")
        sm = SecretManager(cfg)
        mock_hvac = MagicMock()
        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            with pytest.raises(AuthenticationError):
                sm._get_from_vault("s")

    @patch.dict(os.environ, {"VAULT_TOKEN": "tok"})
    def test_vault_not_authenticated(self):
        cfg = SecretConfig(source=SecretSource.HASHICORP_VAULT, vault_url="http://v")
        sm = SecretManager(cfg)
        mock_hvac = MagicMock()
        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = False
        mock_hvac.Client.return_value = mock_client
        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            # AuthenticationError is raised inside try/except, so it gets caught
            result = sm._get_from_vault("s")
        assert result is None

    @patch.dict(os.environ, {"VAULT_TOKEN": "tok"})
    def test_vault_success(self):
        cfg = SecretConfig(
            source=SecretSource.HASHICORP_VAULT, vault_url="http://v", vault_path="kv"
        )
        sm = SecretManager(cfg)
        mock_hvac = MagicMock()
        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = True
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"value": "vault-value"}}
        }
        mock_hvac.Client.return_value = mock_client
        with patch.dict("sys.modules", {"hvac": mock_hvac}):
            result = sm._get_from_vault("s")
        assert result == "vault-value"

    # -- _retrieve_secret routing --
    def test_retrieve_unknown_source(self):
        cfg = SecretConfig(source=SecretSource.ENV)
        sm = SecretManager(cfg)
        # Hack: set source to something unsupported
        sm.config = MagicMock()
        sm.config.source = "not_a_real_source"
        with pytest.raises(ConfigurationError):
            sm._retrieve_secret("key")


# ── Module-level functions ───────────────────────────────────────────


class TestModuleLevelFunctions:
    @patch.dict(os.environ, {"GCP_PROJECT": "my-proj"}, clear=False)
    def test_get_secret_manager_gcp(self):
        import fluid_build.secrets as mod

        mod._global_manager = None
        mgr = get_secret_manager()
        assert mgr.config.source == SecretSource.GCP_SECRET_MANAGER
        mod._global_manager = None  # Reset

    @patch.dict(os.environ, {"AWS_REGION": "eu-west-1"}, clear=False)
    def test_get_secret_manager_aws(self):
        import fluid_build.secrets as mod

        old = os.environ.pop("GCP_PROJECT", None)
        mod._global_manager = None
        mgr = get_secret_manager()
        assert mgr.config.source == SecretSource.AWS_SECRETS_MANAGER
        mod._global_manager = None
        if old:
            os.environ["GCP_PROJECT"] = old

    @patch.dict(os.environ, {}, clear=True)
    def test_get_secret_manager_default_env(self):
        import fluid_build.secrets as mod

        mod._global_manager = None
        mgr = get_secret_manager()
        assert mgr.config.source == SecretSource.ENV
        mod._global_manager = None

    @patch.dict(os.environ, {"TEST_KEY": "hello"}, clear=False)
    def test_get_secret_convenience(self):
        import fluid_build.secrets as mod

        mod._global_manager = None
        result = get_secret("TEST_KEY")
        assert result == "hello"
        mod._global_manager = None

    @patch.dict(os.environ, {}, clear=True)
    def test_get_secret_default(self):
        import fluid_build.secrets as mod

        mod._global_manager = None
        result = get_secret("MISSING_KEY", required=False, default="fallback")
        assert result == "fallback"
        mod._global_manager = None
