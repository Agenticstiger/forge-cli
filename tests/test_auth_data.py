# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build.cli.auth — AuthStatus, AuthResult, CLIError dataclasses."""

from fluid_build.cli.auth import AuthResult, AuthStatus
from fluid_build.cli.auth import CLIError as AuthCLIError


class TestAuthStatus:
    def test_values(self):
        assert AuthStatus.AUTHENTICATED.value == "authenticated"
        assert AuthStatus.NOT_AUTHENTICATED.value == "not_authenticated"
        assert AuthStatus.EXPIRED.value == "expired"
        assert AuthStatus.ERROR.value == "error"
        assert AuthStatus.UNKNOWN.value == "unknown"


class TestAuthResult:
    def test_defaults(self):
        r = AuthResult(provider="gcp", status=AuthStatus.AUTHENTICATED)
        assert r.provider == "gcp"
        assert r.status == AuthStatus.AUTHENTICATED
        assert r.user_info == {}
        assert r.credentials_path is None
        assert r.scopes == []
        assert r.error_message is None

    def test_custom_fields(self):
        r = AuthResult(
            provider="aws",
            status=AuthStatus.EXPIRED,
            user_info={"account": "123"},
            expires_at="2025-01-01",
            scopes=["read", "write"],
            error_message="Token expired",
        )
        assert r.provider == "aws"
        assert r.status == AuthStatus.EXPIRED
        assert r.expires_at == "2025-01-01"
        assert len(r.scopes) == 2


class TestAuthCLIError:
    # CODE_REVIEW C-008: ``cli/auth.py`` re-exports the canonical
    # ``CLIError`` from ``fluid_build.cli._common``. Attribute names
    # migrated: ``.code`` → ``.exit_code``, ``.details`` → ``.context``.
    # ``.message`` kept as a compat alias for ``.event``.

    def test_basic(self):
        e = AuthCLIError(1, "command_not_found", {"command": "gcloud"})
        assert e.exit_code == 1
        assert e.message == "command_not_found"
        assert e.context["command"] == "gcloud"

    def test_default_context(self):
        e = AuthCLIError(2, "some_error")
        assert e.context == {}
