# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Snowflake workload identity federation (keyless CI) from the environment.

The variable names are the ones the snowflakedb/snowflake OpenTofu provider
reads (``SNOWFLAKE_AUTHENTICATOR``, ``SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER``,
``SNOWFLAKE_TOKEN_FILE_PATH``), so one set of variables drives forge's own
connection and ``tofu``. The connection parameters are snowflake-connector-python's
(``authenticator``, ``workload_identity_provider``, ``token_file_path``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import fluid_build.providers.snowflake.connection as connection_mod
import fluid_build.providers.snowflake.util.config as config_mod
from fluid_build.providers.snowflake.connection import SnowflakeConnection
from fluid_build.providers.snowflake.util.config import get_connection_params

pytestmark = pytest.mark.unit

_VARS = (
    "ACCOUNT",
    "USER",
    "ROLE",
    "WAREHOUSE",
    "DATABASE",
    "SCHEMA",
    "AUTHENTICATOR",
    "PASSWORD",
    "PRIVATE_KEY_PATH",
    "OAUTH_TOKEN",
    "WORKLOAD_IDENTITY_PROVIDER",
    "TOKEN_FILE_PATH",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VARS:
        monkeypatch.delenv(f"SNOWFLAKE_{name}", raising=False)
        monkeypatch.delenv(f"SF_{name}", raising=False)
    # Nothing outside the environment (keyring, operator store) may answer.
    monkeypatch.setattr(config_mod, "_resolve_with_adapter", lambda *a, **k: (None, None))


def _wif(monkeypatch: pytest.MonkeyPatch, provider: str = "OIDC", **extra: str) -> None:
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-myaccount")
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "WORKLOAD_IDENTITY")
    monkeypatch.setenv("SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER", provider)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_oidc_needs_no_user_and_passes_the_token_file(monkeypatch, tmp_path):
    token = str(tmp_path / "id_token")
    _wif(monkeypatch, SNOWFLAKE_TOKEN_FILE_PATH=token)

    params = get_connection_params()

    assert params["authenticator"] == "WORKLOAD_IDENTITY"
    assert params["workload_identity_provider"] == "OIDC"
    assert params["token_file_path"] == token
    assert params["account"] == "myorg-myaccount"
    assert params["user"] == ""
    for secret in ("password", "private_key_path", "oauth_token", "token"):
        assert secret not in params


def test_the_authenticator_name_is_case_insensitive(monkeypatch, tmp_path):
    _wif(monkeypatch, provider="oidc", SNOWFLAKE_TOKEN_FILE_PATH=str(tmp_path / "t"))
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "workload_identity")

    params = get_connection_params()

    assert params["authenticator"] == "WORKLOAD_IDENTITY"
    assert params["workload_identity_provider"] == "OIDC"


def test_wif_outranks_a_stray_password_and_key(monkeypatch, tmp_path):
    _wif(
        monkeypatch,
        SNOWFLAKE_TOKEN_FILE_PATH=str(tmp_path / "t"),
        SNOWFLAKE_PASSWORD="not-used",  # pragma: allowlist secret
        SNOWFLAKE_PRIVATE_KEY_PATH=str(tmp_path / "k.p8"),
    )

    params = get_connection_params()

    assert "password" not in params and "private_key_path" not in params


def test_a_cloud_provider_needs_no_token_file(monkeypatch):
    _wif(monkeypatch, provider="aws")

    params = get_connection_params()

    assert params["workload_identity_provider"] == "AWS"
    assert "token_file_path" not in params


def test_oidc_without_a_token_file_is_refused_with_the_variable(monkeypatch):
    _wif(monkeypatch)
    with pytest.raises(ValueError, match="SNOWFLAKE_TOKEN_FILE_PATH"):
        get_connection_params()


def test_wif_without_a_provider_is_refused_with_the_variable(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-myaccount")
    monkeypatch.setenv("SNOWFLAKE_AUTHENTICATOR", "WORKLOAD_IDENTITY")
    with pytest.raises(ValueError, match="SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER"):
        get_connection_params()


def test_other_authenticators_still_need_a_user(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "myorg-myaccount")
    monkeypatch.setenv("SNOWFLAKE_PASSWORD", "not-used")  # pragma: allowlist secret
    with pytest.raises(ValueError, match="user"):
        get_connection_params()


def test_explicit_settings_outrank_the_environment(monkeypatch, tmp_path):
    _wif(monkeypatch, SNOWFLAKE_TOKEN_FILE_PATH=str(tmp_path / "env"))
    explicit = str(tmp_path / "explicit")

    settings = config_mod.resolve_snowflake_settings(token_file_path=explicit)

    assert settings["token_file_path"] == explicit


def test_the_connection_hands_the_connector_the_wif_parameters(monkeypatch, tmp_path):
    seen: dict = {}
    conn = SimpleNamespace(cursor=lambda: None, close=lambda: None)
    monkeypatch.setattr(connection_mod, "SNOWFLAKE_AVAILABLE", True)
    monkeypatch.setattr(
        connection_mod,
        "snowflake",
        SimpleNamespace(connector=SimpleNamespace(connect=lambda **kw: seen.update(kw) or conn)),
    )
    monkeypatch.setattr(SnowflakeConnection, "_initialize_session", lambda *a, **k: None)
    _wif(monkeypatch, SNOWFLAKE_TOKEN_FILE_PATH=str(tmp_path / "id_token"))

    SnowflakeConnection(**get_connection_params())._connect()

    assert seen["authenticator"] == "WORKLOAD_IDENTITY"
    assert seen["workload_identity_provider"] == "OIDC"
    assert seen["token_file_path"] == str(tmp_path / "id_token")
    assert "user" not in seen and "password" not in seen and "token" not in seen


def test_the_tofu_plugin_lists_the_wif_variables():
    from fluid_build.iac.providers.snowflake import SnowflakeIacPlugin

    names = SnowflakeIacPlugin.credential_env_vars
    assert "SNOWFLAKE_WORKLOAD_IDENTITY_PROVIDER" in names
    assert "SNOWFLAKE_TOKEN_FILE_PATH" in names


class TestReadinessWithoutAUser:
    """A workload identity login names no user: the readiness checks agree."""

    def test_auth_report_does_not_ask_for_a_user(self):
        from fluid_build.providers.snowflake.util.auth import _required_actions

        config = {"account": "org-acct", "authenticator": "WORKLOAD_IDENTITY"}
        actions = _required_actions(config)
        assert actions["auth_ready_missing"] == []
        assert "user" not in actions["provider_ready_missing"]
        assert "user" in _required_actions({"account": "a", "password": "p"})["auth_ready_missing"]

    @pytest.mark.parametrize("method", ["login", "check_auth"])
    def test_auth_login_and_status_do_not_ask_for_a_user(self, monkeypatch, method):
        import asyncio
        import logging

        from fluid_build.cli._auth_provider_impls import SnowflakeAuthProvider as Provider
        from fluid_build.cli.auth import AuthResult, AuthStatus

        connected = AuthResult(provider="snowflake", status=AuthStatus.AUTHENTICATED)
        monkeypatch.setattr(Provider, "_check_auth_with_connector", lambda self, s: connected)
        provider = Provider({}, logging.getLogger("test"))

        def settings(**values):
            monkeypatch.setattr(Provider, "_resolve_settings", lambda self: dict(values))
            return asyncio.run(getattr(provider, method)())

        assert settings(account="org-acct", authenticator="WORKLOAD_IDENTITY") is connected
        refused = settings(authenticator="WORKLOAD_IDENTITY")
        assert refused.status == AuthStatus.NOT_AUTHENTICATED
        assert "account" in refused.error_message and "user" not in refused.error_message
        assert "user" in settings(account="org-acct", password="p").error_message
