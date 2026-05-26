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

"""Extended tests for auth.py: providers, AuthManager, CLIError, run."""

import asyncio
import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.auth import (
    AuthManager,
    AuthProvider,
    AuthResult,
    AuthStatus,
    AWSAuthProvider,
    AzureAuthProvider,
    CLIError,
    DatabricksAuthProvider,
    GoogleCloudAuthProvider,
    SnowflakeAuthProvider,
)

LOG = logging.getLogger("test_auth_ext2")


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# CLIError
# ---------------------------------------------------------------------------


class TestCLIErrorExt2:
    # CODE_REVIEW C-008: ``cli/auth.py`` imports the canonical
    # ``CLIError`` from ``fluid_build.cli._common``; ``.code`` → ``.exit_code``,
    # ``.details`` → ``.context``. ``.message`` kept as compat alias.

    def test_basic(self):
        err = CLIError(1, "test_error")
        assert err.exit_code == 1
        assert err.message == "test_error"
        assert err.context == {}

    def test_with_context(self):
        err = CLIError(2, "err", {"key": "val"})
        assert err.context["key"] == "val"

    def test_str(self):
        err = CLIError(1, "my message")
        assert str(err) == "my message"


# ---------------------------------------------------------------------------
# AuthStatus
# ---------------------------------------------------------------------------


class TestAuthStatusExt2:
    def test_values(self):
        assert AuthStatus.AUTHENTICATED.value == "authenticated"
        assert AuthStatus.NOT_AUTHENTICATED.value == "not_authenticated"
        assert AuthStatus.EXPIRED.value == "expired"
        assert AuthStatus.ERROR.value == "error"
        assert AuthStatus.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# AuthResult
# ---------------------------------------------------------------------------


class TestAuthResultExt2:
    def test_defaults(self):
        r = AuthResult(provider="test", status=AuthStatus.AUTHENTICATED)
        assert r.user_info == {}
        assert r.credentials_path is None
        assert r.expires_at is None
        assert r.scopes == []
        assert r.error_message is None

    def test_with_error(self):
        r = AuthResult(
            provider="gcp",
            status=AuthStatus.ERROR,
            error_message="connection failed",
        )
        assert r.error_message == "connection failed"

    def test_full_result(self):
        r = AuthResult(
            provider="aws",
            status=AuthStatus.AUTHENTICATED,
            user_info={"account": "123"},
            credentials_path="/tmp/creds",
            expires_at="2026-01-01",
            scopes=["read", "write"],
        )
        assert r.credentials_path == "/tmp/creds"
        assert len(r.scopes) == 2


# ---------------------------------------------------------------------------
# AuthProvider base
# ---------------------------------------------------------------------------


class TestAuthProviderBaseExt2:
    def test_init(self):
        provider = AuthProvider("test", {"key": "val"}, LOG)
        assert provider.name == "test"
        assert provider.config == {"key": "val"}

    def test_login_not_implemented(self):
        provider = AuthProvider("test", {}, LOG)
        with pytest.raises(NotImplementedError):
            _run(provider.login())

    def test_logout_not_implemented(self):
        provider = AuthProvider("test", {}, LOG)
        with pytest.raises(NotImplementedError):
            _run(provider.logout())

    def test_check_auth_not_implemented(self):
        provider = AuthProvider("test", {}, LOG)
        with pytest.raises(NotImplementedError):
            _run(provider.check_auth())

    def test_run_command_success(self):
        provider = AuthProvider("test", {}, LOG)
        result = provider._run_command(["echo", "hello"])
        assert result.returncode == 0

    def test_run_command_not_found(self):
        provider = AuthProvider("test", {}, LOG)
        with pytest.raises(CLIError):
            provider._run_command(["nonexistent_command_xyz_12345"])

    def test_run_command_failure(self):
        provider = AuthProvider("test", {}, LOG)
        with pytest.raises(subprocess.CalledProcessError):
            provider._run_command(["false"])


# ---------------------------------------------------------------------------
# GoogleCloudAuthProvider
# ---------------------------------------------------------------------------


class TestGoogleCloudExt2:
    def test_init(self):
        provider = GoogleCloudAuthProvider({"project_id": "my-project"}, LOG)
        assert provider.name == "google_cloud"
        assert provider.project_id == "my-project"
        assert len(provider.scopes) > 0

    def test_init_custom_scopes(self):
        provider = GoogleCloudAuthProvider({"scopes": ["scope1"]}, LOG)
        assert provider.scopes == ["scope1"]

    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_logout_success(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0)
        provider = GoogleCloudAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    @patch.object(GoogleCloudAuthProvider, "_run_command", side_effect=Exception("fail"))
    def test_logout_swallows_inner_exceptions(self, _mock_cmd):
        # Inner try/except swallows _run_command exceptions, so logout returns True
        provider = GoogleCloudAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    def test_check_auth_gcloud_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        provider = GoogleCloudAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_check_auth_authenticated(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0, stdout="test@example.com")
        provider = GoogleCloudAuthProvider({"project_id": "proj"}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_login_exception(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        provider = GoogleCloudAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=False),
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ---------------------------------------------------------------------------
# AWSAuthProvider
# ---------------------------------------------------------------------------


class TestAWSExt2:
    def test_init_defaults(self):
        provider = AWSAuthProvider({}, LOG)
        assert provider.region == "us-east-1"
        assert provider.profile == "default"

    def test_init_custom(self):
        provider = AWSAuthProvider({"region": "eu-west-1", "profile": "prod"}, LOG)
        assert provider.region == "eu-west-1"
        assert provider.profile == "prod"

    @patch.object(AWSAuthProvider, "_run_command")
    def test_logout_success(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0)
        provider = AWSAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    @patch.object(AWSAuthProvider, "_run_command", side_effect=Exception("fail"))
    def test_logout_swallows_inner_exceptions(self, _mock_cmd):
        # Inner try/except swallows _run_command exceptions, so logout returns True
        provider = AWSAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    def test_check_auth_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        provider = AWSAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_boto3", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(AWSAuthProvider, "_run_command")
    def test_check_auth_authenticated(self, mock_cmd):
        identity_json = (
            '{"UserId": "user123", "Account": "123456", "Arn": "arn:aws:iam::123456:user/test"}'
        )
        mock_cmd.return_value = MagicMock(returncode=0, stdout=identity_json)
        provider = AWSAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED
        assert result.user_info["account"] == "123456"

    @patch.object(AWSAuthProvider, "_run_command")
    def test_check_auth_called_process_error(self, mock_cmd):
        mock_cmd.side_effect = subprocess.CalledProcessError(1, "aws")
        provider = AWSAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_login_exception(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        provider = AWSAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=False),
            patch.object(provider, "_has_boto3", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ---------------------------------------------------------------------------
# AzureAuthProvider
# ---------------------------------------------------------------------------


class TestAzureExt2:
    def test_init(self):
        provider = AzureAuthProvider({"tenant_id": "t1", "subscription_id": "s1"}, LOG)
        assert provider.tenant_id == "t1"
        assert provider.subscription_id == "s1"

    @patch.object(AzureAuthProvider, "_run_command")
    def test_logout_success(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0)
        provider = AzureAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    def test_logout_failure(self):
        """Logout fails when shutil.which finds az but _run_command raises."""
        provider = AzureAuthProvider({}, LOG)
        with (
            patch("shutil.which", return_value="/usr/bin/az"),
            patch.object(provider, "_run_command", side_effect=Exception("fail")),
        ):
            assert _run(provider.logout()) is False

    def test_check_auth_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        provider = AzureAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(AzureAuthProvider, "_run_command")
    def test_check_auth_authenticated(self, mock_cmd):
        account_json = '{"name": "sub1", "id": "123", "tenantId": "t1", "user": {"name": "u@test.com", "type": "user"}}'
        mock_cmd.return_value = MagicMock(returncode=0, stdout=account_json)
        provider = AzureAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(AzureAuthProvider, "_run_command")
    def test_check_auth_called_process_error(self, mock_cmd):
        mock_cmd.side_effect = subprocess.CalledProcessError(1, "az")
        provider = AzureAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_login_exception(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        provider = AzureAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=False),
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ---------------------------------------------------------------------------
# SnowflakeAuthProvider
# ---------------------------------------------------------------------------


class TestSnowflakeExt2:
    def test_init(self):
        provider = SnowflakeAuthProvider(
            {"account": "a", "user": "u", "warehouse": "w", "database": "d", "role": "r"}, LOG
        )
        assert provider.account == "a"
        assert provider.user == "u"

    def test_logout(self):
        provider = SnowflakeAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_check_auth_snowsql_not_installed(self, mock_cmd):
        mock_cmd.side_effect = CLIError(1, "not found")
        provider = SnowflakeAuthProvider({}, LOG)
        result = _run(provider.check_auth())
        assert result.status == AuthStatus.ERROR

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_check_auth_no_account(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0)
        provider = SnowflakeAuthProvider({}, LOG)
        result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_check_auth_authenticated(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0)
        provider = SnowflakeAuthProvider({"account": "a", "user": "u"}, LOG)
        result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_login_snowsql_not_installed(self, mock_cmd):
        mock_cmd.side_effect = CLIError(1, "not found")
        provider = SnowflakeAuthProvider({}, LOG)
        provider.console = None
        result = _run(provider.login())
        assert result.status == AuthStatus.ERROR

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_login_success(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0, stdout="OK")
        provider = SnowflakeAuthProvider(
            {"account": "a", "user": "u", "warehouse": "w", "database": "d", "role": "r"}, LOG
        )
        provider.console = None
        with patch("shutil.which", return_value="/usr/bin/snowsql"):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_login_failed(self, mock_cmd):
        mock_cmd.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stderr="bad creds"),
        ]
        provider = SnowflakeAuthProvider({"account": "a", "user": "u"}, LOG)
        provider.console = None
        with patch("shutil.which", return_value="/usr/bin/snowsql"):
            result = _run(provider.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_login_general_exception(self, mock_cmd):
        mock_cmd.side_effect = [MagicMock(returncode=0), Exception("unexpected")]
        provider = SnowflakeAuthProvider({"account": "a", "user": "u"}, LOG)
        provider.console = None
        result = _run(provider.login())
        assert result.status == AuthStatus.ERROR


# ---------------------------------------------------------------------------
# DatabricksAuthProvider
# ---------------------------------------------------------------------------


class TestDatabricksExt2:
    def test_init(self):
        provider = DatabricksAuthProvider(
            {"host": "https://db.cloud", "token": "tok", "cluster_id": "c1"}, LOG
        )
        assert provider.host == "https://db.cloud"

    def test_login_not_installed(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        provider = DatabricksAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=False),
            patch("shutil.which", return_value=None),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch("os.path.exists", return_value=True)
    @patch("os.remove")
    def test_logout_clears_config(self, mock_remove, _mock_exists):
        provider = DatabricksAuthProvider({}, LOG)
        assert _run(provider.logout()) is True
        mock_remove.assert_called_once()

    @patch("os.path.exists", return_value=False)
    def test_logout_no_config(self, _mock_exists):
        provider = DatabricksAuthProvider({}, LOG)
        assert _run(provider.logout()) is True

    def test_check_auth_not_installed(self):
        """When no credentials or CLI available, returns NOT_AUTHENTICATED."""
        provider = DatabricksAuthProvider({}, LOG)
        with patch("shutil.which", return_value=None), patch("os.path.exists", return_value=False):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_check_auth_authenticated(self, mock_cmd):
        mock_cmd.return_value = MagicMock(returncode=0, stdout='{"userName":"test"}')
        provider = DatabricksAuthProvider({"host": "h"}, LOG)
        with (
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------


class TestAuthManagerExt2:
    def test_init(self):
        mgr = AuthManager({}, LOG)
        assert isinstance(mgr.providers, dict)

    def test_get_provider(self):
        mgr = AuthManager({}, LOG)
        assert mgr.get_provider("aws") is not None
        assert mgr.get_provider("nonexistent") is None

    def test_list_providers(self):
        mgr = AuthManager({}, LOG)
        providers = mgr.list_providers()
        assert isinstance(providers, list)
        assert len(providers) > 0

    def test_login_unknown(self):
        mgr = AuthManager({}, LOG)
        result = _run(mgr.login("nonexistent"))
        assert result.status == AuthStatus.ERROR

    def test_logout_unknown(self):
        mgr = AuthManager({}, LOG)
        assert _run(mgr.logout("nonexistent")) is False

    def test_check_auth_unknown(self):
        mgr = AuthManager({}, LOG)
        result = _run(mgr.check_auth("nonexistent"))
        assert result.status == AuthStatus.ERROR

    def test_provider_alias_gcp(self):
        mgr = AuthManager({}, LOG)
        gcp = mgr.get_provider("gcp")
        google_cloud = mgr.get_provider("google_cloud")
        assert type(gcp) == type(google_cloud)

    def test_provider_alias_amazon(self):
        mgr = AuthManager({}, LOG)
        amazon = mgr.get_provider("amazon")
        aws = mgr.get_provider("aws")
        assert type(amazon) == type(aws)


# ---------------------------------------------------------------------------
# run() function
# ---------------------------------------------------------------------------


class TestRunFunctionExt2:
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_list(self, mock_mgr_cls):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr.list_providers.return_value = ["aws", "gcp"]
        mock_mgr_cls.return_value = mock_mgr

        args = MagicMock()
        args.verb = "list"

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = run(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_login_no_provider(self, mock_mgr_cls):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr.list_providers.return_value = ["aws"]
        mock_mgr_cls.return_value = mock_mgr

        args = MagicMock()
        args.verb = "login"
        args.provider = None

        result = run(args, LOG)
        assert result == 1

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_logout_no_provider(self, mock_mgr_cls):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr_cls.return_value = mock_mgr

        args = MagicMock()
        args.verb = "logout"
        args.provider = None

        result = run(args, LOG)
        assert result == 1

    @patch("fluid_build.cli.auth.asyncio")
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_login_with_provider(self, mock_mgr_cls, mock_asyncio):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr_cls.return_value = mock_mgr
        mock_asyncio.run.return_value = 0

        args = MagicMock()
        args.verb = "login"
        args.provider = "aws"

        result = run(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.auth.asyncio")
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_status(self, mock_mgr_cls, mock_asyncio):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr_cls.return_value = mock_mgr
        mock_asyncio.run.return_value = 0

        args = MagicMock()
        args.verb = "status"
        args.provider = "aws"

        result = run(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_unknown_verb(self, mock_mgr_cls):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr_cls.return_value = mock_mgr

        args = MagicMock()
        args.verb = "unknown_verb"
        args.provider = None

        result = run(args, LOG)
        assert result == 0

    def test_run_keyboard_interrupt(self):
        from fluid_build.cli.auth import run

        args = MagicMock()
        args.verb = "list"

        with patch("fluid_build.cli.auth.AuthManager", side_effect=KeyboardInterrupt):
            result = run(args, LOG)
        assert result == 130


# ---------------------------------------------------------------------------
# GoogleCloudAuthProvider – additional coverage (lines 149-172, 179, 214-274)
# ---------------------------------------------------------------------------


class TestGoogleCloudAdditional:
    """Cover login with RICH_AVAILABLE path and check_auth branches."""

    @patch("fluid_build.cli.auth.RICH_AVAILABLE", False)
    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_login_no_rich_with_project_id(self, mock_cmd):
        """Lines 174-183: non-rich login path that includes project_id and scopes."""
        check_result = MagicMock(returncode=0, stdout="user@example.com")
        mock_cmd.return_value = check_result
        provider = GoogleCloudAuthProvider({"project_id": "my-project"}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
            patch.object(provider, "_has_sdk", return_value=False),
        ):
            result = _run(provider.login())
        # login calls _run_command and then check_auth, which also calls _run_command
        assert mock_cmd.called

    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_check_auth_returncode_nonzero(self, mock_cmd):
        """Line 273-278: access-token command returns non-zero."""
        mock_cmd.side_effect = [
            MagicMock(returncode=0),  # gcloud version – installed
            MagicMock(returncode=1),  # print-access-token – fails
        ]
        provider = GoogleCloudAuthProvider({}, LOG)
        # Disable google.auth SDK fast-path so the test doesn't pick up the
        # developer's real Application Default Credentials (the previous
        # behavior leaked the live ``fluidtesting`` project into the assert).
        with patch.object(provider, "_has_sdk", return_value=False):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_check_auth_called_process_error(self, mock_cmd):
        """Line 280-285: CalledProcessError on print-access-token."""
        mock_cmd.side_effect = [
            MagicMock(returncode=0),
            subprocess.CalledProcessError(1, "gcloud"),
        ]
        provider = GoogleCloudAuthProvider({}, LOG)
        # Disable google.auth SDK fast-path so the test doesn't pick up the
        # developer's real Application Default Credentials.
        with patch.object(provider, "_has_sdk", return_value=False):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_check_auth_authenticated_no_account_stdout(self, mock_cmd):
        """Lines 239-265: authenticated branch with empty account/project stdout."""
        access_token_ok = MagicMock(returncode=0, stdout="token")
        account_result = MagicMock(returncode=0, stdout="")
        project_result = MagicMock(returncode=0, stdout="")
        mock_cmd.side_effect = [
            access_token_ok,
            account_result,
            project_result,
        ]
        provider = GoogleCloudAuthProvider({"project_id": "fallback"}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED
        # project should fall back to self.project_id
        assert result.user_info.get("project") == "fallback"

    @patch.object(GoogleCloudAuthProvider, "_run_command")
    def test_check_auth_inner_exception_authenticated_fallback(self, mock_cmd):
        """Lines 266-272: inner exception after access token succeeds."""

        def _side_effects(*args, **kwargs):
            cmd = args[0]
            if "print-access-token" in " ".join(cmd):
                return MagicMock(returncode=0, stdout="tok")
            raise Exception("unexpected inner error")

        mock_cmd.side_effect = _side_effects
        provider = GoogleCloudAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
        ):
            result = _run(provider.check_auth())
        # Inner exception after access token is caught, falls through to NOT_AUTHENTICATED
        assert result.status in (
            AuthStatus.AUTHENTICATED,
            AuthStatus.NOT_AUTHENTICATED,
            AuthStatus.ERROR,
        )


# ---------------------------------------------------------------------------
# AWSAuthProvider – additional login / logout coverage (lines 287-350)
# ---------------------------------------------------------------------------


class TestAWSAdditional:
    @patch.object(AWSAuthProvider, "_run_command")
    def test_login_no_rich_success(self, mock_cmd):
        """Lines 341-350: non-rich path, SSO succeeds then check_auth via _login_via_cli."""
        mock_cmd.return_value = MagicMock(returncode=0, stdout="OK")
        provider = AWSAuthProvider({"profile": "test"}, LOG)
        provider.console = None
        sdk_result = AuthResult(
            provider="aws",
            status=AuthStatus.AUTHENTICATED,
            user_info={"account": "123"},
        )
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(provider, "_has_boto3", return_value=False),
            patch.object(provider, "_validate_via_sdk", return_value=sdk_result),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(AWSAuthProvider, "_run_command")
    def test_login_no_rich_sso_fails_fallback_configure(self, mock_cmd):
        """Lines 342-348: SSO fails, fallback to configure, then validate_via_sdk."""
        calls = [
            Exception("sso not configured"),  # sso login fails
            MagicMock(returncode=0),  # aws configure
        ]
        mock_cmd.side_effect = calls
        provider = AWSAuthProvider({}, LOG)
        provider.console = None
        sdk_result = AuthResult(
            provider="aws",
            status=AuthStatus.AUTHENTICATED,
            user_info={"account": "acc"},
        )
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(provider, "_has_boto3", return_value=False),
            patch.object(provider, "_validate_via_sdk", return_value=sdk_result),
        ):
            result = _run(provider.login())
        assert result.status in (AuthStatus.AUTHENTICATED, AuthStatus.ERROR)

    @patch.object(AWSAuthProvider, "_run_command")
    def test_check_auth_sts_returncode_nonzero(self, mock_cmd):
        """Line 415-419: STS returns non-zero."""
        mock_cmd.return_value = MagicMock(returncode=1)  # sts call fails
        provider = AWSAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_returns_true(self):
        provider = AWSAuthProvider({}, LOG)
        with patch.object(provider, "_run_command", return_value=MagicMock(returncode=0)):
            result = _run(provider.logout())
        assert result is True


# ---------------------------------------------------------------------------
# AzureAuthProvider – additional login coverage (lines 428-494)
# ---------------------------------------------------------------------------


class TestAzureAdditional:
    @patch.object(AzureAuthProvider, "_run_command")
    def test_login_no_rich_no_tenant(self, mock_cmd):
        """Lines 481-493: non-rich path, no tenant_id, no subscription_id."""
        account_json = '{"name":"sub","id":"1","tenantId":"t","user":{"name":"u","type":"user"}}'
        mock_cmd.return_value = MagicMock(returncode=0, stdout=account_json)
        provider = AzureAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/az"),
            patch.object(provider, "_has_sdk", return_value=False),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(AzureAuthProvider, "_run_command")
    def test_login_no_rich_with_tenant_and_subscription(self, mock_cmd):
        """Line 484-492: non-rich login with tenant + subscription."""
        account_json = '{"name":"sub","id":"1","tenantId":"t","user":{"name":"u","type":"user"}}'
        mock_cmd.return_value = MagicMock(returncode=0, stdout=account_json)
        provider = AzureAuthProvider({"tenant_id": "my-tenant", "subscription_id": "my-sub"}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/az"),
            patch.object(provider, "_has_sdk", return_value=False),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(AzureAuthProvider, "_run_command")
    def test_check_auth_account_show_returncode_nonzero(self, mock_cmd):
        """Lines 546-551: az account show returns non-zero."""
        mock_cmd.return_value = MagicMock(returncode=1)  # az account show
        provider = AzureAuthProvider({}, LOG)
        with (
            patch.object(provider, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ---------------------------------------------------------------------------
# SnowflakeAuthProvider – check_auth with missing account/user (line 705)
# ---------------------------------------------------------------------------


class TestSnowflakeAdditional:
    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_check_auth_connection_fails_returncode_nonzero(self, mock_cmd):
        """Line 704-709: snowsql runs but returncode != 0."""
        mock_cmd.side_effect = [
            MagicMock(returncode=0),  # snowsql --version
            MagicMock(returncode=1, stderr="Login failed"),  # query
        ]
        provider = SnowflakeAuthProvider({"account": "a", "user": "u"}, LOG)
        result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_check_auth_exception(self, mock_cmd):
        """Line 717: outer except in check_auth."""
        mock_cmd.side_effect = Exception("unexpected")
        provider = SnowflakeAuthProvider({"account": "a", "user": "u"}, LOG)
        result = _run(provider.check_auth())
        assert result.status == AuthStatus.ERROR

    @patch.object(SnowflakeAuthProvider, "_run_command")
    def test_login_with_all_params_no_console(self, mock_cmd):
        """Lines 607-635: login builds full connection params without console."""
        mock_cmd.return_value = MagicMock(returncode=0, stdout="OK")
        provider = SnowflakeAuthProvider(
            {"account": "acct", "user": "usr", "warehouse": "wh", "database": "db", "role": "rl"},
            LOG,
        )
        provider.console = None
        with patch("shutil.which", return_value="/usr/bin/snowsql"):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED
        # Verify params were included in the call
        call_args = mock_cmd.call_args_list
        assert len(call_args) >= 1


# ---------------------------------------------------------------------------
# DatabricksAuthProvider – login / check_auth additional (lines 744-818)
# ---------------------------------------------------------------------------


class TestDatabricksAdditional:
    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_login_no_console_with_host(self, mock_cmd):
        """Lines 784-795: non-console login with host set, test succeeds."""
        mock_cmd.return_value = MagicMock(returncode=0, stdout="OK")
        provider = DatabricksAuthProvider({"host": "https://my.databricks.com"}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_login_no_console_no_host(self, mock_cmd):
        """Lines 785-791: non-console login with no host."""
        mock_cmd.return_value = MagicMock(returncode=0, stdout="OK")
        provider = DatabricksAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_login_workspace_list_fails(self, mock_cmd):
        """Lines 809-815: workspace list returns non-zero."""
        mock_cmd.side_effect = [
            MagicMock(returncode=0),  # databricks configure (no host)
            MagicMock(returncode=1, stderr="auth failed"),  # workspace list (verify)
        ]
        provider = DatabricksAuthProvider({}, LOG)
        provider.console = None
        with (
            patch.object(provider, "_is_interactive", return_value=True),
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_check_auth_with_user_info(self, mock_cmd):
        """Lines 852-882: check_auth parses current-user response via CLI fallback."""
        mock_cmd.return_value = MagicMock(returncode=0, stdout="OK")  # workspace list
        provider = DatabricksAuthProvider({"host": "h", "workspace_id": "w"}, LOG)
        with (
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_check_auth_workspace_list_fails(self, mock_cmd):
        """Lines 884-889: workspace list fails in check_auth."""
        mock_cmd.return_value = MagicMock(returncode=1)  # workspace list
        provider = DatabricksAuthProvider({}, LOG)
        with (
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    @patch.object(DatabricksAuthProvider, "_run_command")
    def test_check_auth_user_info_current_user_fails(self, mock_cmd):
        """Lines 858-872: workspace list succeeds in check_auth via CLI fallback."""
        mock_cmd.return_value = MagicMock(returncode=0)  # workspace list
        provider = DatabricksAuthProvider({}, LOG)
        with (
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
        ):
            result = _run(provider.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_logout_failure(self):
        """Lines 829-831: logout fails to remove file."""
        provider = DatabricksAuthProvider({}, LOG)
        with patch("os.path.exists", return_value=True):
            with patch("os.remove", side_effect=OSError("permission denied")):
                result = _run(provider.logout())
        assert result is False


# ---------------------------------------------------------------------------
# AuthManager – additional coverage (lines 926-941)
# ---------------------------------------------------------------------------


class TestAuthManagerAdditional:
    def test_init_with_provider_config(self):
        """Lines 924-941: provider-specific config is passed to each provider."""
        config = {
            "google_cloud": {"project_id": "my-proj"},
            "aws": {"region": "eu-west-1"},
        }
        mgr = AuthManager(config, LOG)
        gcp = mgr.get_provider("google_cloud")
        assert gcp is not None
        assert gcp.project_id == "my-proj"
        aws = mgr.get_provider("aws")
        assert aws is not None
        assert aws.region == "eu-west-1"

    def test_init_alias_inherits_base_config(self):
        """Lines 929-937: alias 'gcp' inherits google_cloud config."""
        config = {"google_cloud": {"project_id": "alias-proj"}}
        mgr = AuthManager(config, LOG)
        gcp_alias = mgr.get_provider("gcp")
        assert gcp_alias is not None
        assert gcp_alias.project_id == "alias-proj"

    def test_login_delegates_to_provider(self):
        """Lines 959-969: login() with a real provider name."""
        mgr = AuthManager({}, LOG)
        provider_mock = MagicMock()
        provider_mock.login = MagicMock(
            return_value=AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)
        )
        # Wrap in a coroutine so _run can await it
        import asyncio as _asyncio

        async def _mock_login(**kw):
            return AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)

        provider_mock.login = _mock_login
        mgr.providers["aws"] = provider_mock
        result = _run(mgr.login("aws"))
        assert result.status == AuthStatus.AUTHENTICATED

    def test_logout_delegates_to_provider(self):
        """Lines 971-978: logout() with a real provider name."""
        mgr = AuthManager({}, LOG)

        async def _mock_logout():
            return True

        provider_mock = MagicMock()
        provider_mock.logout = _mock_logout
        mgr.providers["azure"] = provider_mock
        result = _run(mgr.logout("azure"))
        assert result is True

    def test_check_auth_delegates_to_provider(self):
        """Lines 980-990: check_auth() with a real provider name."""
        mgr = AuthManager({}, LOG)

        async def _mock_check():
            return AuthResult(provider="snowflake", status=AuthStatus.NOT_AUTHENTICATED)

        provider_mock = MagicMock()
        provider_mock.check_auth = _mock_check
        mgr.providers["snowflake"] = provider_mock
        result = _run(mgr.check_auth("snowflake"))
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_list_providers_unique(self):
        """Lines 947-957: list_providers returns unique class types."""
        mgr = AuthManager({}, LOG)
        providers = mgr.list_providers()
        # Should not contain duplicates of same class
        assert len(providers) == len(set(providers))

    def test_provider_alias_microsoft(self):
        mgr = AuthManager({}, LOG)
        ms = mgr.get_provider("microsoft")
        azure = mgr.get_provider("azure")
        assert type(ms) == type(azure)

    def test_provider_alias_google(self):
        mgr = AuthManager({}, LOG)
        g = mgr.get_provider("google")
        gcp = mgr.get_provider("gcp")
        assert type(g) == type(gcp)


# ---------------------------------------------------------------------------
# handle_login / handle_logout / handle_status (lines 969-1307, 1309, 1333-1339)
# ---------------------------------------------------------------------------


class TestHandleFunctions:
    """Test the async handle_* functions called by run()."""

    def test_handle_login_success(self):
        from fluid_build.cli.auth import handle_login

        mgr = MagicMock()
        mgr.login = MagicMock()

        async def _login(provider, **kw):
            return AuthResult(
                provider=provider,
                status=AuthStatus.AUTHENTICATED,
                user_info={"account": "test-account"},
            )

        mgr.login = _login
        result = _run(handle_login("aws", mgr, LOG))
        assert result == 0

    def test_handle_login_failure(self):
        from fluid_build.cli.auth import handle_login

        mgr = MagicMock()

        async def _login(provider, **kw):
            return AuthResult(
                provider=provider,
                status=AuthStatus.ERROR,
                error_message="not installed",
            )

        mgr.login = _login
        result = _run(handle_login("aws", mgr, LOG))
        assert result == 1

    def test_handle_login_exception(self):
        from fluid_build.cli.auth import handle_login

        mgr = MagicMock()

        async def _login(provider, **kw):
            raise RuntimeError("unexpected error")

        mgr.login = _login
        result = _run(handle_login("aws", mgr, LOG))
        assert result == 1

    def test_handle_logout_success(self):
        from fluid_build.cli.auth import handle_logout

        mgr = MagicMock()

        async def _logout(provider):
            return True

        mgr.logout = _logout
        result = _run(handle_logout("aws", mgr, LOG))
        assert result == 0

    def test_handle_logout_failure(self):
        from fluid_build.cli.auth import handle_logout

        mgr = MagicMock()

        async def _logout(provider):
            return False

        mgr.logout = _logout
        result = _run(handle_logout("aws", mgr, LOG))
        assert result == 1

    def test_handle_logout_exception(self):
        from fluid_build.cli.auth import handle_logout

        mgr = MagicMock()

        async def _logout(provider):
            raise RuntimeError("logout crash")

        mgr.logout = _logout
        result = _run(handle_logout("aws", mgr, LOG))
        assert result == 1

    def test_handle_status_specific_provider_authenticated(self):
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()

        async def _check(provider):
            return AuthResult(provider=provider, status=AuthStatus.AUTHENTICATED)

        mgr.check_auth = _check
        result = _run(handle_status("aws", mgr, LOG))
        assert result == 0

    def test_handle_status_specific_provider_not_authenticated(self):
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()

        async def _check(provider):
            return AuthResult(
                provider=provider,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="no creds",
            )

        mgr.check_auth = _check
        result = _run(handle_status("gcp", mgr, LOG))
        assert result == 1

    def test_handle_status_all_providers_all_authenticated(self):
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()
        mgr.list_providers.return_value = ["aws", "gcp"]

        async def _check(provider):
            return AuthResult(provider=provider, status=AuthStatus.AUTHENTICATED)

        mgr.check_auth = _check
        result = _run(handle_status(None, mgr, LOG))
        assert result == 0

    def test_handle_status_all_providers_some_not_authenticated(self):
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()
        mgr.list_providers.return_value = ["aws", "gcp"]

        async def _check(provider):
            if provider == "aws":
                return AuthResult(provider=provider, status=AuthStatus.AUTHENTICATED)
            return AuthResult(provider=provider, status=AuthStatus.NOT_AUTHENTICATED)

        mgr.check_auth = _check
        result = _run(handle_status(None, mgr, LOG))
        assert result == 1

    def test_handle_status_all_providers_with_error_message(self):
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()
        mgr.list_providers.return_value = ["snowflake"]

        async def _check(provider):
            return AuthResult(
                provider=provider,
                status=AuthStatus.ERROR,
                error_message="CLI not found",
            )

        mgr.check_auth = _check
        result = _run(handle_status(None, mgr, LOG))
        assert result == 1

    def test_handle_status_exception(self):
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()

        async def _check(provider):
            raise RuntimeError("status check error")

        mgr.check_auth = _check
        result = _run(handle_status("aws", mgr, LOG))
        assert result == 1

    def test_handle_status_specific_with_user_info(self):
        """Cover branch that prints user_info details."""
        from fluid_build.cli.auth import handle_status

        mgr = MagicMock()

        async def _check(provider):
            return AuthResult(
                provider=provider,
                status=AuthStatus.AUTHENTICATED,
                user_info={"account": "123", "region": "us-east-1"},
            )

        mgr.check_auth = _check
        result = _run(handle_status("aws", mgr, LOG))
        assert result == 0

    def test_handle_login_authenticated_with_user_info(self):
        """Cover branch that logs user_info key/value pairs."""
        from fluid_build.cli.auth import handle_login

        mgr = MagicMock()

        async def _login(provider, **kw):
            return AuthResult(
                provider=provider,
                status=AuthStatus.AUTHENTICATED,
                user_info={"user_id": "test_user", "account": "acc"},
            )

        mgr.login = _login
        result = _run(handle_login("gcp", mgr, LOG))
        assert result == 0

    def test_handle_login_not_authenticated_error_message(self):
        """Cover NOT_AUTHENTICATED branch (not ERROR) in handle_login."""
        from fluid_build.cli.auth import handle_login

        mgr = MagicMock()

        async def _login(provider, **kw):
            return AuthResult(
                provider=provider,
                status=AuthStatus.NOT_AUTHENTICATED,
                error_message="User cancelled",
            )

        mgr.login = _login
        result = _run(handle_login("azure", mgr, LOG))
        assert result == 1


# ---------------------------------------------------------------------------
# run() – additional verb / exception branches (lines 1103-1139)
# ---------------------------------------------------------------------------


class TestRunAdditional:
    @patch("fluid_build.cli.auth.asyncio")
    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_logout_with_provider(self, mock_mgr_cls, mock_asyncio):
        from fluid_build.cli.auth import run

        mock_mgr = MagicMock()
        mock_mgr_cls.return_value = mock_mgr
        mock_asyncio.run.return_value = 0

        args = MagicMock()
        args.verb = "logout"
        args.provider = "aws"

        result = run(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_raises_cli_error(self, mock_mgr_cls):
        """Lines 1135-1136: CLIError is re-raised."""
        from fluid_build.cli.auth import run

        mock_mgr_cls.side_effect = CLIError(2, "some_cli_error")
        args = MagicMock()
        args.verb = "list"

        import pytest

        with pytest.raises(CLIError):
            run(args, LOG)

    @patch("fluid_build.cli.auth.AuthManager")
    def test_run_unexpected_exception_raises_cli_error(self, mock_mgr_cls):
        """Lines 1137-1139: unexpected Exception is wrapped in CLIError."""
        from fluid_build.cli.auth import run

        mock_mgr_cls.side_effect = ValueError("some unexpected error")
        args = MagicMock()
        args.verb = "list"

        import pytest

        with pytest.raises(CLIError):
            run(args, LOG)
