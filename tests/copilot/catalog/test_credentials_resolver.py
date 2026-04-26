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

"""Coverage for the V1.5 :class:`CredentialResolver`.

The resolver is the foundation under every catalog adapter; if it
silently leaks credentials, picks up the wrong source, or fails open
on missing config, every adapter inherits the bug. The pins below
exercise every branch of the resolution chain (inline → keyring →
sources.yaml → env vars → metadata-service → fail-closed) and the
guarantees the plan promises:

1. **No credential leakage.** ``SecretStr`` redacts in ``repr`` /
   JSON / dict-dump.
2. **Resolution priority.** Inline beats credential_id beats
   keyring beats YAML beats env beats metadata-service.
3. **Fail-closed.** Missing credentials raise
   :class:`CredentialNotFoundError` with actionable suggestions.
4. **MCP defense.** ``allow_metadata_service=False`` (default)
   silently skips the cloud-metadata path even if env vars would
   have populated ADC.
5. **Source-type mismatch detection.** Looking up a Snowflake
   credential under a Unity catalog raises a clear error, not a
   silently-mis-typed credential.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from fluid_build.copilot.catalog.base import CatalogConfigError
from fluid_build.copilot.catalog.credentials import (
    PLAINTEXT_SOURCE_SECRETS_ENV,
    CredentialNotFoundError,
    CredentialResolver,
    SnowflakeCredentials,
    UnityCredentials,
)

# ----------------------------------------------------------------------
# Fixtures: stubbed keyring + sources.yaml fixtures
# ----------------------------------------------------------------------


class _FakeKeyring:
    """In-memory replacement for the OS keyring.

    Lets tests pre-populate ``fluid_source_<name>`` entries without
    touching the user's real keyring (and without any platform
    install dependency)."""

    def __init__(self, entries: Optional[Dict[str, str]] = None) -> None:
        self._entries: Dict[str, Dict[str, str]] = {}
        for compound_key, value in (entries or {}).items():
            service, account = compound_key.split("/", 1)
            self._entries.setdefault(service, {})[account] = value

    def get_password(self, service: str, account: str) -> Optional[str]:
        return self._entries.get(service, {}).get(account)


def _write_sources_yaml(path: Path, payload: Dict[str, Any]) -> Path:
    """Helper — write a sources.yaml fixture; tests then point the
    resolver at it via ``sources_config_path=path``."""
    import yaml

    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _scrub_catalog_env(monkeypatch):
    """Strip every catalog-related env var before each test so the
    env-var resolution path is deterministic. Tests that need a
    specific env var ``monkeypatch.setenv`` it themselves."""
    for prefix in ("SNOWFLAKE_", "DATABRICKS_", "GOOGLE_", "AWS_", "DATAHUB_", "DMM_"):
        for k in list(os.environ):
            if k.startswith(prefix):
                monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("FLUID_ALLOW_METADATA_SERVICE", raising=False)
    monkeypatch.delenv(PLAINTEXT_SOURCE_SECRETS_ENV, raising=False)


# ----------------------------------------------------------------------
# SecretStr leakage guard
# ----------------------------------------------------------------------


class TestSecretStrRedaction:
    def test_repr_redacts_password(self):
        creds = SnowflakeCredentials(
            account="acct",
            user="usr",
            auth_method="password",
            password="example-secret-password",
        )
        rendered = repr(creds)
        assert "example-secret-password" not in rendered
        assert "**********" in rendered

    def test_model_dump_redacts_password(self):
        creds = SnowflakeCredentials(
            account="acct",
            user="usr",
            auth_method="password",
            password="example-secret-password",
        )
        # Pydantic's ``model_dump`` returns a SecretStr instance, not
        # the raw string. Serialising to JSON via ``model_dump_json``
        # produces ``**********`` verbatim — the audit-trail layer
        # consumes JSON dumps so this is the path that matters.
        as_json = creds.model_dump_json()
        assert "example-secret-password" not in as_json
        assert "**********" in as_json

    def test_get_secret_value_returns_real_value(self):
        """Adapters need the raw value to construct the SDK request.
        Round-trip: SecretStr -> get_secret_value() -> raw string."""
        creds = SnowflakeCredentials(
            account="acct",
            user="usr",
            auth_method="password",
            password="example-secret-password",
        )
        assert creds.password.get_secret_value() == "example-secret-password"


# ----------------------------------------------------------------------
# Resolution priority — inline > keyring > yaml > env > metadata
# ----------------------------------------------------------------------


class TestResolutionPriority:
    def test_inline_credentials_win_over_everything(self, tmp_path, monkeypatch):
        """Inline must beat the keyring + yaml + env even when all
        three are configured and would otherwise resolve to a
        different account."""
        # Populate keyring + yaml + env with "wrong-account" creds.
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(
            sources_path,
            {
                "sources": {
                    "prod": {
                        "source_type": "snowflake",
                        "config": {
                            "account": "wrong-yaml-account",
                            "user": "wrong-yaml-user",
                            "auth_method": "password",
                        },
                    }
                }
            },
        )
        keyring = _FakeKeyring(
            {
                "fluid_source/prod": json.dumps({"password": "wrong-yaml-secret"}),
            }
        )
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "wrong-env-account")
        monkeypatch.setenv("SNOWFLAKE_USER", "wrong-env-user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "wrong-env-secret")

        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=keyring,
        )
        out = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
            credential_id="prod",  # would hit yaml + keyring
            inline_credentials={
                "account": "inline-wins",
                "user": "inline-user",
                "auth_method": "sso",
            },
        )
        assert out.account == "inline-wins"
        assert out.auth_method == "sso"

    def test_credential_id_beats_env_vars(self, tmp_path, monkeypatch):
        """When ``credential_id`` resolves, env-var fallback is NOT
        attempted — even if env vars would also have resolved."""
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(
            sources_path,
            {
                "sources": {
                    "prod": {
                        "source_type": "snowflake",
                        "config": {
                            "account": "yaml-account",
                            "user": "yaml-user",
                            "auth_method": "password",
                        },
                    }
                }
            },
        )
        keyring = _FakeKeyring(
            {
                "fluid_source/prod": json.dumps({"password": "yaml-secret"}),
            }
        )
        # Env vars present but should be ignored when credential_id wins.
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "env-account")
        monkeypatch.setenv("SNOWFLAKE_USER", "env-user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "env-secret")

        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=keyring,
        )
        out = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
            credential_id="prod",
        )
        assert out.account == "yaml-account"
        assert out.password.get_secret_value() == "yaml-secret"

    def test_yaml_and_keyring_merge_into_typed_credentials(self, tmp_path):
        """The non-sensitive YAML config + secret-bearing keyring entry
        merge into a single typed credential. This is the canonical
        production path."""
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(
            sources_path,
            {
                "sources": {
                    "prod": {
                        "source_type": "snowflake",
                        "config": {
                            "account": "abc-xyz",
                            "user": "ANALYST",
                            "auth_method": "private_key",
                            "private_key_path": "/etc/snowflake/key.p8",
                            "role": "ANALYST_RW",
                            "warehouse": "ANALYTICS_XS",
                        },
                    }
                }
            },
        )
        keyring = _FakeKeyring(
            {
                "fluid_source/prod": json.dumps({"private_key_passphrase": "p4ssphrase"}),
            }
        )
        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=keyring,
        )
        out = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
            credential_id="prod",
        )
        assert out.account == "abc-xyz"
        assert out.role == "ANALYST_RW"
        assert out.warehouse == "ANALYTICS_XS"
        assert out.private_key_path == Path("/etc/snowflake/key.p8")
        assert out.private_key_passphrase.get_secret_value() == "p4ssphrase"

    def test_env_var_path_for_snowflake_password(self, tmp_path, monkeypatch):
        """Env vars are the lowest-priority resolution source. Used
        by CI scripts that don't want a saved-source UX dance."""
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "env-acct")
        monkeypatch.setenv("SNOWFLAKE_USER", "env-user")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "env-pass")
        monkeypatch.setenv("SNOWFLAKE_ROLE", "ENV_ROLE")

        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        out = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
        )
        assert out.account == "env-acct"
        assert out.user == "env-user"
        assert out.auth_method == "password"
        assert out.role == "ENV_ROLE"

    def test_env_var_path_prefers_private_key_over_password(self, tmp_path, monkeypatch):
        """When BOTH SNOWFLAKE_PRIVATE_KEY_PATH and SNOWFLAKE_PASSWORD
        are set, the resolver picks private_key (more secure) — this
        matches the V1.5 plan's "federated identity preferred"
        recommendation."""
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct")
        monkeypatch.setenv("SNOWFLAKE_USER", "user")
        monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY_PATH", "/etc/key.p8")
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "ignored")
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        out = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
        )
        assert out.auth_method == "private_key"
        assert out.private_key_path == Path("/etc/key.p8")
        # Password field stays None — never silently populated when
        # private-key path won.
        assert out.password is None


# ----------------------------------------------------------------------
# Fail-closed
# ----------------------------------------------------------------------


class TestFailClosed:
    def test_no_credentials_anywhere_raises(self, tmp_path):
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        with pytest.raises(CredentialNotFoundError) as exc_info:
            resolver.resolve(
                catalog_name="snowflake",
                credential_type=SnowflakeCredentials,
            )
        # Suggestions name the wizard path AND the env-var path so
        # operators have a clear next action.
        assert exc_info.value.suggestions
        assert any("fluid ai setup" in s for s in exc_info.value.suggestions)

    def test_credential_id_present_but_not_in_storage(self, tmp_path):
        """Asking for ``credential_id='nonexistent'`` falls through to
        env vars (not present here either) and then fails closed —
        rather than silently constructing an empty credential."""
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(sources_path, {"sources": {}})
        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=_FakeKeyring(),
        )
        with pytest.raises(CredentialNotFoundError) as exc_info:
            resolver.resolve(
                catalog_name="snowflake",
                credential_type=SnowflakeCredentials,
                credential_id="nonexistent",
            )
        # Error message names the credential_id the user asked for.
        assert "nonexistent" in exc_info.value.message

    def test_env_vars_partially_populated_falls_through(self, tmp_path, monkeypatch):
        """SNOWFLAKE_ACCOUNT alone (no user, no auth material) must
        NOT produce a half-baked credential — it falls through to
        the next source and fails closed when nothing else resolves."""
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct-only")
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        with pytest.raises(CredentialNotFoundError):
            resolver.resolve(
                catalog_name="snowflake",
                credential_type=SnowflakeCredentials,
            )

    def test_yaml_plaintext_secrets_are_refused_without_opt_in(self, tmp_path):
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(
            sources_path,
            {
                "sources": {
                    "prod": {
                        "source_type": "snowflake",
                        "config": {
                            "account": "yaml-account",
                            "user": "yaml-user",
                            "auth_method": "password",
                        },
                        "secrets": {"password": "plain-secret"},
                    }
                }
            },
        )
        sources_path.chmod(0o600)
        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=_FakeKeyring(),
        )

        with pytest.raises(CatalogConfigError) as exc_info:
            resolver.resolve(
                catalog_name="snowflake",
                credential_type=SnowflakeCredentials,
                credential_id="prod",
            )

        assert "Refusing plaintext source secrets" in exc_info.value.message

    def test_yaml_plaintext_secrets_require_opt_in_and_private_file(self, tmp_path, monkeypatch):
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(
            sources_path,
            {
                "sources": {
                    "prod": {
                        "source_type": "snowflake",
                        "config": {
                            "account": "yaml-account",
                            "user": "yaml-user",
                            "auth_method": "password",
                        },
                        "secrets": {"password": "plain-secret"},
                    }
                }
            },
        )
        sources_path.chmod(0o600)
        monkeypatch.setenv(PLAINTEXT_SOURCE_SECRETS_ENV, "1")
        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=_FakeKeyring(),
        )

        out = resolver.resolve(
            catalog_name="snowflake",
            credential_type=SnowflakeCredentials,
            credential_id="prod",
        )

        assert out.password.get_secret_value() == "plain-secret"


# ----------------------------------------------------------------------
# Source-type mismatch
# ----------------------------------------------------------------------


class TestSourceTypeMismatch:
    def test_yaml_entry_with_wrong_source_type_raises(self, tmp_path):
        """A user storing a Snowflake credential under a Unity
        catalog name (typo / copy-paste error) MUST surface a clear
        mismatch error rather than silently coercing fields."""
        sources_path = tmp_path / "sources.yaml"
        _write_sources_yaml(
            sources_path,
            {
                "sources": {
                    "prod": {
                        "source_type": "snowflake",  # type the YAML claims
                        "config": {"account": "x", "user": "y", "auth_method": "sso"},
                    }
                }
            },
        )
        resolver = CredentialResolver(
            sources_config_path=sources_path,
            keyring_module=_FakeKeyring(),
        )
        # Looking up "prod" under unity catalog — type mismatch.
        with pytest.raises(CatalogConfigError) as exc_info:
            resolver.resolve(
                catalog_name="unity",  # different from yaml's source_type
                credential_type=UnityCredentials,
                credential_id="prod",
            )
        assert "snowflake" in exc_info.value.message
        assert "unity" in exc_info.value.message


# ----------------------------------------------------------------------
# allow_metadata_service gate
# ----------------------------------------------------------------------


class TestAllowMetadataServiceGate:
    def test_metadata_service_disabled_by_default(self, tmp_path):
        """The plan's A/B/B Choice 3 = (B): metadata-service is
        opt-in. Without ``allow_metadata_service=True``, the
        cloud-metadata path is silently skipped."""
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        assert resolver.allow_metadata_service is False

    def test_env_var_enables_metadata_service(self, tmp_path, monkeypatch):
        """``FLUID_ALLOW_METADATA_SERVICE=1`` is the env-var
        equivalent of the constructor kwarg — for CI environments
        that want to opt in once."""
        monkeypatch.setenv("FLUID_ALLOW_METADATA_SERVICE", "1")
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        assert resolver.allow_metadata_service is True


# ----------------------------------------------------------------------
# Unity-specific paths
# ----------------------------------------------------------------------


class TestUnityResolution:
    def test_env_var_oauth_m2m(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_CLIENT_ID", "client-x")
        monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "client-secret-x")
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        out = resolver.resolve(
            catalog_name="unity",
            credential_type=UnityCredentials,
        )
        assert out.host == "https://example.cloud.databricks.com"
        assert out.auth_method == "oauth_m2m"
        assert out.oauth_client_id == "client-x"
        assert out.oauth_client_secret.get_secret_value() == "client-secret-x"

    def test_env_var_pat_falls_through_to_pat_when_no_m2m(self, tmp_path, monkeypatch):
        """When only ``DATABRICKS_TOKEN`` is set, the resolver picks
        PAT auth — secondary preference behind OAuth M2M."""
        monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
        monkeypatch.setenv("DATABRICKS_TOKEN", "dapi123")
        resolver = CredentialResolver(
            sources_config_path=tmp_path / "no.yaml",
            keyring_module=_FakeKeyring(),
        )
        out = resolver.resolve(
            catalog_name="unity",
            credential_type=UnityCredentials,
        )
        assert out.auth_method == "pat"
        assert out.token.get_secret_value() == "dapi123"


# ----------------------------------------------------------------------
# Connection-kwargs translation
# ----------------------------------------------------------------------


class TestConnectionKwargsTranslation:
    def test_snowflake_password_kwargs(self):
        creds = SnowflakeCredentials(
            account="acct",
            user="usr",
            auth_method="password",
            password="secret",
            role="R",
            warehouse="WH",
        )
        kw = creds.to_connection_kwargs()
        # Raw password value reaches the SDK kwargs (the only place
        # the secret value is needed).
        assert kw["password"] == "secret"
        assert kw["account"] == "acct"
        assert kw["role"] == "R"
        assert kw["warehouse"] == "WH"

    def test_snowflake_private_key_kwargs(self):
        creds = SnowflakeCredentials(
            account="acct",
            user="usr",
            auth_method="private_key",
            private_key_path=Path("/etc/key.p8"),
            private_key_passphrase="phrase",
        )
        kw = creds.to_connection_kwargs()
        assert kw["private_key_file"] == "/etc/key.p8"
        assert kw["private_key_file_pwd"] == "phrase"
        assert "password" not in kw

    def test_snowflake_password_method_with_no_password_raises(self):
        creds = SnowflakeCredentials(
            account="acct",
            user="usr",
            auth_method="password",
        )
        with pytest.raises(CredentialNotFoundError):
            creds.to_connection_kwargs()

    def test_unity_pat_kwargs(self):
        creds = UnityCredentials(
            host="https://x",
            auth_method="pat",
            token="tok",
        )
        kw = creds.to_connection_kwargs()
        assert kw["token"] == "tok"

    def test_unity_oauth_m2m_kwargs(self):
        creds = UnityCredentials(
            host="https://x",
            auth_method="oauth_m2m",
            oauth_client_id="cid",
            oauth_client_secret="csecret",
        )
        kw = creds.to_connection_kwargs()
        assert kw["client_id"] == "cid"
        assert kw["client_secret"] == "csecret"
        assert kw["auth_type"] == "oauth-m2m"

    def test_unity_oauth_m2m_with_missing_secret_raises(self):
        creds = UnityCredentials(
            host="https://x",
            auth_method="oauth_m2m",
            oauth_client_id="cid",
            # Missing oauth_client_secret.
        )
        with pytest.raises(CredentialNotFoundError):
            creds.to_connection_kwargs()
