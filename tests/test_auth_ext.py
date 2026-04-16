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

"""Extended unit tests for fluid_build.cli.auth module."""

import asyncio
import logging
import subprocess
from unittest.mock import Mock, patch

import pytest

from fluid_build.cli.auth import (
    AuthManager,
    AuthResult,
    AuthStatus,
    AWSAuthProvider,
    AzureAuthProvider,
    CLIError,
    DatabricksAuthProvider,
    GoogleCloudAuthProvider,
    SnowflakeAuthProvider,
    handle_login,
    handle_logout,
    handle_status,
    run,
)

logger = logging.getLogger("test_auth_ext")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_coro(coro):
    """Run a coroutine in a new event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_args(**kwargs):
    defaults = {
        "verb": "list",
        "provider": None,
    }
    defaults.update(kwargs)
    args = Mock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


def _make_manager():
    return AuthManager({}, logger)


def _make_completed_process(returncode=0, stdout="", stderr=""):
    cp = Mock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# AuthStatus
# ---------------------------------------------------------------------------


class TestAuthStatus:
    def test_authenticated_value(self):
        assert AuthStatus.AUTHENTICATED.value == "authenticated"

    def test_not_authenticated_value(self):
        assert AuthStatus.NOT_AUTHENTICATED.value == "not_authenticated"

    def test_expired_value(self):
        assert AuthStatus.EXPIRED.value == "expired"

    def test_error_value(self):
        assert AuthStatus.ERROR.value == "error"

    def test_unknown_value(self):
        assert AuthStatus.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# AuthResult
# ---------------------------------------------------------------------------


class TestAuthResult:
    def test_basic_fields(self):
        r = AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)
        assert r.provider == "aws"
        assert r.status == AuthStatus.AUTHENTICATED
        assert r.user_info == {}
        assert r.credentials_path is None
        assert r.expires_at is None
        assert r.scopes == []
        assert r.error_message is None

    def test_error_message(self):
        r = AuthResult(
            provider="gcp",
            status=AuthStatus.ERROR,
            error_message="gcloud not installed",
        )
        assert r.error_message == "gcloud not installed"


# ---------------------------------------------------------------------------
# CLIError (auth module's own CLIError)
# ---------------------------------------------------------------------------


class TestCLIError:
    def test_code_attribute(self):
        err = CLIError(1, "something_failed")
        assert err.code == 1

    def test_message_attribute(self):
        err = CLIError(2, "auth_failed")
        assert err.message == "auth_failed"

    def test_details_default_empty(self):
        err = CLIError(1, "msg")
        assert err.details == {}

    def test_details_populated(self):
        err = CLIError(1, "msg", {"key": "value"})
        assert err.details["key"] == "value"

    def test_is_exception(self):
        with pytest.raises(CLIError):
            raise CLIError(1, "boom")


# ---------------------------------------------------------------------------
# AuthProvider._run_command
# ---------------------------------------------------------------------------


class TestAuthProviderRunCommand:
    def _provider(self):
        return GoogleCloudAuthProvider({}, logger)

    def test_success_returns_completed_process(self):
        cp = _make_completed_process(returncode=0, stdout="v1.0")
        with patch("subprocess.run", return_value=cp):
            result = self._provider()._run_command(["gcloud", "version"])
        assert result.returncode == 0

    def test_file_not_found_raises_cli_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("not found")):
            with pytest.raises(CLIError) as exc_info:
                self._provider()._run_command(["nonexistent_cmd"])
        assert exc_info.value.code == 1

    def test_called_process_error_reraises(self):
        cpe = subprocess.CalledProcessError(1, ["gcloud"])
        with patch("subprocess.run", side_effect=cpe):
            with pytest.raises(subprocess.CalledProcessError):
                self._provider()._run_command(["gcloud", "fail"])


# ---------------------------------------------------------------------------
# GoogleCloudAuthProvider
# ---------------------------------------------------------------------------


class TestGCPAuthProvider:
    def _provider(self):
        return GoogleCloudAuthProvider({}, logger)

    def test_check_auth_authenticated(self):
        token_cp = _make_completed_process(0, "ya29.token")
        account_cp = _make_completed_process(0, "user@example.com")
        project_cp = _make_completed_process(0, "my-project")

        side_effects = [token_cp, account_cp, project_cp]
        p = self._provider()
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
            patch("subprocess.run", side_effect=side_effects),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED
        assert result.user_info.get("account") == "user@example.com"

    def test_check_auth_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        p = self._provider()
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_returns_true(self):
        revoke_cp = _make_completed_process(0)
        with patch("subprocess.run", return_value=revoke_cp):
            result = _run_coro(self._provider().logout())
        assert result is True

    def test_login_exception_returns_not_authenticated(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        p = self._provider()
        with (
            patch.object(p, "_is_interactive", return_value=False),
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_coro(p.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ---------------------------------------------------------------------------
# AWSAuthProvider
# ---------------------------------------------------------------------------


class TestAWSAuthProvider:
    def _provider(self):
        return AWSAuthProvider({"region": "us-east-1", "profile": "default"}, logger)

    def test_check_auth_authenticated(self):
        identity_json = '{"UserId": "AIDA", "Account": "123", "Arn": "arn:aws"}'
        identity_cp = _make_completed_process(0, identity_json)

        p = self._provider()
        with (
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch("subprocess.run", return_value=identity_cp),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_configured(self):
        fail_cp = _make_completed_process(255, "", "Unable to locate credentials")

        p = self._provider()
        with (
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch("subprocess.run", return_value=fail_cp),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_check_auth_cli_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        p = self._provider()
        with (
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_returns_true(self):
        cp = _make_completed_process(0)
        with patch("subprocess.run", return_value=cp):
            result = _run_coro(self._provider().logout())
        assert result is True


# ---------------------------------------------------------------------------
# AzureAuthProvider
# ---------------------------------------------------------------------------


class TestAzureAuthProvider:
    def _provider(self):
        return AzureAuthProvider({}, logger)

    def test_check_auth_authenticated(self):
        account_json = '{"id": "sub-id", "name": "My Sub", "tenantId": "t-1", "user": {"name": "user@ms.com", "type": "user"}}'
        account_cp = _make_completed_process(0, account_json)

        p = self._provider()
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
            patch("subprocess.run", return_value=account_cp),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_authenticated(self):
        fail_cp = _make_completed_process(1, "[]", "Please run az login")

        p = self._provider()
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
            patch("subprocess.run", return_value=fail_cp),
        ):
            result = _run_coro(p.check_auth())
        assert result.status in (AuthStatus.NOT_AUTHENTICATED, AuthStatus.ERROR)

    def test_logout_returns_true(self):
        cp = _make_completed_process(0)
        with patch("subprocess.run", return_value=cp):
            result = _run_coro(self._provider().logout())
        assert result is True


# ---------------------------------------------------------------------------
# SnowflakeAuthProvider
# ---------------------------------------------------------------------------


class TestSnowflakeAuthProvider:
    def _provider(self):
        return SnowflakeAuthProvider(
            {"account": "myaccount", "user": "myuser", "warehouse": "wh"}, logger
        )

    def test_check_auth_authenticated(self):
        version_cp = _make_completed_process(0, "snowsql 1.2")
        query_cp = _make_completed_process(0, "MYUSER")

        with patch("subprocess.run", side_effect=[version_cp, query_cp]):
            result = _run_coro(self._provider().check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_no_account_user(self):
        provider = SnowflakeAuthProvider({}, logger)
        version_cp = _make_completed_process(0, "snowsql 1.2")

        with patch("subprocess.run", return_value=version_cp):
            result = _run_coro(provider.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_always_returns_true(self):
        result = _run_coro(self._provider().logout())
        assert result is True

    def test_login_not_installed_returns_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no snowsql")):
            result = _run_coro(self._provider().login())
        assert result.status == AuthStatus.ERROR


# ---------------------------------------------------------------------------
# DatabricksAuthProvider
# ---------------------------------------------------------------------------


class TestDatabricksAuthProvider:
    def _provider(self):
        return DatabricksAuthProvider({"host": "https://my.azuredatabricks.net"}, logger)

    def test_check_auth_authenticated(self):
        workspace_cp = _make_completed_process(0, "/Shared\n/Users")

        p = self._provider()
        with (
            patch("shutil.which", return_value="/usr/bin/databricks"),
            patch("os.path.exists", return_value=False),
            patch("subprocess.run", return_value=workspace_cp),
        ):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_check_auth_not_authenticated(self):
        """When no credentials or CLI available, returns NOT_AUTHENTICATED."""
        p = DatabricksAuthProvider({}, logger)
        with patch("shutil.which", return_value=None), patch("os.path.exists", return_value=False):
            result = _run_coro(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_removes_config_file(self):
        with patch("os.path.exists", return_value=True):
            with patch("os.remove") as mock_remove:
                result = _run_coro(self._provider().logout())
        assert result is True
        mock_remove.assert_called_once()


# ---------------------------------------------------------------------------
# AuthManager
# ---------------------------------------------------------------------------


class TestAuthManager:
    def test_list_providers_returns_unique_types(self):
        manager = _make_manager()
        providers = manager.list_providers()
        assert isinstance(providers, list)
        assert len(providers) >= 1

    def test_get_provider_known(self):
        manager = _make_manager()
        provider = manager.get_provider("aws")
        assert provider is not None
        assert isinstance(provider, AWSAuthProvider)

    def test_get_provider_unknown_returns_none(self):
        manager = _make_manager()
        provider = manager.get_provider("nonexistent")
        assert provider is None

    def test_login_unknown_provider_returns_error_result(self):
        manager = _make_manager()
        result = _run_coro(manager.login("nonexistent_provider"))
        assert result.status == AuthStatus.ERROR

    def test_logout_unknown_provider_returns_false(self):
        manager = _make_manager()
        result = _run_coro(manager.logout("nonexistent_provider"))
        assert result is False

    def test_check_auth_unknown_provider_returns_error(self):
        manager = _make_manager()
        result = _run_coro(manager.check_auth("nonexistent_provider"))
        assert result.status == AuthStatus.ERROR

    def test_gcp_alias_same_class(self):
        manager = _make_manager()
        gcp = manager.get_provider("gcp")
        google_cloud = manager.get_provider("google_cloud")
        assert type(gcp) is type(google_cloud)

    def test_amazon_alias_same_class(self):
        manager = _make_manager()
        amazon = manager.get_provider("amazon")
        aws = manager.get_provider("aws")
        assert type(amazon) is type(aws)


# ---------------------------------------------------------------------------
# handle_login
# ---------------------------------------------------------------------------


class TestHandleLogin:
    def _manager(self):
        return _make_manager()

    def test_handle_login_authenticated_returns_0(self):
        m = self._manager()
        ok = AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)

        async def _fake_login(*a, **kw):
            return ok

        with patch.object(m, "login", side_effect=_fake_login):
            with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
                result = _run_coro(handle_login("aws", m, logger))
        assert result == 0

    def test_handle_login_failure_returns_1(self):
        m = self._manager()
        fail = AuthResult(
            provider="aws",
            status=AuthStatus.NOT_AUTHENTICATED,
            error_message="no creds",
        )

        async def _fake_login(*a, **kw):
            return fail

        with patch.object(m, "login", side_effect=_fake_login):
            with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
                result = _run_coro(handle_login("aws", m, logger))
        assert result == 1

    def test_handle_login_exception_returns_1(self):
        m = self._manager()

        async def _fake_login(*a, **kw):
            raise Exception("network error")

        with patch.object(m, "login", side_effect=_fake_login):
            result = _run_coro(handle_login("aws", m, logger))
        assert result == 1


# ---------------------------------------------------------------------------
# handle_logout
# ---------------------------------------------------------------------------


class TestHandleLogout:
    def _manager(self):
        return _make_manager()

    def test_handle_logout_success_returns_0(self):
        m = self._manager()

        async def _fake_logout(*a, **kw):
            return True

        with patch.object(m, "logout", side_effect=_fake_logout):
            result = _run_coro(handle_logout("aws", m, logger))
        assert result == 0

    def test_handle_logout_failure_returns_1(self):
        m = self._manager()

        async def _fake_logout(*a, **kw):
            return False

        with patch.object(m, "logout", side_effect=_fake_logout):
            result = _run_coro(handle_logout("aws", m, logger))
        assert result == 1

    def test_handle_logout_exception_returns_1(self):
        m = self._manager()

        async def _fake_logout(*a, **kw):
            raise Exception("failed")

        with patch.object(m, "logout", side_effect=_fake_logout):
            result = _run_coro(handle_logout("aws", m, logger))
        assert result == 1


# ---------------------------------------------------------------------------
# handle_status
# ---------------------------------------------------------------------------


class TestHandleStatus:
    def _manager(self):
        return _make_manager()

    def test_handle_status_specific_provider_authenticated(self):
        m = self._manager()
        result_obj = AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)

        async def _fake_check(*a, **kw):
            return result_obj

        with patch.object(m, "check_auth", side_effect=_fake_check):
            with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
                code = _run_coro(handle_status("aws", m, logger))
        assert code == 0

    def test_handle_status_specific_provider_not_authenticated(self):
        m = self._manager()
        result_obj = AuthResult(
            provider="aws",
            status=AuthStatus.NOT_AUTHENTICATED,
            error_message="no creds",
        )

        async def _fake_check(*a, **kw):
            return result_obj

        with patch.object(m, "check_auth", side_effect=_fake_check):
            with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
                code = _run_coro(handle_status("aws", m, logger))
        assert code == 1

    def test_handle_status_all_providers(self):
        m = self._manager()
        result_obj = AuthResult(provider="aws", status=AuthStatus.AUTHENTICATED)

        async def _fake_check(*a, **kw):
            return result_obj

        with patch.object(m, "check_auth", side_effect=_fake_check):
            with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
                code = _run_coro(handle_status(None, m, logger))
        assert isinstance(code, int)


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRunFunction:
    def test_run_list_verb_returns_0(self):
        args = _make_args(verb="list")
        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = run(args, logger)
        assert result == 0

    def test_run_login_no_provider_returns_1(self):
        args = _make_args(verb="login", provider=None)
        result = run(args, logger)
        assert result == 1

    def test_run_logout_no_provider_returns_1(self):
        args = _make_args(verb="logout", provider=None)
        result = run(args, logger)
        assert result == 1

    def test_run_keyboard_interrupt_returns_130(self):
        args = _make_args(verb="list")
        with patch("fluid_build.cli.auth.AuthManager", side_effect=KeyboardInterrupt):
            result = run(args, logger)
        assert result == 130

    def test_run_unknown_verb_returns_0(self):
        args = _make_args(verb="unknown_verb")
        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            result = run(args, logger)
        assert result == 0
