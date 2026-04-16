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

"""Branch coverage tests for auth.py (fluid_build/cli/auth.py)."""

import asyncio
import json
import logging
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fluid_build.cli.auth import AuthResult

# ---- AuthStatus and AuthResult dataclass tests ----


class TestAuthStatusEnum:
    def test_all_statuses(self):
        from fluid_build.cli.auth import AuthStatus

        assert AuthStatus.AUTHENTICATED.value == "authenticated"
        assert AuthStatus.NOT_AUTHENTICATED.value == "not_authenticated"
        assert AuthStatus.EXPIRED.value == "expired"
        assert AuthStatus.ERROR.value == "error"
        assert AuthStatus.UNKNOWN.value == "unknown"


class TestAuthResult:
    def test_fields(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus

        result = AuthResult(
            provider="aws",
            status=AuthStatus.AUTHENTICATED,
            user_info={"account": "123"},
            credentials_path="/tmp/creds",
            expires_at="2025-01-01",
            scopes=["read"],
            error_message=None,
        )
        assert result.provider == "aws"
        assert result.status == AuthStatus.AUTHENTICATED
        assert result.user_info["account"] == "123"

    def test_error_result(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus

        result = AuthResult(
            provider="gcp",
            status=AuthStatus.ERROR,
            error_message="connection failed",
        )
        assert result.error_message == "connection failed"

    def test_defaults(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus

        result = AuthResult(provider="test", status=AuthStatus.UNKNOWN)
        assert result.user_info == {}
        assert result.scopes == []
        assert result.credentials_path is None


# ---- AuthProvider base class ----


class TestAuthProviderBase:
    def test_run_command_success(self):
        from fluid_build.cli.auth import AuthProvider

        provider = AuthProvider.__new__(AuthProvider)
        provider.name = "test"
        provider.config = {}
        provider.logger = logging.getLogger("test")
        provider.console = None
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok")
            result = provider._run_command(["echo", "hello"])
            assert result.returncode == 0

    def test_run_command_file_not_found(self):
        from fluid_build.cli.auth import AuthProvider, CLIError

        provider = AuthProvider.__new__(AuthProvider)
        provider.name = "test"
        provider.config = {}
        provider.logger = logging.getLogger("test")
        provider.console = None
        with patch("subprocess.run", side_effect=FileNotFoundError("no such file")):
            with pytest.raises(CLIError):
                provider._run_command(["nonexistent"])

    def test_run_command_called_process_error(self):
        from fluid_build.cli.auth import AuthProvider

        provider = AuthProvider.__new__(AuthProvider)
        provider.name = "test"
        provider.config = {}
        provider.logger = logging.getLogger("test")
        provider.console = None
        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd")):
            with pytest.raises(subprocess.CalledProcessError):
                provider._run_command(["fail"])

    def test_login_not_implemented(self):
        from fluid_build.cli.auth import AuthProvider

        provider = AuthProvider.__new__(AuthProvider)
        provider.name = "test"
        with pytest.raises(NotImplementedError):
            asyncio.run(provider.login())

    def test_logout_not_implemented(self):
        from fluid_build.cli.auth import AuthProvider

        provider = AuthProvider.__new__(AuthProvider)
        provider.name = "test"
        with pytest.raises(NotImplementedError):
            asyncio.run(provider.logout())

    def test_check_auth_not_implemented(self):
        from fluid_build.cli.auth import AuthProvider

        provider = AuthProvider.__new__(AuthProvider)
        provider.name = "test"
        with pytest.raises(NotImplementedError):
            asyncio.run(provider.check_auth())


# ---- GoogleCloudAuthProvider ----


class TestGoogleCloudAuthProvider:
    def _make_provider(self):
        from fluid_build.cli.auth import GoogleCloudAuthProvider

        return GoogleCloudAuthProvider(
            config={
                "project_id": "my-project",
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            },
            logger=logging.getLogger("test"),
        )

    def test_init(self):
        p = self._make_provider()
        assert p.name == "google_cloud"
        assert p.project_id == "my-project"

    def test_check_auth_gcloud_not_installed(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        p._run_command = MagicMock(side_effect=Exception("not installed"))
        result = asyncio.run(p.check_auth())
        assert result.status in (AuthStatus.ERROR, AuthStatus.NOT_AUTHENTICATED)

    def test_check_auth_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        mock_cp = MagicMock(returncode=0, stdout="test-account@example.com")
        p._run_command = MagicMock(return_value=mock_cp)
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
        ):
            result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # gcloud version
                return MagicMock(returncode=0)
            else:  # print-access-token fails
                raise subprocess.CalledProcessError(1, "gcloud")

        p._run_command = mock_run
        result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_success(self):
        p = self._make_provider()
        p._run_command = MagicMock(return_value=MagicMock(returncode=0))
        result = asyncio.run(p.logout())
        assert result is True

    def test_logout_exception_handled(self):
        p = self._make_provider()
        p._run_command = MagicMock(side_effect=RuntimeError("fail"))
        result = asyncio.run(p.logout())
        # GCP logout swallows exceptions in inner try blocks
        assert result is True


# ---- AWSAuthProvider ----


class TestAWSAuthProvider:
    def _make_provider(self):
        from fluid_build.cli.auth import AWSAuthProvider

        return AWSAuthProvider(
            config={"region": "us-west-2", "profile": "dev"},
            logger=logging.getLogger("test"),
        )

    def test_init(self):
        p = self._make_provider()
        assert p.name == "aws"
        assert p.region == "us-west-2"
        assert p.profile == "dev"

    def test_init_defaults(self):
        from fluid_build.cli.auth import AWSAuthProvider

        p = AWSAuthProvider(config={}, logger=logging.getLogger("test"))
        assert p.region == "us-east-1"
        assert p.profile == "default"

    def test_check_auth_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        mock_cp = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {"Account": "123456", "UserId": "user1", "Arn": "arn:aws:iam::123:user/test"}
            ),
        )
        p._run_command = MagicMock(return_value=mock_cp)
        with (
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
        ):
            result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_aws_not_installed(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        p._run_command = MagicMock(side_effect=Exception("not found"))
        result = asyncio.run(p.check_auth())
        assert result.status in (AuthStatus.ERROR, AuthStatus.NOT_AUTHENTICATED)

    def test_check_auth_not_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # aws --version
                return MagicMock(returncode=0)
            raise subprocess.CalledProcessError(1, "aws")

        p._run_command = mock_run
        result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout(self):
        p = self._make_provider()
        p._run_command = MagicMock(return_value=MagicMock(returncode=0))
        result = asyncio.run(p.logout())
        assert result is True

    def test_logout_exception(self):
        p = self._make_provider()
        p._run_command = MagicMock(side_effect=RuntimeError)
        result = asyncio.run(p.logout())
        assert result is True  # AWS logout swallows inner exceptions


# ---- AzureAuthProvider ----


class TestAzureAuthProvider:
    def _make_provider(self):
        from fluid_build.cli.auth import AzureAuthProvider

        return AzureAuthProvider(
            config={"tenant_id": "tenant-123", "subscription_id": "sub-456"},
            logger=logging.getLogger("test"),
        )

    def test_init(self):
        p = self._make_provider()
        assert p.name == "azure"
        assert p.tenant_id == "tenant-123"
        assert p.subscription_id == "sub-456"

    def test_init_no_tenant(self):
        from fluid_build.cli.auth import AzureAuthProvider

        p = AzureAuthProvider(config={}, logger=logging.getLogger("test"))
        assert p.tenant_id is None
        assert p.subscription_id is None

    def test_check_auth_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        mock_cp = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "name": "my-sub",
                    "user": {"name": "test@example.com", "type": "user"},
                    "tenantId": "t1",
                    "id": "sub-1",
                }
            ),
        )
        p._run_command = MagicMock(return_value=mock_cp)
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
        ):
            result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_installed(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        p._run_command = MagicMock(side_effect=Exception("az not found"))
        result = asyncio.run(p.check_auth())
        assert result.status in (AuthStatus.ERROR, AuthStatus.NOT_AUTHENTICATED)

    def test_logout(self):
        p = self._make_provider()
        p._run_command = MagicMock(return_value=MagicMock(returncode=0))
        result = asyncio.run(p.logout())
        assert result is True

    def test_logout_exception(self):
        p = self._make_provider()
        p._run_command = MagicMock(side_effect=RuntimeError)
        with patch("shutil.which", return_value="/usr/bin/az"):
            result = asyncio.run(p.logout())
        assert result is False


# ---- SnowflakeAuthProvider ----


class TestSnowflakeAuthProvider:
    def _make_provider(self):
        from fluid_build.cli.auth import SnowflakeAuthProvider

        return SnowflakeAuthProvider(
            config={
                "account": "my_account",
                "user": "admin",
                "warehouse": "WH",
                "database": "DB",
                "role": "SYSADMIN",
            },
            logger=logging.getLogger("test"),
        )

    def test_init(self):
        p = self._make_provider()
        assert p.name == "snowflake"
        assert p.account == "my_account"

    def test_check_auth_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            return MagicMock(returncode=0, stdout="ADMIN")

        p._run_command = mock_run
        result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_configured(self):
        from fluid_build.cli.auth import AuthStatus, SnowflakeAuthProvider

        p = SnowflakeAuthProvider(config={}, logger=logging.getLogger("test"))
        p._run_command = MagicMock(return_value=MagicMock(returncode=0))
        result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_check_auth_snowsql_not_installed(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        p._run_command = MagicMock(side_effect=Exception("snowsql not found"))
        result = asyncio.run(p.check_auth())
        assert result.status in (AuthStatus.ERROR, AuthStatus.NOT_AUTHENTICATED)

    def test_logout(self):
        p = self._make_provider()
        result = asyncio.run(p.logout())
        assert result is True  # Session-based, always True


# ---- DatabricksAuthProvider ----


class TestDatabricksAuthProvider:
    def _make_provider(self):
        from fluid_build.cli.auth import DatabricksAuthProvider

        return DatabricksAuthProvider(
            config={"host": "https://my-workspace.cloud.databricks.com", "token": "abc123"},
            logger=logging.getLogger("test"),
        )

    def test_init(self):
        p = self._make_provider()
        assert p.name == "databricks"
        assert p.host == "https://my-workspace.cloud.databricks.com"

    def test_check_auth_authenticated(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        # Databricks check_auth tries REST API first with host+token from config/env
        with patch.object(p, "_validate_via_api") as mock_api:
            mock_api.return_value = AuthResult(
                provider="databricks",
                status=AuthStatus.AUTHENTICATED,
                user_info={"user_name": "test"},
            )
            result = asyncio.run(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_installed(self):
        from fluid_build.cli.auth import AuthStatus

        p = self._make_provider()
        p._run_command = MagicMock(side_effect=Exception("not found"))
        result = asyncio.run(p.check_auth())
        assert result.status in (AuthStatus.ERROR, AuthStatus.NOT_AUTHENTICATED)

    def test_logout_removes_config(self, tmp_path):
        p = self._make_provider()
        config_file = tmp_path / ".databrickscfg"
        config_file.write_text("[DEFAULT]\nhost = test\n")
        with patch("os.path.expanduser", return_value=str(config_file)):
            result = asyncio.run(p.logout())
            assert result is True
            assert not config_file.exists()

    def test_logout_no_config(self, tmp_path):
        p = self._make_provider()
        with patch("os.path.expanduser", return_value=str(tmp_path / "nonexistent")):
            result = asyncio.run(p.logout())
            assert result is True

    def test_logout_exception(self):
        p = self._make_provider()
        with patch("os.path.expanduser", side_effect=RuntimeError):
            result = asyncio.run(p.logout())
            assert result is False


# ---- AuthManager ----


class TestAuthManager:
    def _make_manager(self):
        from fluid_build.cli.auth import AuthManager

        return AuthManager(config={}, logger=logging.getLogger("test"))

    def test_init(self):
        m = self._make_manager()
        assert isinstance(m.providers, dict)

    def test_get_provider_exists(self):
        m = self._make_manager()
        provider_names = m.list_providers()
        assert len(provider_names) > 0

    def test_get_provider_missing(self):
        m = self._make_manager()
        assert m.get_provider("nonexistent_provider_xyz") is None

    def test_list_providers(self):
        m = self._make_manager()
        providers = m.list_providers()
        assert isinstance(providers, list)

    def test_login_unknown_provider(self):
        from fluid_build.cli.auth import AuthStatus

        m = self._make_manager()
        result = asyncio.run(m.login("nonexistent_xyz"))
        assert result.status == AuthStatus.ERROR

    def test_check_auth_unknown_provider(self):
        from fluid_build.cli.auth import AuthStatus

        m = self._make_manager()
        result = asyncio.run(m.check_auth("nonexistent_xyz"))
        assert result.status == AuthStatus.ERROR

    def test_logout_unknown_provider(self):
        m = self._make_manager()
        result = asyncio.run(m.logout("nonexistent_xyz"))
        assert result is False

    def test_get_provider_alias(self):
        m = self._make_manager()
        # gcp is an alias for google_cloud
        gcp = m.get_provider("gcp")
        gc = m.get_provider("google_cloud")
        # Both should be the same type
        if gcp and gc:
            assert type(gcp).__name__ == type(gc).__name__


# ---- CLI register/run functions ----


class TestAuthCLI:
    def test_register(self):
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        from fluid_build.cli.auth import register

        register(sub)
        # Should not raise

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_list(self, mock_manager_cls):
        from fluid_build.cli.auth import run

        mock_manager = MagicMock()
        mock_manager.list_providers.return_value = ["aws", "gcp", "azure"]
        mock_manager_cls.return_value = mock_manager
        args = MagicMock()
        args.verb = "list"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_unknown_verb(self, mock_manager_cls):
        from fluid_build.cli.auth import run

        mock_manager_cls.return_value = MagicMock()
        args = MagicMock()
        args.verb = "unknown_action"
        logger = logging.getLogger("test")
        result = run(args, logger)
        # Should handle gracefully
        assert isinstance(result, int)

    @patch("fluid_build.cli.auth.handle_login")
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_login(self, mock_manager_cls, mock_login):
        from fluid_build.cli.auth import run

        mock_manager_cls.return_value = MagicMock()
        mock_login.return_value = 0
        args = MagicMock()
        args.verb = "login"
        args.provider = "aws"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.auth.handle_status")
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_status(self, mock_manager_cls, mock_status):
        from fluid_build.cli.auth import run

        mock_manager_cls.return_value = MagicMock()
        mock_status.return_value = 0
        args = MagicMock()
        args.verb = "status"
        args.provider = None
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.auth.handle_logout")
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_logout(self, mock_manager_cls, mock_logout):
        from fluid_build.cli.auth import run

        mock_manager_cls.return_value = MagicMock()
        mock_logout.return_value = 0
        args = MagicMock()
        args.verb = "logout"
        args.provider = "gcp"
        logger = logging.getLogger("test")
        result = run(args, logger)
        assert result == 0


# ---- handle_login / handle_logout / handle_status ----


class TestHandleLogin:
    def test_login_success(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus, handle_login

        mgr = MagicMock()
        mgr.login = AsyncMock(
            return_value=AuthResult(
                provider="aws", status=AuthStatus.AUTHENTICATED, user_info={"account": "123"}
            )
        )
        result = asyncio.run(handle_login("aws", mgr, logging.getLogger("test")))
        assert result == 0

    def test_login_failure(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus, handle_login

        mgr = MagicMock()
        mgr.login = AsyncMock(
            return_value=AuthResult(
                provider="aws", status=AuthStatus.NOT_AUTHENTICATED, error_message="bad creds"
            )
        )
        result = asyncio.run(handle_login("aws", mgr, logging.getLogger("test")))
        assert result == 1

    def test_login_exception(self):
        from fluid_build.cli.auth import handle_login

        mgr = MagicMock()
        mgr.login = AsyncMock(side_effect=RuntimeError("boom"))
        result = asyncio.run(handle_login("aws", mgr, logging.getLogger("test")))
        assert result == 1


class TestHandleLogout:
    def test_logout_success(self):
        from fluid_build.cli.auth import handle_logout

        mgr = MagicMock()
        mgr.logout = AsyncMock(return_value=True)
        result = asyncio.run(handle_logout("aws", mgr, logging.getLogger("test")))
        assert result == 0

    def test_logout_failure(self):
        from fluid_build.cli.auth import handle_logout

        mgr = MagicMock()
        mgr.logout = AsyncMock(return_value=False)
        result = asyncio.run(handle_logout("aws", mgr, logging.getLogger("test")))
        assert result == 1


class TestHandleStatus:
    def test_status_single_provider_authenticated(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus, handle_status

        mgr = MagicMock()
        mgr.check_auth = AsyncMock(
            return_value=AuthResult(
                provider="aws", status=AuthStatus.AUTHENTICATED, user_info={"account": "123"}
            )
        )
        result = asyncio.run(handle_status("aws", mgr, logging.getLogger("test")))
        assert result == 0

    def test_status_single_provider_not_authenticated(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus, handle_status

        mgr = MagicMock()
        mgr.check_auth = AsyncMock(
            return_value=AuthResult(provider="aws", status=AuthStatus.NOT_AUTHENTICATED)
        )
        result = asyncio.run(handle_status("aws", mgr, logging.getLogger("test")))
        assert result == 1

    def test_status_all_providers(self):
        from fluid_build.cli.auth import AuthResult, AuthStatus, handle_status

        mgr = MagicMock()
        mgr.list_providers.return_value = ["aws", "gcp"]
        mgr.check_auth = AsyncMock(
            return_value=AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)
        )
        result = asyncio.run(handle_status(None, mgr, logging.getLogger("test")))
        assert result in (0, 1)
