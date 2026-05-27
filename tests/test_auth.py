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

"""Tests for fluid_build.cli.auth."""

import argparse
import asyncio
import logging
from unittest.mock import MagicMock, patch

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
    register,
    run,
)

LOG = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────


def _make_auth_result(status=AuthStatus.AUTHENTICATED, provider="test", **kwargs):
    return AuthResult(provider=provider, status=status, **kwargs)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── CLIError ──────────────────────────────────────────────────────────


class TestCLIError:
    # CODE_REVIEW C-008: ``cli/auth.py`` no longer defines its own
    # ``CLIError`` — it imports the canonical one from
    # ``fluid_build.cli._common``. These tests cover the shared class.

    def test_attributes(self):
        err = CLIError(2, "something_went_wrong", {"detail": "x"})
        assert err.exit_code == 2
        assert err.event == "something_went_wrong"
        assert err.message == "something_went_wrong"  # compat alias = event
        assert err.context == {"detail": "x"}

    def test_default_context_is_empty_dict(self):
        err = CLIError(1, "oops")
        assert err.context == {}

    def test_is_exception(self):
        with pytest.raises(CLIError):
            raise CLIError(1, "fail")


# ── AuthStatus / AuthResult ───────────────────────────────────────────


class TestAuthResult:
    def test_defaults(self):
        r = AuthResult(provider="gcp", status=AuthStatus.UNKNOWN)
        assert r.user_info == {}
        assert r.credentials_path is None
        assert r.expires_at is None
        assert r.scopes == []
        assert r.error_message is None

    def test_status_enum_values(self):
        assert AuthStatus.AUTHENTICATED.value == "authenticated"
        assert AuthStatus.NOT_AUTHENTICATED.value == "not_authenticated"
        assert AuthStatus.EXPIRED.value == "expired"
        assert AuthStatus.ERROR.value == "error"
        assert AuthStatus.UNKNOWN.value == "unknown"


# ── GoogleCloudAuthProvider ───────────────────────────────────────────


class TestGoogleCloudAuthProvider:
    def _make(self, config=None):
        return GoogleCloudAuthProvider(config or {}, LOG)

    def test_default_name(self):
        p = self._make()
        assert p.name == "google_cloud"

    def test_project_id_from_config(self):
        p = self._make({"project_id": "my-project"})
        assert p.project_id == "my-project"

    def test_default_scopes_populated(self):
        p = self._make()
        assert len(p.scopes) > 0
        assert any("cloud-platform" in s for s in p.scopes)

    def test_check_auth_gcloud_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        p = self._make()
        with (
            patch.object(p, "_run_command", side_effect=CLIError(1, "cmd_not_found")),
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_check_auth_authenticated(self):
        p = self._make({"project_id": "proj"})
        mock_cp = MagicMock()
        mock_cp.returncode = 0
        mock_cp.stdout = "user@example.com\n"

        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/gcloud"),
            patch.object(p, "_run_command", return_value=mock_cp),
        ):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED

    def test_logout_removes_env_var(self):
        p = self._make()
        with patch.object(p, "_run_command", return_value=MagicMock(returncode=0)):
            with patch.dict("os.environ", {"GOOGLE_APPLICATION_CREDENTIALS": "/tmp/creds.json"}):
                result = _run_async(p.logout())
        assert result is True

    def test_login_non_interactive_no_creds_returns_not_authenticated(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        p = self._make()
        with (
            patch.object(p, "_is_interactive", return_value=False),
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_async(p.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ── AWSAuthProvider ───────────────────────────────────────────────────


class TestAWSAuthProvider:
    def _make(self, config=None):
        return AWSAuthProvider(config or {}, LOG)

    def test_default_region_and_profile(self):
        p = self._make()
        assert p.region == "us-east-1"
        assert p.profile == "default"

    def test_config_overrides(self):
        p = self._make({"region": "eu-west-1", "profile": "prod"})
        assert p.region == "eu-west-1"
        assert p.profile == "prod"

    def test_check_auth_cli_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        p = self._make()
        with (
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_check_auth_authenticated(self):
        import json

        p = self._make()
        identity = {
            "UserId": "AIDATEST",
            "Account": "123456789",
            "Arn": "arn:aws:iam::123:user/x",
        }
        mock_identity = MagicMock(returncode=0, stdout=json.dumps(identity))

        with (
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value="/usr/bin/aws"),
            patch.object(p, "_run_command", return_value=mock_identity),
        ):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED
        assert result.user_info["account"] == "123456789"

    def test_logout_success(self):
        p = self._make()
        with patch.object(p, "_run_command", return_value=MagicMock(returncode=0)):
            result = _run_async(p.logout())
        assert result is True

    def test_login_exception_returns_not_authenticated(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        p = self._make()
        with (
            patch.object(p, "_is_interactive", return_value=False),
            patch.object(p, "_has_boto3", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_async(p.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ── AzureAuthProvider ─────────────────────────────────────────────────


class TestAzureAuthProvider:
    def _make(self, config=None):
        return AzureAuthProvider(config or {}, LOG)

    def test_default_name(self):
        p = self._make()
        assert p.name == "azure"

    def test_tenant_from_config(self):
        p = self._make({"tenant_id": "t-123", "subscription_id": "s-456"})
        assert p.tenant_id == "t-123"
        assert p.subscription_id == "s-456"

    def test_check_auth_cli_not_installed(self):
        """When neither SDK nor CLI is available, check_auth returns NOT_AUTHENTICATED."""
        p = self._make()
        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value=None),
        ):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_check_auth_authenticated(self):
        import json

        p = self._make()
        account_data = {
            "name": "My Sub",
            "id": "sub-id",
            "tenantId": "t-id",
            "user": {"name": "user@example.com", "type": "user"},
        }
        mock_acct = MagicMock(returncode=0, stdout=json.dumps(account_data))

        with (
            patch.object(p, "_has_sdk", return_value=False),
            patch("shutil.which", return_value="/usr/bin/az"),
            patch.object(p, "_run_command", return_value=mock_acct),
        ):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.AUTHENTICATED
        assert result.user_info["user"] == "user@example.com"

    def test_logout_calls_az_logout(self):
        p = self._make()
        called = []

        def _side(cmd, **kw):
            called.append(cmd)
            return MagicMock(returncode=0)

        with (
            patch("shutil.which", return_value="/usr/bin/az"),
            patch.object(p, "_run_command", side_effect=_side),
        ):
            result = _run_async(p.logout())
        assert result is True
        assert any("logout" in str(c) for c in called)


# ── SnowflakeAuthProvider ─────────────────────────────────────────────


class TestSnowflakeAuthProvider:
    def _make(self, config=None):
        return SnowflakeAuthProvider(config or {}, LOG)

    def test_default_name(self):
        p = self._make()
        assert p.name == "snowflake"

    def test_config_fields(self):
        p = self._make(
            {
                "account": "myacct",
                "user": "myuser",
                "warehouse": "WH",
                "role": "SYSADMIN",
            }
        )
        assert p.account == "myacct"
        assert p.user == "myuser"
        assert p.role == "SYSADMIN"

    def test_logout_always_true(self):
        p = self._make()
        result = _run_async(p.logout())
        assert result is True

    def test_check_auth_no_account_returns_not_authenticated(self):
        p = self._make()
        mock_ver = MagicMock(returncode=0, stdout="")

        with patch.object(p, "_run_command", return_value=mock_ver):
            result = _run_async(p.check_auth())
        assert result.status == AuthStatus.NOT_AUTHENTICATED


# ── DatabricksAuthProvider ────────────────────────────────────────────


class TestDatabricksAuthProvider:
    def _make(self, config=None):
        return DatabricksAuthProvider(config or {}, LOG)

    def test_default_name(self):
        p = self._make()
        assert p.name == "databricks"

    def test_config_fields(self):
        p = self._make({"host": "https://my.databricks.com", "cluster_id": "cl-123"})
        assert p.host == "https://my.databricks.com"
        assert p.cluster_id == "cl-123"

    def test_login_cli_not_installed_returns_not_authenticated(self):
        """In non-interactive mode with no credentials, login returns NOT_AUTHENTICATED."""
        p = self._make()
        with (
            patch.object(p, "_is_interactive", return_value=False),
            patch("shutil.which", return_value=None),
            patch("os.path.exists", return_value=False),
        ):
            result = _run_async(p.login())
        assert result.status == AuthStatus.NOT_AUTHENTICATED

    def test_logout_returns_true_when_no_config_file(self):
        p = self._make()
        with patch("fluid_build.cli.auth.os.path.exists", return_value=False):
            result = _run_async(p.logout())
        assert result is True


# ── AuthManager ───────────────────────────────────────────────────────


class TestAuthManager:
    def _make(self, config=None):
        return AuthManager(config or {}, LOG)

    def test_providers_initialized(self):
        m = self._make()
        assert "google_cloud" in m.providers
        assert "aws" in m.providers
        assert "azure" in m.providers

    def test_get_provider_case_insensitive(self):
        m = self._make()
        p = m.get_provider("AWS")
        assert p is not None
        assert p.name == "aws"

    def test_get_provider_unknown_returns_none(self):
        m = self._make()
        assert m.get_provider("oracle_cloud") is None

    def test_list_providers_unique(self):
        m = self._make()
        providers = m.list_providers()
        assert len(providers) == len(set(providers))

    def test_login_unknown_provider_returns_error(self):
        m = self._make()
        result = _run_async(m.login("oracle_cloud"))
        assert result.status == AuthStatus.ERROR
        assert "not supported" in result.error_message.lower()

    def test_logout_unknown_provider_returns_false(self):
        m = self._make()
        result = _run_async(m.logout("oracle_cloud"))
        assert result is False

    def test_check_auth_unknown_provider_returns_error(self):
        m = self._make()
        result = _run_async(m.check_auth("oracle_cloud"))
        assert result.status == AuthStatus.ERROR

    def test_alias_gcp_maps_to_google_cloud(self):
        m = self._make()
        p = m.get_provider("gcp")
        assert p is not None
        assert p.name == "google_cloud"

    def test_alias_amazon_maps_to_aws(self):
        m = self._make()
        p = m.get_provider("amazon")
        assert p is not None
        assert p.name == "aws"


# ── run() dispatcher ──────────────────────────────────────────────────


class TestRun:
    def _make_args(self, verb, provider=None):
        args = argparse.Namespace(verb=verb, provider=provider)
        return args

    def test_run_list(self):
        args = self._make_args("list")
        rc = run(args, LOG)
        assert rc == 0

    def test_run_login_no_provider_returns_1(self):
        args = self._make_args("login", provider=None)
        rc = run(args, LOG)
        assert rc == 1

    def test_run_logout_no_provider_returns_1(self):
        args = self._make_args("logout", provider=None)
        rc = run(args, LOG)
        assert rc == 1

    def test_run_login_with_provider_calls_handle_login(self):
        args = self._make_args("login", provider="aws")
        with patch("fluid_build.cli.auth.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = 0
            rc = run(args, LOG)
        assert rc == 0

    def test_run_status_calls_handle_status(self):
        args = self._make_args("status", provider=None)
        with patch("fluid_build.cli.auth.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = 0
            rc = run(args, LOG)
        assert rc == 0

    def test_run_logout_with_provider_calls_handle_logout(self):
        args = self._make_args("logout", provider="aws")
        with patch("fluid_build.cli.auth.asyncio") as mock_asyncio:
            mock_asyncio.run.return_value = 0
            rc = run(args, LOG)
        assert rc == 0

    def test_run_keyboard_interrupt_returns_130(self):
        args = self._make_args("login", provider="aws")
        with patch("fluid_build.cli.auth.asyncio") as mock_asyncio:
            mock_asyncio.run.side_effect = KeyboardInterrupt
            rc = run(args, LOG)
        assert rc == 130

    def test_run_unknown_verb_returns_0(self):
        args = self._make_args("other_verb")
        rc = run(args, LOG)
        assert rc == 0


# ── handle_login / handle_logout / handle_status ──────────────────────


class TestHandleLogin:
    def test_authenticated_returns_0(self):
        result = _make_auth_result(AuthStatus.AUTHENTICATED, provider="aws")
        m = MagicMock()

        async def _fake_login(p, **kw):
            return result

        m.login = _fake_login

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_login("aws", m, LOG))
        assert rc == 0

    def test_not_authenticated_returns_1(self):
        result = _make_auth_result(
            AuthStatus.NOT_AUTHENTICATED, provider="aws", error_message="no creds"
        )
        m = MagicMock()

        async def _fake_login(p, **kw):
            return result

        m.login = _fake_login

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_login("aws", m, LOG))
        assert rc == 1

    def test_error_status_returns_1(self):
        result = _make_auth_result(
            AuthStatus.ERROR, provider="aws", error_message="CLI not installed"
        )
        m = MagicMock()

        async def _fake_login(p, **kw):
            return result

        m.login = _fake_login

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_login("aws", m, LOG))
        assert rc == 1


class TestHandleLogout:
    def test_success_returns_0(self):
        m = MagicMock()

        async def _fake_logout(p):
            return True

        m.logout = _fake_logout
        rc = _run_async(handle_logout("aws", m, LOG))
        assert rc == 0

    def test_failure_returns_1(self):
        m = MagicMock()

        async def _fake_logout(p):
            return False

        m.logout = _fake_logout
        rc = _run_async(handle_logout("aws", m, LOG))
        assert rc == 1


class TestHandleStatus:
    def test_specific_provider_authenticated_returns_0(self):
        result = _make_auth_result(AuthStatus.AUTHENTICATED, provider="aws")
        m = MagicMock()

        async def _fake_check(p):
            return result

        m.check_auth = _fake_check

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_status("aws", m, LOG))
        assert rc == 0

    def test_specific_provider_not_authenticated_returns_1(self):
        result = _make_auth_result(AuthStatus.NOT_AUTHENTICATED, provider="aws")
        m = MagicMock()

        async def _fake_check(p):
            return result

        m.check_auth = _fake_check

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_status("aws", m, LOG))
        assert rc == 1

    def test_all_providers_check_authenticated(self):
        result = _make_auth_result(AuthStatus.AUTHENTICATED, provider="aws")
        m = MagicMock()

        async def _fake_check(p):
            return result

        m.list_providers.return_value = ["aws"]
        m.check_auth = _fake_check

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_status(None, m, LOG))
        assert rc == 0

    def test_all_providers_check_not_all_authenticated(self):
        result = _make_auth_result(AuthStatus.NOT_AUTHENTICATED, provider="aws")
        m = MagicMock()

        async def _fake_check(p):
            return result

        m.list_providers.return_value = ["aws"]
        m.check_auth = _fake_check

        with patch("fluid_build.cli.auth.RICH_AVAILABLE", False):
            rc = _run_async(handle_status(None, m, LOG))
        assert rc == 1


# ── register() ────────────────────────────────────────────────────────


class TestRegister:
    def test_register_adds_auth_subparser(self):
        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")
        register(sp)
        args = p.parse_args(["auth", "list"])
        assert args.cmd == "auth"
        assert args.verb == "list"

    def test_register_login_subcommand(self):
        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")
        register(sp)
        args = p.parse_args(["auth", "login"])
        assert args.verb == "login"

    def test_register_status_subcommand(self):
        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")
        register(sp)
        args = p.parse_args(["auth", "status"])
        assert args.verb == "status"

    def test_register_logout_subcommand(self):
        p = argparse.ArgumentParser()
        sp = p.add_subparsers(dest="cmd")
        register(sp)
        args = p.parse_args(["auth", "logout"])
        assert args.verb == "logout"


# ── S-011: _sanitize_argv redacts credential-bearing flag values ────


class TestSanitizeArgv:
    """SECURITY_REVIEW S-011: AuthProvider._run_command logs full argv at
    DEBUG. Credential-bearing flag values (e.g. ``--password hunter2``,
    ``--key-file /path/to/key.json``, ``--client-secret=xxx``) must be
    redacted before the string reaches the formatter."""

    def _redact(self, argv):
        from fluid_build.cli.auth import _sanitize_argv

        return _sanitize_argv(argv)

    def test_space_separated_password_value_redacted(self):
        out = self._redact(["gcloud", "auth", "activate", "--password", "hunter2"])
        assert "hunter2" not in out
        assert "***REDACTED***" in out
        assert out[:-1] == ["gcloud", "auth", "activate", "--password"]

    def test_equals_separated_password_redacted(self):
        out = self._redact(["gcloud", "--password=hunter2"])
        assert "hunter2" not in " ".join(out)
        assert "--password=***REDACTED***" in out

    def test_key_file_argument_redacted(self):
        out = self._redact(
            ["gcloud", "auth", "activate-service-account", "--key-file", "/tmp/k.json"]
        )
        assert "/tmp/k.json" not in out
        assert "***REDACTED***" in out

    def test_provider_specific_secret_flag_matches_suffix(self):
        """--client-secret isn't in the exact-match set but must match
        via the ``-secret`` suffix rule."""
        out = self._redact(["gh", "auth", "login", "--client-secret", "shh"])
        assert "shh" not in out
        assert "***REDACTED***" in out

    def test_non_sensitive_flag_unchanged(self):
        out = self._redact(["gcloud", "--project", "my-project", "--region", "us"])
        assert out == ["gcloud", "--project", "my-project", "--region", "us"]

    def test_mixed_sensitive_and_non_sensitive(self):
        out = self._redact(
            [
                "gcloud",
                "auth",
                "activate-service-account",
                "--project",
                "p1",
                "--key-file",
                "/path/to/key.json",
                "--verbose",
            ]
        )
        assert "p1" in out
        assert "--verbose" in out
        assert "/path/to/key.json" not in out
        assert out[out.index("--key-file") + 1] == "***REDACTED***"

    def test_empty_argv(self):
        assert self._redact([]) == []

    def test_short_password_flag_redacts_next(self):
        """The short ``-p`` flag is in the exact-match set; next arg redacted."""
        out = self._redact(["mysql", "-p", "mypassword"])
        assert "mypassword" not in out
        assert "***REDACTED***" in out


# ── H17: Snowflake CLIError + dedup credential log ────────────────────


class TestSnowflakeCLIErrorIsImported:
    """H17 — when SnowSQL is not installed and no connector auth is
    configured, ``check_auth`` used to fall through to ``_run_command``
    which raises ``CLIError`` — but ``CLIError`` was not imported in the
    extracted ``_auth_provider_impls.py`` module, so the
    ``except CLIError:`` clauses crashed with a bare Python NameError
    that surfaced to the user as
    ``Snowflake | Error | name 'CLIError' is not defined``.

    These tests pin the import in place and assert the error message
    is human-readable rather than a Python traceback.
    """

    def _make(self, config=None):
        return SnowflakeAuthProvider(config or {}, LOG)

    def test_cli_error_importable_in_provider_impls(self):
        """Smoke: ``_auth_provider_impls`` carries the ``CLIError`` symbol."""
        from fluid_build.cli import _auth_provider_impls

        assert hasattr(_auth_provider_impls, "CLIError"), (
            "CLIError must be importable inside _auth_provider_impls "
            "for the snowflake check_auth/login fallback paths"
        )
        # Same class as the canonical CLIError in cli._common.
        from fluid_build.cli._common import CLIError as CanonicalCLIError

        assert _auth_provider_impls.CLIError is CanonicalCLIError

    def test_snowsql_missing_returns_human_readable_error_not_nameerror(self):
        """When SnowSQL is missing AND no connector auth is configured,
        the result is a typed error string, not a Python NameError.

        Without the import fix, this test crashed with
        ``NameError: name 'CLIError' is not defined``.
        """
        from fluid_build.cli._common import CLIError as CanonicalCLIError

        p = self._make({"account": "myacct", "user": "myuser"})

        def _raise_cli_error(*args, **kwargs):
            raise CanonicalCLIError(1, "command_not_found", {"command": "snowsql"})

        with patch.object(p, "_run_command", side_effect=_raise_cli_error):
            result = _run_async(p.check_auth())

        assert result.status == AuthStatus.ERROR
        assert "SnowSQL" in (result.error_message or "")
        # Not a python traceback fragment.
        assert "NameError" not in (result.error_message or "")

    def test_snowsql_login_missing_cli_returns_typed_error(self):
        """Same path as above but on the login flow (``_login_with_snowsql``)."""
        from fluid_build.cli._common import CLIError as CanonicalCLIError

        p = self._make({"account": "myacct", "user": "myuser"})

        def _raise_cli_error(*args, **kwargs):
            raise CanonicalCLIError(1, "command_not_found", {"command": "snowsql"})

        # ``_login_with_snowsql`` is a sync helper — call directly. The
        # ``except CLIError:`` block at the top of the function is what
        # this test pins; without the import fix it crashes with
        # ``NameError: name 'CLIError' is not defined``.
        with patch.object(p, "_run_command", side_effect=_raise_cli_error):
            result = p._login_with_snowsql()

        assert result.status == AuthStatus.ERROR
        assert "SnowSQL" in (result.error_message or "")
        assert "NameError" not in (result.error_message or "")


class TestDotenvCredentialLogDedup:
    """H17 — the ``Loaded N credentials from .env files`` info message
    fires once per ``DotEnvCredentialStore.load()`` call. ``fluid auth
    status`` would previously echo the same banner 7× because
    connector / credential-store / catalog-resolver each instantiate
    their own store. After the fix the banner is gated by a process-wide
    flag so only the first loader logs at INFO."""

    def _reset(self):
        from fluid_build.credentials import dotenv_store as ds

        ds.reset_dotenv_load_log()

    def test_loaded_credentials_logged_once(self, caplog, tmp_path, monkeypatch):
        from fluid_build.credentials.dotenv_store import DotEnvCredentialStore

        self._reset()
        # Write a .env so the load actually has something to report on.
        (tmp_path / ".env").write_text("FOO=1\nBAR=2\n")

        # First store load: INFO line.
        store1 = DotEnvCredentialStore(project_root=tmp_path, environment="dev")
        # Second store, fresh instance (simulates connector / catalog
        # resolver each getting their own store).
        store2 = DotEnvCredentialStore(project_root=tmp_path, environment="dev")

        with caplog.at_level(logging.INFO, logger="fluid_build.credentials.dotenv_store"):
            store1.load()
            store2.load()

        info_lines = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno >= logging.INFO and "Loaded" in rec.getMessage()
        ]
        assert len(info_lines) == 1, (
            f"expected the 'Loaded N credentials' banner exactly once, "
            f"got {len(info_lines)}: {info_lines}"
        )

    def test_reset_dotenv_load_log_re_enables_first_loader(self, caplog, tmp_path, monkeypatch):
        from fluid_build.credentials.dotenv_store import DotEnvCredentialStore

        self._reset()
        (tmp_path / ".env").write_text("FOO=1\n")

        with caplog.at_level(logging.INFO, logger="fluid_build.credentials.dotenv_store"):
            DotEnvCredentialStore(project_root=tmp_path, environment="dev").load()
        # second call (without reset) drops to DEBUG.
        with caplog.at_level(logging.INFO, logger="fluid_build.credentials.dotenv_store"):
            DotEnvCredentialStore(project_root=tmp_path, environment="dev").load()
        # Now reset and call again → banner reappears.
        self._reset()
        with caplog.at_level(logging.INFO, logger="fluid_build.credentials.dotenv_store"):
            DotEnvCredentialStore(project_root=tmp_path, environment="dev").load()

        info_lines = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno >= logging.INFO and "Loaded" in rec.getMessage()
        ]
        # First-call INFO + post-reset INFO; middle call should be DEBUG only.
        assert len(info_lines) == 2
